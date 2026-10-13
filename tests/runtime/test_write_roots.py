"""Guarantee validated terminal roots, stable merging, and fail-open write restrictions."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from hermes_kubernetes.runtime.write_roots import ROOT_VARIABLE, apply, roots


class WriteRootsTests(unittest.TestCase):
    def test_roots_accepts_home_subfolders_without_reordering(self):
        # Cobre: subpastas de home + falha se reordenar ou recusar /root e caminhos aninhados.
        configured = ["/home/u/code-repos", "/home/u/notes/a", "/root/work"]

        self.assertEqual(roots({"write_roots": configured}), configured)

    def test_roots_defaults_to_empty_list(self):
        # Cobre: chave ausente/lista vazia + falha se inventar raízes permitidas.
        for instance in ({}, {"write_roots": []}):
            with self.subTest(instance=instance):
                self.assertEqual(roots(instance), [])

    def test_roots_rejects_non_lists(self):
        # Cobre: tipo inválido + falha se aceitar string, objeto ou tupla como lista.
        for configured in (None, "/home/u/code-repos", {}, ("/home/u/code-repos",), 42):
            with self.subTest(configured=configured), self.assertRaises(ValueError):
                roots({"write_roots": configured})

    def test_roots_rejects_unsafe_or_non_string_entries(self):
        # Cobre: cada entrada + falha se aceitar homes, dot folders, traversal ou raízes externas.
        for root in (
            "",
            "relative/path",
            "/",
            "/home/u",
            "/root",
            "/home/u/work:/home/u",
            "/root/work:/root",
            "/home",
            "/home/u/.ssh-backup",
            "/home/u/.config/gh",
            "/home/u/.hermes",
            "/opt/work",
            "/srv/x",
            "..",
            "/home/u/../other",
            "/.ssh",
            "/home/u/.ssh",
            "/home/u/.ssh/id_ed25519",
            "/home/u/code-repos/.ssh",
            "/home/.ssh/u",
            None,
            42,
            Path("/home/u/code-repos"),
        ):
            with self.subTest(root=root), self.assertRaises(ValueError):
                roots({"write_roots": ["/home/u/code-repos", root]})

    def test_apply_appends_once_and_preserves_order(self):
        # Cobre: merge/idempotência + falha se revalidar raízes atuais, duplicar ou reordenar.
        for current, extra, expected in (
            (["/opt/data"], ["/home/u/code-repos"], ["/opt/data", "/home/u/code-repos"]),
            (
                ["/opt/data", "/home/u/code-repos"],
                ["/home/u/code-repos"],
                ["/opt/data", "/home/u/code-repos"],
            ),
            (
                ["/opt/cache", "/opt/data", "/opt/cache"],
                ["/home/u/code-repos", "/home/u/other", "/home/u/code-repos"],
                ["/opt/cache", "/opt/data", "/home/u/code-repos", "/home/u/other"],
            ),
        ):
            with self.subTest(current=current, extra=extra):
                with tempfile.TemporaryDirectory() as directory:
                    policy = Path(directory)
                    (policy / "instance.json").write_text(json.dumps({"write_roots": extra}))
                    environ = {"KEEP": "unchanged", ROOT_VARIABLE: os.pathsep.join(current)}

                    for _ in range(2):
                        apply(environ, policy)
                        self.assertEqual(
                            environ,
                            {"KEEP": "unchanged", ROOT_VARIABLE: os.pathsep.join(expected)},
                        )

    def test_apply_leaves_unrestricted_environment_unchanged(self):
        # Cobre: variável ausente/vazia + falha se transformar unrestricted em restrito.
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory)
            (policy / "instance.json").write_text(
                json.dumps({"write_roots": ["/home/u/code-repos"]})
            )
            for initial in ({}, {ROOT_VARIABLE: ""}):
                with self.subTest(initial=initial):
                    environ = {"KEEP": "unchanged", **initial}
                    before = environ.copy()
                    stderr = io.StringIO()

                    with redirect_stderr(stderr):
                        apply(environ, policy)

                    self.assertEqual(environ, before)
                    self.assertEqual(stderr.getvalue(), "")

    def test_apply_without_configured_roots_keeps_existing_restriction(self):
        # Cobre: chave ausente/lista vazia + falha se apagar a restrição já configurada.
        for instance in ({}, {"write_roots": []}):
            with self.subTest(instance=instance), tempfile.TemporaryDirectory() as directory:
                policy = Path(directory)
                (policy / "instance.json").write_text(json.dumps(instance))
                environ = {"KEEP": "unchanged", ROOT_VARIABLE: "/opt/data"}

                apply(environ, policy)

                self.assertEqual(environ, {"KEEP": "unchanged", ROOT_VARIABLE: "/opt/data"})

    def test_apply_missing_or_invalid_policy_does_not_raise_or_mutate(self):
        # Cobre: falha aberta/validação atômica + falha se abortar ou liberar parte da lista.
        contents = [None, "{invalid json"] + [
            json.dumps({"write_roots": configured})
            for configured in (
                None,
                "/home/u/code-repos",
                {},
                ["relative/path"],
                ["/"],
                ["/home/u/../other"],
                ["/home/u/.ssh"],
                ["/home/u/.ssh/id_ed25519"],
                ["/home/u/work:/home/u"],
                ["/root/work:/root"],
                [42],
                ["/home/u/code-repos", "/home/u/.ssh"],
                ["/home/u/code-repos", "/opt/data"],
            )
        ]
        for content in contents:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                policy = Path(directory)
                if content is not None:
                    (policy / "instance.json").write_text(content)
                environ = {
                    "KEEP": "unchanged",
                    ROOT_VARIABLE: os.pathsep.join(["/opt/data", "/opt/data"]),
                }
                before = environ.copy()
                stderr = io.StringIO()

                with redirect_stderr(stderr):
                    apply(environ, policy)

                self.assertEqual(environ, before)
                self.assertIn("Write roots skipped:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
