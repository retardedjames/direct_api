/*
 * discover_metasec.js — first-pass Frida script for TikTok Lite libmetasec_ov.so.
 *
 * Goals:
 *  1. Confirm libmetasec_ov.so loads and capture its base address.
 *  2. Hook RegisterNatives from inside JNIEnv so we see what JNI methods
 *     the lib registers and their native function pointers.
 *  3. Start a generic tracer on loaded .so exports matching /sign|Sign|argus|gorgon|ladon/i.
 *
 * Usage (from host):
 *   frida -U -n "TikTok Lite" -l discover_metasec.js --no-pause
 */

'use strict';

const TARGET_SO = 'libmetasec_ov.so';
const verbose = true;

function logln(msg) { console.log('[mx] ' + msg); }

function hookDlopen() {
    const dlopens = [
        { name: 'dlopen', signature: ['pointer', 'int'] },
        { name: 'android_dlopen_ext', signature: ['pointer', 'int', 'pointer'] },
    ];

    for (const { name } of dlopens) {
        const addr = Module.findGlobalExportByName(name);
        if (!addr) {
            logln('skip ' + name + ' (not found)');
            continue;
        }
        Interceptor.attach(addr, {
            onEnter(args) {
                this.path = args[0].readCString();
            },
            onLeave(retval) {
                if (!this.path) return;
                if (this.path.includes(TARGET_SO) ||
                    /metasec|argus|ladon|gorgon/i.test(this.path)) {
                    logln('DLOPEN ' + name + ' -> ' + this.path + ' = ' + retval);
                    tryRegisterModule(TARGET_SO);
                }
            },
        });
    }
    logln('dlopen hooks installed');
}

let registered = false;
function tryRegisterModule(soName) {
    if (registered) return;
    // enumerate modules and find target
    const mods = Process.enumerateModules();
    for (const m of mods) {
        if (m.name === soName || m.path.endsWith(soName)) {
            registered = true;
            logln('FOUND module ' + m.name + ' @ ' + m.base + ' size=0x' + m.size.toString(16) + ' path=' + m.path);
            // Dump its exports if any
            const exports = m.enumerateExports();
            logln('  exports: ' + exports.length);
            for (const exp of exports) {
                logln('    ' + exp.type + ' ' + exp.name + ' @ ' + exp.address);
            }
            hookJniOnLoad(m);
            return;
        }
    }
}

function hookJniOnLoad(mod) {
    // Find JNI_OnLoad in this module. libmetasec_ov's only export is JNI_OnLoad.
    const jniOnLoad = mod.findExportByName('JNI_OnLoad');
    if (!jniOnLoad) { logln('no JNI_OnLoad in ' + mod.name); return; }
    logln('hooking JNI_OnLoad @ ' + jniOnLoad);

    Interceptor.attach(jniOnLoad, {
        onEnter(args) {
            logln('JNI_OnLoad CALLED vm=' + args[0]);
            // RegisterNatives is function pointer at JNIEnv[215] in ART.
            // We install a generic RegisterNatives hook by wrapping the
            // function table entry. Simpler: hook after the lib has resolved
            // its RegisterNatives pointer — we do it from JavaCore directly.
        },
        onLeave(retval) {
            logln('JNI_OnLoad returned ' + retval);
        },
    });
}

function hookRegisterNatives() {
    // Hook libart.so's RegisterNatives so we see EVERY JNI method registered
    // by ANY .so. Each RegisterNatives call gives us a (class, JNINativeMethod[], count)
    // and each JNINativeMethod is {name, signature, fnPtr}.
    const RegisterNatives = Module.findGlobalExportByName('_ZN3art3JNI15RegisterNativesEP7_JNIEnvP7_jclassPK15JNINativeMethodi');
    if (!RegisterNatives) {
        logln('RegisterNatives symbol not found on libart — trying fallback');
        // Fallback: enumerate libart exports for regex.
        const art = Process.findModuleByName('libart.so');
        if (art) {
            const fns = art.enumerateExports().filter(e => e.name.includes('RegisterNatives'));
            logln('libart exports matching RegisterNatives:');
            for (const f of fns) logln('  ' + f.name + ' @ ' + f.address);
        }
        return;
    }
    logln('hooking RegisterNatives @ ' + RegisterNatives);
    Interceptor.attach(RegisterNatives, {
        onEnter(args) {
            const env = args[0];
            const clazz = args[1];
            const methods = args[2];
            const count = args[3].toInt32();
            // Get class name: JNIEnv->GetObjectClass already gave us jclass.
            // Use JNIEnv->CallObjectMethod to get name? Easier: FindClass internal.
            const className = getJClassName(env, clazz);
            logln('RegisterNatives class=' + className + ' count=' + count);
            for (let i = 0; i < count; i++) {
                const methodPtr = methods.add(i * Process.pointerSize * 3);
                const namePtr = methodPtr.readPointer();
                const sigPtr = methodPtr.add(Process.pointerSize).readPointer();
                const fnPtr = methodPtr.add(Process.pointerSize * 2).readPointer();
                const name = namePtr.readCString();
                const sig = sigPtr.readCString();
                const mod = Process.findModuleByAddress(fnPtr);
                const modInfo = mod ? (mod.name + '+0x' + fnPtr.sub(mod.base).toString(16)) : '?';
                logln('  [' + i + '] ' + name + sig + ' -> ' + fnPtr + ' (' + modInfo + ')');
            }
        },
    });
}

function getJClassName(envPtr, jclass) {
    try {
        const env = Java.vm.getEnv();
        const cls = Java.cast(Java.vm.getEnv().handle, Java.use('java.lang.Class'));
        // Simpler: call env.findClass or GetObjectClass machinery.
        // We'll just attempt via Java.use after we have the name.
        // If that fails, return '?'.
        return Java.vm.tryGetEnv()
            .newStringUtf('x'); // placeholder
    } catch (e) { return '?'; }
}

// Actually simpler: read the JNIEnv vtable offset for GetObjectClass and print name via JNI.
// We skip that wrapping and rely on Java.perform's ability to wrap jclass handles.

function bootstrap() {
    logln('Frida script bootstrap');
    // Start hooking early so we catch libmetasec_ov's JNI_OnLoad even if not loaded yet.
    hookDlopen();
    Java.perform(() => {
        logln('Java.perform ready; hooking RegisterNatives');
        hookRegisterNatives();
        // If the lib is already loaded, log it now.
        tryRegisterModule(TARGET_SO);
    });
}

setImmediate(bootstrap);
