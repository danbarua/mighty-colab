# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import json
import nbformat
import os
import re
import subprocess
import sys
import tempfile
import typer
import uuid
from nbformat.v4 import new_output
from rich.console import Console
from typing import List, Optional
from typing_extensions import Annotated

from colab_cli.common import (
    build_envelope,
    emit_json,
    json_safe_outputs,
    pid_alive,
)
from colab_cli.common import _exit_code_from_outputs
from colab_cli.runtime import ColabRuntime
from colab_cli.utils import handle_image, is_terminal_error, render_display_data
from colab_cli.console import connect_console

_console = Console()

TITLE_REGEX = re.compile(r"^\s*#\s*@title\s+(.*)", re.MULTILINE)
ENV_KEY_REGEX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_stdin_tty():
    return sys.stdin.isatty()


def _parse_env_vars(env: Optional[List[str]]) -> dict[str, str]:
    """Parse repeatable --env KEY=VALUE entries into an ordered mapping."""
    env_vars = {}
    for item in env or []:
        if "=" not in item:
            typer.echo(
                f"[colab] Invalid --env value {item!r}. Expected KEY=VALUE.",
                err=True,
            )
            raise typer.Exit(2)

        key, value = item.split("=", 1)
        if not ENV_KEY_REGEX.fullmatch(key):
            typer.echo(
                f"[colab] Invalid --env key {key!r}. Expected a valid "
                "environment variable name.",
                err=True,
            )
            raise typer.Exit(2)

        env_vars[key] = value
    return env_vars


def _build_env_prelude(env_vars: dict[str, str]) -> str:
    """Build Python source that sets environment variables in the remote kernel."""
    if not env_vars:
        return ""

    lines = ["import os"]
    lines.extend(f"os.environ[{key!r}] = {value!r}" for key, value in env_vars.items())
    return "\n".join(lines) + "\n"


def _build_script_prelude(basename: str, script_args: Optional[List[str]] = None) -> str:
    """Python source giving text-transmitted code `python script.py`-like
    semantics: `sys.argv`, `__name__ == "__main__"`, and a `__file__`
    sentinel.

    Nothing from the caller's local filesystem exists on the remote kernel
    -- the code is sent as text, not read from a real file there -- so a
    plausible-looking local path (e.g. the caller's actual local path)
    would be actively misleading: code doing `open(__file__)` would fail
    on a path that looks like it should exist, rather than obviously not.
    `<mighty-colab-exec:basename>` follows Python's own convention for
    code with no backing file (CPython itself uses `<stdin>`, `<string>`,
    `<doctest ...>`), keeping the basename for readable tracebacks while
    being unambiguous that it's synthetic. Without this, module-scope code
    referencing `__file__` (e.g. `os.path.dirname(os.path.abspath(__file__))`
    to locate a sibling module) raises `NameError` -- this crashed a real
    billing run.
    """
    argv_literal = repr([basename, *(script_args or [])])
    file_literal = repr(f"<mighty-colab-exec:{basename}>")
    return (
        "import sys\n"
        f"sys.argv = {argv_literal}\n"
        "__name__ = '__main__'\n"
        f"__file__ = {file_literal}\n"
    )


def save_output(outputs, cell):
    if cell is None:
        return

    if not hasattr(cell, "outputs"):
        cell.outputs = []
    else:
        cell.outputs.clear()

    for out in outputs:
        if out.get("output_type") == "stream":
            cell.outputs.append(
                new_output(
                    output_type="stream",
                    name=out.get("name", "stdout"),
                    text=out.get("text", ""),
                )
            )
        elif "data" in out:
            output_type = out.get("output_type", "display_data")
            cell.outputs.append(
                new_output(
                    output_type=output_type,
                    data=out["data"],
                    metadata=out.get("metadata", {}),
                )
            )
        elif out.get("output_type") == "error":
            cell.outputs.append(
                new_output(
                    output_type="error",
                    ename=out.get("ename", "Error"),
                    evalue=out.get("evalue", ""),
                    traceback=out.get("traceback", []),
                )
            )


