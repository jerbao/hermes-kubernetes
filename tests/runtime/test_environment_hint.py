"""Guarantee topology hints, address privacy, and fail-open environment exports."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from hermes_kubernetes.runtime.environment_hint import HINT_VARIABLE, apply, build


class EnvironmentHintTests(unittest.TestCase):
    def test_build_without_mcp_describes_boundaries_and_no_executor(self):
        # Cobre: mcp ausente + falha se inventar executor ou omitir limites.
        hint = build({"ssh_user": "fixture-user", "workspace": "/checkout"}, Path("/state"))

        for text in (
            "Kubernetes pod",
            "HERMES_HOME",
            "/state",
            "hermes-terminal",
            "fixture-user",
            "/checkout",
            "HERMES_WRITE_SAFE_ROOT",
            "authorization boundary",
            "Stop and ask",
            "No isolated MCP executor is configured",
        ):
            with self.subTest(text=text):
                self.assertIn(text, hint)

    def test_build_lists_only_configured_executors_and_workspace_roots(self):
        # Cobre: MCP configurado + falha se omitir roles/raiz ou inventar codegraph.
        instance = {
            "mcp": {
                "executor_targets": {"api": {}, "browser": {}},
                "workspace_target": {"roots": ["/workspace"]},
            }
        }

        hint = build(instance, Path("/state"))

        self.assertIn("executor pods (api, browser, workspace)", hint)
        self.assertIn("workspace executor mounts only: /workspace", hint)
        self.assertNotIn("codegraph", hint)
        self.assertNotIn("No isolated MCP executor", hint)

    def test_build_omits_addresses_and_unconfigured_users(self):
        # Cobre: dados privados ignorados + falha se expor host/IP ou outro usuário.
        for host in ("192.0.2.10", "2001:db8::10", "terminal.private.invalid"):
            with self.subTest(host=host):
                instance = {
                    "ssh_user": "fixture-user",
                    "workspace": "/checkout",
                    "terminal_host": host,
                    "name": "unrelated-user",
                    "mcp": {"host": "executor.private.invalid"},
                }

                hint = build(instance, Path("/state"))

                for private in (host, "executor.private.invalid", "unrelated-user"):
                    self.assertNotIn(private, hint)
                self.assertEqual(
                    hint,
                    "Installation topology. This is NOT stock Hermes; verify it with "
                    "`python3 hermes.py runtime info` in the terminal.\n"
                    "- The gateway and agent run in a Kubernetes pod. Live state "
                    "(HERMES_HOME) is a PVC mounted at /state; core code is at /opt/hermes. "
                    "Neither path exists on the terminal.\n"
                    "- Shell commands run on a separate SSH terminal "
                    "(alias hermes-terminal, user fixture-user). Its /checkout is a Git "
                    "checkout of the private instance repository, not the pod's "
                    "HERMES_HOME. Equal path names do not mean shared files.\n"
                    "- MCP servers run in separate executor pods (none configured), "
                    "reached over SSH from the gateway. They cannot see /state.\n"
                    "- The pod is never edited in place: a change goes through Git, "
                    "then a new pod rolls out. Live configuration goes through "
                    "`python3 hermes.py runtime exec -- hermes --profile NAME "
                    "config set KEY VALUE` "
                    "or the Desktop API. The terminal's config.yaml is a read-only projection.\n"
                    "- A write denied by HERMES_WRITE_SAFE_ROOT is an authorization boundary. "
                    "Stop and ask; do not retry it through the terminal, "
                    "Python or another tool.",
                )

    def test_apply_exports_when_absent_or_blank(self):
        # Cobre: variável ausente/branca + falha se não exportar ou alterar outras chaves.
        instance = {"ssh_user": "fixture-user"}
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory)
            (policy / "instance.json").write_text(json.dumps(instance))
            for initial in ({}, {HINT_VARIABLE: ""}, {HINT_VARIABLE: " \t\n"}):
                with self.subTest(initial=initial):
                    environ = {"KEEP": "unchanged", **initial}

                    apply(environ, Path("/state"), policy)

                    self.assertEqual(
                        environ,
                        {"KEEP": "unchanged", HINT_VARIABLE: build(instance, Path("/state"))},
                    )

    def test_apply_preserves_nonempty_operator_value_without_reading_policy(self):
        # Cobre: retorno antecipado + falha se sobrescrever valor ou ler policy ausente.
        with tempfile.TemporaryDirectory() as directory:
            environ = {HINT_VARIABLE: "  operator hint \n", "KEEP": "unchanged"}
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                apply(environ, Path("/state"), Path(directory))

            self.assertEqual(environ, {HINT_VARIABLE: "  operator hint \n", "KEEP": "unchanged"})
            self.assertEqual(stderr.getvalue(), "")

    def test_apply_missing_or_invalid_json_does_not_raise_or_export(self):
        # Cobre: falha aberta de leitura/JSON + falha se abortar ou exportar hint parcial.
        for content in (None, "{invalid json"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                policy = Path(directory)
                if content is not None:
                    (policy / "instance.json").write_text(content)
                environ = {"KEEP": "unchanged"}
                stderr = io.StringIO()

                with redirect_stderr(stderr):
                    apply(environ, Path("/state"), policy)

                self.assertEqual(environ, {"KEEP": "unchanged"})
                self.assertIn("Environment hint skipped:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
