# Changelog

## [2.1.156] — 2026-06-23

### Added
- `src/` layout with `pyproject.toml` (PEP 621 standard)
- Anthropic Messages API executor (`anthropic_api.py`)
- Codegraph integration for code structure awareness
- Diff hard rules: deleted tests, weakened security, bare except detection
- Cost tracking with per-model pricing and `/api/cost` endpoint
- Quality trends endpoint `/api/quality/trends`
- Provider health probe (`api_store.probe()`)
- Route learner integration in dispatcher agent selection
- `ruff` lint gate in supervisor artifact check
- Pytest test gate in supervisor (replaces `py_compile` only)
- Convergence detection in review-fix loop
- Orphan worktree cleanup on startup
- Git subprocess timeout (60s)
- Pre-commit config (`.pre-commit-config.yaml`)

### Changed
- All imports: relative → absolute (`from singularity.scheduler.x import y`)
- Judge fail-safe: defaults to **block** instead of pass when unavailable
- Conductor autopilot: exponential backoff polling (was fixed 300s timeout)
- Model isolation enforced in supervisor
- `sys.path.insert` removed, replaced by `pip install -e .`

### Removed
- `python/` flat directory layout
- `claude-cli` hardcoded binary path dependency (Anthropic API executor as replacement)
- `mypy.ini` → merged into `pyproject.toml`
- `requirements.txt` → merged into `pyproject.toml`

### Structure
```
Singularity/
├── pyproject.toml
├── CHANGELOG.md
├── src/singularity/{scheduler,web,skills}
├── tests/{test_scheduler,test_exec_run,smoke_test}
├── tools/
├── data/
├── docs/
└── .github/workflows/test.yml
```
