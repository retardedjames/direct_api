"""
Open a Chromium browser so you can log in / search on tiktok.com, then
read the cookies back out of the browser context and rewrite the
account's cookie.py. After writing, verify with a real
/api/search/item/full/ call.

Per-account layout — every account has its own dir:
  accounts/<account>/cookie.py
  accounts/<account>/playwright_profile/

Usage:
  python3 refresh_web_cookie.py --account 20mythoughts
  python3 refresh_web_cookie.py --account 20mythoughts --fresh
  python3 refresh_web_cookie.py --account 20mythoughts --auto

The script:
  1. Launches Chromium with the account's persistent profile so you stay
     logged in across runs.
  2. Navigates to www.tiktok.com.
  3. (Headed mode) Waits for you to log in and signal "ready"; (--auto)
     navigates to a search page, waits for networkidle, grabs cookies.
  4. Reads context.cookies() for .tiktok.com, formats them into the
     `name=value; ...` string the scraper expects, and rewrites
     accounts/<account>/cookie.py. USER_AGENT is taken live from the
     browser so it always matches the session.
  5. Verifies with a real fetch_page() call against a benign keyword.
"""

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

PROJECT_DIR = Path(__file__).resolve().parent
ACCOUNTS_DIR = PROJECT_DIR / "accounts"


def account_paths(account: str) -> tuple[Path, Path]:
    """Return (cookie_file, profile_dir) for the given account."""
    base = ACCOUNTS_DIR / account
    return base / "cookie.py", base / "playwright_profile"

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


def write_cookie_file(cookie_file: Path, cookie_str: str, user_agent: str) -> None:
    body = (
        '"""Logged-in www.tiktok.com cookie for the web-search scraper.\n\n'
        'Gitignored. Auto-written by refresh_web_cookie.py — do not hand-edit\n'
        'unless you know what you\'re doing. Re-run that script if scraping\n'
        'starts silent-failing (msToken rotates within hours).\n'
        '"""\n\n'
        f'COOKIE = (\n    {cookie_str!r}\n)\n\n'
        f'USER_AGENT = (\n    {user_agent!r}\n)\n'
    )
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    cookie_file.write_text(body)


def grab_cookies(profile_dir: Path, ready_signal_path: Path | None,
                 auto: bool) -> tuple[str, str]:
    profile_dir.mkdir(parents=True, exist_ok=True)
    # playwright-stealth patches the standard automation tells (navigator.webdriver,
    # missing chrome.runtime, headless UA hints, WebGL vendor strings, etc.).
    # Without these patches TikTok flags the session as a bot.
    stealth = Stealth()
    with stealth.use_sync(sync_playwright()) as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if auto:
            # Headless-style flow: navigate to a search page so msToken rotates,
            # wait a bit for XHRs/scripts to finish, then grab. No human in loop.
            target = "https://www.tiktok.com/search?q=help"
            print(f"[refresh] auto mode: navigating to {target}", file=sys.stderr)
            page.goto(target, wait_until="domcontentloaded")
            # Let scripts run + msToken rotation happen.
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            time.sleep(3)
            cookies = ctx.cookies()
            cookie_str = format_cookie_header(cookies)
            user_agent = page.evaluate("() => navigator.userAgent")
            ctx.close()
            return cookie_str, user_agent

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


# Verify-result codes — also used as process exit codes so the scraper can
# distinguish "rate-limited, wait it out" from "real login required".
VERIFY_OK = "ok"
VERIFY_RATE_LIMITED = "rate_limited"  # status_code=2484 "Try again in 1 hour"
VERIFY_FAIL = "fail"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_RATE_LIMITED = 2

# TikTok web rate-limit code. Confirmed 2026-04-26: status_msg = "Too many
# attempts. Try again in 1 hour." Account-level, not cookie-level — the
# cookie is fine, just sleep it off.
RATE_LIMIT_STATUS_CODE = 2484


