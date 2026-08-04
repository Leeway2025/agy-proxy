"""mitmproxy addon — inference translation for Antigravity CLI.

Policy:
  * Every flow EXCEPT model inference is passed through untouched
    (auth, feature flags, telemetry, updater, experiments — all pristine).
  * Model inference calls (`:generateContent` / `:streamGenerateContent` on
    cloudcode-pa / aiplatform) are answered locally by the configured backend,
    translated to/from the Gemini wire format. Fail-open: any translation
    error passes the request through to the real Google backend.

Backends (env AGY_BACKEND):
  anthropic-vertex (default here) — Claude via Vertex AI rawPredict, ADC auth.
      CLAUDE_PROJECT / CLAUDE_REGION / CLAUDE_MODEL configure it.
  openai — any OpenAI-compatible endpoint.
      OPENAI_BASE_URL / OPENAI_API_KEY / NEW_MODEL_ID configure it.
"""
import gzip
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from mitmproxy import ctx, http

_HERE = Path(__file__).resolve().parent
DUMP_DIR = _HERE / "dumps"
DUMP_DIR.mkdir(parents=True, exist_ok=True)
FLOWLOG = str(_HERE / "flows.log")

TRANSLATE = os.environ.get("AGY_TRANSLATE", "0") == "1"
BACKEND = os.environ.get("AGY_BACKEND", "anthropic-vertex")

OPENAI_BASE = os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:19090/v1")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "dummy")
NEW_MODEL = os.environ.get("NEW_MODEL_ID", "newmodel-preview")

CLAUDE_PROJECT = os.environ.get("CLAUDE_PROJECT", "")
CLAUDE_REGION = os.environ.get("CLAUDE_REGION", "global")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")

INFERENCE_HOSTS = ("cloudcode-pa.googleapis.com", "aiplatform.googleapis.com")
INFERENCE_MARKERS = ("generatecontent",)


def is_inference(flow: http.HTTPFlow) -> bool:
    host = flow.request.pretty_host.lower()
    return any(host.endswith(h) for h in INFERENCE_HOSTS) and any(
        m in flow.request.path.lower() for m in INFERENCE_MARKERS
    )


