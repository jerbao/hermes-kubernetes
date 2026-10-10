"""Resolve an MCP stdio executor without changing the server's real argv.

The executor selector is an opt-in bridge argument, ``--hermes-executor=...``.
It is consumed before the MCP process starts. HTTP MCPs stay on Hermes' native
transport and are only checked here for loopback endpoints that cannot work
from the public API pod.
"""

from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


EXECUTORS = frozenset({"api", "workspace", "browser", "vm"})
SELECTOR_PREFIX = "--hermes-executor="


class RoutingError(ValueError):
    """The requested MCP route cannot be resolved safely."""


def load_routing_catalog(path: Path | None = None) -> dict:
    module = Path(__file__).resolve()
    project_root = module.parent.parent if module.parent.name == "runtime" else module.parent
    catalog_path = path or project_root / "config" / "mcp-executors.json"
    try:
        value = json.loads(catalog_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutingError(f"MCP executor catalog unavailable: {catalog_path}") from exc
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise RoutingError("MCP executor catalog has an unsupported schema")
    return value


def extract_executor(args: list[str] | tuple[str, ...]) -> tuple[str | None, list[str]]:
    """Remove the reserved selector from argv; reject duplicate or malformed selectors."""
    selected = None
    remaining: list[str] = []
    for arg in args:
        if not isinstance(arg, str):
            raise RoutingError("MCP arguments must be strings")
        if arg.startswith(SELECTOR_PREFIX):
            if selected is not None:
                raise RoutingError("MCP arguments contain more than one --hermes-executor selector")
            selected = arg[len(SELECTOR_PREFIX):]
            if selected not in EXECUTORS:
                raise RoutingError("--hermes-executor must be api, workspace, browser, or vm")
        elif arg == "--hermes-executor":
            raise RoutingError("Use --hermes-executor=api|workspace|browser|vm")
        else:
            remaining.append(arg)
    return selected, remaining


def _clean_package(value: str) -> str:
    """Remove an npm/uv version suffix while preserving scoped package names."""
    value = value.strip()
    if value.startswith("@"):
        slash = value.find("/")
        if slash >= 0:
            at = value.find("@", slash + 1)
            return value[:at] if at >= 0 else value
    at = value.find("@")
    return value[:at] if at >= 0 else value


def package_identities(command: str, args: list[str]) -> set[str]:
    """Return package names explicitly named to npx/uvx, without looking at arbitrary argv."""
    executable = Path(command).name.lower()
    values = list(args)
    packages: set[str] = set()
    if executable in {"npx", "npm"}:
        if executable == "npm" and values[:1] == ["exec"]:
            values = values[1:]
        while values and values[0] in {"-y", "--yes"}:
            values.pop(0)
        while values and values[0] in {"-p", "--package"}:
            values.pop(0)
            if values:
                packages.add(_clean_package(values.pop(0)))
        while values and values[0].startswith("--package="):
            packages.add(_clean_package(values.pop(0).split("=", 1)[1]))
        if values and values[0] == "--":
            values.pop(0)
        if not packages and values:
            packages.add(_clean_package(values.pop(0)))
    elif executable in {"uvx", "uv"}:
        if executable == "uv" and values[:1] == ["tool"]:
            values = values[1:]
        while values and values[0].startswith("-"):
            option = values.pop(0)
            if option in {"--from", "--with", "--python"} and values:
                value = values.pop(0)
                if option in {"--from", "--with"}:
                    packages.add(_clean_package(value))
            elif option.startswith("--from=") or option.startswith("--with="):
                packages.add(_clean_package(option.split("=", 1)[1]))
            elif option == "--":
                break
            else:
                break
        if values:
            packages.add(_clean_package(values[0]))
    return {package for package in packages if package}


def _normalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if not parsed.scheme or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    port = parsed.port
    if port and not ((parsed.scheme.lower() == "https" and port == 443)
                     or (parsed.scheme.lower() == "http" and port == 80)):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    return f"{parsed.scheme.lower()}://{host}{path}" + (f"?{parsed.query}" if parsed.query else "")


def _is_loopback_url(value: str) -> bool:
    try:
        host = (urlsplit(value).hostname or "").lower().rstrip(".")
        if host == "localhost" or host.endswith(".localhost"):
            return True
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_http_server(name: str, server: Mapping[str, Any], catalog: dict | None = None) -> None:
    """Reject the catalog's editor-local Unreal endpoint from container-side API routing."""
    url = server.get("url")
    if not isinstance(url, str) or not url:
        return
    if not _is_loopback_url(url):
        return
    entries = (catalog or load_routing_catalog()).get("catalog", {})
    entry = entries.get(name, {}) if isinstance(entries, dict) else {}
    if name != "unreal-engine" and entry.get("safety") != "local-only":
        return
    reason = entry.get("reason") or "the Unreal Editor binds only to its own host"
    raise RoutingError(f"MCP '{name}' points to a local loopback endpoint ({reason}); configure a reachable host URL")


def _matches_catalog(name: str, url: str, entries: Mapping[str, Any]) -> dict | None:
    normalized = _normalize_url(url) if url else ""
    for entry in entries.values():
        identity = entry.get("identity", {})
        expected = identity.get("url") if isinstance(identity, dict) else None
        if normalized and expected and _normalize_url(str(expected)) == normalized:
            return entry
    candidate = entries.get(name)
    if candidate is not None:
        identity = candidate.get("identity", {})
        expected = identity.get("url") if isinstance(identity, dict) else None
        if not expected or (normalized and _normalize_url(str(expected)) == normalized):
            return candidate
    return None


def _path_is_under(value: str, roots: list[str]) -> bool:
    path = value.removeprefix("file:")
    for root in roots:
        boundary = root.rstrip("/")
        if path == boundary or path.startswith(boundary + "/"):
            return True
    return False


def _uses_workspace_paths(definition: Mapping[str, Any], roots: list[str]) -> bool:
    values = [definition.get("cwd", ""), *(definition.get("args") or [])]
    return any(isinstance(value, str) and _path_is_under(value, roots)
               for value in values)


def _uses_plugin_paths(definition: Mapping[str, Any]) -> bool:
    """Portable plugin payloads live with Hermes and must not be sent to public executors."""
    values = [definition.get("command", ""), definition.get("cwd", ""),
              *(definition.get("args") or [])]
    environment = definition.get("env", {})
    if isinstance(environment, dict):
        if {"PLUGIN_ROOT", "PLUGIN_DATA"} & set(environment):
            return True
        values.extend(environment.values())
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = value.replace("\\", "/")
        if "/plugins/" in normalized or normalized.startswith("plugins/"):
            return True
    return False


def resolve_executor(
    name: str,
    definition: Mapping[str, Any],
    policy: Mapping[str, Any],
    catalog: dict | None = None,
) -> tuple[str, str | None, list[str]]:
    """Resolve target in the agreed order and consume the selector from returned argv.

    Returns ``(executor, legacy_server_target, stripped_args)``. A non-None legacy
    target names a pre-existing explicit ``mcp.servers.<name>`` host override.
    """
    current = catalog or load_routing_catalog()
    command = definition.get("command") or ""
    args = list(definition.get("args") or [])
    if not isinstance(command, str):
        raise RoutingError("MCP stdio command must be a string")
    explicit, stripped = extract_executor(args)
    mcp_policy = policy.get("mcp") if isinstance(policy.get("mcp"), dict) else {}
    servers = mcp_policy.get("servers", {})
    legacy = servers.get(name) if isinstance(servers, dict) else None
    legacy_target = name if isinstance(legacy, dict) and isinstance(legacy.get("host"), str) else None
    if explicit:
        # A route selector names the executor, not merely an annotation on the
        # old compatibility host. Resolve the actual host from executor_targets.
        return explicit, None, stripped

    packages = package_identities(command, stripped)
    package_map = current.get("packages", {})
    for package in sorted(packages):
        mapped = package_map.get(package) if isinstance(package_map, dict) else None
        if isinstance(mapped, dict) and mapped.get("executor") in EXECUTORS:
            executor = mapped["executor"]
            return executor, None, stripped

    presets = current.get("presets", {})
    executable = Path(command).name
    preset = presets.get(executable) if isinstance(presets, dict) else None
    if isinstance(preset, dict):
        identity = preset.get("identity", {})
        expected_args = identity.get("args", []) if isinstance(identity, dict) else []
        if (identity.get("command") == command and stripped[:len(expected_args)] == expected_args
                and preset.get("executor") in EXECUTORS):
            return preset["executor"], None, stripped

    executables = current.get("executables", {})
    mapped = executables.get(executable) if isinstance(executables, dict) else None
    if isinstance(mapped, dict) and mapped.get("executor") in EXECUTORS:
        executor = mapped["executor"]
        return executor, None, stripped

    entry = _matches_catalog(name, str(definition.get("url") or ""),
                             current.get("catalog", {}) if isinstance(current, dict) else {})
    if entry is not None:
        executor = entry.get("executor")
        if executor in EXECUTORS:
            return executor, None, stripped
        if entry.get("safety") == "local-only":
            raise RoutingError(f"MCP '{name}' is a local-only endpoint and cannot be routed through a public executor")

    if isinstance(legacy, dict):
        selected = legacy.get("executor")
        if selected in EXECUTORS:
            return selected, legacy_target if selected != "vm" else None, stripped
        if isinstance(legacy.get("host"), str) and legacy.get("host"):
            return "api", name, stripped

    routed_definition = {**definition, "args": stripped}
    if _uses_plugin_paths(routed_definition):
        raise RoutingError(
            f"MCP '{name}' references files under a plugin directory; set an explicit executor "
            "or an administrator target override"
        )

    workspace = mcp_policy.get("workspace_target", {})
    roots = workspace.get("roots", []) if isinstance(workspace, dict) else []
    roots = [root for root in roots if isinstance(root, str) and root.startswith("/") and root != "/"]
    if _uses_workspace_paths({**definition, "args": stripped}, roots):
        return "workspace", None, stripped
    return "api", None, stripped


def prepare_definition(name: str, definition: Mapping[str, Any], policy: Mapping[str, Any],
                       catalog: dict | None = None) -> tuple[str, str | None, dict]:
    """Resolve a definition and remove the bridge-only selector before SSH transport."""
    executor, legacy_server, args = resolve_executor(name, definition, policy, catalog)
    clean = dict(definition)
    clean["args"] = args
    return executor, legacy_server, clean


def executor_target(policy: Mapping[str, Any], executor: str, name: str,
                    legacy_server: str | None = None) -> dict:
    """Select a configured SSH target; VM always requires a separately configured MCP target."""
    mcp_policy = policy.get("mcp") if isinstance(policy.get("mcp"), dict) else {}
    if executor == "vm":
        targets = mcp_policy.get("executor_targets", {})
        if not isinstance(targets, dict) or not isinstance(targets.get("vm"), dict):
            raise RoutingError(f"MCP '{name}' requested executor 'vm' but no mcp.executor_targets.vm is configured")
        return dict(targets["vm"])
    if legacy_server:
        legacy = mcp_policy.get("servers", {}).get(legacy_server, {})
        if isinstance(legacy, dict):
            return {**mcp_policy, **legacy}
    targets = mcp_policy.get("executor_targets", {})
    if isinstance(targets, dict) and isinstance(targets.get(executor), dict):
        return dict(targets[executor])
    if executor == "api":
        return dict(mcp_policy)
    if executor == "workspace" and isinstance(mcp_policy.get("workspace_target"), dict):
        return dict(mcp_policy["workspace_target"])
    raise RoutingError(f"MCP '{name}' requires an explicitly configured {executor} executor target")
