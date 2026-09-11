# Vendored: `jupyter-kernel-client` (googlecolab fork)

## Why this exists

`jupyter-kernel-client` is claimed on PyPI by an unrelated, newer release line
of the *same upstream project* (Datalayer's), which renamed `KernelClient` to
`JupyterKernelClient` starting at 1.0.0. Google's fork this CLI needs is
pinned in git only, via `[tool.uv.sources]` -- a **uv-local-dev mechanism that
is stripped from published wheel metadata**. Every `pip install mighty-colab`
or `uv tool install mighty-colab` therefore resolved to Datalayer's PyPI
package instead, and `exec` died with a bare `AttributeError` on first use.

Checked before vendoring, so this isn't guesswork:
- PyPI `jupyter-kernel-client==0.8.0` (same version number as the fork) is
  missing `JupyterSubprotocol` and `deserialize_msg_from_ws_default` entirely
  -- `runtime.py` calls both unconditionally. No PyPI release, pinned or not,
  satisfies this CLI's needs.
- The fork is a genuine BSD-3 fork of Datalayer's 0.8.0, not a clean-room
  rewrite (`diff` against upstream 0.8.0 shows the same file layout, same
  license text, ~230 changed lines concentrated in `client.py`/`manager.py`/
  `utils.py`/`wsclient.py`).

## Provenance

- Source: `https://github.com/googlecolab/jupyter-kernel-client`
- Commit: `f18e982c3265df5e923aa9def101ab3fd737e139` (2026-09-11)
- Vendored version: `0.8.0`
- License: BSD 3-Clause (Datalayer, Inc. copyright retained per-file; see
  `LICENSE` in this directory)

## What changed from the installed package

1. **All intra-package imports rewritten absolute -> relative**
   (`from jupyter_kernel_client.x import y` -> `from .x import y`). Vendored
   code must resolve to itself, never to a same-named package that might
   still be installed in the environment -- an absolute import here would
   silently bind to the wrong distribution and reintroduce the exact bug
   this vendoring closes.
2. **`manager.py`'s `client_class` dotted string repointed**:
   `"jupyter_kernel_client.wsclient.KernelWebSocketClient"` ->
   `"colab_cli._vendor.jupyter_kernel_client.wsclient.KernelWebSocketClient"`.
   This is resolved at runtime via `traitlets.utils.importstring.import_item`,
   not a normal `import` statement -- a plain grep for import lines misses it.
3. **`jupyter_mimetypes` import made lazy** in `client.py`'s `set_variable`/
   `get_variable` (the only two call sites). That package pulls in `pyarrow`
   (~35MB) and is not used by anything this CLI calls; a module-level import
   made every install pay for a dependency the CLI never exercises.
4. **`konsoleapp.py` and `shell.py` dropped.** They implement an interactive
   console app (`jupyter-console` dependency) this CLI never launches.
   `__init__.py`'s `KonsoleApp` export was removed to match.

Everything else is unmodified.

## `colab_cli/runtime.py`'s consumption

`runtime.py` imports `from colab_cli._vendor import jupyter_kernel_client` and
calls `jupyter_kernel_client.KernelClient(...)` directly -- no version
sniffing, no `hasattr` branching. That machinery existed only to handle "which
distribution got installed"; vendoring removes the question entirely.

## Updating this vendor copy

1. Pick a commit on `googlecolab/jupyter-kernel-client`.
2. Re-copy `*.py` (except `konsoleapp.py`/`shell.py`) into this directory.
3. Re-apply the four changes above (imports, dotted string, lazy
   `jupyter_mimetypes`, dropped console app).
4. Update the commit SHA and date in this file.
5. Run `uv run pytest tests/ -q` and the live spike/integration suite before
   merging -- this is the transport layer every `exec`/`run`/`repl` call goes
   through.
