"""
Open a Chromium browser so you can log in / search on tiktok.com, then
read the cookies back out of the browser context and rewrite web_cookie.py.
After writing, verify with a real /api/search/item/full/ call. Optionally
reset failed terms and relaunch continual_scraper_web.py.

Usage:
  python3 refresh_web_cookie.py                  # refresh + verify
  python3 refresh_web_cookie.py --restart        # also relaunch the scraper
  python3 refresh_web_cookie.py --restart \
      --reset-failed 1190,1191,1192              # also flip those rows back to pending

The script:
  1. Launches Chromium with a persistent profile under
     ./.playwright_profile/  (so you stay logged in across runs).
  2. Navigates to www.tiktok.com.
  3. Waits until it sees a request to /api/search/ — that proves the
     session is real (not a login wall) before we trust the cookies.
  4. Reads context.cookies() for .tiktok.com, formats them into the
     same `name=value; ...` string web_cookie.py expects, and rewrites
     the file in place. USER_AGENT is taken live from the browser so
     it always matches the session.
  5. Verifies by importlib.reload-ing scrape_keyword_web and doing one
     fetch_page() call against a benign keyword. Aborts if it fails.
  6. (Optional) Resets the given term ids back to pending.
  7. (Optional) Relaunches continual_scraper_web.py in a new logfile.
"""

import argparse
import datetime as dt
import importlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

PROJECT_DIR = Path(__file__).resolve().parent
COOKIE_FILE = PROJECT_DIR / "web_cookie.py"
PROFILE_DIR = PROJECT_DIR / ".playwright_profile"
LOG_DIR = PROJECT_DIR / "logs"

SEARCH_URL_PATTERN = re.compile(r"tiktok\.com/api/(search|recommend|item|post|user|aweme)/")
VERIFY_KEYWORD = "mario"


def format_cookie_header(cookies: list[dict]) -> str:
    parts = []
    for c in cookies:
        domain = c.get("domain", "")
        if "tiktok.com" not in domain:
            continue
        parts.append(f"{c['name']}={c['value']}")
    return "; ".join(parts)


def write_cookie_file(cookie_str: str, user_agent: str) -> None:
    body = (
        '"""Logged-in www.tiktok.com cookie for the web-search scraper.\n\n'
        'Gitignored. Auto-written by refresh_web_cookie.py — do not hand-edit\n'
        'unless you know what you\'re doing. Re-run that script if scraping\n'
        'starts silent-failing (msToken rotates within hours).\n'
        '"""\n\n'
        f'COOKIE = (\n    {cookie_str!r}\n)\n\n'
        f'USER_AGENT = (\n    {user_agent!r}\n)\n'
    )
    COOKIE_FILE.write_text(body)


def grab_cookies(ready_signal_path: Path) -> tuple[str, str]:
    PROFILE_DIR.mkdir(exist_ok=True)
    # playwright-stealth patches the standard automation tells (navigator.webdriver,
    # missing chrome.runtime, headless UA hints, WebGL vendor strings, etc.).
    # Without these patches TikTok flags the session as a bot.
    stealth = Stealth()
    with stealth.use_sync(sync_playwright()) as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        print("[refresh] navigating to tiktok.com...", file=sys.stderr)
        page.goto("https://www.tiktok.com/", wait_until="domcontentloaded")

        # Wait for the user to signal "ready". Detecting tiktok's API requests is
        # unreliable (they hit *.tiktokv.com and other hosts that vary by region
        # and feature) — simpler to let the user say "I've logged in and done a
        # search, grab the cookies now". Two ways to signal:
        #   1. `touch <ready_signal_path>` from another shell
        #   2. press Enter in this script's terminal (if attached to a tty)
        ready_signal_path.unlink(missing_ok=True)
        print(f"[refresh] log in + do a search in the browser, then signal ready:",
              file=sys.stderr)
        print(f"[refresh]   touch {ready_signal_path}", file=sys.stderr)
        print(f"[refresh]   (or press Enter if running in foreground)",
              file=sys.stderr)

        # Poll for the file or stdin
        import select
        while True:
            if ready_signal_path.exists():
                print("[refresh] ready signal received", file=sys.stderr)
                break
            if sys.stdin.isatty():
                rlist, _, _ = select.select([sys.stdin], [], [], 0.5)
                if rlist:
                    sys.stdin.readline()
                    print("[refresh] Enter pressed", file=sys.stderr)
                    break
            else:
                time.sleep(0.5)

        ready_signal_path.unlink(missing_ok=True)

        cookies = ctx.cookies()
        cookie_str = format_cookie_header(cookies)
        user_agent = page.evaluate("() => navigator.userAgent")
        ctx.close()
    return cookie_str, user_agent


