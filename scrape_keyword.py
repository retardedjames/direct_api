"""
Scrape all sort-by-likes videos for a keyword, straight off TikTok's mobile
API via the Frida signer-proxy (live TT Lite on the ARM Waydroid VM). No
RapidAPI quota, no mitmproxy.

Protocol (verified from phone captures):
  - Endpoint is always /aweme/v1/search/item/
  - Page 1: cursor=0, sort_type=1, is_filter_search=1, search_source=tab_search,
    search_id="". Response carries x-tt-logid header.
  - Page 2+: cursor=PAGE_SIZE, 2*PAGE_SIZE, ...; all other params the
    same, and search_id = <x-tt-logid from the page-1 response>, held
    constant across every subsequent page.
  - `count` is the per-page size. Server ceiling is 30; phone captures
    used 10. We use 30 for fewer sign calls per keyword.

Stop conditions:
  1. response `has_more` is 0/false, OR
  2. response `aweme_list` is empty, OR
  3. every video in the response has digg_count < LIKE_FLOOR (default 1000)

After scraping, all unique videos with digg_count >= 1000 are persisted to
the Oracle VPS Postgres (see ../db.py): Search row + Video/Author upserts +
SearchResult links — same shape mobile_scrape.py / batch_scrape.py produce.

Usage:
  python3 scrape_keyword.py mario
  python3 scrape_keyword.py "kawaii desk" --floor 5000 --max-pages 30
  python3 scrape_keyword.py mario --out /tmp/mario.jsonl
  python3 scrape_keyword.py mario --no-db        # skip DB write
"""

import argparse
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_ACCOUNT = os.environ.get("TIKTOK_ACCOUNT", "")
if _ACCOUNT in ("vm3", "vm4", "vm5"):
    _mod = __import__(f"replay_search_{_ACCOUNT}")
    DEVICE      = _mod.DEVICE
    USER_AGENT  = _mod.USER_AGENT
    COOKIE      = _mod.COOKIE
    X_TT_TOKEN  = _mod.X_TT_TOKEN
    TIKTOK_HOST = _mod.TIKTOK_HOST
    TIKTOK_PATH = _mod.TIKTOK_PATH
else:
    from replay_search import (
        DEVICE, USER_AGENT, COOKIE, X_TT_TOKEN,
        TIKTOK_HOST, TIKTOK_PATH,
    )
from frida_signer import FridaSigner

PAGE_SIZE = 30
LIKE_FLOOR = 1000
MAX_PAGES_DEFAULT = 100

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

SIGNER = FridaSigner()


def build_query(keyword: str, cursor: int, search_id: str, count: int = PAGE_SIZE) -> str:
    now_ms = int(time.time() * 1000)
    now_s = now_ms // 1000
    params = {
        "cursor": str(cursor),
        "sort_type": "1",
        "enter_from": "homepage_hot",
        "count": str(count),
        "source": "video_search",
        "keyword": keyword,
        "query_correct_type": "0",
        "is_filter_search": "1",
        "search_source": "tab_search",
        "search_id": search_id,
        "request_tag_from": "h5",
        "_rticket": str(now_ms),
        "ts": str(now_s),
        **DEVICE,
    }
    return urllib.parse.urlencode(
        [(k, params[k]) for k in SEARCH_PARAM_ORDER],
        quote_via=urllib.parse.quote_plus,
    )


def base_headers() -> dict:
    """Pre-signing request headers. Must match the input the live TT process
    sees when it signs its own /search/item/ GETs — otherwise MSSDK's hash
    will diverge from what TikTok expects. Matches replay_search_frida.py."""
    return {
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
        "x-vc-bdturing-sdk-version": "2.3.15.i18n",
        "ttzip-tlb": "1",
        "accept-encoding": "gzip, ttzip",
        "user-agent": USER_AGENT,
        "cookie": COOKIE,
    }


def call_tiktok(query: str, sig: dict):
    url = f"https://{TIKTOK_HOST}{TIKTOK_PATH}?{query}"
    headers = dict(base_headers())
    headers["x-argus"]   = sig.get("X-Argus", "")
    headers["x-ladon"]   = sig.get("X-Ladon", "")
    headers["x-gorgon"]  = sig.get("X-Gorgon", "")
    headers["x-khronos"] = sig.get("X-Khronos", "")
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
    if resp_headers.get("Content-Encoding", "").lower() == "gzip":
        raw = gzip.decompress(raw)
    return status, raw, resp_headers


def fetch_page(keyword: str, cursor: int, search_id: str):
    query = build_query(keyword, cursor, search_id)
    url = f"https://{TIKTOK_HOST}{TIKTOK_PATH}?{query}"
    t0 = time.time()
    sig = SIGNER.sign_request(url, base_headers())
    sign_ms = int((time.time() - t0) * 1000)
    status, body, hdrs = call_tiktok(query, sig)
    if status != 200:
        raise RuntimeError(f"tiktok status {status}: {body[:200]!r}")
    parsed = json.loads(body)
    logid = hdrs.get("X-Tt-Logid") or hdrs.get("x-tt-logid") or ""
    return parsed, logid, sign_ms


