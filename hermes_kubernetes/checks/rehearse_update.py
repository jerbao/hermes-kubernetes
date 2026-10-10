"""Rehearse a candidate image against a private copy of a saved Hermes home.

The input home or archive is never mounted into Docker. The candidate container
gets only a temporary, private copy with messaging adapters disabled and no
network. Rollback evidence always names both the pre-update snapshot and the
previous image digest.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

import yaml

from hermes_kubernetes.backup.state import MANIFEST


class RehearsalRejected(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def reject_active_home(home: Path, *, environ: dict | None = None) -> None:
    env = os.environ if environ is None else environ
    resolved = home.resolve()
    protected = (
        {Path(env["HERMES_HOME"]).expanduser().resolve()} if env.get("HERMES_HOME") else set()
    )
    protected.add((Path.home() / ".hermes").resolve())
    if resolved in protected:
        raise RehearsalRejected(
            "Refusing active HERMES_HOME; provide an offline snapshot or a separate copy"
        )
    for pid_file in (home / "gateway.pid", home / "gateway" / "gateway.pid"):
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
        except (OSError, ValueError):
            continue
        raise RehearsalRejected(f"Refusing a home with a live gateway PID ({pid})")
    databases = [home / "state.db"]
    profiles = home / "profiles"
    if profiles.is_dir():
        databases.extend(profiles.glob("*/state.db"))
    for database in databases:
        if not database.exists():
            continue
        try:
            result = subprocess.run(
                ["lsof", "-t", str(database)], capture_output=True, text=True, timeout=5
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.stdout.strip():
            raise RehearsalRejected(
                f"Refusing a home with an open SQLite database: {database.name}"
            )
    for scope in ([], ["--user"]):
        try:
            result = subprocess.run(
                ["systemctl", *scope, "is-active", "hermes-gateway.service"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.stdout.strip() in {"active", "activating", "deactivating"}:
            raise RehearsalRejected("Refusing rehearsal while hermes-gateway.service is active")


def snapshot_home(home: Path, archive: Path) -> None:
    """Use the project's bounded, SQLite-aware state snapshot implementation."""
    from hermes_kubernetes.backup.state import build_snapshot

    build_snapshot(home, archive)


