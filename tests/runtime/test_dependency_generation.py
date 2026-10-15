import json
from pathlib import Path
import tempfile
import unittest

from hermes_kubernetes.runtime.dependency_generation import core_drift, reconcile


class DependencyGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.facts = self.root / "install/facts.json"
        self.recipe = self.facts.parent / "environments/selected/workspace/uv.lock"
        self.recipe.parent.mkdir(parents=True)
        self.source.joinpath("uv.lock").write_text(self.lock("0.10.0"))
        self.recipe.write_text(self.lock("0.9.3", plugin=True))
        self.record = {
            "packages": {
                "venv": {
                    "environment": str(self.recipe.parent.parent / "venv"),
                    "resolved_lock": str(self.recipe),
                    "extras": ["messaging", "firecrawl"],
                }
            }
        }
        self.facts.write_text(json.dumps(self.record))

    @staticmethod
    def lock(version, plugin=False):
        text = f'[[package]]\nname = "nemo-relay"\nversion = "{version}"\n'
        return text + (
            '[[package]]\nname = "private-plugin-dependency"\nversion = "1.2.3"\n' if plugin else ""
        )

    def test_core_upgrade_uses_native_transaction_and_preserves_plugin_selection(self):
        calls = []

        def synchronize(**kwargs):
            calls.append(kwargs)
            self.recipe.write_text(self.lock("0.10.0", plugin=True))

        self.assertEqual(
            reconcile(self.source, facts_path=self.facts, synchronize=synchronize), ["nemo-relay"]
        )
        self.assertEqual(calls, [{"explicit": True}])
        self.assertEqual(json.loads(self.facts.read_text()), self.record)
        self.assertIn("private-plugin-dependency", self.recipe.read_text())
        self.assertEqual(reconcile(self.source, facts_path=self.facts, synchronize=synchronize), [])
        self.assertEqual(len(calls), 1)

    def test_failed_native_transaction_preserves_existing_selection(self):
        before = self.facts.read_bytes(), self.recipe.read_bytes()

        def failed(**kwargs):
            raise RuntimeError("native transaction failed")

        with self.assertRaisesRegex(RuntimeError, "native transaction failed"):
            reconcile(self.source, facts_path=self.facts, synchronize=failed)
        self.assertEqual((self.facts.read_bytes(), self.recipe.read_bytes()), before)

    def test_success_without_core_reconciliation_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "still differ"):
            reconcile(self.source, facts_path=self.facts, synchronize=lambda **kw: None)

    def test_packaged_environment_without_writable_generation_needs_no_sync(self):
        self.facts.unlink()
        self.assertEqual(core_drift(self.source, self.facts), [])
        self.facts.write_text(json.dumps({"packages": {"venv": {"stamp": "image"}}}))
        self.assertEqual(core_drift(self.source, self.facts), [])

    def test_generation_recipe_cannot_escape_install(self):
        self.record["packages"]["venv"]["resolved_lock"] = str(self.source / "uv.lock")
        self.facts.write_text(json.dumps(self.record))
        with self.assertRaisesRegex(ValueError, "outside"):
            core_drift(self.source, self.facts)
