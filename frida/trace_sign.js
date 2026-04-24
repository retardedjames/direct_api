/*
 * trace_sign.js — log every call to ms.bd.o.k.a (the single MSSDK JNI entrypoint)
 * discovered in hook_metasec.js.
 *
 * Two ways to get there:
 *   A) native hook on libmetasec_ov+0xfb27c (direct fnPtr we got from RegisterNatives)
 *   B) Java-level hook via Java.use("ms.bd.o.k").a.overload(...)
 *
 * Approach A is cheaper (no Java reflection inside the hot path) and gives us the
 * raw JNIEnv* args exactly as C sees them. But for readability we also register
 * B — calling the logged `(int, int, long, String, Object)` overload — to print
 * the decoded Java-side String/Object args.
 *
 * Usage: attach to a running TT Lite, not spawn (we want to avoid re-discovering).
 */

const TARGET_SO = "libmetasec_ov.so";
const TARGET_OFFSET = 0xfb27c;   // ms.bd.o.k.a inside libmetasec_ov
const TARGET_CLASS = "ms.bd.o.k";

// op codes observed so far:
//   0x01000001 = string decoder (class/method name de-obfuscation) — VERY noisy,
//                hide by default unless DEBUG_NOISE env-level flag set.
const STRING_DECODE_OP = 0x01000001;
const HIDE_STRING_DECODE = true;

// Bucket: count how many times we've seen each op, and hang onto a small sample
// of inputs for ops we haven't seen before.
const opStats = {};          // op -> count
let callCount = 0;

function log(msg) { console.log("[tr] " + msg); }

function bytesToHex(arr, max) {
    max = max || 64;
    let out = [];
    for (let i = 0; i < Math.min(arr.length, max); i++) {
        out.push(("0" + (arr[i] & 0xff).toString(16)).slice(-2));
    }
    return out.join("") + (arr.length > max ? "..(" + arr.length + ")" : "");
}

function describeObj(obj) {
    // Try to decode common types — byte[], String, Map, null.
    if (obj === null || obj === undefined) return "null";
    try {
        // Primitives — when they flow through our implementation fn they arrive as JS values
        if (typeof obj === "string") return JSON.stringify(obj);
        if (typeof obj === "number") return String(obj);
        if (typeof obj === "boolean") return String(obj);
        const cls = obj.getClass ? obj.getClass().getName() : null;
        if (!cls) return "<no class> " + String(obj);
        if (cls === "java.lang.String") return JSON.stringify(obj.toString());
        if (cls === "[B") {
            // Frida auto-coerces Java byte[] into a regular JS array on the wrapper.
            // But on the raw proxy we need to use Java.array or force toString().
            // Simplest reliable path: let Java side give us hex via a String.
            try {
                const arr = Java.array("byte", obj);
                return "byte[" + arr.length + "] " + bytesToHex(arr, 64);
            } catch (e) {
                // Last resort: force hex via toString bytes
                const s = obj.toString();
                return "byte[?] toString=" + s;
            }
        }
        if (cls === "[Ljava.lang.String;") {
            // String[] — most interesting return/input type for signer dispatch
            try {
                const n = obj.length !== undefined ? obj.length : null;
                // Use Java reflection to read each element
                const JArray = Java.use("java.lang.reflect.Array");
                const len = JArray.getLength(obj);
                let out = [];
                for (let i = 0; i < len; i++) {
                    const v = JArray.get(obj, i);
                    out.push(v === null ? "null" : JSON.stringify(v.toString()));
                }
                return "String[" + len + "] [" + out.join(", ") + "]";
            } catch (e) { return "[Ljava.lang.String; <decode err: " + e + ">"; }
        }
        if (cls === "[B") {
            // (re-route; already handled above but kept in sync)
        }
        if (cls.startsWith("[")) {
            try {
                const JArray = Java.use("java.lang.reflect.Array");
                return cls + " len=" + JArray.getLength(obj);
            } catch (e) { return cls + " <array>"; }
        }
        // Fallback: toString()
        return cls + " " + obj.toString();
    } catch (e) {
        return "<decode err: " + e + ">";
    }
}

function installJavaHook() {
    Java.perform(() => {
        let K;
        try {
            K = Java.use(TARGET_CLASS);
        } catch (e) {
            log("FAIL Java.use(" + TARGET_CLASS + "): " + e);
            return;
        }
        log("class loaded: " + TARGET_CLASS);
        // List overloads to be sure we hook the right one.
        const overloads = K.a.overloads;
        log("overload count: " + overloads.length);
        for (const o of overloads) {
            log("  overload: " + o.returnType.name + " a(" +
                o.argumentTypes.map(t => t.name).join(", ") + ")");
        }
        // We want: a(int, int, long, String, Object) -> Object.
        // Frida reports argumentTypes[].name in JNI descriptor form:
        //   I, J, Ljava/lang/String;, Ljava/lang/Object;, etc.
        let target = null;
        for (const o of overloads) {
            const ts = o.argumentTypes.map(t => t.name);
            if (ts.length === 5 &&
                ts[0] === "I" && ts[1] === "I" && ts[2] === "J" &&
                ts[3] === "Ljava/lang/String;" && ts[4] === "Ljava/lang/Object;") {
                target = o;
                break;
            }
        }
        if (!target && overloads.length === 1) {
            log("only one overload, using it");
            target = overloads[0];
        }
        if (!target) {
            log("FAIL: couldn't find (int,int,long,String,Object)->Object overload");
            return;
        }
        log("hooking overload " + target.returnType.name + " a(int,int,long,String,Object)");
        target.implementation = function(op, sub, ts, key, payload) {
            callCount++;
            const n = callCount;
            opStats[op] = (opStats[op] || 0) + 1;
            // Delegate first so behavior is unchanged even if we early-return below.
            let result;
            try {
                result = target.call(this, op, sub, ts, key, payload);
            } catch (e) {
                log("!! THROW in op=0x" + op.toString(16) + ": " + e);
                throw e;
            }
            // Filter: hide the super-frequent string-decode op, but print a periodic
            // summary so we know the hook is still alive.
            if (HIDE_STRING_DECODE && op === STRING_DECODE_OP) {
                if (opStats[op] === 1 || (opStats[op] % 500) === 0) {
                    log("[stringDecode] count=" + opStats[op] +
                        " sample key=" + JSON.stringify(key ? key.toString() : null) +
                        " -> " + describeObj(result));
                }
                return result;
            }
            log("---- call #" + n + " op=0x" + op.toString(16) +
                " (seen " + opStats[op] + "x) ----");
            log("  op=" + op + " sub=" + sub + " ts=" + ts);
            log("  key=" + (key === null ? "null" : JSON.stringify(key.toString())));
            log("  payload=" + describeObj(payload));
            log("  result=" + describeObj(result));
            return result;
        };
        log("Java hook installed");
    });
}

setImmediate(() => {
    log("bootstrap (pid=" + Process.id + ")");
    installJavaHook();
    log("ready");
});
