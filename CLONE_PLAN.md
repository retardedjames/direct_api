# CLONE_PLAN.md — bring up VM-3 (and any future clone) after VM-1 is rate-limited

**Status:** plan, not yet executed. Author: 2026-04-24 session.
**Audience:** future Claude Code session + the user, executing together.

## Why this exists

VM-1 (`34.134.113.65`) is now silent-rejecting with the rate-limit variant:
HTTP 200 + `aweme_list=null` + `server_stream_time` field **missing entirely**
(not just <200ms). Per `project_tiktok_silent_reject.md`, that's account-level
throttling, not signature-level. **The signer is fine; the account is cooked.**

VM-3 (`34.45.120.10`) was just spun up as a clone (presumably a GCP machine
image of VM-1). Goal of this doc: a repeatable runbook to bring it from
"freshly booted clone" → "scraping into the shared Postgres queue" with a
fresh, unflagged identity, in as few steps as possible. Re-running this
playbook should be the answer next time too.

## What we are NOT doing — and why

- **Not reusing VM-1's `replay_search.py:DEVICE/COOKIE/X_TT_TOKEN`.** The
  cookies are bound to the rate-limited account; the device fingerprint is
  bound (server-side) to that account's `install_id`. Both are tainted. If
  we ship VM-3 with VM-1's `replay_search.py` we'll just inherit the same
  rate-limit state.
- **Not migrating a phone-captured session into VM-3.** `sid_guard` is
  bound to the `install_id` registered the first time TT Lite reached
  ByteDance from a given device. A phone session won't validate against
  Waydroid's install_id (see VM2_PLAN §"Hard constraints" #3).
- **Not skipping fingerprint randomization.** A clone of VM-1 ships with
  identical LXC MAC, identical `ro.serialno` (empty), identical
  `Settings.Secure.android_id` (well, this one's randomized at clone-boot
  but worth re-rolling), identical `ro.product.model` etc. TikTok's
  device-dedup is the most likely thing to flag a clone in <1 hour.
- **Not skipping VNC account creation.** Yes, we need to make a fresh
  account inside VM-3 via VNC so the new `sid_guard` is bound to VM-3's
  newly-registered `install_id` from the start.

The single biggest lesson from VM-1: **the account is the throttled
resource, not the IP and not the signer**. So a new VM is wasted unless it
gets a new account on a new device fingerprint. Everything else is
plumbing.

## High-level shape

```
  VM-1 (34.134.113.65) — rate-limited        VM-3 (34.45.120.10) — this plan
    TT Lite [account-1, throttled]              TT Lite [account-3, fresh]
    Frida signer (still works)                  Frida signer
    continual_scraper.py (silent-rejecting)     continual_scraper.py
                                  │
                                  ▼
                  Postgres terms queue (150.136.40.239)
                  FOR UPDATE SKIP LOCKED — already concurrency-safe
```

VM-1 stays alive, idle, while we test. If VM-3 comes online cleanly we can
rotate: stop VM-1's scraper, leave VM-3 running. If account-1 ever
recovers (unlikely soon) we can flip VM-1 back on. Multi-VM concurrent
scraping is the eventual goal but not required for this clone's first run.

## The "spin up another clone with minimal input" target

After this is debugged once on VM-3, the steady-state recipe should be:

1. `gcloud` clone VM-1's disk → new instance, new IP. (User does this in
   GCP console — it's the part that doesn't need Claude.)