def verify_cookie() -> bool:
    """Force-reload web_cookie + scrape_keyword_web (in case they were
    imported earlier with the stale cookie), then do one real page-0 fetch.
    Returns True iff status_code==0 and item_list is non-empty."""
    import web_cookie
    importlib.reload(web_cookie)
    if "scrape_keyword_web" in sys.modules:
        skw = importlib.reload(sys.modules["scrape_keyword_web"])
    else:
        import scrape_keyword_web as skw

    print(f"[verify] fetching page 0 for {VERIFY_KEYWORD!r}...", file=sys.stderr)
    try:
        parsed, impr_id, fetch_ms = skw.fetch_page(VERIFY_KEYWORD, 0, "", 1, 0)
    except Exception as e:
        print(f"[verify] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return False

    item_list = parsed.get("item_list") or []
    status_code = parsed.get("status_code")
    print(f"[verify] fetch={fetch_ms}ms items={len(item_list)} "
          f"status_code={status_code}", file=sys.stderr)

    if status_code not in (0, None):
        print(f"[verify] FAIL: status_code={status_code} "
              f"status_msg={parsed.get('status_msg')!r}", file=sys.stderr)
        return False
    if not item_list:
        print("[verify] FAIL: item_list empty (silent-reject pattern)",
              file=sys.stderr)
        return False
    print("[verify] OK", file=sys.stderr)
    return True


def reset_failed_terms(term_ids: list[int]) -> None:
    from sqlalchemy import text
    from db import engine
    with engine.begin() as conn:
        result = conn.execute(
            text("UPDATE terms SET status='pending', started_at=NULL, "
                 "completed_at=NULL WHERE id = ANY(:ids) AND status='failed'"),
            {"ids": term_ids},
        )
        print(f"[reset] flipped {result.rowcount} failed term(s) back to pending",
              file=sys.stderr)


def restart_scraper() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"continual_web_{stamp}.log"
    cmd = ["python3", "-u", str(PROJECT_DIR / "continual_scraper_web.py")]
    log_fh = open(log_path, "w")
    proc = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT,
                            cwd=str(PROJECT_DIR), start_new_session=True)
    # Update the tmp pointer continual_scraper_web.py uses to find its log.
    Path("/tmp/web_scraper_logfile.txt").write_text(
        str(log_path.relative_to(PROJECT_DIR)) + "\n")
    print(f"[restart] launched continual_scraper_web.py pid={proc.pid}",
          file=sys.stderr)
    print(f"[restart] log: {log_path}", file=sys.stderr)
    print(f"[restart] tail: tail -f {log_path}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ready-signal", type=str,
                    default="/tmp/refresh_web_cookie.ready",
                    help="path to touch from another shell to signal 'grab "
                         "cookies now' (default /tmp/refresh_web_cookie.ready)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the verification fetch")
    ap.add_argument("--reset-failed", type=str, default="",
                    help="comma-separated term ids to flip from failed→pending "
                         "after a successful verify (e.g. 1190,1191,1192)")
    ap.add_argument("--restart", action="store_true",
                    help="relaunch continual_scraper_web.py after verify")
    ap.add_argument("--fresh", action="store_true",
                    help="delete the persistent Chromium profile before launching "
                         "(forces fresh login, drops any flagged fingerprint state)")
    args = ap.parse_args()

    if args.fresh and PROFILE_DIR.exists():
        print(f"[refresh] --fresh: removing {PROFILE_DIR}", file=sys.stderr)
        shutil.rmtree(PROFILE_DIR)

    cookie_str, user_agent = grab_cookies(Path(args.ready_signal))
    if "sessionid=" not in cookie_str or "sid_guard=" not in cookie_str:
        print("[refresh] WARNING: cookie missing sessionid/sid_guard — "
              "you may not be logged in. Writing anyway.", file=sys.stderr)
    write_cookie_file(cookie_str, user_agent)
    n_cookies = cookie_str.count(";") + 1 if cookie_str else 0
    print(f"[refresh] wrote {COOKIE_FILE} ({n_cookies} cookies, "
          f"UA len={len(user_agent)})", file=sys.stderr)

    if args.no_verify:
        print("[refresh] --no-verify: skipping verification", file=sys.stderr)
    else:
        if not verify_cookie():
            print("[refresh] verification failed — NOT resetting terms or "
                  "restarting scraper. Try refresh again.", file=sys.stderr)
            sys.exit(1)

    if args.reset_failed:
        try:
            ids = [int(x) for x in args.reset_failed.split(",") if x.strip()]
        except ValueError:
            print(f"[refresh] bad --reset-failed value: {args.reset_failed!r}",
                  file=sys.stderr)
            sys.exit(2)
        if ids:
            reset_failed_terms(ids)

    if args.restart:
        restart_scraper()

    print("[refresh] done.", file=sys.stderr)


if __name__ == "__main__":
    main()
