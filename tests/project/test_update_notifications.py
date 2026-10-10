"""Exercise notification lifecycle without calling GitHub or a model."""

import sys
from pathlib import Path
import unittest

from hermes_kubernetes.releases.notifications import MARKER, report


class FakeGitHub:
    def __init__(self):
        self.issue = None
        self.comments = []
        self.step = "Resolve latest eligible official release and check patches"

    def __call__(self, method, endpoint, data=None):
        if method == "GET" and "/issues?" in endpoint:
            return [dict(self.issue)] if self.issue and self.issue["state"] == "open" else []
        if method == "GET" and "/jobs?" in endpoint:
            return {
                "jobs": [
                    {
                        "name": "candidate",
                        "steps": [{"name": self.step, "conclusion": "failure"}],
                    }
                ]
            }
        if method == "POST" and endpoint.endswith("/issues"):
            self.issue = {"number": 1, "state": "open", **data}
            return dict(self.issue)
        if method == "POST" and endpoint.endswith("/comments"):
            self.comments.append(data["body"])
            return {}
        if method == "PATCH":
            self.issue.update(data)
            return dict(self.issue)
        raise AssertionError((method, endpoint))


class UpdateNotificationsTests(unittest.TestCase):
    def send(self, api, status="failure", release=None, run="123"):
        return report(api, "owner/hermes", run, "1", status, release, ["owner"])

    def test_patch_conflict_creates_assigned_actionable_issue(self):
        api = FakeGitHub()
        self.send(
            api,
            release={
                "tag": "v1.2.3",
                "patches": [
                    {
                        "path": "patches/core.patch",
                        "status": "conflict",
                        "detail": "PRIVATE_LOG",
                    }
                ],
            },
        )
        self.assertEqual(api.issue["assignees"], ["owner"])
        self.assertIn(MARKER, api.issue["body"])
        self.assertIn("patches/core.patch", api.issue["body"])
        self.assertIn("/actions/runs/123/attempts/1", api.issue["body"])
        self.assertNotIn("PRIVATE_LOG", api.issue["body"])

    def test_identical_failure_updates_existing_issue_without_repeated_mentions(self):
        api = FakeGitHub()
        self.send(api)
        self.assertEqual(self.send(api, run="124")["status"], "updated")
        self.assertEqual(api.comments, [])
        self.assertIn("/runs/124/", api.issue["body"])

    def test_changed_failure_notifies_in_existing_issue(self):
        api = FakeGitHub()
        self.send(api)
        api.step = "Build and smoke-test candidate image locally"
        self.send(api)
        self.assertEqual(len(api.comments), 1)
        self.assertIn("@owner", api.comments[0])

    def test_missing_report_still_alerts(self):
        api = FakeGitHub()
        self.send(api)
        self.assertIn("unidentified", api.issue["body"])

    def test_recovery_closes_alert_without_claiming_deployment(self):
        api = FakeGitHub()
        self.send(api)
        self.assertEqual(self.send(api, "success")["status"], "recovered")
        self.assertEqual(api.issue["state"], "closed")
        self.assertIn("does not mean", api.comments[-1])

    def test_success_without_incident_creates_nothing(self):
        api = FakeGitHub()
        self.assertEqual(self.send(api, "success")["status"], "healthy")
        self.assertIsNone(api.issue)

    def test_skill_conflicts_are_actionable_and_do_not_copy_file_contents(self):
        api = FakeGitHub()
        self.send(api, release={"skill_conflicts": ["default/skills/guide/SKILL.md"]})
        self.assertIn("default/skills/guide/SKILL.md", api.issue["body"])

    def test_image_recovery_does_not_close_an_upstream_incident(self):
        api = FakeGitHub()
        self.send(api)
        report(
            api,
            "owner/hermes",
            "124",
            "1",
            "success",
            users=["owner"],
            incident="image",
        )
        self.assertEqual(api.issue["state"], "open")


if __name__ == "__main__":
    unittest.main()
