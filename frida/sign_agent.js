/*
 * sign_agent.js — Frida agent that exposes ms.bd.o.k.a as an RPC.
 *
 * Contract (discovered via trace_sign.js, 2026-04-24):
 *
 *   Object ms.bd.o.k.a(
 *       int  op,        // 0x03000001 = "sign request"
 *       int  sub,       // always 0 for signing in observed traffic
 *       long ts,        // process-monotonic ts, e.g. 256047302243392
 *                       // — same value across sign calls in one process
 *                       //   (so we cache whatever we see first)
 *       String url,     // full request URL WITH query string
 *       Object hdrsIn   // String[] of flat [k,v,k,v,...] request headers
 *   ) -> Object         // String[] of flat [k,v,k,v,...] signed headers
 *                       //   to ADD: X-Argus, X-Ladon, X-Gorgon, X-Khronos
 *                       //   (some endpoints get fewer, e.g. just Gorgon+Khronos)
 *
 * Usage from Python:
 *   session = device.attach(pid_of_tt_lite)
 *   script  = session.create_script(open("sign_agent.js").read(), runtime="v8")
 *   script.load()
 *   api = script.exports_sync
 *   api.sign("https://api19-normal-useast8.tiktokv.us/aweme/v1/search/item/?...",
 *            {"cookie": "...", "user-agent": "...", ...})
 *   -> {"X-Argus": "...", "X-Gorgon": "...", "X-Khronos": "...", "X-Ladon": "..."}
 */

const TARGET_CLASS = "ms.bd.o.k";
const SIGN_OP   = 0x03000001;
const SIGN_SUB  = 0;

// Cached live `ts` captured from an organic TT sign call. We sniff the first one
// via a passive hook on the same method; we don't MODIFY its behavior. If the app
// never calls the signer on its own (e.g. TT is pinned on a login screen before
// any network), we fall back to 0 — works in practice because the URL already
// carries wall-clock in _rticket / ts.
let cachedTs = null;
let pendingWaiters = [];
let signMethod = null;       // the bound overload once resolved
let stringClass = null;
let objectClass = null;

function log(msg) { console.log("[sa] " + msg); }

function resolveSigner() {
    return new Promise((resolve, reject) => {
        Java.perform(() => {
            try {
                const K = Java.use(TARGET_CLASS);
                const overloads = K.a.overloads;
                let target = null;
                for (const o of overloads) {
                    const ts = o.argumentTypes.map(t => t.name);
                    if (ts.length === 5 &&
                        ts[0] === "I" && ts[1] === "I" && ts[2] === "J" &&
                        ts[3] === "Ljava/lang/String;" &&
                        ts[4] === "Ljava/lang/Object;") {
                        target = o;
                        break;
                    }
                }
                if (!target && overloads.length === 1) target = overloads[0];
                if (!target) return reject("ms.bd.o.k.a overload not found");
                signMethod = target;
                stringClass = Java.use("java.lang.String");
                objectClass = Java.use("java.lang.Object");
                log("signer resolved: " + target.returnType.name +
                    " a(I,I,J,String,Object)");
                resolve();
            } catch (e) { reject("resolveSigner: " + e); }
        });
    });
}

function installTsSniffer() {
    Java.perform(() => {
        const K = Java.use(TARGET_CLASS);
        const overloads = K.a.overloads;
        let target = null;
        for (const o of overloads) {
            const ts = o.argumentTypes.map(t => t.name);
            if (ts.length === 5 && ts[0] === "I" && ts[2] === "J") { target = o; break; }
        }
        if (!target) { log("WARN: ts sniffer not installed"); return; }
        const orig = target.implementation;
        target.implementation = function(op, sub, ts, key, payload) {
            if (op === SIGN_OP && cachedTs === null) {
                cachedTs = ts;
                log("captured live ts=" + ts + " from organic sign call");
                // Release anyone who was waiting
                const w = pendingWaiters.slice();
                pendingWaiters = [];
                for (const fn of w) fn();
            }
            return target.call(this, op, sub, ts, key, payload);
        };
        log("ts sniffer installed");
    });
}

function headersDictToStringArray(headers) {
    // Build a real Java String[] via reflection so the result is an actual
    // jobject (not a Frida JS-array shim). This is required because the
    // target method's 5th parameter is java.lang.Object, and Frida's bridge
    // cannot auto-convert a JS-only array shim through an Object-typed slot.
    const flat = [];
    if (Array.isArray(headers)) {
        for (const x of headers) flat.push(String(x));
    } else if (headers && typeof headers === "object") {
        for (const k of Object.keys(headers)) {
            flat.push(String(k));
            flat.push(String(headers[k]));
        }
    }
    const Array_ = Java.use("java.lang.reflect.Array");
    const StringClass = Java.use("java.lang.String").class;
    const jarr = Array_.newInstance(StringClass, flat.length);
    for (let i = 0; i < flat.length; i++) {
        Array_.set(jarr, i, flat[i]);
    }
    return jarr;
}

function stringArrayToDict(arr) {
    // Convert a Java String[] jobject into a {k: v} dict using reflection.
    const JArray = Java.use("java.lang.reflect.Array");
    const len = JArray.getLength(arr);
    const out = {};
    for (let i = 0; i + 1 < len; i += 2) {
        const k = JArray.get(arr, i);
        const v = JArray.get(arr, i + 1);
        if (k !== null) out[k.toString()] = v === null ? null : v.toString();
    }
    return out;
}

function doSign(url, headersInJsObj, tsOverride) {
    return new Promise((resolve, reject) => {
        Java.perform(() => {
            try {
                log("doSign enter url=" + JSON.stringify(String(url).slice(0, 80)));
                const ts = (tsOverride !== null && tsOverride !== undefined)
                    ? tsOverride
                    : (cachedTs !== null ? cachedTs : 0);
                log("doSign ts=" + ts + " cachedTs=" + cachedTs);
                const hdrArr = headersDictToStringArray(headersInJsObj || {});
                log("doSign hdr built (real jobject String[])");
                const K = Java.use(TARGET_CLASS);
                log("doSign calling K.a ...");
                const res = K.a(SIGN_OP, SIGN_SUB, ts, url, hdrArr);
                log("doSign K.a returned");
                if (res === null || res === undefined) return resolve({});
                const cls = res.getClass ? res.getClass().getName() : "?";
                log("doSign result class=" + cls);
                if (cls !== "[Ljava.lang.String;") {
                    return reject("unexpected return type: " + cls +
                        " (value=" + String(res).slice(0, 200) + ")");
                }
                resolve(stringArrayToDict(res));
            } catch (e) {
                const msg = "doSign: " + (e && e.message ? e.message : String(e)) +
                    (e && e.stack ? ("\n" + e.stack) : "");
                log("EXC " + msg);
                reject(msg);
            }
        });
    });
}

// ---------------------------------------------------------
// RPC
// ---------------------------------------------------------
rpc.exports = {
    // Note: Frida's Python bindings camelCase method names on access, so JS
    // keys must be camelCase to match `exports_sync.cached_ts() ->
    // rpc 'cachedTs'` resolution. (Single-word names need no translation.)
    async ready() {
        return {
            resolved: !!signMethod,
            cachedTs: cachedTs,
        };
    },
    async cachedTs() { return cachedTs; },
    async sign(url, headers, tsOverride) {
        return await doSign(url, headers, tsOverride);
    },
};

setImmediate(() => {
    log("bootstrap (pid=" + Process.id + ")");
    resolveSigner()
        .then(() => {
            installTsSniffer();
            log("ready");
        })
        .catch(err => log("FATAL: " + err));
});
