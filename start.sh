#!/usr/bin/env bash
# 启动翻译代理(以及 openai 后端模式下的本地 mock)
set -euo pipefail
cd "$(dirname "$0")"
source ./agyproxy.env

./stop.sh >/dev/null 2>&1 || true

if [ "$AGY_BACKEND" = "openai" ] && [[ "$OPENAI_BASE_URL" == *"127.0.0.1:19090"* ]]; then
    nohup python3 mock_openai.py > mock.out 2>&1 &
    echo $! > .mock.pid
    echo "mock 后端已启动 (pid $(cat .mock.pid), :19090)"
fi

# env -u: 防止 shell 里残留的 SSL_CERT_FILE(常见于手动跑过 agy 的终端)
# 泄漏进 mitmdump —— 那会让后端出连接只信任 mitm CA,必然 CERTIFICATE_VERIFY_FAILED
AGY_TRANSLATE=1 AGY_BACKEND="$AGY_BACKEND" \
CLAUDE_PROJECT="$CLAUDE_PROJECT" CLAUDE_REGION="$CLAUDE_REGION" CLAUDE_MODEL="$CLAUDE_MODEL" \
OPENAI_BASE_URL="$OPENAI_BASE_URL" OPENAI_API_KEY="$OPENAI_API_KEY" NEW_MODEL_ID="$NEW_MODEL_ID" \
AGY_BACKEND_CA_BUNDLE="${AGY_BACKEND_CA_BUNDLE:-}" \
nohup env -u SSL_CERT_FILE -u REQUESTS_CA_BUNDLE -u CURL_CA_BUNDLE \
    "$MITM_VENV/bin/mitmdump" --listen-port "$PROXY_PORT" -s translate.py \
    --set stream_large_bodies=5m > mitmdump.out 2>&1 &
echo $! > .mitm.pid

sleep 2
kill -0 "$(cat .mitm.pid)" 2>/dev/null || { echo "代理启动失败,见 mitmdump.out"; exit 1; }
curl -s -x "http://127.0.0.1:$PROXY_PORT" --cacert "$MITM_CA" https://www.google.com \
    -o /dev/null -w "代理已就绪 (:$PROXY_PORT, 透传自检 HTTP %{http_code})\n"

echo "后端: $AGY_BACKEND$([ "$AGY_BACKEND" = anthropic-vertex ] && echo " → $CLAUDE_MODEL @ $CLAUDE_PROJECT")"
echo "启动 Antigravity: ./agy-claude   (或手动: HTTPS_PROXY=http://127.0.0.1:$PROXY_PORT SSL_CERT_FILE=$MITM_CA agy)"
