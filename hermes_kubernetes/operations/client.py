"""Inspect and manage the live pod from the Hermes terminal VM, without kubectl."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import json
import os
import sys
from pathlib import Path

from hermes_kubernetes.operations.protocol import Client, OperationError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--socket",
        default=os.environ.get(
            "HERMES_RUNTIME_SOCKET",
            str(
                Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
                / ".local/runtime/control.sock"
            ),
        ),
    )
    parser.add_argument("--transfer-timeout", type=float, default=900)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("info")
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--output", required=True)
    for name in ("list", "stat", "get", "put", "sqlite-backup"):
        command = commands.add_parser(name)
        command.add_argument("path", nargs="?", default=".")
        command.add_argument("--scope", choices=["state", "source", "integration"], default="state")
        if name == "stat":
            command.add_argument("--sha256", action="store_true")
        if name in {"get", "sqlite-backup"}:
            command.add_argument("--output", required=True)
        if name == "put":
            command.add_argument("--input", required=True)
            command.add_argument(
                "--expected",
                required=True,
                help="Previous SHA-256, or 'absent' to create",
            )
    command = commands.add_parser("exec")
    command.add_argument("--timeout", type=float, default=120)
    command.add_argument("--scope", choices=["state", "source", "integration"], default="state")
    command.add_argument("--cwd", default=".")
    command.add_argument("argv", nargs=argparse.REMAINDER)
    arguments = vars(parser.parse_args())
    client = Client(
        arguments.pop("socket"),
        timeout=max(arguments.pop("transfer_timeout"), arguments.get("timeout", 0) + 10),
    )
    operation = arguments.pop("operation")
    try:
        if operation in {"get", "sqlite-backup", "snapshot"}:
            result = client.download(arguments.pop("output"), operation, **arguments)
        elif operation == "put":
            result = client.upload(arguments.pop("input"), **arguments)
        elif operation == "exec":
            argv = arguments.pop("argv")
            if argv[:1] == ["--"]:
                argv = argv[1:]
            result = client.execute(argv, sys.stdout.buffer, sys.stderr.buffer, **arguments)
            if result["timed_out"]:
                print("Runtime command timed out", file=sys.stderr)
                return 124
            return result["returncode"] if result["returncode"] >= 0 else 128 - result["returncode"]
        else:
            result = client.request(operation, **arguments)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OperationError, OSError, EOFError) as error:
        print(f"Runtime operation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
