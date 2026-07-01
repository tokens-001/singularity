.PHONY: install test lint typecheck build-frontend clean run

install:
	pip install -e ".[dev]"
	cd src/singularity/web/frontend && npm install

run:
	python3 -m singularity.web.app

test:
	python3 -m pytest tests/test_scheduler/ -q --tb=short

test-fast:
	python3 -m pytest tests/test_scheduler/test_core.py tests/test_scheduler/test_router.py tests/test_scheduler/test_model_registry.py tests/test_scheduler/test_project.py -q

test-all:
	python3 -m pytest tests/test_scheduler/ -q --tb=short
	python3 tests/test_scheduler/test_step4_execution.py
	python3 tests/test_scheduler/test_step5_verification.py

lint:
	ruff check src/singularity/

lint-fix:
	ruff check --fix src/singularity/

typecheck:
	mypy src/singularity/

format:
	ruff format src/singularity/

build-frontend:
	cd src/singularity/web/frontend && npm run build

check: lint test-fast
	@echo "✅ all checks passed"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf src/singularity/web/static/dist/
