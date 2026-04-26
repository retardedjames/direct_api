"""
Scrape sort-by-likes videos for a keyword via TikTok's *web* search API
(www.tiktok.com/api/search/item/full/). No mobile signing, no Frida, no
Waydroid — a single logged-in browser cookie is all that's needed.

Why this exists alongside scrape_keyword.py:
  - The mobile API requires X-Argus/Gorgon/Khronos/Ladon from libmetasec.
    That's the whole point of the Waydroid+Frida stack.
  - The web API at /api/search/item/full/ accepts requests with just a
    valid sid_guard cookie. No _signature, no X-Bogus required (verified
    2026-04-25). 30 items/page, deep pagination, full schema.
  - Sort-by-likes ONLY engages with sort_type=1 + is_filter_search=1.
    Drop either flag and the server returns default-ranked results
    (sort param silently ignored). Verified across 5 keywords: with the
    flag combo, top-6 likes are monotonically descending and reach into
    the millions; without it, position is effectively random.
  - sort_type=2 + is_filter_search=1 = sort by LEAST liked (ascending).
    Useful for sampling the long tail; not what we usually want.

Cookie source: web_cookie.py at the repo root (gitignored). Drop in your
logged-in www.tiktok.com cookie + UA there. See web_cookie.example.py.

Stop conditions: same as scrape_keyword.py
  1. has_more == 0
  2. item_list empty
  3. every item on a page < floor likes

Usage:
  python3 scrape_keyword_web.py mario
  python3 scrape_keyword_web.py "kawaii desk" --floor 5000 --max-pages 30
  python3 scrape_keyword_web.py mario --no-db
"""

import argparse
import gzip
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import brotli
except ImportError:
    import brotlicffi as brotli  # type: ignore

from web_cookie import COOKIE, USER_AGENT
from web_remap import web_to_mobile

PAGE_SIZE = 30
LIKE_FLOOR = 1000
MAX_PAGES_DEFAULT = 100

ENDPOINT = "https://www.tiktok.com/api/search/item/full/"


def _ms_token_from_cookie(cookie: str) -> str:
    """Extract the *last* msToken value from the cookie header. The browser
    appends a fresh msToken on each navigation, so the last one wins."""
    last = ""
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("msToken="):
            last = part[len("msToken="):]
    return last


def build_params(keyword: str, cursor: int, search_id: str = "",
                 sort_type: int = 1, publish_time: int = 0,
                 count: int = PAGE_SIZE) -> dict:
    """sort_type: 0=default, 1=most-liked (descending), 2=least-liked
    (ascending). Sort only takes effect when is_filter_search=1, which
    this function always sets. publish_time: 0=all, 7/30/90/180=days."""
    p = {
        "aid": "1988",
        "app_language": "en",
        "app_name": "tiktok_web",
        "browser_language": "en-US",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Win32",
        "browser_version": "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "channel": "tiktok_web",
        "cookie_enabled": "true",
        "device_id": "7445000000000000000",
        "device_platform": "web_pc",
        "focus_state": "true",
        "from_page": "search",
        "history_len": "3",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "keyword": keyword,
        "os": "windows",
        "priority_region": "US",
        "referer": "",
        "region": "US",
        "screen_height": "1080",
        "screen_width": "1920",
        "tz_name": "America/New_York",
        "webcast_language": "en",
        "msToken": _ms_token_from_cookie(COOKIE),
        "offset": str(cursor),
        "count": str(count),
        "sort_type": str(sort_type),
        "publish_time": str(publish_time),
        # is_filter_search + search_source=tab_search is the magic combo
        # that makes the server actually honor sort_type. Without these
        # the sort param is silently ignored and you get default ranking.
        "is_filter_search": "1",
        "search_source": "tab_search",
    }
    if search_id:
        p["search_id"] = search_id
    return p


def fetch_page(keyword: str, cursor: int, search_id: str,
               sort_type: int = 1, publish_time: int = 0):
    params = build_params(keyword, cursor, search_id, sort_type, publish_time)
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Cookie": COOKIE,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": f"https://www.tiktok.com/search?q={urllib.parse.quote(keyword)}",
        "Origin": "https://www.tiktok.com",
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            ce = resp.headers.get("content-encoding", "")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        ce = e.headers.get("content-encoding", "") if e.headers else ""
        status = e.code
    fetch_ms = int((time.time() - t0) * 1000)

    if ce == "gzip":
        raw = gzip.decompress(raw)
    elif ce == "br":
        raw = brotli.decompress(raw)

    if status != 200:
        raise RuntimeError(f"web search status={status}: {raw[:200]!r}")

    parsed = json.loads(raw)
    log_pb = parsed.get("log_pb") or {}
    impr_id = log_pb.get("impr_id") or ""
    return parsed, impr_id, fetch_ms


