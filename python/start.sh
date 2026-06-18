#!/bin/bash
cd "$(dirname "$0")"
echo "奇点调度平台启动..."
echo "  Web: http://127.0.0.1:5050"
echo "  PID: $$"
nohup python3 app.py > /tmp/qidian.log 2>&1 &
echo "后台运行中 (日志: /tmp/qidian.log)"
echo "停止: pkill -f 'python.*app.py'"
