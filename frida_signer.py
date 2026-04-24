"""
frida_signer.py — attach to a live TikTok Lite process on a Waydroid ARM64
Android box and ask it to sign HTTP requests via libmetasec_ov.so.

Reachable only via the USB/adb bridge at 127.0.0.1:5556 on the ARM VM
(34.133.197.84). Launch pattern:

    from frida_signer import FridaSigner

    sig = FridaSigner()      # attaches on first sign_request(); reuses thereafter
    headers = sig.sign_request(
        "https://api19-normal-useast8.tiktokv.us/aweme/v1/search/item/?...",
        base_headers,        # dict of {name: value}
    )
    # headers is {'X-Argus': '...', 'X-Gorgon': '...', 'X-Khronos': '...', 'X-Ladon': '...'}

The ARM VM must be reachable and TT Lite must be running. If not, call
FridaSigner.bootstrap() (see Step 0 of HANDOFF.md) — or run this module's
main, which launches TT Lite via adb before attaching.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time
from typing import Optional

import frida


AGENT_PATH = pathlib.Path(__file__).parent / "frida" / "sign_agent.js"
TT_PACKAGE = "com.tiktok.lite.go"
ADB_DEVICE = os.environ.get("ADB_DEVICE", "127.0.0.1:5556")


class FridaSigner:
    def __init__(self, agent_path: pathlib.Path = AGENT_PATH):
        self._agent_src = agent_path.read_text()
        self._device = None
        self._session = None
        self._script = None

    def _pid_of_tt(self) -> Optional[int]:
        out = subprocess.run(
            ["adb", "-s", ADB_DEVICE, "shell", "pidof", TT_PACKAGE],
            capture_output=True, text=True, timeout=10,
        )
        pid_str = out.stdout.strip().split()[0] if out.stdout.strip() else ""
        return int(pid_str) if pid_str.isdigit() else None

    def _launch_tt(self) -> int:
        subprocess.run(
            ["adb", "-s", ADB_DEVICE, "shell", "am", "start", "-n",
             f"{TT_PACKAGE}/com.ss.android.ugc.aweme.main.homepage.MainActivity"],
            check=True, timeout=15,
        )
        for _ in range(30):
            pid = self._pid_of_tt()
            if pid:
                return pid
            time.sleep(0.5)
        raise RuntimeError("TikTok Lite did not start within 15s")

    def connect(self) -> None:
        if self._script is not None:
            return
        self._device = frida.get_usb_device(timeout=10)
        pid = self._pid_of_tt() or self._launch_tt()
        self._session = self._device.attach(pid)
        self._script = self._session.create_script(self._agent_src, runtime="v8")

        def on_message(m, data):
            if m["type"] == "error":
                print(f"[sign_agent:error] {m.get('description')}")

        self._script.on("message", on_message)
        self._script.load()

    def sign_request(
        self,
        url: str,
        headers: dict[str, str],
        ts_override: Optional[int] = None,
    ) -> dict[str, str]:
        """Return {'X-Argus': ..., 'X-Gorgon': ..., 'X-Khronos': ..., 'X-Ladon': ...}.

        Some endpoints only get a subset (e.g. Gorgon+Khronos for monitor/log
        collectors). Search/item returns all four.
        """
        self.connect()
        return self._script.exports_sync.sign(url, headers, ts_override)

    def cached_ts(self) -> Optional[int]:
        self.connect()
        # JS export is cachedTs (camelCase); Frida's Python binding
        # translates snake_case attribute access -> camelCase wire name.
        return self._script.exports_sync.cached_ts()

    def close(self) -> None:
        if self._script is not None:
            try: self._script.unload()
            except Exception: pass
            self._script = None
        if self._session is not None:
            try: self._session.detach()
            except Exception: pass
            self._session = None


def main() -> int:
    import argparse, pprint
    p = argparse.ArgumentParser(description="Invoke the live TT Lite MSSDK signer")
    p.add_argument("--url", required=True)
    p.add_argument("--headers", default="{}",
                   help="JSON dict of request headers")
    args = p.parse_args()
    sig = FridaSigner()
    signed = sig.sign_request(args.url, json.loads(args.headers))
    pprint.pp(signed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
