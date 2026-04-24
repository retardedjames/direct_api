# CLONE_SETUP.md — bring up a new scraper VM after the previous one is rate-limited

**Purpose:** when the active scraper VM hits TikTok's per-account
silent-reject (variant B: `aweme_list=null` with `server_stream_time`
field absent), spin up a clone with a new device fingerprint + new
account and put it on the queue. This runbook is the entire process,
proven on VM-3 (`34.45.120.10`) on 2026-04-24.

**Your only synchronous task:** create the TikTok account via VNC at
**Phase 4**. Everything else is mechanical.

## Prerequisites (do once before the first clone)

- A GCP image of a working scraper VM. As of 2026-04-24, snapshot VM-1
  (`34.134.113.65`) was the source — `t2a-standard-2` ARM64 Ubuntu
  26.04, Waydroid + TT Lite already installed, frida client present.
- SSH key `~/.ssh/jamescvermont` works for any VM in the project.
- A fresh email + phone number ready for TikTok signup. Real SIM,
  not used with TikTok before. VOIP often gets rejected.

## Quick reference — host roles after clone is up

| | |
|---|---|
| New VM external IP | placeholder `$VMN_IP` throughout this doc |
| Postgres queue | `150.136.40.239` (Oracle VPS, account `app1_user`) |
| Account label | `vmN` — pick a free integer (vm3, vm4, …) |
| ntfy topic | `retardedjames-tiktok` (shared, but messages prefixed `[vmN]`) |

## The runbook

> Run on your laptop unless prefixed `# on VMN`. Long blocks below are
> meant to be pasted whole into a single SSH session.

### Phase 0 — confirm previous VM is actually rate-limited

A rate-limit looks identical to a broken signer in the logs. Verify
before cloning:

```bash
# Check the active scraper VM (substitute its IP)
ssh -i ~/.ssh/jamescvermont jamescvermont@<OLD_VM_IP> \
    'cd ~/direct_api && timeout 60 python3 replay_search_frida.py mario 2>&1 | grep verdict'
# Expected if rate-limited: SILENT-REJECT  aweme_count=0  sst=-1ms
#                          (sst=-1 = field absent in response)
# If sst is ~80ms — bad signature, fix the signer; cloning won't help.
# If ACCEPTED with real items — old VM is fine, don't clone.
```

### Phase 1 — clone the VM in GCP

