"""Run only explicitly selected native tests; a full suite is an explicit command."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import os
import sys
import subprocess
from pathlib import Path

from hermes_kubernetes.source.upstream import specification
from hermes_kubernetes.source.editable import ensure


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--source", type=Path, default=root / "hermes-agent")
    parser.add_argument(
        "--changed-from",
        help="Skip native tests when upstream/patch inputs did not change",
    )
    args = parser.parse_args()
    if args.changed_from and set(args.changed_from) != {"0"}:
        changed = subprocess.check_output(
            ["git", "diff", "--name-only", args.changed_from, "HEAD"],
            cwd=root,
            text=True,
        ).splitlines()
        if not any(p == "upstream.lock.json" or p.startswith("patches/") for p in changed):
            print("Native inputs unchanged; native tests skipped")
            raise SystemExit(0)
    paths = ["tests/"] if args.full else specification(root).get("native_tests", [])
    if not paths:
        print("No local core patches: native full suite is not required for this build")
        raise SystemExit(0)
    source = args.source.absolute()
    report = ensure(root, source, allow_local_edits=True)
    print(
        f"Native source: {report['state']}; base {report['actual_revision']}",
        flush=True,
    )
    for name in paths:
        if not (source / name).exists():
            raise ValueError("Selected native test path does not exist: " + name)
    environment = {
        **os.environ,
        "HERMES_HOME": str(root / ".local/test-home"),
        "HERMES_RUNTIME_DIR": str(root / ".local/test-runtime"),
    }
    subprocess.run(
        ["bash", str(source / "scripts/run_tests.sh"), *paths],
        cwd=source,
        env=environment,
        check=True,
    )
