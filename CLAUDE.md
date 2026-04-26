# direct_api — pure-HTTP TikTok web scraper

Scrapes TikTok's logged-in **web** search API (`www.tiktok.com/api/search/item/full/`)
with logged-in browser cookies. No mobile signing, no Frida, no Waydroid.
Runs 24/7 on the Oracle VPS under systemd; auto-recovers from cookie rot
via a headless Playwright refresh; only pages the human (ntfy) when the
account itself needs a fresh login.

**Multi-account:** each account has its own dir under `accounts/<name>/`
(cookie + Chromium profile). One templated systemd unit per account
(`tiktok-web-scraper@<name>.service`) — workers share the queue via
`FOR UPDATE SKIP LOCKED`, so parallel-safe by construction. Per-account
daily quota (`status_code=2484`) means one account getting throttled
doesn't block the others.

> **Ignore everything in `OLD/`.** That directory holds the archived
> mobile/Frida/Waydroid scraper stack (Frida-signed `/aweme/v1/search/item/`
> via TT Lite on Waydroid VMs). Kept for reference only — none of it is
> part of the current pipeline. If you find yourself reading anything
> under `OLD/`, you're probably solving the wrong problem.

## Architecture

```
                          ┌──────────────────────────────────┐
                          │  Oracle VPS  150.136.40.239      │
                          │  (Ubuntu 24.04 ARM64)            │
                          │                                  │
   you ──VNC :5901──────▶ │  Xvnc + xfce4 (display :1)       │
   (only when login       │      └─ Chromium (logged-in)     │
    needed; "james" pw)   │                                  │
                          │  systemd --user (templated)      │
                          │  ├─ tiktok-web-scraper@20mythoughts.service │
                          │  ├─ tiktok-web-scraper@21mythoughts.service │
                          │  └─ ...                          │
                          │       continual_scraper_web.py   │
                          │           --account <name>       │
                          │           ├─ scrape_keyword_web  │
                          │           │     /api/search/...  │
                          │           ├─ db.save_search ──┐  │
                          │           └─ on cookie rot:   │  │
                          │              refresh --auto   │  │
                          │              --account <name> │  │
                          │                               ▼  │
                          │  PostgreSQL (local)              │
                          │  ntfy server :2586               │
                          └──────────────────────────────────┘
```

The whole pipeline lives on the VPS. The web API is unsigned — a valid
`sid_guard` cookie + `msToken` is all you need. No request signing.

## What runs where

| Component | Location | Purpose |
|---|---|---|
| `continual_scraper_web.py` | VPS, systemd unit | 24/7 queue worker; pulls pending terms, scrapes, upserts to Postgres |
| `scrape_keyword_web.py` | VPS, library | Single-keyword pagination; called by continual + standalone |
| `refresh_web_cookie.py` | VPS, on-demand | Cookie refresh: headed (human via VNC) or `--auto` (silent self-heal) |
| `db.py` | VPS, library | SQLAlchemy models + queue helpers (`claim_next_term`, etc.) |
| `web_remap.py` | VPS, library | Maps web-schema items → mobile-schema dicts so `db.save_search` doesn't care which scraper wrote them |
| `web_cookie.py` | VPS, gitignored | Live cookie + UA. Auto-rewritten by `refresh_web_cookie.py` |
| Postgres | VPS, port 5432 | `tiktoks` DB, `terms` queue + `videos`/`authors`/`searches`/`search_results` |
| ntfy | VPS, port 2586 | Push notifications: term completions + halt alerts |

Connect from your laptop:
- **SSH**: `ssh -i ~/.ssh/id_rsa ubuntu@150.136.40.239`
- **VNC**: `150.136.40.239:5901`, password `james`

## Cookie lifecycle (the heart of the system)

The web cookie has a short lifetime — TikTok soft-revokes after some
unknown velocity threshold, returning **HTTP 200 with a zero-byte body**
(distinct from the silent-reject pattern of the mobile API). The scraper
treats 3 consecutive `JSONDecodeError` (or any generic exception) as a
cookie-rot signal.

**Three failure modes the scraper distinguishes:**

| Page-0 response | Meaning | Scraper action |
|---|---|---|
| `status_code = 0`, items populated | Success | Save to DB |
| `status_code = 403` | Keyword-level block (content moderation; e.g. "water fasting", "OMAD diet") | `WebKeywordBlocked` → mark term done(0), advance |
| `status_code != 0, != 403` | Session-level reject (auth wall, captcha) | `WebReject` → backoff + halt after 3 consecutive |
| HTTP 200, empty body → `JSONDecodeError` | Cookie soft-revoked | Generic except → release-not-fail; trigger auto-refresh after 3 consecutive |

