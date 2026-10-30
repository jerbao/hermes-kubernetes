import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from hermes_kubernetes.mcp.routing import (  # noqa: E402
    RoutingError,
    executor_target,
    extract_executor,
    load_routing_catalog,
    package_identities,
    prepare_definition,
    resolve_executor,
    validate_http_server,
)
from hermes_kubernetes.runtime.configuration import restore_mcp  # noqa: E402
from hermes_kubernetes.mcp.bridge import REMOTE, build_mcp_command  # noqa: E402


class McpRoutingTests(unittest.TestCase):
    def test_remote_exit_reaps_stdin_relay_without_waiting_for_caller_eof(self):
        child = "import sys;sys.stdin.buffer.readline();sys.exit(23)"
        code = (
            f"import sys;sys.path.insert(0,{str(ROOT / 'runtime')!r});"
            f"from hermes_kubernetes.mcp.bridge import forward;sys.exit(forward([sys.executable,'-c',{child!r}],{{}}))"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            # Keep caller stdin open, reproducing a server that dies first.
            self.assertEqual(process.wait(timeout=10), 23)
            self.assertNotIn(b"Fatal Python error", process.stderr.read())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()

    def test_relay_preserves_binary_input_until_eof(self):
        child = "import sys;sys.stdin.buffer.readline();sys.stdout.buffer.write(sys.stdin.buffer.read())"
        code = (
            f"import sys;sys.path.insert(0,{str(ROOT / 'runtime')!r});"
            f"from hermes_kubernetes.mcp.bridge import forward;sys.exit(forward([sys.executable,'-c',{child!r}],{{}}))"
        )
        payload = bytes(range(256)) * 4096
        result = subprocess.run(
            [sys.executable, "-c", code], input=payload, capture_output=True, timeout=10
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, payload)

    def setUp(self):
        self.catalog = load_routing_catalog()

    def test_selector_is_removed_and_preserves_real_argument_order(self):
        executor, args = extract_executor(["--before", "--hermes-executor=browser", "--after"])
        self.assertEqual(executor, "browser")
        self.assertEqual(args, ["--before", "--after"])

    def test_selector_rejects_invalid_or_duplicate_values(self):
        for args in (
            ["--hermes-executor=terminal"],
            ["--hermes-executor=api", "--hermes-executor=vm"],
            ["--hermes-executor", "api"],
        ):
            with self.subTest(args=args), self.assertRaises(RoutingError):
                extract_executor(args)

    def test_explicit_selector_precedes_package_override_and_workspace_path(self):
        definition = {
            "command": "npx",
            "args": [
                "--hermes-executor=vm",
                "-y",
                "@colbymchenry/codegraph@2.0.0",
                "/workspace/repo",
            ],
        }
        policy = {
            "mcp": {
                "servers": {"graph": {"host": "legacy-mcp"}},
                "workspace_target": {"roots": ["/workspace"]},
            }
        }
        executor, legacy, clean = resolve_executor("graph", definition, policy, self.catalog)
        self.assertEqual((executor, legacy), ("vm", None))
        self.assertEqual(clean, ["-y", "@colbymchenry/codegraph@2.0.0", "/workspace/repo"])

    def test_package_identity_beats_name_and_policy_override(self):
        definition = {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}
        policy = {"mcp": {"servers": {"custom": {"host": "legacy-mcp"}}}}
        executor, legacy, _ = resolve_executor("custom", definition, policy, self.catalog)
        self.assertEqual((executor, legacy), ("browser", None))
        self.assertEqual(
            package_identities("npx", ["-y", "@playwright/mcp@latest"]),
            {"@playwright/mcp"},
        )

    def test_all_configured_stdio_package_identities_have_executor_routes(self):
        configured = [
            ("chrome-devtools", ["npx", "-y", "chrome-devtools-mcp@latest"], "browser"),
            ("codegraph", ["codegraph"], "workspace"),
            ("context7", ["npx", "-y", "@upstash/context7-mcp@latest"], "api"),
            ("github", ["npx", "-y", "@modelcontextprotocol/server-github"], "api"),
            ("interjato-gitlab", ["npx", "-y", "@zereight/mcp-gitlab@latest"], "api"),
            ("playwright", ["npx", "-y", "@playwright/mcp@latest"], "browser"),
            (
                "sequential_thinking",
                ["npx", "-y", "@modelcontextprotocol/server-sequential-thinking"],
                "api",
            ),
        ]
        for name, command, expected in configured:
            with self.subTest(name=name):
                executor, _, _ = resolve_executor(
                    name, {"command": command[0], "args": command[1:]}, {}, self.catalog
                )
                self.assertEqual(executor, expected)

    def test_validation_profile_union_covers_catalog_and_all_configured_mcp_names(self):
        fixture_path = ROOT / "tests/fixtures/mcp-validation-profile.json"
        fixture = json.loads(fixture_path.read_text())
        configured = fixture["servers"]
        catalog_names = set(self.catalog["catalog"])
        configured_names = {entry["name"] for entry in configured}
        self.assertEqual(len(catalog_names), 66)  # upstream v0.22.5 catalog adds rentahuman
        self.assertEqual(len(configured_names), 10)
        self.assertEqual(catalog_names & configured_names, {"aws-knowledge", "context7", "gitlab"})
        self.assertEqual(len(catalog_names | configured_names), 73)

        for entry in configured:
            if entry["transport"] == "http":
                self.assertEqual(entry["identity"], "provider-managed")
                continue
            identity = entry["identity"].removeprefix("npm:")
            definition = (
                {"command": "codegraph", "args": []}
                if entry["name"] == "codegraph"
                else {"command": "npx", "args": ["-y", identity]}
            )
            executor, _, _ = resolve_executor(entry["name"], definition, {}, self.catalog)
            package_ids = package_identities("npx", ["-y", identity])
            self.assertTrue(package_ids <= set(self.catalog["packages"]))
            self.assertIn(executor, {"api", "workspace", "browser"})

    def test_explicit_and_catalog_routes_select_their_real_target_not_legacy_host(self):
        definition = {
            "command": "npx",
            "args": ["--hermes-executor=browser", "-y", "custom-mcp"],
        }
        policy = {
            "mcp": {
                "host": "legacy-api",
                "servers": {"browser-one": {"host": "legacy-custom"}},
                "executor_targets": {"browser": {"host": "browser-pod"}},
            }
        }
        executor, legacy, _ = resolve_executor("browser-one", definition, policy, self.catalog)
        self.assertEqual((executor, legacy), ("browser", None))
        self.assertEqual(
            executor_target(policy, executor, "browser-one", legacy)["host"],
            "browser-pod",
        )
        auto = {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}
        self.assertEqual(
            resolve_executor("browser-one", auto, policy, self.catalog)[:2],
            ("browser", None),
        )

    def test_shared_executor_host_key_alias_is_preserved_for_non_vm_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = root / "policy"
            policy.mkdir()
            (policy / "instance.json").write_text(
                json.dumps({"mcp": {"executor_targets": {"browser": {"host": "browser-pod"}}}})
            )
            command = build_mcp_command(root, policy, "custom", executor="browser")
            self.assertEqual(
                command[command.index("HostKeyAlias=hermes-mcp")],
                "HostKeyAlias=hermes-mcp",
            )

    def test_packaged_binary_and_native_preset_are_mapped_by_real_identity(self):
        codegraph = resolve_executor(
            "custom", {"command": "codegraph", "args": []}, {}, self.catalog
        )[0]
        codex = resolve_executor(
            "custom", {"command": "codex", "args": ["mcp-server"]}, {}, self.catalog
        )[0]
        self.assertEqual(codegraph, "workspace")
        self.assertEqual(codex, "vm")

    def test_unrecognized_custom_command_uses_legacy_override_before_path_policy(self):
        definition = {"command": "node", "args": ["/workspace/project/index.js"]}
        policy = {
            "mcp": {
                "servers": {"private": {"host": "custom-mcp"}},
                "workspace_target": {"roots": ["/workspace"]},
            }
        }
        executor, legacy, _ = resolve_executor("private", definition, policy, self.catalog)
        self.assertEqual((executor, legacy), ("api", "private"))

    def test_plugin_payload_is_not_sent_to_public_api_without_explicit_route(self):
        with self.assertRaisesRegex(RoutingError, "plugin directory"):
            resolve_executor(
                "plugin-server",
                {
                    "command": "node",
                    "args": ["/home/agent/.hermes/plugins/local/server.js"],
                },
                {},
                self.catalog,
            )
        with self.assertRaisesRegex(RoutingError, "plugin directory"):
            resolve_executor(
                "plugin-server",
                {
                    "command": "npx",
                    "args": ["custom-package"],
                    "env": {"PLUGIN_DATA": "/home/agent/.hermes/plugin-data/local"},
                },
                {},
                self.catalog,
            )

    def test_custom_path_uses_workspace_with_a_boundary_check(self):
        policy = {"mcp": {"workspace_target": {"roots": ["/workspace/project"]}}}
        selected = resolve_executor(
            "custom",
            {"command": "node", "args": ["file:/workspace/project/app.js"]},
            policy,
            self.catalog,
        )[0]
        sibling = resolve_executor(
            "custom",
            {"command": "node", "args": ["/workspace/project-old/app.js"]},
            policy,
            self.catalog,
        )[0]
        self.assertEqual(selected, "workspace")
        self.assertEqual(sibling, "api")

    def test_unknown_stdio_defaults_to_api_and_catalog_map_has_typed_identities(self):
        executor, _, _ = resolve_executor(
            "custom", {"command": "python", "args": ["server.py"]}, {}, self.catalog
        )
        self.assertEqual(executor, "api")
        for name, entry in self.catalog["catalog"].items():
            self.assertIn(entry["transport"], {"http", "stdio"}, name)
            self.assertIsInstance(entry["identity"], dict, name)
            if entry.get("executor") is not None:
                self.assertIn(entry["executor"], {"api", "workspace", "browser", "vm"}, name)

    def test_loopback_catalog_endpoint_fails_with_local_editor_message(self):
        with self.assertRaisesRegex(RoutingError, "local loopback endpoint"):
            validate_http_server(
                "unreal-engine", {"url": "http://127.0.0.1:8000/mcp"}, self.catalog
            )
        with self.assertRaisesRegex(RoutingError, "local loopback endpoint"):
            restore_mcp({"mcp_servers": {"unreal-engine": {"url": "http://127.0.0.1:8000/mcp"}}})

    def test_vm_route_needs_its_own_target_and_mcp_ssh_material(self):
        with self.assertRaisesRegex(RoutingError, "mcp.executor_targets.vm"):
            executor_target({"mcp": {"host": "api-mcp"}}, "vm", "codex")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = root / "policy"
            policy.mkdir()
            (policy / "instance.json").write_text(
                json.dumps(
                    {
                        "mcp": {
                            "executor_targets": {
                                "vm": {"host": "hermes-vm", "user": "mcp", "port": 2222}
                            }
                        }
                    }
                )
            )
            with self.assertRaisesRegex(RoutingError, "separately provisioned"):
                build_mcp_command(root, policy, "codex", executor="vm")

    def test_vm_route_uses_only_mcp_key_and_explicit_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = root / "policy"
            policy.mkdir()
            (policy / "instance.json").write_text(
                json.dumps(
                    {
                        "terminal_host": "terminal-host",
                        "mcp": {
                            "executor_targets": {
                                "vm": {"host": "hermes-vm", "user": "mcp", "port": 2222}
                            }
                        },
                    }
                )
            )
            key_dir = root / ".ssh/mcp"
            key_dir.mkdir(parents=True)
            (key_dir / "id_ed25519").write_text("fixture")
            (key_dir / "known_hosts").write_text("fixture")
            command = build_mcp_command(root, policy, "codex", executor="vm")
            self.assertIn("mcp@hermes-vm", command)
            self.assertEqual(command[command.index("-i") + 1], str(key_dir / "id_ed25519"))
            self.assertNotIn("terminal-host", " ".join(command))

    def test_no_executor_configuration_never_falls_back_to_terminal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = root / "policy"
            policy.mkdir()
            (policy / "instance.json").write_text(json.dumps({"terminal_host": "terminal-host"}))
            with self.assertRaisesRegex(RoutingError, "terminal SSH fallback"):
                build_mcp_command(root, policy, "custom", executor="api")

    def test_json_definition_consumes_selector_before_remote_server_argv(self):
        imported = json.loads(
            '{"command":"' + sys.executable + '","args":['
            '"--hermes-executor=api","-c",'
            '"import json,sys;print(json.dumps(sys.argv[1:]))","real-argument"]}'
        )
        policy = {"mcp": {"host": "api-mcp"}}
        executor, legacy, clean = prepare_definition("custom", imported, policy, self.catalog)
        self.assertEqual((executor, legacy), ("api", None))
        # REMOTE defaults its child cwd to ~/.hermes, as the installed runtime does.
        # CI starts with a clean HOME, so create the isolated workspace it expects.
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / ".hermes").mkdir()
            result = subprocess.run(
                [sys.executable, "-u", "-c", REMOTE],
                input=(json.dumps(clean) + "\n").encode(),
                capture_output=True,
                env={**os.environ, "HOME": str(home)},
                timeout=10,
                check=True,
            )
        self.assertEqual(json.loads(result.stdout), ["real-argument"])


if __name__ == "__main__":
    unittest.main()
