# direct_api handoff

Goal: replace the phone+Waydroid+mitmproxy scraper with pure HTTP calls
to TikTok's mobile search API. The blocker was signing — TikTok's MSSDK
computes `X-Argus`/`X-Ladon`/`X-Gorgon`/`X-Khronos` with an algorithm
specific to aid=1340 (TikTok Lite) that no public signer implements
correctly. **Why we need this, not mitmproxy scraping**: TikTok detects
and rate-limits accounts that drive the app at scrape-speed. A
per-account cap makes the app-driven approach untenable at scale. Pure
HTTP with stolen device+cookies (which we have, logged in, warm) means
one account signs thousands of queries per day with no UI friction.

## TL;DR — current state (2026-04-24 late evening) — ✅ WORKING

**Plan D (Frida signer-proxy) succeeded end-to-end.** Running locally-signed
requests through TT Lite on the ARM VM is producing accepted, populated
search responses. Signing takes ~1.3s per request and returns all four
MSSDK headers (`X-Argus`, `X-Gorgon`, `X-Khronos`, `X-Ladon`).

Proof (ARM VM, 2026-04-24):
```
[sig] ['X-Argus', 'X-Gorgon', 'X-Khronos', 'X-Ladon'] in 1281ms (cached_ts=0)
[tiktok] status=200 bytes=2055911
[verdict] ACCEPTED  aweme_count=30  sst=717ms
first_item: "What are those pants?" digg_count=4,637,035 (= mario top hit)
```

Key files that landed (all committed):
- `direct_api/frida/hook_metasec.js` — Frida script that hooks
  `art::JNI<kEnableIndexIds>::RegisterNatives` and discovers
  libmetasec_ov's single JNI entrypoint.
- `direct_api/frida/trace_sign.js` — tracer that logs every call to
  `ms.bd.o.k.a`, filtering out the noisy string-decoder op.
- `direct_api/frida/sign_agent.js` — persistent Frida agent that
  exposes `rpc.exports.sign(url, headers)` backed by live TT Lite.
- `direct_api/frida_signer.py` — Python client that attaches to the
  running TT Lite over adb + frida and relays sign requests.
- `direct_api/replay_search_frida.py` — end-to-end validator:
  `base_headers + build_query → FridaSigner.sign_request →
  call_tiktok` and checks aweme_count/sst for acceptance.