def summarise(item_mobile: dict) -> dict:
    """Same shape as scrape_keyword.summarise() — works on the *remapped*
    mobile-schema dict so logging stays consistent across both scrapers."""
    stats = item_mobile.get("statistics") or {}
    author = item_mobile.get("author") or {}
    return {
        "aweme_id": item_mobile.get("aweme_id"),
        "desc": (item_mobile.get("desc") or "")[:200],
        "digg_count": stats.get("digg_count"),
        "play_count": stats.get("play_count"),
        "share_count": stats.get("share_count"),
        "comment_count": stats.get("comment_count"),
        "create_time": item_mobile.get("create_time"),
        "author_unique_id": author.get("unique_id"),
        "author_nickname": author.get("nickname"),
    }


def scrape(keyword: str, floor: int, max_pages: int, out_fh,
           sort_type: int = 1, publish_time: int = 0):
    seen_ids: set[str] = set()
    collected_mobile: list[dict] = []
    page = 0
    cursor = 0
    search_id = ""
    total_written = 0
    stop_reason: str | None = None

    while page < max_pages:
        print(f"[page {page}] cursor={cursor} search_id={search_id[:16] + '...' if search_id else '(empty)'}",
              file=sys.stderr)
        parsed, impr_id, fetch_ms = fetch_page(keyword, cursor, search_id,
                                               sort_type, publish_time)
        item_list = parsed.get("item_list") or []
        has_more = parsed.get("has_more")
        next_cursor = parsed.get("cursor")
        print(f"[page {page}] fetch={fetch_ms}ms items={len(item_list)} has_more={has_more}",
              file=sys.stderr)

        if page == 0 and impr_id:
            search_id = impr_id

        if not item_list:
            stop_reason = f"item_list empty (has_more={has_more})"
            break

        new_this_page = 0
        all_below_floor = True
        for it in item_list:
            mobile = web_to_mobile(it)
            aid = mobile.get("aweme_id")
            if not aid or aid in seen_ids:
                continue
            seen_ids.add(aid)
            collected_mobile.append(mobile)
            digg = (mobile.get("statistics") or {}).get("digg_count") or 0
            if digg >= floor:
                all_below_floor = False
            out_fh.write(json.dumps({"summary": summarise(mobile), "raw_web": it}) + "\n")
            total_written += 1
            new_this_page += 1

        likes = [(web_to_mobile(it).get("statistics") or {}).get("digg_count") or 0
                 for it in item_list]
        if likes:
            print(f"[page {page}] new={new_this_page} likes range "
                  f"{min(likes):,}..{max(likes):,}; total={total_written}",
                  file=sys.stderr)

        if all_below_floor:
            stop_reason = f"all {len(item_list)} items on page < {floor} likes"
            break

        if has_more == 0 or has_more is False:
            stop_reason = "has_more=0"
            break

        if isinstance(next_cursor, int) and next_cursor > cursor:
            cursor = next_cursor
        else:
            cursor += PAGE_SIZE
        page += 1
    else:
        stop_reason = f"hit max_pages={max_pages}"

    return total_written, stop_reason, collected_mobile


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("keyword")
    ap.add_argument("--floor", type=int, default=LIKE_FLOOR)
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES_DEFAULT)
    ap.add_argument("--sort-type", type=int, default=1,
                    help="0=default, 1=most-liked, 2=least-liked (default 1)")
    ap.add_argument("--publish-time", type=int, default=0,
                    help="0=all, 7/30/90/180=last N days (default 0)")
    ap.add_argument("--out", type=str, default="-")
    ap.add_argument("--no-db", action="store_true")
    args = ap.parse_args()

    out_fh = sys.stdout if args.out == "-" else open(args.out, "w")
    t0 = time.time()
    try:
        total, reason, raws = scrape(args.keyword, args.floor, args.max_pages,
                                     out_fh, args.sort_type, args.publish_time)
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
    print(f"[db] {len(raws)} scraped, {len(raws) - len(to_save)} under floor dropped, "
          f"{len(to_save)} to save.", file=sys.stderr)
    if not to_save:
        return

    from db import save_search
    saved = save_search(args.keyword, str(args.sort_type), to_save)
    print(f"[db] saved {saved} unique videos for keyword={args.keyword!r}", file=sys.stderr)


if __name__ == "__main__":
    main()
