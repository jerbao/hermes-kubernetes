"""Rebase installed skills onto managed templates without publishing conflicts."""

import base64
import fcntl
import gzip
import hashlib
import json
import re
from pathlib import Path
import stat
import subprocess
import tempfile


def snapshot(directory, prefix="skills", names=None):
    directory = Path(directory)
    result = {}
    if not directory.is_dir():
        return result
    roots = sorted(p.parent for p in directory.rglob("SKILL.md") if not p.is_symlink())
    for root in roots:
        if names is not None and root.relative_to(directory).parts[0] not in names:
            continue
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError("Symlink in managed skill: " + str(path))
            if not path.is_file() or any(
                part in {".git", ".venv", "__pycache__", "node_modules"}
                for part in path.relative_to(directory).parts
            ):
                continue
            data = path.read_bytes()
            if len(data) > 8 * 1024 * 1024:
                raise ValueError("Managed skill file exceeds 8 MiB")
            name = str(Path(prefix) / path.relative_to(directory))
            result[name] = {
                "data": base64.b64encode(data).decode(),
                "mode": 0o700 if path.stat().st_mode & 0o111 else 0o600,
            }
    return result


def content(item):
    return base64.b64decode(item["data"], validate=True) if item is not None else None


def group(name, templates):
    path = Path(name)
    for parent in [path.parent, *path.parents]:
        if (parent / "SKILL.md").as_posix() in templates:
            return parent.as_posix()
    return None


def merge(base, wanted, current):
    if current == wanted or wanted == base:
        return current
    if current == base:
        return wanted
    if any(value is None or b"\0" in value for value in (base, wanted, current)):
        raise ValueError("Conflicting creation, deletion or binary edit")
    with tempfile.TemporaryDirectory(prefix="hermes-skill-merge-") as temporary:
        paths = [Path(temporary) / name for name in ["current", "base", "wanted"]]
        for path, data in zip(paths, [current, base, wanted]):
            path.write_bytes(data)
        result = subprocess.run(
            ["git", "merge-file", "-p", *map(str, paths)],
            capture_output=True,
            timeout=30,
        )
    if result.returncode:
        raise ValueError("Conflicting skill edits")
    return result.stdout


def plan(previous, desired, installed, distribution=(), new_profile=False, initializing=False):
    """Return a complete plan. A conflict means none of its changes may be applied."""
    templates = {**previous, **desired}
    roots = {group(name, templates) for name in templates}
    changes, conflicts = {}, []
    for root in sorted(roots - {None}):
        marker = root + "/SKILL.md"
        # Preserve withdrawn skills as personal skills rather than dropping a capability.
        if marker not in desired:
            old_name = re.search(
                rb'^name:\s*["\']?([^\n"\']+)',
                content(previous.get(marker)) or b"",
                re.M,
            )
            if (
                marker in installed
                and old_name
                and any(
                    name.endswith("/SKILL.md")
                    and name != marker
                    and re.search(rb'^name:\s*["\']?([^\n"\']+)', content(item), re.M)
                    and re.search(rb'^name:\s*["\']?([^\n"\']+)', content(item), re.M)[1]
                    == old_name[1]
                    for name, item in desired.items()
                )
            ):
                conflicts.append(marker)  # A moved skill needs an explicit migration.
            continue
        managed_distribution = marker in distribution
        present = marker in installed
        added = marker not in previous
        if not present and not (managed_distribution and (new_profile or added)):
            continue  # A removed/never-installed official skill stays removed.
        for name in sorted(n for n in templates if group(n, templates) == root):
            old, new, actual = (
                previous.get(name),
                desired.get(name),
                installed.get(name),
            )
            if managed_distribution and new_profile and actual is None:
                old = None
            if initializing and actual is not None:
                continue  # Existing installations need a recorded base before rebasing.
            try:
                data = merge(content(old), content(new), content(actual))
                old_mode = old["mode"] if old else None
                new_mode = new["mode"] if new else None
                actual_mode = actual["mode"] if actual else None
                if actual_mode == new_mode or old_mode == new_mode:
                    mode = actual_mode
                elif actual_mode == old_mode:
                    mode = new_mode
                else:
                    raise ValueError("Conflicting executable mode")
                result = (
                    {"data": base64.b64encode(data).decode(), "mode": mode or 0o600}
                    if data is not None
                    else None
                )
                if result != actual:
                    changes[name] = {"expected": actual, "value": result}
            except ValueError:
                conflicts.append(name)
    return {"ready": not conflicts, "changes": changes, "conflicts": conflicts}


