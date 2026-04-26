# HISTORICAL/ — for reference only, not part of the live pipeline

Anything in this directory was load-bearing during development but is
no longer used by the production scraper. Kept so a future session
can read the rationale for "why we didn't just…" decisions instead of
re-deriving them.

If you're trying to bring up a new clone or operate the live scraper,
**you should not need anything in here**. If you do, the live runbook
(`../CLONE_SETUP.md`) probably needs an update.

## What's in here

### Documentation

| File | Why archived |
|---|---|
| `HISTORY.md` | Failed approaches (x86 Waydroid + Houdini, frida 17.x, public MSSDK signers). Useful when something on a new VM fails the same way. |
| `VM2_PLAN.md` | Original plan doc for the second scraper stack. Superseded by `../CLONE_SETUP.md`. |

### Python — research / RE phase

| File | What it did |
|---|---|
| `replay_search_vm1_template.py` | The original VM-1 identity (cookies, device dict). Kept as a structural template — `replay_search_vmN.py` files emitted by `scripts/capture_session.py` mirror its shape. Cookies long since expired. |
| `capture_oracles.py` | Burned RapidAPI quota to capture known-good (query, sig, response) tuples for offline signer comparison. Useful only when validating a candidate signer against a real oracle. |
| `diff_signers.py` | Re-signed the same query with each candidate self-signer and diffed the bytes. RE phase. |
| `dump_argus_protobufs.py` | Extracted pre-encryption Argus protobufs from candidate signers. RE phase. |
| `local_signer.py` | Open-source SignerPy fallback. Confirmed non-viable on aid=1340 (silent-rejects). |
| `test_local_signer.py` | End-to-end test for `local_signer.py`. |
| `try_metasec.py` | int4444/Metasec signer experiment. Same outcome as SignerPy — aid=1233 only. |
| `tt_capture_signed.py` | mitmproxy addon for dumping signed phone traffic. Used to pull the device fingerprint + cookie set we now persist in `replay_search_vmN.py`. |
| `capture_session_okhttp_attempt.py` | Abandoned approach: hook `okhttp3.RealCall.execute` with Frida to live-capture the next search. TT Lite uses ByteDance's ttnet, not okhttp, so the hook never fires. The shared_prefs parser in `../scripts/capture_session.py` is the working path. |

### Frida scripts — discovery / tracing

| File | What it did |
|---|---|
| `frida/discover_metasec.js` | First-pass agent that hooks `RegisterNatives` to find libmetasec_ov.so's JNI entrypoints. Used once to find `ms.bd.o.k.a`. |
| `frida/hook_metasec.js` | Discovery agent that exposed the candidate signers as RPC. |
| `frida/trace_sign.js` | Tracer that logged every call to `ms.bd.o.k.a` with arguments. Used to reverse the signing contract that's now hardcoded in `../frida/sign_agent.js`. |
| `frida/capture_search.js` | Pair script for `capture_session_okhttp_attempt.py`. Same dead end. |

### Bash — clone-bootstrap helpers no longer in the live flow

| File | Why archived |
|---|---|
| `scripts/vm2_base_install.sh` | Installs Waydroid + apt deps on a fresh Ubuntu. Only needed when bootstrapping the very first scraper VM from a blank Ubuntu image — which we did once. Future clones inherit a ready GCP machine image, so this script is unused. Keep for re-creating the source image from scratch. |
| `scripts/vm2_start_session.sh` | Earlier (pre-VNC) session bring-up. Superseded by `scripts/vm2_start_display.sh` which adds the Xvfb + weston + x11vnc stack. |

### `data/` — research artifacts (gitignored)

| Path | What it is |
|---|---|
| `data/captured_*.jsonl`, `data/captured_*.json` | mitmproxy dumps from a real Android phone running TT Lite. Source of truth for the canonical query parameter order, cookie names, and header set. Contains live cookies — never commit. |
| `data/full_capture.jsonl` | Same — single-session phone capture. |
| `data/oracles/` | RapidAPI-signed oracles paired with their input queries. Used by `diff_signers.py` and `dump_argus_protobufs.py`. |
| `data/traces/` | Live Frida sign-call traces from the ARM VM. Contain real `odin_tt`/`install_id`/`ttreq` cookies — never commit. |
| `data/replay_search_vm3.py` | Local mirror of vm3's identity, kept here after vm3 is shut down. The vm3 instance itself still has the original until the user retires it. |
