/*
 * capture_search.js — Frida agent that hooks TT Lite's HTTP send path and
 * dumps the next /aweme/v1/search/item/ request as a JSON blob to stdout
 * via send(). Pair with capture_session.py.
 *
 * Strategy: hook all OkHttp `RealCall` overloads that exist (TT bundles its
 * own minified copies) plus the ttnet HTTP layer. When we see a Request
 * whose URL contains /aweme/v1/search/item/, snapshot URL + headers and
 * send() it back, then unhook so we only capture once.
 *
 * Why hook here, not at MSSDK: MSSDK only sees the URL and a subset of
 * headers (the ones that need signing). The Cookie + X-Tt-Token are
 * applied by the HTTP interceptor chain AFTER signing. So we hook at the
 * point where the FULL request is assembled.
 */

const TARGET_PATH = "/aweme/v1/search/item/";
let captured = false;

function log(msg) { console.log("[cap] " + msg); }

function snapshotRequest(req) {
    try {
        const url = req.url().toString();
        if (!url.includes(TARGET_PATH)) return false;
        if (captured) return true;

        const headers = req.headers();
        const names = headers.names().toArray();
        const out = { url: url, headers: {} };
        for (const n of names) {
            const k = n.toString();
            // Cookie can have multiple values; join them.
            const vals = headers.values(k).toArray().map(v => v.toString());
            out.headers[k] = vals.length === 1 ? vals[0] : vals.join("; ");
        }
        captured = true;
        log("captured " + url.slice(0, 100) + "...");
        send({ type: "search_request", payload: out });
        return true;
    } catch (e) {
        log("snapshot error: " + e);
        return false;
    }
}

function tryHookOkHttp(className) {
    try {
        const RealCall = Java.use(className);
        const overloads = RealCall.execute.overloads;
        for (const o of overloads) {
            o.implementation = function() {
                if (!captured) snapshotRequest(this.request());
                return o.apply(this, arguments);
            };
        }
        log("hooked " + className + ".execute (" + overloads.length + " overloads)");
        return true;
    } catch (e) {
        return false;
    }
}

function tryHookEnqueue(className) {
    try {
        const RealCall = Java.use(className);
        const overloads = RealCall.enqueue.overloads;
        for (const o of overloads) {
            o.implementation = function(cb) {
                if (!captured) snapshotRequest(this.request());
                return o.apply(this, arguments);
            };
        }
        log("hooked " + className + ".enqueue (" + overloads.length + " overloads)");
        return true;
    } catch (e) {
        return false;
    }
}

setImmediate(() => Java.perform(() => {
    log("bootstrap");

    // Standard okhttp3 names. TT bundles its own; the package is usually
    // intact on aid=1340 Lite builds.
    const candidates = [
        "okhttp3.RealCall",
        "okhttp3.internal.connection.RealCall",
    ];
    let any = false;
    for (const c of candidates) {
        if (tryHookOkHttp(c)) any = true;
        if (tryHookEnqueue(c)) any = true;
    }

    if (!any) {
        log("WARN no okhttp3.RealCall — searching loaded classes");
        Java.enumerateLoadedClasses({
            onMatch(name) {
                if (name.endsWith(".RealCall") || name.endsWith("$RealCall")) {
                    log("  candidate: " + name);
                }
            },
            onComplete() { log("done enumerating"); },
        });
    }

    log("ready — drive a search in the TT UI now");
}));
