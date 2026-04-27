"""
24/7 web-search scraper. Pulls pending terms from the `terms` queue,
scrapes www.tiktok.com/api/search/item/full/, upserts to Postgres, pings
ntfy. Auto-recovers from cookie rot via refresh_web_cookie.py --auto.
Halts (with ntfy) only when a fresh login is needed.

Per-account: each running instance is bound to one account via --account
<name>, which resolves accounts/<name>/cookie.py and
accounts/<name>/playwright_profile/. Multiple accounts can run in parallel
as separate processes — the queue is parallel-safe (FOR UPDATE SKIP
LOCKED). Daily search quota (status_code=2484) is per-account, so one
worker getting throttled does not affect the others.
"""

import argparse
import datetime as dt
import os
import random
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from db import (
    claim_next_term,
    mark_term_done,
    mark_term_failed,
    release_term,
    reclaim_stale_terms,
    save_search,
)

skw = None  # lazy import so --dry-run / --help work without an account configured


def _load_scraper():
    global skw
    if skw is None:
        import scrape_keyword_web as _skw
        skw = _skw


# Exit codes from refresh_web_cookie.py — must match the constants there.
REFRESH_EXIT_OK = 0
REFRESH_EXIT_FAIL = 1
REFRESH_EXIT_RATE_LIMITED = 2

# Backoff schedule for self-healing on auto-refresh failure. Each entry is
# (sleep_seconds, label). After the last attempt fails, ntfy + exit. The
# rate-limited path skips straight to the long sleeps since shorter retries
# are pointless during a 1-hour TikTok throttle.
REFRESH_BACKOFF_NORMAL = [(300, "5min"), (900, "15min"), (3600, "60min")]
REFRESH_BACKOFF_RATE_LIMITED = [(3600, "60min"), (3600, "60min")]


def attempt_auto_refresh(account: str) -> int:
    """Run refresh_web_cookie.py --auto --account <name> in a subprocess.
    Returns the refresh script's exit code: 0=ok, 1=fail, 2=rate-limited.
    Inherits DISPLAY env so the headed-but-on-VNC Chromium can render."""
    script = Path(__file__).resolve().parent / "refresh_web_cookie.py"
    print(f"[auto-refresh] running refresh_web_cookie.py --auto --account {account}",
          file=sys.stderr)
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--auto", "--account", account],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        print("[auto-refresh] TIMEOUT after 120s", file=sys.stderr)
        return REFRESH_EXIT_FAIL
    print(result.stdout, file=sys.stderr)
    print(result.stderr, file=sys.stderr)
    return result.returncode

NTFY_TOPIC = "retardedjames-tiktok"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "http://150.136.40.239:2586")
NTFY_URL = f"{NTFY_SERVER}/{NTFY_TOPIC}"

# TikTok per-account daily search quota. Confirmed 2026-04-26: status_msg =
# "You have reached the maximum number of searched today." Account-level,
# resets around 00:00 UTC. A new cookie does NOT help — the throttle is on
# the account, not the session. Sleep until next UTC midnight + jitter.
QUOTA_STATUS_CODE = 2484

MAX_PAGES_DEFAULT = 50
LIKE_FLOOR_DEFAULT = 1000

INTER_TERM_SLEEP_MIN = 3
INTER_TERM_SLEEP_MAX = 10
INTER_PAGE_SLEEP_MIN = 0.5
INTER_PAGE_SLEEP_MAX = 2.0
REJECT_BACKOFF_SECONDS = 300

# If many consecutive terms come back with zero raw items on page 0, the
# cookie is probably expired/revoked — TikTok serves a 200 with an empty
# list rather than an error. Flag it as a session failure once we hit this
# many in a row. Only counts terms where the API returned ZERO items
# (suspected cookie rot); a term that returned items but all under the
# like floor is a legit empty result and does not increment the counter.
ZERO_RESULT_HALT_THRESHOLD = 4

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


class WebQuotaExceeded(Exception):
    """Raised when page 0 returns status_code=2484 ("max searches today").
    Per-account daily quota — refreshing the cookie does NOT help. Main
    loop handles by sleeping until the next UTC midnight + jitter."""


def seconds_until_next_utc_midnight(jitter_max: int = 600) -> int:
    """Seconds from now until the next 00:00 UTC, plus 0..jitter_max
    random seconds so multiple accounts/workers don't thunder back."""
    now = dt.datetime.now(dt.timezone.utc)
    tomorrow = (now + dt.timedelta(days=1)).date()
    next_midnight = dt.datetime.combine(tomorrow, dt.time.min, tzinfo=dt.timezone.utc)
    delta = (next_midnight - now).total_seconds()
    return int(delta) + random.randint(0, jitter_max)


