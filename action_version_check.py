"""GitHub Actions entry point and Markdown reporting."""

import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from github_api import GitHub, GitHubError
from release_check import FULL_SHA, REPOSITORY, ReleaseChecker, Result
from workflow_scan import WORKFLOWS_DIR, RepositorySource, Scanner, WorkspaceSource


COMMENT_MARKER = "<!-- action-version-check -->"
STATUS_LABELS = {
    "update": "🟠 Update available",
    "unchecked": "🟡 Unable to check",
    "current": "🟢 Current",
}
MARKDOWN_ENTITIES = str.maketrans({char: f"&#{ord(char)};" for char in "&<>\"'|@[]()!\\`*~_:\r\n."})


@dataclass(frozen=True)
class Row:
    location: str
    reference: str
    result: Result


def code(value: str) -> str:
    return f"<code>{value.translate(MARKDOWN_ENTITIES)}</code>" if value else "—"


def report(rows: list[Row], counts: Counter, byte_limit: int, *, prefix: str = "", suffix: str = "") -> str:
    lines = [
        prefix + "## Action Version Check\n\n",
        f"🟢 **{counts['current']} current** · 🟠 **{counts['update']} updates available**"
        f" · 🟡 **{counts['unchecked']} unable to check**\n\n",
    ]
    if not rows:
        return "".join(lines) + "No upstream references found in workflow files.\n" + suffix
    lines.extend([
        "| Status | Location | Reference | Latest release | Release commit SHA | Details |\n",
        "| --- | --- | --- | --- | --- | --- |\n",
    ])
    size = sum(len(line.encode()) for line in lines) + len(suffix.encode())
    for index, row in enumerate(rows):
        result = row.result
        detail = result.detail or "—"
        line = (
            f"| {STATUS_LABELS[result.status]} | {code(row.location)} | {code(row.reference)} | "
            f"{code(result.tag)} | {code(result.sha)} | {detail} |\n"
        )
        size += len(line.encode())
        if size > byte_limit - 100:
            lines.append(f"\n{len(rows) - index} additional rows omitted due to GitHub’s size limit.\n")
            break
        lines.append(line)
    return "".join(lines) + suffix


def update_comment(api: GitHub, repository: str, number: int, body: str):
    comments_path = f"/repos/{repository}/issues/{number}/comments"
    for comment in api.pages(comments_path):
        author = comment.get("user") or {}
        if author.get("login") != "github-actions[bot]" or author.get("type") != "Bot":
            continue
        if (comment.get("body") or "").startswith(COMMENT_MARKER + "\n"):
            if comment["body"] != body:
                api.request("PATCH", f"/repos/{repository}/issues/comments/{comment['id']}", {"body": body})
            return
    api.request("POST", comments_path, {"body": body})


def append_to_environment_file(name: str, content: str):
    if path := os.environ.get(name):
        with Path(path).open("a", encoding="utf-8", newline="\n") as output:
            output.write(content)


def main():
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    token = os.environ.get("AVC_REPOSITORY_TOKEN", "")
    api = GitHub(api_url, token)
    upstream_token = os.environ.get("AVC_UPSTREAM_TOKEN")
    checker = ReleaseChecker(GitHub(api_url, upstream_token) if upstream_token else api)
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    pull = {}
    if event_name.startswith("pull_request") and (event_path := os.environ.get("GITHUB_EVENT_PATH")):
        pull = json.loads(Path(event_path).read_text(encoding="utf-8")).get("pull_request") or {}

    if os.environ.get("GITHUB_ACTIONS") == "true":
        if event_name == "pull_request_target":
            sha = (pull.get("head") or {}).get("sha") or ""
        else:
            sha = os.environ.get("GITHUB_SHA", "")
        if not REPOSITORY.fullmatch(repository) or not FULL_SHA.fullmatch(sha):
            scanner = None
            rows = [Row(WORKFLOWS_DIR, "", Result("unchecked", "Workflow revision is unavailable"))]
        else:
            scanner = Scanner(RepositorySource(api, repository, sha))
    else:
        scanner = Scanner(WorkspaceSource(Path(os.environ.get("GITHUB_WORKSPACE", "."))))
    if scanner is not None:
        scanner.scan()
        rows = [Row(ref.location, ref.value, checker.check(ref)) for ref in scanner.references]
        rows.extend(Row(path, "", Result("unchecked", message)) for path, message in scanner.issues)

    status_order = list(STATUS_LABELS)
    rows.sort(key=lambda row: status_order.index(row.result.status))
    counts = Counter(row.result.status for row in rows)
    summary = report(rows, counts, 1_000_000)
    append_to_environment_file("GITHUB_STEP_SUMMARY", summary)
    append_to_environment_file(
        "GITHUB_OUTPUT", f"current={counts['current']}\nupdates={counts['update']}\nunchecked={counts['unchecked']}\n"
    )
    print(f"Action Version Check: {counts['current']} current, {counts['update']} updates, {counts['unchecked']} unchecked")
    if not os.environ.get("GITHUB_STEP_SUMMARY"):
        print(summary)

    number = pull.get("number")
    if REPOSITORY.fullmatch(repository) and type(number) is int and number > 0:
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        suffix = f"\n[Open the workflow run summary]({server}/{repository}/actions/runs/{run_id})\n" if run_id.isdecimal() else ""
        body = report(rows, counts, 60_000, prefix=COMMENT_MARKER + "\n", suffix=suffix)
        try:
            update_comment(api, repository, number, body)
        except GitHubError as error:
            message = f"PR comment unavailable. {error}."
            if error.status in {403, 404}:
                message += " Check the workflow's `pull-requests: write` permission."
            append_to_environment_file("GITHUB_STEP_SUMMARY", f"\n> {message}\n")
            print(f"::warning::{message}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
