PY ?= .venv/bin/python

.PHONY: install test lint evals evals-live serve mcp demo-video docker lock-prompts

install:            ## create venv + install with dev extras
	python3 -m venv .venv && $(PY) -m pip install -U pip && $(PY) -m pip install -e ".[dev,browser]"

test:               ## unit + integration tests
	$(PY) -m pytest -q

lint:
	.venv/bin/ruff check src evals tests scripts finetune

evals:              ## offline golden-set evals + gate (free, deterministic)
	$(PY) -m evals.run --provider mock --gate evals/baseline.mock.json

evals-live:         ## real Claude evals + gate (needs ANTHROPIC_API_KEY, costs money)
	$(PY) -m evals.run --provider anthropic --gate evals/baseline.live.json --judge

serve:              ## web UI on http://127.0.0.1:8765
	./scripts/dev_server.sh

mcp:                ## read-only MCP server on stdio
	$(PY) -m ticketpilot.mcp_server

demo-video:         ## record the 60-second demo (needs: playwright install chromium, ffmpeg)
	$(PY) scripts/record_demo.py

lock-prompts:       ## re-pin prompt hashes after a version-bumped prompt edit
	$(PY) scripts/lock_prompts.py

docker:
	docker build -t ticketpilot . && docker run --rm -p 8000:8000 ticketpilot