In GCP Console (this is the human step that doesn't fit in a script):

1. Stop the source VM (don't delete — its disk is the template).
2. **Create machine image** from the source VM's boot disk.
3. **Create instance from image** in any region (different region from
   source = better IP-reputation diversity).
4. Boot the new VM. Note its external IP — that's `$VMN_IP`.
5. Restart the source VM if you want it on standby (it stays
   rate-limited but is reusable later).

Confirm the new VM is reachable:

```bash
VMN_IP=34.X.X.X    # set this
ssh -i ~/.ssh/jamescvermont -o StrictHostKeyChecking=no jamescvermont@$VMN_IP 'uname -m'
# Expect: aarch64
```

### Phase 2 — wipe TT Lite state + randomize fingerprint

Paste this whole block into one SSH session — it's idempotent and
order-sensitive (container must be stopped during fingerprint randomize).

```bash
ssh -i ~/.ssh/jamescvermont jamescvermont@$VMN_IP <<'REMOTE'
set -e

# 1. Stop any running session so the container is in a clean state.
waydroid session stop 2>/dev/null || true
sleep 2

# 2. Re-randomize device fingerprint (LXC MAC, ro.serialno, persona,
#    waydroid_base.prop). Stops + restarts container.
sudo bash ~/direct_api/scripts/vm2_fingerprint_randomize.sh

# 3. Bring up the display stack (Xvfb + weston + waydroid session +
#    x11vnc on 127.0.0.1:5901 + socat 127.0.0.1:5556 -> container).
bash ~/direct_api/scripts/vm2_start_display.sh

# 4. Apply runtime identity (android_id, bluetooth_address, device_name).
bash ~/direct_api/scripts/vm2_apply_runtime_identity.sh

# 5. Wipe TT Lite app data so it re-registers fresh on next launch.
#    Cloned image already has TT Lite installed; we keep the install
#    but blow away the per-account state ByteDance caches.
adb -s 127.0.0.1:5556 shell pm clear com.tiktok.lite.go

# 6. Push frida-server 16.6.6 + start it inside the LXC.
#    (TT Lite already installed from the clone image — vm2_install_tt.sh
#    detects this and skips install-multiple, but does push frida.)
bash ~/direct_api/scripts/vm2_install_tt.sh

# 7. Launch TT Lite once via adb so it's on screen for VNC connect.
adb -s 127.0.0.1:5556 shell am start \
    -n com.tiktok.lite.go/com.ss.android.ugc.aweme.main.homepage.MainActivity

# 8. Restart x11vnc — vm2_start_display.sh started it but it can die
#    when ssh exits; relaunch detached.
pkill -u $(id -u) -x x11vnc 2>/dev/null || true
nohup x11vnc -display :1 -forever -nopw -localhost -shared -rfbport 5901 \
    > ~/logs/x11vnc.log 2>&1 &
disown

echo "=== fingerprint summary ==="
echo "model:    $(adb -s 127.0.0.1:5556 shell getprop ro.product.model)"
echo "serial:   $(adb -s 127.0.0.1:5556 shell getprop ro.serialno)"
echo "android_id: $(adb -s 127.0.0.1:5556 shell settings get secure android_id)"
echo "MAC:      $(adb -s 127.0.0.1:5556 shell ip link show eth0 | grep -oE 'ether [0-9a-f:]+' | awk '{print $2}')"
echo "tt_pid:   $(adb -s 127.0.0.1:5556 shell pidof com.tiktok.lite.go)"

REMOTE
```

That should print 5 unique-per-clone identifiers and a non-empty TT Lite
PID. Sanity check: model = `SM-A546U1` (the script's hardcoded persona),
serial + MAC + android_id all freshly random.

### Phase 3 — open VNC

In a separate terminal on your laptop:

```bash
ssh -L 5901:localhost:5901 -i ~/.ssh/jamescvermont jamescvermont@$VMN_IP
# Leave this open. Then in a VNC client (TigerVNC / RealVNC / macOS
# Screen Sharing), connect to localhost:5901 — no password.
```

### Phase 4 — create + warm account in VNC ⬅ **YOUR PART**

In the VNC view:

1. **TT Lite is already on screen.** If it crashed, tap the icon.
2. **Sign up.** Use the prepared fresh email + phone. Complete email
   verification + phone verification. *If SMS verification rejects from
   the GCP IP*: stop, get help — you'll need a residential-IP proxy or a
   different region. (Hasn't happened yet on us-central1 / us-east1.)
3. **Warm the account ~10 minutes** before exiting:
   - Scroll the For You feed for a couple minutes
   - Watch 3-5 videos to completion
   - Like 2-3 things
   - Tap the in-app **search bar**, type one keyword, run the search
     (this populates the cookie store with realistic state)
   - Follow 1 account
4. **Tell the assistant you're done.** That triggers Phase 5.

### Phase 5 — capture session + write per-VM identity file

What this phase does (run by the assistant, not you):

1. Reads `/data/data/com.tiktok.lite.go/shared_prefs/ttnetCookieStore.xml`
   and parses the Java-serialized cookies for `sid_guard`, `sessionid`,
   `odin_tt`, `cmpl_token`, etc.
2. Reads `applog_stats.xml` for `install_id` + `device_id`,
   `Cdid.xml` for `cdid`, `push_multi_process_config.xml` for `openudid`,
   `token_shared_preference.xml` for `X-Tt-Token`.
3. Writes `~/direct_api/replay_search_vmN.py` on the new VM with these
   plugged into the `DEVICE` dict + `COOKIE` + `X_TT_TOKEN` constants.
4. The file is gitignored — never committed.

Cookie names that matter (in priority order — first 5 are essential):

```
sid_guard sessionid sid_tt uid_tt store-idc store-country-code
odin_tt cmpl_token uid_tt_ss sessionid_ss tt_session_tlb_tag
store-country-sign store-country-code-src tt-target-idc
```

`ttreq` and `d_ticket` are NOT in the clean cookie store — they're
server-set per-response. Omit them; `/aweme/v1/search/item/` accepts
without them. They show up after the first authenticated call and update
in place (we don't refresh them, and it has worked across thousands of
calls).

`install_id` is NOT in the cookie store — read it from
`applog_stats.xml` and prepend it manually to the cookie string.

`DEVICE.iid` = `install_id` (same value, different field).
`DEVICE.device_id` from `applog_stats.xml`.
`DEVICE.openudid` from `push_multi_process_config.xml`'s `ssids` JSON.
`DEVICE.cdid` from `Cdid.xml`.

`os_version` / `os_api`: read live from `getprop` (e.g. Galaxy A54 →
13 / 33). Don't copy VM-1's spoofed `16/36`.

`USER_AGENT`: build from the persona's actual values:

```
com.tiktok.lite.go/430553 (Linux; U; Android 13; en_US;
SM-A546U1; Build/TP1A.220624.014;tt-ok/3.12.13.51.lite-ul)
```

(replace model + Build tag from the persona's `ro.build.fingerprint`.)

### Phase 6 — smoke test

```bash
ssh -i ~/.ssh/jamescvermont jamescvermont@$VMN_IP <<'REMOTE'
cd ~/direct_api
adb -s 127.0.0.1:5556 forward tcp:27042 tcp:27042
export TIKTOK_ACCOUNT=vmN          # match the file you wrote
export ADB_DEVICE=127.0.0.1:5556   # CRITICAL — the default in
                                   # frida_signer.py is VM-1's container
                                   # IP and won't work here
timeout 120 python3 replay_search_frida.py mario 2>&1 | grep -E "verdict|sign|tiktok"
REMOTE
```

Pass = `[verdict] ACCEPTED  aweme_count=30  sst=>200ms`.

Triage if it fails:

| Symptom | Likely cause | Fix |
|---|---|---|
| `SILENT-REJECT sst=-1ms` (field absent) | Account already rate-limited, or fingerprint matches a flagged device | Re-warm longer, or redo Phase 2 with stronger randomization |
| `SILENT-REJECT sst=~80ms` | Bad signature — version mismatch or libmetasec stale | Cross-check TT Lite versionCode vs source VM (both should be 430553); if differs, recapture splits |
| `adb: device '192.168.240.112:5555' not found` | Forgot `ADB_DEVICE` env var | Set `ADB_DEVICE=127.0.0.1:5556` |
| `frida.ProcessNotFoundError` | TT Lite died | Relaunch via `adb shell am start -n com.tiktok.lite.go/com.ss.android.ugc.aweme.main.homepage.MainActivity` |
| HTTP 4xx, status_code=8 | Cookie/token rejected | Cookie capture from Phase 5 was wrong — usually `install_id` mismatch. Re-extract. |

### Phase 7 — start the continual scraper

```bash
ssh -i ~/.ssh/jamescvermont jamescvermont@$VMN_IP <<'REMOTE'
cd ~/direct_api
mkdir -p logs
LOG=logs/continual_$(date +%Y%m%d_%H%M%S).log

# Conservative pacing — VM-1 hit the per-account limit running 30-90s
# between terms. New VMs start at 60-180s; can lower after 24h clean.
sed -i 's/^INTER_TERM_SLEEP_MIN = .*/INTER_TERM_SLEEP_MIN = 60/' continual_scraper.py
sed -i 's/^INTER_TERM_SLEEP_MAX = .*/INTER_TERM_SLEEP_MAX = 180/' continual_scraper.py

export TIKTOK_ACCOUNT=vmN
export ADB_DEVICE=127.0.0.1:5556
export NTFY_PREFIX="[vmN]"           # so ntfy messages show which worker

nohup python3 -u continual_scraper.py > "$LOG" 2>&1 &
disown
sleep 5
echo "=== first lines ==="
grep -v '\[sa\]' "$LOG" | tail -10
REMOTE
```

ntfy will start emitting `[vmN] 'keyword': saved N videos (...)` on each
term completion.

### Phase 8 — watch for the first hour

If `[vmN]` ntfy messages stop within an hour: the new account got
flagged anyway. Most likely cause: warming was too short. Either retry
on a fresh VM with longer warming (24h is the safe number; 10 min is
the aggressive number we used), or look at fleet-wide tells in
`memory/project_fingerprint_leaks.md`.

If both VMs silent-reject simultaneously: IP-class block. Stop +
change-external-IP on the new VM in GCP.

If `[vmN]` runs clean overnight: it's done. Next clone uses this exact
runbook.

## Critical gotchas (learned the hard way)

### `ADB_DEVICE` env var is required

`frida_signer.py` defaults to `192.168.240.112:5555` (VM-1's container
IP). New clones get a different container IP from Waydroid's DHCP. The
display script socat-forwards `127.0.0.1:5556 -> <container_ip>:5555`,
so set `ADB_DEVICE=127.0.0.1:5556` in every shell that runs the scraper
or signer.

### `pm clear` not just uninstall

Cloned images already have TT Lite installed. We keep the install (so we
don't have to scp the 23MB of split APKs again) but `pm clear` blows
away the per-account state. Without this, ByteDance's servers see a
fingerprint mismatch (new MAC/serial/android_id but cached
device_id/install_id from the source VM) and silent-reject from request
0.

### Fingerprint must change with container stopped

`vm2_fingerprint_randomize.sh` writes to `waydroid_base.prop` and the
LXC config — both are read at container start. If you edit them with
the container running, edits get masked. The script handles this
correctly (stops → edits → restarts). Don't run it manually with the
session up.

### Cookie store is Java-serialized, not plain XML

`ttnetCookieStore.xml` looks like normal Android shared_prefs but each
`<string>` value is hex-encoded `ObjectOutputStream` output of
`SerializableHttpCookie`. The cookie name + value appear as `writeUTF`
strings (`74 00 <len> <bytes>`). Don't try to grep values out of the
hex — parse the `74 00 LL` prefixes.

We initially tried hooking `okhttp3.RealCall.execute` with Frida but TT
Lite uses ByteDance's ttnet, not okhttp. The cookie-store-XML parser is
simpler anyway and what's documented above. (`capture_session.py` +
`frida/capture_search.js` are the abandoned okhttp approach, kept for
later iteration if someone wants to make the live-capture path work.)

### Persona pool

`vm2_fingerprint_randomize.sh` hardcodes the persona to Samsung Galaxy
A54 (`SM-A546U1`). Every clone we make using it will look like a Galaxy
A54. That's per-clone unique enough at the unique-ID layer (MAC, serial,
android_id all randomized) but a fleet-wide tell that "all our scrapers
are A54s." Acceptable through ~3 clones. At N≥4, edit the script to
pick from a small pool: Pixel 6a, Galaxy S22, Moto G Power, etc. Each
needs matching `device`, `product`, and a plausible `Build/...` tag.

### `replay_search_vm{N}.py` is gitignored on purpose

VM-1's `replay_search.py` is committed because the user opted in. New
per-VM files are not — they have live cookies. The `.gitignore` rule
`replay_search_vm*.py` covers all of them.

### Don't transfer cookies between VMs

The cookies in `replay_search_vmN.py` are bound to that VM's
`install_id` server-side. Copying VM-3's `replay_search_vm3.py` to VM-4
will silent-reject. Each clone needs its own account and its own
capture.

## What's still manual (TODOs for next iteration)

After the next 1-2 clones, the things below are worth automating —
they're the parts where "paste this block" is still a step:

1. **`scripts/cloneN_bootstrap.sh`** — wraps Phase 2 into one
   non-interactive script. Outputs VNC connect instructions, exits.
2. **`scripts/cloneN_capture_session.sh`** — wraps Phase 5: parses the
   shared_prefs files, writes `replay_search_vm{N}.py` into the right
   place. The cookie-jar parser from this session is in the bash
   history; lift it into a standalone Python script.
3. **`scripts/cloneN_start.sh`** — wraps Phases 6-7: smoke test, then
   start continual_scraper with the right env vars and pacing.
4. **Persona pool** in `vm2_fingerprint_randomize.sh` (see "Persona
   pool" above).

Once those exist, the entire runbook becomes:

```bash
ssh $VMN_IP 'bash ~/direct_api/scripts/cloneN_bootstrap.sh'
# → user does VNC signup
ssh $VMN_IP 'bash ~/direct_api/scripts/cloneN_capture_session.sh vmN'
ssh $VMN_IP 'bash ~/direct_api/scripts/cloneN_start.sh vmN'
```

## Appendix — files to know

| Path | Role |
|---|---|
| `scripts/vm2_fingerprint_randomize.sh` | Phase 2 step 2 — LXC MAC + persona + `ro.serialno` + waydroid_base.prop. Despite vm2 in name, used for all clones. |
| `scripts/vm2_start_display.sh` | Phase 2 step 3 — Xvfb + weston + waydroid session + x11vnc + socat 5556 forward. |
| `scripts/vm2_apply_runtime_identity.sh` | Phase 2 step 4 — android_id + bluetooth_address + device_name via adb. |
| `scripts/vm2_install_tt.sh` | Phase 2 step 6 — frida-server push (skips TT Lite install if already present). |
| `replay_search.py` | VM-1's identity. Template for `replay_search_vmN.py`. |
| `replay_search_vm{N}.py` | Per-VM identity. Gitignored. |
| `scrape_keyword.py` | Reads `TIKTOK_ACCOUNT` env var to pick which `replay_search_*.py` to use. |
| `replay_search_frida.py` | Smoke test for one keyword. Same env-var dispatch. |
| `continual_scraper.py` | Production daemon. Reads `NTFY_PREFIX` env var for `[vmN]` tagging. |
| `frida/sign_agent.js` | Frida agent that exposes `rpc.exports.sign(url, headers)` from MSSDK. Same for all VMs. |
| `frida_signer.py` | Python client for the agent. Reads `ADB_DEVICE` + `FRIDA_HOST` env vars. |

## Appendix — references

- `HANDOFF.md` — original VM-1 architecture, MSSDK signing contract,
  bring-up-from-cold for the active scraper VM.
- `HISTORY.md` — failed approaches (x86 Waydroid + Houdini, frida 17.x,
  public MSSDK signers). Useful when something on a new VM fails the
  same way.
- `memory/project_tiktok_silent_reject.md` — the variant-A vs variant-B
  detection used in Phase 0 and Phase 6 triage.
- `memory/project_fingerprint_leaks.md` — fleet-wide and per-clone tells
  that need randomizing. Consult if a new clone gets flagged within
  hours.
- `memory/reference_vms.md` — VM inventory; update with each new clone.
