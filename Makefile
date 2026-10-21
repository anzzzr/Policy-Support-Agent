.PHONY: test chat eval publish-results validate-scenarios manifest

PYTHON ?= python3

test:
	$(PYTHON) -m pytest tests/ -v

chat:
	$(PYTHON) -m app.agent

eval:
	PYTHONPATH=. $(PYTHON) eval/run_eval.py

publish-results:
	@mkdir -p eval/published
	@target_dir=$${RUN_DIR:-eval/runs/latest}; \
	if [ -d "$$target_dir" ]; then \
		if [ -f "$$target_dir/.smoke" ] || grep -Eq '"smoke"[[:space:]]*:[[:space:]]*true' "$$target_dir"/*.jsonl 2>/dev/null; then \
			echo "Cannot publish smoke run results: $$target_dir contains smoke rows."; \
			exit 1; \
		fi; \
		for f in results.jsonl results_heldout.jsonl RESULTS.md RESULTS_HELDOUT.md checkpoint.json config.json; do \
			if [ -f "$$target_dir/$$f" ]; then \
				cp "$$target_dir/$$f" eval/published/$$f; \
			fi; \
		done; \
		echo "Published results from $$target_dir to eval/published/"; \
	else \
		echo "No eval runs found in $$target_dir to publish."; \
		exit 1; \
	fi

validate-scenarios:
	PYTHONPATH=. $(PYTHON) eval/validate_scenarios.py

manifest:
	@$(PYTHON) -c 'import hashlib, pathlib; p = pathlib.Path("scenarios"); files = sorted(p.glob("*.yaml")); lines = [f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}\n" for f in files]; (p / "MANIFEST.sha256").write_text("".join(lines)); print(f"Generated scenarios/MANIFEST.sha256 with {len(lines)} files.")'



