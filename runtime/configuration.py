"""Retain personal preferences and route stdio MCPs to an isolated executor."""
import copy

from mcp_routing import validate_http_server

BRIDGE = "/opt/hermes-personal/ssh_mcp.py"
PYTHON = "/opt/hermes/.venv/bin/python"


def ssh_workspace_configuration(configuration, home, root, instance):
    """Make the profile's SSH policy available to Desktop as well as terminal tools."""
    config = copy.deepcopy(configuration)
    from agent.secret_scope import load_env_file

    profile_env = load_env_file(home / ".env")
    terminal = config.setdefault("terminal", {})
    backend = terminal.get("backend") or profile_env.get("TERMINAL_ENV") or "ssh"
    if backend != "ssh":
        return config
    terminal["backend"] = backend
    if not terminal.get("cwd"):
        terminal["cwd"] = profile_env.get("TERMINAL_CWD") or instance.get("workspace") or "~/.hermes"
    defaults = {"ssh_host": "hermes-terminal", "ssh_user": instance.get("ssh_user"),
                "ssh_port": instance.get("terminal_port", 22),
                "ssh_key": str(root / ".ssh/id_ed25519")}
    for key, fallback in defaults.items():
        value = profile_env.get("TERMINAL_" + key.upper()) or fallback
        if not terminal.get(key) and value:
            terminal[key] = int(value) if key == "ssh_port" else value
    return config


def restore_mcp(configuration, previous=None):
    """Migrate legacy bridge definitions back to native, editable configuration."""
    config = copy.deepcopy(configuration)
    for name, server in config.get("mcp_servers", {}).items():
        if not isinstance(server, dict) or server.get("args", [])[:1] != [BRIDGE]:
            continue
        if name not in (previous or {}):
            raise ValueError(f"Definição original do MCP {name} ausente")
        original = copy.deepcopy(previous[name])
        original.update({k: v for k, v in server.items() if k not in {"command", "args"}})
        config["mcp_servers"][name] = original
    for name, server in config.get("mcp_servers", {}).items():
        if isinstance(server, dict) and server.get("url"):
            validate_http_server(name, server)
    return config


def route_mcp(configuration, previous=None, *, state_home=None, ssh_home=None):
    config = copy.deepcopy(configuration)
    definitions = copy.deepcopy(previous or {})
    for name, server in config.get("mcp_servers", {}).items():
        if not isinstance(server, dict):
            continue
        if server.get("url"):
            validate_http_server(name, server)
            continue
        if not server.get("command"):
            continue
        if server.get("args", [])[:1] == [BRIDGE]:
            if name not in definitions:
                raise ValueError(f"Definição original do MCP {name} ausente")
        else:
            definitions[name] = copy.deepcopy(server)
        # Keep timeout, enabled, exclusions and other discovery settings. Env
        # and arguments travel over SSH stdin, never in the SSH command line.
        server["command"] = PYTHON
        server["args"] = [BRIDGE, name]
        if state_home is not None:
            server["args"] += ["--state-home", str(state_home), "--ssh-home", str(ssh_home or state_home)]
        server.pop("env", None)
        server.pop("cwd", None)
    return config, definitions
