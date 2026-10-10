import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import subprocess
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "runtime"))
from state_snapshot import build_snapshot
from rehearse_update import (RehearsalRejected, _require_digest, disable_messaging,
                             _config_report, _disable_config_sources, extract_snapshot,
                             _migrate_native, rehearse, reject_active_home, sanitize_error,
                             _run_container, _database_owner)


PREVIOUS = "ghcr.io/example/hermes@sha256:" + "1" * 64
CANDIDATE = "ghcr.io/example/hermes@sha256:" + "2" * 64


def snapshot(home: Path, output: Path) -> None:
    build_snapshot(home, output)


_FIXTURE_SESSION_ID = "update-rehearsal-session-001"
_FIXTURE_EXECUTION_ID = "update-rehearsal-execution-001"
_FIXTURE_INCIDENT_ID = "update-rehearsal-incident-001"
_FIXTURE_DELIVERY_ID = "update-rehearsal-delivery-001"
_FIXTURE_JOB_ID = "update-rehearsal-job-001"
_FIXTURE_QUERY = "hermesrehearsalpreservationtoken"


def _seed_native_legacy_state(image: str, home: Path) -> None:
    """Build a tiny schema-v1 state DB using the candidate's official legacy FTS DDL."""
    script = f'''import sqlite3
from pathlib import Path
from hermes_state_common import LEGACY_FTS_SQL
from hermes_cli.session_schema_history import declared_snapshots

path = Path('/opt/data/state.db')
db = sqlite3.connect(path)
sessions = declared_snapshots('sessions')[0]
messages = declared_snapshots('messages')[0]
session_types = {{'id':'TEXT PRIMARY KEY', 'source':'TEXT NOT NULL', 'started_at':'REAL NOT NULL'}}
message_types = {{'id':'INTEGER PRIMARY KEY AUTOINCREMENT', 'session_id':'TEXT NOT NULL',
                 'role':'TEXT NOT NULL', 'timestamp':'REAL NOT NULL'}}
def ddl(table, columns, types):
    return 'CREATE TABLE ' + table + '(' + ', '.join(c + ' ' + types.get(c, 'TEXT') for c in columns) + ')'
db.execute('CREATE TABLE schema_version(version INTEGER NOT NULL)')
db.execute('INSERT INTO schema_version VALUES (1)')
db.execute(ddl('sessions', sessions, session_types))
db.execute(ddl('messages', messages, message_types))
db.executescript(LEGACY_FTS_SQL)
session_values = {{'id':{_FIXTURE_SESSION_ID!r}, 'source':'cli', 'model':'fixture-model',
                  'model_config':'{{}}', 'started_at':1700000000.0, 'message_count':2,
                  'input_tokens':7, 'output_tokens':5}}
db.execute('INSERT INTO sessions (' + ','.join(session_values) + ') VALUES (' +
           ','.join('?' for _ in session_values) + ')', tuple(session_values.values()))
for role, content, stamp in (('user', {_FIXTURE_QUERY!r}, 1700000001.0),
                             ('assistant', 'synthetic answer preserved across migration', 1700000002.0)):
    db.execute('INSERT INTO messages(session_id, role, content, timestamp, token_count) VALUES (?,?,?,?,?)',
               ({_FIXTURE_SESSION_ID!r}, role, content, stamp, 3))
db.commit()
db.close()
'''
    result = subprocess.run([
        "docker", "run", "--rm", "--network", "none", "--user", "0:0",
        "--env", "HERMES_HOME=/opt/data",
        "--mount", f"type=bind,src={home},dst=/opt/data",
        "--entrypoint", "/opt/hermes/.venv/bin/python", image, "-c", script,
    ], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise AssertionError("native fixture seeding failed: " + result.stderr[-1000:])
    (home / "SOUL.md").unlink(missing_ok=True)


def _seed_legacy_cron(home: Path) -> None:
    cron = home / "cron"
    cron.mkdir(parents=True, exist_ok=True)
    timestamp = "2026-01-02T03:04:05+00:00"
    with sqlite3.connect(cron / "executions.db") as db:
        db.execute("""CREATE TABLE executions (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
            process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER,
            status TEXT NOT NULL CHECK(status IN ('claimed','running','completed','failed','unknown')),
            claimed_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT)""")
        db.execute("INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (_FIXTURE_EXECUTION_ID, _FIXTURE_JOB_ID, "manual", "fixture-process", 1,
                    None, "completed", timestamp, timestamp, timestamp, None))
        db.execute("""CREATE TABLE cron_incidents (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL, error_sig TEXT NOT NULL,
            state TEXT NOT NULL, failure_type TEXT NOT NULL DEFAULT 'unknown',
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, acked_at TEXT,
            closed_at TEXT, error TEXT NOT NULL, output_file TEXT)""")
        db.execute("INSERT INTO cron_incidents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (_FIXTURE_INCIDENT_ID, _FIXTURE_JOB_ID, "synthetic-signature", "detected",
                    "agent", timestamp, timestamp, None, None, "synthetic fixture incident", None))
    with sqlite3.connect(cron / "deliveries.db") as db:
        db.execute("""CREATE TABLE deliveries (
            execution_id TEXT PRIMARY KEY, job_json TEXT NOT NULL, content TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','delivering','delivered','failed','unknown')),
            owner_process_id TEXT, owner_pid INTEGER, owner_started_at INTEGER,
            created_at TEXT NOT NULL, finished_at TEXT, error TEXT)""")
        db.execute("""CREATE TABLE delivery_tombstones (
            execution_id TEXT PRIMARY KEY,
            terminal_status TEXT NOT NULL CHECK(terminal_status IN ('delivered','failed','unknown')),
            finished_at TEXT)""")
        db.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (_FIXTURE_DELIVERY_ID, '{"fixture":"stable"}', "synthetic pending payload",
                    "pending", None, None, None, timestamp, None, None))
        db.execute("INSERT INTO delivery_tombstones VALUES (?,?,?)",
                   ("update-rehearsal-tombstone-001", "delivered", timestamp))
    with sqlite3.connect(cron / "notepad.db") as db:
        db.execute("CREATE TABLE cron_notepad(job_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(job_id,key))")
        db.execute("INSERT INTO cron_notepad VALUES (?,?,?,?)",
                   (_FIXTURE_JOB_ID, "cursor", "synthetic-cursor-17", timestamp))


def _fixture_before(home: Path) -> dict:
    with sqlite3.connect(home / "state.db") as db:
        session_ids = [row[0] for row in db.execute("SELECT id FROM sessions ORDER BY id")]
        message_ids = [row[0] for row in db.execute(
            "SELECT id FROM messages WHERE session_id=? ORDER BY id", (_FIXTURE_SESSION_ID,))]
        search_ids = [row[0] for row in db.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ? ORDER BY rowid",
            (_FIXTURE_QUERY,))]
    with sqlite3.connect(home / "cron" / "executions.db") as db:
        executions = [list(row) for row in db.execute("SELECT id,status FROM executions ORDER BY id")]
        incidents = [list(row) for row in db.execute("SELECT id,state FROM cron_incidents ORDER BY id")]
    with sqlite3.connect(home / "cron" / "deliveries.db") as db:
        deliveries = [list(row) for row in db.execute(
            "SELECT execution_id,status,content FROM deliveries ORDER BY execution_id")]
        tombstones = [list(row) for row in db.execute(
            "SELECT execution_id,terminal_status FROM delivery_tombstones ORDER BY execution_id")]
    with sqlite3.connect(home / "cron" / "notepad.db") as db:
        notes = [list(row) for row in db.execute("SELECT job_id,key,value FROM cron_notepad ORDER BY job_id,key")]
    profile_home = home / "profiles" / "fixture"
    with sqlite3.connect(profile_home / "cron" / "executions.db") as db:
        profile_executions = [list(row) for row in db.execute("SELECT id,status FROM executions ORDER BY id")]
        profile_incidents = [list(row) for row in db.execute("SELECT id,state FROM cron_incidents ORDER BY id")]
    with sqlite3.connect(profile_home / "cron" / "deliveries.db") as db:
        profile_deliveries = [list(row) for row in db.execute(
            "SELECT execution_id,status,content FROM deliveries ORDER BY execution_id")]
        profile_tombstones = [list(row) for row in db.execute(
            "SELECT execution_id,terminal_status FROM delivery_tombstones ORDER BY execution_id")]
    with sqlite3.connect(profile_home / "cron" / "notepad.db") as db:
        profile_notes = [list(row) for row in db.execute(
            "SELECT job_id,key,value FROM cron_notepad ORDER BY job_id,key")]
    return {"session_ids": session_ids, "message_ids": message_ids,
            "message_count": len(message_ids), "search_query": _FIXTURE_QUERY,
            "search_message_ids": search_ids, "executions": executions,
            "incidents": incidents, "deliveries": deliveries,
            "delivery_tombstones": tombstones, "notepad": notes,
            "profile_cron": {"executions": profile_executions, "incidents": profile_incidents,
                             "deliveries": profile_deliveries, "tombstones": profile_tombstones,
                             "notepad": profile_notes}}


def _native_fixture_after(image: str, home: Path) -> dict:
    script = f'''import json
from pathlib import Path
from hermes_state import SessionDB
from cron import executions, incidents, delivery_queue, notepad
from hermes_constants import set_hermes_home_override, reset_hermes_home_override

root = Path('/opt/data')
db = SessionDB(db_path=root/'state.db')
messages = db.get_messages({_FIXTURE_SESSION_ID!r}, include_inactive=True, include_compacted=True)
matches = db.search_messages({_FIXTURE_QUERY!r}, include_inactive=True,
                             fields=['id','session_id','role'])
db.close()
execution = executions.get_execution({_FIXTURE_EXECUTION_ID!r})
incident = incidents.get_incident({_FIXTURE_INCIDENT_ID!r})
delivery = delivery_queue.get_status({_FIXTURE_DELIVERY_ID!r})
note = notepad.get_note({_FIXTURE_JOB_ID!r}, 'cursor')
token = set_hermes_home_override(str(root/'profiles/fixture'))
try:
 profile_execution = executions.get_execution({_FIXTURE_EXECUTION_ID!r})
 profile_incident = incidents.get_incident({_FIXTURE_INCIDENT_ID!r})
 profile_delivery = delivery_queue.get_status({_FIXTURE_DELIVERY_ID!r})
 profile_note = notepad.get_note({_FIXTURE_JOB_ID!r}, 'cursor')
 profile_tombstones = [list(r) for r in __import__('sqlite3').connect(root/'profiles/fixture/cron/deliveries.db').execute('SELECT execution_id,terminal_status FROM delivery_tombstones ORDER BY execution_id')]
finally:
 reset_hermes_home_override(token)
print('HERMES_FIXTURE_AFTER=' + json.dumps({{
 'session_ids': [r[0] for r in __import__('sqlite3').connect(root/'state.db').execute('SELECT id FROM sessions ORDER BY id')],
 'message_ids': [m['id'] for m in messages], 'message_count': len(messages),
 'search_query': {_FIXTURE_QUERY!r}, 'search_message_ids': [m['id'] for m in matches],
 'executions': [[execution['id'],execution['status']]] if execution else [],
 'incidents': [[incident['id'],incident['state']]] if incident else [],
 'deliveries': [[delivery['execution_id'],delivery['status'],delivery['content']]] if delivery else [],
 'delivery_tombstones': [list(r) for r in __import__('sqlite3').connect(root/'cron/deliveries.db').execute('SELECT execution_id,terminal_status FROM delivery_tombstones ORDER BY execution_id')],
 'notepad': [[{_FIXTURE_JOB_ID!r},'cursor',note]] if note is not None else [],
 'profile_cron': {{
  'executions': [[profile_execution['id'],profile_execution['status']]] if profile_execution else [],
  'incidents': [[profile_incident['id'],profile_incident['state']]] if profile_incident else [],
  'deliveries': [[profile_delivery['execution_id'],profile_delivery['status'],profile_delivery['content']]] if profile_delivery else [],
  'tombstones': profile_tombstones,
  'notepad': [[{_FIXTURE_JOB_ID!r},'cursor',profile_note]] if profile_note is not None else []
 }}
}}, sort_keys=True))
'''
    result = subprocess.run([
        "docker", "run", "--rm", "--network", "none",
        "--mount", f"type=bind,src={home},dst=/opt/data",
        "--entrypoint", "/opt/hermes/.venv/bin/python", image, "-c", script,
    ], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise AssertionError("native preservation probe failed: " + result.stderr[-1000:])
    marker = "HERMES_FIXTURE_AFTER="
    line = next(line[len(marker):] for line in result.stdout.splitlines() if line.startswith(marker))
    return json.loads(line)


class RehearseUpdateTests(unittest.TestCase):
    def test_home_argument_requires_offline_and_refuses_active_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            with self.assertRaisesRegex(RehearsalRejected, "offline"):
                rehearse(previous_image=PREVIOUS, candidate_image=CANDIDATE, home=home,
                         report_path=Path(temporary) / "report.json")
            with patch("rehearse_update.reject_active_home"):
                with self.assertRaisesRegex(RehearsalRejected, "snapshot-output"):
                    rehearse(previous_image=PREVIOUS, candidate_image=CANDIDATE, home=home, offline=True,
                             report_path=Path(temporary) / "report.json")
            with self.assertRaisesRegex(RehearsalRejected, "active HERMES_HOME"):
                reject_active_home(home, environ={"HERMES_HOME": str(home)})

    def test_snapshot_extraction_rejects_traversal_and_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            archive_path = base / "unsafe.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                info = tarfile.TarInfo("../../outside")
                info.size = 1
                import io
                archive.addfile(info, io.BytesIO(b"x"))
            with self.assertRaisesRegex(RehearsalRejected, "unsafe path"):
                extract_snapshot(archive_path, base / "private")

    def test_snapshot_requires_native_manifest_and_detects_changed_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            missing = base / "missing.tar.gz"
            with tarfile.open(missing, "w:gz") as archive:
                info = tarfile.TarInfo("config.yaml")
                info.size = 1
                import io
                archive.addfile(info, io.BytesIO(b"x"))
            with self.assertRaisesRegex(RehearsalRejected, "required native manifest"):
                extract_snapshot(missing, base / "missing-home")

            home = base / "home"
            home.mkdir()
            (home / "config.yaml").write_text("gateway: {platforms: {}}\n")
            valid = base / "valid.tar.gz"
            snapshot(home, valid)
            changed = base / "changed.tar.gz"
            with tarfile.open(valid, "r:gz") as source, tarfile.open(changed, "w:gz") as output:
                for member in source.getmembers():
                    contents = source.extractfile(member) if member.isfile() else None
                    if member.name == "config.yaml":
                        import io
                        contents = io.BytesIO(b"changed")
                        member.size = len(b"changed")
                    output.addfile(member, contents)
            with self.assertRaisesRegex(RehearsalRejected, "manifest verification failed"):
                extract_snapshot(changed, base / "changed-home")

    def test_native_channel_disabler_uses_candidate_registry_and_synthetic_env_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "config.yaml").write_text(
                "gateway:\n  relay_url: https://relay.example.invalid\n  platforms:\n    telegram: {enabled: true}\n  slack: {enabled: true}\n"
                "platforms:\n  discord: {enabled: true}\n")
            (home / "gateway.json").write_text(json.dumps({
                "platforms": {"matrix": {"enabled": True}},
                "gateway": {"relay_url": "https://relay.example.invalid", "signal": {"enabled": True}}}))
            roster = type("Result", (), {"returncode": 0, "stderr": "", "stdout": (
                'HERMES_PLATFORM_ROSTER={".": ["telegram", "discord", "slack", "buzz", "signal"]}\n')})()
            verified = type("Result", (), {"returncode": 0, "stderr": "", "stdout": (
                'HERMES_MESSAGING_DISABLED={".": {"enabled_after_native_synthetic_env_probe": [], '
                '"synthetic_env_names_checked": 25}}\n')})()
            with patch("rehearse_update.subprocess.run", side_effect=[roster, verified]) as run:
                evidence = disable_messaging("sha256:" + "2" * 64, home)
            self.assertEqual(evidence["."]["enabled_after_native_synthetic_env_probe"], [])
            roster_command = run.call_args_list[0].args[0]
            roster_script = roster_command[roster_command.index("-c") + 1]
            verify_command = run.call_args_list[1].args[0]
            verify_script = verify_command[verify_command.index("-c") + 1]
            self.assertIn("platform_registry.all_entries()", roster_script)
            self.assertIn("load_gateway_config()", verify_script)
            self.assertIn("_ENV_ENABLE_CREDENTIALS", verify_script)
            self.assertIn("os.environ.pop('GATEWAY_RELAY_URL', None)", verify_script)
            config = yaml.safe_load((home / "config.yaml").read_text())
            legacy = json.loads((home / "gateway.json").read_text())
            self.assertNotIn("relay_url", config["gateway"])
            self.assertFalse(config["gateway"]["platforms"]["telegram"]["enabled"])
            self.assertFalse(config["gateway"]["slack"]["enabled"])
            self.assertFalse(config["platforms"]["discord"]["enabled"])
            self.assertNotIn("relay_url", legacy["gateway"])
            self.assertFalse(legacy["platforms"]["matrix"]["enabled"])
            self.assertFalse(legacy["gateway"]["signal"]["enabled"])
            self.assertEqual(run.call_count, 2)

    def test_report_records_snapshot_both_digests_migrations_and_startup(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "home"
            home.mkdir()
            (home / "config.yaml").write_text("_config_version: 1\ngateway:\n  platforms: {}\n")
            with sqlite3.connect(home / "state.db") as database:
                database.execute("CREATE TABLE schema_version(version INTEGER)")
                database.execute("INSERT INTO schema_version VALUES (2)")
            archive = base / "state.tar.gz"
            snapshot(home, archive)
            report_path = base / "report.json"

            def migrate(_image, private_home):
                config_path = private_home / "config.yaml"
                config = yaml.safe_load(config_path.read_text())
                config["_config_version"] = 3
                config_path.write_text(yaml.safe_dump(config))
                with sqlite3.connect(private_home / "state.db") as database:
                    database.execute("UPDATE schema_version SET version = 4")
                return {"stdout": "migrated"}

            def start(_image, private_home, _policy, _ssh):
                config = yaml.safe_load((private_home / "config.yaml").read_text())
                self.assertEqual(config["gateway"]["platforms"], {})
                return {"container_id": "fixture"}

            with patch("rehearse_update.image_provenance", side_effect=[{"version": "old"}, {"version": "new"}]), \
                    patch("rehearse_update.disable_messaging", return_value={".": {
                        "candidate_platform_count": 40, "enabled_after_native_synthetic_env_probe": []}}), \
                    patch("rehearse_update._migrate_native", side_effect=migrate), \
                    patch("rehearse_update._set_tree_owner"), \
                    patch("rehearse_update._run_container", side_effect=start):
                result = rehearse(previous_image=PREVIOUS, candidate_image=CANDIDATE,
                                  snapshot=archive, report_path=report_path)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["rollback"]["previous_image_digest"], PREVIOUS)
            self.assertEqual(result["rollback"]["state_snapshot_sha256"], result["snapshot"]["sha256"])
            self.assertEqual(result["config_migration"]["before"]["config.yaml"], 1)
            self.assertEqual(result["config_migration"]["after"]["config.yaml"], 3)
            self.assertEqual(result["sqlite_migration"]["before"]["state.db"]["schema_version"], 2)
            self.assertEqual(result["sqlite_migration"]["after"]["state.db"]["schema_version"], 4)
            self.assertEqual(result["startup"]["container_id"], "fixture")
            self.assertEqual(result["messaging"]["status"], "disabled-and-verified")
            self.assertEqual(report_path.stat().st_mode & 0o777, 0o600)

    def test_image_reference_requires_sha256_digest(self):
        with self.assertRaisesRegex(RehearsalRejected, "SHA-256 digest"):
            _require_digest("ghcr.io/example/hermes:latest", "candidate image")
        self.assertEqual(_require_digest("sha256:" + "3" * 64, "candidate image"),
                         "sha256:" + "3" * 64)

    def test_error_sanitizer_redacts_secrets_and_private_paths(self):
        private = Path("/sensitive/hermes-home")
        value = sanitize_error("Bearer abc.def token=xyz https://user:pass@example.test " + str(private),
                               (private,))
        for secret in ("abc.def", "xyz", "user:pass", str(private)):
            self.assertNotIn(secret, value)

    def test_invalid_yaml_is_rejected_without_traceback_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "config.yaml").write_text("gateway: [\n")
            with self.assertRaisesRegex(RehearsalRejected, "Invalid Hermes config YAML"):
                _disable_config_sources(home, {"telegram"})
            with self.assertRaisesRegex(RehearsalRejected, "Invalid Hermes config YAML"):
                _config_report(home)

    def test_running_container_is_not_ready_without_native_loop_probe(self):
        result = lambda stdout, returncode=0: type("Result", (), {
            "stdout": stdout, "returncode": returncode})()
        with patch("rehearse_update.subprocess.run") as run, \
                patch("rehearse_update.time.monotonic", side_effect=[0.0, 0.0, 2.0]), \
                patch("rehearse_update.time.sleep"):
            run.side_effect = [result("container-id"), result("true"), result("unknown\n"),
                               result("stopped"), result("removed")]
            with self.assertRaisesRegex(RehearsalRejected, "Native gateway loop did not become ready"):
                _run_container("image", Path("/copy"), Path("/policy"), Path("/ssh"), readiness_timeout=1)

    def test_running_container_becomes_ready_only_after_exact_native_probe(self):
        result = lambda stdout, returncode=0: type("Result", (), {
            "stdout": stdout, "returncode": returncode})()
        with patch("rehearse_update.subprocess.run") as run:
            run.side_effect = [result("container-id"), result("true"), result("ready\n"),
                               result("stopped"), result("removed")]
            startup = _run_container("image", Path("/copy"), Path("/policy"), Path("/ssh"))
        self.assertEqual(startup["readiness"], "native-loop-tick")
        docker_run = run.call_args_list[0].args[0]
        self.assertIn("GATEWAY_ON_ALL_ADAPTERS_DOWN=stay_alive", docker_run)

    def test_known_non_session_database_owners_are_reported(self):
        self.assertIn("ResponseStore", _database_owner("response_store.db"))
        self.assertIn("idempotency", _database_owner("runs_idempotency.db"))
        self.assertIn("hosted_rooms", _database_owner("shared-state.db"))

    def test_cron_ledger_classifies_both_native_owners(self):
        owner = _database_owner("profiles/fixture/cron/executions.db")
        self.assertIn("cron.executions._connect", owner)
        self.assertIn("cron.incidents._connect", owner)
        self.assertEqual(_database_owner("cron/incidents.db").split(";")[0],
                         "unclassified native store")

    def test_candidate_provenance_must_match_expected_release_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "home"
            home.mkdir()
            (home / "config.yaml").write_text("gateway: {platforms: {}}\n")
            archive = base / "state.tar.gz"
            snapshot(home, archive)
            with patch("rehearse_update.disable_messaging", return_value={".": {
                        "candidate_platform_count": 40, "enabled_after_native_synthetic_env_probe": []}}), \
                    patch("rehearse_update.image_provenance", side_effect=[{}, {"upstream_revision": "a" * 40}]):
                with self.assertRaisesRegex(RehearsalRejected, "does not match"):
                    rehearse(previous_image=PREVIOUS, candidate_image=CANDIDATE,
                             snapshot=archive, report_path=base / "report.json",
                             expected_upstream_commit="b" * 40)


