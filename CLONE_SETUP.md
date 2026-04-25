# CLONE_SETUP.md — bring up a new scraper VM

The whole process is two scripts on the new VM, with a VNC TikTok
signup in between. Anything more involved than what's here means the
runbook needs an update — don't paste workarounds.

## What the new VM inherits from the source image

The current GCP machine image (cut from a working scraper) ships with
Waydroid + TT Lite installed, the `direct_api` repo cloned, frida-server
binary present, and the source VM's TikTok account state baked in. That
last part is the catch: **the image has a userkey + per-package SSAID**
that ByteDance hashes into `openudid`, which is then deduped server-side.
If we just boot a clone and start scraping, every clone looks like the
same physical device with a different account.

`scripts/clone_bootstrap.sh` handles all of this — it randomizes the
fingerprint, wipes the SSAID DB so Android regenerates a fresh userkey
on next boot, force-clears TT Lite, and re-launches it for fresh
ByteDance registration. You don't need to know the details to run it.

## Prerequisites

- A fresh email + phone number not previously used with TikTok. Real SIM
  preferred — VOIP often gets rejected by SMS verification.
- The current GCP machine image (cut from a working scraper VM after
  this runbook was last validated).
- `~/.ssh/jamescvermont` SSH key.

## Phase 1 — clone the VM in GCP

In GCP Console:

1. Stop the source VM if it's still running.
2. **Create instance from machine image** — pick a different region from
   the source for IP-reputation diversity.
3. Boot the new VM, copy its external IP. That's `$NEW_IP` below.

Confirm the new VM is reachable:

```bash
NEW_IP=...
ssh -i ~/.ssh/jamescvermont -o StrictHostKeyChecking=no jamescvermont@$NEW_IP 'uname -m'
# expect: aarch64
```

## Phase 2 — bootstrap

```bash
ssh -i ~/.ssh/jamescvermont jamescvermont@$NEW_IP \
    'bash ~/direct_api/scripts/clone_bootstrap.sh'
```

Takes ~2 minutes. Prints a summary at the end with the clone's
`openudid`, `device_id`, `install_id`. **Sanity check before continuing:**
`openudid` must NOT match the source image's value. (Each fresh clone
gets a different one if the SSAID reset took effect; if it matches the
image, something silently went wrong — re-run the script.)

If the script errors, see "If something goes wrong" below. **Do not
proceed to VNC if bootstrap didn't finish cleanly** — TT Lite will
register with the wrong fingerprint and the account is wasted.

## Phase 3 — VNC TikTok signup ⬅ your part

On your laptop:

```bash
ssh -L 5901:localhost:5901 -i ~/.ssh/jamescvermont jamescvermont@$NEW_IP
```

Leave that SSH session open. In a VNC client (TigerVNC / RealVNC /
macOS Screen Sharing), connect to `localhost:5901`. No password.

TT Lite is already on screen. Then:

1. **Sign up** with the prepared fresh email + phone. Complete email +
   phone verification. If SMS verification rejects from the GCP IP
   you'll need a residential-IP proxy — hasn't happened on us-central1
   / us-east1.
2. **Warm the account ~10 minutes:**
   - Scroll the For You feed for a couple minutes.
   - Watch 3-5 videos to completion.
   - Like 2-3 things.
   - Tap the in-app search bar, type one keyword, run the search (this
     populates the cookie store with realistic state).
   - Follow 1 account.

Longer warming is safer — 24h is the conservative number, 10 min has
worked but gets flagged sometimes.

## Phase 4 — finalize

Pick any `vmN` label that's locally unique on this VM (used for the
identity file name + the `TIKTOK_ACCOUNT` env var). The label is purely
local — it doesn't need to match anything on other VMs and there's
nothing to coordinate.

```bash
ssh -i ~/.ssh/jamescvermont jamescvermont@$NEW_IP \
    'bash ~/direct_api/scripts/clone_finalize.sh vmN'
```

This:

1. Parses the cookie store and identity files into
   `~/direct_api/replay_search_vmN.py` (gitignored).
2. Smoke tests: searches `mario`, asserts ACCEPTED with 30 results.
3. Starts `continual_scraper` in the background with conservative
   60-180s pacing and `NTFY_PREFIX=[vmN]`.

