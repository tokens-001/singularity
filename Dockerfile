FROM python:3.12-slim

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN pip install --no-cache-dir .

# 数据目录 (挂载点)
RUN mkdir -p /app/.qidian

EXPOSE 5050

# 默认跳过模型下载，生产环境可设 QIDIAN_SKIP_EMBED=0 启用记忆 embedding
ENV QIDIAN_SKIP_EMBED=1

CMD ["python", "-m", "singularity.web.app"]
