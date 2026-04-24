# direct_api handoff

Goal: replace the phone+Waydroid+mitmproxy scraper with pure HTTP calls to
TikTok's mobile search API, signed by a live TT Lite process on an ARM
Waydroid VM. **Why**: TikTok rate-limits accounts driven at scrape-speed
through the UI, so the mitmproxy pattern caps at a few keywords/day per
account. One logged-in device + pure HTTP = thousands of queries/day per
account, no UI friction.

For failed approaches, Frida gotchas, and other background that's no
longer needed day-to-day, see `HISTORY.md`.

## TL;DR — current state (2026-04-24) — ✅ END-TO-END SHIPPED

- Frida signer-proxy runs 24/7 on ARM VM `34.133.197.84`.
  `frida-server 16.6.6`, TT Lite pid attached, persistent agent
  `frida/sign_agent.js` exposes `rpc.exports.sign`.
- `scrape_keyword.py` on the ARM VM paginates a keyword, signs each page
  via `FridaSigner`, calls TikTok's `/aweme/v1/search/item/`, and writes
  videos ≥ `--floor` likes (default 1000) to the shared Postgres on
  `150.136.40.239` via `db.save_search`.
- First real run: `"brattleboro vermont"` → 40 videos in 4.6s → 24 saved.
  Smoke test: `mario` → 30 videos, ~700ms server_stream_time.
- Sign latency ~1.3s on first call (attach + warm), ~40–80ms thereafter
  for the life of the process. Keep one long-lived `FridaSigner` —
  don't re-attach per page.

## Architecture

```
    WSL (/home/james/direct_api, dev)
        │  git push origin main
        ▼
    GitHub (retardedjames/direct_api)
        │  git pull
        ▼
    ARM64 Waydroid VM 34.133.197.84  ──► Postgres 150.136.40.239
      ├─ Waydroid 1.6.2 + LineageOS 20 GAPPS (Android 13)       (db=tiktoks, app1_user)
      ├─ TT Lite 24/7 (com.tiktok.lite.go)
      ├─ frida-server 16.6.6 on 127.0.0.1:27042
      ├─ ~/direct_api (git clone; PAT embedded in remote URL)
      └─ scrape_keyword.py + frida_signer.py + sign_agent.js
```

- VM deps: `psycopg2-binary`, `sqlalchemy` installed with
  `pip3 install --user --break-system-packages` (PEP 668).
- VM git remote URL has the gh-auth PAT baked in
  (`https://retardedjames:<token>@github.com/...`), so `git pull` JFW.

## The signing contract

ByteDance's MSSDK exposes one JNI entrypoint from `libmetasec_ov.so`:

```java
class ms.bd.o.k {
    static native Object a(int op, int sub, long ts, String arg, Object payload);
}
```

- Registered via `art::JNI<kEnableIndexIds>::RegisterNatives` @
  `libmetasec_ov+0xfb27c`.
- `op` integer dispatches to internal services. Known ops:
  - `0x01000001` — string de-obfuscation (thousands of calls/sec; noise).
  - `0x03000001` — **request signing**. `arg` = full URL w/ query,
    `payload` = flat `String[]{k,v,k,v,...}` of existing request headers.
    Returns flat `String[]{"X-Argus",...,"X-Ladon",...}`. Some endpoints
    only get a 2-header subset (Gorgon+Khronos for monitor/log
    collectors); `/aweme/v1/search/item/` gets all 4.
- `ts` is process-monotonic (same across sign calls in one process).
  `0` works; the URL's own `_rticket` / `ts` params carry wall-clock.

## Key files

| File | Purpose |
|---|---|
| `scrape_keyword.py` | Production entry point. Paginates cursor=0→10→… for a keyword, writes videos via `db.save_search`. Runs on the ARM VM. |
| `replay_search_frida.py` | Single-page smoke test — sign + call + print verdict. |
| `replay_search.py` | Authoritative `DEVICE` / `COOKIE` / `X_TT_TOKEN` / `USER_AGENT` + the RapidAPI single-page reference path. **Do not regenerate** — `sid_guard` valid until 2026-10-18. |
| `frida_signer.py` | Python client. One singleton per process, keeps one attached session. |
| `frida/sign_agent.js` | Persistent Frida agent loaded into TT Lite; exposes `rpc.exports.sign(url, headers)`. |
| `frida/hook_metasec.js` | Discovery script — used once to find the JNI entrypoint. Kept for re-derivation if libmetasec_ov changes. |
| `frida/trace_sign.js` | Tracer — logs every call to `ms.bd.o.k.a` (filters op `0x01000001`). Kept for future debugging. |
| `db.py` | SQLAlchemy models + `save_search`. Also lives in the parent tiktok-scraper schema. |
| `libs/libmetasec_ov.so` | Extracted ByteDance security SDK, ARM64. **Gitignored.** |

