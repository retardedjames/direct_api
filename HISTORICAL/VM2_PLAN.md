# VM-2 plan — second scraper stack for parallel throughput

**Status**: planning doc, not yet executed. Author: previous session (2026-04-24).
**Audience**: a fresh Claude Code session + the user, who will execute together.

## Why this exists

VM-1 (`34.133.197.84`) hit a real per-account rate-limit on 2026-04-24 at
~19:11 UTC after scraping ~500 terms in the day. Pattern: page-0 returns
`aweme_list=null` with `extra.server_stream_time` **missing** (not just low —
field absent entirely), response latency ~0.3–0.4s, no 4xx status. Classic
silent-reject with a new variant we hadn't seen in HANDOFF. The detection
bug (only checked `isinstance(sst, int) and sst < 200`) allowed the scraper
to burn 11 terms before the user noticed ntfy had gone quiet (ntfy.sh also
429'd us — coincidental but compounding).

This plan is the response: a second fully-independent scraper stack on a new
VM + new TikTok account + new device fingerprint, both workers claiming from
the same `terms` queue via `FOR UPDATE SKIP LOCKED` (already works — no code
change needed for concurrency).

Expected gain: roughly double sustainable throughput, from ~500 terms/day to
~1000–1200/day, at the cost of a second GCP ARM VM (~$15–25/mo) and a few
hours of setup.

## Hard constraints discovered the hard way

These are the things that will silent-reject or ban account-2 if you get
them wrong. Read them twice.

### 1. Device fingerprint must be unique AND match the account

TikTok's `libmetasec_ov.so` signing reads on-device state (ANDROID_ID, build
props, MAC, server-minted `install_id`/`device_id`) into every signature.
The signature is cryptographically bound to the device state of **the TT
Lite process that signed it**. You cannot route account-2's cookies through
VM-1's signer — the backend will detect the mismatch between cookie-bound
`install_id` and signature-bound `install_id`.

**Implication**: VM-2 needs its own TT Lite process, running in its own
Android container, logged into account-2. The signer attaches to that
process, not VM-1's.

### 2. Waydroid images are clones — fingerprint must be randomized before TT Lite install

Stock Waydroid VMs have identical `Settings.Secure.android_id`, build props,
`ro.build.fingerprint`, MAC on vsoc, etc. Two clones running two accounts
looks exactly like a bot farm to TikTok's device-dedup.

**Implication**: before installing TT Lite on VM-2, randomize at minimum:
- `Settings.Secure.android_id`
- MAC address on the Waydroid network interface
- `ro.serialno` (if patchable)
- `ro.product.model` / `ro.build.fingerprint` (optional but cheap)

Verify with Frida hooks that TT Lite reads the new values, not stale ones.

### 3. sid_guard is bound to install_id

Do **not** transfer account-1's cookies to VM-2. Do **not** transfer
account-2's cookies captured from a phone to VM-2's TT Lite. The session
must be minted *inside* VM-2's TT Lite (log in via VNC), so `sid_guard` is
bound to VM-2's server-registered `install_id`.

### 4. Fresh accounts get flagged fast — warm up before scraping

Brand-new TikTok accounts that immediately make thousands of `/search/item/`
calls get flagged faster than old accounts making the same calls. Spend 1–2
days doing organic activity in VM-2's TT Lite before turning on the
scraper — watch feed videos, search a handful of terms via the UI, like a
few things, follow a couple accounts.

### 5. Account creation is the single biggest anti-abuse signal

TikTok's signup flow is where they burn the most detection budget. This is
the phase most likely to fail. Prepare for:
- SMS verification required — VOIP numbers often rejected.
- Email-only signup sometimes works but is throttled per-IP.
- If VM-2's IP is a known datacenter (GCP is), signup may require extra
  challenges.

Options in rough order of cost/reliability:
- **Cleanest**: use a real SIM/phone number you haven't used for TikTok
  before. Create the account via VNC on VM-2 (so signup is bound to VM-2's
  fingerprint from day zero). Most likely to succeed.