def distribution_digest(directory, names=None):
    return hashlib.sha256(
        json.dumps(snapshot(directory, names=names), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def installed_update(
    home,
    source=Path("/opt/hermes"),
    bundle=Path("/opt/hermes-personal"),
    *,
    seed_only=False,
    distribution_baseline=None,
):
    """Prepare every profile before writing any skill; retain a private source baseline."""
    from hermes_kubernetes.state.versioning import atomic, dump, profiles

    home, source, bundle = map(lambda p: Path(p).resolve(), [home, source, bundle])
    directory = home / "state/versioning/managed-skills"
    if any(p.is_symlink() for p in [directory, *directory.parents]):
        raise ValueError("Symlink in skill baseline")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        state = directory / "bases.json.gz"
        old = json.loads(gzip.decompress(state.read_bytes())) if state.exists() else {}
        provenance = source / "source-provenance.json"
        identity = (
            hashlib.sha256(provenance.read_bytes()).hexdigest() if provenance.exists() else None
        )
        if distribution_baseline is not None and not seed_only:
            raise ValueError("A distribution baseline is only accepted during initial recording")
        distribution = (
            distribution_baseline
            if distribution_baseline is not None
            else snapshot(bundle / "skills")
        )
        if old and seed_only:
            return {
                "ready": True,
                "conflicts": [],
                "profiles": old.get("profiles", []),
                "updated_files": 0,
                "model_calls": 0,
                "already_recorded": True,
            }
        if identity and identity == old.get("source_identity"):
            official = old["official"]
        else:
            official = snapshot(source / "skills")
        collisions = official.keys() & distribution.keys()
        if collisions:
            raise ValueError("Distribution shadows official skill paths")
        desired = {**official, **distribution}
        previous = {**old.get("official", {}), **old.get("distribution", {})}
        roots = {
            name: root for name, root in profiles(home).items() if (root / "config.yaml").is_file()
        }
        updates, conflicts = [], []
        for profile, root in roots.items():
            installed = {}
            for name in set(previous) | set(desired):
                path = root / name
                if any(p.is_symlink() for p in [path, *path.parents]):
                    raise ValueError("Symlink in installed managed skill")
                if path.is_file():
                    installed[name] = {
                        "data": base64.b64encode(path.read_bytes()).decode(),
                        "mode": 0o700 if path.stat().st_mode & 0o111 else 0o600,
                    }
            result = plan(
                previous,
                desired,
                installed,
                distribution,
                new_profile=profile not in old.get("profiles", []),
                initializing=not old,
            )
            conflicts += [profile + "/" + name for name in result["conflicts"]]
            for name, change in result["changes"].items():
                updates.append((root / name, change))
        report = {
            "ready": not conflicts,
            "conflicts": conflicts,
            "profiles": sorted(roots),
            "updated_files": 0,
            "model_calls": 0,
        }
        conflict_path = directory / "conflicts.json"
        if conflicts:
            atomic(conflict_path, dump(report))
            raise ValueError(
                "Managed skill update blocked; see state/versioning/managed-skills/conflicts.json"
            )
        if not seed_only:
            # Check every preimage before the first write, then check again per file.
            for path, change in updates:
                if any(p.is_symlink() for p in [path, *path.parents]):
                    raise ValueError("Symlink in installed skill")
                actual = path.read_bytes() if path.exists() else None
                if actual != content(change["expected"]):
                    raise ValueError("Concurrent skill edit: " + str(path))
            written = []
            try:
                for path, change in updates:
                    expected = content(change["expected"])
                    if (path.read_bytes() if path.exists() else None) != expected:
                        raise ValueError("Concurrent skill edit: " + str(path))
                    before_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
                    if expected is not None:
                        saved = directory / "history" / hashlib.sha256(expected).hexdigest()
                        if not saved.exists():
                            atomic(saved, expected, before_mode)
                    value = content(change["value"])
                    if value is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic(path, value, change["value"]["mode"])
                    written.append((path, expected, before_mode, value))
            except BaseException:
                for path, before, mode, after in reversed(written):
                    if (
                        not path.is_symlink()
                        and (path.read_bytes() if path.exists() else None) == after
                    ):
                        if before is None:
                            path.unlink(missing_ok=True)
                        else:
                            atomic(path, before, mode)
                raise
            report["updated_files"] = len(written)
        record = {
            "schema": 1,
            "source_identity": identity,
            "official": official,
            "distribution": distribution,
            "profiles": sorted(roots),
        }
        data = gzip.compress(dump(record), mtime=0)
        if not state.exists() or state.read_bytes() != data:
            atomic(state, data)
        conflict_path.unlink(missing_ok=True)
        return report
