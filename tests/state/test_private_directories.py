import json
from pathlib import Path
import tempfile
import unittest

import hermes_kubernetes.state.versioning as native


class IndexCachePrivateTests(unittest.TestCase):
    # Regressão: skills/.hub/index-cache/hermes-index.json (54 MB, cache da internet)
    # entrava no capture e check_content o recusava (> MAX_FILE) em loop.
    # Sem 'index-cache' em PRIVATE, estes testes falham.
    def home(self, directory):
        home = directory / "home"
        (home / "skills/exemplo").mkdir(parents=True)
        (home / "skills/exemplo/SKILL.md").write_text("legit skill")
        (home / "config.yaml").write_text("model: {default: test}\n")
        cache = home / "skills/.hub/index-cache"
        cache.mkdir(parents=True)
        with open(cache / "hermes-index.json", "wb") as big:
            big.truncate(native.MAX_FILE + 1)  # esparso: sem gastar disco
        return home

    def test_source_files_skips_index_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = self.home(Path(temporary).resolve())
            found = [p.relative_to(home).as_posix() for p in native.source_files(home / "skills")]
            self.assertIn("skills/exemplo/SKILL.md", found)
            self.assertEqual([p for p in found if "index-cache" in p], [])

    def test_capture_does_not_need_review_for_index_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            home = self.home(directory)
            result = native.capture(home, directory / "source", bundle=directory / "bundle")
            self.assertIn("index-cache", native.PRIVATE)
            self.assertIn("skills/exemplo/SKILL.md", json.dumps(result, default=str))
            self.assertNotIn("index-cache", json.dumps(result, default=str))


if __name__ == "__main__":
    unittest.main()
