---
log:
2026-09-13: Default `sync` copies a private no-follow snapshot before tar so a live tree cannot swap a file into an escaping symlink during archive.
2026-09-13: Added `sync`, which always sends one gzip-compressed archive. Default mode mirrors a file or directory without `.git`; `--git-aware` sends shallow blob-filtered Git metadata plus the selected index and worktree changes so offline `git rev-parse HEAD` and scoped `git status --porcelain` remain available. Extraction replaces only a named destination below `/content`. Public or signed URLs should be fetched by the VM instead of relayed through the client. One live OAuth CPU lifecycle replaced a stale default tree, preserved `HEAD` plus seven staged/unstaged/untracked status entries, confirmed the unrelated committed blob was unavailable offline, and ended with no active sessions.
---

# Design: File Management (`ls`, `rm`, `upload`, `download`, `edit`, `sync`)

## Overview
File management on the Colab VM uses the Jupyter Contents API, with kernel-side extraction for compressed tree synchronization.

## Approach

### 1. Listing Files (`colab ls`)
- **API**: `GET /api/contents/<path>` (as seen in HAR L68181).
- **Parameters**: 
    - `authuser`: 0
    - `colab-runtime-proxy-token`: <session_token>
- **Response**: JSON with `content` field containing an array of directory entries.
- **Display**: Pretty-print the list (similar to `ls -F` or a formatted table).

### 2. Uploading Files (`colab upload`)
- **API**: `PUT /api/contents/<remote_path>` (as seen in HAR).
- **Payload**: JSON body:
    ```json
    {
      "name": "filename.txt",
      "path": "path/filename.txt",
      "type": "file",
      "format": "text",
      "content": "..."
    }
    ```
- **Base64 Encoding**: Use `format: base64` for binary files.
- **Progress**: Implement a simple progress bar for large uploads by chunking or providing status updates.

### 3. Downloading Files (`colab download`)
- **API**: `GET /api/contents/<remote_path>?content=1` (as seen in HAR).
- **Response**: JSON with `content` field.
- **Handling**: Decodes content based on `format` (text or base64) and saves it locally.

### 4. Deleting Files (`colab rm`)
- **API**: `DELETE /api/contents/<remote_path>`.

### 5. Editing Files (`colab edit`)
- **Approach**: Combines downloading the remote file, opening it in the user's `$EDITOR` locally, and subsequently uploading the changed file if modifications were made.
- **State tracking**: Uses a SHA-256 hash to track file changes securely and deterministically between before and after the editor is invoked.
- **Fallbacks**: Creates an empty local temporary file if the target file on the Colab runtime doesn't exist yet, essentially acting like `touch`.

### 6. Synchronizing Trees (`mighty-colab sync`)
- **Syntax**: `mighty-colab sync LOCAL_PATH REMOTE_PATH [-s SESSION] [--git-aware]`.
- **Compression**: `sync` always creates one gzip-compressed tar archive before using the existing 1MB-chunked Contents API upload. Files and directories use the same transfer path.
- **Mirror semantics**: after safe extraction into a sibling staging path, the VM atomically replaces the named destination. Files absent from the new payload do not remain at the destination. The destination must be below `/content`; replacing `/content` itself or escaping it with `..` is rejected.
- **Default mode**: the destination receives the selected file or directory contents. `.git` directories are omitted. The client copies a private no-follow snapshot before tar so a live file cannot be swapped into an escaping symlink during archive. Relative symlinks are accepted only when they resolve inside the payload; escaping or absolute symlinks are rejected.
- **Git-aware mode**: `--git-aware` finds the source worktree and sends a shallow, `blob:none` metadata reconstruction. Unrelated index entries remain sparse and their blobs are not transferred. The selected path retains its repository-relative location under `REMOTE_PATH`, while tracked modifications, tracked deletions, intent-to-add entries, non-ignored untracked files, and source skip-worktree/assume-unchanged flags are reconstructed. Ignored files and unrelated worktree files are omitted. `origin` is rewritten to `offline://mighty-colab/sync`, so no local path or credential-bearing upstream URL is sent and the clone cannot fetch accidentally. The supported offline contract is commit identity (`git rev-parse HEAD`) and status for the selected path, not a general-purpose clone; submodules and embedded repositories are rejected.
- **Failure handling**: upload and extraction errors return exit code 1, and an operation-specific completion marker is required before the client reports success. The client uses bounded, best-effort removal for its temporary remote archive. Remote traversal pins `/content` and each destination parent with directory descriptors, refusing symlink traversal. Pre-install validation or exchange failure leaves an existing destination in place; replacement of an existing destination uses the Colab Linux kernel's atomic path-exchange operation. Post-commit residue cleanup exceptions are attempted independently and ordinary cleanup failures are warnings rather than false rollback claims.
- **[Open] Interrupted verdict**: if the client loses contact after the atomic install but before it receives the completion marker, it returns failure because it cannot prove whether the destination changed. A rerun safely replaces the destination, but it is not equivalent when the local source changed between attempts. There is no remote transaction journal for reconciliation.
- **[Open] Hard-stop residue**: `SIGKILL`, kernel death, or VM loss can interrupt all cleanup and leave the operation's hidden staging directory or archive under `/content`; in-process exceptions, including `BaseException`, still attempt every cleanup step.

