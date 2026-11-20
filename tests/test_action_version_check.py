import io
import json
import os
import tempfile
import unittest
from base64 import b64encode
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from action_version_check import COMMENT_MARKER, Row, code, main, report, update_comment
from github_api import GitHub, GitHubError
from release_check import ReleaseChecker, Result
from workflow_scan import Reference, Scanner, WorkspaceSource


UPSTREAM = "/repos/owner/action"
REPOSITORY = "caller/repo"
OLD_SHA, NEW_SHA, TAG_SHA, AHEAD_SHA, UNRELATED_SHA = (char * 40 for char in "abcde")


def release(tag, date, *, draft=False, prerelease=False):
    return {"tag_name": tag, "published_at": date, "draft": draft, "prerelease": prerelease}


def target(sha, kind="commit"):
    return {"object": {"type": kind, "sha": sha}}


def tree_entry(path, sha, *, directory=False):
    return {"path": path, "sha": sha, "mode": "040000" if directory else "100644", "type": "tree" if directory else "blob"}


class FixtureGitHub(GitHub):
    def __init__(self, routes):
        self.routes = routes
        self.writes = []

    def request(self, method, path, body=None):
        if method != "GET":
            self.writes.append((method, path, body))
        try:
            response = self.routes[method, path] if method != "GET" else self.routes[path]
        except KeyError as error:
            raise AssertionError(f"Unexpected call {method} {path}") from error
        if isinstance(response, Exception):
            raise response
        return response


def upstream_routes():
    return {
        f"{UPSTREAM}/releases?per_page=100&page=1": [
            release("v9.0.0", "2026-11-01", draft=True),
            release("v8.0.0-rc.1", "2026-11-01", prerelease=True),
            release("v1.12.0", "2026-10-01"),
            release("v1.9.0", "2026-02-01"),
            release("v2.10.0", "2025-10-01"),
            release("v2.9.0", "2026-01-01"),
        ],
        f"{UPSTREAM}/git/ref/tags/v2.10.0": target(TAG_SHA, "tag"),
        f"{UPSTREAM}/git/tags/{TAG_SHA}": target(NEW_SHA),
        f"{UPSTREAM}/git/ref/tags/v2": target(NEW_SHA),
        f"{UPSTREAM}/git/ref/tags/v2.9.0": target(NEW_SHA),
        f"{UPSTREAM}/git/ref/tags/v1.9.0": target(OLD_SHA),
        f"{UPSTREAM}/git/ref/tags/main": GitHubError("GitHub API returned HTTP 404", 404),
        f"{UPSTREAM}/contents?ref={NEW_SHA}": [{"name": "action.yml", "type": "file"}],
        f"{UPSTREAM}/compare/{OLD_SHA}...{NEW_SHA}?per_page=1": {"status": "diverged"},
        f"{UPSTREAM}/compare/{AHEAD_SHA}...{NEW_SHA}?per_page=1": {"status": "behind"},
        f"{UPSTREAM}/compare/{UNRELATED_SHA}...{NEW_SHA}?per_page=1": {"status": "diverged"},
        f"{UPSTREAM}/tags?per_page=100&page=1": [
            {"name": "v1.9.0", "commit": {"sha": OLD_SHA}},
            {"name": "unreleased", "commit": {"sha": UNRELATED_SHA}},
        ],
    }


class ReleaseCheckerTests(unittest.TestCase):
    def test_stable_releases_and_commit_pins(self):
        checker = ReleaseChecker(FixtureGitHub(upstream_routes()))
        for ref, status in {
            "v2": "current",
            NEW_SHA.upper(): "current",
            "v2.9.0": "update",
            "v1.9.0": "update",
            OLD_SHA: "update",
            AHEAD_SHA: "unchecked",
            UNRELATED_SHA: "unchecked",
            "main": "unchecked",
        }.items():
            with self.subTest(ref=ref):
                result = checker.check(Reference("ci.yml", 1, f"Owner/Action@{ref}", False))
                self.assertEqual(result.status, status)
                if status != "unchecked":
                    self.assertEqual((result.tag, result.sha), ("v2.10.0", NEW_SHA))

    def test_legacy_tags_do_not_make_backports_newer_than_the_latest_major(self):
        routes = upstream_routes()
        routes[f"{UPSTREAM}/releases?per_page=100&page=1"].append(release("v1-beta", "2024-01-01"))
        routes[f"{UPSTREAM}/releases/latest"] = release("v2.10.0", "2025-10-01")
        checker = ReleaseChecker(FixtureGitHub(routes))
        result = checker.check(Reference("ci.yml", 1, "owner/action@v2", False))
        self.assertEqual(result, Result("current", tag="v2.10.0", sha=NEW_SHA))

    def test_reusable_workflow_path_and_release_pagination(self):
        routes = upstream_routes()
        releases = f"{UPSTREAM}/releases?per_page=100&page="
        routes[releases + "1"] = [release(f"v3.0.{index}-beta", "2026-11-01", prerelease=True) for index in range(100)]
        routes[releases + "2"] = [release("v2.10.0", "2026-01-01"), release("v1.9.0", "2025-10-01")]
        routes[f"{UPSTREAM}/contents/.github/workflows?ref={NEW_SHA}"] = []
        routes[f"{UPSTREAM}/contents/.github/workflows?ref={OLD_SHA}"] = [{"name": "build.yml", "type": "file"}]
        checker = ReleaseChecker(FixtureGitHub(routes))
        result = checker.check(Reference("ci.yml", 2, "owner/action/.github/workflows/build.yml@v1.9.0", True))
        self.assertEqual(result, Result("current", tag="v1.9.0", sha=OLD_SHA))

    def test_api_failure_is_reported(self):
        routes = upstream_routes()
        routes[f"{UPSTREAM}/releases?per_page=100&page=1"] = GitHubError("GitHub API rate limit reached", 429)
        checker = ReleaseChecker(FixtureGitHub(routes))
        self.assertEqual(checker.check(Reference("ci.yml", 1, f"owner/action@{OLD_SHA}", False)),
                         Result("unchecked", "GitHub API rate limit reached"))