- **Cheap**: Google Voice or similar, accept higher failure rate.
- **Gray-market**: buy a pre-aged TikTok account. Violates ToS; account
  transfer is itself a flaggable event.
- **Workaround**: create account on a phone with fresh SIM, do a few days
  of organic activity on phone, **then** the hard part — move session to
  VM-2. But step 3 above forbids this cleanly, so this only works if we
  also make VM-2's fingerprint identical to the phone, which means we can't
  start from a fresh Waydroid. Don't go this route; too fragile.

## Architecture target

```
    VM-1 (34.133.197.84) — existing                VM-2 (NEW IP) — this plan
      TT Lite [account-1]                             TT Lite [account-2]
      Frida signer                                    Frida signer
      continual_scraper.py ───────┐        ┌────── continual_scraper.py
                                  │        │
                                  ▼        ▼
                        Postgres terms queue (150.136.40.239)
                        FOR UPDATE SKIP LOCKED — already safe
```

One queue, two independent workers, no cross-talk. If VM-1 gets flagged,
VM-2 keeps running, and vice versa.

## Execution plan — step by step

### Phase 0 — prerequisites & account creation (do BEFORE spinning VM-2)

**Why first**: accounts sometimes take days to survive the signup gauntlet.
If you can't get account-2 into a state where it's working on a phone, the
VM work is wasted.

1. **Decide the phone number strategy.** (See "Hard constraints #5" above.)
   Default recommendation: real SIM, one you haven't associated with TikTok
   before. Cost: the SIM.

2. **Pre-create the account** — but with a twist. You have two sub-options:

   **2a. Create on phone first, then migrate (lower risk for signup, higher
   risk for device-binding).**
   Less recommended. Noted here for completeness only. Skip.

   **2b. Create inside VM-2 via VNC (what we'll do — cleanest device
   binding, but signup may be harder from datacenter IP).**
   See Phase 3.

3. **Prepare the email** — use a fresh email (Protonmail or similar). Don't
   reuse anything touched by account-1.

### Phase 1 — provision VM-2

**Target spec**: same as VM-1 — GCP `t2a-standard-2` (ARM64, 2 vCPU, 8GB),
Ubuntu 26.04, in a **different region from VM-1** (VM-1 is in
`us-central1`; put VM-2 in e.g. `us-east1` or `europe-west1` to get a
different datacenter IP range and avoid GCP-specific IP-reputation
correlations).

```bash
# from local
gcloud compute instances create tiktok-signer-2 \
    --project=<project> \
    --zone=us-east1-b \
    --machine-type=t2a-standard-2 \
    --image-family=ubuntu-2604-lts-arm64 \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size=30GB \
    --boot-disk-type=pd-balanced \
    --tags=ssh-in

gcloud compute firewall-rules create allow-ssh-tiktok-signer-2 \
    --allow=tcp:22 --source-ranges=<your-ip>/32 --target-tags=ssh-in
```

Confirm the external IP. Call it **`VM2_IP`** throughout this doc.

SSH key: reuse `~/.ssh/jamescvermont` (same key works for VM-1). Add the
corresponding public key to VM-2's metadata, or use `gcloud compute ssh`.

**Checklist after creation**:
- [ ] `ssh -i ~/.ssh/jamescvermont jamescvermont@VM2_IP` works
- [ ] `uname -m` reports `aarch64`
- [ ] `cat /etc/os-release` shows Ubuntu 26.04

### Phase 2 — base install (Waydroid, Python, deps)

Bring VM-2 up to parity with VM-1's system-level setup. This closely
mirrors what's documented in HANDOFF §Architecture but is repeated here so
you don't need to cross-reference.

On VM-2:

```bash
# System packages
sudo apt-get update
sudo apt-get install -y curl wget git python3-pip python3-venv \
    lxc lxc-templates cgroup-lite \
    android-tools-adb \
    xvfb x11vnc openbox \
    build-essential pkg-config \
    libglib2.0-0 libx11-6 libxkbcommon0

# Waydroid
curl https://repo.waydro.id | sudo bash
sudo apt-get install -y waydroid

# Waydroid needs binder + ashmem kernel modules. On GCP ARM Ubuntu these
# usually require a custom kernel. VM-1 has this working — if VM-2 doesn't,
# SSH to VM-1 and copy the kernel setup. (VM-1: `lsmod | grep -E "binder|ashmem"`)

sudo waydroid init -s GAPPS -f   # LineageOS 20 GAPPS arm64
# Accept the image download. This is ~1GB.

# Start Waydroid — this will need to survive reboots later.
sudo systemctl enable --now waydroid-container
```

**Checklist**:
- [ ] `sudo waydroid status` shows `Session: STOPPED, Container: RUNNING`
- [ ] `lsmod | grep binder` returns something

### Phase 3 — device fingerprint randomization (THE KEY STEP)

**Do this BEFORE installing TT Lite.** Once TT Lite is installed and
registers with ByteDance's servers, the server-side `install_id` gets
bound to whatever fingerprint Android reports at that moment. You can't
change the fingerprint retroactively without re-registering.

Start a Waydroid session so the Android container is running:

```bash
# Bring up the session + UI (same as HANDOFF step 0)
bash /tmp/wstart.sh   # if this script doesn't exist on VM-2, copy from VM-1
sudo -u jamescvermont env XDG_RUNTIME_DIR=/run/user/1001 \
    WAYLAND_DISPLAY=wayland-1 waydroid show-full-ui > /tmp/ui.log 2>&1 &
adb kill-server && adb connect 127.0.0.1:5556
adb -s 127.0.0.1:5556 shell getprop sys.boot_completed   # wait for "1"
```

Now randomize. Run these commands via `adb shell`:

```bash
# 1. ANDROID_ID — a 16-char hex string, different from VM-1
python3 -c "import secrets; print(secrets.token_hex(8))"
# e.g. "a3f91c8e4b2d5f67"
adb -s 127.0.0.1:5556 shell "settings put secure android_id a3f91c8e4b2d5f67"

# 2. MAC address on vsoc — regenerate
adb -s 127.0.0.1:5556 shell "ip link show"  # find the interface, usually vsoc0 or eth0
# Inside the container:
adb -s 127.0.0.1:5556 shell "ip link set <iface> down && ip link set <iface> address 02:XX:XX:XX:XX:XX && ip link set <iface> up"

# 3. ro.serialno — usually read-only unless you have root + resetprop.
# Waydroid runs in LXC so you may be able to modify build.prop on the host
# before the container starts. Check /var/lib/waydroid/rootfs/system/build.prop
# and restart the session after editing.

# 4. (Optional) ro.product.model + ro.build.fingerprint — pick a plausible
# real device. VM-1 reports as "moto g power - 2025". VM-2 should be
# something else, e.g. "samsung SM-A546U" or a different Moto model.
```

**Verify with Frida that TT Lite will actually see the new values** — do
this AFTER TT Lite is installed but BEFORE first launch. The concern is
that Android caches some identifiers at boot, so randomization needs to
happen with the container fully restarted afterward.

Hook script to run after install (save as `~/direct_api/frida/verify_identity.js`
on VM-2, adapt from existing `frida/hook_metasec.js` patterns):

```javascript
// Reads what TT Lite's process sees for key identifiers
Java.perform(() => {
    const Settings = Java.use("android.provider.Settings$Secure");
    const ctx = Java.use("android.app.ActivityThread").currentApplication().getApplicationContext();
    const cr = ctx.getContentResolver();
    console.log("ANDROID_ID:", Settings.getString(cr, "android_id"));
    console.log("Build.MODEL:", Java.use("android.os.Build").MODEL.value);
    console.log("Build.FINGERPRINT:", Java.use("android.os.Build").FINGERPRINT.value);
    console.log("Build.SERIAL:", Java.use("android.os.Build").SERIAL.value);
});
```

Run the same script on VM-1 for comparison. **Every identifier must
differ.** If any match, redo randomization until they don't.

