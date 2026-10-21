.PHONY: test chat eval publish-results

PYTHON ?= python3

test:
	$(PYTHON) -m pytest tests/ -v

chat:
	$(PYTHON) -m app.agent

eval:
	PYTHONPATH=. $(PYTHON) eval/run_eval.py

publish-results:
	@mkdir -p eval/published
	@if [ -d eval/runs/latest ]; then \
		for f in results.jsonl RESULTS.md checkpoint.json config.json; do \
			if [ -f eval/runs/latest/$$f ]; then \
				cp eval/runs/latest/$$f eval/published/$$f; \
			fi; \
		done; \
		echo "Published results from eval/runs/latest/ to eval/published/"; \
	else \
		echo "No eval runs found in eval/runs/latest to publish."; \
	fi

