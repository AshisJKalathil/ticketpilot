"""Carrier integrations: a JSON API where one exists, a browser where it doesn't.

* **SwiftShip** has an API (simulated here, including realistic 503s).
* **PacificParcel** has *no API* - only a public tracking web page. The
  browser fallback loads the page and extracts the data from the DOM:

    - ``playwright`` engine: headless Chromium, the right tool for real sites
      with JavaScript, logins or anti-bot redirects;
    - ``http`` engine: plain HTTP + HTML parsing, for simple server-rendered
      pages and small deploy images.

  Both engines enforce an egress allowlist and run inside the process sandbox.
  Everything scraped is third-party content and is treated as untrusted.
"""

from __future__ import annotations

import json
import re
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urlparse

from ticketpilot.retry import TransientError
from ticketpilot.store import Store
from ticketpilot.tools.base import PermissionDenied, ToolContext, ToolError

API_CARRIERS = {"swiftship"}


def _eta(events: list[dict]) -> str | None:
    for ev in reversed(events):
        m = re.search(r"estimated delivery (\d{4}-\d{2}-\d{2})", ev["description"])
        if m:
            return m.group(1)
    return None


def _days_since(day: str, today: date) -> int:
    return (today - date.fromisoformat(day)).days


# --- SwiftShip: has an API --------------------------------------------------


def swiftship_api(ctx: ToolContext, tracking_number: str) -> dict:
    failures = int(ctx.settings.fault_injection.get("swiftship_failures", 0))
    if ctx.attempt <= failures:
        raise TransientError("SwiftShip API 503 Service Unavailable", retry_after=None)
    row = ctx.store.one("SELECT * FROM shipments WHERE tracking_number=? AND carrier='swiftship'", (tracking_number,))
    if not row:
        raise ToolError(f"SwiftShip has no record of {tracking_number}")
    events = json.loads(row["events_json"])
    return {"status": row["status"], "events": events, "estimated_delivery": _eta(events)}


# --- PacificParcel: browser fallback -----------------------------------------


class _PortalParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields: dict[str, str] = {}
        self.rows: list[list[str]] = []
        self._capture: str | None = None
        self._in_events = False
        self._cell: list[str] | None = None
        self._row: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id") in {"status", "eta", "tracking", "carrier-notes"}:
            self._capture = a["id"]
            self.fields[self._capture] = ""
        if tag == "table" and a.get("id") == "events":
            self._in_events = True
        elif self._in_events and tag == "tr":
            self._row = []
        elif self._in_events and tag == "td":
            self._cell = []

    def handle_endtag(self, tag):
        if tag in {"span", "div"}:
            self._capture = None
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row:
            self.rows.append(self._row)
            self._row = None
        elif tag == "table":
            self._in_events = False

    def handle_data(self, data):
        if self._capture:
            self.fields[self._capture] += data
        if self._cell is not None:
            self._cell.append(data)


STATUS_FROM_LABEL = {"in transit": "in_transit", "delivered": "delivered", "exception": "exception",
                     "label created": "label_created"}


def _check_egress(url: str, allowed: list[str]) -> None:
    host = urlparse(url).hostname
    if host not in allowed:
        raise PermissionDenied(f"egress to {host} is not on the allowlist {allowed}")


def _fetch_html(ctx: ToolContext, tracking_number: str) -> str:
    base = ctx.settings.portal_base_url
    if base == "inproc":
        from ticketpilot.web.carrier_portal import render_tracking_page

        status, html = render_tracking_page(ctx.store, tracking_number)
        if status == 404:
            raise ToolError(f"PacificParcel has no record of {tracking_number}")
        return html
    url = f"{base.rstrip('/')}/carrier/pacificparcel/track?n={tracking_number}"
    _check_egress(url, ctx.allowed_hosts)
    import httpx

    try:
        resp = httpx.get(url, timeout=8.0, follow_redirects=False, headers={"User-Agent": "TicketPilot/0.1"})
    except httpx.TransportError as exc:
        raise TransientError(f"PacificParcel portal unreachable: {exc}") from exc
    if resp.status_code >= 500:
        raise TransientError(f"PacificParcel portal returned {resp.status_code}")
    if resp.status_code == 404:
        raise ToolError(f"PacificParcel has no record of {tracking_number}")
    return resp.text


def _extract_with_playwright(ctx: ToolContext, tracking_number: str) -> dict:
    from playwright.sync_api import sync_playwright

    base = ctx.settings.portal_base_url
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            allowed = ctx.allowed_hosts

            def guard(route):
                host = urlparse(route.request.url).hostname
                return route.continue_() if host in allowed else route.abort()

            page.route("**/*", guard)
            if base == "inproc":
                page.set_content(_fetch_html(ctx, tracking_number))
            else:
                url = f"{base.rstrip('/')}/carrier/pacificparcel/track?n={tracking_number}"
                _check_egress(url, allowed)
                resp = page.goto(url, timeout=8000)
                if resp is None or resp.status >= 500:
                    raise TransientError("PacificParcel portal unavailable")
                if resp.status == 404:
                    raise ToolError(f"PacificParcel has no record of {tracking_number}")
            rows = page.locator("table#events tr").all()
            events = []
            for row in rows[1:]:
                cells = [c.inner_text().strip() for c in row.locator("td").all()]
                if len(cells) == 3:
                    events.append(cells)
            return {
                "status": page.locator("#status").inner_text().strip(),
                "eta": page.locator("#eta").inner_text().strip(),
                # text_content (not inner_text) also returns hidden text - exactly what an
                # attacker hides from humans, so we must see it to screen it.
                "notes": (page.locator("#carrier-notes").text_content() or "").strip(),
                "events": events,
            }
        finally:
            browser.close()


def pacificparcel_browser(ctx: ToolContext, tracking_number: str) -> dict:
    if ctx.settings.browser_engine == "playwright":
        raw = _extract_with_playwright(ctx, tracking_number)
    else:
        parser = _PortalParser()
        parser.feed(_fetch_html(ctx, tracking_number))
        raw = {"status": parser.fields.get("status", "").strip(), "eta": parser.fields.get("eta", "").strip(),
               "notes": parser.fields.get("carrier-notes", "").strip(), "events": parser.rows}
    status = STATUS_FROM_LABEL.get(raw["status"].lower())
    if status is None:
        # The page changed shape: fail loudly rather than guess.
        raise ToolError(f"PacificParcel page layout not recognised (status={raw['status']!r})")
    events = [{"date": d, "location": loc, "description": desc} for d, loc, desc in raw["events"]]
    eta = raw["eta"] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw["eta"] or "") else None
    return {"status": status, "events": events, "estimated_delivery": eta, "notes": raw["notes"] or None}


def track(ctx: ToolContext, store: Store, carrier: str, tracking_number: str) -> tuple[dict, str]:
    if carrier in API_CARRIERS:
        return swiftship_api(ctx, tracking_number), "api"
    return pacificparcel_browser(ctx, tracking_number), "browser"
