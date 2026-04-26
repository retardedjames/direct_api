# direct_api — pure-HTTP TikTok scraper

Scrapes TikTok Lite's mobile search API (`/aweme/v1/search/item/`) with no
phone, no Waydroid, no mitmproxy. Requests are signed on a live TT Lite
process via Frida RPC; results with ≥1k likes are upserted into the shared
Postgres (`tiktoks` DB on Oracle VPS `150.136.40.239`).

See HANDOFF.md for the full architecture + history, including why prior
approaches (public MSSDK signers, x86 Waydroid + Houdini) did not work.

## Key files

| File | Purpose |
|---|---|
| `scrape_keyword.py` | **Production entry point.** Paginates cursor=0→10→20→… for one keyword, writes videos to Postgres via `db.save_search`. |
| `replay_search_frida.py` | Single-page smoke test — sign + call + print verdict. |
| `replay_search.py` | Authoritative device/cookie/token constants (`DEVICE`, `COOKIE`, `X_TT_TOKEN`, `USER_AGENT`) + the RapidAPI single-page reference path. |
| `frida_signer.py` | Python wrapper over `frida.get_usb_device().attach(TT_pid)` that exposes `sign_request(url, headers) → {X-Argus, X-Gorgon, X-Khronos, X-Ladon}`. |
| `frida/sign_agent.js` | Persistent Frida agent loaded into TT Lite; exposes `rpc.exports.sign`. |
| `frida/hook_metasec.js` | Discovery script — hooks `art::JNI<...>::RegisterNatives` and finds `libmetasec_ov.so`'s single JNI entrypoint (`ms.bd.o.k.a`). |
| `frida/trace_sign.js` | Tracer — logs every call to `ms.bd.o.k.a` (filters out noisy string-decoder op `0x01000001`). |
| `db.py` | SQLAlchemy models (`Author`, `Video`, `Search`, `SearchResult`) + `save_search(keyword, sort_type, aweme_infos)`. Shared schema with the previous mobile scraper. |
| `capture_oracles.py` | Burns RapidAPI quota to produce known-good (query, sig, response) tuples under `oracles/`. Use sparingly — BASIC plan = 20/day. |
| `diff_signers.py` / `dump_argus_protobufs.py` | Offline tools to compare candidate signers against captured oracles. |
| `libs/libmetasec_ov.so` | Extracted ByteDance security SDK, ARM64. Gitignored. |

## Running

**Smoke test (ARM VM, TT Lite must be running, frida-server 16.6.6):**
```bash
python3 replay_search_frida.py mario
# Expect: ACCEPTED aweme_count=30 sst=~700ms
```

**Full keyword scrape → Postgres:**
```bash
python3 scrape_keyword.py mario
python3 scrape_keyword.py "kawaii desk" --floor 5000 --max-pages 30
python3 scrape_keyword.py mario --no-db           # skip DB write
```

Output: one JSONL line per unique video to stdout (or `--out file`). stderr
carries per-page progress + DB summary. Videos with <1000 likes are dropped
before the DB write.

## Infrastructure

| Role | Host | Purpose |
|---|---|---|
| **ARM64 Waydroid VM** | `34.133.197.84` | Runs TT Lite + frida-server 16.6.6. Signer currently runs here; so does `scrape_keyword.py` until we add a remote-signer proxy (Step 5 in HANDOFF). |
| Postgres | `150.136.40.239` (Oracle VPS) | `db=tiktoks user=app1_user password=app1dev`. Same schema as the retired mobile scraper. |
| x86 Waydroid VM | `34.171.201.223` | Static RE only — Frida can't hook ARM64 code under Houdini. Keep for reading `libmetasec_ov.so` memory / symbols. |

SSH to any VM: `ssh -i ~/.ssh/jamescvermont jamescvermont@<IP>`

## Important quirks

- **Silent-reject pattern**: bad signatures return HTTP 200 + `aweme_list=null`
  + `status_code=0` + `server_stream_time≈80ms`. Valid: populated list +
  `server_stream_time>200ms`. Check response shape, not HTTP status.
- **Signing latency**: ~1.3s per page via Frida (vs ~200ms via RapidAPI). The
  `FridaSigner` keeps one attached session — don't re-instantiate per page.
- **Parameter order matters**: `SEARCH_PARAM_ORDER` in `scrape_keyword.py`
  must match the canonical phone-capture order; MSSDK hashes the canonical
  query string.
- **search_id carries forward**: page-0's `x-tt-logid` response header becomes
  the `search_id` query param for every subsequent page in that session.
- **Session cookies**: `replay_search.py:COOKIE` holds the logged-in
  `sid_guard` (valid until 2026-10-18). After expiry, recapture from a real
  phone session.

## Why we need this vs. the old mitmproxy scraper

TikTok rate-limits per-account when the app is driven at scrape-speed. One
account → few keywords/day. Pure HTTP with stolen device+cookies means one
logged-in device signs thousands of queries/day without UI friction.

## Sensitive data — never commit

- `libs/libmetasec_ov.so` (1.8MB binary)
- `oracles/*.json` (real session cookies + signed headers)
- `traces/*.log` (live JNI trace logs include `odin_tt` / `install_id`)
- `captured_*.json*` (raw phone captures with cookies)
- `replay_search.py` contains `COOKIE` + `X_TT_TOKEN` + `RAPIDAPI_KEY` —
  don't push this repo to a public GitHub remote without scrubbing first.

All listed paths (except `replay_search.py`) are in `.gitignore`.