2. SSH in, run `bash scripts/cloneN_bootstrap.sh` (TBD — built out of the
   phases below once they're proven).
3. Wait for VNC instructions; the user opens VNC, makes account, comes
   back.
4. Run `bash scripts/cloneN_capture_and_start.sh` — captures the session,
   patches it into a per-VM identity file, starts continual_scraper.

Goal: the human's only synchronous role is the VNC signup step. Steps 1
and 4 are mostly mechanical.

## Phase 0 — verify the failure mode before doing anything

Don't start cloning yet. Confirm that VM-1's failure is the account-rate-limit
variant (sst missing) and not a transient signer issue, because the latter
would be wasted work to clone away from.

```bash
ssh -i ~/.ssh/jamescvermont jamescvermont@34.134.113.65
cd ~/direct_api
python3 replay_search_frida.py mario
# Expected if rate-limited: ACCEPTED but aweme_count=0, sst MISSING
# (or [verdict] line shows rejection per replay_search_frida.py logic)
# If sst is ~80ms — that's variant A (bad sig); different problem, fix the signer.
# If sst > 200ms with real aweme_count — VM-1 isn't actually rejected; reconsider.
```

Also, sanity-check that VM-3 (`34.45.120.10`) is reachable:

```bash
ssh -i ~/.ssh/jamescvermont jamescvermont@34.45.120.10 "uname -m && cat /etc/os-release | head -3"
# Expect: aarch64, Ubuntu 26.04
```

**Checklist:**
- [ ] VM-1 confirmed rate-limited (variant B, sst missing)
- [ ] VM-3 reachable over SSH
- [ ] VM-3 is aarch64 Ubuntu 26.04

## Phase 1 — discover what the clone actually has

A clone of VM-1 = identical disk = identical `/var/lib/waydroid/**`,
identical TT Lite install, identical `~/direct_api`. Inventory before we
touch anything, so we know what to wipe vs keep.

```bash
ssh -i ~/.ssh/jamescvermont jamescvermont@34.45.120.10
sudo waydroid status              # may say container STOPPED on a fresh boot
ls ~/direct_api 2>/dev/null && echo "repo present"
ls ~/ttapk 2>/dev/null && echo "ttapk present"
ls ~/.local/bin/frida 2>/dev/null && echo "frida client present"
which adb python3                 # base tools
ls /var/lib/waydroid/waydroid_base.prop && cat /var/lib/waydroid/vm_fingerprint.txt 2>/dev/null
ls -la ~/.ssh/                    # whatever keys came along with the clone
```

Three likely states:

- **A. Full clone of VM-1 working state** — repo, TT Lite installed,
  frida-server pushed, `replay_search.py` with VM-1's cookies. We need to
  wipe the TT Lite app data + uninstall TT Lite + re-randomize fingerprint
  + reinstall TT Lite. Heaviest path but most likely.
- **B. Bare Ubuntu image with `~/direct_api` checked in** — must run all of
  `vm2_base_install.sh` first. Less likely if it's a disk-clone of VM-1.
- **C. Clone that doesn't include `~/ttapk/`** — APKs gitignored (see
  CLAUDE.md). If absent, scp them from this laptop or from VM-1.

Treat A as the default and adapt downward if 1+ thing is missing.

