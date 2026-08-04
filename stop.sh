#!/usr/bin/env bash
# 停止翻译代理与 mock 后端(不动 tmux 里的 agy 会话)
cd "$(dirname "$0")"
for f in .mitm.pid .mock.pid; do
    if [ -f "$f" ]; then
        kill "$(cat "$f")" 2>/dev/null && echo "已停止 $(basename "$f" .pid) (pid $(cat "$f"))"
        rm -f "$f"
    fi
done
# 兜底:清理游离实例
pkill -f "mitmdump --listen-port 18080" 2>/dev/null || true
pkill -f "mock_openai.py" 2>/dev/null || true
exit 0