def ntfy(message: str, *, title: str | None = None, priority: str | None = None,
         account: str | None = None) -> None:
    """Per-account ntfy. Adds [<account>] prefix so multiple workers'
    notifications are distinguishable on the phone."""
    try:
        prefix = f"[{account}]" if account else "[web]"
        message = f"{prefix} {message}"
        if title:
            title = f"{prefix} {title}"
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
               sort_type: int, publish_time: int,
               *, account: str) -> tuple[int, str, list[dict]]:
    seen_ids: set[str] = set()
    collected: list[dict] = []
    page = 0
    cursor = 0
    search_id = ""
    stop_reason: str | None = None

    while page < max_pages:
        print(f"  [page {page}] cursor={cursor}", file=sys.stderr)
        parsed, impr_id, fetch_ms = skw.fetch_page(keyword, cursor, search_id,
                                                    sort_type, publish_time,
                                                    account=account)
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
        #   - 2484: per-account daily search quota. Cookie is fine, account is
        #     throttled. Refresh won't help — sleep until next UTC midnight.
        #   - anything else: session-level reject (auth wall, captcha, etc.)
        if page == 0 and status_code == 403:
            raise WebKeywordBlocked(
                f"keyword blocked (status_code=403, status_msg={parsed.get('status_msg')!r})")
        if page == 0 and status_code == QUOTA_STATUS_CODE:
            raise WebQuotaExceeded(
                f"daily quota hit (status_code={status_code}, "
                f"status_msg={parsed.get('status_msg')!r})")
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
             sort_type: int, publish_time: int,
             *, account: str) -> tuple[int, int, str]:
    """Returns (saved, total_raw, stop_reason). `total_raw` is the unfiltered
    item count returned by the API — distinguishes cookie-rot (total_raw=0)
    from legitimate empty results (total_raw>0 but all under the like floor)."""
    keyword = term["term"]
    total, reason, raws = scrape_one(keyword, floor, max_pages, sort_type,
                                     publish_time, account=account)
    to_save = [r for r in raws
               if ((r.get("statistics") or {}).get("digg_count") or 0) >= floor]
    dropped = len(raws) - len(to_save)
    if not to_save:
        print(f"  [db] nothing to save ({len(raws)} scraped, {dropped} under floor)",
              file=sys.stderr)
        return 0, total, reason
    saved = save_search(keyword, str(sort_type), to_save)
    print(f"  [db] saved {saved} videos ({dropped} under {floor}-like floor dropped)",
          file=sys.stderr)
    return saved, total, reason


