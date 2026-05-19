# direct_api — pure-HTTP TikTok web scraper

Scrapes TikTok's logged-in **web** search API
(`www.tiktok.com/api/search/item/full/`) using logged-in browser cookies.
No mobile signing, no Frida, no Waydroid. Auto-recovers from cookie rot
via headless Playwright; only pages the human (ntfy) when an account
needs a fresh login.

> **Ignore everything in `OLD/`.** That directory holds the archived
> mobile/Frida/Waydroid scraper (Frida-signed `/aweme/v1/search/item/`
> via TT Lite on Waydroid VMs). Kept for reference only — none of it is
> part of the current pipeline. If you find yourself reading anything
> under `OLD/`, you're probably solving the wrong problem.

## Topology — one VPS per TikTok account

TikTok ties cookie + account + IP together. Running multiple accounts
behind one egress IP gets them throttled together (and one stale cookie
poisons the others' apparent reputation), so we pin **one TikTok account
to one VPS**. Each VPS has its own egress IP, its own logged-in
Chromium profile, its own systemd unit, and its own per-account daily
quota; all of them write into the same shared Postgres.

```
                ┌──────────────────────────────────────────┐
                │  Oracle VPS  150.136.40.239 (ARM64)      │
                │   — central services for the whole fleet │
                │                                          │
                │   ├─ PostgreSQL :5432  (tiktoks DB)      │
                │   │   listen_addresses='*'               │
                │   │   pg_hba: app1_user from 0.0.0.0/0   │
                │   ├─ ntfy :2586                          │
                │   ├─ Xvnc :5901 (pw "james")             │
                │   └─ tiktok-web-scraper@{20,21,23}mythoughts.service
                │       (also runs scrapers locally)       │
                └──────────────────────────────────────────┘
                       ▲
                       │ writes
                       │ to Postgres
                       │
   ┌───────────────────┴────────────┐
   │ Per-account satellite VPS      │
   │  account=<name>                │
   │  Xtigervnc :1 / 5901           │
   │  systemd --user:               │
   │  tiktok-web-scraper@<name>     │
   └────────────────────────────────┘
```

The previous GCP satellite fleet (`try2 34.148.104.145` / `again1
34.182.184.254` / `dallas1 34.174.48.214`, accounts `24`/`25`/`27`) and
the `direct-api-clean-base` GCP machine image are **gone** as of
2026-05-18. Bring-ups now use the slow path on whatever VPS is
available; see the fleet roster below.

The web API is unsigned — a valid `sid_guard` + `msToken` cookie pair is
all you need.

## What runs where

| Component | Where | Purpose |
|---|---|---|
| Postgres | Oracle VPS only | `tiktoks` DB, shared by all VMs. Open to 0.0.0.0/0 on `app1_user`. |
| ntfy | Oracle VPS only | Push notifications for term completions + halt alerts |
| `continual_scraper_web.py --account <name>` | Every VPS, systemd `--user` | 24/7 queue worker — claims terms via `FOR UPDATE SKIP LOCKED`, scrapes, upserts |
| `scrape_keyword_web.py` | Every VPS, library | Single-keyword pagination |
| `refresh_web_cookie.py --account <name>` | Every VPS, on-demand | Cookie refresh: headed (human via VNC) or `--auto` (silent self-heal) |
| `db.py` | Every VPS, library | SQLAlchemy. **Defaults to remote Oracle Postgres** — no env override needed on a fresh VM. Override with `TIKTOKS_DATABASE_URL` if needed. |
| `web_remap.py` | Every VPS, library | Maps web-schema items → mobile-schema dicts so `db.save_search` doesn't care which scraper wrote them |
| `accounts/<name>/cookie.py` | Per-VPS, per-account, gitignored | Live `sid_guard` + UA. Auto-rewritten by refresh. |
| `accounts/<name>/playwright_profile/` | Per-VPS, per-account, gitignored | Chromium cookies/IndexedDB |
| `tiktok-web-scraper@<name>.service` | Per-VPS systemd template | One unit per account on that VM. |

## Account naming

The original family on the Oracle VPS used `20mythoughts`, `21mythoughts`,
`23mythoughts`. New per-VM accounts can use whatever you want — just be
consistent: the directory `accounts/<name>/`, the systemd unit
`tiktok-web-scraper@<name>.service`, the refresh ready-file
`/tmp/refresh_web_cookie.<name>.ready`, and the value of `--account`
must all use the same string.

## Fleet roster (2026-05-18)

| VM | SSH | Account(s) | Notes |
|---|---|---|---|
| `150.136.40.239` (Oracle ARM64, Ubuntu 24.04) | `ssh -i ~/.ssh/id_rsa ubuntu@150.136.40.239` | `20mythoughts`, `21mythoughts`, `23mythoughts` | Also hosts Postgres + ntfy. VNC `:5901`, password `james`, **publicly reachable**. |
| `95.217.215.96` (Hetzner cx23 Helsinki, x86_64) | `ssh -i ~/.ssh/hetzner_key root@95.217.215.96` | `28` | Lean install (no VNC, no Xvfb, no headed Chromium). Scraper resident memory ~46 MB. Also runs the Piped `yt-proxy-hel` workload. |
| `35.209.37.192` (GCP yt-proxy-us1, e2-micro) | `ssh -i ~/.ssh/google_compute_engine james@35.209.37.192` | — | Not bootstrapped (1 GB RAM is fine in principle since lean install is tiny, but no account assigned yet). |
| `34.138.158.255` (GCP instance-1, e2-micro) | `ssh -i ~/.ssh/claude_key ubuntu@34.138.158.255` | — | Same — could host an account on the lean install. |
| `100.48.8.98` (AWS yt-proxy-aws-us, t2.micro) | `ssh -i ~/.ssh/id_ed25519 ubuntu@100.48.8.98` (must chmod 600 the key) | — | Same. AWS instance `i-04efe2a38e1df774f`, key-pair `boombox-james`; new instance launched 2026-05-18 (old IP 54.145.145.26 was retired). **AWS public IP changes on stop/start.** |

When a new VM joins the fleet, append a row here.

## Bring-up runbook — lean cookie-import flow (current path)

This is the path verified on Hetzner 2026-05-18. The scraper itself is
pure `urllib.request` (no Playwright, no browser), so a satellite VM
needs no display server, no Chromium, no xfce4 — just Python + the repo.
The only piece that ever needed a browser was `refresh_web_cookie.py`,
and we replace that with a one-time cookie import from a logged-in
browser.

**1. Provision auth.** Whatever user you want; passwordless sudo or root.

**2. Install system packages (lean):**
```bash
sudo apt update && sudo apt install -y python3-venv git
```

**3. Clone repo + venv:**
```bash
cd ~ && git clone https://github.com/retardedjames/direct_api.git
cd direct_api
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```
Skip `playwright install chromium` — we don't launch a browser on the VM.

`requirements.txt` pins `brotli` (TikTok responses are br-encoded — without it
`scrape_keyword_web` blows up at import time), plus sqlalchemy/psycopg2.
Playwright is listed but never imported by the scraper; can be removed
from requirements.txt if you want a smaller install.

**4. Import cookies from a browser where you're already logged in:**

On your laptop, open `www.tiktok.com` in any browser logged into the
target account. Either:
- DevTools → Network → reload → click any `www.tiktok.com` request →
  copy the full `cookie:` request header and the `user-agent:` request
  header; or
- Use the **Cookie-Editor** extension → "Export → JSON" — Claude can
  convert that JSON to the header-string format.

`dallas1` precedent confirms cookies transfer across IPs — you do NOT
need to log in *from* the satellite VM's IP. If a cookie ever does get
rejected after transfer, fall back to the SSH SOCKS5 variant (see "If
imported cookies are rejected" below).

Write `accounts/<name>/cookie.py`:
```python
COOKIE = "ttwid=...; sid_guard=...; sessionid=...; msToken=..."  # full cookie header
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
```
`scrape_keyword_web` extracts the **last** `msToken=` value from the
cookie string, so when multiple are present, order the longer-lived one
last.

**5. Verify the cookie:**
```bash
cd ~/direct_api
.venv/bin/python scrape_keyword_web.py mario --account <name> --no-db
```
Expect `[page 0] items=30 has_more=1 status_code=0`. Anything else and
the cookie was rejected.

**6. Install systemd template + enable linger:**
```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/tiktok-web-scraper@.service <<'EOF'
[Unit]
Description=TikTok web-search scraper (account=%i)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=900
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=%h/direct_api
Environment=PYTHONUNBUFFERED=1
ExecStart=%h/direct_api/.venv/bin/python %h/direct_api/continual_scraper_web.py --account %i
Restart=on-failure
RestartSec=60s

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
loginctl enable-linger $USER
systemctl --user enable --now tiktok-web-scraper@<name>.service
journalctl --user -u tiktok-web-scraper@<name>.service -f
```
No `Environment=DISPLAY=` — the scraper is headless.

**If imported cookies are rejected** (TikTok IP-binds the session):
SOCKS-tunnel through the VM and re-log-in:
```
# On laptop:
ssh -D 1080 -N -i <vm-key> <user>@<vm-ip>
# In Firefox: Settings → SOCKS5 127.0.0.1:1080, "Proxy DNS over SOCKS5"
# Confirm ifconfig.me shows the VM's IP, log into TikTok, re-export cookies.
```

**Auto-refresh:** the lean install drops `refresh_web_cookie.py`'s
auto-recovery path (it needs Playwright + a display + a saved profile).
When cookies rot, the scraper ntfys "login required" and you re-import
manually. On Oracle (legacy installation) the headed/VNC flow is still
present.

## Oracle-specific legacy notes

Oracle (`150.136.40.239`) still has the old setup: Xtigervnc on `:5901`
+ xfce4 + headed Chromium + saved Playwright profile, and the systemd
unit there carries `Environment=DISPLAY=:1`. Auto-refresh works there.
When that VM eventually needs reinstall, switch it to the lean flow
above.

## Cookie lifecycle (the heart of the system)

The web cookie has a short lifetime — TikTok soft-revokes after some
unknown velocity threshold, returning **HTTP 200 with a zero-byte body**.
The scraper treats 3 consecutive `JSONDecodeError` (or any generic
exception) as a cookie-rot signal.

**Failure modes the scraper distinguishes:**

| Page-0 response | Meaning | Scraper action |
|---|---|---|
| `status_code = 0`, items populated | Success | Save to DB |
| `status_code = 403` | Keyword-level block (content moderation; e.g. "water fasting", "OMAD diet") | `WebKeywordBlocked` → mark term done(0), advance |
| `status_code = 2484` | Daily account quota | Sleep until UTC midnight + small jitter, resume |
| `status_code != 0, != 403, != 2484` | Session-level reject (auth wall, captcha) | `WebReject` → backoff + halt after 3 consecutive |
| HTTP 200, empty body → `JSONDecodeError` | Cookie soft-revoked | Generic except → release-not-fail; trigger auto-refresh after 3 consecutive |

**Auto-refresh flow** (silent recovery, no human in loop):

1. Scraper hits `ERROR_HALT_THRESHOLD = 3` consecutive errors.
2. Spawns `refresh_web_cookie.py --auto --account <name>` as subprocess.
3. The script reuses the existing logged-in profile at
   `accounts/<name>/playwright_profile/`, navigates to
   `tiktok.com/search?q=help` under stealth-patched Chromium
   (DISPLAY=:1), waits for `networkidle`, grabs cookies, writes
   `accounts/<name>/cookie.py`.
4. Verifies with a real `fetch_page("mario")` call.
5. On success: scraper `importlib.reload`s the cookie module, resets
   the counter, resumes. No restart needed.
6. On failure (login wall, captcha, account flagged): scraper ntfys
   "login required" + exits. Systemd will restart but auto-refresh
   keeps failing until you VNC in (step 6 of the runbook above, this
   time without `--fresh` unless the profile is wedged).

## Running locally (for development)

The same code runs fine on your laptop — `db.py` defaults to the Oracle
Postgres so a one-off scrape just works:

```bash
cd ~/direct_api
python3 scrape_keyword_web.py mario --no-db
python3 scrape_keyword_web.py "kawaii desk" --floor 5000 --max-pages 30
```

For the queue-driven loop, prefer the VPSs (they're the production
runners) — running it locally too will compete for terms via the
`FOR UPDATE SKIP LOCKED` claim but won't break anything.

## Database

Postgres on the Oracle VPS, listening on all interfaces, accepting
`app1_user` connections from any IP (this is by design — the per-account
VMs need to write to it). Schema:

- `authors` — TikTok user
- `videos` — one per aweme_id, with denormalized stats
- `searches` — one row per (keyword, sort_type) scrape run
- `search_results` — many-to-many (search ↔ video), rank-ordered
- `terms` — the **queue**: `(id, term, type, status, ...)`.
  `status ∈ {pending, in_progress, done, failed}`. `claim_next_term`
  uses `FOR UPDATE SKIP LOCKED` for safe parallel workers across all
  VMs in the fleet.

Connection string baked into `db.py`:
`postgresql://app1_user:app1dev@150.136.40.239:5432/tiktoks` (override
with `TIKTOKS_DATABASE_URL`).

## Sensitive data — never commit

- `accounts/<name>/cookie.py` — live `sid_guard` + `msToken` per account
- `accounts/<name>/playwright_profile/` — Chromium cookies/IndexedDB
- legacy: `web_cookie.py` (older single-account layout)
- `OLD/libs/libmetasec_ov.so` — extracted ByteDance binary
- `OLD/ttapk/*.apk` — TT Lite split-APKs (redistribution risk)
- `OLD/HISTORICAL/data/` — captured oracles, real session cookies
- `OLD/replay_search_vm*.py` — per-VM identity files

All gitignored.

## Common ops

```bash
# Watch a specific account's scraper (replace VM + account name)
ssh -i ~/.ssh/id_rsa ubuntu@150.136.40.239 \
    'journalctl --user -u tiktok-web-scraper@20mythoughts.service -f'

# Queue health (run from any VM, or laptop, since DB is remote-friendly)
ssh -i ~/.ssh/id_rsa ubuntu@150.136.40.239 \
    'cd ~/direct_api && source .venv/bin/activate && python3 -c "
from sqlalchemy import text; from db import engine
with engine.begin() as c:
    for s in [\"pending\", \"in_progress\", \"done\", \"failed\"]:
        n = c.execute(text(\"SELECT COUNT(*) FROM terms WHERE status=:s\"), {\"s\": s}).scalar()
        print(f\"{s}: {n}\")"'

# Reset wrongly-failed terms (e.g. if scraper churned during a cookie outage)
# UPDATE terms SET status='pending', started_at=NULL, completed_at=NULL
#  WHERE status='failed' AND completed_at > now() - interval '90 minutes';

# Service control on a given VM (substitute account name)
systemctl --user restart tiktok-web-scraper@<name>.service
systemctl --user status  tiktok-web-scraper@<name>.service

# List every scraper unit on a VM
systemctl --user list-units 'tiktok-web-scraper@*' --all --no-pager
```
