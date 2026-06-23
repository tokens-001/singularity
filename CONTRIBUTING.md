# Contributing

## Quick Start
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

## Pre-commit Checks
All commits must pass:
```bash
ruff check src/ && ruff format --check src/
mypy src/singularity/
pytest tests/ -q
python3 tests/test_exec_run.py
```

## Code Style
- Python 3.11+, `ruff` formatted
- Absolute imports: `from singularity.scheduler.x import y`
- No bare `except:`, use `except Exception:`
- Type hints on public functions

## Testing
```bash
pytest tests/ -q                # Unit tests (75)
python3 tests/test_exec_run.py  # Exec regression (21)
QIDIAN_SKIP_EMBED=1 python3 tests/smoke_test.py  # Integration (45, needs server)
```

## Release
1. Update version in `pyproject.toml`
2. Add entry to `CHANGELOG.md`
3. Run full test suite
4. Tag: `git tag v$(python -c "from importlib.metadata import version; print(version('singularity'))")`