def try_auto_refresh_with_backoff(account: str) -> bool:
    """Try a silent cookie refresh; if it fails, retry with backoff.
    Returns True if a refresh ultimately succeeded, False if all retries
    failed (caller should escalate to human-login halt)."""
    rc = attempt_auto_refresh(account)
    if rc == REFRESH_EXIT_OK:
        return True

    if rc == REFRESH_EXIT_RATE_LIMITED:
        print("[auto-refresh] account rate-limited — waiting it out",
              file=sys.stderr)
        schedule = REFRESH_BACKOFF_RATE_LIMITED
    else:
        schedule = REFRESH_BACKOFF_NORMAL

    for sleep_s, label in schedule:
        print(f"[auto-refresh] sleeping {label} before retry...", file=sys.stderr)
        time.sleep(sleep_s)
        rc = attempt_auto_refresh(account)
        if rc == REFRESH_EXIT_OK:
            print(f"[auto-refresh] success after {label} backoff", file=sys.stderr)
            return True
        print(f"[auto-refresh] retry after {label} still failing (rc={rc})",
              file=sys.stderr)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=str, required=True,
                    help="account name (looks up accounts/<name>/cookie.py "
                         "and accounts/<name>/playwright_profile/)")
    ap.add_argument("--floor", type=int, default=LIKE_FLOOR_DEFAULT)
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES_DEFAULT)
    ap.add_argument("--sort-type", type=int, default=1,
                    help="0=default, 1=most-liked, 2=least-liked (default 1)")
    ap.add_argument("--publish-time", type=int, default=0,
                    help="0=all, 7/30/90/180=last N days (default 0)")
    ap.add_argument("--stale-minutes", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    account = args.account

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
            ntfy(msg, title="Web scraper: queue drained", account=account)
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
            saved, total, reason = run_once(term, args.floor, args.max_pages,
                                            args.sort_type, args.publish_time,
                                            account=account)
        except WebKeywordBlocked as e:
            print(f"[blocked] {keyword!r}: {e} — marking done(0), continuing",
                  file=sys.stderr)
            mark_term_done(claimed_id, 0)
            claimed_id = None
            # 403 is a legitimate per-keyword block, not a cookie problem.
            # Reset both counters so a cluster of moderation-blocked terms
            # doesn't trip the zero-result halt + auto-refresh.
            consecutive_rejects = 0
            consecutive_zero_results = 0
            time.sleep(random.uniform(INTER_TERM_SLEEP_MIN, INTER_TERM_SLEEP_MAX))
            continue
        except WebQuotaExceeded as e:
            # Per-account daily search quota. Refresh would be wasted; the
            # account itself is throttled, not the cookie. Release the term
            # (so a different worker / a different account could pick it up)
            # and sleep until the next UTC midnight + jitter.
            sleep_s = seconds_until_next_utc_midnight()
            wake_at = (dt.datetime.now(dt.timezone.utc)
                       + dt.timedelta(seconds=sleep_s))
            print(f"[quota] {e} — sleeping {sleep_s}s until {wake_at:%Y-%m-%d %H:%M UTC}",
                  file=sys.stderr)
            release_term(claimed_id)
            claimed_id = None
            ntfy(f"Daily search quota hit; sleeping until {wake_at:%H:%M UTC}. "
                 f"No action needed.",
                 title="TikTok web scraper: daily quota",
                 account=account)
            consecutive_rejects = 0
            consecutive_zero_results = 0
            consecutive_errors = 0
            time.sleep(sleep_s)
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
                     priority="high", account=account)
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
                print(f"[auto-refresh] {consecutive_errors} consecutive errors — "
                      "attempting silent cookie refresh", file=sys.stderr)
                if try_auto_refresh_with_backoff(account):
                    consecutive_errors = 0
                    time.sleep(random.uniform(INTER_TERM_SLEEP_MIN, INTER_TERM_SLEEP_MAX))
                    continue

                msg = (f"Web scraper halting: auto-refresh failed after backoff "
                       f"retries. Login needed: VNC into VPS and run "
                       f"`refresh_web_cookie.py --account {account} --fresh`. "
                       f"Last error: {type(e).__name__}: {e}. "
                       f"Last term: {keyword!r}.")
                print(f"[halt] {msg}", file=sys.stderr)
                ntfy(msg, title="TikTok web scraper: login required",
                     priority="high", account=account)
                sys.exit(1)

            time.sleep(random.uniform(INTER_TERM_SLEEP_MIN, INTER_TERM_SLEEP_MAX))
            continue

        consecutive_rejects = 0
        consecutive_errors = 0
        elapsed = time.time() - t0

        if saved == 0 and total == 0:
            # Suspected cookie rot: API returned zero raw items. Release back
            # to pending (don't burn the term as done(0)) and don't ntfy —
            # the per-term notification is noise until we know the cookie is
            # actually broken. Halt + auto-refresh once threshold trips.
            consecutive_zero_results += 1
            print(f"[zero] {keyword!r}: 0 raw items "
                  f"(consecutive={consecutive_zero_results}/{ZERO_RESULT_HALT_THRESHOLD}) "
                  f"reason={reason}", file=sys.stderr)
            release_term(claimed_id)
            claimed_id = None

            if consecutive_zero_results >= ZERO_RESULT_HALT_THRESHOLD:
                print(f"[auto-refresh] {consecutive_zero_results} consecutive zero-result "
                      "terms — attempting silent cookie refresh", file=sys.stderr)
                if try_auto_refresh_with_backoff(account):
                    consecutive_zero_results = 0
                    time.sleep(random.uniform(INTER_TERM_SLEEP_MIN, INTER_TERM_SLEEP_MAX))
                    continue

                msg = (f"Web scraper halting: {consecutive_zero_results} consecutive "
                       f"terms returned 0 raw items and auto-refresh failed after "
                       f"backoff. Login needed: VNC into VPS and run "
                       f"`refresh_web_cookie.py --account {account} --fresh`. "
                       f"Last term: {keyword!r}.")
                print(f"[halt] {msg}", file=sys.stderr)
                ntfy(msg, title="TikTok web scraper: login required",
                     priority="high", account=account)
                sys.exit(1)

            time.sleep(random.uniform(INTER_TERM_SLEEP_MIN, INTER_TERM_SLEEP_MAX))
            continue

        # API returned items (total>0). Either we saved some, or all were
        # below the like floor — both are legitimate "this term is done"
        # outcomes; cookie is healthy.
        consecutive_zero_results = 0
        mark_term_done(claimed_id, saved)
        claimed_id = None
        print(f"[done] {keyword!r} saved={saved} total={total} reason={reason} "
              f"elapsed={elapsed:.1f}s", file=sys.stderr)
        if saved > 0:
            ntfy(f"{keyword!r}: saved {saved} videos ({reason}, {elapsed:.0f}s)",
                 title=f"TikTok web: {keyword}", account=account)

        pause = random.uniform(INTER_TERM_SLEEP_MIN, INTER_TERM_SLEEP_MAX)
        print(f"[sleep] {pause:.1f}s before next term", file=sys.stderr)
        time.sleep(pause)


if __name__ == "__main__":
    main()