class ScannerTests(unittest.TestCase):
    def test_scans_workflow_uses_and_follows_local_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "build.yml").write_text("""\
on: push
jobs:
  test:
    steps:
      - run: |
          echo 'uses: ignored/example@v1'
      - uses: ./local
      - uses: &action actions/checkout@v4
      - uses: *action
  reusable:
    uses: org/templates/.github/workflows/build.yaml@v1
  local:
    uses: ./.github/workflows/template.yaml
""", encoding="utf-8")
            (workflows / "template.yaml").write_text("jobs: {example: {steps: [{uses: org/tool@v1}]}}", encoding="utf-8")
            (workflows / "invalid.yml").write_text("jobs: [", encoding="utf-8")
            local = root / "local"
            local.mkdir()
            (local / "action.yaml").write_text(
                "runs: {using: composite, steps: [{uses: ./local}, {uses: org/subdir/action@v2}]}", encoding="utf-8"
            )
            scanner = Scanner(WorkspaceSource(root))
            scanner.scan()

        self.assertCountEqual([ref.value for ref in scanner.references], [
            "actions/checkout@v4", "actions/checkout@v4", "org/templates/.github/workflows/build.yaml@v1",
            "org/tool@v1", "org/subdir/action@v2",
        ])
        self.assertEqual(scanner.issues, [(".github/workflows/invalid.yml:1", "Could not parse YAML")])


class ReportingTests(unittest.TestCase):
    def test_pr_target_reads_exact_head_and_reports_with_both_outputs(self):
        git = f"/repos/{REPOSITORY}/git"
        comments = f"/repos/{REPOSITORY}/issues/17/comments"
        routes = upstream_routes() | {
            f"{git}/commits/{NEW_SHA}": {"tree": {"sha": "root"}},
            f"{git}/trees/root": {"tree": [tree_entry(".github", "github", directory=True)]},
            f"{git}/trees/github": {"tree": [tree_entry("workflows", "workflows", directory=True)]},
            f"{git}/trees/workflows": {"tree": [tree_entry("build.yml", "workflow")]},
            f"{git}/blobs/workflow": {"encoding": "base64", "content": b64encode(b"""\
jobs:
  check:
    steps:
      - uses: owner/action@v1.9.0
      - uses: owner/action@v2
      - uses: owner/action@main
""").decode()},
            f"{comments}?per_page=100&page=1": [],
            ("POST", comments): {},
        }
        api = FixtureGitHub(routes)
        with tempfile.TemporaryDirectory() as directory:
            paths = Path(directory)
            event = paths / "event.json"
            event.write_text(json.dumps({"pull_request": {"number": 17, "head": {"sha": NEW_SHA}}}), encoding="utf-8")
            env = {
                "GITHUB_ACTIONS": "true",
                "GITHUB_REPOSITORY": REPOSITORY,
                "GITHUB_EVENT_NAME": "pull_request_target",
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_SHA": OLD_SHA,
                "GITHUB_RUN_ID": "100",
                "GITHUB_STEP_SUMMARY": str(paths / "summary"),
                "GITHUB_OUTPUT": str(paths / "output"),
            }
            with patch.dict(os.environ, env, clear=True), patch("action_version_check.GitHub", return_value=api), redirect_stdout(io.StringIO()):
                main()
            summary = (paths / "summary").read_text(encoding="utf-8")
            self.assertEqual((paths / "output").read_text(encoding="utf-8"), "current=1\nupdates=1\nunchecked=1\n")
        self.assertIn("🟠 Update available", summary)
        self.assertIn("🟢 Current", summary)
        self.assertIn("🟡 Unable to check", summary)
        self.assertIn(NEW_SHA, summary)
        [(method, path, body)] = api.writes
        self.assertEqual((method, path), ("POST", comments))
        self.assertIn(COMMENT_MARKER + "\n" + summary, body["body"])
        self.assertIn(f"https://github.com/{REPOSITORY}/actions/runs/100", body["body"])

    def test_updates_own_comment_and_ignores_a_users_marker(self):
        path = f"/repos/{REPOSITORY}/issues/comments/22"
        routes = {
            f"/repos/{REPOSITORY}/issues/17/comments?per_page=100&page=1": [
                {"id": 21, "user": {"login": "contributor", "type": "User"}, "body": COMMENT_MARKER + "\n"},
                {"id": 22, "user": {"login": "github-actions[bot]", "type": "Bot"}, "body": COMMENT_MARKER + "\n"},
            ],
            ("PATCH", path): {},
        }
        api = FixtureGitHub(routes)
        body = COMMENT_MARKER + "\nNew summary"
        update_comment(api, REPOSITORY, 17, body)
        self.assertEqual(api.writes, [("PATCH", path, {"body": body})])

    def test_large_report_retains_totals_and_escapes_workflow_values(self):
        value = "owner/a|b@v1\n[link](https://example.com)"
        self.assertNotIn("|", code(value))
        self.assertNotIn("[link]", code(value))
        row = Row("build.yml:4", value, Result("update", tag="v2", sha=NEW_SHA))
        output = report([row] * 1000, Counter(update=1000), 2000)
        self.assertIn("1000 updates available", output)
        self.assertIn("additional rows omitted", output)
        self.assertLess(len(output.encode()), 2000)
