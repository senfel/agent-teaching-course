# Alex · 第 15 期 Docker 镜像
#
# 把"本地能跑的 Web 服务"打成"一次构建、处处可跑"的镜像:
#   docker build -t alex-web .
#   docker run --env-file .env -p 8000:8000 alex-web
#
# 分层思路:依赖最稳、变化最少 → 放最前面单独一层,构建缓存命中时
# 重装依赖就被跳过;源码最后 COPY,改代码只重建最后一层。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AGENT_WEB_HOST=0.0.0.0 \
    AGENT_WEB_PORT=8000

WORKDIR /app

# ① 依赖层(独立一层,依赖不变则不重装)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# ② 运行时资源(只拷 step15 包 + 人设/技能/MCP 服务器,不打进其它 step 代码)
COPY code/step15_deploy/ /app/code/step15_deploy/
COPY templates/ /app/templates/
COPY skills/ /app/skills/
COPY mcp_servers/ /app/mcp_servers/

# ③ 记忆目录:运行时用挂卷持久化,这里只建空目录
RUN mkdir -p /app/memory

# ④ 默认启动 Web(cwd 必须在 /app/code,相对导入与 python -m 才成立);
#    想在容器里跑 CLI: docker run ... alex-web python -m step15_deploy --cli
WORKDIR /app/code
EXPOSE 8000

CMD ["python", "-m", "step15_deploy"]