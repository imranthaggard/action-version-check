"""Compare action references with commits attached to published stable releases."""

import re
from dataclasses import dataclass
from functools import cache
from pathlib import PurePosixPath
from urllib.parse import quote

from github_api import GitHub, GitHubError
from workflow_scan import ACTION_FILES, WORKFLOWS_DIR, Reference, file_names, is_workflow_file, valid_path


FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
VERSION = re.compile(r"[vV]?([0-9]+)(?:\.([0-9]+))?(?:\.([0-9]+))?(?:\+[A-Za-z0-9.-]+)?")


@dataclass(frozen=True)
class Release:
    tag: str
    published_at: str

    @property
    def version(self) -> tuple[int, int, int] | None:
        match = VERSION.fullmatch(self.tag)
        if match:
            major, minor, patch = match.groups(default="0")
            return int(major), int(minor), int(patch)
        return None


@dataclass(frozen=True)
class Result:
    status: str
    detail: str = ""
    tag: str = ""
    sha: str = ""


class ReleaseChecker:
    def __init__(self, api: GitHub):
        self.api = api

    @cache
    def releases(self, repository: str) -> tuple[Release, ...]:
        releases = [
            Release(item["tag_name"], item["published_at"])
            for item in self.api.pages(f"/repos/{repository}/releases")
            if not item["draft"] and not item["prerelease"] and item["published_at"]
        ]
        if not releases:
            return ()
        versions = {release.tag: release.version for release in releases}
        preferred_tag = ""
        if any(version is None for version in versions.values()):
            preferred = self.api.get_optional(f"/repos/{repository}/releases/latest")
            if preferred:
                preferred_tag = preferred["tag_name"]
        if all(version is not None for version in versions.values()) or versions.get(preferred_tag) is not None:
            # Legacy or beta tag names should not make a newly published backport the newest version.
            releases.sort(key=lambda release: (versions[release.tag] or (-1, 0, 0), release.published_at), reverse=True)
        else:
            releases.sort(key=lambda release: (release.tag == preferred_tag, release.published_at), reverse=True)
        return tuple(releases)

    @cache
    def tag_commit(self, repository: str, tag: str) -> str | None:
        if not valid_path(tag):
            return None
        ref = self.api.get_optional(f"/repos/{repository}/git/ref/tags/{quote(tag, safe='/')}")
        if ref is None:
            return None
        target = ref["object"]
        seen = set()
        while target["type"] == "tag" and target["sha"] not in seen:
            sha = target["sha"]
            if not FULL_SHA.fullmatch(sha):
                return None
            seen.add(sha)
            annotated = self.api.get_optional(f"/repos/{repository}/git/tags/{sha}")
            if annotated is None:
                return None
            target = annotated["object"]
        if target["type"] == "commit" and FULL_SHA.fullmatch(target["sha"]):
            return target["sha"].lower()
        return None

    @cache
    def latest_for(self, repository: str, path: str, reusable: bool):
        directory, names = (WORKFLOWS_DIR, (PurePosixPath(path).name,)) if reusable else (path, ACTION_FILES)
        for release in self.releases(repository):
            sha = self.tag_commit(repository, release.tag)
            if sha and file_names(self.api.contents(repository, directory, sha)).intersection(names):
                return release, sha
        return None

    @cache
    def tagged_commits(self, repository: str) -> dict[str, str]:
        return {
            item["name"]: item["commit"]["sha"].lower()
            for item in self.api.pages(f"/repos/{repository}/tags")
        }

    def check(self, reference: Reference) -> Result:
        if reference.value.startswith("docker://"):
            return Result("unchecked", "Container registry image has no GitHub release reference")
        target, separator, ref = reference.value.partition("@")
        owner, _, rest = target.partition("/")
        name, _, path = rest.partition("/")
        repository = f"{owner}/{name}".lower()
        if not separator or not valid_path(ref) or not REPOSITORY.fullmatch(repository) or not valid_path(target):
            return Result("unchecked", "Expected a GitHub repository reference with a tag or commit SHA")
        if reference.reusable_workflow and (
            PurePosixPath(path).parent.as_posix() != WORKFLOWS_DIR or not is_workflow_file(path)
        ):
            return Result("unchecked", "Invalid reusable workflow path")
        try:
            return self.compare(repository, path, ref, reference.reusable_workflow)
        except GitHubError as error:
            return Result("unchecked", str(error))

    @cache
    def compare(self, repository: str, path: str, ref: str, reusable: bool) -> Result:
        if ref.startswith("refs/heads/"):
            return Result("unchecked", "Branch references do not identify a release")
        is_sha = FULL_SHA.fullmatch(ref) is not None
        tag = ref.removeprefix("refs/tags/")
        current_sha = ref.lower() if is_sha else self.tag_commit(repository, tag)
        if current_sha is None:
            return Result("unchecked", "Ref is a branch or could not be resolved to a Git tag")

        latest = self.latest_for(repository, path, reusable)
        if latest is None:
            return Result("unchecked", "No published stable release contains this action or workflow")
        release, latest_sha = latest
        releases = self.releases(repository)
        latest_index = releases.index(release)
        update = Result("update", tag=release.tag, sha=latest_sha)
        for index, candidate in enumerate(releases):
            if not is_sha and candidate.tag == tag:
                if index > latest_index:
                    return update
                if index < latest_index:
                    return Result("unchecked", "Referenced release is newer than the latest release containing this path")
                break
        if current_sha == latest_sha:
            return Result("current", tag=release.tag, sha=latest_sha)

        comparison = self.api.get_optional(
            f"/repos/{repository}/compare/{current_sha}...{latest_sha}?per_page=1"
        )
        status = comparison.get("status") if comparison else None
        if status == "ahead" and latest_index == 0:
            return update

        # Backports can be on different branches even when their release versions can be ordered.
        if len(releases) > 1:
            tags = self.tagged_commits(repository)
            for index, candidate in enumerate(releases):
                if tags.get(candidate.tag) == current_sha:
                    if index > latest_index:
                        return update
                    return Result("unchecked", "Referenced release is newer than the latest release containing this path")
        if status == "ahead":
            return update
        if status == "behind":
            return Result("unchecked", "Ref is ahead of the latest applicable release commit")
        return Result("unchecked", "Ref could not be ordered against a published release")
