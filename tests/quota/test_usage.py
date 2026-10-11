"""Quota contracts, failure isolation and the read-only HTTP boundary."""

from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hermes_kubernetes.quota.configuration import prepare
from hermes_kubernetes.quota.inventory import usage_for, apply_usage
from hermes_kubernetes.quota.normalization import normalize
from hermes_kubernetes.quota.proxy import ProxyClient
from hermes_kubernetes.quota.server import Cache, handler


class UsageTests(unittest.TestCase):
    def test_optional_claude_model_windows_are_reported_only_when_present(self):
        base = {"five_hour": {"utilization": 20}, "seven_day": {"utilization": 40}}
        self.assertEqual(
            [item["label"] for item in normalize("claude", base)], ["5 hours", "7 days"]
        )
        reported = {**base, "seven_day_fable": {"utilization": 15}, "seven_day_other": None}
        self.assertEqual(
            normalize("claude", reported)[-1],
            {
                "label": "Fable · 7 days",
                "used_percent": 15,
                "resets_at": None,
                "scope": "model",
            },
        )

    def test_authoritative_windows_and_fractional_percentages(self):
        result = normalize(
            "codex",
            {
                "rate_limit": {
                    "primary_window": {
                        "limit_window_seconds": 604800,
                        "used_percent": 0,
                        "reset_at": 1800000000,
                    }
                }
            },
        )
        self.assertEqual(result[0]["label"], "7 days")
        self.assertEqual(result[0]["used_percent"], 0)
        self.assertTrue(result[0]["resets_at"].endswith("+00:00"))
        result = normalize(
            "claude", {"five_hour": {"utilization": 0.5}, "seven_day_opus": {"utilization": 100}}
        )
        self.assertEqual(result[0]["used_percent"], 0.5)
        self.assertEqual(result[1]["scope"], "model")
        result = normalize(
            "gemini",
            {
                "buckets": [
                    {"modelId": "flash", "remainingFraction": 1},
                    {"modelId": "pro", "remainingFraction": 0},
                ]
            },
        )
        self.assertEqual([w["used_percent"] for w in result], [0, 100])

    def test_antigravity_summary_uses_weekly_and_five_hour_buckets(self):
        payload = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {
                            "window": "weekly",
                            "remainingFraction": 0.8,
                            "resetTime": "2026-10-18T12:00:00Z",
                        },
                        {"window": "5h", "remainingFraction": 0.9},
                        {"window": "5h", "remainingFraction": None},
                    ],
                }
            ]
        }
        calls = []

        def request(url, *, token, data):
            calls.append(data)
            self.assertEqual(
                data["url"],
                "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
            )
            self.assertEqual(json.loads(data["data"]), {"project": "project"})
            self.assertEqual(data["header"]["Authorization"], "Bearer $TOKEN$")
            self.assertIn("antigravity/cli/", data["header"]["User-Agent"])
            return {"status_code": 200, "body": json.dumps(payload)}

        result = ProxyClient("http://127.0.0.1:8317", "secret", request).account(
            {"provider": "antigravity", "auth_index": "google", "project_id": "project"}
        )
        self.assertEqual(result[0], "gemini")
        self.assertEqual(result[1]["state"], "ready")
        windows = result[1]["windows"]
        self.assertEqual([w["used_percent"] for w in windows], [20, 10])
        self.assertEqual(
            [w["label"] for w in windows], ["Gemini Models · 7 days", "Gemini Models · 5 hours"]
        )
        self.assertTrue(all(w["scope"] == "model" for w in windows))
        self.assertTrue(windows[0]["resets_at"].endswith("+00:00"))

    def test_unknown_values_are_not_zero(self):
        self.assertEqual(normalize("codex", {"rate_limit": {"primary_window": {}}}), [])
        self.assertEqual(normalize("claude", {"five_hour": {"utilization": True}}), [])
        self.assertEqual(
            normalize(
                "gemini",
                {"buckets": [{"remainingFraction": float("nan")}, {"remainingFraction": 1.2}]},
            ),
            [],
        )

    def test_proxy_substitutes_credentials_and_isolates_failures(self):
        calls = []

        def request(url, *, token, data):
            calls.append((url, token, data))
            if url.endswith("auth-files"):
                return {
                    "files": [
                        {
                            "provider": "codex",
                            "auth_index": "a",
                            "email": "private@example.com",
                            "id_token": {"chatgpt_account_id": "principal"},
                        },
                        {"provider": "claude", "auth_index": "b"},
                        {"provider": "gemini", "auth_index": "c", "project_id": "project"},
                        {"provider": "claude", "auth_index": "d", "disabled": True},
                    ]
                }
            if data["auth_index"] == "b":
                return {"status_code": 401, "body": "sensitive upstream error"}
            if data["auth_index"] == "c":
                self.assertEqual(json.loads(data["data"]), {"project": "project"})
                return {
                    "status_code": 200,
                    "body": json.dumps({"buckets": [{"remainingFraction": 0.3}]}),
                }
            self.assertEqual(data["header"]["ChatGPT-Account-ID"], "principal")
            self.assertEqual(data["header"]["Authorization"], "Bearer $TOKEN$")
            return {
                "status_code": 200,
                "body": json.dumps({"rate_limit": {"primary_window": {"used_percent": 23}}}),
            }

        payload = ProxyClient("http://127.0.0.1:8317", "secret", request).collect()
        self.assertEqual(
            payload["providers"]["codex"]["accounts"][0]["windows"][0]["used_percent"], 23
        )
        self.assertEqual(payload["providers"]["claude"]["accounts"][0]["state"], "unavailable")
        serialized = json.dumps(payload)
        for forbidden in (
            "private@example.com",
            "principal",
            "secret",
            "sensitive upstream error",
            "auth_index",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(len(calls), 4)  # Inventory + three enabled credentials, no inference.

    def test_stale_and_unavailable_data_clear_gauges(self):
        payload = {
            "fetched_at": (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(),
            "providers": {
                "codex": {
                    "accounts": [
                        {
                            "id": "a",
                            "label": "A",
                            "state": "ready",
                            "windows": [{"used_percent": 10}],
                        }
                    ]
                }
            },
        }
        row = usage_for(payload, "codex")["accounts"][0]
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["windows"], [])
        self.assertEqual(
            usage_for({"providers": {}}, "claude")["accounts"][0]["state"], "unavailable"
        )

    def test_http_service_cannot_execute_arbitrary_management_operations(self):
        cache = Cache(None)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(cache))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(url + "/v1/usage") as response:
                self.assertEqual(json.load(response)["status"], "pending")
            for request in (
                url + "/v0/management/auth-files",
                url + "/v1/usage?url=https://example.com",
                Request(url + "/v1/usage", data=b"{}"),
            ):
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request)
                caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_management_configuration_stays_local_and_preserves_proxy_settings(self):
        import yaml

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").write_text(
                "server: {port: 8317}\noauth: {auth-dir: /auth}\nmanagement: {}\n"
            )
            (root / "key").write_text("a" * 40)
            prepare(root / "config", root / "key", root / "output")
            value = yaml.safe_load((root / "output").read_text())
            self.assertFalse(value["management"]["allow-remote"])
            self.assertEqual(value["oauth"]["auth-dir"], "/auth")
            self.assertEqual((root / "output").stat().st_mode & 0o777, 0o600)

    def test_native_picker_opt_in_and_custom_rows_only(self):
        import sys
        from types import ModuleType

        config = ModuleType("hermes_cli.config")
        config.load_config = lambda: {
            "proxy_usage": {"url": "http://quota:8318", "providers": {"proxy": "codex"}}
        }
        constants = ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: "/profile"
        rows = [
            {"slug": "proxy", "is_user_defined": True},
            {"slug": "anthropic", "usage": {"windows": []}},
        ]
        payload = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "providers": {"codex": {"accounts": [{"id": "a", "state": "ready", "windows": []}]}},
        }
        with (
            patch.dict(sys.modules, {"hermes_cli.config": config, "hermes_constants": constants}),
            patch("hermes_kubernetes.quota.inventory.cached", return_value=payload),
        ):
            apply_usage(rows)
        self.assertEqual(rows[0]["usage"]["accounts"][0]["id"], "a")
        self.assertEqual(rows[1]["usage"], {"windows": []})


if __name__ == "__main__":
    unittest.main()
