"""Forward MCP stdio to its dedicated pod; credentials use encrypted stdin."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import json
import os
import subprocess
import sys
import threading
import re
import select
from pathlib import Path

from hermes_kubernetes.mcp.routing import (
    RoutingError,
    executor_target,
    prepare_definition,
    resolve_executor,
)

REMOTE = r"""
import json, os, pathlib, subprocess, sys, threading
definition = json.loads(sys.stdin.buffer.readline())
env = os.environ.copy()
home = pathlib.Path.home()
workspace = home / ".hermes"
env["PATH"] = str(home / ".local/bin") + ":" + str(workspace / "hermes-agent/.venv/bin") + ":" + str(workspace / "hermes-agent/venv/bin") + ":" + env.get("PATH", "")
env.update({str(k): str(v) for k, v in definition.get("env", {}).items()})
def copy(source, destination):
    try:
        while True:
            chunk = source.read1(65536)
            if not chunk:
                break
            destination.write(chunk)
            destination.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try: destination.close()
        except OSError: pass
process = subprocess.Popen([definition["command"], *definition.get("args", [])],
    cwd=definition.get("cwd") or str(workspace), env=env,
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr.buffer)
threading.Thread(target=copy, args=(sys.stdin.buffer, process.stdin), daemon=True).start()
copy(process.stdout, sys.stdout.buffer)
sys.exit(process.wait())
"""


def copy_stream(source, destination, stopped):
    try:
        # Poll the descriptor instead of holding BufferedReader's lock while
        # stdin remains open. The remote process can exit before its caller.
        while not stopped.is_set():
            if not select.select([source.fileno()], [], [], 0.1)[0]:
                continue
            chunk = os.read(source.fileno(), 65536)
            if not chunk:
                break
            destination.write(chunk)
            destination.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        destination.close()


def forward(command, definition):
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=sys.stdout.buffer,
        stderr=sys.stderr.buffer,
    )
    stopped = threading.Event()
    reader = None
    try:
        process.stdin.write(json.dumps(definition).encode() + b"\n")
        process.stdin.flush()
        reader = threading.Thread(
            target=copy_stream,
            args=(sys.stdin.buffer, process.stdin, stopped),
            daemon=True,
        )
        reader.start()
        return process.wait()
    finally:
        stopped.set()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if reader is not None:
            reader.join(timeout=1)
        else:
            process.stdin.close()


def build_ssh_command(home, policy=Path("/etc/hermes-policy")):
    # Hermes deliberately filters stdio child environments. Connection settings
    # must also be available from the mounted administrator policy.
    instance = json.loads((policy / "instance.json").read_text())
    host = os.environ.get("TERMINAL_SSH_HOST", "hermes-terminal")
    user = os.environ.get("TERMINAL_SSH_USER") or instance["ssh_user"]
    port = os.environ.get("TERMINAL_SSH_PORT", str(instance.get("terminal_port", 22)))
    key = os.environ.get("TERMINAL_SSH_KEY", str(home / ".ssh/id_ed25519"))
    command = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={home / '.ssh/known_hosts'}",
        "-o",
        "ConnectTimeout=10",
        "-i",
        key,
        "-p",
        port,
        f"{user}@{host}",
        "python3",
        "-c",
    ]
    import shlex

    command.append(shlex.quote(REMOTE))
    return command


def settings(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("server", nargs="?")
    parser.add_argument("--execution", action="store_true")
    parser.add_argument("--state-home", type=Path)
    parser.add_argument("--ssh-home", type=Path)
    args = parser.parse_args(argv)
    if args.execution:
        value = json.loads(os.environ.pop("HERMES_MCP_EXECUTION"))
        return (
            value["server"],
            Path(value["state_home"]),
            Path(value["ssh_home"]),
            value["definition"],
        )
    if not args.server:
        parser.error("server or --execution is required")
    home = args.state_home or Path(
        os.environ.get("HERMES_HOME") or os.environ.get("HOME") or "/opt/data"
    )
    return args.server, home, args.ssh_home or home, None


def build_mcp_command(
    home,
    policy=Path("/etc/hermes-policy"),
    server=None,
    definition=None,
    executor=None,
    legacy_server=None,
):
    instance = json.loads((policy / "instance.json").read_text())
    mcp = instance.get("mcp")
    if mcp is not None and not isinstance(mcp, dict):
        raise ValueError("Invalid MCP target")
    if not isinstance(mcp, dict):
        raise RoutingError("No MCP executor target is configured; refusing terminal SSH fallback")
    if executor is None:
        if definition is None:
            legacy = (
                mcp.get("servers", {}).get(server, {})
                if isinstance(mcp.get("servers"), dict)
                else {}
            )
            if isinstance(legacy, dict) and legacy.get("host"):
                executor, legacy_server = legacy.get("executor", "api"), server
            else:
                executor, legacy_server = "api", None
        else:
            executor, legacy_server, clean_args = resolve_executor(
                server or "", definition, instance
            )
            definition = {**definition, "args": clean_args}
    if executor not in {"api", "workspace", "browser", "vm"}:
        raise RoutingError("Invalid MCP executor")
    target = executor_target(instance, executor, server or "", legacy_server)
    if executor == "vm":
        private = home / ".ssh/mcp"
        if not (private / "id_ed25519").is_file() or not (private / "known_hosts").is_file():
            raise RoutingError(
                "MCP executor 'vm' requires its separately provisioned MCP SSH key and host key"
            )
    roots = target.get("roots", [])
    if roots and (
        not isinstance(roots, list)
        or not all(isinstance(root, str) and root.startswith("/") and root != "/" for root in roots)
    ):
        raise ValueError(f"Invalid {executor} MCP executor roots")
    host = target.get("host")
    if not isinstance(host, str) or not host:
        raise RoutingError(f"MCP executor '{executor}' has no configured host")
    user, port = target.get("user", "mcp"), target.get("port", 2222)
    # Current shared MCP pods publish the same host key known as hermes-mcp.
    # VM has a separate key file and therefore a separate alias by default.
    alias = target.get("host_key_alias", "hermes-mcp-vm" if executor == "vm" else "hermes-mcp")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9._:-]+", host):
        raise ValueError("Invalid MCP host")
    if not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
        raise ValueError("Invalid MCP user")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Invalid MCP port")
    if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", alias):
        raise ValueError("Invalid MCP host key alias")
    private = home / ".ssh/mcp"
    return [
        "ssh",
        "-T",
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        f"HostKeyAlias={alias}",
        "-o",
        f"UserKnownHostsFile={private / 'known_hosts'}",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-i",
        str(private / "id_ed25519"),
        "-p",
        str(port),
        f"{user}@{host}",
        "hermes-mcp",
    ]


def profile_name(home, root):
    if home == root:
        return "default"
    if home.parent != root / "profiles" or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", home.name):
        raise ValueError("Invalid MCP profile state path")
    return home.name


def main():
    name, home, ssh_home, original = settings()
    if original is None:
        original = json.loads((home / "state/hermes-ssh-mcp.json").read_text())[name]
    definition = {**original, "profile": profile_name(home, ssh_home), "server": name}
    policy = json.loads((Path("/etc/hermes-policy") / "instance.json").read_text())
    executor, legacy_server, definition = prepare_definition(name, definition, policy)
    command = build_mcp_command(
        ssh_home,
        server=name,
        definition=definition,
        executor=executor,
        legacy_server=legacy_server,
    )
    return forward(command, definition)


if __name__ == "__main__":
    sys.exit(main())
