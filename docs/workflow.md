# Development Workflow

How work moves from an idea to a release. This is a proposal for the team to agree on; update it when practice shows it needs to change.

## Roles

- **Task owner:** the assignee of the GitHub issue. Ownership of features lives on the issues, not in this file, so it stays current when work is reassigned.
- **Reviewer:** any teammate other than the author; requested on the PR.
- **Release coordinator:** one per release, named in the release's tracking issue. The final release must have a different coordinator from the review release.

## Selecting and limiting work

- All work is tracked as GitHub issues. Each issue has one owner (assignee) and clear acceptance criteria.
- Work is prioritised by the next milestone's requirements first, then by what unblocks teammates (for example, shared contract changes before the code that depends on them).
- Each person has **at most two issues in progress** at a time. Finish or hand off before starting more.
- **Urgent work** (CI broken on `dev`, a broken release, a blocker for another member) goes first. Post it in the team channel and assign an owner right away.
- **Unfinished work** at a deadline stays open: the owner comments on the issue with what's done, what's left and what's blocked, and the gap is listed in the release notes.
- If someone is blocked or unavailable for more than a day, they say so in the team channel; another member picks up the issue and reassigns it.

## From issue to merge

| Step | What happens | Owner |
|------|--------------|-------|
| 1. Plan | Open an issue describing the behaviour and acceptance criteria. Contract changes (a `Client` method or an HTTP shape) are agreed in the issue before coding. | Issue author |
| 2. Implement | Branch from `dev` as `<name>-<feature>`. Write tests with the code. | Issue owner |
| 3. Open PR | Open a PR into `dev` using the PR template, with `Closes #N`. Describe what works, what changed, what's left, and any AI-generated code. | Issue owner |
| 4. Review | At least **one teammate other than the author** reviews behaviour, tests, docs and contract boundaries (see `AGENTS.md` at the repository root). Feedback is labelled as *blocking*, *question* or *suggestion*. | Reviewer |
| 5. Verify | CI is green. Changes to the Dropbox integration are also checked against a real account by the author (integration tests or manual curl), and the PR says how. | Author |
| 6. Merge | The author merges after approval, using a merge commit, then deletes the branch. The issue closes automatically. | Author |

### Required before merging into `dev`

- CircleCI passes: lint (`ruff check`), type checks (`mypy`), and tests with coverage at or above 85%.
- One approving review from a teammate who isn't the author.
- Docs updated for any user-facing change (README, package README or `docs/`).
- No secrets, generated files or unrelated changes in the diff.

### Required before releasing (`dev` → `main`)

- Everything above, on the exact commit being released.
- The integration tests pass against real Dropbox (`uv run pytest -m integration`).
- A teammate who isn't the coordinator verifies setup and one workflow from a fresh clone of the tag.

## Versioning

We use [Semantic Versioning](https://semver.org/) with pre-release labels:

| Version | Meaning |
|---------|---------|
| `v0.1.0-rc.1`, `v0.1.0-rc.2`, … | Review release (pre-release). May be incomplete; gaps listed in the notes. |
| `v0.1.0` | Final release for this homework. |
| `v0.1.1` | Bug fix on top of a published release. |
| `v0.2.0` | New features or contract changes in later homeworks. |

While the version is `0.x`, the public contract may change between minor versions. Every contract change (a `Client` method or an HTTP request/response shape) is called out under **Breaking changes** in the release notes and PR.

## Releasing

The release coordinator:

1. Picks the exact commit on `main` (after merging `dev` into `main` through a PR) and confirms CI passed on it.
2. Checks the behaviour included in the release and lists any incomplete scope.
3. Creates and pushes an **annotated** tag: `git tag -a v0.1.0-rc.1 -m "Review release"` then `git push origin v0.1.0-rc.1`.
4. Asks a teammate to verify setup and one workflow from a fresh clone of the tag.
5. Publishes a GitHub Release from the tag, with notes covering user-visible changes, linked issues and PRs, breaking changes, setup or configuration changes, known limitations, and links to CI and verification. Review releases are marked as **pre-release**.

## Defective releases

- **Published tags are never moved or deleted.** Fixes go into a new version (for example `v0.1.1` or `v0.1.0-rc.2`).
- The coordinator adds a warning to the defective release's notes saying whether users should go back to the previous version or wait for the fix, and links the issue tracking it.
- The notes also call out state that reverting code won't restore: files already created, moved or deleted in a Dropbox account, and saved OAuth credentials (`.dropbox_token.json`) or Dropbox app permission changes.
- The fix follows the normal issue → PR → review flow, with a regression test.
