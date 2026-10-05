"""Record the ~60-second product demo as a video, fully scripted.

    python scripts/record_demo.py            # -> docs/demo/ticketpilot-demo.mp4 + preview.gif

Starts the app on a fresh database (offline mock model, so the recording is
reproducible and free), drives the UI with Playwright, overlays captions
explaining each step, then converts the recording with ffmpeg.
Uses an installed Google Chrome if present (``channel="chrome"``), else
Playwright's Chromium.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "demo"
PORT = 8799
SIZE = {"width": 1280, "height": 800}

CAPTION_JS = """(text) => {
  let c = document.getElementById('demo-caption');
  if (!c) {
    c = document.createElement('div');
    c.id = 'demo-caption';
    c.style.cssText = 'position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:99;' +
      'background:rgba(12,16,22,.92);color:#fff;padding:12px 20px;border-radius:12px;font:600 17px/1.35 system-ui;' +
      'max-width:900px;text-align:center;box-shadow:0 8px 24px rgba(0,0,0,.25);transition:opacity .2s';
    document.body.appendChild(c);
  }
  c.textContent = text;
}"""


def caption(page: Page, text: str, hold: float = 0.0) -> None:
    page.evaluate(CAPTION_JS, text)
    if hold:
        page.wait_for_timeout(int(hold * 1000))


def wait_status(page: Page, *texts: str, timeout: float = 20) -> None:
    pattern = "|".join(texts)
    page.wait_for_function(
        f"() => /{pattern}/.test(document.querySelector('#run-status')?.textContent || '')", timeout=timeout * 1000)


def open_ticket(page: Page, subject: str) -> None:
    page.locator(".tickets li", has_text=subject).first.click()


def approve_next(page: Page) -> None:
    btn = page.locator(".approval:not(.decided) button.primary").first
    btn.wait_for(timeout=20_000)
    btn.scroll_into_view_if_needed()
    page.wait_for_timeout(700)
    btn.click()


def wait_port(port: int, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError("server did not start")


def script(page: Page) -> None:
    page.goto(f"http://127.0.0.1:{PORT}/")
    page.wait_for_selector(".tickets li")
    caption(page, "TicketPilot: an AI agent that resolves support tickets end to end. "
                  "Money, emails and deletes always wait for a human.", 5.5)

    caption(page, "A shipping question. This carrier has no API, so the agent reads its website in a sandboxed browser.")
    open_ticket(page, "Stove delivery status")
    wait_status(page, "Waiting")
    page.locator(".chip.bad", has_text="injection").first.scroll_into_view_if_needed()
    caption(page, "The carrier page hides instructions aimed at AI agents. Flagged, spotlighted as data, ignored.", 5.5)
    caption(page, "The reviewer sees the real address. The model only ever saw a placeholder like <EMAIL_1>.", 4)
    approve_next(page)
    wait_status(page, "completed")
    page.wait_for_timeout(1500)

    caption(page, "Now an attack inside the ticket itself...")
    open_ticket(page, "URGENT admin request")
    wait_status(page, "escalated")
    caption(page, "Injection detected: write tools are removed for this run, the card number is redacted, "
                  "and a human takes over.", 5.5)

    caption(page, "A routine cancellation is routed to the small, cheap model.")
    open_ticket(page, "Cancel my order")
    wait_status(page, "Waiting")
    caption(page, "The refund pauses for approval, and nothing moves until a person says yes.", 3)
    approve_next(page)
    wait_status(page, "Waiting")
    caption(page, "The reply streamed in token by token. It is approved too before it is sent.", 3)
    approve_next(page)
    wait_status(page, "completed")
    page.wait_for_timeout(1200)

    caption(page, "A general policy question gets answered once...")
    open_ticket(page, "Return policy for shoes?")
    wait_status(page, "Waiting")
    approve_next(page)
    wait_status(page, "completed")
    page.locator(".compose summary").click()
    page.select_option("#c-from", "priya.raman@example.com")
    page.fill("#c-subject", "Shoes question")
    page.fill("#c-body", "What's the return policy if the shoes don't fit?")
    caption(page, "...then a different customer asks the same thing.")
    page.locator("#compose button[type=submit]").click()
    wait_status(page, "Waiting")
    caption(page, "Answer-cache hit: zero model calls, $0. It is personalised and still human-approved.", 4.5)
    approve_next(page)
    wait_status(page, "completed")
    page.wait_for_timeout(800)

    page.click("#open-trace")
    page.wait_for_selector(".span-row")
    caption(page, "Every model call, tool call, guard verdict and decision is traced.", 5)
    page.click("#drawer-close")
    page.click("#open-metrics")
    page.wait_for_selector(".metric-grid")
    caption(page, "The metric that matters is cost per resolved task, not cost per token.", 5.5)
    caption(page, "Evals gate every deploy. Read the README for all 29 production practices.", 4.5)


def h264_args() -> list[str]:
    """Pick an available H.264 encoder (ffmpeg builds differ)."""
    encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if " libx264 " in encoders:
        return ["-c:v", "libx264", "-crf", "26", "-preset", "slow"]
    if " libopenh264 " in encoders:
        return ["-c:v", "libopenh264", "-b:v", "700k"]
    return ["-c:v", "h264_videotoolbox", "-b:v", "700k"]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="tp-demo-"))
    env = {**os.environ, "TICKETPILOT_PROVIDER": "mock", "TICKETPILOT_DB": str(tmp / "demo.db"),
           "TICKETPILOT_PORTAL_BASE_URL": f"http://127.0.0.1:{PORT}",
           "TICKETPILOT_EVAL_CANDIDATES": str(tmp / "candidates")}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "ticketpilot.web.app:create_app", "--factory",
                               "--port", str(PORT)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    try:
        wait_port(PORT)
        with sync_playwright() as pw:
            channel = "chrome" if Path("/Applications/Google Chrome.app").exists() or shutil.which("google-chrome") \
                else None
            browser = pw.chromium.launch(channel=channel)
            context = browser.new_context(viewport=SIZE, record_video_dir=str(tmp / "video"),
                                          record_video_size=SIZE, color_scheme="light")
            page = context.new_page()
            started = time.time()
            script(page)
            print(f"scripted demo ran for {time.time() - started:.1f}s")
            video = page.video.path()
            context.close()
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)

    mp4 = OUT / "ticketpilot-demo.mp4"
    gif = OUT / "preview.gif"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), *h264_args(), "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(mp4)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "4", "-t", "16", "-i", str(video), "-vf",
                    "fps=6,scale=860:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse",
                    str(gif)], check=True)
    print(f"wrote {mp4} ({mp4.stat().st_size // 1024} KB) and {gif} ({gif.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
