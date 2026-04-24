"""
Capture VM-3's TikTok session by hooking TT Lite's OkHttp send path.

Run from VM-3 (where adb + frida-server are local):
    python3 capture_session.py
Then in TT Lite UI: type a keyword in the search bar, hit search.
The script prints the captured URL + headers as JSON to stdout, then exits.

Output is a dict: {"url": "...", "headers": {"Cookie": "...", ...}}
Pipe to a file and feed to render_replay_vm3.py.
"""

import json
import pathlib
import subprocess
import sys
import time

import frida


AGENT_PATH = pathlib.Path(__file__).parent / "frida" / "capture_search.js"
ADB_DEVICE = "192.168.240.204:5555"
FRIDA_HOST = "127.0.0.1:27042"
TT_PACKAGE = "com.tiktok.lite.go"


def pid_of_tt() -> int:
    out = subprocess.run(
        ["adb", "-s", "127.0.0.1:5556", "shell", "pidof", TT_PACKAGE],
        capture_output=True, text=True, timeout=10,
    )
    pid_str = out.stdout.strip().split()[0] if out.stdout.strip() else ""
    return int(pid_str) if pid_str.isdigit() else 0


def main() -> int:
    pid = pid_of_tt()
    if not pid:
        print("ERROR: TT Lite not running. Open it in VNC first.", file=sys.stderr)
        return 1
    print(f"[capture] TT Lite pid={pid}", file=sys.stderr)

    device = frida.get_device_manager().add_remote_device(FRIDA_HOST)
    session = device.attach(pid)
    script = session.create_script(AGENT_PATH.read_text(), runtime="v8")

    captured = {}

    def on_message(m, data):
        if m["type"] == "send":
            payload = m["payload"]
            if isinstance(payload, dict) and payload.get("type") == "search_request":
                captured["data"] = payload["payload"]
        elif m["type"] == "error":
            print(f"[agent:error] {m.get('description')}", file=sys.stderr)

    script.on("message", on_message)
    script.load()

    print("[capture] hook installed. In TT Lite UI: open the search bar,",
          file=sys.stderr)
    print("[capture] type any keyword, tap search. Waiting up to 5 minutes...",
          file=sys.stderr)

    # Wait up to 5 minutes for the user to drive the UI.
    deadline = time.time() + 300
    while not captured and time.time() < deadline:
        time.sleep(0.5)

    if not captured:
        print("[capture] TIMEOUT — no search captured. Did you tap the search button?",
              file=sys.stderr)
        return 2

    data = captured["data"]
    print(f"[capture] got URL: {data['url'][:120]}...", file=sys.stderr)
    print(f"[capture] {len(data['headers'])} headers", file=sys.stderr)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
