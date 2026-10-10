"""Generate or drift-check the executor identity map for one Hermes source release.

Examples:
  python3 hermes_kubernetes/mcp/catalog.py --source .build/hermes-agent --write
  python3 hermes_kubernetes/mcp/catalog.py --source .build/hermes-agent --check

The generated file contains public connector identities only. User config, endpoint
credentials and profile-specific URLs are never copied into the map.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import argparse
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESTINATION = ROOT / "config" / "mcp-executors.json"


def default_source() -> Path:
    canonical = ROOT / ".build" / "hermes-agent"
    if canonical.is_dir():
        return canonical
    releases = sorted(
        path.parent.parent for path in (ROOT / ".build").glob("hermes-v*/hermes_cli/mcp_config.py")
    )
    return releases[0] if len(releases) == 1 else canonical


# Reviewed package defaults for the MCP packages currently shipped by the
# executor image. A newly-added package must be classified here before the
# drift check can pass.
PACKAGE_EXECUTORS = {
    "@colbymchenry/codegraph": (
        "workspace",
        "Reads project files from the mounted workspace.",
    ),
    "@modelcontextprotocol/server-sequential-thinking": (
        "api",
        "Stateless package; no workspace path is required.",
    ),
    "@upstash/context7-mcp": (
        "api",
        "Remote documentation service; no workspace path is required.",
    ),
    "@zereight/mcp-gitlab": (
        "api",
        "Remote GitLab service; no workspace path is required.",
    ),
    "chrome-devtools-mcp": (
        "browser",
        "Controls a browser session on the browser executor.",
    ),
    "@playwright/mcp": (
        "browser",
        "Controls a browser session on the browser executor.",
    ),
    "@modelcontextprotocol/server-github": (
        "api",
        "Remote GitHub service; no workspace path is required.",
    ),
}

PRESET_EXECUTORS = {
    # The Codex CLI carries its own account state. Its dedicated VM target must
    # be separately provisioned; this label never grants terminal SSH access.
    "codex": "vm",
}


def _scalar(value: str) -> str:
    value = value.split(" #", 1)[0].strip()
    if value.startswith('"'):
        return str(json.loads(value))
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value


def manifest_transport(path: Path) -> dict[str, str]:
    """Read the small, stable transport subsection without adding a YAML dependency."""
    lines = path.read_text().splitlines()
    block = False
    result: dict[str, str] = {}
    for line in lines:
        if not block:
            if re.fullmatch(r"transport:\s*(?:#.*)?", line):
                block = True
            continue
        if line and not line[0].isspace():
            break
        match = re.match(r"^  (type|url|command):\s*(.*?)\s*$", line)
        if match:
            result[match.group(1)] = _scalar(match.group(2))
    if result.get("type") not in {"http", "stdio"}:
        raise ValueError(f"{path}: unsupported/missing transport type")
    return result


def _assignment(path: Path, name: str):
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            return ast.literal_eval(node.value)
    raise ValueError(f"{path}: assignment {name} was not found")


def _package_from_argv(command: str, args: list[str]) -> str | None:
    executable = Path(command).name.lower()
    values = list(args)
    if executable in {"npx", "npm"}:
        if executable == "npm" and values[:1] == ["exec"]:
            values = values[1:]
        while values and values[0] in {"-y", "--yes"}:
            values.pop(0)
        while values and values[0] in {"-p", "--package"}:
            values.pop(0)
            if values:
                package = values.pop(0)
                return _package_name(package)
        if values and values[0].startswith("--package="):
            return _package_name(values.pop(0).split("=", 1)[1])
        return _package_name(values[0]) if values else None
    if executable in {"uvx", "uv"}:
        if executable == "uv" and values[:1] == ["tool"]:
            values = values[1:]
        package = None
        while values and values[0].startswith("-"):
            option = values.pop(0)
            if option in {"--from", "--with", "--python"} and values:
                value = values.pop(0)
                if option == "--from":
                    package = value
            elif option.startswith("--from="):
                package = option.split("=", 1)[1]
        return _package_name(package or values[0]) if package or values else None
    return None


def _package_name(value: str) -> str:
    value = value.strip()
    if value.startswith("@") and "/" in value:
        slash = value.find("/")
        at = value.find("@", slash + 1)
        return value[:at] if at >= 0 else value
    return value.split("@", 1)[0]


def generate(source: Path) -> dict:
    source = source.resolve()
    mcp_config = source / "hermes_cli" / "mcp_config.py"
    runner = ROOT / "mcp" / "runner.py"
    presets = _assignment(mcp_config, "_MCP_PRESETS")
    package_bins = _assignment(runner, "NPM_BINS")
    if set(presets) != set(PRESET_EXECUTORS):
        missing = sorted(set(presets) - set(PRESET_EXECUTORS))
        stale = sorted(set(PRESET_EXECUTORS) - set(presets))
        raise ValueError(f"preset map drift: unmapped={missing}, stale={stale}")
    if set(package_bins) != set(PACKAGE_EXECUTORS):
        missing = sorted(set(package_bins) - set(PACKAGE_EXECUTORS))
        stale = sorted(set(PACKAGE_EXECUTORS) - set(package_bins))
        raise ValueError(f"package map drift: unmapped={missing}, stale={stale}")

    entries = {}
    for manifest in sorted((source / "optional-mcps").glob("*/manifest.yaml")):
        name = manifest.parent.name
        transport = manifest_transport(manifest)
        kind = transport["type"]
        identity: dict[str, str]
        executor: str | None
        safety = None
        if kind == "http":
            url = transport.get("url", "")
            if not url:
                raise ValueError(f"{manifest}: HTTP transport has no URL")
            identity = {"url": url}
            if url.startswith("https://") or "${" in url:
                executor = "api"
                reason = "Remote HTTP endpoint; Hermes uses its native HTTP transport."
            elif url.startswith("http://127.0.0.1") or url.startswith("http://localhost"):
                executor = None
                safety = "local-only"
                reason = "Editor-local loopback; a public executor cannot reach the editor host."
            else:
                raise ValueError(f"{manifest}: HTTP URL needs an explicit reviewed executor policy")
        else:
            command = transport.get("command", "")
            args = []
            collecting = False
            for line in manifest.read_text().splitlines():
                if re.match(r"^  args:\s*$", line):
                    collecting = True
                    continue
                if collecting:
                    item = re.match(r"^    -\s*(.*?)\s*$", line)
                    if item:
                        args.append(_scalar(item.group(1)))
                    elif line and not line[0].isspace():
                        collecting = False
            package = _package_from_argv(command, args)
            if package not in PACKAGE_EXECUTORS:
                raise ValueError(
                    f"{manifest}: stdio package {package!r} needs a reviewed executor mapping"
                )
            executor, reason = PACKAGE_EXECUTORS[package]
            identity = {"command": command, "package": package}
        entries[name] = {
            "transport": kind,
            "identity": identity,
            "executor": executor,
            "reason": reason,
            **({"safety": safety} if safety else {}),
        }

    packages = {
        name: {
            "executor": PACKAGE_EXECUTORS[name][0],
            "reason": PACKAGE_EXECUTORS[name][1],
            "executable": package_bins[name],
        }
        for name in sorted(package_bins)
    }
    executables = {}
    for package, entry in packages.items():
        executable = entry["executable"]
        prior = executables.get(executable)
        if prior and prior["executor"] != entry["executor"]:
            raise ValueError(
                f"executable {executable!r} is shared by packages with conflicting routes"
            )
        executables[executable] = {
            "package": package,
            "executor": entry["executor"],
            "reason": entry["reason"],
        }
    preset_entries = {}
    for name, config in sorted(presets.items()):
        if not isinstance(config, dict) or not isinstance(config.get("command"), str):
            raise ValueError(f"{mcp_config}: malformed MCP preset {name}")
        preset_entries[name] = {
            "identity": {
                "command": config["command"],
                "args": list(config.get("args") or []),
            },
            "executor": PRESET_EXECUTORS[name],
            "reason": "Preset-specific identity; target must be provisioned explicitly.",
        }

    return {
        "schema": 1,
        "source": "hermes-agent optional-mcps manifests, MCP presets and personal executor package registry",
        "defaults": {"http": "api", "stdio": "api"},
        "catalog": entries,
        "packages": packages,
        "executables": executables,
        "presets": preset_entries,
        "path_policy": {"workspace": "mcp.workspace_target.roots; exact path boundary match"},
        "safety": {
            "vm_requires": "mcp.executor_targets.vm and the MCP-specific SSH key",
            "loopback": "local-only endpoints cannot be routed through a public executor",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=default_source(),
        help="Hermes release checkout root",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="regenerate config/mcp-executors.json")
    action.add_argument(
        "--check", action="store_true", help="fail if the checked-in map has drifted"
    )
    args = parser.parse_args(argv)
    expected = generate(args.source)
    encoded = json.dumps(expected, ensure_ascii=False, indent=2) + "\n"
    if args.write:
        DESTINATION.parent.mkdir(parents=True, exist_ok=True)
        DESTINATION.write_text(encoded)
        print(
            f"Wrote {len(expected['catalog'])} catalog MCPs, {len(expected['packages'])} packages, "
            f"and {len(expected['presets'])} presets to {DESTINATION.relative_to(ROOT)}"
        )
        return 0
    try:
        actual = json.loads(DESTINATION.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Cannot read {DESTINATION}: {exc}")
        return 1
    if actual != expected:
        print(
            "MCP executor map drifted; review the release manifests/presets/packages, then run --write"
        )
        return 1
    print(
        f"MCP executor map matches: {len(expected['catalog'])} catalog MCPs, "
        f"{len(expected['packages'])} packages, {len(expected['presets'])} presets"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
