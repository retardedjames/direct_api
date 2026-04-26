"""
Web-endpoint twin of continual_scraper.py.

Pulls pending search terms from the `terms` queue, scrapes them via
www.tiktok.com/api/search/item/full/, upserts to Postgres, pings ntfy.

Key differences from continual_scraper.py:
  - No FridaSigner. No TIKTOK_ACCOUNT env var. No Waydroid.
  - One web cookie (web_cookie.py) authenticates all requests.
  - "Silent reject" detection works differently: the web endpoint
    doesn't return server_stream_time. Instead we watch for HTTP errors,
    sudden empty item_list on page 0, or status_code != 0 in the JSON.
"""

import argparse
import os
import random
import signal
import sys
import time
import urllib.request

from db import (
    claim_next_term,
    mark_term_done,
    mark_term_failed,
    release_term,
    reclaim_stale_terms,
    save_search,
)

skw = None  # lazy import so --dry-run / --help work without web_cookie.py


def _load_scraper():
    global skw
    if skw is None:
        import scrape_keyword_web as _skw
        skw = _skw

NTFY_TOPIC = "retardedjames-tiktok"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "http://150.136.40.239:2586")
NTFY_URL = f"{NTFY_SERVER}/{NTFY_TOPIC}"
NTFY_PREFIX = os.environ.get("NTFY_PREFIX", "[web]")

MAX_PAGES_DEFAULT = 50
LIKE_FLOOR_DEFAULT = 1000

INTER_TERM_SLEEP_MIN = 3
INTER_TERM_SLEEP_MAX = 10
INTER_PAGE_SLEEP_MIN = 0.5
INTER_PAGE_SLEEP_MAX = 2.0
REJECT_BACKOFF_SECONDS = 300

# If many consecutive terms come back with zero items on page 0, the cookie
# is probably expired/revoked — TikTok serves a 200 with an empty list rather
# than an error. Flag it as a session failure once we hit this many in a row.
ZERO_RESULT_HALT_THRESHOLD = 5

# Generic per-term exceptions (JSONDecodeError, network errors, etc.) used
# to be tolerated forever — leading to silent queue burn when the cookie
# went bad with HTTP 200 + empty body (a third reject pattern beyond the
# documented status_code != 0 and status_code == 0 + empty list cases).
# Halt after this many consecutive generic errors and ntfy.
ERROR_HALT_THRESHOLD = 3


class WebReject(Exception):
    """Raised when page 0 looks like a session-level rejection (auth/captcha
    wall) rather than a genuinely empty result. Distinguished from a normal
    exception so the main loop can apply per-account backoff."""


class WebKeywordBlocked(Exception):
    """Raised when page 0 returns status_code=403 — TikTok blocks the
    *keyword* (content-moderation list, e.g. eating-disorder-adjacent
    terms). Not a session problem — other keywords keep working. Main
    loop handles by marking the term done(0) and moving on."""


