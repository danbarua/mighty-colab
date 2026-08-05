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

## Wait for Cloud Build, then publish the GitHub Release

The tag push above triggers Cloud Build (`cloudbuild.yaml`, trigger
`publish-on-tag` in the `mighty-colab` GCP project) to build and publish to
PyPI — asynchronously, on GitHub's webhook, completely decoupled from this
script. **Do not create the GitHub Release until that build actually
succeeds** — the Release object is what GitHub's "Watch → Releases" email
notification fires on, so creating it unconditionally would email "release
done" even when the PyPI publish silently failed.

14. **Locate the triggered build** (there's a webhook delay before it
    appears — poll up to ~60s):
    ```bash
    BUILD_ID=""
    for i in $(seq 1 12); do
      BUILD_ID=$(gcloud builds list --project=mighty-colab \
        --filter="substitutions.TAG_NAME=vX.Y.Z" \
        --format="value(id)" --limit=1)
      [ -n "$BUILD_ID" ] && break
      sleep 5
    done
    ```
    If `BUILD_ID` is still empty after this loop, skip straight to the
    "Cloud Build not confirmed" outcome below — do not treat it as a script
    error, and do not retry indefinitely.

15. **Poll until the build reaches a terminal status** (cap ~10 minutes;
    real releases have taken ~60-90s historically, so this is a generous
    ceiling, not an expected wait):
    ```bash
    STATUS="QUEUED"
    for i in $(seq 1 40); do
      STATUS=$(gcloud builds describe "$BUILD_ID" --project=mighty-colab \
        --format="value(status)")
      case "$STATUS" in
        SUCCESS|FAILURE|INTERNAL_ERROR|TIMEOUT|CANCELLED|EXPIRED) break ;;
      esac
      sleep 15
    done
    ```

16. **Branch on the outcome:**
    - **`STATUS = SUCCESS`**: publish the Release using the section you
      just wrote in `CHANGELOG.md` (step 7's renamed heading) as the
      release notes — not `--generate-notes`, which fabricates notes from
      commit/PR history and would diverge from the curated changelog:
      ```bash
      awk '/^## \[X\.Y\.Z\]/{flag=1; next} /^## \[/{flag=0} flag' \
        CHANGELOG.md > /tmp/release-notes-X.Y.Z.md
      gh release create vX.Y.Z --title vX.Y.Z --notes-file /tmp/release-notes-X.Y.Z.md
      rm -f /tmp/release-notes-X.Y.Z.md
      ```
      The Release object itself exists purely so GitHub's own repo-watch
      notification can email the maintainer on a *confirmed-published*
      release — `CHANGELOG.md` stays the canonical source, this just
      mirrors that exact section into the Release rather than reinventing
      separate notes. If `gh` itself fails here (not installed/
      authenticated), don't fail the whole release over it — report that
      the Release step failed and that the user can run the commands
      above manually once `gh` is fixed.
    - **`BUILD_ID` was never found, or `STATUS` is anything else** (still
      building past the cap, `FAILURE`, `INTERNAL_ERROR`, `TIMEOUT`,
      `CANCELLED`, `EXPIRED`): do **not** create a GitHub Release. Report
      to the user that the tag/version bump succeeded but Cloud Build did
      not confirm success, with a link to
      `https://console.cloud.google.com/cloud-build/builds;region=global/${BUILD_ID}?project=mighty-colab`
      (or, if `BUILD_ID` is empty, the builds list:
      `https://console.cloud.google.com/cloud-build/builds?project=mighty-colab`)
      so they can check manually and create the Release themselves once
      they've confirmed the PyPI publish actually happened.

## Done

Report the new tag to the user and note that `hatch-vcs` will pick it up
automatically on the next build (`uv run mighty-colab version` will reflect
it once the tag is checked out locally). State clearly whether the GitHub
Release was published (Cloud Build confirmed SUCCESS) or skipped (build
not confirmed) — these are two different end states, not the same "done."