def extract_snapshot(archive_path: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                raise RehearsalRejected("Snapshot contains an unsafe path")
            if not (member.isfile() or member.isdir()):
                raise RehearsalRejected("Snapshot must contain only regular files and directories")
            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise RehearsalRejected("Snapshot path escapes the private copy")
            if member.isdir():
                target.mkdir(mode=member.mode & 0o700 or 0o700, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise RehearsalRejected("Snapshot file has no data")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(target, flags, member.mode & 0o600 or 0o600)
            with os.fdopen(descriptor, "wb") as output:
                shutil.copyfileobj(source, output)
        manifest = destination / MANIFEST
        if not manifest.is_file():
            raise RehearsalRejected("Snapshot is missing the required native manifest")
        value = json.loads(manifest.read_text())
        if (
            value.get("schema") != 1
            or value.get("consistency") != "sqlite-per-database"
            or not isinstance(value.get("started_at"), str)
            or not isinstance(value.get("completed_at"), str)
            or not isinstance(value.get("files"), list)
        ):
            raise RehearsalRejected("Snapshot manifest does not match the native schema 1 format")
        declared: set[str] = set()
        for entry in value["files"]:
            if (
                not isinstance(entry, dict)
                or not {"path", "size", "sha256", "sqlite"} <= entry.keys()
                or not isinstance(entry["path"], str)
                or not isinstance(entry["size"], int)
                or isinstance(entry["size"], bool)
                or entry["size"] < 0
                or not isinstance(entry["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                or not isinstance(entry["sqlite"], bool)
            ):
                raise RehearsalRejected("Snapshot manifest has an invalid file entry")
            name = entry["path"]
            relative = PurePosixPath(name)
            if (
                relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
                or name in declared
            ):
                raise RehearsalRejected("Snapshot manifest contains an unsafe or duplicate path")
            declared.add(name)
            path = destination.joinpath(*relative.parts)
            if (
                not path.resolve().is_relative_to(destination.resolve())
                or not path.is_file()
                or path.stat().st_size != entry["size"]
                or sha256(path) != entry["sha256"]
            ):
                raise RehearsalRejected(f"Snapshot manifest verification failed: {name}")
            with path.open("rb") as stream:
                is_sqlite = stream.read(16) == b"SQLite format 3\x00"
            if bool(entry["sqlite"]) != is_sqlite:
                raise RehearsalRejected(f"Snapshot manifest SQLite marker mismatch: {name}")
        actual = {
            str(path.relative_to(destination)) for path in destination.rglob("*") if path.is_file()
        }
        actual.discard(MANIFEST)
        if declared != actual:
            raise RehearsalRejected(
                "Snapshot manifest does not cover exactly the archived state files"
            )


_NATIVE_PLATFORM_ROSTER = r"""
import json, os
from pathlib import Path

from gateway.config import Platform
from gateway.config_loader import load_legacy_gateway_json, merge_platform_sections, read_yaml_layers
from gateway.platform_registry import platform_registry
from hermes_cli.plugins import discover_plugins

root = Path('/opt/data')
homes = [root]
profiles = root / 'profiles'
if profiles.is_dir():
    homes.extend(sorted(p for p in profiles.iterdir() if p.is_dir() and p.name != 'default'))

def platform_name(value):
    try:
        return Platform(str(value)).value
    except (TypeError, ValueError):
        return None

def add_mapping_names(names, mapping):
    if isinstance(mapping, dict):
        names.update(str(key) for key in mapping if platform_name(key))

evidence = {}
for home in homes:
    os.environ['HERMES_HOME'] = str(home)
    os.environ.pop('HERMES_PROFILE', None)
    discover_plugins(force=True)
    legacy = load_legacy_gateway_json(home)
    config = read_yaml_layers(home)
    gateway = config.get('gateway')
    gateway = gateway if isinstance(gateway, dict) else {}
    names = {member.value for member in Platform.__members__.values()}
    names.update(entry.name for entry in platform_registry.all_entries())
    bundled, aliases = Platform._scan_bundled_plugin_platforms()
    names.update(bundled)
    names.update(aliases)
    merged = merge_platform_sections(config, gateway, legacy)
    add_mapping_names(names, merged)
    add_mapping_names(names, legacy.get('platforms'))
    add_mapping_names(names, config.get('platforms'))
    add_mapping_names(names, gateway.get('platforms'))
    for source in (config, gateway, legacy, legacy.get('gateway')):
        if isinstance(source, dict):
            for key, value in source.items():
                if isinstance(value, dict):
                    name = platform_name(key)
                    if name:
                        names.add(name)
    evidence[str(home.relative_to(root)) or '.'] = sorted(names)

print('HERMES_PLATFORM_ROSTER=' + json.dumps(evidence, sort_keys=True))
"""


_NATIVE_VERIFY_CHANNELS = r"""
import json, os, re
from pathlib import Path
from gateway.config import PLATFORM_TOKEN_ENV_NAMES, load_gateway_config
from gateway.config_env import _ENV_ENABLE_CREDENTIALS
from gateway.platform_registry import platform_registry
from hermes_cli.plugins import discover_plugins

root = Path('/opt/data')
homes = [root]
profiles = root / 'profiles'
if profiles.is_dir():
    homes.extend(sorted(p for p in profiles.iterdir() if p.is_dir() and p.name != 'default'))

def add_env_names(names, value):
    if isinstance(value, str):
        if re.fullmatch(r'[A-Z][A-Z0-9_]+', value):
            names.add(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            add_env_names(names, key)
            add_env_names(names, item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            add_env_names(names, item)

def synthetic_value(name):
    upper = name.upper()
    if upper.endswith('_URL') or upper.endswith('_URI'):
        return 'http://127.0.0.1:9/'
    if upper.endswith('_PORT'):
        return '9'
    if upper.endswith('_ENABLED'):
        return 'true'
    if upper.endswith('_ADDRESS') or upper.endswith('_EMAIL'):
        return 'hermes-rehearsal@example.invalid'
    return 'rehearsal-synthetic-token'

evidence = {}
for home in homes:
    os.environ['HERMES_HOME'] = str(home)
    os.environ.pop('HERMES_PROFILE', None)
    discover_plugins(force=True)
    env_names = set(PLATFORM_TOKEN_ENV_NAMES.values())
    for values in _ENV_ENABLE_CREDENTIALS.values():
        add_env_names(env_names, values)
    for entry in platform_registry.plugin_entries():
        add_env_names(env_names, entry.required_env)
    # This variable activates the relay connector itself (and changes every adapter),
    # so a fake URL would test a configured external transport, not credential hooks.
    # Explicit false config entries above are the relevant disable invariant.
    os.environ.pop('GATEWAY_RELAY_URL', None)
    env_names.discard('GATEWAY_RELAY_URL')
    for variable in env_names:
        os.environ[variable] = synthetic_value(variable)
    loaded = load_gateway_config()
    enabled = sorted(platform.value for platform, cfg in loaded.platforms.items() if cfg.enabled)
    if enabled:
        raise RuntimeError('candidate config still enables platforms: ' + ', '.join(enabled))
    evidence[str(home.relative_to(root)) or '.'] = {
        'enabled_after_native_synthetic_env_probe': enabled,
        'synthetic_env_names_checked': len(env_names),
    }
print('HERMES_MESSAGING_DISABLED=' + json.dumps(evidence, sort_keys=True))
"""


def _native_candidate_json(image: str, home: Path, script: str, marker: str) -> dict:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--env",
            "HERMES_HOME=/opt/data",
            "--mount",
            f"type=bind,src={home},dst=/opt/data",
            "--entrypoint",
            "/opt/hermes/.venv/bin/python",
            image,
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode:
        exception = next(
            (
                match.group(1)
                for line in reversed(result.stderr.splitlines())
                if (match := re.match(r"^([A-Za-z_][A-Za-z0-9]*(?:Error|Exception)):\s", line))
            ),
            "NativeCheckError",
        )
        raise RehearsalRejected(f"Native candidate check failed: {exception}")
    line = next(
        (line[len(marker) :] for line in result.stdout.splitlines() if line.startswith(marker)),
        None,
    )
    try:
        evidence = json.loads(line) if line is not None else None
    except json.JSONDecodeError as error:
        raise RehearsalRejected(
            "Native candidate check returned invalid structured evidence"
        ) from error
    if not isinstance(evidence, dict):
        raise RehearsalRejected("Native candidate check returned no structured evidence")
    return evidence


def _disable_config_sources(home: Path, names: set[str]) -> None:
    config_path = home / "config.yaml"
    try:
        config = yaml.safe_load(config_path.read_text()) if config_path.is_file() else {}
    except yaml.YAMLError as error:
        raise RehearsalRejected("Invalid Hermes config YAML in private copy") from error
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise RehearsalRejected(f"Invalid Hermes config in private copy: {config_path.name}")
    gateway = config.setdefault("gateway", {})
    if not isinstance(gateway, dict):
        raise RehearsalRejected(f"Invalid gateway config in private copy: {config_path.name}")
    # relay_url is a connector control, not a platform credential; clear it so the
    # candidate cannot activate an external transport while probing a private copy.
    gateway.pop("relay_url", None)

    def disable_map(mapping: dict) -> dict:
        if not isinstance(mapping, dict):
            mapping = {}
        for key, block in list(mapping.items()):
            if isinstance(block, dict):
                block["enabled"] = False
        for name in names:
            block = mapping.get(name)
            if not isinstance(block, dict):
                block = {}
                mapping[name] = block
            block["enabled"] = False
        return mapping

    config["platforms"] = disable_map(config.get("platforms"))
    gateway["platforms"] = disable_map(gateway.get("platforms"))
    for name in names:
        if isinstance(gateway.get(name), dict):
            gateway[name]["enabled"] = False
        if isinstance(config.get(name), dict):
            config[name]["enabled"] = False
    _write_private_config(config_path, config, yaml_file=True)

    legacy_path = home / "gateway.json"
    if legacy_path.is_file():
        try:
            legacy = json.loads(legacy_path.read_text())
        except json.JSONDecodeError as error:
            raise RehearsalRejected("Invalid legacy gateway.json in private copy") from error
        if not isinstance(legacy, dict):
            raise RehearsalRejected("Invalid legacy gateway.json in private copy")
        legacy["platforms"] = disable_map(legacy.get("platforms"))
        legacy_gateway = legacy.get("gateway")
        if isinstance(legacy_gateway, dict):
            legacy_gateway.pop("relay_url", None)
            legacy_gateway["platforms"] = disable_map(legacy_gateway.get("platforms"))
            for name in names:
                if isinstance(legacy_gateway.get(name), dict):
                    legacy_gateway[name]["enabled"] = False
        for name in names:
            if isinstance(legacy.get(name), dict):
                legacy[name]["enabled"] = False
        _write_private_config(legacy_path, legacy, yaml_file=False)


def _write_private_config(path: Path, data: dict, *, yaml_file: bool) -> None:
    temporary = path.with_name(path.name + ".rehearsal-tmp")
    serialized = (
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        if yaml_file
        else json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    )
    temporary.write_text(serialized)
    temporary.chmod(0o600)
    os.replace(temporary, path)


def disable_messaging(image: str, home: Path) -> dict:
    roster = _native_candidate_json(image, home, _NATIVE_PLATFORM_ROSTER, "HERMES_PLATFORM_ROSTER=")
    all_names: set[str] = set()
    for relative, names in roster.items():
        if (
            not isinstance(relative, str)
            or not isinstance(names, list)
            or not all(isinstance(name, str) for name in names)
        ):
            raise RehearsalRejected("Native candidate platform roster is invalid")
        profile_home = home if relative == "." else home.joinpath(*PurePosixPath(relative).parts)
        if not profile_home.resolve().is_relative_to(home.resolve()) or not profile_home.is_dir():
            raise RehearsalRejected(
                "Native candidate platform roster contains an unsafe profile path"
            )
        profile_names = set(names)
        all_names.update(profile_names)
        _disable_config_sources(profile_home, profile_names)
    if not roster:
        raise RehearsalRejected("Native candidate platform roster is empty")
    # Validate every migrated config with the actual candidate loader after injecting
    # fake built-in/plugin credentials. Any enabled adapter aborts before migration/startup.
    verified = _native_candidate_json(
        image, home, _NATIVE_VERIFY_CHANNELS, "HERMES_MESSAGING_DISABLED="
    )
    if set(verified) != set(roster):
        raise RehearsalRejected("Native channel-disable verification missed a profile")
    for evidence in verified.values():
        if (
            not isinstance(evidence, dict)
            or evidence.get("enabled_after_native_synthetic_env_probe") != []
        ):
            raise RehearsalRejected(
                "Candidate gateway loader still enables a platform in the private copy"
            )
    return {
        profile: {"candidate_platform_count": len(roster[profile]), **verified[profile]}
        for profile in sorted(roster)
    }


def image_provenance(image: str) -> dict:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "/bin/cat",
            image,
            "/opt/hermes/source-provenance.json",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RehearsalRejected(f"Invalid image provenance for {image}")
    return value


def _sqlite_report(home: Path) -> dict:
    stores = {}
    for path in sorted(home.rglob("*")):
        if not path.is_file():
            continue
        try:
            with path.open("rb") as stream:
                if stream.read(16) != b"SQLite format 3\x00":
                    continue
            uri = path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=10) as database:
                result = database.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise RehearsalRejected(
                        f"SQLite integrity_check failed: {path.relative_to(home)}: {result}"
                    )
                version = None
                if database.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
                ).fetchone():
                    row = database.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
                    version = row[0] if row else None
                relative = str(path.relative_to(home))
                owner = _database_owner(relative)
                tables = [
                    row[0]
                    for row in database.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    )
                ]
                row_counts = {}
                for table in tables:
                    safe_table = table.replace('"', '""')
                    row_counts[table] = database.execute(
                        f'SELECT COUNT(*) FROM "{safe_table}"'
                    ).fetchone()[0]
                stores[relative] = {
                    "integrity": result,
                    "schema_version": version,
                    "native_owner": owner,
                    "tables": tables,
                    "row_counts": row_counts,
                    "migration_evidence": "native-owner-classified; integrity is not migration evidence",
                }
        except sqlite3.DatabaseError as error:
            raise RehearsalRejected(
                f"Could not inspect SQLite store {path.name}: {error}"
            ) from error
    return stores


def _database_owner(relative: str) -> str:
    if relative.endswith("/state.db") or relative == "state.db":
        return "hermes_state.SessionDB (explicitly opened by native migration step)"
    name = PurePosixPath(relative).name
    owners = {
        "executions.db": "cron.executions._connect + cron.incidents._connect (shared cron ledger)",
        "deliveries.db": "cron.delivery_queue._connect / gateway delivery worker",
        "notepad.db": "cron.notepad._connect / gateway cron notepad",
        "response_store.db": "gateway.platforms.api_server.ResponseStore",
        "runs_idempotency.db": "gateway.platforms.api_server_run_idempotency",
        "shared-state.db": "gateway.hosted_rooms / gateway.hosted_room_policy_checkpoint",
    }
    if name in owners:
        return owners[name] + "; startup access is reported separately, migration is not inferred"
    if name.startswith("kanban"):
        return (
            "gateway Kanban store; startup access is reported separately, migration is not inferred"
        )
    return "unclassified native store; migration is not inferred"


def _config_report(home: Path) -> dict:
    result = {}
    paths = [home / "config.yaml"]
    profiles = home / "profiles"
    if profiles.is_dir():
        paths.extend(path for path in profiles.glob("*/config.yaml") if path.is_file())
    for path in paths:
        if path.is_file():
            try:
                config = yaml.safe_load(path.read_text()) or {}
            except yaml.YAMLError as error:
                raise RehearsalRejected(
                    f"Invalid Hermes config YAML in private copy: {path.relative_to(home)}"
                ) from error
            if not isinstance(config, dict):
                raise RehearsalRejected(
                    f"Invalid Hermes config in private copy: {path.relative_to(home)}"
                )
            result[str(path.relative_to(home))] = config.get("_config_version")
    return result


def _run_container(
    image: str,
    copy: Path,
    policy: Path,
    secrets_dir: Path,
    *,
    readiness_timeout: float = 180.0,
) -> dict:
    name = f"hermes-update-rehearsal-{os.getpid()}"
    mount = f"type=bind,src={copy},dst=/opt/data"
    proc = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            "--env",
            "GATEWAY_ON_ALL_ADAPTERS_DOWN=stay_alive",
            "--mount",
            mount,
            "--mount",
            f"type=bind,src={policy},dst=/etc/hermes-policy,readonly",
            "--mount",
            f"type=bind,src={secrets_dir},dst=/opt/hermes-ssh,readonly",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=512m",
            image,
            "gateway",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        deadline = time.monotonic() + readiness_timeout
        last_probe = "no heartbeat"
        while time.monotonic() < deadline:
            state = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", name],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            if state != "true":
                raise RehearsalRejected(
                    "Candidate container exited before native gateway readiness"
                )
            probe = subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "python",
                    "-c",
                    "import json; from pathlib import Path; "
                    "from hermes_cli.gateway import probe_gateway_loop_liveness, GATEWAY_LOOP_ALIVE; "
                    "p=Path('/opt/data/state/gateway.heartbeat'); "
                    "v=json.loads(p.read_text()) if p.is_file() else {}; "
                    "verdict=probe_gateway_loop_liveness(int(v.get('pid', 0)), home=Path('/opt/data')); "
                    "print('ready' if verdict == GATEWAY_LOOP_ALIVE else 'not-ready')",
                ],
                capture_output=True,
                text=True,
                timeout=8,
            )
            last_probe = probe.stdout.strip() if probe.returncode == 0 else "probe error"
            if probe.returncode == 0 and last_probe == "ready":
                return {
                    "container_id": proc.stdout.strip(),
                    "readiness": "native-loop-tick",
                    "probe": "gateway_loop_alive",
                }
            time.sleep(2)
        else:
            raise RehearsalRejected(
                f"Native gateway loop did not become ready within {readiness_timeout:g}s (last probe: {last_probe})"
            )
    finally:
        subprocess.run(
            ["docker", "stop", "--time", "15", name],
            capture_output=True,
            text=True,
            timeout=25,
        )
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=30)


