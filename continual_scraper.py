"""
Continual scraper: pulls pending search terms from the `terms` queue one at
a time, signs + runs the TikTok search paginated, upserts results into
Postgres, and pings ntfy with the count. Sleeps (jittered) between terms to
stay under TikTok's per-account velocity thresholds.

Stop conditions (beyond normal scrape pagination):
  - Silent-reject: page-0 returns aweme_list=null with server_stream_time
    below ~200ms. First hit → mark term as failed, sleep 10 minutes, keep
    going. If the *next* term also silent-rejects on page 0, the signer or
    session is cooked; ntfy a failure alert and exit.
  - Ctrl-C / crash: release the in_progress row back to pending so it'll be
    picked up again (reclaim_stale_terms covers the case where the process
    is killed hard).

Usage:
  python3 continual_scraper.py
  python3 continual_scraper.py --floor 5000 --max-pages 20
  python3 continual_scraper.py --dry-run           # claim/release only, no scrape
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

# scrape_keyword triggers FridaSigner() at import time — defer so --dry-run
# and --help work on machines without the TT Lite signer attached.
sk = None


def _load_signer_module():
    global sk
    if sk is None:
        import scrape_keyword as _sk
        sk = _sk

NTFY_TOPIC = "retardedjames-tiktok"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "http://150.136.40.239:2586")
NTFY_URL = f"{NTFY_SERVER}/{NTFY_TOPIC}"
NTFY_PREFIX = os.environ.get("NTFY_PREFIX", "")  # e.g. "[vm3]" to distinguish workers

MAX_PAGES_DEFAULT = 50
LIKE_FLOOR_DEFAULT = 1000

INTER_TERM_SLEEP_MIN = 30   # seconds
INTER_TERM_SLEEP_MAX = 90
INTER_PAGE_SLEEP_MIN = 0.5
INTER_PAGE_SLEEP_MAX = 2.0
REJECT_BACKOFF_SECONDS = 600  # 10 minutes after a suspected silent-reject

SILENT_REJECT_SST_MS = 200  # anything below this with empty aweme_list → rejected


class SilentReject(Exception):
    """Raised when page 0 comes back empty with a suspiciously fast
    server_stream_time — almost certainly our signature was rejected."""


def ntfy(message: str, *, title: str | None = None, priority: str | None = None) -> None:
    """Best-effort push to ntfy.sh. Never raises — a notification failure
    must not kill the scraper."""
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


def scrape_one(keyword: str, floor: int, max_pages: int) -> tuple[int, str, list[dict]]:
    """Run the paginated search for one keyword. Same control flow as
    scrape_keyword.scrape(), but inspects page-0's server_stream_time so we
    can distinguish silent-reject from genuinely-no-results, and adds a
    small jittered sleep between pages.

    Returns (total_collected, stop_reason, raws). Raises SilentReject on
    suspected rejection.
    """
    seen_ids: set[str] = set()
    collected: list[dict] = []
    page = 0
    cursor = 0
    search_id = ""
    stop_reason: str | None = None

    while page < max_pages:
        print(f"  [page {page}] cursor={cursor}", file=sys.stderr)
        parsed, logid, sign_ms = sk.fetch_page(keyword, cursor, search_id)
        sst = (parsed.get("extra") or {}).get("server_stream_time")
        aweme_list = parsed.get("aweme_list") or []
        has_more = parsed.get("has_more")
        print(f"  [page {page}] sign={sign_ms}ms sst={sst}ms items={len(aweme_list)}",
              file=sys.stderr)

        # Silent-reject signatures seen in the wild:
        #   - aweme_list=null + sst ~80ms  (the classic, from HANDOFF)
        #   - aweme_list=null + sst missing entirely (rate-limited state, 2026-04-24)
        # Either way: page-0 empty with no proof the server actually ran the
        # query = treat as reject, not as "no results."
        if page == 0 and not aweme_list:
            is_reject = (not isinstance(sst, int)) or sst < SILENT_REJECT_SST_MS
            if is_reject:
                raise SilentReject(
                    f"page-0 aweme_list empty, sst={sst!r} "
                    f"(accepted requires sst >= {SILENT_REJECT_SST_MS}ms)"
                )

        if page == 0 and logid:
            search_id = logid

        if not aweme_list:
            stop_reason = f"aweme_list empty (has_more={has_more}, sst={sst}ms)"
            break

        all_below_floor = True
        for a in aweme_list:
            aid = a.get("aweme_id")
            if not aid or aid in seen_ids:
                continue
            seen_ids.add(aid)
            collected.append(a)
            digg = ((a.get("statistics") or {}).get("digg_count") or 0)
            if digg >= floor:
                all_below_floor = False

        if all_below_floor:
            stop_reason = f"all {len(aweme_list)} items on page < {floor} likes"
            break

        if has_more == 0 or has_more is False:
            stop_reason = "has_more=0"
            break

        server_next_cursor = parsed.get("cursor")
        if isinstance(server_next_cursor, int) and server_next_cursor > cursor:
            cursor = server_next_cursor
        else:
            cursor += sk.PAGE_SIZE
        page += 1

        time.sleep(random.uniform(INTER_PAGE_SLEEP_MIN, INTER_PAGE_SLEEP_MAX))
    else:
        stop_reason = f"hit max_pages={max_pages}"

    return len(collected), stop_reason, collected


def run_once(term: dict, floor: int, max_pages: int) -> tuple[int, str]:
    """Scrape one term and persist. Returns (videos_saved, stop_reason).
    Raises SilentReject up to the main loop on page-0 rejection."""
    keyword = term["term"]
    total, reason, raws = scrape_one(keyword, floor, max_pages)
    to_save = [r for r in raws
               if ((r.get("statistics") or {}).get("digg_count") or 0) >= floor]
    dropped = len(raws) - len(to_save)
    if not to_save:
        print(f"  [db] nothing to save ({len(raws)} scraped, {dropped} under floor)",
              file=sys.stderr)
        return 0, reason
    saved = save_search(keyword, "1", to_save)
    print(f"  [db] saved {saved} videos ({dropped} under {floor}-like floor dropped)",
          file=sys.stderr)
    return saved, reason


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--floor", type=int, default=LIKE_FLOOR_DEFAULT)
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES_DEFAULT)
    ap.add_argument("--stale-minutes", type=int, default=30,
                    help="Reclaim in_progress rows older than this (default 30)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Claim/release rows without actually scraping")
    args = ap.parse_args()

    claimed_id: int | None = None

    def handle_sigint(signum, frame):
        # Signal handlers are async — just flag and let the main loop clean up
        # on its next iteration. But for a Ctrl-C mid-scrape, we want to
        # release immediately.
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
        _load_signer_module()

    reclaimed = reclaim_stale_terms(args.stale_minutes)
    if reclaimed:
        print(f"[startup] reclaimed {reclaimed} stale in_progress rows", file=sys.stderr)

    consecutive_rejects = 0

    while True:
        term = claim_next_term()
        if not term:
            msg = "Queue empty — no pending search terms left."
            print(f"[queue] {msg}", file=sys.stderr)
            ntfy(msg, title="Continual scraper: queue drained")
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
            saved, reason = run_once(term, args.floor, args.max_pages)
        except SilentReject as e:
            consecutive_rejects += 1
            print(f"[reject] {e} (consecutive={consecutive_rejects})", file=sys.stderr)
            mark_term_failed(claimed_id)
            claimed_id = None

            if consecutive_rejects >= 2:
                msg = (f"Continual scraper halting: {consecutive_rejects} consecutive "
                       f"silent-rejects. Signer or session likely dead. "
                       f"Last term: {keyword!r}.")
                print(f"[halt] {msg}", file=sys.stderr)
                ntfy(msg, title="TikTok scraper: halted on repeated rejects",
                     priority="high")
                sys.exit(1)

            print(f"[reject] backing off {REJECT_BACKOFF_SECONDS}s before next term",
                  file=sys.stderr)
            time.sleep(REJECT_BACKOFF_SECONDS)
            continue
        except Exception as e:
            print(f"[error] {type(e).__name__}: {e}", file=sys.stderr)
            mark_term_failed(claimed_id)
            claimed_id = None
            ntfy(f"Scraper error on {keyword!r}: {type(e).__name__}: {e}",
                 title="TikTok scraper: term failed")
            # Don't treat generic errors as reject signal; short pause and continue.
            time.sleep(random.uniform(INTER_TERM_SLEEP_MIN, INTER_TERM_SLEEP_MAX))
            continue

        consecutive_rejects = 0
        dt = time.time() - t0
        mark_term_done(claimed_id, saved)
        claimed_id = None
        print(f"[done] {keyword!r} saved={saved} reason={reason} elapsed={dt:.1f}s",
              file=sys.stderr)
        ntfy(f"{keyword!r}: saved {saved} videos ({reason}, {dt:.0f}s)",
             title=f"TikTok: {keyword}")

        pause = random.uniform(INTER_TERM_SLEEP_MIN, INTER_TERM_SLEEP_MAX)
        print(f"[sleep] {pause:.1f}s before next term", file=sys.stderr)
        time.sleep(pause)


if __name__ == "__main__":
    main()
