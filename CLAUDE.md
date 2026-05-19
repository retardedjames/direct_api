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
| `95.217.215.96` (Hetzner cx23 Helsinki, x86_64) | `ssh -i ~/.ssh/hetzner_key root@95.217.215.96` | _placeholder, login TBD_ | 2 vCPU / 4 GB RAM — large enough for headed Chromium. Also runs the Piped `yt-proxy-hel` workload. |
| `35.209.37.192` (GCP yt-proxy-us1, e2-micro) | `ssh -i ~/.ssh/google_compute_engine james@35.209.37.192` | — | **Not bootstrapped:** 1 GB RAM is too small for headed Chromium + Playwright. |
| `34.138.158.255` (GCP instance-1, e2-micro) | `ssh -i ~/.ssh/claude_key ubuntu@34.138.158.255` | — | **Not bootstrapped:** 1 GB RAM, same constraint. |
| `100.48.8.98` (AWS yt-proxy-aws-us, t2.micro) | `ssh -i ~/.ssh/id_ed25519 ubuntu@100.48.8.98` (must chmod 600 the key) | — | **Not bootstrapped:** 1 GB RAM, same constraint. AWS instance `i-04efe2a38e1df774f`, key-pair `boombox-james`; new instance launched 2026-05-18 (old IP 54.145.145.26 was retired). |

When a new VM joins the fleet, append a row here.

## Bring-up runbook for a new per-account VPS

The old `direct-api-clean-base` GCP image is gone (it was snapshotted
from a GCP instance that no longer exists). `bringup_clone.sh` will not
work without that image — use the slow path below.

### Slow path — provisioning a brand-new VM from a stock image

Use this when there's no clean base image to clone (or when migrating
to a different distro / region). Spin up any Linux x86_64 VM.

**1. Provision auth.** Add `~/.ssh/jamescvermont.pub` to
`~/.ssh/authorized_keys` for user `jamescvermont`. Passwordless sudo.

**2. Install system packages:**
```bash
sudo apt update && sudo apt install -y \
    python3-venv git tigervnc-standalone-server xfce4 \
    dbus-x11 tmux
```

**3. Clone repo + venv:**
```bash
cd ~ && git clone <direct_api remote> direct_api
cd direct_api
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

`requirements.txt` pins `brotli` (TikTok responses are br-encoded — without it
`scrape_keyword_web` blows up at import time), plus playwright/sqlalchemy/psycopg2.

**4. TigerVNC (localhost-only):** put this in `~/.vnc/xstartup`:
```bash
#!/bin/sh
unset SESSION_MANAGER DBUS_SESSION_BUS_ADDRESS
exec startxfce4
```
Then:
```bash
chmod +x ~/.vnc/xstartup
vncpasswd          # set the VNC password
vncserver :1 -geometry 1280x800 -depth 24 -localhost yes
```
(`-localhost yes` binds 5901 to 127.0.0.1 only.)

**5. Install systemd template + enable linger:**
```bash
mkdir -p ~/.config/systemd/user
cp ~/direct_api/tiktok-web-scraper@.service ~/.config/systemd/user/
systemctl --user daemon-reload
loginctl enable-linger jamescvermont
```

**6. Run the bringup script:**
```bash
cd ~/direct_api
./bringup_clone.sh --account <name>
```
Then follow the printed instructions (steps 1-6 of the fast path).

**7. (Optional) Snapshot this VM as a new clean base image** —
before running the bringup script. That way you only do steps 1-5
once and every future account is the fast path.

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
