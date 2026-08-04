"""Stand-in "new model" backend: minimal OpenAI-compatible /v1/chat/completions.

Replies in text only (no tool calls) — enough to prove the end-to-end path:
agy TUI -> mitmproxy -> translation -> this backend -> translated SSE -> agy TUI.
Swap OPENAI_BASE_URL to a real endpoint for the actual new model.
"""
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 19090


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        last_user = next((m["content"] for m in reversed(body.get("messages", []))
                          if m.get("role") == "user" and m.get("content")), "")
        n_msgs = len(body.get("messages", []))
        n_tools = len(body.get("tools", []))
        reply = (
            "【NewModel-Preview 已接管本次推理】\n"
            f"我不是 Gemini —— 这条回复由本地代理翻译层转发给自有模型后端生成。\n"
            f"收到上下文:{n_msgs} 条消息、{n_tools} 个工具定义。\n"
            f"你最后说的是:{last_user[:200]!r}"
        )
        resp = {
            "id": f"chatcmpl-mock-{int(time.time())}",
            "object": "chat.completion",
            "model": body.get("model", "newmodel-preview"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": reply}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        data = json.dumps(resp, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print(f"[mock-backend] {fmt % args}")


if __name__ == "__main__":
    print(f"[mock-backend] listening on 127.0.0.1:{PORT}")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
