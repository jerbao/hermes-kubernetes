"""Let the file tools write to the terminal's work folders, not only to the pod's state.

The upstream image sets HERMES_WRITE_SAFE_ROOT=/opt/data, which suits a single container.
Here the file tools write over SSH to a separate terminal where /opt/data does not exist, so
every workspace write is denied. Unsetting the variable is not an option: the credential
denylist is built from the pod's homes, so the terminal's ~/.ssh would become writable.
Instead, append the installation's ``write_roots`` (terminal paths) to the existing roots.
"""

import json
import os
import sys
from pathlib import Path, PurePosixPath

ROOT_VARIABLE = "HERMES_WRITE_SAFE_ROOT"


def roots(instance):
    """Return the validated absolute terminal paths listed in ``instance["write_roots"]``."""
    configured = instance.get("write_roots", [])
    if not isinstance(configured, list):
        raise ValueError("write_roots must be a list")
    for root in configured:
        path = PurePosixPath(root) if isinstance(root, str) else None
        if (
            path is None
            or not path.is_absolute()
            or path == PurePosixPath("/")
            or ".." in path.parts
        ):
            raise ValueError("write_roots must contain absolute paths other than /")
        if ".ssh" in path.parts:
            raise ValueError("write_roots must not include credential directories")
    return configured


def apply(environ, policy=Path("/etc/hermes-policy")):
    """Append the configured terminal roots, keeping the roots already set.

    An unset or empty variable means writes are unrestricted; leave that choice alone.
    ponytail: realpath runs in the pod, not on the terminal, so a symlink inside a root that
    points to ~/.ssh is not caught; resolving on the terminal needs an SSH round trip per write.
    """
    current = [root for root in environ.get(ROOT_VARIABLE, "").split(os.pathsep) if root]
    if not current:
        return
    try:
        extra = roots(json.loads((policy / "instance.json").read_text()))
    except (OSError, ValueError) as error:
        print(f"Write roots skipped: {type(error).__name__}", file=sys.stderr)
        return
    environ[ROOT_VARIABLE] = os.pathsep.join(dict.fromkeys(current + extra))
