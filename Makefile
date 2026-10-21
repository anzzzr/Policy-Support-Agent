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
		cp -r eval/runs/latest/* eval/published/; \
		echo "Published results from eval/runs/latest/ to eval/published/"; \
	else \
		echo "No eval runs found in eval/runs/latest to publish."; \
	fi