**Auto-refresh flow** (silent recovery, no human in loop):

1. Scraper hits `ERROR_HALT_THRESHOLD = 3` consecutive errors.
2. Spawns `refresh_web_cookie.py --auto` as subprocess.
3. The script reuses the existing logged-in profile at `.playwright_profile/`,
   navigates to `tiktok.com/search?q=help` under stealth-patched Chromium
   (DISPLAY=:1, the VNC server's X session), waits for `networkidle`,
   grabs cookies, writes [web_cookie.py](web_cookie.py).
4. Verifies with a real `fetch_page("mario")` call.
5. On success: scraper `importlib.reload`s `web_cookie` + `scrape_keyword_web`,
   resets counter, resumes. No restart needed.
6. On failure (login wall, captcha, account flagged): scraper ntfys
   "login required" + exits. Systemd will restart but auto-refresh keeps
   failing until you VNC in.

**When you need to log in fresh** (account expired, IP banned, etc.):

```bash
ssh -i ~/.ssh/id_rsa ubuntu@150.136.40.239
cd ~/direct_api && source .venv/bin/activate
DISPLAY=:1 python3 refresh_web_cookie.py --fresh
# (then VNC in, log in to TikTok, do any search, ssh terminal: touch /tmp/refresh_web_cookie.ready)
systemctl --user restart tiktok-web-scraper
```

`--fresh` deletes `.playwright_profile/` so you log in from a clean slate.

## Running locally (for development)

The same code runs fine on your laptop too — just point at the same
Postgres. Useful for one-off keyword tests:

```bash
cd ~/direct_api
python3 scrape_keyword_web.py mario --no-db
python3 scrape_keyword_web.py "kawaii desk" --floor 5000 --max-pages 30
```

For the queue-driven loop, prefer the VPS (it's the production runner) —
running it locally too will compete for terms via the `FOR UPDATE SKIP
LOCKED` claim, but won't break anything.

## Database

Postgres on the VPS itself. Schema (shared with the archived mobile
scraper, hence `web_to_mobile` remapping):

- `authors` — TikTok user
- `videos` — one per aweme_id, with denormalized stats
- `searches` — one row per (keyword, sort_type) scrape run
- `search_results` — many-to-many (search ↔ video), rank-ordered
- `terms` — the **queue**: `(id, term, type, status, ...)`. `status ∈
  {pending, in_progress, done, failed}`. `claim_next_term` uses `FOR
  UPDATE SKIP LOCKED` for safe parallel workers.

Connection (set in `db.py`): `db=tiktoks user=app1_user password=app1dev host=localhost`.

## Sensitive data — never commit

- `web_cookie.py` — live `sid_guard` + `msToken` for the active account
- `.playwright_profile/` — Chromium cookies/IndexedDB for the same account
- `OLD/libs/libmetasec_ov.so` — extracted ByteDance binary (1.8MB)
- `OLD/ttapk/*.apk` — TT Lite split-APKs (redistribution risk)
- `OLD/HISTORICAL/data/` — captured oracles, real session cookies
- `OLD/replay_search_vm*.py` — per-VM identity files

All gitignored.

## Common ops

```bash
# Watch the live scraper
ssh -i ~/.ssh/id_rsa ubuntu@150.136.40.239 'journalctl --user -u tiktok-web-scraper -f'

# Queue health
ssh -i ~/.ssh/id_rsa ubuntu@150.136.40.239 'cd ~/direct_api && source .venv/bin/activate && python3 -c "
from sqlalchemy import text; from db import engine
with engine.begin() as c:
    for s in [\"pending\", \"in_progress\", \"done\", \"failed\"]:
        n = c.execute(text(\"SELECT COUNT(*) FROM terms WHERE status=:s\"), {\"s\": s}).scalar()
        print(f\"{s}: {n}\")"'

# Reset wrongly-failed terms (e.g. if scraper churned during a cookie outage)
# UPDATE terms SET status='pending', started_at=NULL, completed_at=NULL
#  WHERE status='failed' AND completed_at > now() - interval '90 minutes';

# Service control
systemctl --user restart tiktok-web-scraper
systemctl --user status  tiktok-web-scraper
```
