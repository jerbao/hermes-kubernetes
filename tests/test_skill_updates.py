"""Exercise managed skill upgrades using isolated Git repositories and native homes."""
import base64
import gzip
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'runtime'),str(ROOT/'scripts'),str(ROOT/'vm')]
from skill_updates import content, installed_update, merge, plan, snapshot
from reconcile_skills import reconcile
from version_workspace import PendingRuntime, SocketRuntime, reconcile as reconcile_workspace

TEXT = b'---\nname: guide\ndescription: Example.\n---\nfirst\n'+b'middle\n'*12+b'last\n'


def put(path, data=TEXT):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(data)


def git(root,*args):
    return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.PIPE,text=True).strip()


class SkillUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home,self.source,self.bundle = [self.root/name for name in ['home','source','bundle']]
        put(self.home/'config.yaml',b'{}\n')
        put(self.source/'skills/guide/SKILL.md')
        put(self.source/'source-provenance.json',b'{"project_revision":"old"}')
        put(self.home/'skills/guide/SKILL.md')
        put(self.bundle/'skills/management/SKILL.md',TEXT.replace(b'guide',b'management'))

    def upgrade(self):
        put(self.source/'source-provenance.json',b'{"project_revision":"new"}')

    def test_nonoverlapping_edits_merge_without_model(self):
        result = merge(TEXT,TEXT.replace(b'first',b'new first'),TEXT.replace(b'last',b'personal last'))
        self.assertIn(b'new first',result)
        self.assertIn(b'personal last',result)

    def test_initial_distribution_install_is_idempotent_and_new_profiles_receive_it(self):
        first = installed_update(self.home,self.source,self.bundle)
        self.assertEqual(first['updated_files'],1)
        self.assertEqual(installed_update(self.home,self.source,self.bundle)['updated_files'],0)
        put(self.home/'profiles/second/config.yaml',b'{}')
        self.assertEqual(installed_update(self.home,self.source,self.bundle)['updated_files'],1)
        self.assertTrue((self.home/'profiles/second/skills/management/SKILL.md').is_file())

    def test_root_distribution_update_preserves_profile_customization(self):
        installed_update(self.home,self.source,self.bundle)
        active = self.home/'skills/management/SKILL.md'
        active.write_bytes(active.read_bytes().replace(b'last',b'personal last'))
        template = self.bundle/'skills/management/SKILL.md'
        template.write_bytes(template.read_bytes().replace(b'first',b'new first'))
        installed_update(self.home,self.source,self.bundle)
        self.assertIn(b'new first',active.read_bytes())
        self.assertIn(b'personal last',active.read_bytes())

    def test_upstream_update_preserves_personal_edits(self):
        installed_update(self.home,self.source,self.bundle)
        active = self.home/'skills/guide/SKILL.md'
        active.write_bytes(TEXT.replace(b'last',b'personal last'))
        put(self.source/'skills/guide/SKILL.md',TEXT.replace(b'first',b'new first'))
        self.upgrade()
        installed_update(self.home,self.source,self.bundle)
        self.assertIn(b'new first',active.read_bytes())
        self.assertIn(b'personal last',active.read_bytes())

    def test_conflict_blocks_every_profile_and_keeps_original_baseline(self):
        installed_update(self.home,self.source,self.bundle)
        active = self.home/'skills/guide/SKILL.md'
        active.write_bytes(TEXT.replace(b'first',b'personal first'))
        management = self.home/'skills/management/SKILL.md'
        before = management.read_bytes()
        baseline = self.home/'state/versioning/managed-skills/bases.json.gz'
        original_baseline = baseline.read_bytes()
        put(self.source/'skills/guide/SKILL.md',TEXT.replace(b'first',b'upstream first'))
        put(self.bundle/'skills/management/SKILL.md',before.replace(b'first',b'new first'))
        self.upgrade()
        with self.assertRaisesRegex(ValueError,'blocked'):
            installed_update(self.home,self.source,self.bundle)
        self.assertEqual(management.read_bytes(),before)
        self.assertEqual(active.read_bytes(),TEXT.replace(b'first',b'personal first'))
        self.assertEqual(baseline.read_bytes(),original_baseline)
        self.assertNotIn(b'<<<<<<<',active.read_bytes())
        self.assertTrue((baseline.parent/'conflicts.json').is_file())

    def test_native_removal_is_not_resurrected(self):
        installed_update(self.home,self.source,self.bundle)
        shutil.rmtree(self.home/'skills/guide')
        shutil.rmtree(self.home/'skills/management')
        put(self.source/'skills/guide/SKILL.md',TEXT.replace(b'first',b'new first'))
        put(self.bundle/'skills/management/SKILL.md',TEXT.replace(b'guide',b'management').replace(b'first',b'new first'))
        self.upgrade()
        self.assertEqual(installed_update(self.home,self.source,self.bundle)['updated_files'],0)

    def test_upstream_removal_preserves_whole_skill_and_changed_reference_blocks_deletion(self):
        installed_update(self.home,self.source,self.bundle)
        shutil.rmtree(self.source/'skills/guide')
        self.upgrade()
        installed_update(self.home,self.source,self.bundle)
        self.assertEqual((self.home/'skills/guide/SKILL.md').read_bytes(),TEXT)
        with self.assertRaises(ValueError): merge(b'base',None,b'personal')

    def test_binary_conflict_is_not_merged(self):
        with self.assertRaises(ValueError): merge(b'\0base',b'\0new',b'\0personal')

    def test_moved_skill_requires_review_instead_of_installing_duplicate_name(self):
        previous = snapshot(self.source/'skills')
        desired = {name.replace('/guide/','/moved/'):item for name,item in previous.items()}
        self.assertFalse(plan(previous,desired,previous)['ready'])

    def test_seed_records_base_without_modifying_installed_skills(self):
        before = (self.home/'skills/guide/SKILL.md').read_bytes()
        report = installed_update(self.home,self.source,self.bundle,seed_only=True)
        self.assertEqual(report['updated_files'],0)
        self.assertEqual((self.home/'skills/guide/SKILL.md').read_bytes(),before)

    def test_seed_uses_installed_project_base_and_never_replaces_recorded_base(self):
        base = snapshot(self.bundle/'skills')
        template = self.bundle/'skills/management/SKILL.md'
        template.write_bytes(template.read_bytes().replace(b'first',b'stale image first'))
        installed_update(self.home,self.source,self.bundle,seed_only=True,distribution_baseline=base)
        state = self.home/'state/versioning/managed-skills/bases.json.gz'
        self.assertEqual(json.loads(gzip.decompress(state.read_bytes()))['distribution'],base)
        before = state.read_bytes()
        report = installed_update(self.home,self.source,self.bundle,seed_only=True)
        self.assertTrue(report['already_recorded'])
        self.assertEqual(state.read_bytes(),before)

    def project(self):
        project = self.root/'project'
        project.mkdir()
        for path in [project,self.source]:
            git(path,'init','-q','-b','main')
            git(path,'config','user.name','Fixture')
            git(path,'config','user.email','fixture@example.test')
        git(self.source,'add','skills')
        git(self.source,'commit','-qm','base')
        lock = {'schema':1,'revision':git(self.source,'rev-parse','HEAD'),
                'repository':str(self.source),'patches':[],'native_tests':[]}
        put(project/'upstream.lock.json',json.dumps(lock).encode())
        put(project/'project-files.json',b'{"schema":1,"skills":["management"],"runtime_files":[]}')
        shutil.copytree(self.bundle/'skills',project/'skills')
        put(project/'customizations/default/skills/management/SKILL.md',
            (self.bundle/'skills/management/SKILL.md').read_bytes().replace(b'last',b'personal last'))
        put(project/'customizations/default/installations.json',b'{"schema":1,"plugins":{},"links":{},"upstream_skills":[]}')
        git(project,'add','.')
        git(project,'commit','-qm','base')
        return project,git(project,'rev-parse','HEAD')

    def test_candidate_rebases_distribution_override_and_write_is_explicit(self):
        project, base = self.project()
        template = project/'skills/management/SKILL.md'
        put(template,template.read_bytes().replace(b'first',b'new first'))
        active = project/'customizations/default/skills/management/SKILL.md'
        before = active.read_bytes()
        self.assertTrue(reconcile(project,self.source,base)['ready'])
        self.assertEqual(active.read_bytes(),before)
        result = reconcile(project,self.source,base,write=True)
        self.assertGreater(result['written_files'],0)
        self.assertIn(b'new first',active.read_bytes())
        self.assertIn(b'personal last',active.read_bytes())

    def test_candidate_conflict_leaves_entire_git_projection_untouched(self):
        project, base = self.project()
        template = project/'skills/management/SKILL.md'
        put(template,template.read_bytes().replace(b'last',b'upstream last'))
        before = {p:p.read_bytes() for p in (project/'customizations').rglob('*') if p.is_file()}
        report = reconcile(project,self.source,base,write=True)
        self.assertFalse(report['ready'])
        self.assertEqual(report['written_files'],0)
        self.assertEqual({p:p.read_bytes() for p in before},before)

    def test_release_candidate_rebases_official_skill_from_previous_patched_tree(self):
        project, base = self.project()
        active = project/'customizations/default/skills/guide/SKILL.md'
        put(active,TEXT.replace(b'last',b'personal last'))
        git(project,'add','customizations')
        git(project,'commit','-qm','personal official skill')
        base = git(project,'rev-parse','HEAD')
        put(self.source/'skills/guide/SKILL.md',TEXT.replace(b'first',b'upstream first'))
        git(self.source,'add','skills')
        git(self.source,'commit','-qm','new official release')
        lock = json.loads((project/'upstream.lock.json').read_text())
        lock['revision'] = git(self.source,'rev-parse','HEAD')
        put(project/'upstream.lock.json',json.dumps(lock).encode())
        self.assertTrue(reconcile(project,self.source,base,write=True)['ready'])
        self.assertIn(b'upstream first',active.read_bytes())
        self.assertIn(b'personal last',active.read_bytes())

    def test_controller_waits_without_overwriting_projection_when_image_is_old(self):
        class WaitingRuntime:
            def capture(self): raise PendingRuntime('old image')
            def apply(self, changes): raise AssertionError('Must not apply')
        project = self.root/'waiting';project.mkdir()
        put(project/'customizations/default/skills/own/SKILL.md')
        result = reconcile_workspace(project,WaitingRuntime())
        self.assertEqual(result['status'],'waiting-for-runtime')
        self.assertFalse((project/'.local/versioning/baseline.json').exists())


if __name__ == '__main__':
    unittest.main()