def _require_digest(value: str, name: str) -> str:
    digest = (
        value.split(":", 1)[1]
        if value.startswith("sha256:")
        else value.rsplit("@sha256:", 1)[1]
        if "@sha256:" in value
        else ""
    )
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise RehearsalRejected(f"{name} must be pinned to an image SHA-256 digest")
    return value


_SECRET_PATTERNS = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*"), r"\1<redacted>"),
    (
        re.compile(r"(?i)(api[_-]?key|token|password|secret)(\s*[=:]\s*)[^\s,;]+"),
        r"\1\2<redacted>",
    ),
    (re.compile(r"https?://[^/@\s]+:[^/@\s]+@"), "https://<redacted>@"),
)


def sanitize_error(value: str, private_paths: tuple[Path, ...] = ()) -> str:
    result = str(value).replace("\x00", "")[:1000]
    for path in private_paths:
        result = result.replace(str(path), "<PRIVATE_STATE>")
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return " ".join(result.split())


def _migrate_native(image: str, copy: Path) -> dict:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--mount",
            f"type=bind,src={copy},dst=/opt/data",
            "--entrypoint",
            "/opt/hermes/.venv/bin/python",
            image,
            "-c",
            "from pathlib import Path; "
            "from hermes_cli.config import migrate_config; migrate_config(interactive=False, quiet=True); "
            "from hermes_cli.update_cmd_config import _migrate_sibling_profile_configs; "
            "_migrate_sibling_profile_configs(); "
            "from hermes_cli.backup import _sibling_profile_homes; "
            "from hermes_constants import get_process_hermes_home, set_hermes_home_override, reset_hermes_home_override; "
            "from hermes_state import SessionDB; "
            "import json; "
            "home=Path(get_process_hermes_home()); homes=[home,*[p for _,p in _sibling_profile_homes(home)]]; "
            "opened=[str(p.relative_to(home)) for p in homes if (p/'state.db').is_file()]; "
            "[SessionDB(db_path=p/'state.db').close() for p in homes if (p/'state.db').is_file()]; "
            "cron_opened=[]\n"
            "for profile_home in homes:\n"
            " token=set_hermes_home_override(str(profile_home))\n"
            " try:\n"
            "  cron_dir=profile_home/'cron'\n"
            "  if (cron_dir/'executions.db').is_file():\n"
            "   from cron import executions, incidents\n"
            "   executions._connect().close(); incidents._connect().close()\n"
            "   database=str((cron_dir/'executions.db').relative_to(home))\n"
            "   cron_opened.extend([{'database':database,'owner':'cron.executions._connect'}, "
            " {'database':database,'owner':'cron.incidents._connect'}])\n"
            "  if (cron_dir/'deliveries.db').is_file():\n"
            "   from cron import delivery_queue\n"
            "   delivery_queue._connect().close()\n"
            "   cron_opened.append({'database':str((cron_dir/'deliveries.db').relative_to(home)),'owner':'cron.delivery_queue._connect'})\n"
            "  if (cron_dir/'notepad.db').is_file():\n"
            "   from cron import notepad\n"
            "   notepad._connect().close()\n"
            "   cron_opened.append({'database':str((cron_dir/'notepad.db').relative_to(home)),'owner':'cron.notepad._connect'})\n"
            " finally:\n"
            "  reset_hermes_home_override(token)\n"
            "print('HERMES_REHEARSAL_EVIDENCE='+json.dumps({'state_databases_opened_by': 'hermes_state.SessionDB', "
            "'profiles': opened, 'cron_databases_opened_by': cron_opened}))",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    marker = "HERMES_REHEARSAL_EVIDENCE="
    evidence_line = next(
        (line[len(marker) :] for line in result.stdout.splitlines() if line.startswith(marker)),
        None,
    )
    try:
        evidence = json.loads(evidence_line) if evidence_line is not None else None
    except json.JSONDecodeError as error:
        raise RehearsalRejected(
            "Native migration command returned invalid structured evidence"
        ) from error
    if not isinstance(evidence, dict):
        raise RehearsalRejected("Native migration command did not return structured evidence")
    return {
        "config_owner": "hermes_cli.config.migrate_config",
        "profile_config_owner": "hermes_cli.update_cmd_config._migrate_sibling_profile_configs",
        "cron_owners": "native owner open invoked for each pre-existing cron database",
        **evidence,
        "stderr": sanitize_error(result.stderr[-1000:], (copy,)),
    }