- `direct_api/traces/*.log` (gitignored; contain live `odin_tt` +
  `install_id` session cookies from the ARM VM's TT Lite) — the
  Rosetta Stone from the first successful JNI trace. Regenerate with
  `ssh jamescvermont@34.133.197.84 'python3 /tmp/run_trace.py >
  /tmp/frida-trace.log 2>&1 &'` and scp it back if you need to
  re-derive the contract.

## The signing contract (verified via trace_sign.js)

ByteDance's MSSDK exposes a single JNI entrypoint from
`libmetasec_ov.so`:

```java
class ms.bd.o.k {
    static native Object a(int op, int sub, long ts, String arg, Object payload);
}
```

- registered via `JNI<bool>::RegisterNatives` @ `libmetasec_ov+0xfb27c`
- `op` integer dispatches to different internal services. Known ops:
  - `0x01000001` — string de-obfuscation (thousands of calls per second;
    reflects encrypted Java class/method names at runtime). Ignore.
  - `0x03000001` — **request signing**. `arg`=full URL w/ query,
    `payload`=flat `String[]{k,v,k,v,...}` of existing request headers.
    Returns flat `String[]{"X-Argus",...,"X-Ladon",...}` of headers
    to add. Some endpoints only get a 2-header subset (Gorgon+Khronos
    for monitor/log collectors); search/item gets all 4.
- `ts` is a process-monotonic value (same value across sign calls in
  one process). Passing `0` works; the URL's own `_rticket` / `ts`
  params carry wall-clock.

## Known things that *didn't* work, and why — don't re-try

1. **Public MSSDK signers** (SignerPy 0.12.0, int4444/Metasec,
   iqbalmh18/tiktok-signer) — all target aid=1233 protobuf schemas.
   TikTok returns HTTP 200 + `aweme_list=null` + ~80ms
   server_stream_time. Not usable.
2. **x86 Waydroid + Houdini-translated libmetasec_ov** — Frida CAN
   read ARM64 memory but `Interceptor.attach` fails
   (`unable to intercept function at 0x400021238560`). Houdini
   doesn't route hooks through its translation layer. VM
   `34.171.201.223` remains OK for static RE only.
3. **frida-server 17.9.1 on ARM64 Android 13 LineageOS** — crashes
   on startup with
   `frida_android_helper_service_do_start: assertion failed`.
   **16.6.6 works**; that's the pinned version now.
4. **Frida `Java.use("ms.bd.o.k").a(...)` with `Java.array(...)` as
   the 5th (Object-typed) argument** — fails with
   `argument types do not match` / `expected a pointer`. Frida's
   JS-only array shim isn't a real jobject. **Fix**: build the array
   via `Array.newInstance(String.class, n) + Array.set(...)` so it
   is a real jobject; then direct `K.a(...)` works.
5. **Frida `Class.forName("ms.bd.o.k")`** — throws
   `ClassNotFoundException` because the system classloader doesn't
   see TT's app classloader. Use `Java.use(...).class` instead.
6. **RPC export `cached_ts` in JS** — Frida's Python binding
   camelCases snake_case Python attribute access, so the JS name
   must be camelCase (`cachedTs`) to match.

## RapidAPI key (fresh as of 2026-04-24)

```
RAPIDAPI_KEY  = "2861349ef0mshd02e93636381db1p17b22cjsn20fbcdc12948"
RAPIDAPI_HOST = "bytedance-services.p.rapidapi.com"
```

BASIC plan = 20/day. At the time of writing ~13 left today. Used to
capture the oracles. Don't burn quota debugging signer output — diff
against saved oracles in `oracles/*.json` instead.

Quota probe:
```bash
python3 -c "
import urllib.request, urllib.error, json
body=json.dumps({'method':'GET','query':'ts=1','x_ss_stub':'0'*32,
  'mssdk_app_id':1340,'mssdk_license_id':'224921550',
  'mssdk_version':'v05.01.05-alpha.5-ov-android','mssdk_version_int':83952928,
  'device_id':'1','device_type':'x','channel':'g','os_version':'14','version_name':'1'}).encode()
req=urllib.request.Request('https://bytedance-services.p.rapidapi.com/mssdk_common/sign',data=body,method='POST',
  headers={'content-type':'application/json','x-rapidapi-key':'2861349ef0mshd02e93636381db1p17b22cjsn20fbcdc12948','x-rapidapi-host':'bytedance-services.p.rapidapi.com'})
try:
  with urllib.request.urlopen(req,timeout=20) as r: print('OK remaining:', dict(r.headers).get('X-RateLimit-Requests-Remaining'))
except urllib.error.HTTPError as e: print(e.code, dict(e.headers).get('X-RateLimit-Requests-Remaining'),'/',dict(e.headers).get('X-RateLimit-Requests-Limit'),'reset',dict(e.headers).get('X-RateLimit-Requests-Reset'),'s')
"
```

## Infrastructure inventory

| Role | Host | SSH | Status |
|---|---|---|---|
| x86 Waydroid VM (Houdini-translated ARM) | `34.171.201.223` | `ssh -i ~/.ssh/jamescvermont jamescvermont@34.171.201.223` | Waydroid UP, TT installed. Frida hooks **do not work** (Houdini blocks Interceptor.attach). Use only for static analysis / reading memory. |
| **ARM64 Waydroid VM (primary target)** | `34.133.197.84` | `ssh -i ~/.ssh/jamescvermont jamescvermont@34.133.197.84` | Waydroid UP, TT installed, frida-server 17.9.1 pushed but **crashes on startup**. Need to downgrade to 16.6.6. |
| Oracle ARM VPS | `150.136.40.239` | `ssh -i ~/.ssh/id_rsa ubuntu@150.136.40.239` | Waydroid installed but **SurfaceFlinger crash-loops** (headless VPS GPU issue). Don't use. |
| Postgres DB | `150.136.40.239` | user=`app1_user` db=`tiktoks` password=`app1dev` | Production DB. See global CLAUDE.md. |
| WSL (local) | this machine | — | Dev environment. libmetasec_ov.so + oracles are here. |

### State of the ARM64 VM (`34.133.197.84`) in detail

- User: `jamescvermont` (uid=1001)
- Waydroid 1.6.2, LineageOS 20 GAPPS build (Android 13)
- Container service: `sudo systemctl status waydroid-container` — running
- Start script: `/tmp/wstart.sh` (written by previous session; adapted
  from `phone/waydroid/waydroid-start.sh`)
- After boot: `sys.boot_completed=1`, adb connect `127.0.0.1:5556` works
- TT Lite installed: `com.tiktok.lite.go` under
  `/data/app/~~PuJIMsuF.../com.tiktok.lite.go-PtyvEj3J281y9eyyBgeYZQ==/`
  (all 10 splits)
- frida-server binary: `/data/local/tmp/frida-server` (version 17.9.1;
  replace with 16.6.6 — see next-steps)
- host ptrace_scope = 0; container reads same /proc so also 0

**Known quirks on this ARM VM:**
- `sys.boot_completed` **requires `waydroid show-full-ui` to be running
  *after* session start**; otherwise container freezes before Android
  finishes boot.
- adbd uses existing host `~/.android/adbkey.pub`. Auth is auto if
  the key was injected into `/data/misc/adb/adb_keys` in the container
  during boot.

### Boot procedure (tested, works)

```bash
ssh -i ~/.ssh/jamescvermont jamescvermont@34.133.197.84
bash /tmp/wstart.sh
# If container freezes / boot_completed never goes to 1:
sudo -u jamescvermont env XDG_RUNTIME_DIR=/run/user/1001 WAYLAND_DISPLAY=wayland-1 \
    waydroid show-full-ui > /tmp/ui.log 2>&1 &
# Re-auth adb after show-full-ui unfreezes the container:
adb kill-server && adb connect 127.0.0.1:5556
adb -s 127.0.0.1:5556 shell getprop sys.boot_completed  # should be "1"
```

## What's been built in this workstream

All under `direct_api/`:

| File | Purpose |
|---|---|
| `replay_search.py` | Single-page replay. Signs via RapidAPI, GETs TikTok. Contains `DEVICE`, `MSSDK`, `USER_AGENT`, `COOKIE`, `X_TT_TOKEN` — the logged-in session state copied from phone capture. **Authoritative device fingerprint — use exactly these values.** |
| `scrape_keyword.py` | Full paginator. Loops cursor 0→10→20→…, carries `search_id` from page-0's `x-tt-logid`. Stops on empty / has_more=0 / all-below-floor-likes. Ready to run once we have a working signer. |
| `capture_oracles.py` | Burns RapidAPI quota to produce known-good (query, sig, TikTok-response) tuples under `oracles/`. 3 already captured. |
| `local_signer.py` | Thin wrapper around SignerPy. **Confirmed insufficient** (silent-reject). Kept as structural reference. |
| `try_metasec.py` | Test harness for int4444/tiktok-api Metasec signer. **Confirmed insufficient.** |
| `test_local_signer.py` | End-to-end: sign locally → hit TikTok → check response. |
| `diff_signers.py` | Re-signs a captured oracle query with each candidate signer, prints Argus/Ladon/Gorgon/Khronos side by side. Run `python3 diff_signers.py oracles/oracle_00_mario_c0.json`. |
| `dump_argus_protobufs.py` | Dumps the pre-encryption protobuf field dict that each signer emits, so you can see exactly which fields + values differ. |
| `frida/hook_metasec.js` | Frida script targeting `libart.so!RegisterNatives` to discover libmetasec_ov's JNI methods. **Not yet run successfully** because frida-server won't start on ARM VM. |
| `frida/discover_metasec.js` | Earlier draft of the hook script. `hook_metasec.js` supersedes it. |
| `libs/libmetasec_ov.so` | Extracted from x86 Waydroid's TT. ARM64 ELF, 1.8MB. 764 printable strings (heavily obfuscated — typical ByteDance security SDK). Only one symbol exported: `JNI_OnLoad`. All signing functions are registered dynamically via `RegisterNatives` from inside `JNI_OnLoad`. Gitignored. |
| `oracles/oracle_00_mario_c0.json` | Known-good RapidAPI signature for keyword=mario, cursor=0, count=10. Gitignored (contains session cookies). |
| `oracles/oracle_01_kawaii_desk_c0.json` | Same shape, keyword="kawaii desk". |
| `oracles/oracle_02_cat_videos_c0.json` | Same shape, keyword="cat videos". |

## Key protocol facts (verified — don't re-discover)

### TikTok Lite search-by-likes endpoint (aid=1340)

```
GET https://api19-normal-useast8.tiktokv.us/aweme/v1/search/item/?<query>
```

Only ONE endpoint for both cursor=0 and cursor≥10 (the x86 CLAUDE.md's
two-endpoint design is for full TikTok aid=1233, not Lite).

### Query parameter order (signature-sensitive)

See `scrape_keyword.py:SEARCH_PARAM_ORDER` and `replay_search.py:DEVICE`.
48 parameters, exact order matters — MD5 of the canonical query string is
part of the Argus protobuf input.

### Pagination

- Page 0: `cursor=0`, `search_id=""`. Response header `x-tt-logid` →
  becomes `search_id` for all subsequent pages.
- Page 1+: `cursor=10, 20, ...`, `search_id=<logid>` (constant across
  the full session).
- Server accepts `count=30` (ceiling); phone uses `count=10`.

### Silent-reject pattern

**TikTok does not return 4xx for bad signatures.** It returns
`HTTP 200` + `aweme_list=null` + `status_code=0` +
`extra.server_stream_time ≈ 80ms`. Valid signatures take ~500-700ms and
return a populated `aweme_list`. When testing a new signer, check
`len(aweme_list) > 0` AND `server_stream_time > 200`, not HTTP status.

### Argus size varies

- Phone MSSDK v05.01.05 (aid=1340): **120 decoded bytes**
- RapidAPI (unknown aid, accepted by server): **274 bytes**
- SignerPy (aid=1233): 242 bytes
- Metasec (aid=1233): 258 bytes
- iqbalmh18 (aid=1233): 306 bytes

TikTok accepts 120 AND 274, rejects all three public signers'.
The phone's tiny Argus suggests aid=1340's protobuf is *leaner* than
public ports, not richer.

### Gorgon prefix

`8404` prefix is aid=1340's Gorgon version. Bytes 3,5 are random per
call (not hardcoded — that was another wrong hypothesis). Public
signers either hardcode them or use different constants. Doesn't
directly block acceptance — the hash body inside is what matters.

## Why B (x86 Waydroid + Houdini) failed

Frida runs as a native **x86_64** process. TikTok's `libmetasec_ov.so`
is **ARM64** code translated on-the-fly by Houdini
(`/system/lib64/libhoudini.so`). These live in disjoint memory spaces:

- ARM64 code: `0x4000_00000000 — 0x4000_xxxx_xxxx`
- x86_64 code (incl. Frida trampolines): `0x7c7a_xxxx_xxxx`

Confirmed experimentally:
- `Process.enumerateModules()` does NOT return `libmetasec_ov.so`
- `Process.findRangeByAddress(ARM_ADDR)` DOES return the range
- `ptr(ARM_ADDR).readByteArray()` DOES read memory correctly (valid
  AArch64 `stp x29, x30, [sp, #-0x1b0]!` at JNI_OnLoad offset 0x38560)
- `Interceptor.attach(ARM_ADDR, ...)` **fails**:
  `Error: unable to intercept function at 0x400021238560; please file a bug`

Frida's Interceptor patches x86_64 instructions at the target address.
Houdini's ARM-to-x86 translator maintains its own instruction stream
and doesn't route around patches in the source ARM pages. Hooks never
fire.

Static-read of memory still works from x86 — useful if a future
session wants to scan the lib for the 32-byte AES sign_key (one of
the few high-entropy blobs in `.rodata`) or map the RegisterNatives
call sites — but dynamic tracing requires native ARM64 Frida.

## Why `frida-server-17.9.1-android-arm64` fails on ARM VM

Repro:

```bash
ssh -i ~/.ssh/jamescvermont jamescvermont@34.133.197.84
sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- /data/local/tmp/frida-server -l 0.0.0.0:27042
# Immediate output:
# Bail out! Frida:ERROR:../subprojects/frida-core/src/linux/linux-host-session.vala:704:
#   frida_android_helper_service_do_start: assertion failed: (res == OK)
```

Has already tried:
- **ptrace_scope** set to 0 on host (synced to container /proc). Didn't help.
- **Running as root** via `lxc-attach` (uid=0). Didn't help.
- **SELinux** is Disabled. Not the cause.

Failure is in `frida_android_helper_service_do_start`. This service
installs a dex file into `/data/local/tmp/frida-helper-*.dex` and
executes it via `app_process`. On this Android 13 ARM build, something
in that bootstrap fails. Reports in frida issues suggest a fix in
older versions (16.x line). **Next step is 16.6.6 — do this first.**

## Operational — bring the stack up from cold

### Step 0 — Verify state hasn't drifted

```bash
ssh -i ~/.ssh/jamescvermont jamescvermont@34.133.197.84
sudo waydroid status  # Session=RUNNING, Container=RUNNING
adb -s 127.0.0.1:5556 shell getprop sys.boot_completed  # "1"
adb -s 127.0.0.1:5556 shell pm path com.tiktok.lite.go  # package paths

# If frozen:
bash /tmp/wstart.sh
sudo -u jamescvermont env XDG_RUNTIME_DIR=/run/user/1001 \
    WAYLAND_DISPLAY=wayland-1 waydroid show-full-ui > /tmp/ui.log 2>&1 &
```

### Step 1 — Make sure frida-server 16.6.6 is running

```bash
# Check:
sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- pidof frida-server

# If missing:
nohup sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- \
    /data/local/tmp/frida-server -l 0.0.0.0:27042 > /tmp/frida-server.log 2>&1 &

# Confirm:
~/.local/bin/frida-ps -U | head
~/.local/bin/frida --version  # 16.6.6
```

### Step 2 — Make sure TT Lite is running

```bash
adb -s 127.0.0.1:5556 shell pidof com.tiktok.lite.go
# If missing:
adb -s 127.0.0.1:5556 shell am start -n \
    com.tiktok.lite.go/com.ss.android.ugc.aweme.main.homepage.MainActivity
```

TT pinning on SignUpActivity is fine — the signer works without login;
only the actual `/search/item/` call needs the logged-in device
cookies/tokens (which live in `replay_search.py:DEVICE/COOKIE/X_TT_TOKEN`,
not in the running TT session).

### Step 3 — Smoke-test the signer

```bash
cd ~/direct_api
python3 replay_search_frida.py mario
# Expect:
# [sig] ['X-Argus', 'X-Gorgon', 'X-Khronos', 'X-Ladon'] in ~1200ms
# [tiktok] status=200 bytes=~2MB
# [verdict] ACCEPTED  aweme_count=30  sst=~700ms
```

## Next steps — toward production

### Step 4 — Wire the signer into scrape_keyword.py

`scrape_keyword.py` currently uses `replay_search.call_signer()` (RapidAPI).
Swap it for a `FridaSigner`-backed path, matching the interface in
`replay_search_frida.py`. The FridaSigner is **slow** relative to RapidAPI
(~1.3s vs ~200ms), so keep one process-wide singleton (already the default)
rather than re-attaching per page.

### Step 5 — Remote signer pattern (scraper on JC1, signer on ARM VM)

Today the signer must run on the same machine as adb (the ARM VM). For
production the scraper will run on JC1 or similar, NOT the ARM VM. Two
options:
- **Forward frida-server's TCP socket** to the scraper host. On JC1:
  `ssh -L 27042:127.0.0.1:27042 jamescvermont@34.133.197.84` — then
  `frida.get_device_manager().add_remote_device("127.0.0.1:27042")`.
  Frida also needs adb to resolve the TT pid — so either mirror that,
  or refactor `frida_signer.py` to ask the agent for the pid via an
  `enumerate` RPC and bypass adb on the client side.
- **Run scrape_keyword on the ARM VM directly.** Simpler. Downside:
  one IP per scraper limits concurrency.

Pick option A only when multi-VM scraping becomes necessary.

### Step 6 — Persistent agent (so we don't re-attach per run)

Each invocation of `frida_signer.py` spends ~1-2s attaching + loading
the agent. For a 35,000-sign/month workload that's wasted latency.
Simplest fix: wrap `FridaSigner` in a long-running local HTTP server
(Flask/FastAPI) on the ARM VM that keeps the Frida session alive and
exposes `POST /sign {url, headers}`. `scrape_keyword.py` hits it over
loopback. This is also what the "scraper on a different VM" pattern
wants anyway.

### Step 7 — Keep TT Lite alive

If TT Lite ever dies (OOM, background killer), the Frida session dies
with it. Supervise with either:
- A systemd user service that monitors `adb pidof com.tiktok.lite.go`
  and re-launches + re-attaches if empty.
- Or add a `frida.get_device().on('lost', ...)` handler in the
  long-running server from Step 6 that reattaches automatically.

### Step 8 — Account rotation (future)

The ARM VM's TT install is currently NOT the logged-in account — our
requests use cookies from `replay_search.py:DEVICE`. If that session
expires (sid_guard goes until 2026-10-18), OR if TikTok starts
rate-limiting that device specifically, capture fresh tokens from a
real phone + mitmproxy and update `DEVICE`/`COOKIE`/`X_TT_TOKEN`.
Whether the ARM VM's TT is logged in with the SAME account is
irrelevant — the signer signs URLs, not identities; the cookies we
attach in `call_tiktok` are what TikTok uses for auth.

## Appendix A — Commands to re-capture an oracle

If all current oracles' khronos timestamps are hours old and you want
fresh ground truth:

```bash
cd /home/james/tiktok-scraper/direct_api
python3 capture_oracles.py  # burns 3 RapidAPI calls (3/20 daily)
ls oracles/
python3 diff_signers.py oracles/oracle_00_mario_c0.json  # shows sig diffs
```

## Appendix B — Known-good device identity

In `replay_search.py:DEVICE`:

- aid: 1340
- app_name: musically_go
- device_id: 7630963143929628173
- iid: 7631123284638189325
- device_type: moto g power - 2025
- os_version: 16
- channel: googleplay
- sys_region: US, op_region: US, region: GB
- Logged-in session token + cookies in `X_TT_TOKEN` and `COOKIE`
  (session expires 2026-10-18 per `sid_guard`)

Do NOT regenerate this — TikTok treats this device as a normal,
warmed-up, logged-in user. Starting fresh means warming a new device
(days), logging in (UI), re-capturing tokens.

## Appendix C — If Plan D (Frida signer-proxy) doesn't work

Fallback A: **Static RE of `libmetasec_ov.so`**. Tools: Ghidra or
radare2 on `direct_api/libs/libmetasec_ov.so`. Start at
`JNI_OnLoad @ 0x38560`. Find RegisterNatives call sites. ByteDance
security libs are heavily obfuscated — this is 20-40 hours of work
without a head start.

Fallback B: **Upgrade RapidAPI plan.** Pro tier = 500K calls/month.
Our target is 3,524 keywords × ~10 pages = 35K sign calls/month.
Well within Pro. ~$30/mo. Clearest path to production if Frida doesn't
pan out.

Fallback C: **Multi-account mitmproxy scraper** (the JC1 model).
Rotate 20+ TikTok accounts, each scraping a few keywords/day before
hitting rate limit. Painful but working. JC1 (`34.30.234.222`) proves
the pattern.

## Appendix D — Files NOT to commit

- `direct_api/libs/libmetasec_ov.so` — 1.8MB, easy to re-extract
- `direct_api/oracles/*.json` — contain session cookies + session
  tokens
- `replay_search.py:RAPIDAPI_KEY` — the current key lives in the
  file but shouldn't be committed publicly if that ever changes

Both already in `direct_api/.gitignore`.