If the smoke test fails, the script aborts before starting the scraper.
Most common cause: VNC signup wasn't completed (cookie store has no
`sid_guard`). Finish signup and re-run `clone_finalize.sh`.

## Phase 5 — watch the first hour

`ntfy` will emit `[vmN] 'keyword': saved N videos (...)` per term.

- **Notifications keep flowing** → done. Lower pacing after 24h clean if
  you want more throughput.
- **Notifications stop within an hour** → account got flagged. Most
  common cause: warming was too short. Spin up a fresh clone with a new
  email + phone + longer warming.
- **Smoke-test passes but no notifications start** → check the log under
  `~/direct_api/logs/continual_*.log` on the VM.

## If something goes wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| `clone_bootstrap.sh` prints `openudid` matching the source image | SSAID reset didn't take effect | Re-run `sudo bash scripts/vm2_reset_ssaid.sh && bash scripts/clone_bootstrap.sh` |
| Smoke test: `SILENT-REJECT sst=-1ms` | New account already flagged, or fingerprint matches a flagged device | Burn the clone, longer warming on the next |
| Smoke test: `SILENT-REJECT sst=~80ms` | Bad signature — version mismatch | TT Lite versionCode must be 430553 (the value the signer agent expects). If it differs, the image is stale and the APK splits in `~/ttapk/` need re-pulling from a real phone capture. |
| Smoke test: `frida.ProcessNotFoundError` | TT Lite died after launch | `adb -s 127.0.0.1:5556 shell am start -n com.tiktok.lite.go/com.ss.android.ugc.aweme.main.homepage.MainActivity` then re-run finalize |
| Smoke test: `adb: device '...' not found` | Display stack didn't fully come up | `bash scripts/vm2_start_display.sh` then re-run finalize |
| Capture aborts: missing session cookies | VNC signup not complete | Finish signup in VNC, re-run `clone_finalize.sh vmN` |
| Every VNC tap triggers "System UI isn't responding" | InputDispatcher wedged | `clone_bootstrap.sh` already handles this; if it recurs, repeat: get pid via `adb shell pidof com.android.systemui`, kill via `sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- kill -9 <pid>` |

## Maintenance — when to refresh the source image

The image needs to be re-cut when:

- The TT Lite APK in `~/ttapk/` goes stale (TT Lite app updates every
  ~2 weeks; if `versionCode` on the live device drifts from what the
  signer agent expects, smoke tests will silent-reject with sst≈80ms).
- A clone bring-up reveals a new fleet-wide tell that needs to be
  randomized at clone time (add a step to `clone_bootstrap.sh`, then
  re-cut the image so future clones inherit the fix).

To refresh: bring up a clone with this runbook, scrape successfully for
24h+ to confirm health, stop the VM, create a new machine image from
its boot disk. The next person who clones uses this same runbook
unchanged.

## Files involved

| Path | Role |
|---|---|
| `scripts/clone_bootstrap.sh` | One-shot Phase 2: randomize → reset SSAID → start session → push frida → launch TT Lite. |
| `scripts/clone_finalize.sh` | One-shot Phase 4: capture session → smoke test → start scraper. |
| `scripts/capture_session.py` | Parses TT Lite's shared_prefs (cookies, install_id, openudid, X-Tt-Token, etc.) and emits `replay_search_vmN.py`. |
| `scripts/vm2_fingerprint_randomize.sh` | Persistent fingerprint: LXC MAC, persona, `ro.serialno`, build.prop. Container must be stopped. |
| `scripts/vm2_reset_ssaid.sh` | Wipes `/data/system/users/0/settings_ssaid.xml` so Android regenerates with a fresh userkey. **Load-bearing** — without it `openudid` is shared across all clones. |
| `scripts/vm2_start_display.sh` | Xvfb + weston + waydroid session + x11vnc + adb forward. |
| `scripts/vm2_apply_runtime_identity.sh` | Sets `bluetooth_address` + `device_name`. The global `android_id` it also sets is a no-op for fingerprinting (apps read the per-package SSAID, not the global value). |
| `scripts/vm2_install_tt.sh` | Pushes frida-server 16.6.6. Skips TT Lite install if already present. |
| `replay_search.py` | Original VM-1 identity, kept as a structural template. |
| `replay_search_vm{N}.py` | Per-VM identity. Gitignored. Generated by `capture_session.py`. |
| `scrape_keyword.py`, `replay_search_frida.py` | Read `TIKTOK_ACCOUNT` env var, dynamically import `replay_search_${TIKTOK_ACCOUNT}` if it matches `vm\d+`. No code change needed for new VMs. |
| `continual_scraper.py` | Production daemon. Reads `NTFY_PREFIX` env var. |
| `frida/sign_agent.js` | Frida agent loaded into TT Lite, exposes `rpc.exports.sign(url, headers)` from MSSDK. Same for all VMs. |
| `frida_signer.py` | Python client for the agent. Reads `ADB_DEVICE` + `FRIDA_HOST` env vars. |