**Checklist**:
- [ ] ANDROID_ID differs from VM-1's
- [ ] MAC address differs from VM-1's
- [ ] `ro.product.model` or fingerprint differs (nice-to-have)
- [ ] Confirmed via Frida hook, not just `getprop`

### Phase 4 — install TT Lite + frida-server

Only now is it safe to let TikTok's backend register VM-2.

**TT Lite is a split-APK bundle** (10 files, ~23MB total) — not a single
`base.apk`. The full set is staged locally in this repo at
[ttapk/](ttapk/) (gitignored; copied from VM-1). You'll `scp` it from
the user's laptop to VM-2, then install all splits together in one
`install-multiple` invocation. Trying `adb install base.apk` alone fails
with `INSTALL_FAILED_MISSING_SPLIT`.

```bash
# From the user's laptop (where /home/james/direct_api/ttapk/ lives):
scp -i ~/.ssh/jamescvermont /home/james/direct_api/ttapk/*.apk \
    jamescvermont@VM2_IP:~/ttapk/

# On VM-2 — install the whole split bundle atomically:
cd ~/ttapk
adb -s 127.0.0.1:5556 install-multiple \
    base.apk \
    split_config.arm64_v8a.apk \
    split_config.en.apk \
    split_config.mdpi.apk \
    split_df_edit_effects.apk \
    split_df_edit_filter.apk \
    split_df_edit_sticker.apk \
    split_df_fusing.apk \
    split_df_record_prop.apk \
    split_post_video.apk

# Or, more robust to the directory contents:
adb -s 127.0.0.1:5556 install-multiple ~/ttapk/*.apk

# Confirm install succeeded:
adb -s 127.0.0.1:5556 shell pm path com.tiktok.lite.go
# Expect: package:/data/app/~~XXXX/com.tiktok.lite.go-YYYY/base.apk (and splits)

# frida-server 16.6.6 — DO NOT use 17.x (crashes on Android 13, per HISTORY)
wget https://github.com/frida/frida/releases/download/16.6.6/frida-server-16.6.6-android-arm64.xz
unxz frida-server-16.6.6-android-arm64.xz
adb -s 127.0.0.1:5556 push frida-server-16.6.6-android-arm64 /data/local/tmp/frida-server
adb -s 127.0.0.1:5556 shell chmod 755 /data/local/tmp/frida-server

# Launch frida-server inside the container (same incantation as VM-1)
nohup sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- \
    /data/local/tmp/frida-server -l 0.0.0.0:27042 > /tmp/frida-server.log 2>&1 &
```

**Version concern**: the APKs in `ttapk/` were captured from VM-1 on
2026-04-24. If VM-1's TT Lite has auto-updated since then, the version
on disk and the version running may differ. Cross-check the captured
APK's versionCode against VM-1's live install before trusting it:

```bash
# On VM-1:
adb -s 127.0.0.1:5556 shell dumpsys package com.tiktok.lite.go | grep versionCode
# On laptop, from the ttapk bundle:
aapt dump badging /home/james/direct_api/ttapk/base.apk | grep versionCode
```

If they diverge, re-pull from VM-1 before installing on VM-2.
**Matching versions matters** because `libmetasec_ov.so` is version-tied —
the .so at `libs/libmetasec_ov.so` was extracted from a specific TT Lite
build. Version skew between VM-1 and VM-2 means VM-2's signer may
produce signatures that TikTok rejects, even if fingerprint + session
are otherwise correct.

**First TT Lite launch matters** — this is when ByteDance's servers mint
`install_id` / `device_id` bound to the (now-randomized) device
fingerprint. Launch via VNC, not via adb am start, so the full
registration flow runs.

**Checklist**:
- [ ] `ttapk/` copied from laptop to VM-2 (~23MB)
- [ ] TT Lite installed via `install-multiple`, `pm path` confirms all splits
- [ ] Version matches VM-1's live install (versionCode cross-check)
- [ ] frida-server 16.6.6 running, `pidof frida-server` returns non-empty
- [ ] TT Lite launched once via VNC, sat on the feed for a minute
- [ ] Frida hook confirms TT Lite sees the randomized ANDROID_ID

