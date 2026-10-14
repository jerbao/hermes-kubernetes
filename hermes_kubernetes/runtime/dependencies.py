"""Seed PM's writable tool store from verified tools already shipped in the image."""

import fcntl
import json
import os
from pathlib import Path
import tempfile


def prepare_store(home, packaged=Path("/opt/hermes/tools")):
    store = home / "tools"
    store.mkdir(mode=0o700, exist_ok=True)
    with (store / ".seed.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        baseline = packaged.parent / "enabled-features.json"
        selected = home / baseline.name
        if baseline.is_file() and not selected.exists():
            features = json.loads(baseline.read_text())
            if (
                features.get("schema") != 1
                or not isinstance(features.get("extras"), list)
                or any(not isinstance(extra, str) for extra in features["extras"])
            ):
                raise ValueError("Invalid packaged feature baseline")
            with tempfile.NamedTemporaryFile(mode="w", dir=home, delete=False) as handle:
                json.dump(features, handle)
                temporary = Path(handle.name)
            temporary.chmod(0o600)
            temporary.replace(selected)
        source = json.loads((packaged / "facts.json").read_text())
        target = store / "facts.json"
        facts = (
            json.loads(target.read_text())
            if target.exists()
            else {"schema": source["schema"], "packages": {}}
        )
        for name, record in source["packages"].items():
            if "entry" not in record:
                # State records (the image's venv selection) carry no files to link.
                extras = record.get("extras")
                if (
                    not isinstance(record.get("stamp"), str)
                    or not isinstance(extras, list)
                    or any(not isinstance(extra, str) for extra in extras)
                ):
                    raise ValueError("Invalid packaged state record")
                facts["packages"].setdefault(name, record)
                continue
            entry = record["entry"]
            if not entry or Path(entry).name != entry or not (packaged / entry).is_dir():
                raise ValueError("Invalid packaged tool record")
            destination = store / entry
            if not destination.exists() and not destination.is_symlink():
                destination.symlink_to(packaged / entry, target_is_directory=True)
            # Existing PM selections belong to PM, including upgrades and repairs.
            facts["packages"].setdefault(name, record)
        with tempfile.NamedTemporaryFile(mode="w", dir=store, delete=False) as handle:
            json.dump(facts, handle)
            temporary = Path(handle.name)
        temporary.chmod(0o600)
        temporary.replace(target)
    return store