## Why this works (background)

If the runbook is doing something that doesn't seem necessary, this
section explains why. Skip it on a successful clone bring-up.

**Why `vm2_reset_ssaid.sh` is load-bearing.** On Android 8+
`Settings.Secure.ANDROID_ID` is per-package: each app gets
`HMAC(userkey, package_signing_cert)`, where `userkey` is a 256-bit
random value generated once at first Android boot and stored in
`/data/system/users/0/settings_ssaid.xml`. That file lives on the
system partition; `pm clear` and `pm uninstall` don't touch it.
A GCP image clone carries the userkey AND every per-app SSAID forward
unchanged. ByteDance reads `Settings.Secure.getString("android_id")`
and hashes it as `openudid`, so without the reset every clone reports
the same openudid and ByteDance's registration server collapses them
to the same `device_id` + `install_id`. Deleting the file forces a
fresh userkey on the next boot.

**Why `vm2_apply_runtime_identity.sh`'s android_id write is a no-op.**
`settings put secure android_id <hex>` writes to the global
secure-settings table — historically Android 7 and earlier had a single
shared android_id and that's what apps read. Android 8+ reads the
per-package SSAID instead. The global value still exists for
back-compat but nothing modern queries it. The script is kept for the
`bluetooth_address` and `device_name` writes, both of which are real
fingerprint inputs.

**Why `pm clear` is in `clone_bootstrap.sh` even though the SSAID reset
already wipes app data.** `vm2_reset_ssaid.sh` wipes
`/data/data/com.tiktok.lite.go` from the host side while the container
is stopped, which is correct and complete. `pm clear` afterwards is a
belt-and-suspenders no-op that keeps the script resilient to changes
in the order or implementation of `vm2_reset_ssaid.sh`.

**Why frida-server 16.6.6 specifically.** 17.x crashes at startup on
Android 13 inside Waydroid (`linux-host-session.vala:704`). Don't
upgrade without testing.

**Why TT Lite versionCode 430553 specifically.** That's the version the
MSSDK sign agent (`frida/sign_agent.js`) was reverse-engineered against
— it hooks the JNI symbol `ms.bd.o.k.a` whose signature is stable
within a version range but can move. If TT Lite gets updated and
versionCode drifts, signatures will start silent-rejecting and the APK
splits in `~/ttapk/` need re-pulling from a real phone capture.

**Why `replay_search_vmN.py` is gitignored.** The file contains live
session cookies (`sid_guard`, `sessionid`, X-Tt-Token) bound server-side
to that VM's `install_id`. Cookies expire and can be revoked; pushing
them to GitHub leaks credentials. The committed `replay_search.py` was
opted-in by the original maintainer and is structural-only.

**Why ByteDance still returns the same device_id across clones even
after the SSAID fix.** Open question. After fixing openudid, vm3/vm4/vm5
still received identical `device_id` + `install_id` from
`/service/2/device_register/`, despite differing MAC, serial,
clientudid, openudid, product_uuid, and external IP. Some other
fingerprint input is being deduped server-side. The active scrapers
have run fine despite this — per-account rate-limit hits first — so
the leak is documented but not yet fixed. If a future clone gets
flagged unexpectedly fast (within hours of bring-up), this is the
prime suspect; would need mitmproxy on the registration call to
identify the matching key.

## See also

- `HANDOFF.md` — overall architecture, signing contract, day-to-day ops.
- `HISTORICAL/README.md` — index of archived files (failed approaches,
  RE artifacts, research data) for "why don't we just…?" answers.
