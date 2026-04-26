# direct_api — historical reference

This file collects reasoning and diagnostics that were load-bearing while
we were building the pipeline but aren't needed for day-to-day work. Kept
here so a future session doesn't re-derive them. For the current state
and operational steps, see `HANDOFF.md`.

## Why B (x86 Waydroid + Houdini) failed

Frida runs as a native **x86_64** process. TikTok's `libmetasec_ov.so` is
**ARM64** code translated on-the-fly by Houdini
(`/system/lib64/libhoudini.so`). These live in disjoint memory spaces:

- ARM64 code: `0x4000_00000000 — 0x4000_xxxx_xxxx`
- x86_64 code (incl. Frida trampolines): `0x7c7a_xxxx_xxxx`

Confirmed experimentally on `34.171.201.223`:

- `Process.enumerateModules()` does NOT return `libmetasec_ov.so`.
- `Process.findRangeByAddress(ARM_ADDR)` DOES return the range.
- `ptr(ARM_ADDR).readByteArray()` DOES read memory correctly (valid
  AArch64 `stp x29, x30, [sp, #-0x1b0]!` at `JNI_OnLoad` offset
  `0x38560`).
- `Interceptor.attach(ARM_ADDR, ...)` **fails**:
  `Error: unable to intercept function at 0x400021238560; please file a bug`.

Frida's Interceptor patches x86_64 instructions at the target address.
Houdini's ARM-to-x86 translator maintains its own instruction stream and
doesn't route around patches in the source ARM pages — hooks never fire.

Static reads from x86 still work — useful if a future session wants to
scan the lib for the 32-byte AES sign_key (one of the few high-entropy
blobs in `.rodata`) or map RegisterNatives call sites — but dynamic
tracing requires a native-ARM64 Frida.

## Why `frida-server 17.9.1` failed on the ARM VM

Repro (pre-downgrade, 2026-04-24):

```bash
ssh -i ~/.ssh/jamescvermont jamescvermont@34.133.197.84
sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- /data/local/tmp/frida-server -l 0.0.0.0:27042
# Immediate output:
# Bail out! Frida:ERROR:../subprojects/frida-core/src/linux/linux-host-session.vala:704:
#   frida_android_helper_service_do_start: assertion failed: (res == OK)
```

Already ruled out:

- **ptrace_scope** set to 0 on host (synced to container /proc). Didn't help.
- **Running as root** via `lxc-attach` (uid=0). Didn't help.
- **SELinux** was Disabled. Not the cause.

Failure is in `frida_android_helper_service_do_start`, the service that
installs a dex file into `/data/local/tmp/frida-helper-*.dex` and
executes it via `app_process`. On this Android 13 ARM build something in
that bootstrap fails. Downgrading to 16.6.6 resolved it. **Don't retry
17.x** on this Waydroid image.

## Public MSSDK signers — confirmed non-viable

All silent-reject against aid=1340 (HTTP 200 + `aweme_list=null` + ~80ms
`server_stream_time`):

- SignerPy 0.12.0
- int4444/Metasec
- iqbalmh18/tiktok-signer

All three target aid=1233 (full TikTok) protobuf schemas. The phone's
MSSDK v05.01.05 on aid=1340 emits a leaner Argus (120 decoded bytes)
than public signers (242–306 bytes). TikTok accepts 120 and also
accepts RapidAPI's 274-byte Argus — size isn't the discriminator, the
protobuf field set is.

Reference artefacts preserved:
- `local_signer.py` — SignerPy wrapper (kept as structural reference)
- `try_metasec.py` — Metasec harness
- `test_local_signer.py` — end-to-end sign → hit → check
- `diff_signers.py` — side-by-side Argus/Ladon/Gorgon/Khronos diff
- `dump_argus_protobufs.py` — per-signer pre-encryption protobuf dump
- `oracles/*.json` — known-good RapidAPI (query, sig, response) tuples

## Frida 16.6.6 gotchas hit during sign_agent.js authoring

Captured here so we don't re-learn them. All applied to the eventually-
working `frida/sign_agent.js`:

1. `Java.use("ms.bd.o.k").a(...)` with `Java.array(...)` as the 5th
   (Object-typed) argument fails with `argument types do not match` /
   `expected a pointer`. Frida's JS-only array shim isn't a real
   jobject. **Fix**: build the array via
   `Array.newInstance(String.class, n) + Array.set(...)` so it is a real
   jobject.
2. `Class.forName("ms.bd.o.k")` throws `ClassNotFoundException` because
   the system classloader doesn't see TT's app classloader. Use
   `Java.use(...).class` instead.
3. RPC export names need to be camelCase in JS. Frida's Python binding
   translates snake_case attribute access → camelCase wire name, so
   `script.exports_sync.cached_ts()` in Python matches
   `rpc.exports.cachedTs = ...` in JS.
4. Hooking `art::JNI<kEnableIndexIds>::RegisterNatives` requires the
   templated symbol (kEnableIndexIds specifically); the non-templated
   variant isn't invoked for runtime registrations on this Android 13
   build.

## Fallbacks we never needed, kept for the file

**Static RE of `libmetasec_ov.so`** (Ghidra/radare2, start at
`JNI_OnLoad @ 0x38560`). 20–40h of obfuscated-binary work. Not
attempted — Plan D superseded it.

**RapidAPI Pro tier**. $30/mo, 500K calls/month. Our target is ~35K
sign calls/month so Pro would fit. Remains the cleanest fallback if
the Frida stack dies and can't be restored.

**Multi-account mitmproxy scraper** (the JC1 model, host
`34.30.234.222`). Rotate 20+ TikTok accounts, scrape a few keywords
each per day. Working but painful — the whole reason this repo exists
is to retire that pattern.

## Stale paths

Earlier docs referenced `/home/james/tiktok-scraper/direct_api`. The
repo was split out on 2026-04-24 and now lives standalone at
`/home/james/direct_api` with remote
`github.com/retardedjames/direct_api`. Any command referencing the old
nested path needs the prefix stripped.
