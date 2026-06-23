#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "═══ Singularity Agent 调度平台 ═══"
echo ""

# 检查 .env
if [ ! -f .env ]; then
  echo "⚠ 未找到 .env 文件，创建模板..."
  cat > .env << 'EOF'
# Singularity Dispatch API Key 配置
DEEPSEEK_API_KEY=sk-your-key
ZHIPU_API_KEY=your-key
OPENAI_API_KEY=sk-your-key
KIMI_API_KEY=sk-your-key
ANTHROPIC_API_KEY=sk-ant-your-key
EOF
  echo "已创建 .env 模板，请编辑填入你的 API Key 后再启动"
  exit 1
fi

# 检查 Python
PYTHON=$(which python3 || which python)
if [ -z "$PYTHON" ]; then
  echo "❌ 未找到 Python 3"
  exit 1
fi

# 检查依赖
if ! $PYTHON -c "import flask" 2>/dev/null; then
  echo "安装依赖..."
  $PYTHON -m pip install -e . --break-system-packages
fi

# Docker 模式
if [ "$1" = "docker" ]; then
  echo "🐳 Docker 启动..."
  docker compose up -d --build
  echo "  Web: http://127.0.0.1:5050"
  echo "  停止: docker compose down"
  exit 0
fi

# 本地模式
echo "本地启动..."
echo "  Web:   http://127.0.0.1:5050"
echo "  看板:  python3 tools/dash.py"
echo "  停止:  pkill -f 'python.*app.py'"
echo ""

if [ "$1" = "-d" ] || [ "$1" = "--daemon" ]; then
  nohup $PYTHON -m singularity.web.app > /tmp/qidian.log 2>&1 &
  echo "后台运行 (PID: $!, 日志: /tmp/qidian.log)"
else
  exec $PYTHON -m singularity.web.app
fi
