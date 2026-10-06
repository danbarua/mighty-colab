---
name: release
description: Cut a new release of mighty-colab — bump the version, roll the Unreleased CHANGELOG section into a dated release section, tag, and push. Use when the user asks to "cut a release", "release vX.Y.Z", or "tag a new version".
---

# Release

Fully automated: version bump, `CHANGELOG.md` update, tag, push — no PR, no
confirmation prompt. Only run this when the user explicitly asks for a
release (e.g. "cut a release", "release v0.2.0"). Never propose or perform
a release proactively. Run every command with the repo root as the working
directory.

The package version is derived entirely from the git tag via `hatch-vcs` —
nothing else needs a manual version bump.

## Preconditions

Check these in order. If any fails, stop immediately — no commits, no
tags, no pushes.

1. **On `main`**:
   ```bash
   git rev-parse --abbrev-ref HEAD
   ```
   Must print `main`. Otherwise fail: "release must be run from main."

2. **Clean working tree**:
   ```bash
   git status --porcelain
   ```
   Must be empty. Otherwise fail: "working tree has uncommitted changes —
   commit, stash, or discard them first." This flow commits and pushes
   unattended, so it must never bundle unrelated local changes.

3. **`main` in sync with `origin/main`**:
   ```bash
   git fetch origin main --quiet
   git rev-parse main
   git rev-parse origin/main
   ```
   The two SHAs must match. If they differ in either direction, fail and
   tell the user to `git pull --ff-only` (if behind) or `git push` (if
   ahead) first. Do not attempt to resolve divergence yourself.

## Determine the version

4. **Current version** — highest existing tag:
   ```bash
   git tag --list 'v*.*.*' --sort=-v:refname | head -n1
   ```
   If this is empty (no tags yet), treat the current version as `v0.0.0`.

5. **New version**:
   - If the user gave one, normalize it to `vX.Y.Z` (prefix with `v` if
     they omitted it). Validate it's strictly greater than the current
     version; fail otherwise.
   - Otherwise, default to a **patch** bump: `vX.Y.(Z+1)` — increment the
     patch component only. Always patch by default, regardless of what
     kind of changes are in the changelog.

## Update CHANGELOG.md

`CHANGELOG.md` follows Keep a Changelog. Only edit the *live* section —
the top of the file, from the `## [Unreleased]` heading down to (and
including) its link-reference line just above the `---` separator that
precedes the frozen upstream changelog. Never touch anything at or after
that `---` separator.

6. Find the `## [Unreleased]` heading and read the content beneath it up
   to the next `## [` heading. If there are no bullets in it, fail:
   "nothing to release — Unreleased is empty."

7. Rename that heading to:
   ```
   ## [X.Y.Z] - YYYY-MM-DD
   ```
   using the new version and today's date.

8. Insert a fresh, empty heading directly above it so the file always has
   a blank slot ready for the next round of changes:
   ```
   ## [Unreleased]

   ```

9. Update the link-reference footer (the lines just above the `---`
   separator):
   - Change the existing `[Unreleased]: .../compare/v<PREV>...HEAD` line so
     it compares from the new version instead:
     `[Unreleased]: https://github.com/danbarua/mighty-colab/compare/vX.Y.Z...HEAD`
   - Add a new line directly after it for the release itself:
     `[X.Y.Z]: https://github.com/danbarua/mighty-colab/compare/v<PREV>...vX.Y.Z`
   - If `<PREV>` was `v0.0.0` (no prior tags), skip this line — there's no
     meaningful compare link for a first release.

## Commit, tag, push

10. ```bash
    git add CHANGELOG.md
    git commit -m "docs: release vX.Y.Z"
    ```

11. ```bash
    git push origin main
    ```

12. ```bash
    git tag -a vX.Y.Z -m "vX.Y.Z"
    ```
    Always annotated and `v`-prefixed, matching every existing tag in this
    repo.

