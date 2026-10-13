"""Describe this installation's topology to the agent through HERMES_ENVIRONMENT_HINT.

Upstream Hermes tells the model that file and shell tools run on a remote backend, but it cannot
know that the gateway lives in a pod, that MCP servers live in other pods, or what is writable.
The facts come from the installation's own ``instance.json``, so each deployment describes itself.
"""

import json
import sys
from pathlib import Path

HINT_VARIABLE = "HERMES_ENVIRONMENT_HINT"


def build(instance, home):
    """Return the topology text for *instance* (parsed ``instance.json``) and the state root *home*."""
    user = instance.get("ssh_user", "the terminal user")
    workspace = instance.get("workspace", "~/.hermes")
    lines = [
        "Installation topology. This is NOT stock Hermes; verify it with "
        "`python3 hermes.py runtime info` in the terminal.",
        f"- The gateway and agent run in a Kubernetes pod. Live state (HERMES_HOME) is a PVC "
        f"mounted at {home}; core code is at /opt/hermes. Neither path exists on the terminal.",
        f"- Shell commands run on a separate SSH terminal (alias hermes-terminal, user {user}). "
        f"Its {workspace} is a Git checkout of the private instance repository, not the pod's "
        "HERMES_HOME. Equal path names do not mean shared files.",
    ]
    mcp = instance.get("mcp")
    if mcp is None:
        lines.append("- No isolated MCP executor is configured for this installation.")
    else:
        targets = mcp.get("executor_targets") or {}
        workspace_target = mcp.get("workspace_target")
        roles = sorted(targets) + (["workspace"] if workspace_target else [])
        line = (
            f"- MCP servers run in separate executor pods ({', '.join(roles) or 'none configured'}), "
            f"reached over SSH from the gateway. They cannot see {home}."
        )
        roots = (workspace_target or {}).get("roots")
        if roots:
            line += f" The workspace executor mounts only: {', '.join(roots)}."
        lines.append(line)
    lines += [
        "- The pod is never edited in place: a change goes through Git, then a new pod rolls out. "
        "Live configuration goes through `python3 hermes.py runtime exec -- hermes --profile NAME "
        "config set KEY VALUE` or the Desktop API. The terminal's config.yaml is a read-only projection.",
    ]
    write_roots = instance.get("write_roots") or []
    lines.append(
        f"- File tools (write_file, patch) may write only under {home}"
        + (f" and these terminal folders: {', '.join(write_roots)}." if write_roots else ".")
        + " A write outside them is denied by HERMES_WRITE_SAFE_ROOT: report it and ask where the "
        "file should go instead of retrying through the terminal or another tool."
    )
    return "\n".join(lines)


def apply(environ, home, policy=Path("/etc/hermes-policy")):
    """Export the hint unless the operator already set one.

    Fail-open: a hint must never keep the pod from booting, so a missing or malformed
    instance.json only logs a warning (bootstrap.prepare already validates the file).
    """
    if environ.get(HINT_VARIABLE, "").strip():
        return
    try:
        environ[HINT_VARIABLE] = build(json.loads((policy / "instance.json").read_text()), home)
    except (OSError, ValueError, AttributeError, TypeError) as error:
        print(f"Environment hint skipped: {type(error).__name__}", file=sys.stderr)
