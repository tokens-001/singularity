#!/bin/bash
# Singularity 本地启动脚本
# 用法: bash run.sh

cd "$(dirname "$0")"

# 检查 .env 文件
if [ ! -f .env ]; then
    echo "未找到 .env 文件，请输入 DeepSeek API Key："
    read -s API_KEY
    echo "DEEPSEEK_API_KEY=$API_KEY" > .env
    echo "DAILY_LIMIT=99999" >> .env
    echo "FLASK_SECRET_KEY=local-test-$(date +%s)" >> .env
    echo ".env 已创建"
fi

echo "Singularity 启动 → http://127.0.0.1:5050"
python3 -m singularity.web.app
