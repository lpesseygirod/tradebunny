# Grid Pilot / Prediction Ledger
#
# `make check` is the one to run before trusting anything: the whole test suite,
# then a LIVE call to OKX's public endpoints. The tests are hermetic by design —
# they stub every network boundary — which means a green suite says nothing
# about whether the exchange is reachable, the rate limit still 20/2s, or the
# candle format unchanged. The connectivity check is what closes that gap.

PY      ?= ./.venv/bin/python
VENV    ?= .venv

.DEFAULT_GOAL := help
.PHONY: help venv test check lint run supervise web scan status halt clean

help:
	@echo "make venv       create .venv and install requirements"
	@echo "make test       run the test suite (hermetic, no network)"
	@echo "make check      test suite + LIVE OKX connectivity check"
	@echo "make run        executor loop, foreground"
	@echo "make supervise  executor loop, restarted if it crashes"
	@echo "make web        approval interface"
	@echo "make scan       run the analyst once, now"
	@echo "make status     what is running, what is halted"
	@echo "make halt       stop everything"

venv:
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

test:
	$(PY) -m pytest -q

# Tests first: if the logic is broken there is no point asking the exchange
# anything. Then the live check, which is the only thing here that touches the
# network. Public endpoints only — no key, no order, nothing that can trade.
check:
	@echo "=== test suite ==============================================="
	@$(PY) -m pytest -q
	@echo
	@echo "=== live connectivity (OKX public endpoints) ================="
	@PYTHONPATH=src $(PY) -m grid.cli check

lint:
	$(PY) -m compileall -q src

run:
	PYTHONPATH=src $(PY) -m grid.cli run

supervise:
	PYTHONPATH=src $(PY) -m grid.cli supervise

web:
	PYTHONPATH=src $(PY) -m grid.cli web

scan:
	PYTHONPATH=src $(PY) -m grid.cli scan

status:
	PYTHONPATH=src $(PY) -m grid.cli status

halt:
	PYTHONPATH=src $(PY) -m grid.cli halt

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete
