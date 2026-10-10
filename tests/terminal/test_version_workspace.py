import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
import hermes_kubernetes.state.versioning as native

spec = importlib.util.spec_from_file_location(
    "version_workspace", ROOT / "hermes_kubernetes/terminal/versioning.py"
)
workspace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workspace)


class VersioningTests(unittest.TestCase):
    def test_inventory_refresh_keeps_managed_restoration_intent_for_deleted_override(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            root = directory / "project"
            root.mkdir()
            home = self.home(directory / "native")
            source = directory / "source"
            bundle = directory / "bundle"
            template = bundle / "skills/own/SKILL.md"
            template.parent.mkdir(parents=True)
            template.write_text("canonical instructions")

            class Runtime:
                def capture(self):
                    return native.capture(home, source, bundle=bundle)

                def apply(self, changes):
                    return native.apply(home, changes, source, bundle)

            runtime = Runtime()
            workspace.reconcile(root, runtime)
            recipe_path = root / "customizations/default/installations.json"
            recipe = json.loads(recipe_path.read_text())
            recipe["distribution_skills"] = ["skills/own/SKILL.md"]
            recipe_path.write_bytes(native.dump(recipe))
            (root / "customizations/default/skills/own/SKILL.md").unlink()
            native.atomic(
                home / "state/hermes-provenance.json",
                native.dump({"upstream_revision": "new inventory"}),
            )
            self.assertEqual(workspace.reconcile(root, runtime)["status"], "synchronized")
            self.assertEqual((home / "skills/own/SKILL.md").read_bytes(), template.read_bytes())
            self.assertEqual(workspace.reconcile(root, runtime)["status"], "synchronized")

    def test_rebuilt_managed_inventory_is_not_a_plugin_configuration_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            root = directory / "project"
            root.mkdir()
            home = self.home(directory / "native")
            runtime = workspace.LocalRuntime(home, directory / "source")
            workspace.reconcile(root, runtime)
            path = root / "customizations/default/installations.json"
            recipe = json.loads(path.read_text())
            recipe["upstream_revision"] = "candidate metadata"
            path.write_bytes(native.dump(recipe))
            native.atomic(
                home / "state/hermes-provenance.json",
                native.dump({"upstream_revision": "active source"}),
            )
            result = workspace.reconcile(root, runtime)
            self.assertEqual(result["status"], "synchronized")
            self.assertEqual(json.loads(path.read_text())["upstream_revision"], "active source")

    def test_conflicting_plugin_metadata_is_still_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            root = directory / "project"
            root.mkdir()
            home = self.home(directory / "native")
            runtime = workspace.LocalRuntime(home, directory / "source")
            workspace.reconcile(root, runtime)
            path = root / "customizations/default/installations.json"
            recipe = json.loads(path.read_text())
            recipe["plugins"] = {"git-plugin": {"source": "git"}}
            path.write_bytes(native.dump(recipe))
            native.atomic(
                home / "plugins/.install-metadata.json",
                native.dump({"native-plugin": {"source": "native"}}),
            )
            self.assertEqual(workspace.reconcile(root, runtime)["status"], "conflict")

    def test_removing_redundant_projection_restores_managed_skill_instead_of_uninstalling(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            home = self.home(directory / "native")
            bundle = directory / "bundle"
            template = bundle / "skills/own/SKILL.md"
            template.parent.mkdir(parents=True)
            template.write_text("managed distribution instructions")
            script = template.parent / "tool.sh"
            script.write_text("#!/bin/sh\necho managed\n")
            script.chmod(0o700)
            installed = home / "skills/own/tool.sh"
            installed.write_text("#!/bin/sh\necho personal\n")
            installed.chmod(0o700)
            snapshot = native.capture(home, bundle=bundle)
            name = "customizations/default/skills/own/SKILL.md"
            recipe_name = "customizations/default/installations.json"
            recipe = json.loads(base64.b64decode(snapshot["files"][recipe_name]["data"]))
            recipe["distribution_skills"] = [
                "skills/own/SKILL.md",
                "skills/own/tool.sh",
            ]
            script_name = "customizations/default/skills/own/tool.sh"
            native.apply(
                home,
                {
                    name: {"expected": snapshot["files"][name]["sha256"], "data": None},
                    script_name: {
                        "expected": snapshot["files"][script_name]["sha256"],
                        "data": None,
                    },
                    recipe_name: {
                        "expected": snapshot["files"][recipe_name]["sha256"],
                        "data": base64.b64encode(native.dump(recipe)).decode(),
                    },
                },
                bundle=bundle,
            )
            self.assertEqual((home / "skills/own/SKILL.md").read_bytes(), template.read_bytes())
            self.assertEqual(installed.read_bytes(), script.read_bytes())
            self.assertTrue(installed.stat().st_mode & 0o111)
            self.assertNotIn(name, native.capture(home, bundle=bundle)["files"])

    def test_removing_personal_skill_still_uninstalls_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = self.home(Path(temporary).resolve() / "native")
            snapshot = native.capture(home)
            name = "customizations/default/skills/own/SKILL.md"
            native.apply(
                home,
                {name: {"expected": snapshot["files"][name]["sha256"], "data": None}},
            )
            self.assertFalse((home / "skills/own/SKILL.md").exists())

    def test_exported_environment_examples_restore_without_admitting_private_envs(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = self.home(Path(temporary).resolve() / "native")
            sample = home / "plugins/own/.env.example"
            sample.write_text("API_KEY=${API_KEY}\n")
            snapshot = native.capture(home)
            name = "customizations/default/plugins/own/.env.example"
            self.assertIn(name, snapshot["files"])
            for exported in snapshot["files"]:
                native.target(home, exported)
            native.apply(
                home,
                {
                    name: {
                        "expected": snapshot["files"][name]["sha256"],
                        "data": base64.b64encode(b"API_KEY=${OTHER_KEY}\n").decode(),
                    }
                },
            )
            self.assertEqual(sample.read_text(), "API_KEY=${OTHER_KEY}\n")
            for private in [".env", ".env.production", ".env.local"]:
                with self.assertRaises(ValueError):
                    native.target(home, "customizations/default/plugins/own/" + private)

    def test_git_projection_cache_follows_staged_working_and_deleted_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(root), *args], stderr=subprocess.PIPE, text=True
                ).strip()

            git("init", "-q")
            git("config", "user.name", "fixture")
            git("config", "user.email", "fixture@example.test")
            (root / ".gitignore").write_text(".local/\n")
            name = "customizations/default/skills/own/SKILL.md"
            skill = root / name
            skill.parent.mkdir(parents=True)
            skill.write_text("first")
            git("add", ".")
            git("commit", "-qm", "initial")
            self.assertEqual(base64.b64decode(workspace.projection(root)[name]["data"]), b"first")
            read_bytes = Path.read_bytes

            def forbid_source_read(path):
                if path == skill:
                    raise AssertionError("Unchanged Git blob reread from Ceph")
                return read_bytes(path)

            with patch.object(Path, "read_bytes", forbid_source_read):
                workspace.projection(root)
            skill.write_text("working edit")
            self.assertEqual(
                base64.b64decode(workspace.projection(root)[name]["data"]),
                b"working edit",
            )
            git("add", name)
            with patch.object(Path, "read_bytes", forbid_source_read):
                workspace.projection(root)
            git("commit", "-qm", "staged edit")
            with patch.object(Path, "read_bytes", forbid_source_read):
                workspace.projection(root)
            skill.unlink()
            self.assertNotIn(name, workspace.projection(root))
            skill.write_text("restored")
            self.assertEqual(
                base64.b64decode(workspace.projection(root)[name]["data"]), b"restored"
            )
            git("update-index", "--assume-unchanged", name)
            with self.assertRaisesRegex(ValueError, "assume-unchanged"):
                workspace.projection(root)

    def test_corrupt_projection_cache_cannot_replace_the_git_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            name = "customizations/default/config.json"
            path = root / name
            path.parent.mkdir(parents=True)
            path.write_text("{}")
            subprocess.run(["git", "-C", str(root), "add", name], check=True)
            first = workspace.projection(root)
            cached = root / ".local/versioning/projection-cache.json.gz"
            record = json.loads(workspace.gzip.decompress(cached.read_bytes()))
            record[name]["item"]["data"] = base64.b64encode(b"corrupt cache").decode()
            native.atomic(cached, workspace.gzip.compress(native.dump(record)))
            self.assertEqual(workspace.projection(root), first)

    def test_terminal_controller_runs_without_site_packages(self):
        result = subprocess.run(
            [
                sys.executable,
                "-S",
                str(ROOT / "hermes_kubernetes/terminal/versioning.py"),
                "--help",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def home(self, path):
        path.mkdir()
        (path / "config.yaml").write_text(
            "model: {default: test}\nmcp_servers:\n  example:\n    command: npx\n    args: [example, --api-key, PRIVATE_CREDENTIAL_EXAMPLE]\n"
        )
        (path / ".env").write_text("API_KEY=PRIVATE_CREDENTIAL_EXAMPLE\n")
        (path / "skills/own").mkdir(parents=True)
        (path / "skills/own/SKILL.md").write_text("own skill")
        (path / "plugins/own").mkdir(parents=True)
        (path / "plugins/own/plugin.py").write_text('print("plugin")\n')
        return path

    def test_capture_references_secrets_tracks_own_code_and_omits_databases_and_dependencies(
        self,
    ):
        with tempfile.TemporaryDirectory() as t:
            home = self.home(Path(t).resolve() / "native")
            (home / "plugins/own/state.db").write_bytes(b"SQLite format 3\0private")
            (home / "plugins/own/node_modules").mkdir()
            (home / "plugins/own/node_modules/code.js").write_text("dependency")
            snapshot = native.capture(home, Path(t).resolve() / "source")
            encoded = json.dumps(snapshot)
            self.assertNotIn("PRIVATE_CREDENTIAL_EXAMPLE", encoded)
            config = json.loads(
                base64.b64decode(snapshot["files"]["customizations/default/config.json"]["data"])
            )
            self.assertIn("$hermes_secret", config["mcp_servers"]["example"]["args"][2])
            self.assertEqual(
                json.loads((home / "state/versioning/secrets.json").read_text()).popitem()[1],
                "PRIVATE_CREDENTIAL_EXAMPLE",
            )
            names = set(snapshot["files"])
            self.assertIn("customizations/default/plugins/own/plugin.py", names)
            self.assertFalse(
                any("node_modules" in p or p.endswith(".db") or ".env" in p for p in names)
            )

    def test_gui_and_git_edits_flow_both_ways_and_conflicts_preserve_both(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t).resolve() / "project"
            root.mkdir()
            home = self.home(Path(t).resolve() / "native")
            runtime = workspace.LocalRuntime(home, Path(t).resolve() / "source")
            first = workspace.reconcile(root, runtime)
            self.assertGreater(first["from_runtime"], 0)
            skill = root / "customizations/default/skills/own/SKILL.md"
            skill.write_text("edit from Git")
            self.assertEqual(workspace.reconcile(root, runtime)["to_runtime"], 1)
            self.assertEqual((home / "skills/own/SKILL.md").read_text(), "edit from Git")
            (home / "skills/new").mkdir()
            (home / "skills/new/SKILL.md").write_text("installed from GUI")
            workspace.reconcile(root, runtime)
            self.assertEqual(
                (root / "customizations/default/skills/new/SKILL.md").read_text(),
                "installed from GUI",
            )
            skill.write_text("Git concurrent")
            (home / "skills/own/SKILL.md").write_text("GUI concurrent")
            self.assertEqual(workspace.reconcile(root, runtime)["status"], "conflict")
            self.assertEqual(skill.read_text(), "Git concurrent")
            self.assertEqual((home / "skills/own/SKILL.md").read_text(), "GUI concurrent")

    def test_configuration_round_trip_keeps_private_values_and_cas_rejects_stale_changes(
        self,
    ):
        with tempfile.TemporaryDirectory() as t:
            home = self.home(Path(t).resolve() / "native")
            snap = native.capture(home)
            key = "customizations/default/config.json"
            record = snap["files"][key]
            config = json.loads(base64.b64decode(record["data"]))
            config["model"]["default"] = "changed"
            payload = base64.b64encode(native.dump(config)).decode()
            native.apply(home, {key: {"expected": record["sha256"], "data": payload}})
            self.assertIn("PRIVATE_CREDENTIAL_EXAMPLE", (home / "config.yaml").read_text())
            with self.assertRaises(ValueError):
                native.apply(home, {key: {"expected": record["sha256"], "data": payload}})

    def test_restore_empty_home_and_new_profiles_with_independent_environment(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t).resolve() / "project"
            root.mkdir()
            home = self.home(Path(t).resolve() / "native")
            runtime = workspace.LocalRuntime(home, Path(t).resolve() / "source")
            workspace.reconcile(root, runtime)
            # A fresh state needs its private credential vault restored independently.
            restored = Path(t).resolve() / "restored"
            restored.mkdir()
            native.atomic(
                restored / "state/versioning/secrets.json",
                (home / "state/versioning/secrets.json").read_bytes(),
            )
            (root / ".local/versioning/baseline.json").unlink()
            result = workspace.reconcile(
                root, workspace.LocalRuntime(restored, Path(t).resolve() / "source")
            )
            self.assertGreater(result["to_runtime"], 0)
            self.assertEqual((restored / "skills/own/SKILL.md").read_text(), "own skill")
            new = root / "customizations/second/config.json"
            new.parent.mkdir()
            new.write_text('{"model":{"default":"second"}}')
            workspace.reconcile(
                root, workspace.LocalRuntime(restored, Path(t).resolve() / "source")
            )
            self.assertTrue((restored / "profiles/second/.env").is_file())
            self.assertFalse((restored / "profiles/second/.env").is_symlink())
            self.assertNotEqual(
                (restored / ".env").stat().st_ino,
                (restored / "profiles/second/.env").stat().st_ino,
            )

    def test_plugin_secret_is_a_private_reference_and_symlink_cannot_escape(self):
        with tempfile.TemporaryDirectory() as t:
            home = self.home(Path(t).resolve() / "native")
            (home / "plugins/own/plugin.py").write_text('secret="PRIVATE_CREDENTIAL_EXAMPLE"')
            snap = native.capture(home)
            entry = snap["files"]["customizations/default/plugins/own/plugin.py"]
            self.assertNotIn("PRIVATE_CREDENTIAL_EXAMPLE", base64.b64decode(entry["data"]).decode())
            self.assertIn("${HERMES_SECRET:", base64.b64decode(entry["data"]).decode())
            name = "customizations/default/skills/own/SKILL.md"
            (home / "skills/own/SKILL.md").unlink()
            (home / "skills/own/SKILL.md").symlink_to(Path(t).resolve() / "outside")
            with self.assertRaises(ValueError):
                native.apply(
                    home,
                    {name: {"expected": None, "data": snap["files"][name]["data"]}},
                )

    def test_auto_commit_push_and_remote_conflict_without_force(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t).resolve()
            origin = base / "origin.git"
            root = base / "project"

            def git(path, *args):
                return subprocess.check_output(
                    ["git", "-C", str(path), *args], stderr=subprocess.PIPE, text=True
                ).strip()

            subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
            subprocess.run(
                ["git", "clone", "-q", str(origin), str(root)],
                check=True,
                stderr=subprocess.DEVNULL,
            )
            git(root, "switch", "-c", "main")
            git(root, "config", "user.name", "fixture")
            git(root, "config", "user.email", "fixture@example.test")
            (root / "project-files.json").write_text(
                json.dumps({"schema": 1, "runtime_files": [], "skills": []})
            )
            (root / ".gitignore").write_text("/*\n!project-files.json\n!customizations/\n")
            git(root, "add", ".")
            git(root, "commit", "-qm", "initial")
            git(root, "push", "-u", "origin", "main")
            path = root / "customizations/default/skills/new/SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text("new own skill")
            result = workspace.publish(root, quiet_seconds=0)
            self.assertEqual(result["status"], "published")
            self.assertEqual(
                git(origin, "show", "main:customizations/default/skills/new/SKILL.md"),
                "new own skill",
            )
            self.assertEqual(workspace.publish(root, quiet_seconds=0)["status"], "current")
            other = base / "other"
            subprocess.run(
                ["git", "clone", "-q", "--branch", "main", str(origin), str(other)],
                check=True,
            )
            git(other, "config", "user.name", "fixture")
            git(other, "config", "user.email", "fixture@example.test")
            remote_file = other / "customizations/default/skills/remote/SKILL.md"
            remote_file.parent.mkdir()
            remote_file.write_text("remote install")
            git(other, "add", ".")
            git(other, "commit", "-qm", "remote install")
            git(other, "push", "origin", "main")
            path.write_text("local edit")
            self.assertEqual(workspace.publish(root, quiet_seconds=0)["status"], "published")
            self.assertEqual(
                git(origin, "show", "main:customizations/default/skills/new/SKILL.md"),
                "local edit",
            )
            self.assertEqual((root / remote_file.relative_to(other)).read_text(), "remote install")
            git(other, "pull", "--ff-only", "origin", "main")
            (other / path.relative_to(root)).write_text("remote conflict")
            git(other, "add", ".")
            git(other, "commit", "-qm", "remote conflict")
            git(other, "push", "origin", "main")
            path.write_text("local conflict")
            self.assertEqual(workspace.publish(root, quiet_seconds=0)["status"], "git-conflict")
            self.assertEqual(
                git(root, "show", ":2:customizations/default/skills/new/SKILL.md"),
                "local conflict",
            )
            self.assertEqual(
                git(root, "show", ":3:customizations/default/skills/new/SKILL.md"),
                "remote conflict",
            )
            self.assertEqual(workspace.publish(root, quiet_seconds=0)["status"], "git-conflict")

    def test_private_key_body_stays_private_and_native_alias_restores(self):
        with tempfile.TemporaryDirectory() as t:
            home = self.home(Path(t).resolve() / "native")
            credential = (
                "-----"
                + "BEGIN PRIVATE KEY-----\nPRIVATE_BODY_NOT_FOR_GIT\n-----END PRIVATE KEY-----"
            )
            (home / "plugins/own/credential.txt").write_text(credential)
            (home / "skills/alias").symlink_to("../plugins/own", target_is_directory=True)
            snapshot = native.capture(home)
            encoded = json.dumps(snapshot)
            self.assertNotIn("PRIVATE_BODY_NOT_FOR_GIT", encoded)
            saved = json.loads(
                base64.b64decode(
                    snapshot["files"]["customizations/default/installations.json"]["data"]
                )
            )
            self.assertEqual(saved["links"], {"skills/alias": "plugins/own"})
            restored = Path(t).resolve() / "restored"
            restored.mkdir()
            native.atomic(
                restored / "state/versioning/secrets.json",
                (home / "state/versioning/secrets.json").read_bytes(),
            )
            native.apply(
                restored,
                {name: {"expected": None, **item} for name, item in snapshot["files"].items()},
            )
            self.assertEqual((restored / "plugins/own/credential.txt").read_text(), credential)
            self.assertEqual((restored / "skills/alias").resolve(), restored / "plugins/own")

    def test_failed_install_restores_native_files(self):
        with tempfile.TemporaryDirectory() as t:
            home = self.home(Path(t).resolve() / "native")
            snap = native.capture(home)
            name = "customizations/default/config.json"
            config = json.loads(base64.b64decode(snap["files"][name]["data"]))
            config["model"]["default"] = "candidate"
            original = (home / "config.yaml").read_bytes()
            changes = {
                name: {
                    "expected": snap["files"][name]["sha256"],
                    "data": base64.b64encode(native.dump(config)).decode(),
                },
                "customizations/default/python-lock.json": {
                    "expected": None,
                    "data": base64.b64encode(
                        native.dump({"extras": [], "lock": "fixture"})
                    ).decode(),
                },
            }
            with patch.object(
                native,
                "restore_python_installation",
                side_effect=ValueError("fixture failure"),
            ):
                with self.assertRaises(ValueError):
                    native.apply(home, changes)
            self.assertEqual((home / "config.yaml").read_bytes(), original)
            self.assertFalse((home / "state/versioning/desired-python.json").exists())


if __name__ == "__main__":
    unittest.main()