### Phase 5 — create + warm up account-2 via VNC

Enable VNC so you can drive TT Lite from your desktop:

```bash
# On VM-2 — start Waydroid's Wayland session + an X proxy
# Easiest: use waydroid's built-in show-full-ui then tunnel via ssh -L
# Alternative: install x11vnc on top of the existing Wayland display.
# (VM-1's setup notes should be adapted here.)

# From your laptop:
ssh -L 5901:localhost:5901 -i ~/.ssh/jamescvermont jamescvermont@VM2_IP
# Then connect TigerVNC/RealVNC to localhost:5901
```

With VNC connected:

1. **Launch TT Lite**. Let the feed render. Watch 3–5 videos in full.
2. **Sign up for the account** using the prepared email + phone number.
   - If SMS verification fails from this IP, you may need to use a GCP
     region with better reputation, or proxy the signup through a
     residential IP (mobile tethering works in a pinch).
   - Complete email + phone verification.
3. **Warm the account for 1–2 days** before scraping:
   - Day 1: 15 min/day of feed watching, a few likes, one follow, two
     UI searches (e.g. "cats", "cooking").
   - Day 2: same pattern, different content.
   - Do not use the scraper API during this time.

**Why 1–2 days**: fresh accounts that immediately make API calls to
`/search/item/` without UI activity get flagged in under an hour. The
warming period is a real signal TikTok uses.

**Checklist**:
- [ ] Account-2 created, email + phone verified
- [ ] 24–48h of organic activity logged via VNC
- [ ] Account not shadowbanned — try searching for your own username on
      another phone; account should appear

### Phase 6 — deploy the scraper stack

Once account-2 is warm, clone the scraper code onto VM-2.

```bash
# On VM-2
cd ~
git clone https://retardedjames:<PAT>@github.com/retardedjames/direct_api.git
# (use the same PAT technique as VM-1 — see HANDOFF)

cd ~/direct_api
pip3 install --user --break-system-packages psycopg2-binary sqlalchemy frida

# Extract libmetasec_ov.so — same process as VM-1 (see try_metasec.py /
# HISTORY.md). The .so is gitignored so it won't be in the clone.
# Quickest: scp from VM-1.
scp -i ~/.ssh/jamescvermont jamescvermont@34.133.197.84:~/direct_api/libs/libmetasec_ov.so \
    ~/direct_api/libs/libmetasec_ov.so
# This is the ARM64 extracted SDK, identical across devices running the
# same TT Lite version — OK to share.
```

**Critical file to replace**: `replay_search.py` holds VM-1's
`DEVICE`/`COOKIE`/`X_TT_TOKEN`. VM-2 needs its own versions.

Capture VM-2's fresh session:

1. Launch TT Lite on VM-2 via VNC. Let it settle.
2. Run a mitmproxy-style capture of one `/aweme/v1/search/item/` call from
   the UI (search for something like "test"). **Inside VM-2's container**,
   not on a phone.
3. Extract cookies (`sid_guard`, `odin_tt`, `install_id`), `X-Tt-Token`,
   and the full `DEVICE` dict (device_id, iid, openudid, cdid, etc.) from
   the captured request.
4. Create `replay_search_vm2.py` (or put VM-2's constants behind an env
   var switch — cleaner). **Do not commit** to a public repo — cookies.

Alternative to mitmproxy-on-Waydroid: use Frida itself to log the request.
The `frida/sign_agent.js` already has access to the URL + headers the app
is signing. Add a one-off logger that dumps the next N search calls to
a file. Easier than setting up mitmproxy inside a Waydroid VM.

**Then patch `continual_scraper.py` / `frida_signer.py`** to read whichever
device constants are appropriate:

```python
# Simplest: an env var selects the identity module
import os
if os.environ.get("TIKTOK_ACCOUNT") == "vm2":
    from replay_search_vm2 import DEVICE, COOKIE, X_TT_TOKEN, USER_AGENT, TIKTOK_HOST, TIKTOK_PATH
else:
    from replay_search import DEVICE, COOKIE, X_TT_TOKEN, USER_AGENT, TIKTOK_HOST, TIKTOK_PATH
```

