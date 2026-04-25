"""mitmproxy addon: dump full request details for every TikTok API call.

Writes one JSON line per request to /tmp/tt_signed_capture.jsonl containing:
  url, method, query params, request headers (incl. X-Argus/X-Ladon/X-Gorgon/
  X-Khronos/X-Cylons/X-SS-Cookie/cookie), request body, response status,
  response body (first 4KB), timestamp.

Purpose: reverse out the device fingerprint + signed headers so we can
replay TikTok requests via the RapidAPI signer without driving the phone.
"""

import json
import time
from mitmproxy import http

OUT_FILE = "/tmp/tt_signed_capture.jsonl"
HOSTS_SUBSTR = ("tiktok", "tiktokv", "byteoversea", "bytedance", "musical.ly")


def response(flow: http.HTTPFlow):
    try:
        _record(flow)
    except Exception as e:
        print(f"[cap] addon error (swallowed): {e!r}", flush=True)


def _record(flow: http.HTTPFlow):
    host = flow.request.host
    if not any(s in host for s in HOSTS_SUBSTR):
        return

    req = flow.request
    resp = flow.response

    # keep response payload small — we only need search response bodies in full,
    # other endpoints (device_register/common) are usually <2KB anyway
    # Use raw_content to bypass auto-decoding — TikTok uses 'ttzip' as a
    # Content-Encoding, and mitmproxy's auto-decode raises ValueError on it,
    # which would abort the addon and leave no record on disk.
    body_b64 = None
    body_text = None
    try:
        body = resp.raw_content or b""
    except Exception:
        body = b""
    try:
        body_text = body[:8192].decode("utf-8", errors="replace")
    except Exception:
        body_b64 = body[:8192].hex()

    rec = {
        "ts": time.time(),
        "method": req.method,
        "scheme": req.scheme,
        "host": host,
        "path": req.path,
        "url": req.pretty_url,
        "query": dict(req.query),
        "req_headers": {k: v for k, v in req.headers.items()},
        "req_body": (req.content or b"")[:4096].decode("utf-8", errors="replace"),
        "resp_status": resp.status_code,
        "resp_headers": {k: v for k, v in resp.headers.items()},
        "resp_body_text": body_text,
        "resp_body_hex": body_b64,
    }

    with open(OUT_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")

    # keep console noise minimal but useful
    tag = ""
    if "search/stream" in req.path or "search/single" in req.path or "search/item" in req.path:
        tag = " [SEARCH]"
    elif "device_register" in req.path or "device/register" in req.path:
        tag = " [DEVICE_REGISTER]"
    elif "/common" in req.path and "mssdk" in req.path.lower():
        tag = " [MSSDK]"
    elif "/common" in req.path or "applog" in host:
        tag = " [APPLOG]"
    print(f"[cap]{tag} {req.method} {host}{req.path[:80]} -> {resp.status_code}",
          flush=True)