def ntfy(message: str, *, title: str | None = None, priority: str | None = None) -> None:
    try:
        if NTFY_PREFIX:
            message = f"{NTFY_PREFIX} {message}"
            if title:
                title = f"{NTFY_PREFIX} {title}"
        data = message.encode("utf-8")
        headers = {}
        if title:
            headers["Title"] = title
        if priority:
            headers["Priority"] = priority
        req = urllib.request.Request(NTFY_URL, data=data, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        print(f"[ntfy] failed: {e}", file=sys.stderr)


def scrape_one(keyword: str, floor: int, max_pages: int,
               sort_type: int, publish_time: int) -> tuple[int, str, list[dict]]:
    seen_ids: set[str] = set()
    collected: list[dict] = []
    page = 0
    cursor = 0
    search_id = ""
    stop_reason: str | None = None

    while page < max_pages:
        print(f"  [page {page}] cursor={cursor}", file=sys.stderr)
        parsed, impr_id, fetch_ms = skw.fetch_page(keyword, cursor, search_id,
                                                    sort_type, publish_time)
        item_list = parsed.get("item_list") or []
        has_more = parsed.get("has_more")
        status_code = parsed.get("status_code")
        print(f"  [page {page}] fetch={fetch_ms}ms items={len(item_list)} "
              f"has_more={has_more} status_code={status_code}", file=sys.stderr)

        # Page-0 status_code interpretation:
        #   - 0 / None: success
        #   - 403: keyword-level block (content moderation). Other keywords
        #     keep working — this is NOT a session problem. Confirmed empirically
        #     2026-04-25 with water/extended/dry fasting + OMAD: all 403, while
        #     mario/cooking on the same cookie returned status_code=0.
        #   - anything else: session-level reject (auth wall, captcha, etc.)
        if page == 0 and status_code == 403:
            raise WebKeywordBlocked(
                f"keyword blocked (status_code=403, status_msg={parsed.get('status_msg')!r})")
        if page == 0 and status_code not in (0, None):
            raise WebReject(f"status_code={status_code} status_msg={parsed.get('status_msg')!r}")

        if page == 0 and impr_id:
            search_id = impr_id

        if not item_list:
            stop_reason = f"item_list empty (has_more={has_more})"
            break

        all_below_floor = True
        for it in item_list:
            mobile = skw.web_to_mobile(it)
            aid = mobile.get("aweme_id")
            if not aid or aid in seen_ids:
                continue
            seen_ids.add(aid)
            collected.append(mobile)
            digg = (mobile.get("statistics") or {}).get("digg_count") or 0
            if digg >= floor:
                all_below_floor = False

        if all_below_floor:
            stop_reason = f"all {len(item_list)} items on page < {floor} likes"
            break

        if has_more == 0 or has_more is False:
            stop_reason = "has_more=0"
            break

        next_cursor = parsed.get("cursor")
        if isinstance(next_cursor, int) and next_cursor > cursor:
            cursor = next_cursor
        else:
            cursor += skw.PAGE_SIZE
        page += 1

        time.sleep(random.uniform(INTER_PAGE_SLEEP_MIN, INTER_PAGE_SLEEP_MAX))
    else:
        stop_reason = f"hit max_pages={max_pages}"

    return len(collected), stop_reason, collected


def run_once(term: dict, floor: int, max_pages: int,
             sort_type: int, publish_time: int) -> tuple[int, str]:
    keyword = term["term"]
    total, reason, raws = scrape_one(keyword, floor, max_pages, sort_type, publish_time)
    to_save = [r for r in raws
               if ((r.get("statistics") or {}).get("digg_count") or 0) >= floor]
    dropped = len(raws) - len(to_save)
    if not to_save:
        print(f"  [db] nothing to save ({len(raws)} scraped, {dropped} under floor)",
              file=sys.stderr)
        return 0, reason
    saved = save_search(keyword, str(sort_type), to_save)
    print(f"  [db] saved {saved} videos ({dropped} under {floor}-like floor dropped)",
          file=sys.stderr)
    return saved, reason


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--floor", type=int, default=LIKE_FLOOR_DEFAULT)
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES_DEFAULT)
    ap.add_argument("--sort-type", type=int, default=1,
                    help="0=default, 1=most-liked, 2=least-liked (default 1)")
    ap.add_argument("--publish-time", type=int, default=0,
                    help="0=all, 7/30/90/180=last N days (default 0)")
    ap.add_argument("--stale-minutes", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    claimed_id: int | None = None

    def handle_sigint(signum, frame):
        print("\n[sigint] releasing claimed term and exiting...", file=sys.stderr)
        if claimed_id is not None:
            try:
                release_term(claimed_id)
                print(f"[sigint] released term id={claimed_id}", file=sys.stderr)
            except Exception as e:
                print(f"[sigint] release failed: {e}", file=sys.stderr)
        sys.exit(130)

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    if not args.dry_run:
        _load_scraper()

    reclaimed = reclaim_stale_terms(args.stale_minutes)
    if reclaimed:
        print(f"[startup] reclaimed {reclaimed} stale in_progress rows", file=sys.stderr)

    consecutive_rejects = 0
    consecutive_zero_results = 0
    consecutive_errors = 0

    while True:
        term = claim_next_term()
        if not term:
            msg = "Queue empty — no pending search terms left."
            print(f"[queue] {msg}", file=sys.stderr)
            ntfy(msg, title="Web scraper: queue drained")
            break

        claimed_id = term["id"]
        keyword = term["term"]
        print(f"[term] id={claimed_id} keyword={keyword!r}", file=sys.stderr)

        if args.dry_run:
            print("  [dry-run] releasing without scraping", file=sys.stderr)
            release_term(claimed_id)
            claimed_id = None
            break

        t0 = time.time()
        try:
            saved, reason = run_once(term, args.floor, args.max_pages,
                                     args.sort_type, args.publish_time)
        except WebKeywordBlocked as e:
            print(f"[blocked] {keyword!r}: {e} — marking done(0), continuing",
                  file=sys.stderr)
            mark_term_done(claimed_id, 0)
            claimed_id = None
            consecutive_rejects = 0  # not a session problem
            time.sleep(random.uniform(INTER_TERM_SLEEP_MIN, INTER_TERM_SLEEP_MAX))
            continue
        except WebReject as e:
            consecutive_rejects += 1
            print(f"[reject] {e} (consecutive={consecutive_rejects})", file=sys.stderr)
            mark_term_failed(claimed_id)
            claimed_id = None

            if consecutive_rejects >= 3:
                msg = (f"Web scraper halting: {consecutive_rejects} consecutive "
                       f"rejects. Cookie/account likely cooked. "
                       f"Last term: {keyword!r}.")
                print(f"[halt] {msg}", file=sys.stderr)
                ntfy(msg, title="TikTok web scraper: halted on repeated rejects",
                     priority="high")
                sys.exit(1)

            print(f"[reject] backing off {REJECT_BACKOFF_SECONDS}s before next term",
                  file=sys.stderr)
            time.sleep(REJECT_BACKOFF_SECONDS)
            continue
        except Exception as e:
            consecutive_errors += 1
            print(f"[error] {type(e).__name__}: {e} (consecutive={consecutive_errors})",
                  file=sys.stderr)
            # Release back to pending — these are usually transient (network,
            # empty-body cookie failure) and shouldn't poison the queue with
            # `failed` rows that need manual cleanup.
            release_term(claimed_id)
            claimed_id = None

            if consecutive_errors >= ERROR_HALT_THRESHOLD:
                msg = (f"Web scraper halting: {consecutive_errors} consecutive "
                       f"errors. Cookie may be returning empty bodies. "
                       f"Last: {type(e).__name__}: {e}. Last term: {keyword!r}.")
                print(f"[halt] {msg}", file=sys.stderr)
                ntfy(msg, title="TikTok web scraper: halted on repeated errors",
                     priority="high")
                sys.exit(1)

            time.sleep(random.uniform(INTER_TERM_SLEEP_MIN, INTER_TERM_SLEEP_MAX))
            continue

        consecutive_rejects = 0
        consecutive_errors = 0
        if saved == 0:
            consecutive_zero_results += 1
            if consecutive_zero_results >= ZERO_RESULT_HALT_THRESHOLD:
                msg = (f"Web scraper halting: {consecutive_zero_results} consecutive "
                       f"terms returned 0 saved videos. Cookie likely expired — "
                       f"the login wall returns 200 + empty item_list. Last term: "
                       f"{keyword!r}.")
                print(f"[halt] {msg}", file=sys.stderr)
                ntfy(msg, title="TikTok web scraper: halted, cookie likely expired",
                     priority="high")
                mark_term_done(claimed_id, saved)
                sys.exit(1)
        else:
            consecutive_zero_results = 0

        dt = time.time() - t0
        mark_term_done(claimed_id, saved)
        claimed_id = None
        print(f"[done] {keyword!r} saved={saved} reason={reason} elapsed={dt:.1f}s",
              file=sys.stderr)
        ntfy(f"{keyword!r}: saved {saved} videos ({reason}, {dt:.0f}s)",
             title=f"TikTok web: {keyword}")

        pause = random.uniform(INTER_TERM_SLEEP_MIN, INTER_TERM_SLEEP_MAX)
        print(f"[sleep] {pause:.1f}s before next term", file=sys.stderr)
        time.sleep(pause)


if __name__ == "__main__":
    main()
