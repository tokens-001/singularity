#!/bin/bash
# 奇点 pre-commit 钩子: commit 前强制跑全量测试，不绿不提交
# Hook: PreToolUse (matcher: Bash) — 只拦截 git commit

INPUT=$(cat)
CMD=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null)

# 只拦截 git commit，其他 bash 命令放行
if ! echo "$CMD" | grep -qE "^git commit|git commit "; then
  exit 0
fi

echo "🧪 奇点 pre-commit: 跑全量测试..."

cd /Users/jingzhe/奇点/python
QIDIAN_SKIP_EMBED=1 python3 smoke_test.py 2>&1 | tail -1 | grep -q "全通过"
if [ $? -ne 0 ]; then
  echo "❌ smoke_test.py 未通过! Commit 被阻止。"
  exit 1
fi

QIDIAN_SKIP_EMBED=1 python3 test_exec_run.py 2>&1 | tail -1 | grep -q "全通过"
if [ $? -ne 0 ]; then
  echo "❌ test_exec_run.py 未通过! Commit 被阻止。"
  exit 1
fi

QIDIAN_SKIP_EMBED=1 python3 unit_tests.py 2>&1 | tail -1 | grep -q "OK"
if [ $? -ne 0 ]; then
  echo "❌ unit_tests.py 未通过! Commit 被阻止。"
  exit 1
fi

echo "✅ 88/88 全绿! 允许提交。"
exit 0