> **Important:** Do not use `sync` to push bytes that the VM can fetch from a public or signed URL. Run the download on the VM so the transfer uses the VM's network path rather than the client upload path. For automated workloads, use `job` data and artifact URLs for that data plane.

## Implementation Details
- **Base URL**: The backend URL obtained during session assignment.
- **Proxy Token**: The `colab-runtime-proxy-token` is required for each request.
- **Error Handling**: Handle 404 (not found) and 403 (unauthorized).
- **Large Files — fixed via chunked upload**: uploads well under 200MB in a single request could
  fail with a bare `500 Internal Server Error` from the Colab/Jupyter backend, most likely a
  request-body-size limit somewhere in the stack (proxy/gateway/tunnel), not a Contents-API-level
  restriction. The Jupyter Contents API has a real chunked-upload protocol for exactly this —
  confirmed against JupyterLab's own client source (`packages/filebrowser/src/model.ts`): files
  are sliced into `CHUNK_SIZE` (1MB) pieces and sent as sequential `PUT` requests numbered
  `1, 2, 3, ...`, with the final request flagged `chunk: -1` to tell the server to finalize the
  save. `ContentsClient.upload()` (`contents.py`) now implements this correctly — files at/under
  1MB still go out as a single request (`chunk: 1`, unchanged from before); larger files are
  chunked automatically, no separate command or flag needed. Verified live against a real session:
  50MB and 160MB uploads (the latter close to a previously-failing size) both succeeded with
  byte-exact integrity confirmed via `os.path.getsize` on the VM.
- The existing `500`-specific error hint in `ContentsClient._request()` is kept as a fallback for
  any other cause of a `500` (e.g. a genuine per-account storage quota, which chunking can't route
  around) — but the size-limit-driven case that originally motivated it should no longer occur.

## Testing Strategy
TDD is mandatory for all file management features.

### 1. Mock Contents API
- **Test Case**: Verify `colab ls` correctly parses a Jupyter `contents` JSON response with `type: directory` and `type: file`.
- **Test Case**: Verify `colab upload` correctly base64-encodes a binary local file for the `PUT` payload.
- **Test Case**: Verify `colab download` correctly decodes the `content` field from the `GET` response and saves it locally.
- **Test Case**: Verify `colab edit` safely handles when a file is or isn't modified.
- **Test Case**: Verify `colab edit` securely opens a system editor safely through mocks without hanging the testing environment.
- **Test Case**: Verify `sync` always uploads gzip data, excludes `.git` by default, and atomically removes stale destination files.
- **Test Case**: Verify `sync --git-aware` preserves `HEAD`, selected modified/deleted/untracked/intent-to-add status, and index flags while omitting ignored, unrelated, and skipped blobs.
- **Test Case**: Verify unsafe destinations, parent-symlink races, snapshot-before-tar file swaps, escaping and relocated internal symlinks, remote extraction errors, terminal-session reconciliation, and temporary archive cleanup fail safely.

### 2. Error Cases
- **Test Case**: Verify 404 responses are correctly caught and presented as a "File not found" error to the user.
- **Test Case**: Verify correct handling of large file uploads exceeding API limits via kernel streaming.