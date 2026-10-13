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
        if not isinstance(root, str) or os.pathsep in root or not _inside_home(PurePosixPath(root)):
            raise ValueError(
                "write_roots must be folders inside a terminal home, such as "
                "/home/USER/code-repos; the home itself and its dot folders hold credentials"
            )
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


def _inside_home(path):
    """True for /home/USER/NAME/... or /root/NAME/... where NAME is not a dot folder.

    The guard builds its credential denylist from the pod's homes, not the terminal's,
    so a root covering a terminal home would expose ~/.ssh and ~/.config/gh. Allowing
    only plain subfolders of a home keeps them out without copying the upstream list.
    """
    parts = path.parts
    if not path.is_absolute() or ".." in parts or ".ssh" in parts:
        return False
    base = 3 if parts[1:2] == ("home",) else 2 if parts[1:2] == ("root",) else None
    return base is not None and len(parts) > base and not parts[base].startswith(".")
