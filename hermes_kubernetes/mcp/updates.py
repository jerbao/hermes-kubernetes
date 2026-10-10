"""List, plan, or update isolated MCP packages through their authenticated executor."""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
from hermes_kubernetes.mcp.routing import prepare_definition
from hermes_kubernetes.mcp.bridge import build_mcp_command, profile_name
from hermes_kubernetes.runtime.configuration import restore_mcp


def definitions_from(path: Path, state_home: Path) -> dict:
    """Read the live config unless an explicit JSON definitions file was requested."""
    live_config = state_home / "config.yaml"
    if path.resolve() == live_config.resolve():
        path = live_config
        try:
            try:
                import hermes_yaml as yaml

                safe_load = yaml.safe_load
            except ImportError:
                try:
                    import yaml

                    safe_load = yaml.safe_load
                except ImportError:
                    from ruamel.yaml import YAML

                    safe_load = YAML(typ="safe").load
            configuration = safe_load(path.read_text())
            previous = (
                json.loads((state_home / "state/hermes-ssh-mcp.json").read_text())
                if (state_home / "state/hermes-ssh-mcp.json").is_file()
                else {}
            )
            if not isinstance(configuration, dict):
                raise ValueError("Invalid Hermes configuration")
            return restore_mcp(configuration, previous).get("mcp_servers", {})
        except Exception as error:
            raise ValueError("MCP definitions file is unavailable or invalid") from error
    if not path.is_file():
        raise ValueError("MCP definitions file is unavailable or invalid")
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("MCP definitions file is unavailable or invalid") from error
    if isinstance(value, dict) and isinstance(value.get("mcp_servers"), dict):
        value = value["mcp_servers"]
    if isinstance(value, dict) and isinstance(value.get("mcpServers"), dict):
        value = value["mcpServers"]
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, dict) for k, v in value.items()
    ):
        raise ValueError("MCP definitions must be a JSON object keyed by server name")
    return value


def run_request(
    name: str,
    definition: dict,
    action: str,
    profile: str,
    ssh_home: Path,
    policy: dict,
    policy_path: Path,
    version: str | None = None,
    recipe: dict | None = None,
) -> dict:
    if isinstance(definition.get("url"), str):
        return {"name": name, "status": "skipped-http", "provider_managed": True}
    enriched = {**definition, "profile": profile, "server": name}
    executor, legacy_server, routed = prepare_definition(name, enriched, policy)
    command = build_mcp_command(
        ssh_home,
        policy_path,
        server=name,
        definition=routed,
        executor=executor,
        legacy_server=legacy_server,
    )
    item = {"name": name, "definition": routed}
    if version:
        item["version"] = version
    if recipe is not None:
        item["recipe"] = recipe
    payload = {
        "operation": "manage",
        "action": action,
        "profile": profile,
        "servers": [item],
    }
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(payload).encode() + b"\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=300,
            check=False,
        )
        # The bridge returns one JSON response for management. Never show SSH
        # diagnostics: they may contain environment values or remote arguments.
        response = json.loads(completed.stdout.decode()) if completed.stdout else None
    except (OSError, subprocess.TimeoutExpired, UnicodeError, json.JSONDecodeError):
        return {
            "name": name,
            "status": "failed",
            "error": "Authenticated MCP executor did not return a valid result",
        }
    if not isinstance(response, dict) or not isinstance(response.get("results"), list):
        return {
            "name": name,
            "status": "failed",
            "error": "Authenticated MCP executor returned an invalid result",
        }
    rows = response["results"]
    if len(rows) != 1 or not isinstance(rows[0], dict):
        return {
            "name": name,
            "status": "failed",
            "error": "Authenticated MCP executor returned an invalid result",
        }
    return rows[0]


def execute(
    action: str,
    selected: list[str],
    definitions: dict,
    profile: str,
    ssh_home: Path,
    policy: dict,
    policy_path: Path,
    version: str | None = None,
    recipes: dict | None = None,
) -> dict:
    names = list(definitions) if selected == ["--all"] else selected
    results = []
    for name in names:
        definition = definitions.get(name)
        if not isinstance(definition, dict):
            results.append({"name": name, "status": "failed", "error": "MCP is not configured"})
            continue
        try:
            arguments = (
                name,
                definition,
                action,
                profile,
                ssh_home,
                policy,
                policy_path,
                version,
            )
            results.append(
                run_request(*arguments, recipe=recipes.get(name))
                if recipes is not None
                else run_request(*arguments)
            )
        except Exception:
            results.append(
                {
                    "name": name,
                    "status": "failed",
                    "error": "MCP executor routing or request failed",
                }
            )
    report = {
        "action": action,
        "profile": profile,
        "results": results,
        "ok": all(row.get("status") not in {"failed"} for row in results),
    }
    if (
        action in {"apply", "restore"}
        and report["ok"]
        and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", profile)
    ):
        # The GUI and CLI both use this bridge. Record a completed update on the
        # next controller tick instead of waiting for the metadata TTL.
        (ssh_home / "state/versioning/mcp" / (profile + ".json")).unlink(missing_ok=True)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("list", "plan", "apply", "restore"))
    parser.add_argument("servers", nargs="*", help="MCP name(s)")
    parser.add_argument("--all", action="store_true", help="Select all configured MCPs")
    parser.add_argument("--definitions", type=Path, help="JSON snapshot keyed by MCP name")
    parser.add_argument(
        "--state-home",
        type=Path,
        default=Path(os.environ.get("HERMES_HOME", "/opt/data")),
    )
    parser.add_argument("--ssh-home", type=Path, help="State home containing the MCP-only SSH key")
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("/etc/hermes-policy"),
        help="Directory containing instance.json, or its path",
    )
    parser.add_argument(
        "--recipes", type=Path, help="Versioned exact installation recipes for restore"
    )
    parser.add_argument("--version", help="Explicit package version for a single MCP")
    args = parser.parse_args(argv)
    if args.all == bool(args.servers):
        parser.error("select MCP names or use --all")
    if args.version and (
        len(args.servers) != 1
        or args.all
        or not re.fullmatch(r"[A-Za-z0-9.+_-]{1,128}", args.version)
    ):
        parser.error("--version requires one MCP and a simple package version")
    state_home = args.state_home.expanduser().resolve()
    ssh_home = (args.ssh_home or state_home).expanduser().resolve()
    definitions_path = args.definitions or state_home / "config.yaml"
    try:
        definitions = definitions_from(definitions_path, state_home)
        policy_path = args.policy
        if policy_path.is_dir() or policy_path.name != "instance.json":
            policy_dir = policy_path
            policy_file = policy_dir / "instance.json"
        else:
            policy_file = policy_path
            policy_dir = policy_path.parent
        policy = json.loads(policy_file.read_text())
        if not isinstance(policy, dict):
            raise ValueError("Invalid executor policy")
        profile = profile_name(state_home, ssh_home)
        selected = ["--all"] if args.all else args.servers
        report = execute(
            args.action,
            selected,
            definitions,
            profile,
            ssh_home,
            policy,
            policy_dir,
            args.version,
            json.loads(args.recipes.read_text()) if args.recipes else None,
        )
    except (OSError, ValueError, json.JSONDecodeError):
        print(
            json.dumps(
                {
                    "action": args.action,
                    "ok": False,
                    "error": "MCP update configuration is unavailable or invalid",
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