def verify_cookie(account: str) -> str:
    """Do one real page-0 fetch using the account's freshly-written cookie.
    No importlib dance needed: scrape_keyword_web reads the cookie file on
    every fetch, so the new file is picked up automatically.
    Returns one of VERIFY_OK / VERIFY_RATE_LIMITED / VERIFY_FAIL."""
    import scrape_keyword_web as skw

    print(f"[verify] fetching page 0 for {VERIFY_KEYWORD!r} (account={account})...",
          file=sys.stderr)
    try:
        parsed, impr_id, fetch_ms = skw.fetch_page(VERIFY_KEYWORD, 0, "", 1, 0,
                                                    account=account)
    except Exception as e:
        print(f"[verify] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return VERIFY_FAIL

    item_list = parsed.get("item_list") or []
    status_code = parsed.get("status_code")
    print(f"[verify] fetch={fetch_ms}ms items={len(item_list)} "
          f"status_code={status_code}", file=sys.stderr)

    if status_code == RATE_LIMIT_STATUS_CODE:
        print(f"[verify] RATE-LIMITED: status_code={status_code} "
              f"status_msg={parsed.get('status_msg')!r} — cookie is fine, "
              "account is throttled", file=sys.stderr)
        return VERIFY_RATE_LIMITED
    if status_code not in (0, None):
        print(f"[verify] FAIL: status_code={status_code} "
              f"status_msg={parsed.get('status_msg')!r}", file=sys.stderr)
        return VERIFY_FAIL
    if not item_list:
        print("[verify] FAIL: item_list empty (silent-reject pattern)",
              file=sys.stderr)
        return VERIFY_FAIL
    print("[verify] OK", file=sys.stderr)
    return VERIFY_OK


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=str, required=True,
                    help="account name (paths derived: accounts/<name>/cookie.py "
                         "and accounts/<name>/playwright_profile/)")
    ap.add_argument("--ready-signal", type=str, default="",
                    help="path to touch from another shell to signal 'grab "
                         "cookies now'. Defaults to "
                         "/tmp/refresh_web_cookie.<account>.ready")
    ap.add_argument("--auto", action="store_true",
                    help="non-interactive: navigate to /search?q=help, wait for "
                         "page settle, grab cookies. Use when re-using an existing "
                         "logged-in profile (no human needed).")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the verification fetch")
    ap.add_argument("--fresh", action="store_true",
                    help="delete the persistent Chromium profile before launching "
                         "(forces fresh login, drops any flagged fingerprint state)")
    args = ap.parse_args()

    cookie_file, profile_dir = account_paths(args.account)
    ready_signal = Path(args.ready_signal) if args.ready_signal else \
        Path(f"/tmp/refresh_web_cookie.{args.account}.ready")

    if args.fresh and profile_dir.exists():
        print(f"[refresh] --fresh: removing {profile_dir}", file=sys.stderr)
        shutil.rmtree(profile_dir)

    cookie_str, user_agent = grab_cookies(
        profile_dir,
        ready_signal if not args.auto else None,
        auto=args.auto,
    )
    if "sessionid=" not in cookie_str or "sid_guard=" not in cookie_str:
        print("[refresh] WARNING: cookie missing sessionid/sid_guard — "
              "you may not be logged in. Writing anyway.", file=sys.stderr)
    write_cookie_file(cookie_file, cookie_str, user_agent)
    n_cookies = cookie_str.count(";") + 1 if cookie_str else 0
    print(f"[refresh] wrote {cookie_file} ({n_cookies} cookies, "
          f"UA len={len(user_agent)})", file=sys.stderr)

    if args.no_verify:
        print("[refresh] --no-verify: skipping verification", file=sys.stderr)
    else:
        result = verify_cookie(args.account)
        if result == VERIFY_RATE_LIMITED:
            print("[refresh] cookie was written but account is rate-limited — "
                  "exiting with code 2 (caller should sleep, not re-login)",
                  file=sys.stderr)
            sys.exit(EXIT_RATE_LIMITED)
        if result != VERIFY_OK:
            print("[refresh] verification failed. Try refresh again.",
                  file=sys.stderr)
            sys.exit(EXIT_FAIL)

    print("[refresh] done.", file=sys.stderr)


if __name__ == "__main__":
    main()
