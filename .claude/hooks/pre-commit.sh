#!/bin/bash
# Singularity pre-commit hook: run full test suite, block commit on failure
# Hook: PreToolUse (matcher: Bash) — only intercepts git commit

INPUT=$(cat)
CMD=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null)

# Only intercept git commit
if ! echo "$CMD" | grep -qE "^git commit|git commit "; then
  exit 0
fi

echo "🧪 Singularity pre-commit: running full test suite..."

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

QIDIAN_SKIP_EMBED=1 python3 -m pytest tests/unit_tests.py -q 2>&1 | tail -1 | grep -qE "passed|OK"
if [ $? -ne 0 ]; then
  echo "❌ unit tests failed! Commit blocked."
  exit 1
fi

QIDIAN_SKIP_EMBED=1 python3 tests/test_exec_run.py 2>&1 | tail -1 | grep -q "全通过"
if [ $? -ne 0 ]; then
  echo "❌ exec tests failed! Commit blocked."
  exit 1
fi

echo "✅ all tests passed!"
exit 0
