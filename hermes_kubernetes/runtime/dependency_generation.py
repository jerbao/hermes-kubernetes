"""Rebase writable plugin environments when a new image changes locked core packages."""

import json
import logging
from pathlib import Path
import tomllib

logger = logging.getLogger(__name__)


def core_drift(source, facts_path):
    """Compare resolved package versions; plugin-only additions do not constitute drift."""
    if not facts_path.is_file():
        return []
    fact = json.loads(facts_path.read_text()).get("packages", {}).get("venv", {})
    if not fact.get("environment"):
        return []  # No writable generation overrides the packaged environment.
    recipe = Path(fact["resolved_lock"])
    generations = facts_path.parent / "environments"
    if not recipe.resolve().is_relative_to(generations.resolve()):
        raise ValueError("Dependency recipe is outside the install generations")

    def versions(path):
        result = {}
        for package in tomllib.loads(path.read_text()).get("package", []):
            if "version" in package:
                result.setdefault(package["name"], set()).add(package["version"])
        return result

    packaged, selected = versions(source / "uv.lock"), versions(recipe)
    return sorted(
        name
        for name in packaged.keys() & selected.keys()
        if not selected[name].issubset(packaged[name])
    )


def reconcile(source=Path("/opt/hermes"), *, facts_path=None, synchronize=None):
    """Use PM's atomic transaction, preserving extras/plugins and keeping failures visible."""
    if facts_path is None:
        from pm.environments import runtime_facts_path

        facts_path = runtime_facts_path(source)
    changed = core_drift(source, facts_path)
    if not changed:
        return []
    logger.warning(
        "Rebuilding writable dependency generation for image core changes: %s", ", ".join(changed)
    )
    if synchronize is None:
        from pm import sync_venv

        synchronize = sync_venv
    # No repair/eviction and no replacement feature list: PM retains the installed selection.
    synchronize(explicit=True)
    if core_drift(source, facts_path):
        raise RuntimeError("Writable dependencies still differ from the image core lock")
    logger.info("Writable dependencies now match the image core lock")
    return changed