13. ```bash
    git push origin vX.Y.Z
    ```
    Push the tag as an explicit refspec — `git push origin --tag <name>` is
    not valid git syntax (`--tag` isn't a flag; it's `--tags` for "push all
    tags", which isn't what's wanted here).

## Watch the release workflow

Pushing the tag starts `.github/workflows/release.yml` on GitHub Actions.
Its jobs run in order, and each job starts only when the previous one
succeeded:

| job | what it does |
|---|---|
| `test` | `pytest` and `ruff`, as in `test.yml` |
| `build` | `uv build`, then fails unless the wheel and sdist carry the tag's version |
| `publish` | uploads to PyPI through trusted publishing, in the `pypi` environment |
| `github-release` | creates the GitHub Release from this version's `CHANGELOG.md` section and attaches the wheel and sdist |

The workflow creates the GitHub Release itself, and only after the PyPI
upload succeeded. This skill does not create the Release; it watches the run
and reports the outcome.

14. **Find the workflow run for the tag.** GitHub starts the run a few
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
    If `RUN_ID` is still empty, go to the "run not found" outcome in step 16.
    Do not retry indefinitely.

15. **Watch the run with the `Monitor` tool**, not a foreground `sleep`
    loop. The command prints one line each time the run's or a job's status
    changes, and exits when the run completes. Launch it and stop: step 16
    runs when the Monitor notification arrives, not in the same turn.

    ```
    Monitor({
      description: "Release workflow for vX.Y.Z",
      timeout_ms: 900000,
      command: "prev=''; for i in $(seq 1 60); do s=$(gh run view \"$RUN_ID\" --json status,conclusion,jobs --jq '\"run=\\(.status)/\\(.conclusion) \" + ([.jobs[] | \"\\(.name)=\\(if (.conclusion // \"\") == \"\" then .status else .conclusion end)\"] | join(\" \"))'); if [ \"$s\" != \"$prev\" ]; then echo \"$s\"; prev=\"$s\"; fi; case \"$s\" in run=completed/*) exit 0 ;; esac; sleep 15; done; echo 'run=TIMED_OUT_WAITING'"
    })
    ```
    The cap is 15 minutes. The tests take about a minute, and the whole run
    usually finishes within 5 minutes.

16. **Report the outcome** from the last line the monitor printed:
    - **`run=completed/success`**: the version is on PyPI and the GitHub
      Release exists. Report both links:
      `https://pypi.org/project/mighty-colab/X.Y.Z/` and the output of
      `gh release view vX.Y.Z --json url -q .url`.
    - **`run=completed/` with any other conclusion**: find the failed job
      with `gh run view "$RUN_ID" --json jobs --jq '.jobs[] | select(.conclusion == "failure") | .name'`
      and read its log with `gh run view "$RUN_ID" --log-failed | tail -n 60`.
      Report the job, the error, and what the failure means:
      - `test` or `build` failed: nothing was published. The tag and the
        release commit are already on GitHub. Ask the user how to proceed;
        do not delete or move the tag.
      - `publish` failed: PyPI does not have the version. The most common
        cause is the trusted publisher on PyPI not matching the repository,
        `release.yml` or the `pypi` environment. After the cause is fixed,
        `gh run rerun "$RUN_ID" --failed` retries the failed jobs.
      - `github-release` failed: PyPI has the version, but the GitHub
        Release was not created. After the cause is fixed,
        `gh run rerun "$RUN_ID" --failed` retries it.
    - **`run=TIMED_OUT_WAITING`, or `RUN_ID` was never found**: report that
      the tag was pushed but the workflow's outcome is unknown, with a link
      to `https://github.com/danbarua/mighty-colab/actions/workflows/release.yml`.

## Done

Report the new tag, and state which end state the release reached:

- the workflow succeeded: the version is on PyPI and the GitHub Release
  exists;
- the workflow failed: name the job that failed and what was or was not
  published;
- the workflow's outcome is unknown.

These are different end states; do not report any of them as plain "done".
`hatch-vcs` derives the version from the tag, so `uv run mighty-colab version`
shows the new version once the tag is checked out locally.
