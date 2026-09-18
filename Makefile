.PHONY: install test lint typecheck build-frontend clean run

# 🔴 **必须优先用仓库自己的 venv**（2026-09-19 修）。
# 原来这里全是裸 `python3` / `ruff` —— 而系统的 python3 是 homebrew 的，
# **没装 singularity**，直接跑就是 `ModuleNotFoundError`；`ruff` 也只在 `.venv/bin/` 里
# ⇒ `make check`（仓库自己定义的"完成判据"）**从来跑不起来**。
# 那正是 `docs/CI与发布审计-20260919.md` §三 说的"判据不可运行，所以它不指导任何日常动作"。
PY   := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
RUFF := $(shell [ -x .venv/bin/ruff ]   && echo .venv/bin/ruff   || echo ruff)

install:
	pip install -e ".[dev]"
	cd src/singularity/web/frontend && npm install

run:
	$(PY) -m singularity.web.app

test:
	$(PY) -m pytest tests/test_scheduler/ -q --tb=short

test-fast:
	$(PY) -m pytest tests/test_scheduler/test_core.py tests/test_scheduler/test_router.py tests/test_scheduler/test_model_registry.py tests/test_scheduler/test_project.py -q

test-all:
	$(PY) -m pytest tests/test_scheduler/ -q --tb=short
	$(PY) tests/test_scheduler/test_step4_execution.py
	$(PY) tests/test_scheduler/test_step5_verification.py

lint:
	$(RUFF) check src/singularity/

lint-fix:
	$(RUFF) check --fix src/singularity/

typecheck:
	$(PY) -m mypy src/singularity/

format:
	$(RUFF) format src/singularity/

build-frontend:
	cd src/singularity/web/frontend && npm run build

check: lint test-fast
	@echo "✅ all checks passed"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf src/singularity/web/static/dist/
