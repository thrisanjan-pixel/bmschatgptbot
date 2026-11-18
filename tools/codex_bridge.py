"""Bridge between Claude Code and a Codex app thread.

send:  python tools/codex_bridge.py send <message-file>
       Queues the message into the Codex thread with `codex queue`.
wait:  python tools/codex_bridge.py wait [--timeout SECONDS]
       Blocks until the thread logs a new `task_complete` event after the
       current end of its rollout file, then prints Codex's final messages
       from that turn. Exit 0 on a finished turn, 2 on timeout.

Claude runs `wait` in the background; when it exits, Claude reads the
report, decides the next step and runs `send` again.
"""
import glob
import json
import os
import subprocess
import sys
import time

THREAD_ID = "01a1e89d-ae7b-7591-bd4e-caa0b5a84322"
SESSIONS = os.path.join(os.path.expanduser("~"), ".codex", "sessions")


def rollout_path():
    hits = glob.glob(os.path.join(SESSIONS, "**", f"*{THREAD_ID}*.jsonl"), recursive=True)
    if not hits:
        sys.exit(f"No rollout file found for thread {THREAD_ID}")
    return max(hits, key=os.path.getmtime)


def send(msg_file):
    with open(msg_file, encoding="utf-8") as f:
        msg = f.read().strip()
    # PowerShell and cmd mangle embedded double quotes in native arguments.
    msg = msg.replace('"', "'")
    r = subprocess.run(["codex", "queue", "--thread", THREAD_ID, "--message", msg],
                       capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    sys.exit(r.returncode)


def wait(timeout):
    path = rollout_path()
    offset = os.path.getsize(path)
    buf = ""
    messages = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        size = os.path.getsize(path)
        if size > offset:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read(size - offset).decode("utf-8", "replace")
            offset = size
            buf += chunk
            lines = buf.split("\n")
            buf = lines.pop()
            for line in lines:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                p = o.get("payload") or {}
                if p.get("type") == "message" and p.get("role") == "assistant":
                    text = " ".join(c.get("text", "") for c in p.get("content", [])
                                    if isinstance(c, dict)).strip()
                    if text:
                        messages.append(text)
                if o.get("type") == "event_msg" and p.get("type") == "task_complete":
                    print("CODEX TURN COMPLETE")
                    print("\n\n---\n\n".join(messages[-4:]))
                    sys.exit(0)
        time.sleep(5)
    print("TIMEOUT: Codex turn still running. Last messages:")
    print("\n\n---\n\n".join(messages[-2:]))
    sys.exit(2)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "send":
        send(sys.argv[2])
    elif len(sys.argv) >= 2 and sys.argv[1] == "wait":
        t = 3000
        if "--timeout" in sys.argv:
            t = int(sys.argv[sys.argv.index("--timeout") + 1])
        wait(t)
    else:
        sys.exit(__doc__)
