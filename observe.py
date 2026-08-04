"""mitmproxy addon — observation mode.

Logs every request agy makes (host, path, content-type, size) to flows.log.
Everything is passed through untouched. This is step 1: map the RPC surface
so we know which endpoint carries model inference.
"""
import json
from datetime import datetime, timezone

from mitmproxy import http

LOG = str(__import__("pathlib").Path(__file__).resolve().parent / "flows.log")


def _log(entry: dict) -> None:
    entry["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(LOG, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class Observer:
    def request(self, flow: http.HTTPFlow) -> None:
        _log({
            "dir": "req",
            "method": flow.request.method,
            "url": flow.request.pretty_url,
            "content_type": flow.request.headers.get("content-type", ""),
            "size": len(flow.request.raw_content or b""),
        })

    def response(self, flow: http.HTTPFlow) -> None:
        _log({
            "dir": "resp",
            "url": flow.request.pretty_url,
            "status": flow.response.status_code,
            "content_type": flow.response.headers.get("content-type", ""),
            "size": len(flow.response.raw_content or b""),
        })


addons = [Observer()]
