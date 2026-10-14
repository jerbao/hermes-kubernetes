"""Start the personal Hermes distribution on fresh or existing persistent state."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import fcntl
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import hermes_yaml as yaml

from hermes_kubernetes.runtime.configuration import (
    is_bridge,
    restore_mcp,
    ssh_workspace_configuration,
)
from hermes_kubernetes.state.prepare import separate_env_files


def atomic_write(path, text):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    temporary.replace(path)


def ssh_configuration(instance, home):
    target = instance["terminal_host"]
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", target):
        raise ValueError("Invalid SSH target")
    port = instance.get("terminal_port", 22)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Invalid SSH port")
    transport = instance.get(
        "terminal_transport",
        "tailnet" if instance["network"] == "dedicated" else "direct",
    )
    if transport not in {"direct", "tailnet"}:
        raise ValueError("Invalid SSH transport")
    lines = [
        "Host hermes-terminal",
        f"  HostName {target}",
        f"  Port {port}",
        "  HostKeyAlias hermes-terminal",
        "  StrictHostKeyChecking yes",
        "  IdentitiesOnly yes",
        f"  UserKnownHostsFile {home / '.ssh/known_hosts'}",
    ]
    if transport == "tailnet":
        lines.append(
            "  ProxyCommand /usr/bin/ncat --proxy-type socks5 --proxy 127.0.0.1:1055 %h %p"
        )
    return "\n".join(lines) + "\n"


def ensure_home_alias(home):
    """Keep legacy HOME/.hermes lookups valid when HOME is the PVC state root."""
    if Path.home().resolve() != home.resolve():
        return
    alias = home / ".hermes"
    if not alias.exists() and not alias.is_symlink():
        alias.symlink_to(".", target_is_directory=True)
    if not alias.is_symlink() or os.readlink(alias) != ".":
        raise ValueError("HOME/.hermes conflicts with the canonical Hermes state")


def prepare_configuration(home, root, inherited=None, *, instance=None):
    config_path = home / "config.yaml"
    configuration = yaml.safe_load(config_path.read_text())
    if not isinstance(configuration, dict):
        raise ValueError("config.yaml must contain a YAML object")
    state = home / "state"
    state.mkdir(exist_ok=True)
    bridge_path = state / "hermes-ssh-mcp.json"
    previous = json.loads(bridge_path.read_text()) if bridge_path.exists() else {}
    # A profile cloned from default carries its bridge commands. Import only
    # those original definitions; direct profile definitions remain independent.
    for name, server in configuration.get("mcp_servers", {}).items():
        if is_bridge(server):
            if name not in previous and name in (inherited or {}):
                previous[name] = inherited[name]
    routed = restore_mcp(configuration, previous)
    if instance is not None:
        routed = ssh_workspace_configuration(routed, home, root, instance)
    if routed != configuration:
        from hermes_cli.config import atomic_config_replace

        atomic_config_replace(config_path, routed)
    return routed.get("mcp_servers", {})


def prepare(
    home,
    bundle=Path("/opt/hermes-personal"),
    policy=Path("/etc/hermes-policy"),
    ssh_secret=Path("/opt/hermes-ssh"),
    source=Path("/opt/hermes"),
    mcp_secret=Path("/opt/hermes-mcp-ssh"),
):
    home.mkdir(parents=True, exist_ok=True)
    state = home / "state"
    state.mkdir(exist_ok=True)
    with (state / "hermes-bootstrap.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ensure_home_alias(home)
        from hermes_kubernetes.installation.environment import reconcile

        reconcile(home, policy)
        separate_env_files(home)
        instance = json.loads((policy / "instance.json").read_text())
        config_path = home / "config.yaml"
        if not config_path.exists():
            initial = yaml.safe_load((bundle / "config/hermes.yaml").read_text())
            initial["model"]["default"] = instance["model"]
            initial["model"]["base_url"] = instance.get("proxy", {}).get(
                "base_url", initial["model"]["base_url"]
            )
            initial["timezone"] = instance["timezone"]
            initial["terminal"]["cwd"] = instance.get("workspace", initial["terminal"]["cwd"])
            atomic_write(
                config_path,
                yaml.safe_dump(initial, allow_unicode=True, sort_keys=False),
            )
        soul = policy / "SOUL.md"
        if soul.is_file() and not (home / "SOUL.md").exists():
            atomic_write(home / "SOUL.md", soul.read_text())
        definitions = prepare_configuration(home, home, instance=instance)
        profiles = home / "profiles"
        if profiles.is_dir():
            for profile in sorted(profiles.iterdir()):
                if profile.name.startswith(".") or not profile.is_dir() or profile.is_symlink():
                    continue
                if (profile / "config.yaml").is_file():
                    prepare_configuration(profile, home, definitions, instance=instance)
        ssh_dir = home / ".ssh"
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        ssh_dir.chmod(0o700)
        for filename in ("id_ed25519", "known_hosts"):
            atomic_write(ssh_dir / filename, (ssh_secret / filename).read_text())
        if instance.get("mcp") is not None:
            private = ssh_dir / "mcp"
            private.mkdir(mode=0o700, exist_ok=True)
            private.chmod(0o700)
            for filename in ("id_ed25519", "known_hosts"):
                atomic_write(private / filename, (mcp_secret / filename).read_text())
        atomic_write(ssh_dir / "config", ssh_configuration(instance, home))
        for directory in ("skills", "memories", "logs"):
            (home / directory).mkdir(exist_ok=True)
        from hermes_kubernetes.state.skills import installed_update

        skill_update = installed_update(home, source, bundle, allow_conflicts=True)
        if not skill_update["ready"]:
            print(
                "Managed skill update pending: "
                f"{len(skill_update['conflicts'])} conflict(s); see "
                "state/versioning/managed-skills/conflicts.json",
                file=sys.stderr,
            )
        provenance = json.loads((source / "source-provenance.json").read_text())
        provenance["instance"] = instance["name"]
        atomic_write(state / "hermes-provenance.json", json.dumps(provenance))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "gateway"
    if mode not in {"gateway", "dashboard"}:
        raise ValueError("Allowed command: gateway or dashboard")
    home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    prepare(home)
    from hermes_kubernetes.runtime.environment_hint import apply as apply_environment_hint
    from hermes_kubernetes.runtime.write_roots import apply as apply_write_roots

    apply_write_roots(os.environ)
    apply_environment_hint(os.environ, home)
    from hermes_kubernetes.runtime.dependencies import prepare_store

    os.environ["HERMES_RUNTIME_DIR"] = str(prepare_store(home))
    from hermes_cli._launchers import runtime_command

    args = []
    if mode == "gateway":
        args += ["gateway", "run", "--external-supervisor"]
    else:
        args += ["dashboard", "--host", "0.0.0.0", "--port", "9119", "--no-open"]
    # Docker's verified base venv carries the initial dependencies. Bootstrap
    # overlays a committed PM generation when one exists; a bare store Python
    # has no dependency selection on the first boot of an empty PVC.
    command = runtime_command(Path("/opt/hermes"), args, home=home, python=sys.executable)
    os.execv(command[0], command)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Hermes bootstrap failed: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)
