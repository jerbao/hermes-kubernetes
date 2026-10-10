"""Verify release isolation, immutable dependency pins and transactional adoption."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[2]
from hermes_kubernetes.releases.manifest import validate, resolve
from hermes_kubernetes.instances.generator import create
from hermes_kubernetes.instances.updates import update
import hermes_kubernetes.instances.updates as update_instance


def manifest(version="v0.1.0", revision="a" * 40):
    repository = "owner/distribution"
    return {
        "schema": 1,
        "repository": repository,
        "version": version,
        "revision": revision,
        "images": {
            name: "ghcr.io/" + repository + suffix + "@sha256:" + "b" * 64
            for name, suffix in [
                ("hermes", ""),
                ("mcp", "-mcp"),
                ("mcp-codegraph", "-mcp-codegraph"),
            ]
        },
    }


class DistributionTests(unittest.TestCase):
    def test_manifest_requires_exact_commit_and_all_correct_image_repositories(self):
        validate(manifest())
        for mutation in [
            lambda v: v.update(revision="main"),
            lambda v: v.update(repository="../../private"),
            lambda v: v["images"].pop("mcp"),
            lambda v: v["images"].update(hermes="ghcr.io/other/project@sha256:" + "b" * 64),
            lambda v: v.update(version="v0.1.0-beta"),
        ]:
            value = manifest()
            mutation(value)
            with self.assertRaises(ValueError):
                validate(value)

    def test_generation_is_independent_private_and_does_not_copy_shared_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            one, two = [Path(temporary).resolve() / n for n in ("one", "two")]
            create(one, manifest())
            create(two, manifest())
            self.assertNotEqual(
                subprocess.check_output(["git", "-C", str(one), "rev-parse", "--git-dir"]),
                b"",
            )
            self.assertFalse((one / "runtime").exists())
            self.assertFalse((one / "patches").exists())
            for root in (one, two):
                paths = subprocess.check_output(
                    ["git", "-C", str(root), "ls-files"], text=True
                ).splitlines()
                self.assertIn("platform.lock.json", paths)
                self.assertIn("kustomization.yaml", paths)
                self.assertIn("systemd/hermes-workspace-sync.service", paths)
                self.assertIn(".github/workflows/update.yml", paths)
                self.assertNotIn(".local", paths)
                overlay = yaml.safe_load((root / "kustomization.yaml").read_text())
                self.assertIn("ref=" + "a" * 40, overlay["resources"][0])
                self.assertEqual(len(overlay["images"]), 3)
                self.assertEqual(
                    overlay["configMapGenerator"][0]["files"],
                    ["instance.json=instance.json"],
                )
            (one / "instance.json").write_text('{"name":"personal"}')
            self.assertEqual(json.loads((two / "instance.json").read_text())["name"], "hermes")
            with self.assertRaises(FileExistsError):
                create(one, manifest())
            self.assertEqual(json.loads((one / "instance.json").read_text())["name"], "personal")

    def test_compatible_update_changes_pins_and_private_skill_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "instance"
            create(root, manifest())
            skill = root / "customizations/default/skills/own/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_bytes(b"old private skill")
            candidate = manifest("v0.2.0", "c" * 40)
            changes = {
                "customizations/default/skills/own/SKILL.md": (
                    b"old private skill",
                    (b"compatible private skill", 0o600),
                )
            }
            with patch.object(
                update_instance,
                "reconcile_projection",
                return_value=({"ready": True, "conflicts": []}, changes),
            ):
                result = update(root, resolver=lambda *a: candidate, fetch=lambda *a: ROOT)
            self.assertEqual(result["status"], "updated")
            self.assertEqual(skill.read_bytes(), b"compatible private skill")
            self.assertEqual(
                json.loads((root / "platform.lock.json").read_text())["revision"],
                "c" * 40,
            )
            overlay = yaml.safe_load((root / "kustomization.yaml").read_text())
            self.assertIn("ref=" + "c" * 40, overlay["resources"][0])

    def test_skill_conflict_never_changes_lock_deployment_or_skill(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "instance"
            create(root, manifest())
            before = {
                n: (root / n).read_bytes() for n in ["platform.lock.json", "kustomization.yaml"]
            }
            with patch.object(
                update_instance,
                "reconcile_projection",
                return_value=(
                    {"ready": False, "conflicts": ["default/skills/own/SKILL.md"]},
                    {},
                ),
            ):
                with self.assertRaisesRegex(ValueError, "Managed skill conflict"):
                    update(
                        root,
                        resolver=lambda *a: manifest("v0.2.0", "c" * 40),
                        fetch=lambda *a: ROOT,
                    )
            for n, data in before.items():
                self.assertEqual((root / n).read_bytes(), data)
            self.assertTrue((root / ".local/update-report.json").exists())

    def test_concurrent_edit_prevents_all_candidate_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "instance"
            create(root, manifest())
            original = (root / "platform.lock.json").read_bytes()
            path = root / "customizations/default/skills/own/SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"concurrent user edit")
            changes = {
                "customizations/default/skills/own/SKILL.md": (
                    b"old skill",
                    (b"candidate skill", 0o600),
                )
            }
            with patch.object(
                update_instance,
                "reconcile_projection",
                return_value=({"ready": True, "conflicts": []}, changes),
            ):
                with self.assertRaisesRegex(ValueError, "Concurrent private edit"):
                    update(
                        root,
                        resolver=lambda *a: manifest("v0.2.0", "c" * 40),
                        fetch=lambda *a: ROOT,
                    )
            self.assertEqual(path.read_bytes(), b"concurrent user edit")
            self.assertEqual((root / "platform.lock.json").read_bytes(), original)

    def test_existing_version_is_a_noop_and_downgrade_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "instance"
            create(root, manifest("v0.2.0"))
            self.assertEqual(
                update(
                    root,
                    resolver=lambda *a: manifest("v0.2.0"),
                    fetch=lambda *a: self.fail("no fetch needed"),
                )["status"],
                "current",
            )
            with self.assertRaisesRegex(ValueError, "downgrade"):
                update(
                    root,
                    resolver=lambda *a: manifest(),
                    fetch=lambda *a: self.fail("no fetch needed"),
                )

    def test_release_asset_must_match_the_repository_and_tag_commit(self):
        value = manifest()
        responses = [
            {
                "tag_name": "v0.1.0",
                "assets": [
                    {
                        "name": "distribution.json",
                        "browser_download_url": "https://github.com/owner/distribution/releases/download/v0.1.0/distribution.json",
                    }
                ],
            },
            value,
            {"object": {"type": "commit", "sha": "c" * 40}},
        ]
        with patch(
            "hermes_kubernetes.releases.manifest.request",
            side_effect=[json.dumps(v).encode() for v in responses],
        ):
            with self.assertRaisesRegex(ValueError, "tagged source"):
                resolve("owner/distribution")

    def test_actual_cross_repository_skill_reconciliation_preserves_private_edit(self):
        from tests.state.test_skill_updates import SkillUpdatesTests, TEXT, git, put
        from hermes_kubernetes.instances.updates import reconcile_projection

        fixture = SkillUpdatesTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        old, base = fixture.project()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            instance = root / "instance"
            create(instance, manifest())
            import shutil

            shutil.copytree(old / "customizations", instance / "customizations")
            git(instance, "add", "customizations")
            git(
                instance,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "-qm",
                "personal",
            )
            new = root / "candidate"
            subprocess.run(["git", "clone", "-q", str(old), str(new)], check=True)
            template = new / "skills/management/SKILL.md"
            template.write_bytes(template.read_bytes().replace(b"first", b"new first"))
            git(
                new,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "-qam",
                "distribution update",
            )
            original_run = subprocess.run

            def prepare(command, **kwargs):
                if len(command) > 1 and str(command[1]).endswith(
                    "hermes_kubernetes/source/prepare.py"
                ):
                    destination = Path(command[command.index("--output") + 1])
                    return original_run(
                        ["git", "clone", "-q", str(fixture.source), str(destination)],
                        check=True,
                    )
                return original_run(command, **kwargs)

            with patch("hermes_kubernetes.instances.skills.subprocess.run", side_effect=prepare):
                report, changes = reconcile_projection(instance, old, new)
            self.assertTrue(report["ready"])
            desired = changes["customizations/default/skills/management/SKILL.md"][1][0]
            self.assertIn(b"new first", desired)
            self.assertIn(b"personal last", desired)
            self.assertNotIn(
                b"new first",
                (instance / "customizations/default/skills/management/SKILL.md").read_bytes(),
            )
            self.assertEqual(
                json.loads((instance / "platform.lock.json").read_text())["version"],
                "v0.1.0",
            )

    def test_public_workflows_have_no_private_gitops_credentials(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            text = path.read_text()
            self.assertNotIn("GITOPS_TOKEN", text)
            self.assertNotIn("manifestos-kubernetes", text)
        self.assertIn(
            "job.status",
            (ROOT / "examples/instance/.github/workflows/update.yml").read_text(),
        )


if __name__ == "__main__":
    unittest.main()
