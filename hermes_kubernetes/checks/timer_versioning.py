"""Prove that the installed timer publishes and removes an owned native skill.

Does not call reconcile/publish itself: both commits must come from the timer,
including its configured quiet period. No model or MCP inference is involved.
"""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
from hermes_kubernetes.operations.protocol import Client
from hermes_kubernetes.state.versioning import atomic, dump


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--timeout", type=int, default=900)
    a = p.parse_args()
    os.environ["XDG_RUNTIME_DIR"] = "/run/user/" + str(os.getuid())
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + os.environ["XDG_RUNTIME_DIR"] + "/bus"
    if subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", "hermes-workspace-sync.timer"]
    ).returncode:
        raise SystemExit("Enable the timer before this explicit proof")
    client = Client(str(ROOT / ".local/runtime/control.sock"), timeout=300)
    name = "timer-acceptance-" + uuid.uuid4().hex
    relative = "skills/" + name + "/SKILL.md"
    projected = "customizations/default/" + relative
    body = (
        "---\nname: "
        + name
        + "\ndescription: Owned automatic publication fixture.\n---\n\n"
        + name
        + "\n"
    )
    base = (
        'import os,json;from pathlib import Path;p=Path(os.environ["HERMES_HOME"])/'
        + repr(relative)
        + "\n"
    )
    report = {"fixture": name, "passed": False}

    def native(code):
        out, err = io.BytesIO(), io.BytesIO()
        result = client.execute(
            ["/opt/hermes/.venv/bin/python", "-c", base + code], out, err, timeout=60
        )
        if result["returncode"] or result["timed_out"]:
            raise RuntimeError("Owned timer fixture operation failed")

    def wait_for(present):
        deadline = time.monotonic() + a.timeout
        while time.monotonic() < deadline:
            revision = subprocess.check_output(
                ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
            ).strip()
            value = subprocess.run(
                ["git", "-C", str(ROOT), "show", "HEAD:" + projected],
                text=True,
                capture_output=True,
            )
            matches = (
                value.returncode == 0 and value.stdout == body if present else value.returncode != 0
            )
            if matches:
                remote = subprocess.check_output(
                    ["git", "-C", str(ROOT), "ls-remote", "origin", "refs/heads/main"],
                    text=True,
                    timeout=30,
                ).split()[0]
                if remote == revision:
                    return revision
            time.sleep(10)
        raise TimeoutError(
            "Timer did not publish the owned " + ("creation" if present else "removal")
        )

    try:
        native(
            "assert not p.exists();p.parent.mkdir();p.write_text("
            + repr(body)
            + ');p.chmod(0o600);print(json.dumps({"created":True}))'
        )
        print(
            json.dumps({"phase": "waiting-for-timer-publication", "fixture": name}),
            flush=True,
        )
        report["creation"] = wait_for(True)
        print(
            json.dumps({"phase": "timer-published", "revision": report["creation"]}),
            flush=True,
        )
    finally:
        native(
            "\nif p.exists():\n assert "
            + repr(name)
            + ' in p.read_text();p.unlink();p.parent.rmdir()\nprint(json.dumps({"removed":True}))'
        )
        report["removal"] = wait_for(False)
        report["passed"] = "creation" in report
        atomic(ROOT / ".local/versioning/timer-proof.json", dump(report))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
