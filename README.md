# Action Version Check

Check the GitHub Actions and reusable workflows referenced by your repository for newer published releases. The action runs on Python 3.14 and writes a color-coded job summary with the release tag and full commit SHA. It also creates or updates a comment on pull requests when the workflow token can write comments.

## Quick start

After this action is published with a `v1` ref, add the following to `.github/workflows/action-version-check.yml` in the repository you want to audit. The action reads that repository directly through GitHub's API, so the job works without a checkout step.

```yaml
name: Check action versions
on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read
  pull-requests: write

concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: imranthaggard/action-version-check@v1
```

Use a published full commit SHA in place of `v1` to pin the checker itself. While developing the checker in this repository, you can check out a trusted revision and call it with `uses: ./`.

## Pull requests from forks

GitHub normally gives `pull_request` runs from forks and Dependabot a read-only token. Those runs still publish the job summary. To enable PR comments for those contributors, use the workflow below for pull requests. Remove the `pull_request` trigger from the quick-start workflow if you keep it for pushes. The workflow below runs the published checker from a trusted base and reads workflow YAML from the exact PR head SHA through the API. Keep this job limited to trusted steps that treat PR files as data. [GitHub's guidance for `pull_request_target`](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target)

```yaml
name: Check action versions on PRs
on:
  pull_request_target:
    types: [opened, synchronize, reopened]

permissions:
  contents: read
  pull-requests: write

concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: imranthaggard/action-version-check@v1
```

## Release and reporting behavior

- The action scans `.github/workflows/*.yml` and `.github/workflows/*.yaml` at the revision associated with the event. It checks `uses` in steps and reusable workflow jobs and follows local composite actions. It checks the referenced release of each external action or reusable workflow. Dependencies declared inside an external repository are outside the scan.
- Update candidates come from published, non-prerelease GitHub Releases. The action resolves each candidate tag to its commit and checks that the referenced action or workflow exists there. It selects the highest numeric version when the repository uses numeric tags such as `v2.10.0` for its latest release. For other naming schemes it prefers the release GitHub marks as latest, then uses publication date. [GitHub Releases API](https://docs.github.com/en/rest/releases/releases#list-releases)
- 🟢 **Current** means the reference resolves to the latest applicable release commit. A moving tag such as `v4` can be current when it already points to that commit.
- 🟠 **Update available** means a newer published release tag or commit is available. Use the release tag or the full commit SHA shown in the report.
- 🟡 **Unable to check** covers branch refs, container registry images, inaccessible repositories, missing releases, and refs whose order relative to a release could not be established. A ref ahead of the release is in this category too.

Updates and unchecked references are reported without failing the workflow. The action also exposes three step outputs when the caller gives it a step `id`.

| Output | Description |
| --- | --- |
| `current` | References on the latest applicable release |
| `updates` | References with a newer release available |
| `unchecked` | References or files the action could not check |

The caller's `GITHUB_TOKEN` is used for GitHub API reads and PR comments by default. To read releases in other private repositories on trusted workflow events, pass `upstream-token: ${{ secrets.UPSTREAM_READ_TOKEN }}` under `with`. Give that token **Contents read** access to the upstream repositories. PR comments and `pull_request_target` runs use the caller's workflow token.

## Local development

With Python 3.14 active, install the dependency and run the tests from the repository root.

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

To scan a local working tree, run `action_version_check.py` with Python from the repository you want to check. Set `AVC_REPOSITORY_TOKEN` through your shell or secret manager to authenticate API reads if needed. The local run prints the report to standard output.