def _set_tree_owner(image: str, root: Path, owner: str) -> None:
    """Give container UID 10000 access, then restore host ownership for secure cleanup."""
    root.chmod(0o711)
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--mount",
            f"type=bind,src={root},dst=/private",
            "--entrypoint",
            "/bin/chown",
            image,
            "-R",
            owner,
            "/private",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def rehearse(
    *,
    previous_image: str,
    candidate_image: str,
    snapshot: Path | None = None,
    home: Path | None = None,
    offline: bool = False,
    report_path: Path,
    expected_upstream_commit: str | None = None,
    snapshot_output: Path | None = None,
) -> dict:
    previous_image = _require_digest(previous_image, "previous image")
    candidate_image = _require_digest(candidate_image, "candidate image")
    if (snapshot is None) == (home is None):
        raise RehearsalRejected("Provide exactly one of --snapshot or --home")
    if home is not None:
        home = home.resolve(strict=True)
        if not home.is_dir():
            raise RehearsalRejected("--home must name a directory")
        if not offline:
            raise RehearsalRejected(
                "--home requires --offline to assert a stopped, disconnected source"
            )
        reject_active_home(home)
        if snapshot_output is None:
            raise RehearsalRejected(
                "--home requires --snapshot-output so the rollback snapshot is retained"
            )
        snapshot_output = snapshot_output.resolve()
        if snapshot_output.is_relative_to(home.resolve()):
            raise RehearsalRejected("Rollback snapshot must be outside the source HERMES_HOME")
        if snapshot_output.exists() or snapshot_output.is_symlink():
            raise RehearsalRejected("Rollback snapshot target already exists; choose a new path")
        snapshot_output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif snapshot_output is not None:
        raise RehearsalRejected("--snapshot-output is used only with --home")
    if snapshot is not None:
        snapshot = snapshot.resolve(strict=True)
        if not snapshot.is_file():
            raise RehearsalRejected("Snapshot must be a regular file")
        input_snapshot = snapshot
        snapshot_digest = sha256(snapshot)
    elif home is None:
        input_snapshot = None
        snapshot_digest = None
    else:
        input_snapshot = snapshot_output
        snapshot_digest = None

    report_path = report_path.resolve()
    report_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hermes-rehearsal-") as temporary:
        private_root = Path(temporary)
        private_root.chmod(0o700)
        archive = input_snapshot or (private_root / "private-state-snapshot.tar.gz")
        if home is not None:
            snapshot_home(home.resolve(), archive)
            snapshot_digest = sha256(archive)
        private_home = private_root / "home"
        extract_snapshot(archive, private_home)
        messaging_evidence = disable_messaging(candidate_image, private_home)
        before_config = _config_report(private_home)
        before_sqlite = _sqlite_report(private_home)

        previous_provenance = image_provenance(previous_image)
        candidate_provenance = image_provenance(candidate_image)
        if (
            expected_upstream_commit
            and candidate_provenance.get("upstream_revision") != expected_upstream_commit
        ):
            raise RehearsalRejected(
                "Candidate image provenance does not match the requested upstream commit"
            )
        policy = private_root / "policy"
        policy.mkdir(mode=0o700)
        (policy / "instance.json").write_text(
            json.dumps(
                {
                    "name": "update-rehearsal",
                    "model": "offline-placeholder",
                    "timezone": "UTC",
                    "network": "dedicated",
                    "terminal_host": "127.0.0.1",
                    "terminal_port": 22,
                }
            )
        )
        (policy / "instance.json").chmod(0o600)
        ssh = private_root / "ssh"
        ssh.mkdir(mode=0o700)
        (ssh / "id_ed25519").write_text("rehearsal-only-invalid-key\n")
        (ssh / "known_hosts").write_text("\n")
        (ssh / "id_ed25519").chmod(0o600)
        (ssh / "known_hosts").chmod(0o600)

        container_owned = False
        try:
            _set_tree_owner(candidate_image, private_root, "10000:10000")
            container_owned = True
            migration = _migrate_native(candidate_image, private_home)
            startup = _run_container(candidate_image, private_home, policy, ssh)
            _set_tree_owner(candidate_image, private_root, f"{os.getuid()}:{os.getgid()}")
            container_owned = False
            after_config = _config_report(private_home)
            after_sqlite = _sqlite_report(private_home)
            report = {
                "schema": 1,
                "status": "passed",
                "snapshot": {
                    "source": "provided-snapshot" if snapshot else "offline-home-copy",
                    "sha256": snapshot_digest,
                },
                "rollback": {
                    "state_snapshot_sha256": snapshot_digest,
                    "previous_image_digest": previous_image,
                },
                "previous_image": {
                    "digest": previous_image,
                    "provenance": previous_provenance,
                },
                "candidate_image": {
                    "digest": candidate_image,
                    "provenance": candidate_provenance,
                },
                "config_migration": {"before": before_config, "after": after_config},
                "sqlite_migration": {"before": before_sqlite, "after": after_sqlite},
                "native_migration": migration,
                "startup": {"gateway_alive": True, **startup},
                "messaging": {
                    "status": "disabled-and-verified",
                    "profiles": messaging_evidence,
                    "container_network": "none",
                },
            }
        finally:
            try:
                if container_owned:
                    _set_tree_owner(candidate_image, private_root, f"{os.getuid()}:{os.getgid()}")
            finally:
                private_root.chmod(0o700)
    _write_report(report_path, report)
    return report


