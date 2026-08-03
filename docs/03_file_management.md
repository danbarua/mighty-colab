# Design: File Management (`ls`, `rm`, `upload`, `download`, `edit`)

## Overview
File management on the Colab VM will be implemented using the Jupyter Contents API.

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

### 2. Error Cases
- **Test Case**: Verify 404 responses are correctly caught and presented as a "File not found" error to the user.
- **Test Case**: Verify correct handling of large file uploads exceeding API limits via kernel streaming.