def flowlog(entry: dict) -> None:
    with open(FLOWLOG, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def body_bytes(msg) -> bytes:
    raw = msg.raw_content or b""
    if msg.headers.get("content-encoding", "") == "gzip":
        try:
            return gzip.decompress(raw)
        except OSError:
            pass
    return msg.content or raw


def dump(name: str, data: bytes) -> None:
    (DUMP_DIR / f"{int(time.time()*1000)}-{name}").write_bytes(data)


def parts_text(parts) -> str:
    return "\n".join(p.get("text", "") for p in parts or [] if p.get("text"))


def lc_schema(node):
    """Gemini schemas use UPPERCASE types; JSON Schema wants lowercase."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "type" and isinstance(v, str):
                out[k] = v.lower()
            else:
                out[k] = lc_schema(v)
        return out
    if isinstance(node, list):
        return [lc_schema(x) for x in node]
    return node


# ---------------- Gemini -> Anthropic Messages ----------------

def to_anthropic(ca_body: dict) -> dict:
    req = ca_body.get("request", ca_body)
    out = {
        "anthropic_version": "vertex-2023-10-16",
        "max_tokens": 16000,
        "messages": [],
    }
    sysins = req.get("systemInstruction")
    if sysins:
        txt = parts_text(sysins.get("parts")) if isinstance(sysins, dict) else str(sysins)
        if txt:
            out["system"] = txt

    pending_tool_ids = []  # tool_use ids awaiting a tool_result, FIFO
    for content in req.get("contents", []):
        role = "assistant" if content.get("role") == "model" else "user"
        blocks, result_blocks = [], []
        for p in content.get("parts", []):
            if p.get("text"):
                blocks.append({"type": "text", "text": p["text"]})
            elif "functionCall" in p:
                fc = p["functionCall"]
                tid = fc.get("id") or f"toolu_gen_{len(out['messages'])}_{len(blocks)}"
                pending_tool_ids.append(tid)
                blocks.append({"type": "tool_use", "id": tid,
                               "name": fc["name"], "input": fc.get("args", {})})
            elif "functionResponse" in p:
                fr = p["functionResponse"]
                tid = fr.get("id") or (pending_tool_ids.pop(0) if pending_tool_ids
                                       else "toolu_unknown")
                if fr.get("id") and tid in pending_tool_ids:
                    pending_tool_ids.remove(tid)
                # tool_result MUST live in a user message on the Anthropic API,
                # regardless of the role Gemini wire format used
                result_blocks.append({
                    "type": "tool_result", "tool_use_id": tid,
                    "content": json.dumps(fr.get("response", {}), ensure_ascii=False),
                })
        if blocks:
            out["messages"].append({"role": role, "content": blocks})
        if result_blocks:
            out["messages"].append({"role": "user", "content": result_blocks})

    tools = []
    for t in req.get("tools", []):
        for fd in t.get("functionDeclarations", []):
            schema = lc_schema(fd.get("parameters") or {"type": "object", "properties": {}})
            tools.append({
                "name": fd["name"],
                "description": fd.get("description", ""),
                "input_schema": schema,
            })
    if tools:
        out["tools"] = tools
    return out


def gcloud_token(_cache={}) -> str:
    if _cache.get("exp", 0) > time.time():
        return _cache["tok"]
    tok = subprocess.check_output(
        ["gcloud", "auth", "application-default", "print-access-token"],
        text=True, timeout=30,
    ).strip()
    _cache.update(tok=tok, exp=time.time() + 1500)
    return tok


def call_claude_vertex(a_req: dict) -> dict:
    url = (f"https://aiplatform.googleapis.com/v1/projects/{CLAUDE_PROJECT}"
           f"/locations/{CLAUDE_REGION}/publishers/anthropic/models/"
           f"{CLAUDE_MODEL}:rawPredict")
    req = urllib.request.Request(
        url, data=json.dumps(a_req).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {gcloud_token()}"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def anthropic_to_gcr(a_resp: dict) -> dict:
    parts = []
    for block in a_resp.get("content", []):
        if block["type"] == "text" and block.get("text"):
            parts.append({"text": block["text"]})
        elif block["type"] == "tool_use":
            parts.append({"functionCall": {
                "id": block.get("id"), "name": block["name"],
                "args": block.get("input", {}),
            }})
    finish = {"end_turn": "STOP", "tool_use": "STOP",
              "max_tokens": "MAX_TOKENS"}.get(a_resp.get("stop_reason"), "STOP")
    usage = a_resp.get("usage", {})
    return {
        "candidates": [{"content": {"role": "model", "parts": parts},
                        "finishReason": finish, "index": 0}],
        "usageMetadata": {
            "promptTokenCount": usage.get("input_tokens", 0),
            "candidatesTokenCount": usage.get("output_tokens", 0),
            "totalTokenCount": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
        "modelVersion": a_resp.get("model", CLAUDE_MODEL),
    }


# ---------------- Gemini -> OpenAI-compatible (Kimi, DeepSeek, vLLM, ...) ----

def to_openai(ca_body: dict) -> dict:
    req = ca_body.get("request", ca_body)
    messages = []
    sysins = req.get("systemInstruction")
    if sysins:
        txt = parts_text(sysins.get("parts")) if isinstance(sysins, dict) else str(sysins)
        if txt:
            messages.append({"role": "system", "content": txt})

    pending_tool_ids = []  # FIFO pairing, same scheme as the Anthropic path
    for content in req.get("contents", []):
        role = "assistant" if content.get("role") == "model" else "user"
        text_acc, tool_calls, tool_msgs = [], [], []
        for p in content.get("parts", []):
            if p.get("text"):
                text_acc.append(p["text"])
            elif "functionCall" in p:
                fc = p["functionCall"]
                tid = fc.get("id") or f"call_gen_{len(messages)}_{len(tool_calls)}"
                pending_tool_ids.append(tid)
                tool_calls.append({"id": tid, "type": "function", "function": {
                    "name": fc["name"],
                    "arguments": json.dumps(fc.get("args", {}), ensure_ascii=False),
                }})
            elif "functionResponse" in p:
                fr = p["functionResponse"]
                tid = fr.get("id") or (pending_tool_ids.pop(0) if pending_tool_ids
                                       else "call_unknown")
                if fr.get("id") and tid in pending_tool_ids:
                    pending_tool_ids.remove(tid)
                # tool results are their own role:"tool" messages on the OpenAI wire
                tool_msgs.append({"role": "tool", "tool_call_id": tid,
                                  "content": json.dumps(fr.get("response", {}),
                                                        ensure_ascii=False)})
        if tool_calls:
            messages.append({"role": "assistant",
                             "content": "\n".join(text_acc) or None,
                             "tool_calls": tool_calls})
        elif text_acc:
            messages.append({"role": role, "content": "\n".join(text_acc)})
        messages.extend(tool_msgs)

    out = {"model": NEW_MODEL, "messages": messages}
    tools = []
    for t in req.get("tools", []):
        for fd in t.get("functionDeclarations", []):
            tools.append({"type": "function", "function": {
                "name": fd["name"], "description": fd.get("description", ""),
                "parameters": lc_schema(fd.get("parameters", {"type": "object"})),
            }})
    if tools:
        out["tools"] = tools
    return out


def call_openai(oa_req: dict) -> dict:
    req = urllib.request.Request(
        OPENAI_BASE.rstrip("/") + "/chat/completions",
        data=json.dumps(oa_req).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {OPENAI_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def openai_to_gcr(oa_resp: dict) -> dict:
    msg = (oa_resp.get("choices") or [{}])[0].get("message", {})
    parts = []
    if msg.get("content"):
        parts.append({"text": msg["content"]})
    for tc in msg.get("tool_calls", []) or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_raw": fn.get("arguments")}
        parts.append({"functionCall": {"id": tc.get("id"),
                                       "name": fn.get("name"), "args": args}})
    usage = oa_resp.get("usage", {})
    return {
        "candidates": [{"content": {"role": "model", "parts": parts},
                        "finishReason": "STOP", "index": 0}],
        "usageMetadata": {
            "promptTokenCount": usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "totalTokenCount": usage.get("total_tokens", 0),
        },
        "modelVersion": oa_resp.get("model", NEW_MODEL),
    }


# ---------------- addon ----------------

def to_sse(gcr: dict, wrap: bool) -> bytes:
    payload = {"response": gcr} if wrap else gcr
    return (f"data: {json.dumps(payload, ensure_ascii=False)}\r\n\r\n").encode()


class Translator:
    def request(self, flow: http.HTTPFlow) -> None:
        flowlog({"dir": "req", "method": flow.request.method,
                 "url": flow.request.pretty_url,
                 "content_type": flow.request.headers.get("content-type", ""),
                 "size": len(flow.request.raw_content or b"")})
        if not is_inference(flow) or not TRANSLATE:
            return
        # never intercept our own backend calls to aiplatform (publisher anthropic)
        if "/publishers/anthropic/" in flow.request.path:
            return

        raw = body_bytes(flow.request)
        dump("inference-req.bin", raw)
        ctype = flow.request.headers.get("content-type", "")
        if "json" not in ctype.lower():
            ctx.log.warn("[inference] non-JSON body; passthrough (dumped)")
            return
        try:
            ca_body = json.loads(raw)
            if BACKEND == "anthropic-vertex":
                a_req = to_anthropic(ca_body)
                dump("anthropic-req.json", json.dumps(a_req, indent=1).encode())
                a_resp = call_claude_vertex(a_req)
                dump("anthropic-resp.json", json.dumps(a_resp, indent=1).encode())
                gcr = anthropic_to_gcr(a_resp)
                served = f"{CLAUDE_MODEL} (Vertex)"
            else:
                oa_req = to_openai(ca_body)
                oa_resp = call_openai(oa_req)
                gcr = openai_to_gcr(oa_resp)
                served = NEW_MODEL
            wrap = "cloudcode-pa" in flow.request.pretty_host
            flow.response = http.Response.make(
                200, to_sse(gcr, wrap), {"content-type": "text/event-stream"},
            )
            ctx.log.info(f"[inference] answered locally by {served}")
        except urllib.error.HTTPError as e:  # fail open, but log the body
            body = e.read()[:500]
            dump("backend-error.json", body)
            ctx.log.warn(f"[inference] backend HTTP {e.code}: {body!r}; passthrough")
        except Exception as e:  # fail open
            ctx.log.warn(f"[inference] translate failed ({type(e).__name__}: {e}); passthrough")

    def response(self, flow: http.HTTPFlow) -> None:
        flowlog({"dir": "resp", "url": flow.request.pretty_url,
                 "status": flow.response.status_code if flow.response else 0,
                 "content_type": (flow.response.headers.get("content-type", "")
                                  if flow.response else ""),
                 "size": len(flow.response.raw_content or b"") if flow.response else 0})


addons = [Translator()]
