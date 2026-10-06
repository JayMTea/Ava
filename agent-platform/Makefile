# Convenience aliases. Every target is a plain Python command, so `make` is optional -
# on Windows without make, run the command on the right directly.
PY ?= python

.PHONY: setup check fast validate test lint evals demo agents

setup:     ## install with dev + Anthropic extras
	$(PY) -m pip install -e ".[dev,anthropic]"

check:     ## everything CI runs
	$(PY) scripts/check.py

fast:      ## config + tests only
	$(PY) scripts/check.py --fast

validate:
	$(PY) -m app.cli validate

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check .

evals:     ## offline evals (mock model)
	$(PY) -m evals.run --route test

demo:      ## one offline run, no API keys needed
	$(PY) -m app.cli run "What can you help with?" --route test

agents:
	$(PY) -m app.cli agents