def summarise(aweme: dict) -> dict:
    stats = aweme.get("statistics") or {}
    author = aweme.get("author") or {}
    return {
        "aweme_id": aweme.get("aweme_id"),
        "desc": (aweme.get("desc") or "")[:200],
        "digg_count": stats.get("digg_count"),
        "play_count": stats.get("play_count"),
        "share_count": stats.get("share_count"),
        "comment_count": stats.get("comment_count"),
        "create_time": aweme.get("create_time"),
        "author_unique_id": author.get("unique_id"),
        "author_nickname": author.get("nickname"),
    }


def scrape(keyword: str, floor: int, max_pages: int, out_fh):
    """Return (total_written, stop_reason, collected_raws).

    collected_raws is the ordered list of unique raw aweme dicts across all
    pages — passed to save_search so Video/Author rows get upserted and a
    SearchResult row links each id to this keyword's Search row.
    """
    seen_ids = set()
    collected_raws: list[dict] = []
    page = 0
    cursor = 0
    search_id = ""
    total_written = 0
    stop_reason = None

    while page < max_pages:
        print(f"[page {page}] cursor={cursor} search_id={search_id[:12] + '...' if search_id else '(empty)'}", file=sys.stderr)
        parsed, logid, sign_ms = fetch_page(keyword, cursor, search_id)
        print(f"[page {page}] sign={sign_ms}ms", file=sys.stderr)

        if page == 0 and logid:
            search_id = logid
            print(f"[page 0] captured search_id = {search_id}", file=sys.stderr)

        aweme_list = parsed.get("aweme_list") or []
        has_more = parsed.get("has_more")
        server_next_cursor = parsed.get("cursor")

        if not aweme_list:
            stop_reason = f"aweme_list empty (has_more={has_more})"
            break

        new_this_page = 0
        all_below_floor = True
        for a in aweme_list:
            aid = a.get("aweme_id")
            if not aid or aid in seen_ids:
                continue
            seen_ids.add(aid)
            collected_raws.append(a)
            summary = summarise(a)
            digg = summary["digg_count"] or 0
            if digg >= floor:
                all_below_floor = False
            out_fh.write(json.dumps({"summary": summary, "raw": a}) + "\n")
            total_written += 1
            new_this_page += 1

        top_like = max((summarise(a)["digg_count"] or 0) for a in aweme_list)
        min_like = min((summarise(a)["digg_count"] or 0) for a in aweme_list)
        print(f"[page {page}] got {len(aweme_list)} items (new={new_this_page}); "
              f"likes range {min_like:,}..{top_like:,}; running total={total_written}",
              file=sys.stderr)

        if all_below_floor:
            stop_reason = f"all {len(aweme_list)} items on page < {floor} likes"
            break

        if has_more == 0 or has_more is False:
            stop_reason = "has_more=0"
            break

        if isinstance(server_next_cursor, int) and server_next_cursor > cursor:
            cursor = server_next_cursor
        else:
            cursor += PAGE_SIZE
        page += 1
    else:
        stop_reason = f"hit max_pages={max_pages}"

    return total_written, stop_reason, collected_raws


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("keyword")
    ap.add_argument("--floor", type=int, default=LIKE_FLOOR,
                    help=f"Stop when every video on a page has fewer than this many likes (default {LIKE_FLOOR})")
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES_DEFAULT,
                    help=f"Hard cap on pages fetched (default {MAX_PAGES_DEFAULT})")
    ap.add_argument("--out", type=str, default="-",
                    help="Output jsonl path, or '-' for stdout (default -)")
    ap.add_argument("--no-db", action="store_true",
                    help="Don't write results to Postgres")
    args = ap.parse_args()

    out_fh = sys.stdout if args.out == "-" else open(args.out, "w")
    t0 = time.time()
    try:
        total, reason, raws = scrape(args.keyword, args.floor, args.max_pages, out_fh)
    finally:
        if out_fh is not sys.stdout:
            out_fh.close()
    dt = time.time() - t0
    print(f"[done] keyword={args.keyword!r} total={total} stop={reason} elapsed={dt:.1f}s",
          file=sys.stderr)

    if args.no_db:
        return

    to_save = [r for r in raws
               if ((r.get("statistics") or {}).get("digg_count") or 0) >= args.floor]
    print(f"[db] {len(raws)} scraped, {len(raws) - len(to_save)} under {args.floor} likes dropped, "
          f"{len(to_save)} to save.", file=sys.stderr)
    if not to_save:
        return

    from db import save_search
    saved = save_search(args.keyword, "1", to_save)
    print(f"[db] saved {saved} unique videos for keyword={args.keyword!r}", file=sys.stderr)


if __name__ == "__main__":
    main()
