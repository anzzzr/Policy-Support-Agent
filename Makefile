.PHONY: test chat eval

PYTHON ?= python3

test:
	$(PYTHON) -m pytest tests/ -v

chat:
	$(PYTHON) -m app.agent

eval:
	$(PYTHON) eval/run_eval.py