## Infrastructure inventory

| Role | Host | SSH | Notes |
|---|---|---|---|
| **ARM64 Waydroid — Frida signer + scraper** | `34.133.197.84` | `ssh -i ~/.ssh/jamescvermont jamescvermont@34.133.197.84` | GCP t2a, Ubuntu 26.04. Waydroid 1.6.2 + LineageOS 20 GAPPS arm64. TT Lite installed. `frida-server 16.6.6`. `scrape_keyword.py` runs here. |
| x86 Waydroid sandbox | `34.171.201.223` | same key | **Static RE only.** Frida can't `Interceptor.attach` to Houdini-translated ARM64 code — confirmed dead end. Use for reading memory / extracting `libmetasec_ov.so`. See `HISTORY.md`. |
| Oracle ARM VPS — Postgres | `150.136.40.239` | `ssh -i ~/.ssh/id_rsa ubuntu@150.136.40.239` | Prod DB `tiktoks`, `app1_user` / `app1dev`. Shared with the old mobile scraper. |
| APK patching | `34.162.181.247` | see memory | x86 GCP VM with `apktool` + keystore for re-signing TTLite if needed. |

## Database

Shared Postgres on `150.136.40.239`. Current shape:

- `searches` — one row per keyword-scrape (id, keyword, sort_type, searched_at).
- `videos` — aweme_id PK, author_uid FK, stats, author/author-relationship.
- `authors` — uid PK, sec_uid, unique_id, metadata.
- `search_results` — (search_id, video_id, position) link table.
- `terms` — **work queue**. id, term, type, `status`
  (`pending` / `in_progress` / `done` / `failed`), added_at, started_at,
  completed_at, videos_saved, **`done_old_way` BOOLEAN**.

`terms.done_old_way` was added 2026-04-24. All 1,066 then-'done' rows
were backfilled to `done_old_way=TRUE` so we can tell them apart from
new Frida-path completions (which default to `FALSE`). 35 `failed` rows
and 7 `in_progress` rows stayed `FALSE` — no old-path data to preserve.

## Operational — bring the stack up from cold

### Step 0 — Check the VM is alive

```bash
ssh -i ~/.ssh/jamescvermont jamescvermont@34.133.197.84
sudo waydroid status   # Session=RUNNING, Container=RUNNING
adb -s 127.0.0.1:5556 shell getprop sys.boot_completed   # "1"
```

If the container is frozen / `sys.boot_completed` never reaches 1:

```bash
bash /tmp/wstart.sh
sudo -u jamescvermont env XDG_RUNTIME_DIR=/run/user/1001 \
    WAYLAND_DISPLAY=wayland-1 waydroid show-full-ui > /tmp/ui.log 2>&1 &
adb kill-server && adb connect 127.0.0.1:5556
```

`waydroid show-full-ui` **must** run after session start or Android
won't finish boot.

### Step 1 — Confirm frida-server 16.6.6 is running

```bash
adb -s 127.0.0.1:5556 shell "pidof frida-server"
```

If missing, relaunch:

```bash
nohup sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- \
    /data/local/tmp/frida-server -l 0.0.0.0:27042 > /tmp/frida-server.log 2>&1 &
~/.local/bin/frida --version    # 16.6.6
```

**Do not** upgrade to 17.x — crashes on startup on this Android 13 build.
See `HISTORY.md`.

### Step 2 — Confirm TT Lite is running

```bash
adb -s 127.0.0.1:5556 shell pidof com.tiktok.lite.go
# If missing:
adb -s 127.0.0.1:5556 shell am start -n \
    com.tiktok.lite.go/com.ss.android.ugc.aweme.main.homepage.MainActivity
```

