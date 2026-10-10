"""Reconcile installer secrets into default only; retain independent native profile files."""

import hashlib
import json
from pathlib import Path


def reconcile(home, policy):
    initial = Path(policy) / "initial.env"
    if not initial.is_file():
        return
    from agent.secret_scope import load_env_file
    from hermes_kubernetes.installation.files import write_private

    home = Path(home).resolve()
    environment = home / ".env"
    if environment.is_symlink() or environment.exists() and environment.stat().st_nlink > 1:
        from hermes_kubernetes.state.prepare import separate_env_files

        separate_env_files(home)
    baseline = home / "state/installer-environment.json"
    previous = json.loads(baseline.read_text()) if baseline.exists() else {}
    desired, current = load_env_file(initial), load_env_file(home / ".env")
    checksum = lambda value: hashlib.sha256(value.encode()).hexdigest()
    hashes = {key: checksum(value) for key, value in desired.items()}
    candidate = dict(current)
    for key in previous.keys() | desired.keys():
        old, new = previous.get(key), hashes.get(key)
        actual = checksum(current[key]) if key in current else None
        if old == new:
            continue  # GUI additions, edits and removals remain authoritative.
        if key not in current:
            if key in desired:
                candidate[key] = desired[key]
        elif old is None:
            continue  # Preserve pre-existing native credentials on first adoption.
        elif actual == new:
            continue
        elif actual != old:
            raise ValueError(
                "Installer/native credential conflict for "
                + key
                + "; align the installer and default profile before retrying"
            )
        elif key in desired:
            candidate[key] = desired[key]
        else:
            candidate.pop(key, None)
    if candidate != current or not (home / ".env").exists():
        write_private(
            home / ".env",
            "".join(key + "=" + json.dumps(value) + "\n" for key, value in candidate.items()),
        )
    write_private(baseline, json.dumps(hashes))
