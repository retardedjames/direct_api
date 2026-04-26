/*
 * hook_metasec.js — discover libmetasec_ov.so's JNI signing methods
 * and expose them as Frida RPC calls.
 *
 * Usage (from host):
 *   frida -U -f com.tiktok.lite.go -l hook_metasec.js --runtime=v8
 *
 * Once methods are discovered, the Python side can call:
 *   agent.exports.list_methods()
 *   agent.exports.call_sign(args_dict)
 */

const TARGET_SO = "libmetasec_ov.so";
const discovered = {};      // key: className.methodName -> { name, sig, fn, nameAddr, sigAddr }
let metasecRange = null;    // { base, end } for libmetasec_ov

function log(msg) { console.log("[mx] " + msg); }

function getMetasecRange() {
    if (metasecRange) return metasecRange;
    // Primary: native module enum
    let m = Process.findModuleByName(TARGET_SO);
    if (m) { metasecRange = { base: m.base, end: m.base.add(m.size) }; return metasecRange; }
    // Fallback: scan ranges for the file path
    const ranges = Process.enumerateRanges("r-x").filter(r =>
        r.file && r.file.path && r.file.path.includes(TARGET_SO)
    );
    if (ranges.length) {
        metasecRange = { base: ranges[0].base, end: ranges[ranges.length-1].base.add(ranges[ranges.length-1].size) };
        return metasecRange;
    }
    return null;
}

function isInMetasec(addr) {
    const r = getMetasecRange();
    if (!r) return false;
    return addr.compare(r.base) >= 0 && addr.compare(r.end) < 0;
}

function installRegisterNativesHook() {
    // Modern ART (Android 12+) templatizes JNI as art::JNI<bool>::RegisterNatives,
    // giving two instantiations: JNIILb0EE (kEnableIndexIds=false) and JNIILb1EE (true).
    // These are internal symbols, not exports — ApiResolver("exports:") misses them.
    // Use enumerateSymbols instead. Also hook CheckJNI::RegisterNatives (used when JNI
    // checks are enabled, e.g. debug builds).
    const syms = Module.enumerateSymbols("libart.so").filter(s =>
        s.name.indexOf("RegisterNatives") !== -1 && s.type === "function"
    );
    log("libart RegisterNatives symbols: " + syms.length);
    const targets = [];
    for (const s of syms) {
        log("  " + s.name + " @ " + s.address);
        // Accept both JNI<...>::RegisterNatives and CheckJNI::RegisterNatives.
        // Skip only obvious non-matches.
        if (s.name.indexOf("RegisterNatives") !== -1) targets.push(s);
    }
    if (!targets.length) {
        log("WARN: no RegisterNatives symbol found — hook not installed");
        return false;
    }
    const onEnter = function(args) {
        const clazz = args[1];
        const methods = args[2];
        const count = args[3].toInt32();

        // Check if any of the fnPtrs land inside libmetasec_ov
        let anyInMetasec = false;
        for (let i = 0; i < count; i++) {
            const m = methods.add(i * Process.pointerSize * 3);
            const fn = m.add(Process.pointerSize * 2).readPointer();
            if (isInMetasec(fn)) { anyInMetasec = true; break; }
        }
        if (!anyInMetasec) return;

        // Get the class name via Java bridge
        let clsName = "?";
        try {
            const env2 = Java.vm.tryGetEnv();
            if (env2) {
                const Class = Java.use("java.lang.Class");
                const clsObj = Java.cast(clazz, Class);
                clsName = clsObj.getName();
            }
        } catch (e) { clsName = "<err>"; }

        log("=== RegisterNatives class=" + clsName + " count=" + count + " ===");
        for (let i = 0; i < count; i++) {
            const m = methods.add(i * Process.pointerSize * 3);
            const nameAddr = m.readPointer();
            const sigAddr  = m.add(Process.pointerSize).readPointer();
            const fn       = m.add(Process.pointerSize * 2).readPointer();
            const name = nameAddr.readCString();
            const sig  = sigAddr.readCString();
            const r    = getMetasecRange();
            const offset = r ? "+0x" + fn.sub(r.base).toString(16) : "outside";
            log("  [" + i + "] " + name + sig + " -> libmetasec_ov" + offset + " (" + fn + ")");
            discovered[clsName + "." + name] = {
                className: clsName,
                methodName: name,
                signature: sig,
                fnPtr: fn,
                offset: r ? fn.sub(r.base) : null,
            };
        }
    };
    for (const t of targets) {
        try {
            Interceptor.attach(t.address, { onEnter });
            log("hooked RegisterNatives @ " + t.address + " (" + t.name + ")");
        } catch (e) {
            log("FAIL to hook " + t.name + ": " + e);
        }
    }
    return true;
}

// ----------------------------------------------------------------
// RPC: expose to Python side
// ----------------------------------------------------------------
rpc.exports = {
    list_methods() {
        return Object.entries(discovered).map(([k, v]) => ({
            key: k,
            className: v.className,
            methodName: v.methodName,
            signature: v.signature,
            fnPtr: v.fnPtr.toString(),
            offset: v.offset ? v.offset.toString() : null,
        }));
    },

    metasec_base() {
        const r = getMetasecRange();
        return r ? r.base.toString() : null;
    },

    // Call a JNI method we discovered. Args is a list of primitive JS values;
    // we reconstruct proper Java args via JNIEnv.
    // For this iteration we only handle the simple String-in/String-out shape
    // ByteDance signers typically use: sign(byte[] input) returns byte[].
    // More complex signatures we'll special-case later.
    //
    // This is a placeholder — once we know the actual signature from discovery,
    // we'll fill in the proper JNI call.
    async call_string_to_string(method_key, input_str) {
        const m = discovered[method_key];
        if (!m) throw new Error("method not discovered: " + method_key);
        return Java.performNow(() => {
            // TODO: once we see the actual sig, implement properly.
            // For now, return an error so we know this is a stub.
            return {
                error: "call_string_to_string is a stub; implement per-method after discovery",
                method: m,
            };
        });
    },
};

// ----------------------------------------------------------------
// Bootstrap
// ----------------------------------------------------------------
setImmediate(() => {
    log("bootstrap");
    // Install hook early; libart is loaded by default in every app.
    Java.perform(() => {
        log("Java.perform ready");
        const hooked = installRegisterNativesHook();
        log("RegisterNatives hook installed: " + hooked);

        // Also attach dlopen hook to know when libmetasec_ov loads.
        for (const fname of ["dlopen", "__dl_dlopen", "android_dlopen_ext", "__loader_dlopen"]) {
            const addr = Module.findGlobalExportByName(fname);
            if (!addr) continue;
            Interceptor.attach(addr, {
                onEnter(args) { this.path = args[0] ? args[0].readCString() : null; },
                onLeave() {
                    if (this.path && this.path.includes(TARGET_SO)) {
                        log("DLOPEN " + this.path);
                        getMetasecRange();
                    }
                }
            });
        }

        // If already loaded, bind now.
        getMetasecRange();

        log("ready — waiting for RegisterNatives calls");
    });
});
