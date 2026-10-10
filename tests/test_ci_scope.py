"""Personal edits must not schedule image/upstream suites; wrapper edits still get checked."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from ci_scope import scope

class ScopeTests(unittest.TestCase):
    def test_operator_audit_and_historical_sources_do_not_rebuild_image(self):
        result=scope(['scripts/audit_versioning.py','config/retired-automation.json',
                      'vm/history/cutover-traces.py','tests/test_ci_scope.py',
                      '.github/workflows/image.yml'])
        self.assertEqual(result,{'image':False,'project':True,'customizations':False})
    def test_pinned_backup_transport_still_rebuilds_image(self):
        self.assertTrue(scope(['config/rclone.lock.json'])['image'])
    def test_personalization_only_is_lightweight(self):
        self.assertEqual(scope(['customizations/default/skills/new/SKILL.md']),{'image':False,'project':False,'customizations':True})
    def test_terminal_controller_gets_project_checks(self):
        self.assertEqual(scope(['vm/version_workspace.py']),{'image':False,'project':True,'customizations':False})
    def test_runtime_change_gets_image_checks_and_optional_projection_check(self):
        self.assertEqual(scope(['runtime/versioned_state.py','customizations/second/config.json']),{'image':True,'project':True,'customizations':True})
