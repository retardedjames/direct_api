"""
Replay a TikTok mobile search request using headers signed by RapidAPI's
bytedance-services MSSDK signer. Reuses the device fingerprint captured from
the phone session in captured_search_likes.json.

Usage:
  python3 replay_search.py mario
  python3 replay_search.py "kawaii desk" --cursor 0
  python3 replay_search.py mario --cursor 10

Dumps the response JSON to stdout, and a trace (request URL, signing input,
signing output, response status, response body preview) to
/tmp/replay_<keyword>_<cursor>.json for inspection.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

RAPIDAPI_KEY = "2861349ef0mshd02e93636381db1p17b22cjsn20fbcdc12948"
RAPIDAPI_HOST = "bytedance-services.p.rapidapi.com"
SIGN_URL = f"https://{RAPIDAPI_HOST}/mssdk_common/sign"

TIKTOK_HOST = "api19-normal-useast8.tiktokv.us"
TIKTOK_PATH = "/aweme/v1/search/item/"

# Device fingerprint extracted from the phone capture (see HANDOFF.md).
DEVICE = {
    "aid": "1340",
    "app_name": "musically_go",
    "app_package": "com.tiktok.lite.go",
    "version_code": "430553",
    "version_name": "43.5.53",
    "manifest_version_code": "430553",
    "update_version_code": "430553",
    "ab_version": "43.5.53",
    "build_number": "43.5.53",
    "device_id": "7630963143929628173",
    "iid": "7631123284638189325",
    "openudid": "28dcd4741ad9b0e6",
    "cdid": "56f0a19c-154f-487b-949b-ad6a7464c06d",
    "device_brand": "motorola",
    "device_type": "moto g power - 2025",
    "device_platform": "android",
    "os": "android",
    "os_version": "16",
    "os_api": "36",
    "resolution": "1080*2226",
    "dpi": "330",
    "host_abi": "arm64-v8a",
    "channel": "googleplay",
    "sys_region": "US",
    "op_region": "US",
    "region": "GB",
    "locale": "en-GB",
    "language": "en",
    "app_language": "en",
    "timezone_name": "America/New_York",
    "timezone_offset": "-18000",
    "ac": "wifi",
    "ac2": "wifi",
    "ssmix": "a",
    "app_type": "normal",
}

MSSDK = {
    "mssdk_app_id": 1340,
    "mssdk_license_id": "224921550",
    "mssdk_version": "v05.01.05-alpha.5-ov-android",
    "mssdk_version_int": 83952928,
}

USER_AGENT = (
    "com.tiktok.lite.go/430553 (Linux; U; Android 16; en_US; "
    "moto g power - 2025; Build/W1VE36H.10-12-9-8;tt-ok/3.12.13.51.lite-ul)"
)

COOKIE = (
    "store-idc=useast5; store-country-code=us; install_id=7631123284638189325; "
    "ttreq=1$c79d0dd3442f7d7d04daf3e7457da7e036700c43; "
    "d_ticket=2e8cd293a152e6a3736cdd4e5009d5800cafd; "
    "odin_tt=bb932ab14417cebea8013a4c854e630cad3958762b31a1344ac603759b75b76be83a6bd5e19be4201fc570f77901d205a444c131587351311c47acd209717d2e9a34e1b9bbf7fe0458893dcadb5c5ca4; "
    "cmpl_token=AgQQAPNSF-ROXY9EytVVop0884c5vy-ZP4_ZYKBCgQ; "
    "sid_guard=aaf4ce23a9ceed1ca24419829c6168c9%7C1776759672%7C15552000%7CSun%2C+18-Oct-2026+08%3A21%3A12+GMT; "
    "uid_tt=2a32d231c8dae213d69d6eed26a5749b83c48bbfa03463e1dad6e172dfa9cd05; "
    "uid_tt_ss=2a32d231c8dae213d69d6eed26a5749b83c48bbfa03463e1dad6e172dfa9cd05; "
    "sid_tt=aaf4ce23a9ceed1ca24419829c6168c9; "
    "sessionid=aaf4ce23a9ceed1ca24419829c6168c9; "
    "sessionid_ss=aaf4ce23a9ceed1ca24419829c6168c9; "
    "tt_session_tlb_tag=sttt%7C5%7CqvTOI6nO7RyiRBmCnGFoyf_________RXxXJFZn1DOFkts-mQrfHQufNlx1uC3Ep16hO1FR02VA%3D; "
    "store-country-sign=MEIEDJ83-2jN2geqHQfszgQg-orDug6VbeuKPoVBGiMh-TZDfFv6K2Hsw4NgfAyM05EEEJnSnkLie8Um7GgEDcqjqP4; "
    "store-country-code-src=uid; tt-target-idc=useast5"
)

X_TT_TOKEN = (
    "04aaf4ce23a9ceed1ca24419829c6168c9029617e6b3f8692d33e330e9eec0c8715201a4446f80ee555aa1457d7b3bddf866bcc2119f671e2274d9bc5d73e33998d58c72fc992cf7ee7692985d2a0e81e912980401d160e3b3b95ce846f52c023576c"
    "--0a4e0a20a927c5ff3e168d299ab5da90fd0fa3b75609b45fdc9b4c76ccf355747e6d83fd1220f9b01243f1e70c05cb18ec1cfbe3cebe6f2311c67ce72c05b5fb6d7585dd269d1801220674696b746f6b-3.0.1"
)

# Exact parameter order from captured_search_likes.json. Ordering matters
# because the signature is computed over the canonical query string.
SEARCH_PARAM_ORDER = [
    "cursor", "sort_type", "enter_from", "count", "source", "keyword",
    "query_correct_type", "is_filter_search", "search_source", "search_id",
    "request_tag_from",
    "_rticket", "manifest_version_code", "app_language", "app_type", "iid",
    "app_package", "channel", "device_type", "language", "host_abi", "locale",
    "resolution", "openudid", "update_version_code", "ac2", "cdid",
    "sys_region", "os_api", "timezone_name", "dpi", "ac", "os", "device_id",
    "os_version", "timezone_offset", "version_code", "app_name", "ab_version",
    "version_name", "device_brand", "op_region", "ssmix", "device_platform",
    "build_number", "region", "aid", "ts",
]


def build_query(keyword: str, cursor: int, count: int = 30) -> str:
    """Build the query string in the exact order the phone app uses."""
    now_ms = int(time.time() * 1000)
    now_s = now_ms // 1000
    params = {
        "cursor": str(cursor),
        "sort_type": "1",  # 1 = sort by likes
        "enter_from": "homepage_hot",
        "count": str(count),
        "source": "video_search",
        "keyword": keyword,
        "query_correct_type": "0",
        "is_filter_search": "1",
        "search_source": "tab_search",
        "search_id": "",
        "request_tag_from": "h5",
        "_rticket": str(now_ms),
        "ts": str(now_s),
        **DEVICE,
    }
    # urlencode with quote_via=quote_plus mirrors the phone's behaviour:
    # spaces -> '+', '/' gets percent-encoded in timezone_name.
    pairs = [(k, params[k]) for k in SEARCH_PARAM_ORDER]
    return urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote_plus)


def call_signer(query: str) -> dict:
    """POST to RapidAPI /mssdk_common/sign. Returns the 4-header dict."""
    body = json.dumps({
        "method": "GET",
        "query": query,
        # x_ss_stub isn't sent for this GET, but the signer requires one of
        # x_ss_stub/payload. 32-zero MD5 is a harmless placeholder; the
        # search endpoint doesn't verify X-SS-STUB on GETs.
        "x_ss_stub": "00000000000000000000000000000000",
        "mssdk_app_id": MSSDK["mssdk_app_id"],
        "mssdk_license_id": MSSDK["mssdk_license_id"],
        "mssdk_version": MSSDK["mssdk_version"],
        "mssdk_version_int": MSSDK["mssdk_version_int"],
        "device_id": DEVICE["device_id"],
        "device_type": DEVICE["device_type"],
        "channel": DEVICE["channel"],
        "os_version": DEVICE["os_version"],
        "version_name": DEVICE["version_name"],
    }).encode()

    req = urllib.request.Request(
        SIGN_URL,
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-rapidapi-key": RAPIDAPI_KEY,
            "x-rapidapi-host": RAPIDAPI_HOST,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    if payload.get("error"):
        raise RuntimeError(f"signer error: {payload}")
    return payload


def call_tiktok(query: str, sig: dict) -> tuple[int, bytes, dict]:
    url = f"https://{TIKTOK_HOST}{TIKTOK_PATH}?{query}"
    headers = {
        "rpc-persist-pyxis-policy-state-law-is-ca": "0",
        "rpc-persist-pyxis-policy-v-tnc": "1",
        "x-ss-dp": "1340",
        "x-tt-request-tag": "n=0",
        "x-tt-pba-enable": "1",
        "sdk-version": "2",
        "x-tt-dm-status": "login=1;ct=1;",
        "x-tt-token": X_TT_TOKEN,
        "passport-sdk-version": "1",
        "x-tt-ultra-lite": "1",
        "x-tt-store-region": "us",
        "x-tt-store-region-src": "uid",
        # bdturing is TikTok's anti-bot layer; captured phone sent this. Omit
        # and the search service may shadow-reject (HTTP 200 + aweme_list=null).
        "x-vc-bdturing-sdk-version": "2.3.15.i18n",
        "ttzip-tlb": "1",
        "accept-encoding": "gzip, ttzip",
        "user-agent": USER_AGENT,
        "cookie": COOKIE,
        "x-argus": sig["X-Argus"],
        "x-ladon": sig["X-Ladon"],
        "x-gorgon": sig["X-Gorgon"],
        "x-khronos": sig["X-Khronos"],
    }
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            status = resp.status
            resp_headers = dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
        resp_headers = dict(e.headers) if e.headers else {}

    # Transparently decompress gzip responses.
    if resp_headers.get("Content-Encoding", "").lower() == "gzip":
        import gzip
        raw = gzip.decompress(raw)
    return status, raw, resp_headers


def safe_kw(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:40]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("keyword")
    ap.add_argument("--cursor", type=int, default=0)
    ap.add_argument("--count", type=int, default=30,
                    help="Page size (ceiling on /search/item/ is 30)")
    args = ap.parse_args()

    query = build_query(args.keyword, args.cursor, args.count)
    print(f"[query] {query[:200]}...", file=sys.stderr)

    sig = call_signer(query)
    print(f"[sig] X-Khronos={sig['X-Khronos']} X-Gorgon={sig['X-Gorgon'][:16]}...", file=sys.stderr)

    status, body, hdrs = call_tiktok(query, sig)
    print(f"[tiktok] status={status} bytes={len(body)}", file=sys.stderr)

    trace_path = Path(f"/tmp/replay_{safe_kw(args.keyword)}_{args.cursor}.json")
    try:
        parsed = json.loads(body)
        body_preview = json.dumps(parsed)[:400]
    except Exception:
        parsed = None
        body_preview = body[:400].decode("utf-8", "replace")

    trace = {
        "query": query,
        "sig": sig,
        "resp_status": status,
        "resp_headers": hdrs,
        "resp_body_preview": body_preview,
    }
    if parsed is not None:
        aweme = parsed.get("aweme_list") or []
        trace["aweme_count"] = len(aweme)
        trace["status_code"] = parsed.get("status_code")
        trace["status_msg"] = parsed.get("status_msg")
        if aweme:
            first = aweme[0]
            stats = first.get("statistics") or {}
            trace["first_item"] = {
                "aweme_id": first.get("aweme_id"),
                "desc": (first.get("desc") or "")[:120],
                "digg_count": stats.get("digg_count"),
                "play_count": stats.get("play_count"),
            }

    trace_path.write_text(json.dumps(trace, indent=2))
    print(f"[trace] wrote {trace_path}", file=sys.stderr)

    if parsed is not None:
        print(json.dumps(parsed))
    else:
        sys.stdout.buffer.write(body)


if __name__ == "__main__":
    main()