Then on VM-2: `export TIKTOK_ACCOUNT=vm2` in the scraper's environment.

**Checklist**:
- [ ] `~/direct_api` present on VM-2
- [ ] Python deps installed
- [ ] `libs/libmetasec_ov.so` present
- [ ] `replay_search_vm2.py` created with VM-2's captured constants
- [ ] Env-var switch wired into continual_scraper.py (or equivalent)
- [ ] `python3 replay_search_frida.py test` returns
      `ACCEPTED aweme_count=30 sst>200ms` — this is the smoke test that
      proves VM-2's signer + session + device all align

### Phase 7 — test, then turn on

1. **First**: run 5 terms manually from the queue, not via the continual
   scraper. Confirm each one returns real data (`videos_saved > 0`,
   `server_stream_time > 200ms`). If any silent-reject, STOP — something
   in the fingerprint/session/signer chain is misaligned.

2. **Second**: start the continual scraper in a tmux or nohup on VM-2,
   **with more conservative pacing than VM-1**:

   ```bash
   export TIKTOK_ACCOUNT=vm2
   cd ~/direct_api
   mkdir -p logs
   LOG=logs/continual_$(date +%Y%m%d_%H%M%S).log
   nohup python3 -u continual_scraper.py > "$LOG" 2>&1 &
   ```

   Consider editing the script on VM-2 only to bump
   `INTER_TERM_SLEEP_MIN=60, INTER_TERM_SLEEP_MAX=180` to stay further
   from the rate-limit. VM-1 was at 30–90s when it got throttled.

3. **Monitor from both sides**:
   - ntfy pings should now come from both VM-1 and VM-2. The existing
     topic `retardedjames-tiktok` works — optionally add a prefix to
     VM-2's ntfy messages (e.g. `[vm2]`) by editing `ntfy()` on VM-2's
     copy. **Recommended.** Otherwise you can't tell which account sent
     what.
   - Postgres `terms` table — no more than one row `in_progress` per VM
     at a time. Verify with:
     ```sql
     SELECT id, term, started_at FROM terms WHERE status='in_progress';
     ```

**Checklist**:
- [ ] 5 manual test terms all returned real data, no silent-rejects
- [ ] Continual scraper running on VM-2 under nohup
- [ ] ntfy pings distinguishable between VM-1 and VM-2
- [ ] Both workers claim distinct terms (no collisions observed in logs)

### Phase 8 — wait-and-watch

For the first 24h of both-VMs-running, **do not assume success**. Watch for:

- Silent-reject halt alerts on ntfy from either VM
- One VM completing terms while the other stalls
- `videos_saved = 0` rates creeping up (should be < ~5% of completions
  under normal operation; if it climbs past 20% for one VM, that VM is
  being throttled)

If VM-2 silent-rejects early and often, the most likely cause is
device-fingerprint leakage — i.e. some identifier wasn't actually
randomized despite our best efforts, and TikTok recognizes VM-2 as "same
device as VM-1 with a different account." Kill VM-2, go back to Phase 3,
randomize more aggressively (especially `ro.serialno` and any other
`getprop` values).

If VM-1 and VM-2 both silent-reject simultaneously, it's an IP-based
block, not device-based. Rotate VM-2's IP (GCP: stop, change external IP,
restart).

## Rollback plan

At every phase, these are the reversibility guarantees:

| Phase | Reversible? | How |
|---|---|---|
| 1 (VM provision) | yes | `gcloud compute instances delete tiktok-signer-2` |
| 2 (base install) | yes | reimage or delete-and-recreate VM |
| 3 (fingerprint) | partially | harder to fix AFTER TT Lite registers; before that, just re-randomize |
| 4 (TT Lite install) | yes but costly | uninstall TT Lite, re-randomize, reinstall → new install_id minted |
| 5 (account creation) | **no** | once account-2 exists, you can't un-create it. Flagging is permanent for that phone number + email pair. |
| 6 (scraper deploy) | trivially | delete code + env vars |
| 7 (turn on) | trivially | kill the process, reset in_progress terms to pending (same SQL we used earlier today) |