**Checklist:**
- [ ] Wrote down (in this session's notes) which of A/B/C the clone is
- [ ] Made a snapshot of `replay_search.py` from the clone — useful as a
      starting template even though we're replacing the values

## Phase 2 — wipe TikTok's existing state on VM-3

If the clone had TT Lite installed and registered (state A), the server
already knows VM-3 by VM-1's install_id. We must invalidate that binding
before VM-3 attempts to use account-3, otherwise the freshly-randomized
fingerprint will conflict with the cached install_id.

On VM-3:

```bash
# Bring up Waydroid + UI just enough to drive adb.
bash ~/direct_api/scripts/vm2_start_display.sh    # or vm2_start_session.sh if no VNC needed yet

# Uninstall TT Lite — wipes /data/data/com.tiktok.lite.go and the per-app
# state ByteDance uses to lock install_id to the device. (cmpl_token,
# device_id cache, etc. live there.)
adb -s 127.0.0.1:5556 uninstall com.tiktok.lite.go || \
    sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- pm uninstall com.tiktok.lite.go || true

# Belt-and-suspenders: clear the data dir if uninstall left scraps.
sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- \
    rm -rf /data/data/com.tiktok.lite.go /data/user/0/com.tiktok.lite.go 2>/dev/null || true

# Stop the session so the next phase (which edits build.prop / LXC config)
# isn't shadowed by a running container.
waydroid session stop || true
```

**Checklist:**
- [ ] `pm path com.tiktok.lite.go` returns nothing
- [ ] No `/data/data/com.tiktok.lite.go` directory inside the container
- [ ] Session stopped

## Phase 3 — re-randomize the device fingerprint

The cloned VM-1 image will have:

- `lxc.net.0.hwaddr` set to whatever VM-1 randomized at first init —
  **same as VM-1**.
- `ro.serialno` set to whatever VM-1's randomize script wrote — **same as
  VM-1**.
- `ro.product.model` and friends — **same as VM-1**.
- `Settings.Secure.android_id` — possibly different (Android sometimes
  re-rolls per-clone, but don't trust it; re-roll explicitly).

The existing `vm2_fingerprint_randomize.sh` does exactly this work and is
idempotent. Run it.

```bash
sudo bash ~/direct_api/scripts/vm2_fingerprint_randomize.sh
```

It will:
- pick a new random LXC MAC,
- pick a different persona (currently hardcoded to Galaxy A54 — see Caveat
  below),
- re-roll `ro.serialno`,
- restart `waydroid-container`.

**Caveat: persona uniqueness.** The script's persona is hardcoded:
`samsung/SM-A546U1`. If we rerun this on each clone, every clone ends up
as a Galaxy A54 with a different MAC + serial. That's per-clone
randomization at the unique-ID level (which is what TikTok's device-dedup
needs) but it's a fleet-wide tell that "all our scrapers are Galaxy A54s
on Android 13 with build A546U1UEU8CXJ1." Acceptable for N=2; should
become a small pool of personas at N≥4.

Don't fix the persona-pool issue this run. Note it as future work.

Then:

```bash
# Bring up the session again so we can apply the runtime-identity bits
# that need adb (android_id / device_name / bluetooth_address).
bash ~/direct_api/scripts/vm2_start_display.sh
bash ~/direct_api/scripts/vm2_apply_runtime_identity.sh
```

**Verification — must do, not nice-to-have:**

```bash
# Quick getprop diff against VM-1 (run from your laptop):
for host in 34.134.113.65 34.45.120.10; do
    echo "=== $host ==="
    ssh -i ~/.ssh/jamescvermont jamescvermont@$host \
        'adb -s 127.0.0.1:5556 shell "getprop ro.serialno; getprop ro.product.model;
                                       settings get secure android_id;
                                       ip link show eth0 | grep -oE \"ether [0-9a-f:]+\""'
done
```

Every value must differ. If anything is identical, redo Phase 3.

The stronger check (what TT Lite actually reads, including
`Settings.Secure.bluetooth_address` and `NetworkInterface.getHardwareAddress`)
needs Frida — defer to after TT Lite reinstall, when we already have a
session attached.

**Checklist:**
- [ ] LXC MAC differs between VM-1 and VM-3
- [ ] `ro.serialno` differs
- [ ] `Settings.Secure.android_id` differs
- [ ] `Settings.Secure.bluetooth_address` differs
- [ ] No identifier matches VM-1 across the whole getprop set (eyeball
      `getprop` output for both VMs side by side)

## Phase 4 — reinstall TT Lite + frida-server

Now (and only now) is it safe to let TikTok's backend register VM-3 fresh.
Run the existing script:

```bash
# If ~/ttapk/ is missing on VM-3 — scp from VM-1 first:
#   ssh -i ~/.ssh/jamescvermont jamescvermont@34.134.113.65 \
#       "tar -czf - ttapk" | \
#   ssh -i ~/.ssh/jamescvermont jamescvermont@34.45.120.10 \
#       "tar -xzf - -C ~"

bash ~/direct_api/scripts/vm2_install_tt.sh
```

This installs the 10-APK split bundle, pushes frida-server 16.6.6, and
launches it inside the LXC.

**libmetasec_ov.so**: VM-1's working `libs/libmetasec_ov.so` is gitignored,
so it's only on the cloned disk if VM-1 had it (state A). Confirm:

```bash
ssh -i ~/.ssh/jamescvermont jamescvermont@34.45.120.10 \
    "ls -la ~/direct_api/libs/libmetasec_ov.so 2>/dev/null"
# If absent:
scp -i ~/.ssh/jamescvermont \
    jamescvermont@34.134.113.65:~/direct_api/libs/libmetasec_ov.so \
    /tmp/libmetasec_ov.so
scp -i ~/.ssh/jamescvermont /tmp/libmetasec_ov.so \
    jamescvermont@34.45.120.10:~/direct_api/libs/
```

The .so is ARM64, version-tied to the TT Lite build. As long as VM-3
installs the same `~/ttapk/` splits as VM-1, the .so is portable.

**TT Lite version drift check** — before launching TT Lite via VNC, confirm
the installed `versionCode` matches what VM-1 had when its libmetasec was
extracted:

```bash
adb -s 127.0.0.1:5556 shell dumpsys package com.tiktok.lite.go | grep versionCode | head -1
# Cross-reference vs VM-1's:
ssh -i ~/.ssh/jamescvermont jamescvermont@34.134.113.65 \
    'adb -s 127.0.0.1:5556 shell dumpsys package com.tiktok.lite.go | grep versionCode | head -1'
```

If they diverge, VM-1's TT Lite has auto-updated since `~/ttapk/` was
captured, and VM-3 is now on an older version. Either: (a) capture fresh
splits from VM-1's `/data/app/.../*.apk` and reinstall on VM-3, or (b)
disable auto-update on VM-3 (`pm disable com.android.vending` after this
phase, but that may break other Play-services-dependent checks).

**Checklist:**
- [ ] TT Lite installed, `pm path` returns base + 9 splits
- [ ] frida-server 16.6.6 PID > 0 inside LXC
- [ ] `libs/libmetasec_ov.so` present in `~/direct_api/libs/` on VM-3
- [ ] versionCode matches VM-1

## Phase 5 — first launch + Frida-confirm fingerprint

This is where ByteDance servers mint VM-3's `install_id` / `device_id`,
binding them to whatever fingerprint Android reports right now. **Don't
skip this** before signup — every change after this point is fighting the
server's cached binding.

Steps:

1. **Connect VNC** from your laptop:
   ```bash
   ssh -L 5901:localhost:5901 -i ~/.ssh/jamescvermont jamescvermont@34.45.120.10
   # In a separate terminal: open a VNC client → localhost:5901
   ```
2. **Launch TT Lite via VNC** (NOT via `adb am start`). Click the icon
   from the launcher. Let it sit on its first-run / region-pick screen for
   ~30s so the registration handshake completes.
3. **Don't sign in yet.** Just let the app do its initial `register/v3/`
   call and any device-attestation pings. The cookies that carry
   `install_id` get minted here.
4. **Confirm with Frida** that TT Lite is reading the randomized values,
   not stale caches. Save this as `~/direct_api/frida/verify_identity.js`
   on VM-3 (or scp from VM-2_PLAN.md's snippet — it's already there
   verbatim). Then:
   ```bash
   ~/.local/bin/frida -U -n com.tiktok.lite.go -l ~/direct_api/frida/verify_identity.js
   ```
   Expect ANDROID_ID, MODEL, FINGERPRINT, SERIAL all matching what we set
   in Phase 3, NOT VM-1's. **If any value is VM-1's**, kill TT Lite,
   uninstall, redo Phase 3 + 4 with stronger randomization.

**Checklist:**
- [ ] TT Lite launched via VNC (not adb)
- [ ] First-run screen reached, sat for 30s
- [ ] Frida hook confirms TT Lite sees randomized identifiers
- [ ] No identifier matches VM-1

## Phase 6 — create account-3 via VNC

Yes — to answer the user's question directly: **you do need to make a new
account through VNC**. There's no shortcut. The reasons:

1. VM-1's `sid_guard` is bound to VM-1's `install_id`, which is bound to
   VM-1's now-rate-limited account. Bringing it to VM-3 just inherits the
   throttle.
2. Phone-captured cookies won't bind to VM-3's `install_id` (see VM2_PLAN
   §"Hard constraints" #3).
3. The signed cookies must be minted *inside* VM-3's TT Lite process so
   `install_id` from the cookie matches `install_id` from
   `libmetasec_ov.so`'s signature.

So: in VNC, on VM-3:

1. **Sign up.** Email + phone path is most reliable. Use a fresh email
   address and a phone number not previously associated with TikTok. If
   SMS verification fails from the GCP datacenter IP, the signup may
   require re-attempting via mobile-tethered residential IP — escalate to
   user if so.
2. **Don't immediately scrape.** Per VM2_PLAN §"Hard constraints" #4 a
   fresh account hammering `/search/item/` gets flagged in <1 hour.
   Spend 10–30 minutes doing UI activity:
   - Scroll the For You feed.
   - Watch 5 videos to completion.
   - Like 2–3 things.
   - Use the search bar (UI) for 1–2 terms.
   - Follow 1 account.
3. **Optional but recommended**: warm for a full day before turning on the
   scraper. Plan acknowledges this is aspirational — for the first clone,
   user may want to skip and accept higher early-flag risk.

**Account creation strategy decision matrix:**

| Strategy | Speed | Flag risk | Notes |
|---|---|---|---|
| Email + real phone, sign up in VNC, warm 30 min, scrape | fast | medium | Recommended. |
| Email + real phone, sign up in VNC, warm 24h, scrape | slow | low | Best, but blocks scraping for a day. |
| Email-only signup (skip phone) | fastest | high | TikTok may force phone verification later. |
| Reuse VM-1's account by transferring cookies | trivial | DOOMED | Will rate-limit again. **Do not do.** |

Default for first VM-3 attempt: middle option (warm 30 min, accept
moderate risk). If it gets flagged in <2h, retry with 24h warming on the
*next* clone.

**Checklist:**
- [ ] account-3 created, email + phone verified
- [ ] >= 30 min of organic UI activity logged
- [ ] No "unusual activity" challenges seen during signup or warming

## Phase 7 — capture VM-3's session into a per-VM identity module

Now we need VM-3's `DEVICE` dict + `COOKIE` + `X_TT_TOKEN`. Not a
mitmproxy tap — the cleanest path is to extend the existing `frida_signer`
infrastructure, since `sign_agent.js` already sees the URL and headers TT
Lite is signing.

The minimal extension: add an RPC-export to `sign_agent.js` that snapshots
the next call's URL + headers (or just dumps them every call until told to
stop), and a one-shot Python script that turns "drive TT Lite UI → search
something → capture the request" into "produce
`replay_search_vm3.py`."

We don't have this script yet. Build outline:

```
~/direct_api/scripts/cloneN_capture_session.sh:
  1. Confirm TT Lite running + Frida agent attached.
  2. Tell user via stdout: "Open TT Lite in VNC, search for the word 'test',
     then come back and press Enter."
  3. Read until Enter.
  4. Frida script (small, embedded) hooks the okhttp / cronet send and
     captures the next /aweme/v1/search/item/ request:
        - Full URL (we extract DEVICE fields by parsing query params)
        - Headers including Cookie + X-Tt-Token + User-Agent
  5. Parse query string -> DEVICE dict.
  6. Render replay_search_vm3.py from a template.
  7. Print summary: "Wrote replay_search_vm3.py with device_id=<id>,
     install_id=<id>; sid_guard expiry=<date>."
```

Implementing this script is the next big TODO. For the first VM-3 run,
**a manual fallback** is acceptable: use an existing Frida-based logger
(extend `sign_agent.js` by hand to print the URL + Cookie of the first
sign call after attach), drive TT Lite once via VNC by searching "test",
pluck the values out of the log, and write `replay_search_vm3.py` by
hand. Painful but unambiguous.

Also possible (maybe simpler for v1): **search via the in-app search bar
once**, capture the URL + headers from `sign_agent.js`'s built-in logging
(if we add it), then build the file. The sign agent already receives URL
+ headers as parameters of every `sign(url, headers)` call — we just
aren't logging them.

Cookie expiry note: the captured `sid_guard` will have an expiry far in
the future (VM-1's was 6 months out). Record the expiry in a comment at
the top of `replay_search_vm3.py` so we know when to refresh.

**Critical**: don't commit `replay_search_vm3.py` to the public repo
(unlike VM-1's `replay_search.py` which user opted into committing). Add
to `.gitignore`. Mention in commit message that this is gitignored.

**Per-VM identity wiring** in `frida_signer.py` / `scrape_keyword.py` /
`continual_scraper.py`: pick one of:

- **Env var switch** (recommended for N≤3, simplest):
  ```python
  vm = os.environ.get("TIKTOK_ACCOUNT")  # "vm1", "vm2", "vm3", ...
  if vm == "vm3":
      from replay_search_vm3 import DEVICE, COOKIE, X_TT_TOKEN, USER_AGENT, TIKTOK_HOST, TIKTOK_PATH
  else:
      from replay_search import DEVICE, COOKIE, X_TT_TOKEN, USER_AGENT, TIKTOK_HOST, TIKTOK_PATH
  ```
  Set `TIKTOK_ACCOUNT=vm3` in VM-3's continual_scraper environment.

- **Hostname-based dispatch**: detect via `socket.gethostname()` or a
  marker file. Ugly. Skip.

- **Per-clone non-tracked identity file** + a lookup table: best at N≥4,
  too much plumbing now.

Go with env var. Update `scrape_keyword.py` (currently the only place
that imports from `replay_search`) accordingly.

**Checklist:**
- [ ] `replay_search_vm3.py` exists on VM-3
- [ ] `replay_search_vm3.py` is in `.gitignore`, NOT committed
- [ ] `scrape_keyword.py` reads `TIKTOK_ACCOUNT` env var
- [ ] `python3 replay_search_frida.py test` returns ACCEPTED with sst > 200ms
      (the smoke test confirms signer + session + device align)

## Phase 8 — manual smoke test before the daemon

```bash
# On VM-3:
export TIKTOK_ACCOUNT=vm3
cd ~/direct_api
python3 replay_search_frida.py mario      # smoke test; must say ACCEPTED
python3 scrape_keyword.py mario --max-pages 3 --no-db   # 3 pages, skip DB
python3 scrape_keyword.py "kawaii desk" --floor 5000 --max-pages 3   # writes to DB
```

If any of these silent-rejects, STOP. Symptoms guide:

- **sst missing** on the first call → account-3 is already rate-limited.
  This shouldn't happen unless the warming was inadequate or the clone's
  fingerprint still leaks VM-1. Go back to Phase 3, randomize harder.
- **sst ~80ms** → bad signature. Likely libmetasec_ov.so / TT Lite
  version mismatch (Phase 4 cross-check failed). Capture fresh APKs from
  VM-3 itself, re-extract libmetasec_ov.so locally.
- **HTTP 4xx with status_code=8** → cookie/token rejected. Rerun Phase 7
  capture; possibly the session capture grabbed values from a non-logged-
  in state.

**Checklist:**
- [ ] `replay_search_frida.py mario` → ACCEPTED, ~30 items, sst > 200ms
- [ ] 3-page `scrape_keyword.py` returns videos at every page
- [ ] DB write succeeds (single test term)

## Phase 9 — turn on continual_scraper

```bash
export TIKTOK_ACCOUNT=vm3
cd ~/direct_api
mkdir -p logs
LOG=logs/continual_$(date +%Y%m%d_%H%M%S).log

# More conservative pacing than VM-1's defaults — VM-1 was at 30-90s when
# it got throttled. VM-3 should start at 60-180s and only relax if 24h
# pass cleanly.
INTER_TERM_SLEEP_MIN=60 INTER_TERM_SLEEP_MAX=180 \
    nohup python3 -u continual_scraper.py > "$LOG" 2>&1 &
```

(Either patch the constants in continual_scraper.py to read env, or hard-
edit them on VM-3 only — don't push the env-coupling to the repo unless
we want it on all VMs.)

**ntfy distinguishability**: VM-1's continual_scraper currently pings
`http://150.136.40.239:2586/retardedjames-tiktok` with no VM prefix. On
VM-3, edit `ntfy()` to prepend `[vm3]` so we can tell them apart. Same for
title strings.

**Checklist:**
- [ ] continual_scraper running under nohup on VM-3
- [ ] First ntfy ping arrived with `[vm3]` prefix
- [ ] `SELECT id, term, started_at FROM terms WHERE status='in_progress'`
      shows VM-3's claimed term (and only one row per worker)

## Phase 10 — watch for the first 24h

Same watch list as VM2_PLAN §Phase 8:

- Silent-reject from VM-3 within first 2 hours = warming was insufficient
  OR fingerprint leaked. Halt, ablate.
- `videos_saved=0` rate climbing past 20% = throttle creeping in.
- Both VMs silent-rejecting at the same time = IP-class block, not
  device. Rotate VM-3's external IP via GCP.

If VM-3 runs cleanly for 24h, this clone is on its feet. Document the
final identity values in memory (a new
`memory/project_vm3_identity.md`) including:
- IP, account email, phone number tail, signup date
- LXC MAC, ANDROID_ID, ro.serialno, persona model
- sid_guard expiry

## Phase 11 — codify into `cloneN_bootstrap.sh` (next session)

Once Phases 1–9 succeed, lift the manual steps into one script:

```bash
scripts/cloneN_bootstrap.sh   # runs phases 1-4 (wipe, randomize, reinstall)
                              # stops, prints VNC instructions, exits
scripts/cloneN_capture_and_start.sh   # runs phases 7-9 after VNC signup
                              # captures session, writes per-VM file, kicks off scraper
```

The persona-pool issue (every clone is a Galaxy A54) gets fixed here too:
have the script pick from a small list and write the chosen persona into
`vm_fingerprint.txt` so we know later.

This phase is FUTURE work, not part of this run. Do it after VM-3 has run
clean for at least 24h — proven recipe before automation.

## Open questions to resolve before starting

- [ ] **Phone number for account-3**: real SIM (best) or VOIP? User
      decision; VOIP often rejected during TikTok signup.
- [ ] **Email for account-3**: fresh, untouched-by-TikTok address.
- [ ] **GCP region for VM-3**: clone is at `34.45.120.10` — what region is
      that, and is it the same as VM-1's `34.134.113.65`? If same,
      consider rebuilding in a different region for IP-reputation
      diversity (VM2_PLAN §Phase 1 covered this rationale).
- [ ] **Warming time tolerance**: 30 min minimum; willing to wait 24h?
- [ ] **Should we kill VM-1 entirely** once VM-3 is stable, or keep it
      shut down + ready as a "back online if rate-limit lifts" option?
      Cost: $15-25/mo idle. Recommendation: stop the instance (don't
      delete the disk), $0 idle if stopped, can resume later.
- [ ] **Persona variety**: accept "all VM clones are Galaxy A54s" tell for
      now, or pick a different persona for VM-3 to start the pool?
      Recommendation: VM-3 = SM-A546U1 (matches script default), VM-4 =
      pick something else when we get there.

## Rollback and "what if Phase X fails" matrix

| Phase | If it fails | Recovery |
|---|---|---|
| 0 | VM-1 isn't actually rate-limited | Investigate signer, don't clone yet |
| 1 | Clone state is unexpected (B or C) | Run `vm2_base_install.sh` first |
| 2 | TT Lite uninstall fails | `pm disable com.tiktok.lite.go` then `rm -rf` data dir directly |
| 3 | Some prop won't randomize | Edit `vm_fingerprint_randomize.sh` to add the missing key |
| 4 | TT Lite versionCode mismatch | Recapture splits from VM-1's `/data/app/...` |
| 5 | Frida hook shows stale ID | Phase 3 didn't take — re-run, restart container fully |
| 6 | Signup fails (SMS rejected) | Try a different region IP, or proxy via residential network |
| 7 | Session capture missing token | Drive UI more — TT Lite sometimes mints token only after a search |
| 8 | Smoke test silent-rejects | See triage in Phase 8 — sst value tells you which class |
| 9 | continual_scraper rejects on first term | Same as Phase 8; do not let it burn 11 terms again — silent-reject detector should catch on first |

## Appendix — quick reference cheatsheet (paste into terminal)

```bash
# === Initial SSH check ===
ssh -i ~/.ssh/jamescvermont jamescvermont@34.45.120.10 'uname -m'

# === Phase 2 wipe ===
ssh -i ~/.ssh/jamescvermont jamescvermont@34.45.120.10 <<'REMOTE'
bash ~/direct_api/scripts/vm2_start_session.sh
adb -s 127.0.0.1:5556 uninstall com.tiktok.lite.go
sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- rm -rf /data/data/com.tiktok.lite.go
waydroid session stop
REMOTE

# === Phase 3 fingerprint ===
ssh -i ~/.ssh/jamescvermont jamescvermont@34.45.120.10 <<'REMOTE'
sudo bash ~/direct_api/scripts/vm2_fingerprint_randomize.sh
bash ~/direct_api/scripts/vm2_start_display.sh
bash ~/direct_api/scripts/vm2_apply_runtime_identity.sh
REMOTE

# === Phase 3 verification (run from laptop) ===
for host in 34.134.113.65 34.45.120.10; do
    echo "=== $host ==="
    ssh -i ~/.ssh/jamescvermont jamescvermont@$host \
        'adb -s 127.0.0.1:5556 shell "getprop ro.serialno; getprop ro.product.model; settings get secure android_id"'
done

# === Phase 4 install ===
ssh -i ~/.ssh/jamescvermont jamescvermont@34.45.120.10 \
    'bash ~/direct_api/scripts/vm2_install_tt.sh'

# === Phase 5 VNC ===
ssh -L 5901:localhost:5901 -i ~/.ssh/jamescvermont jamescvermont@34.45.120.10
# Then VNC client → localhost:5901

# === Phase 8 smoke ===
ssh -i ~/.ssh/jamescvermont jamescvermont@34.45.120.10 \
    'cd ~/direct_api && export TIKTOK_ACCOUNT=vm3 && python3 replay_search_frida.py mario'

# === Phase 9 start ===
ssh -i ~/.ssh/jamescvermont jamescvermont@34.45.120.10 <<'REMOTE'
cd ~/direct_api
mkdir -p logs
export TIKTOK_ACCOUNT=vm3
LOG=logs/continual_$(date +%Y%m%d_%H%M%S).log
INTER_TERM_SLEEP_MIN=60 INTER_TERM_SLEEP_MAX=180 \
    nohup python3 -u continual_scraper.py > "$LOG" 2>&1 &
echo "started, log=$LOG"
REMOTE

# === Watch from laptop ===
ssh -i ~/.ssh/jamescvermont jamescvermont@34.45.120.10 'tail -f ~/direct_api/logs/continual_*.log'
```

## Appendix — what changes after this works once

The point of this doc is reuse. After VM-3 is up and proven, two
artifacts should ship:

1. **`scripts/cloneN_bootstrap.sh`** — one script that does Phases 1-4
   non-interactively. Stops at "now go to VNC and create the account".
2. **`scripts/cloneN_capture_and_start.sh`** — one script that does
   Phases 7-9 after the user is back from VNC.

Plus the per-VM identity file convention (`replay_search_vmN.py`,
gitignored), the env-var dispatch in `scrape_keyword.py`, and a small
addition to `continual_scraper.py` to read `INTER_TERM_SLEEP_MIN`/`MAX`
from env so we can tune per VM without forking.

These are write-this-time-once-but-pay-back-every-clone changes. Worth
landing as part of the VM-3 bring-up if there's no fire to put out.

## References

- `VM2_PLAN.md` — original VM-2 plan; this doc is its tactical
  successor for the "clone an existing VM after rate-limit" case rather
  than the "build a parallel scraper from scratch" case.
- `HANDOFF.md` — current architecture + bring-up-from-cold for VM-1.
- `HISTORY.md` — failed approaches; useful when something on VM-3 fails
  the same way VM-1 did during initial bring-up.
- `memory/project_tiktok_silent_reject.md` — variant-A vs variant-B
  detection (used in Phase 0 and Phase 8 triage).
- `memory/project_fingerprint_leaks.md` — fleet-wide and per-clone tells
  to randomize away (informs Phase 3).
- `memory/reference_vms.md` — VM inventory; will be updated to add VM-3
  once the runbook proves out.