def _write_report(path: Path, report: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(report, output, indent=2, ensure_ascii=False)
        output.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-image", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument(
        "--expected-upstream-commit",
        help="Require this exact source-provenance upstream_revision",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot", type=Path, help="Project state snapshot archive")
    source.add_argument(
        "--home", type=Path, help="Stopped offline home; it is copied privately first"
    )
    parser.add_argument("--offline", action="store_true", help="Required with --home")
    parser.add_argument(
        "--snapshot-output",
        type=Path,
        help="Retain the rollback snapshot here when --home is used",
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = rehearse(
            previous_image=args.previous_image,
            candidate_image=args.candidate_image,
            snapshot=args.snapshot,
            home=args.home,
            offline=args.offline,
            report_path=args.report,
            expected_upstream_commit=args.expected_upstream_commit,
            snapshot_output=args.snapshot_output,
        )
    except (
        OSError,
        RuntimeError,
        RehearsalRejected,
        subprocess.SubprocessError,
        tarfile.TarError,
        json.JSONDecodeError,
    ) as error:
        safe = sanitize_error(str(error), (Path(args.home) if args.home else Path("/nonexistent"),))
        report = {
            "schema": 1,
            "status": "failed",
            "error": f"{type(error).__name__}: {safe}",
        }
        try:
            args.report.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_report(args.report, report)
        except FileExistsError:
            print(
                "Report target already exists; preserving the previous report",
                file=sys.stderr,
            )
        print(report["error"], file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