@unittest.skipUnless(os.environ.get("HERMES_REHEARSAL_CANDIDATE_IMAGE")
                     and os.environ.get("HERMES_REHEARSAL_PREVIOUS_IMAGE")
                     and os.environ.get("HERMES_REHEARSAL_SNAPSHOT"),
                     "set private snapshot and previous/candidate image digests for real rehearsal")
class RealImageMigrationTests(unittest.TestCase):
    def test_native_migrations_and_gateway_start_on_private_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = rehearse(
                previous_image=os.environ["HERMES_REHEARSAL_PREVIOUS_IMAGE"],
                candidate_image=os.environ["HERMES_REHEARSAL_CANDIDATE_IMAGE"],
                snapshot=Path(os.environ["HERMES_REHEARSAL_SNAPSHOT"]),
                report_path=Path(temporary) / "actual-rehearsal.json")
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["startup"].get("probe"), "gateway_loop_alive")
            self.assertTrue(result["sqlite_migration"]["after"])


@unittest.skipUnless(os.environ.get("HERMES_REHEARSAL_CANDIDATE_IMAGE")
                     and os.environ.get("HERMES_REHEARSAL_PREVIOUS_IMAGE"),
                     "set local candidate and previous image digests for functional rehearsal")
class RealImageFunctionalMigrationTests(unittest.TestCase):
    def test_old_session_fts_and_cron_rows_survive_native_migration(self):
        candidate = os.environ["HERMES_REHEARSAL_CANDIDATE_IMAGE"]
        previous = os.environ["HERMES_REHEARSAL_PREVIOUS_IMAGE"]
        workdir = os.environ.get("HERMES_REHEARSAL_FUNCTIONAL_DIR")
        if workdir:
            persistent_root = Path(workdir).resolve()
            persistent_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if any(persistent_root.iterdir()):
                self.fail("functional fixture directory must be empty before the run")
            temporary_context = nullcontext(str(persistent_root))
        else:
            temporary_context = tempfile.TemporaryDirectory(prefix="hermes-functional-rehearsal-")
        with temporary_context as temporary:
            base = Path(temporary).resolve()
            home = base / "home"
            home.mkdir(mode=0o700)
            (home / "config.yaml").write_text("_config_version: 12\ngateway: {}\n")
            _seed_native_legacy_state(candidate, home)
            _seed_legacy_cron(home)
            profile_home = home / "profiles" / "fixture"
            profile_home.mkdir(parents=True)
            (profile_home / "config.yaml").write_text("_config_version: 12\ngateway: {}\n")
            _seed_legacy_cron(profile_home)
            before = _fixture_before(home)
            archive = base / "legacy-state.tar.gz"
            snapshot(home, archive)
            report_path = Path(os.environ.get("HERMES_REHEARSAL_FUNCTIONAL_REPORT",
                                             base / "functional-report.json")).resolve()
            captured = {}
            native_migrate = _migrate_native

            def migrate_and_probe(image, private_home):
                evidence = native_migrate(image, private_home)
                captured["after"] = _native_fixture_after(image, private_home)
                return evidence

            with patch("rehearse_update._migrate_native", side_effect=migrate_and_probe):
                report = rehearse(
                    previous_image=previous, candidate_image=candidate,
                    snapshot=archive, report_path=report_path,
                    expected_upstream_commit="818c13be1dc4fd28987e1e881a9408224afd4535")

            after = captured["after"]
            self.assertEqual(after, before)
            self.assertEqual(report["native_migration"]["profiles"], ["."])
            patches = report["candidate_image"]["provenance"].get("patches", [])
            self.assertEqual(len(patches), 4, "final image provenance must identify all four upstream patches")
            cron_owners = report["native_migration"]["cron_databases_opened_by"]
            self.assertEqual({entry["owner"] for entry in cron_owners}, {
                "cron.executions._connect", "cron.incidents._connect",
                "cron.delivery_queue._connect", "cron.notepad._connect"})
            self.assertEqual({entry["database"] for entry in cron_owners}, {
                "cron/executions.db", "cron/deliveries.db", "cron/notepad.db",
                "profiles/fixture/cron/executions.db",
                "profiles/fixture/cron/deliveries.db",
                "profiles/fixture/cron/notepad.db"})
            self.assertEqual(report["sqlite_migration"]["after"]["state.db"]["schema_version"], 31)

            functional = {"before": before, "after": after,
                          "candidate_patch_paths": [item["path"] for item in patches],
                          "preservation_assertion": "session/message IDs, counts, FTS search, and cron rows equal"}
            report["functional_preservation"] = functional
            report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
            report_path.chmod(0o600)
            self.assertEqual(report_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
