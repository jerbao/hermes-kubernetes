"""Explicit, reusable production probes; never run unchanged integrations by default."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


async def check_mcp(name, request):
    from tools.mcp_tool_config import _load_mcp_config
    from tools.mcp_tool_discovery import _connect_server
    from tools.mcp_tool_common import mcp_field

    definition = _load_mcp_config()[name]
    if definition.get("enabled") is False:
        raise ValueError("Selected MCP is disabled")
    connection = await asyncio.wait_for(_connect_server(name, definition), 240)
    try:
        response = await connection.session.list_tools()
        names = [tool.name for tool in response.tools]
        if request:
            if request["tool"] not in names:
                raise ValueError("Requested tool is unavailable")
            result = await asyncio.wait_for(
                connection.session.call_tool(request["tool"], request.get("arguments", {})),
                90,
            )
            if mcp_field(result, "is_error", "isError", False):
                raise ValueError("MCP tool returned an error")
        return {"tools": len(names), "called": request.get("tool") if request else None}
    finally:
        await connection.shutdown()


def worker(options):
    from hermes_cli.env_loader import load_hermes_dotenv

    load_hermes_dotenv()
    from hermes_cli.plugins import discover_plugins

    discover_plugins(force=True)
    home = Path(os.environ["HERMES_HOME"])
    log = home / "logs/acceptance.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    requests = json.loads(options.requests.read_text()) if options.requests else {}
    checks = {}
    with (
        log.open("w") as handle,
        contextlib.redirect_stdout(handle),
        contextlib.redirect_stderr(handle),
    ):
        log.chmod(0o600)
        logging.basicConfig(stream=handle, level=logging.INFO, force=True)

        def check(name, callback):
            started = time.monotonic()
            try:
                checks[name] = {
                    "passed": True,
                    **callback(),
                    "seconds": round(time.monotonic() - started, 2),
                }
            except Exception as error:
                logging.exception("Acceptance failed: %s", name)
                checks[name] = {"passed": False, "error_type": type(error).__name__}

        for name in options.mcp:
            check(
                "mcp/" + name,
                lambda name=name: asyncio.run(check_mcp(name, requests.get(name))),
            )
        if options.whatsapp_url:

            def whatsapp():
                with urllib.request.urlopen(options.whatsapp_url, timeout=15) as response:
                    value = json.load(response)
                if value.get("status") != "connected":
                    raise ValueError("WhatsApp session is not connected")
                return {"status": "connected"}

            check("whatsapp", whatsapp)
        if options.moa:

            def moa():
                from run_agent import AIAgent

                agent = AIAgent(
                    provider="moa",
                    model="default",
                    api_key="moa-virtual-provider",
                    base_url="moa://local",
                    enabled_toolsets=[],
                    quiet_mode=True,
                    max_iterations=1,
                    skip_context_files=True,
                    skip_memory=True,
                    skip_background_review=True,
                    run_budget_seconds=180,
                )
                if agent.tools:
                    raise ValueError("Probe must not have tools")
                result = agent.run_conversation("Infrastructure acceptance probe. Reply with OK.")
                if not isinstance(result, dict) or not result.get("final_response"):
                    raise ValueError("Missing MoA response")
                return {"has_response": True, "tools": 0}

            check("moa", moa)
        if options.terminal_config:

            def terminal():
                from tools.environments.ssh import SSHEnvironment

                config = json.loads(options.terminal_config.read_text())
                ssh_config = config.pop("ssh_config", None)

                class ConfiguredSSH(SSHEnvironment):
                    def _build_ssh_command(self, *args, **kwargs):
                        command = super()._build_ssh_command(*args, **kwargs)
                        if ssh_config:
                            command[1:1] = ["-F", ssh_config]
                        return command

                environment = ConfiguredSSH(**config, probe_only=True)
                try:
                    result = environment._run_ssh(
                        "python3 -c "
                        + __import__("shlex").quote(
                            'from pathlib import Path; assert (Path.home()/".hermes/hermes-agent/.git").is_dir(); assert (Path.home()/".hermes-workspace-id").is_file()'
                        ),
                        30,
                    )
                    if result.returncode:
                        raise ValueError("Terminal source/persistence probe failed")
                    return {"source_accessible": True, "workspace_persistent": True}
                finally:
                    environment.cleanup()

            check("terminal", terminal)
    return {
        "passed": bool(checks) and all(item["passed"] for item in checks.values()),
        "checks": checks,
        "private_log": str(log),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mcp", action="append", default=[])
    p.add_argument(
        "--requests",
        type=Path,
        help="Private JSON mapping selected MCP names to {tool, arguments}",
    )
    p.add_argument(
        "--whatsapp-url",
        help="Health endpoint; checks connection without sending a message",
    )
    p.add_argument(
        "--moa",
        action="store_true",
        help="Explicitly spend one inference call on the configured MoA",
    )
    p.add_argument(
        "--terminal-config",
        type=Path,
        help="Private JSON SSHEnvironment parameters and optional ssh_config",
    )
    p.add_argument("--source", type=Path, default=Path("/opt/hermes"))
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args()
    if not (a.mcp or a.whatsapp_url or a.moa or a.terminal_config):
        p.error("Select at least one probe explicitly")
    if not a.worker:
        sys.path.insert(0, str(a.source))
        from hermes_cli._launchers import runtime_command

        code = Path(__file__).read_text()
        command = runtime_command(
            a.source,
            [*sys.argv[1:], "--worker"],
            code=code,
            home=os.environ.get("HERMES_HOME"),
            python=sys.executable,
        )
        return subprocess.run(command).returncode
    result = worker(a)
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
