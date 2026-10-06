---
name: release
description: Cut a new release of mighty-colab — draft the CHANGELOG entries for the user's approval, roll the Unreleased section into a dated release section, push the version tag, and open the release pull request. Use when the user asks to "cut a release", "release vX.Y.Z", or "tag a new version".
---

# Release

Run this only when the user explicitly asks for a release (for example "cut
a release" or "release v0.2.0"). Never propose or perform a release
proactively. Run every command with the repository root as the working
directory.

The skill stops once for the user: to approve the CHANGELOG entries. After
that it runs to the end without asking.

`hatch-vcs` derives the package version from the git tag, so nothing else
needs a version bump. Pushing the tag is what releases: it starts
`.github/workflows/release.yml`, which publishes to PyPI and creates the
GitHub Release.

**Run each command separately, and stop at the first one that fails.** Do not
chain dependent steps with `;`: a tag pushed after a failed push releases a
commit that is in neither `main` nor a pushed branch.

## Preconditions

Check these in order. If one fails, stop: no commits, no tags, no pushes.

1. **The working tree is clean.**
   ```bash
   git status --porcelain
   ```
   The output must be empty. Otherwise stop with: "the working tree has
   uncommitted or untracked changes; commit, stash or remove them first."
   The skill commits and pushes without asking, so it must never include
   unrelated local changes.

2. **The checkout is on an up-to-date `main`.**
   ```bash
   git fetch origin main --quiet
   git rev-parse --abbrev-ref HEAD
   git rev-list --count origin/main..main
   git rev-list --count main..origin/main
   ```
   - The checkout is on `main`, and both counts are 0: continue.
   - Local `main` has no commits that `origin/main` lacks (the first count is
     0), but the checkout is on another branch or `main` is behind: switch
     and fast-forward, then continue.
     ```bash
     git switch main
     git merge --ff-only origin/main
     ```
   - Local `main` has commits that `origin/main` lacks (the first count is
     not 0): stop and report them (`git log origin/main..main --oneline`).
     Do not push, reset or rebase `main` yourself.

## Determine the version

3. **The current version** is the highest existing tag:
   ```bash
   git tag --list 'v*.*.*' --sort=-v:refname | head -n1
   ```
   If there is no tag, the current version is `v0.0.0`.

4. **The new version**:
   - If the user named one, normalize it to `vX.Y.Z` (add the `v` if it is
     missing). It must be strictly greater than the current version;
     otherwise stop.
   - Otherwise, increment the patch number: `vX.Y.(Z+1)`. The default is
     always a patch release, whatever the changes are.

## Draft the CHANGELOG entries

`CHANGELOG.md` follows Keep a Changelog. Edit only the live part of the file:
from the `## [Unreleased]` heading down to and including its link-reference
lines, which are just above the `---` separator. Never change anything at or
after that separator; it is the frozen upstream changelog.

5. **List the pull requests merged since the last tag:**
   ```bash
   git log <PREV>..origin/main --first-parent --format='%h %s'
   ```
   A squash-merged pull request appears as one commit whose subject ends
   with `(#N)`. A pull request merged with a merge commit appears as
   `Merge pull request #N from ...`. Skip the release pull requests
   (`release/vX.Y.Z` branches and `docs: release vX.Y.Z` commits).

6. **Compare them with the `## [Unreleased]` section**, the text between
   that heading and the next `## [` heading. An entry drafted by this skill
   ends with its pull request number, for example `(#84)`. An older entry may
   have no number; judge from its content which pull request it covers.

7. **Draft an entry for each pull request with a user-facing change that
   Unreleased does not cover.** Read the pull request with
   `gh pr view N --json title,body`.
   - Put the entry under `### Added`, `### Changed`, `### Fixed` or
     `### Removed`.
   - Start with a bold summary of the change, followed by one to three
     sentences on what a user sees. End with the pull request number, for
     example `(#84)`.
   - Draft no entry for a pull request that changes only tests, CI, docs or
     internal structure, unless users see the change.
   - When several pull requests build one feature, write one entry that
     names all of their numbers.

8. **Show the user the proposed Unreleased section** (the existing entries
   with the drafts in place), and list the pull requests that have no entry,
   with the reason for each. Stop and wait for the user to approve or edit it.
   Write it to `CHANGELOG.md` only after approval.

   If Unreleased is empty and no pull request has a user-facing change, stop
   with: "nothing to release: no user-facing change since <PREV>."

## Update CHANGELOG.md

9. Rename the `## [Unreleased]` heading to `## [X.Y.Z] - YYYY-MM-DD`, with the
   new version and today's date.

10. Insert a new, empty heading directly above it:
    ```
    ## [Unreleased]

    ```

11. Update the link references just above the `---` separator:
    - Change `[Unreleased]: .../compare/v<PREV>...HEAD` to
      `[Unreleased]: https://github.com/danbarua/mighty-colab/compare/vX.Y.Z...HEAD`.
    - Add this line directly after it:
      `[X.Y.Z]: https://github.com/danbarua/mighty-colab/compare/v<PREV>...vX.Y.Z`.
    - If `<PREV>` is `v0.0.0`, add no `[X.Y.Z]` line: a first release has no
      compare link.

## Commit, tag, open the pull request

The `protect-main` ruleset requires a pull request for every change to `main`,
so the release commit goes on a branch.

12. ```bash
    git switch -c release/vX.Y.Z
    git add CHANGELOG.md
    git commit -m "docs: release vX.Y.Z"
    git push -u origin release/vX.Y.Z
    ```
    If the push fails, stop: no tag exists yet.

13. ```bash
    git tag -a vX.Y.Z -m "vX.Y.Z"
    git push origin vX.Y.Z
    ```
    The tag is annotated and `v`-prefixed, like every existing tag. Push it
    as an explicit refspec: `git push origin --tag <name>` is not valid git
    syntax. Pushing the tag starts the release workflow.

14. Open the release pull request:
    ```bash
    gh pr create --base main --head release/vX.Y.Z \
      --title "docs: release vX.Y.Z" \
      --body "Release commit for vX.Y.Z, which the tag points at. Merge with \"Create a merge commit\", so the tag stays an ancestor of main."
    ```
    Do not merge it. A squash or rebase merge rewrites the commit and leaves
    the tag off `main`; `hatch-vcs` would then derive `main`'s development
    versions from the previous tag.

## Watch the release workflow

`.github/workflows/release.yml` runs its jobs in order, and each job starts
only when the previous one succeeded:

| job | what it does |
|---|---|
| `test` | `pytest` and `ruff`, as in `test.yml` |
| `build` | `uv build`, then fails unless the wheel and sdist carry the tag's version |
| `publish` | uploads to PyPI through trusted publishing, in the `pypi` environment |
| `github-release` | creates the GitHub Release from this version's `CHANGELOG.md` section and attaches the wheel and sdist |

The workflow creates the GitHub Release itself, after the PyPI upload
succeeded. This skill watches the run and reports the outcome.

15. **Find the workflow run for the tag.** GitHub starts the run a few
    seconds after the push, so poll for up to 60 s:
    ```bash
    RUN_ID=""
    for i in $(seq 1 12); do
      RUN_ID=$(gh run list --workflow=release.yml --event=push --limit 10 \
        --json databaseId,headBranch \
        --jq '.[] | select(.headBranch == "vX.Y.Z") | .databaseId' | head -n1)
      [ -n "$RUN_ID" ] && break
      sleep 5
    done
    ```
    If `RUN_ID` is still empty, go to the "no `RUN_ID`" outcome in step 17.

16. **Watch the run with the `Monitor` tool**, not a foreground `sleep` loop.
    The command prints one line each time the run's or a job's status
    changes, and exits when the run completes. Launch it and stop: step 17
    runs when the Monitor notification arrives.

    ```
    Monitor({
      description: "Release workflow for vX.Y.Z",
      timeout_ms: 900000,
      command: "prev=''; for i in $(seq 1 60); do s=$(gh run view \"$RUN_ID\" --json status,conclusion,jobs --jq '\"run=\\(.status)/\\(.conclusion) \" + ([.jobs[] | \"\\(.name)=\\(if (.conclusion // \"\") == \"\" then .status else .conclusion end)\"] | join(\" \"))'); if [ \"$s\" != \"$prev\" ]; then echo \"$s\"; prev=\"$s\"; fi; case \"$s\" in run=completed/*) exit 0 ;; esac; sleep 15; done; echo 'run=TIMED_OUT_WAITING'"
    })
    ```
    Replace `$RUN_ID` in the command with the id from step 15. The cap is 15
    minutes; the whole run usually finishes within 5 minutes.

17. **Report the outcome** from the last line the monitor printed:
    - **`run=completed/success`**: the version is on PyPI and the GitHub
      Release exists. Report `https://pypi.org/project/mighty-colab/X.Y.Z/`
      and the output of `gh release view vX.Y.Z --json url -q .url`.
    - **`run=completed/` with another conclusion**: find the failed job with
      `gh run view "$RUN_ID" --json jobs --jq '.jobs[] | select(.conclusion == "failure") | .name'`
      and read its log with `gh run view "$RUN_ID" --log-failed | tail -n 60`.
      Report the job, the error, and what the failure means:
      - `test` or `build` failed: nothing was published. Ask the user how to
        proceed; do not delete or move the tag.
      - `publish` failed: PyPI does not have the version. The most common
        cause is a trusted publisher on PyPI that does not match the
        repository, `release.yml` or the `pypi` environment. After the cause
        is fixed, `gh run rerun "$RUN_ID" --failed` retries the failed jobs.
      - `github-release` failed: PyPI has the version, but the GitHub Release
        does not exist. After the cause is fixed,
        `gh run rerun "$RUN_ID" --failed` retries the job.
    - **`run=TIMED_OUT_WAITING`, or no `RUN_ID`**: the tag was pushed, but the
      outcome is unknown. Report the link
      `https://github.com/danbarua/mighty-colab/actions/workflows/release.yml`.

## Done

Report:
- the new tag;
- which end state the release reached: the version is on PyPI and the GitHub
  Release exists; or the named job failed, with what was and was not
  published; or the outcome is unknown;
- the release pull request's link, and that it must be merged with "Create a
  merge commit".

These end states differ; do not report any of them as plain "done".