The scraper signs URLs, not identities. The cookies/tokens we attach in
`call_tiktok` (from `replay_search.py:DEVICE/COOKIE/X_TT_TOKEN`) are
what TikTok uses for auth — the ARM VM's TT being logged into a
different account is fine.

### Step 3 — Smoke test

```bash
cd ~/direct_api
python3 replay_search_frida.py mario
# Expect: [verdict] ACCEPTED  aweme_count=30  sst=~700ms
```

### Step 4 — Run a keyword scrape

```bash
cd ~/direct_api
git pull
python3 scrape_keyword.py "brattleboro vermont"
python3 scrape_keyword.py mario --floor 5000 --max-pages 30
python3 scrape_keyword.py mario --no-db            # skip Postgres write
```

Output: one JSONL line per unique video to stdout (or `--out file`),
with per-page progress + DB summary on stderr.

## Next steps — still open

### Remote signer pattern (optional)

Today the scraper has to run on the signer host. For multi-VM scraping,
forward the frida-server TCP socket to the scraper host:

```bash
# on scraper host:
ssh -L 27042:127.0.0.1:27042 jamescvermont@34.133.197.84
# then in Python:
frida.get_device_manager().add_remote_device("127.0.0.1:27042")
```

Frida also needs adb to resolve the TT pid. Either mirror adb on the
scraper host, or refactor `frida_signer.py` to ask the agent for the
pid via an `enumerate` RPC. Not worth doing until we actually want more
than one scraper host.

### Persistent signer HTTP server (optional)

Each invocation of `frida_signer.py` costs ~1–2s attach + load. For a
~35K-sign/month workload we can amortize by wrapping `FridaSigner` in a
FastAPI/Flask server on the ARM VM that keeps one Frida session alive
and exposes `POST /sign {url, headers}`. Also simplifies the remote
signer pattern above. Not blocking.

### Supervise TT Lite

If TT Lite dies (OOM, background killer) the Frida session dies with
it. Worth adding either a systemd user service that polls
`adb pidof com.tiktok.lite.go` and re-launches, or a
`frida.get_device().on('lost', ...)` handler in the HTTP server above
to reattach. Not urgent — the process has been stable so far.

### Account / session refresh

`replay_search.py:COOKIE` holds a `sid_guard` valid until **2026-10-18**.
After that (or if the device starts getting flagged), recapture fresh
tokens from a real phone + mitmproxy and update
`DEVICE`/`COOKIE`/`X_TT_TOKEN`. The `DEVICE` dict is the authoritative
warm fingerprint — don't regenerate unless forced.

## Appendix A — Known-good device identity

In `replay_search.py:DEVICE`:

- aid: 1340, app_name: musically_go
- device_id: 7630963143929628173, iid: 7631123284638189325
- device_type: `moto g power - 2025`, os_version: 16
- channel: googleplay, sys_region/op_region: US, region: GB
- Logged-in session token + cookies in `X_TT_TOKEN` and `COOKIE`
  (sid_guard until 2026-10-18)

Do NOT regenerate — TikTok treats this as a normal, warmed-up,
logged-in user. Starting over means days of device warming + UI login +
recapturing tokens.

## Appendix B — Files NOT to commit

Already in `.gitignore`:

- `libs/libmetasec_ov.so` — 1.8MB, easy to re-extract from an APK.
- `oracles/*.json` — contain session cookies + signed headers.
- `traces/*.log` — include live `odin_tt` / `install_id`.
- `captured_*.json*` — raw phone captures with cookies.

`replay_search.py` contains `COOKIE` + `X_TT_TOKEN` + `RAPIDAPI_KEY`
and IS in the repo. The GitHub remote is currently public; user has
opted in to this for now and plans to make it private later.

## Appendix C — RapidAPI

Reference path only (not in the hot path anymore):

```
RAPIDAPI_KEY  = "2861349ef0mshd02e93636381db1p17b22cjsn20fbcdc12948"
RAPIDAPI_HOST = "bytedance-services.p.rapidapi.com"
```

BASIC plan = 20 calls/day. Used for one-off oracles (see
`capture_oracles.py`). Pro tier ($30/mo, 500K calls/month) remains the
cleanest fallback if the Frida stack ever breaks and can't be restored.
Run `diff_signers.py oracles/oracle_00_mario_c0.json` to sanity-check a
new candidate signer against a saved oracle without burning quota.
