#!/usr/bin/env bash
# agy-proxy 一键安装:依赖 + CA + Antigravity CLI + 后端连通性自检
# 幂等,可重复执行。仅限内部 demo 环境使用。
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f ./agyproxy.env ]; then
    cp agyproxy.env.example agyproxy.env
    echo "已从 agyproxy.env.example 生成 agyproxy.env,请先编辑其中的 CLAUDE_PROJECT 等配置后重新运行。"
    exit 1
fi
source ./agyproxy.env
case "$CLAUDE_PROJECT" in \<*) echo "请先编辑 agyproxy.env,填入真实的 CLAUDE_PROJECT"; exit 1;; esac

ok()   { echo "  ✅ $*"; }
warn() { echo "  ⚠️  $*"; }
die()  { echo "  ❌ $*" >&2; exit 1; }

echo "== [1/5] 系统依赖 =="
command -v python3 >/dev/null || die "缺少 python3"
# tmux 仅脚本化演示需要,日常使用(agy-claude 在自己终端跑)不依赖
if ! command -v tmux >/dev/null; then
    case "$(uname -s)" in
        Linux)  sudo apt-get install -y tmux >/dev/null 2>&1 || warn "tmux 未装(仅脚本化演示需要)" ;;
        Darwin) command -v brew >/dev/null && brew install -q tmux || warn "tmux 未装(仅脚本化演示需要)" ;;
    esac
fi
ok "python3$(command -v tmux >/dev/null && echo ' / tmux')"

echo "== [2/5] mitmproxy(独立 venv)=="
if [ ! -x "$MITM_VENV/bin/mitmdump" ]; then
    python3 -m venv "$MITM_VENV"
    "$MITM_VENV/bin/pip" install --quiet mitmproxy
fi
ok "$("$MITM_VENV/bin/mitmdump" --version | head -1)"

echo "== [3/5] mitmproxy CA 证书 =="
if [ ! -f "$MITM_CA" ]; then
    # 首次运行 mitmdump 会生成 CA;起一个瞬时实例(不用 timeout 命令,mac 没有)
    "$MITM_VENV/bin/mitmdump" --listen-port 0 >/dev/null 2>&1 &
    _pid=$!; sleep 4; kill "$_pid" 2>/dev/null || true; wait "$_pid" 2>/dev/null || true
fi
[ -f "$MITM_CA" ] || die "CA 生成失败"
ok "$MITM_CA"

echo "== [4/5] Antigravity CLI =="
if [ ! -x "$HOME/.local/bin/agy" ]; then
    curl -fsSL https://antigravity.google/cli/install.sh | bash
fi
ok "agy: $HOME/.local/bin/agy"

echo "== [5/5] 后端连通性自检 =="
if [ "$AGY_BACKEND" = "anthropic-vertex" ]; then
    TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null) \
        || die "gcloud ADC 不可用,请先 gcloud auth application-default login"
    HTTP=$(curl -s -o /tmp/agyproxy-selftest.json -w "%{http_code}" -X POST \
        "https://aiplatform.googleapis.com/v1/projects/${CLAUDE_PROJECT}/locations/${CLAUDE_REGION}/publishers/anthropic/models/${CLAUDE_MODEL}:rawPredict" \
        -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        -d '{"anthropic_version":"vertex-2023-10-16","max_tokens":16,"messages":[{"role":"user","content":"ping"}]}')
    [ "$HTTP" = "200" ] || die "Vertex Claude 自检失败 (HTTP $HTTP): $(head -c 200 /tmp/agyproxy-selftest.json)"
    ok "Claude ${CLAUDE_MODEL} @ ${CLAUDE_PROJECT}/${CLAUDE_REGION} 可达"
else
    warn "openai 后端跳过自检(start.sh 会拉起本地 mock)"
fi

echo
echo "安装完成。下一步:"
echo "  ./start.sh          # 启动翻译代理"
echo "  ./agy-claude        # 用 Claude 后端启动 Antigravity CLI"
echo "  ./stop.sh           # 停止全部组件"
