.PHONY: install test lint typecheck build-frontend clean run audit audit-selftest audit-shapes

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

# 🔴 审计脚本**接电**（2026-09-20）。之前 `scripts/` 下三个脚本自带自测，
#    但 CI 0 次命中、Makefile 0 次命中 —— **工具写了、自测写了，没人跑** ⇒
#    改坏了不会红，跟没写一样（`docs/外派评审-20260920.md` 第三节排序的第 0 步）。
audit-selftest:
	$(PY) scripts/test_preflight.py
	$(PY) scripts/test_delivery_facts.py
	$(PY) scripts/test_dispatch.py

# 静态形状扫的**棘轮**：只有新出现的形状才红。
# ⚠️ 别改成裸 `preflight.py shapes` —— 它对已知的 4 条也返回 1，是**恒红**，
#    而恒红的门下一步就是被 `|| true` 掉（`docs/防御模式.md` §78：假门比没门坏）。
audit-shapes:
	$(PY) scripts/preflight.py shapes --baseline scripts/preflight-baseline.json

audit: audit-selftest audit-shapes
	@echo "✅ audit passed"

check: lint test-fast audit
	@echo "✅ all checks passed"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf src/singularity/web/static/dist/
