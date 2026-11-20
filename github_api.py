"""Minimal GitHub REST client for release metadata, file reads, and PR comments."""

import json
from functools import cache
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class GitHubError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if urlsplit(request.full_url)[:2] != urlsplit(newurl)[:2]:
            raise GitHubError("GitHub API redirected to a different origin")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


class GitHub:
    def __init__(self, api_url: str, token: str):
        self.api_url = api_url.rstrip("/")
        self.headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "action-version-check",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.opener = build_opener(SameOriginRedirect())

    def request(self, method: str, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        headers = self.headers | ({} if data is None else {"Content-Type": "application/json"})
        request = Request(self.api_url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            with error:
                if error.code == 429 or error.headers.get("X-RateLimit-Remaining") == "0":
                    raise GitHubError("GitHub API rate limit reached", error.code) from None
                raise GitHubError(f"GitHub API returned HTTP {error.code}", error.code) from None
        except (OSError, ValueError):
            raise GitHubError("Could not read a response from the GitHub API") from None

    @cache
    def get(self, path: str):
        return self.request("GET", path)

    @cache
    def get_optional(self, path: str):
        try:
            return self.get(path)
        except GitHubError as error:
            if error.status == 404:
                return None
            raise

    def pages(self, path: str):
        page = 1
        while True:
            items = self.get(f"{path}?per_page=100&page={page}")
            yield from items
            if len(items) < 100:
                return
            page += 1

    def contents(self, repository: str, path: str, sha: str):
        endpoint = f"/repos/{repository}/contents"
        if path:
            endpoint += f"/{quote(path, safe='/')}"
        return self.get_optional(f"{endpoint}?{urlencode({'ref': sha})}")