def display_output(out, output_image=None):
    if out.get("output_type") == "stream":
        stream = sys.stderr if out.get("name") == "stderr" else sys.stdout
        stream.write(out.get("text", ""))
        stream.flush()
    elif "data" in out:
        data = out["data"]
        text = render_display_data(data)
        if text is not None:
            _console.print(text)
        if png := data.get("image/png"):
            handle_image(png, "image/png", target_path=output_image)
        elif jpeg := data.get("image/jpeg"):
            handle_image(jpeg, "image/jpeg", target_path=output_image)
    elif out.get("output_type") == "error":
        tb = out.get("traceback", [])
        if tb:
            sys.stderr.write("".join(tb) + "\n")
        else:
            ename = out.get("ename", "Error")
            evalue = out.get("evalue", "")
            sys.stderr.write(f"{ename}: {evalue}\n")
    else:
        # Ignore silent outputs like metadata or clear_output for streaming
        pass


def _finish_json(envelope: dict, json_result_path: Optional[str]) -> None:
    """Deliver a `--json` envelope: to a sidecar file (`exec-async`'s child,
    signaled by the hidden `--json-result-path` flag) or to stdout (a
    foreground `exec --json`)."""
    if json_result_path:
        with open(json_result_path, "w", encoding="utf-8") as f:
            json.dump(envelope, f)
    else:
        emit_json(envelope)


