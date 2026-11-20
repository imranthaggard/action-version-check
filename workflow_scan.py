"""Find repository actions through workflow YAML and local composite actions."""

from base64 import b64decode
from collections import deque
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Protocol

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from github_api import GitHub, GitHubError


WORKFLOWS_DIR = ".github/workflows"
ACTION_FILES = ("action.yml", "action.yaml", "Dockerfile", "dockerfile")


def is_workflow_file(path: str) -> bool:
    return path.endswith((".yml", ".yaml"))


def valid_path(path: str) -> bool:
    return not any(
        part in {"", ".", ".."} or any(char in part for char in "\\:\r\n\0")
        for part in path.split("/")
    )


class Source(Protocol):
    def files(self, path: str) -> set[str]: ...

    def read(self, path: str) -> str: ...


class WorkspaceSource:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def resolve(self, path: str) -> Path:
        target = self.root / path
        if not target.resolve().is_relative_to(self.root):
            raise OSError("Path points outside the repository")
        return target

    def files(self, path: str) -> set[str]:
        try:
            return {entry.name for entry in self.resolve(path).iterdir() if entry.is_file()}
        except (FileNotFoundError, NotADirectoryError):
            return set()

    def read(self, path: str) -> str:
        return self.resolve(path).read_text(encoding="utf-8-sig")


class RepositorySource:
    def __init__(self, api: GitHub, repository: str, sha: str):
        self.api = api
        self.repository = repository
        self.sha = sha

    @cache
    def directory(self, path: str) -> dict:
        git_path = f"/repos/{self.repository}/git"
        if not path:
            sha = self.api.get(f"{git_path}/commits/{self.sha}")["tree"]["sha"]
        else:
            parent, _, name = path.rpartition("/")
            item = self.directory(parent).get(name)
            if item is None or item["type"] != "tree":
                return {}
            sha = item["sha"]
        tree = self.api.get(f"{git_path}/trees/{sha}")
        if tree.get("truncated"):
            raise GitHubError("GitHub returned an incomplete directory tree")
        return {item["path"]: item for item in tree["tree"]}

    def files(self, path: str) -> set[str]:
        return {name for name, item in self.directory(path).items() if item["mode"] in {"100644", "100755"}}

    def read(self, path: str) -> str:
        parent, _, name = path.rpartition("/")
        item = self.directory(parent).get(name)
        if item is None or item["mode"] not in {"100644", "100755"}:
            raise OSError("YAML file is unavailable")
        blob = self.api.get(f"/repos/{self.repository}/git/blobs/{item['sha']}")
        if blob.get("encoding") != "base64":
            raise OSError("YAML file contents are unavailable")
        return b64decode(blob["content"]).decode("utf-8-sig")


def file_names(entries) -> set[str]:
    if not isinstance(entries, list):
        return set()
    return {entry["name"] for entry in entries if entry["type"] in {"file", "symlink"}}


@dataclass(frozen=True)
class Reference:
    path: str
    line: int
    value: str
    reusable_workflow: bool

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}"


def field(node, name: str):
    if isinstance(node, MappingNode):
        for key, value in node.value:
            if isinstance(key, ScalarNode) and key.value == name:
                return value
    return None


def step_references(node):
    if isinstance(node, SequenceNode):
        for step in node.value:
            uses = field(step, "uses")
            if uses is not None:
                yield uses, False


def references_in(node, workflow: bool):
    if not workflow:
        runs = field(node, "runs")
        using = field(runs, "using")
        if isinstance(using, ScalarNode) and using.value == "composite":
            yield from step_references(field(runs, "steps"))
        return
    jobs = field(node, "jobs")
    if isinstance(jobs, MappingNode):
        for _, job in jobs.value:
            uses = field(job, "uses")
            if uses is not None:
                yield uses, True
            yield from step_references(field(job, "steps"))


class Scanner:
    def __init__(self, source: Source):
        self.source = source
        self.pending: deque[tuple[str, bool]] = deque()
        self.visited: set[str] = set()
        self.references: list[Reference] = []
        self.issues: list[tuple[str, str]] = []

    def scan(self):
        try:
            files = self.source.files(WORKFLOWS_DIR)
        except GitHubError as error:
            self.issues.append((WORKFLOWS_DIR, str(error)))
            return
        except OSError:
            self.issues.append((WORKFLOWS_DIR, "Could not list workflow files"))
            return
        for name in sorted(files):
            if is_workflow_file(name):
                self.pending.append((f"{WORKFLOWS_DIR}/{name}", True))
        while self.pending:
            path, workflow = self.pending.popleft()
            self.scan_file(path, workflow=workflow)

    def scan_file(self, path: str, *, workflow: bool):
        if path in self.visited:
            return
        self.visited.add(path)
        try:
            node = yaml.compose(self.source.read(path), Loader=yaml.BaseLoader)
        except (yaml.YAMLError, RecursionError) as error:
            mark = getattr(error, "problem_mark", None)
            location = path if mark is None else f"{path}:{mark.line + 1}"
            self.issues.append((location, "Could not parse YAML"))
            return
        except (OSError, ValueError, GitHubError):
            self.issues.append((path, "Could not read YAML file"))
            return
        if not isinstance(node, MappingNode):
            self.issues.append((path, "Expected a YAML mapping"))
            return

        for uses, reusable in references_in(node, workflow):
            value = uses.value if isinstance(uses, ScalarNode) else ""
            reference = Reference(path, uses.start_mark.line + 1, value, reusable)
            if value.startswith("./"):
                self.scan_local(reference)
            else:
                self.references.append(reference)

    def scan_local(self, reference: Reference):
        path = reference.value.removeprefix("./").rstrip("/")
        if path and not valid_path(path):
            self.issues.append((reference.location, "Invalid local repository path"))
            return
        if reference.reusable_workflow:
            if PurePosixPath(path).parent.as_posix() != WORKFLOWS_DIR or not is_workflow_file(path):
                self.issues.append((reference.location, "Invalid local reusable workflow path"))
                return
            self.pending.append((path, True))
            return
        try:
            files = self.source.files(path)
        except (OSError, GitHubError):
            self.issues.append((reference.location, "Could not read the local action directory"))
            return
        for name in ACTION_FILES:
            if name in files:
                if is_workflow_file(name):
                    self.pending.append((f"{path}/{name}" if path else name, False))
                return
        self.issues.append((reference.location, "Could not find a manifest for the local action"))