**The one irreversible step is Phase 5.** Don't rush it.

## Open questions to resolve before starting

- [ ] **Phone number**: real SIM or VOIP? Recommendation: real SIM.
- [ ] **GCP region for VM-2**: pick a different region from VM-1. Suggest
      `us-east1` or `europe-west1`. Consider IP reputation — avoid regions
      known for abuse (some low-cost Asian regions).
- [ ] **Budget**: ~$15–25/mo for VM-2. Plus one-time SIM cost.
- [ ] **Time commitment**: realistically a full day of setup + 1–2 days
      of account warming before the scraper starts.
- [ ] **Do we want per-VM ntfy topics** or a shared one with `[vm1]`/`[vm2]`
      prefixes? Shared with prefixes is lower-friction.
- [ ] **Where to put VM-2's `replay_search_vm2.py`** — since it contains
      cookies, keep it local to VM-2 only, never git push. Unlike VM-1's
      `replay_search.py` (which is in the repo; the user has opted in to
      that).

## What I'd do differently if we end up expanding further (VM-3, VM-4…)

Three things will become painful at N > 2:

1. **Per-VM identity files** — `replay_search_vm2.py`, `_vm3.py`, etc.
   cluttering the repo. At N=3 move the device constants into a
   per-VM-only config file (not in git) and have the scraper read it.

2. **ntfy topic management** — the shared topic will get noisy. At N=3,
   switch to per-VM topics and keep a single dashboard-style summary
   topic that emits once an hour.

3. **Account warming backlog** — if one account gets flagged, you need
   another pre-warmed account ready to swap in. At N=3 start a pipeline
   of "accounts in warm-up" so there's always a spare.

For N=2 none of this is worth doing yet. Handle it when the pain is real.

## Appendix — references to existing code

- `continual_scraper.py` — the daemon we'll clone onto VM-2. No changes
  needed for concurrency; `FOR UPDATE SKIP LOCKED` in `db.claim_next_term`
  already handles multi-worker claiming.
- `db.py` helpers: `claim_next_term`, `mark_term_done`, `mark_term_failed`,
  `release_term`, `reclaim_stale_terms`. Safe to run from multiple hosts.
- `replay_search.py` — VM-1's identity constants. **Template for
  `replay_search_vm2.py`** (same shape, different values).
- `frida_signer.py` + `frida/sign_agent.js` — copy verbatim to VM-2.
- `HANDOFF.md` — current-state reference for VM-1's setup; adapt, don't
  copy, since Phase 3 intentionally diverges (randomization).
- `HISTORY.md` — failed approaches. Useful if something on VM-2 fails the
  same way (e.g. frida 17 crash, Houdini-translated ARM issue).

## Appendix — commands the user will want to run from their laptop

Not VM commands — these are local, for the user's convenience.

```bash
# Watch live log on VM-2 from laptop
ssh -i ~/.ssh/jamescvermont jamescvermont@VM2_IP 'tail -f ~/direct_api/logs/continual_*.log'

# Check both VMs' scraper health in one shot
for host in 34.133.197.84 VM2_IP; do
    echo "=== $host ==="
    ssh -i ~/.ssh/jamescvermont jamescvermont@$host \
        'ps -ef | grep continual_scraper | grep -v grep; tail -5 ~/direct_api/logs/continual_*.log'
done

# Queue state query (runs locally against Postgres)
PGPASSWORD=app1dev psql -h 150.136.40.239 -U app1_user -d tiktoks -c \
    "SELECT status, COUNT(*) FROM terms GROUP BY status ORDER BY status;"

# Recent completions by VM — if ntfy prefixes are added, this is easier
# via ntfy history. Via Postgres alone you can't tell which VM did what
# unless we add a column to terms. (See "open questions".)
```