def exec_command(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    file: Annotated[
        Optional[str],
        typer.Option(
            "-f",
            "--file",
            help=(
                "Local file path (.py or .ipynb) to read and execute on the "
                "remote kernel. Read from the local filesystem and "
                "transmitted as code -- not a path that must already exist "
                "on the VM."
            ),
        ),
    ] = None,
    output_image: Annotated[
        Optional[str], typer.Option("--output-image", help="Path to save plot")
    ] = None,
    timeout: Annotated[
        Optional[float],
        typer.Option("--timeout", help="Timeout in seconds for code execution"),
    ] = 30.0,
    env: Annotated[
        Optional[List[str]],
        typer.Option(
            "--env",
            help=(
                "Set an environment variable in the remote kernel as KEY=VALUE. "
                "Repeat for multiple variables."
            ),
        ),
    ] = None,
    json_result_path: Annotated[
        Optional[str],
        typer.Option(
            "--json-result-path",
            hidden=True,
            help=(
                "Internal: used by `exec-async --json` to have its spawned "
                "child write the final envelope to a sidecar file instead "
                "of stdout, once the run finishes."
            ),
        ),
    ] = None,
):
    """Execute code in a session"""
    from colab_cli.common import state

    want_json = state.json_output or json_result_path is not None

    env_vars = _parse_env_vars(env)
    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        if want_json:
            _finish_json(
                build_envelope("error", exit_code=1, reason="session_not_found"),
                json_result_path,
            )
        typer.echo(f"[colab] Session '{name}' not found.", err=True)
        raise typer.Exit(1)

    code_blocks = []
    if file:
        if file.endswith(".ipynb"):
            typer.echo(f"[colab] Parsing notebook '{file}'...")
            with open(file, "r", encoding="utf-8") as f:
                nb = nbformat.read(f, as_version=4)
                for cell in nb.cells:
                    # nbformat v4.5+ requires 'id' at the top level
                    if not hasattr(cell, "id") or not cell.id:
                        cell.id = str(uuid.uuid4())

                    if cell.cell_type == "code":
                        code_blocks.append(
                            {"code": cell.source, "id": cell.id, "cell": cell}
                        )
        else:
            with open(file, "r") as f:
                code_blocks.append({"code": f.read(), "id": None})
    else:
        if is_stdin_tty():
            if want_json:
                _finish_json(
                    build_envelope("error", exit_code=1, reason="no_input"),
                    json_result_path,
                )
            typer.echo(
                "[colab] Error: No input provided. Pipe code or provide a file.",
                err=True,
            )
            raise typer.Exit(1)
        code_blocks.append({"code": sys.stdin.read(), "id": None})

    if not any(b["code"].strip() for b in code_blocks):
        if want_json:
            _finish_json(build_envelope("ok", exit_code=0, blocks=[]), json_result_path)
        raise typer.Exit(0)

    def on_started(kid):
        s.kernel_id = kid
        state.store.add(s)

    def on_sess_started(sid):
        s.session_id = sid
        state.store.add(s)

    runtime = ColabRuntime(
        s.url,
        s.token,
        kernel_id=s.kernel_id,
        session_id=s.session_id,
        on_kernel_started=on_started,
        on_session_started=on_sess_started,
    )
    try:
        # Ensure we are in /content which is the standard Colab working directory
        runtime.execute_code(
            "import os; os.makedirs('/content', exist_ok=True); os.chdir('/content')"
        )
    except Exception as e:
        runtime.stop()
        if is_terminal_error(e):
            if want_json:
                _finish_json(
                    build_envelope("error", exit_code=1, reason="session_lost"),
                    json_result_path,
                )
            typer.echo(
                f"[colab] Session '{name}' appears to be lost (404/401). Cleaning up.",
                err=True,
            )
            state.prune_session(name)
            raise typer.Exit(1)
        if want_json:
            _finish_json(
                build_envelope("error", exit_code=1, reason="preflight_failed"),
                json_result_path,
            )
        raise e

    had_error = False
    blocks_json = []
    try:
        is_nb = file and file.endswith(".ipynb")
        s.running = f"exec({file or 'stdin'})"
        state.store.add(s)

        for i, block in enumerate(code_blocks):
            code = _build_env_prelude(env_vars) + block["code"]
            if file and not is_nb:
                code = _build_script_prelude(os.path.basename(file)) + code
            identifier = None
            if is_nb:
                title_match = TITLE_REGEX.search(code)
                if title_match:
                    identifier = title_match.group(1).strip()
                elif block.get("id"):
                    identifier = block["id"]
                else:
                    identifier = ""

                identifier_str = f" - {identifier}" if identifier else ""
                typer.echo(
                    f"[colab] Executing cell {i + 1}/{len(code_blocks)}{identifier_str}..."
                )

            s.last_execution = (
                file or "stdin",
                identifier,
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            state.store.add(s)

            outputs = runtime.execute_code(
                code,
                output_hook=(
                    None
                    if state.json_output
                    else lambda o: display_output(o, output_image)
                ),
                timeout=timeout,
            )
            if any(o.get("output_type") == "error" for o in outputs):
                had_error = True
            if "cell" in block:
                save_output(outputs, block["cell"])
            state.history.log_event(
                name,
                "execution",
                {
                    "code": code,
                    "outputs": outputs,
                    "cell_index": i if len(code_blocks) > 1 else None,
                    "cell_id": block.get("id"),
                },
            )
            if want_json:
                blocks_json.append(
                    {
                        "code": code,
                        "outputs": json_safe_outputs(outputs),
                        "cell_index": i if len(code_blocks) > 1 else None,
                        "cell_id": block.get("id"),
                    }
                )
    finally:
        s.running = None
        state.store.add(s)
        runtime.stop()
        if file and file.endswith(".ipynb"):
            output_file = os.path.splitext(file)[0] + "_output.ipynb"
            typer.echo(f"[colab] Saving notebook with outputs to '{output_file}'...")
            with open(output_file, "w", encoding="utf-8") as f:
                nbformat.write(nb, f)

    if want_json:
        job_exit_code = _exit_code_from_outputs(
            [o for block in blocks_json for o in block["outputs"]]
        )
        status = "ok" if job_exit_code == 0 else "job_raised"
        reason = None if job_exit_code == 0 else "job_raised"
        _finish_json(
            build_envelope(
                status, exit_code=job_exit_code, reason=reason, blocks=blocks_json
            ),
            json_result_path,
        )
        return

    # Raised after the `finally` above has run, so all blocks still execute
    # and (for notebooks) the output file is still saved even when a later
    # cell errors -- only the process exit code reflects the failure.
    if had_error:
        raise typer.Exit(1)


def _exec_async_dir() -> str:
    return os.path.expanduser("~/.config/colab-cli/exec-async")


def spawn_exec_async(
    file: str,
    session_name: str,
    log_path: str,
    timeout: Optional[float] = None,
    env: Optional[List[str]] = None,
    output_image: Optional[str] = None,
    auth_provider=None,
    config_path=None,
) -> int:
    """Spawns a detached `exec` process with stdio redirected to a log file.

    Reuses the real (synchronous) `exec` command as the child rather than a
    bespoke worker, so all of its existing state bookkeeping (`running`,
    `last_execution`, history `execution` events, notebook output saving)
    keeps working unmodified. The only difference from a foreground `exec`
    is where stdout/stderr land -- a file instead of this process's
    terminal -- which lets the caller return immediately.

    Both `auth_provider` and `config_path` are propagated as global flags
    for the same reason as `spawn_keep_alive`: the detached child re-parses
    argv from scratch and does not inherit the parent's parsed Typer flags.
    """
    cmd = [sys.executable, "-m", "colab_cli.cli"]
    if auth_provider is not None:
        cmd.append(f"--auth={auth_provider.value}")
    if config_path is not None:
        cmd.extend(["--config", config_path])
    cmd.extend(["exec", "-s", session_name, "-f", file])
    if timeout is not None:
        cmd.extend(["--timeout", str(timeout)])
    if output_image:
        cmd.extend(["--output-image", output_image])
    for item in env or []:
        cmd.extend(["--env", item])

    kwargs = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    else:
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    log_fp = open(log_path, "wb")
    try:
        p = subprocess.Popen(
            cmd,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
    finally:
        log_fp.close()
    return p.pid


def exec_async(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    file: Annotated[
        Optional[str],
        typer.Option(
            "-f",
            "--file",
            help=(
                "Local file path (.py or .ipynb) to read and execute on the "
                "remote kernel in the background. Required unless code is "
                "piped via stdin (a live terminal can't be forwarded to a "
                "detached process)."
            ),
        ),
    ] = None,
    output_image: Annotated[
        Optional[str], typer.Option("--output-image", help="Path to save plot")
    ] = None,
    output_log: Annotated[
        Optional[str], typer.Option("--output-log", help="Path to save log file")
    ] = None,
    timeout: Annotated[
        Optional[float],
        typer.Option("--timeout", help="Timeout in seconds for code execution"),
    ] = 30.0,
    env: Annotated[
        Optional[List[str]],
        typer.Option(
            "--env",
            help=(
                "Set an environment variable in the remote kernel as KEY=VALUE. "
                "Repeat for multiple variables."
            ),
        ),
    ] = None,
):
    """Execute code in a session in the background"""
    from colab_cli.common import state

    # Validate --env up front so we fail fast, before spawning anything.
    _parse_env_vars(env)

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.", err=True)
        raise typer.Exit(1)

    if s.exec_pid and pid_alive(s.exec_pid):
        typer.echo(
            f"[colab] Session '{name}' already has a background exec running "
            f"(pid={s.exec_pid}). Follow it with `mighty-colab log -s {name} -f`, "
            "or wait for it to finish before starting another.",
            err=True,
        )
        raise typer.Exit(1)

    if not file:
        if is_stdin_tty():
            typer.echo(
                "[colab] Error: exec-async requires -f/--file, or piped code. "
                "A live terminal can't be forwarded to a background process.",
                err=True,
            )
            raise typer.Exit(1)
        code = sys.stdin.read()
        if not code.strip():
            raise typer.Exit(0)
        exec_async_dir = _exec_async_dir()
        os.makedirs(exec_async_dir, exist_ok=True)
        fd, file = tempfile.mkstemp(
            prefix=f"{name}-", suffix=".py", dir=exec_async_dir
        )
        with os.fdopen(fd, "w") as f:
            f.write(code)

    if output_log:
        log_path = os.path.expanduser(output_log)
        parent = os.path.dirname(log_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    else:
        log_path = os.path.join(state.history.log_dir, f"{name}.exec.log")

    # Defensive invariant check: log_path is derived from `name` alone, so
    # two different sessions should never compute the same path -- but if
    # local state ever ends up otherwise (hand-edited sessions.json, a
    # future rename flow, a case-insensitive filesystem), the alternative
    # is `spawn_exec_async` silently truncating (`open(log_path, "wb")`) a
    # file a live worker belonging to that other session is still writing.
    # Fail loudly here, before that call, instead.
    for other_name, other in state.store.list().items():
        if other_name == name:
            continue
        if other.exec_log_path == log_path and pid_alive(other.exec_pid):
            typer.echo(
                f"[colab] Refusing to start: '{log_path}' is currently "
                f"being written by session '{other_name}' "
                f"(pid={other.exec_pid}).",
                err=True,
            )
            raise typer.Exit(1)

    # Persist BEFORE spawning so the daemon's own `state.store.get` doesn't
    # race the write (same rule as `spawn_keep_alive`).
    s.exec_log_path = log_path
    state.store.add(s)

    pid = spawn_exec_async(
        file,
        name,
        log_path,
        timeout=timeout,
        env=env,
        output_image=output_image,
        auth_provider=state.auth_provider,
        config_path=state.config_path,
    )
    s.exec_pid = pid
    state.store.add(s)

    typer.echo(f"[colab] Started background exec (pid={pid}).")
    typer.echo(f"[colab] Follow output with: mighty-colab log -s {name} -f")


def repl(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    output_image: Annotated[
        Optional[str], typer.Option("--output-image", help="Path to save plot")
    ] = None,
):
    """Start an interactive REPL"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.", err=True)
        raise typer.Exit(1)

    def on_started(kid):
        s.kernel_id = kid
        state.store.add(s)

    def on_sess_started(sid):
        s.session_id = sid
        state.store.add(s)

    runtime = ColabRuntime(
        s.url,
        s.token,
        kernel_id=s.kernel_id,
        session_id=s.session_id,
        on_kernel_started=on_started,
        on_session_started=on_sess_started,
    )
    try:
        # Ensure we are in /content which is the standard Colab working directory
        runtime.execute_code(
            "import os; os.makedirs('/content', exist_ok=True); os.chdir('/content')"
        )
    except Exception as e:
        runtime.stop()
        if is_terminal_error(e):
            typer.echo(
                f"[colab] Session '{name}' appears to be lost (404/401). Cleaning up.",
                err=True,
            )
            state.prune_session(name)
            raise typer.Exit(1)
        raise e

    if not is_stdin_tty():
        code = sys.stdin.read()
        if not code.strip():
            raise typer.Exit(0)

        s.last_execution = (
            "stdin",
            None,
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        s.running = "repl(stdin)"
        state.store.add(s)
        had_error = False
        try:
            outputs = runtime.execute_code(
                code, output_hook=lambda o: display_output(o, output_image)
            )
            had_error = any(o.get("output_type") == "error" for o in outputs)
            state.history.log_event(
                name, "execution", {"code": code, "outputs": outputs, "source": "piped"}
            )
        finally:
            s.running = None
            state.store.add(s)
            runtime.stop()

        if had_error:
            raise typer.Exit(1)
    else:
        from colab_cli.repl import ColabREPL

        s.running = "repl"
        state.store.add(s)
        try:
            repl_inst = ColabREPL(
                runtime,
                session_name=s.name,
                history_logger=state.history,
                output_image=output_image,
            )
            state.history.log_event(name, "repl_started", {})
            repl_inst.run()
        finally:
            s.running = None
            state.store.add(s)


def console(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
):
    """Connect to raw TTY console"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.", err=True)
        raise typer.Exit(1)
    state.history.log_event(s.name, "console_started", {})
    s.running = "console"
    state.store.add(s)
    try:
        connect_console(s)
    except Exception as e:
        if is_terminal_error(e):
            typer.echo(
                f"[colab] Session '{name}' appears to be lost (404/401). Cleaning up.",
                err=True,
            )
            state.prune_session(name)
            raise typer.Exit(1)
        raise e
    finally:
        s.running = None
        state.store.add(s)


def register(app: typer.Typer):
    app.command(name="exec")(exec_command)
    app.command(name="exec-async")(exec_async)
    app.command()(repl)
    app.command()(console)
