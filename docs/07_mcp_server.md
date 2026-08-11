---
log:
2026-08-02: Initial design and implementation of `colab mcp`, a stdio MCP (Model Context Protocol) server exposing the CLI's own Click command tree as tools. Hand-rolled scanner/dispatcher (`mcp_server.py`) rather than the `click-mcp` package the user had added as a dependency: `click-mcp==0.6.1` calls `Server.list_tools()`/`.call_tool()` as decorator methods, which don't exist on the installed `mcp==2.0.0` SDK's `Server` (rewritten around constructor-based `on_list_tools=`/`on_call_tool=` handlers), and its command-exclusion registry is dead code (`register_mcp_metadata` is never called by any public decorator). It would also have had a correctness bug even working: it dispatches by round-tripping a synthesized argv through `cli_group.main(args=[...], standalone_mode=False)`, re-running the whole group's `main()` -- with no global flags in that argv, `--auth`/`--config` would silently reset to their Typer defaults on every tool call. `mcp_server.py` instead scans `click_group.commands` once at startup and dispatches by invoking `Command.invoke(ctx)` directly in-process, so the root `@app.callback()` runs exactly once, at server startup, and never again per tool call. `ssh`/`repl`/`console`/`edit`/`drivemount` are excluded by name (interactive/TTY-blocking) union with anything `hidden=True` (`auth`, `keep-alive`, `whoami`, the `README`/`SKILL` aliases). Also fixed two bugs found via testing: Typer builds its commands from a *vendored* copy of Click's parameter-type classes (`typer._click.types.*`), so an `isinstance` check against the real `click` module silently mis-typed every bool/int/float param as `"string"` in the generated JSON Schema -- fixed by keying off `param.type.name` instead; and dispatch initially captured only stdout, silently dropping `typer.echo(..., err=True)` error messages sent before `raise typer.Exit(N)`. Removed the `click-mcp` dependency from `pyproject.toml`; added `mcp>=2.0.0,<3.0.0` directly (was already present transitively) with an upper bound, given how much the SDK's public API has moved across major versions. Added `tests/test_mcp_server.py` (14 tests). Verified end-to-end with the `mcp` SDK's own client (`mcp.client.stdio`) against a real `python -m colab_cli.cli mcp` subprocess.
2026-08-11: Added `EXCLUDED_PARAMS`, a per-command parameter-exclusion map alongside the existing whole-command `EXCLUDED_COMMANDS`, and used it to strip `log`'s `--follow` from its MCP tool schema. Motivation: `log -f` (added earlier today alongside `exec-async`) blocks a single MCP tool call for the entire, potentially unbounded duration of a background job and only returns output once, at the very end -- not incrementally -- which Claude Desktop's plain request/response tool-call model can't represent. The rest of `log` stays exposed (fast, bounded, single-shot); `exec-async` itself was already correctly exposed and unaffected -- it returns in ~1s regardless of the submitted script's runtime, making it (not `log -f`) the right way to kick off a long job from MCP. Enforced in `_command_params`, which both `build_tools` (schema generation) and `_build_kwargs` (dispatch) already shared, so excluding a param there is defense in depth: even a client that sends `follow` anyway never reaches the callback with it set. 4 new tests in `tests/test_mcp_server.py`. Also added `log --tail` (in `commands/utility.py`, not `mcp_server.py`) as the MCP-safe replacement capability the exclusion left missing: a one-shot, non-blocking read of a background job's current output, no polling or liveness check, whether it's still running or finished. Being a plain bounded synchronous file read, it needed zero MCP-server changes -- no new `EXCLUDED_PARAMS` entry, just picked up automatically by the existing scanner. This is the intended shape of the "thin wrapper" model: new capabilities belong in the CLI; MCP only ever needs to *exclude*, never to special-case.
2026-08-02: Replaced the stray `integration/repro_mcp_server/test.sh` (an accidental byte-for-byte copy of `repro_run_command/test.sh`, left over from scaffolding) with a real live end-to-end MCP smoke test. Drives `mighty-colab mcp` through the `mcp` SDK's own stdio client -- the same path a real MCP client takes -- across `list_tools()` (exclusion/inclusion), `call_tool("new"/"exec"/"status"/"stop")` against a real CPU VM, an orphan check via `call_tool("sessions", {})` cross-verified with a direct CLI call, and a validation-error path (`call_tool("adopt", {})`) asserting `is_error: True` carries the actual message rather than a bare exit code. Ran live end-to-end; all six phases passed and no orphan VM was left behind.
---

# Design: `colab mcp` — Exposing the CLI as MCP Tools

## Motivation
Agent tooling (Claude and other MCP-aware clients) benefits from calling `mighty-colab`
directly instead of shelling out and scraping output. `mighty-colab mcp` starts a stdio MCP
server that turns the CLI's own Click command tree into an MCP toolset, scanned live
from the same command registry that powers `mighty-colab --help` — one source of truth, no
hand-maintained tool list to keep in sync as commands are added or changed. Commands
that block on a live terminal, an editor, or a Drive re-auth ceremony (`ssh`, `repl`,
`console`, `edit`, `drivemount`) are excluded, since an MCP tool call has no TTY to
satisfy them.

## User Surface

```
mighty-colab mcp
```

| Flag | Type | Default | Purpose |
|---|---|---|---|
| — | — | — | Takes no arguments of its own. Global flags (`--auth`, `--config`, `-c/--client-oauth-config`, `--logtostderr`) apply as normal and are read once, at server startup. |

Starts a stdio MCP server and blocks until the client disconnects (EOF on stdin) or the
process receives a signal. Intended to be launched by an MCP client, not run
interactively — e.g. an MCP client config:

```json
{
  "mcpServers": {
    "mighty-colab": {
      "command": "mighty-colab",
      "args": ["mcp"]
    }
  }
}
```

Any global flag belongs in `args` *before* `mcp` — e.g. `"args": ["--auth", "adc",
"mcp"]` — since it is parsed once by the root `@app.callback()` at startup and never
re-parsed per tool call (see Behavior #4).

## Behavior

1. **Scan**: On startup, walks `click_group.commands` (`typer.main.get_command(app)`)
   once and builds one `mcp.types.Tool` per exposable command, sorted by name.
   `Tool.description` is the command's docstring; `Tool.inputSchema` is a JSON Schema
   object derived from the command's `click.Parameter`s.
2. **Exclude**: A command is exposed only if its name is not in `EXCLUDED_COMMANDS =
   {ssh, repl, console, edit, drivemount, mcp, help, pay}` AND it isn't `hidden=True`
   (`auth`, `keep-alive`, `whoami`, the `README`/`SKILL` aliases). The `--help` option
   Click auto-adds to every command is stripped from the parameter list — it isn't a
   real tool argument. A second, finer-grained map, `EXCLUDED_PARAMS: {command_name:
   {param_names}}`, strips individual parameters from an otherwise-exposed command --
   currently just `log`'s `follow` (`-f`). Unlike a fully-excluded command, `log`'s
   session listing, structured history, and `-n`/`-t`/`-o` export are all fast, bounded,
   single-shot calls and stay exposed; only `--follow` is unsuitable, since it blocks
   the single MCP call for the entire (potentially unbounded) duration of a background
   `exec-async` job and only returns output once, at the very end, not incrementally.
   That's not "streaming" so much as "a call that might never return" -- and MCP's
   plain request/response tool-call model (which Claude Desktop expects) has no way to
   represent it. `exec-async` itself stays fully exposed, `--timeout` and all: it
   returns in around a second regardless of the submitted script's runtime, which is
   exactly the shape an MCP tool call needs -- it's the *right* way to kick off a long
   job from MCP, `log -f` is not. The exclusion is enforced in the parameter list itself
   (`_command_params`), not just the generated schema, so even a client that sends
   `follow` anyway gets it silently dropped before dispatch rather than reaching the
   callback -- defense in depth against exactly the hang this exists to prevent.
3. **Schema generation**: Each parameter maps to a JSON Schema property keyed by
   `param.type.name` (Click's own stable identifier — `"boolean"` / `"integer"` /
   `"float"` / `"text"`), since Typer builds its commands from a vendored copy of
   Click's parameter-type classes (`typer._click.types.*`, not `click.types.*`), so an
   `isinstance` check against the real `click` module never matches. `multiple=True`
   options and variadic (`nargs=-1`) arguments — e.g. `run`'s trailing `script_args`,
   `exec`/`run`'s repeatable `--env` — map to a JSON `array` schema instead of a plain
   string. `click.Choice` values populate `enum`; required parameters populate the
   schema's top-level `required` list.
4. **Dispatch**: `tools/call` invokes the target `click.Command`'s callback directly
   in-process via `Command.invoke(ctx)`, after building `kwargs` from the tool call's
   arguments (converting/validating each value through the parameter's own
   `param.type.convert`, and falling back to `param.get_default(ctx)` for omitted
   optional arguments). This is deliberately **not** a synthesized-argv round trip
   through the group's `main()` — that would re-run the root `@app.callback()`, which
   sets `state.auth_provider`/`state.config_path` from `--auth`/`--config`, and with no
   global flags in a per-call argv those would silently reset to their Typer defaults
   on every single tool call. Invoking the command directly means the root callback
   runs exactly once, at server startup.
5. **Output capture**: Both stdout and stderr are redirected into one buffer during
   dispatch and returned as the tool result's text content. Commands report user-facing
   errors via `typer.echo(..., err=True)` before raising `typer.Exit` — stdout-only
   capture would silently drop the actual error message and return a bare exit code.
6. **Error reporting**: A non-zero `typer.Exit`, a `click.ClickException` (e.g. a
   missing required argument), or any other exception raised by the callback all result
   in `CallToolResult(is_error=True)` with the captured output as the explanation,
   rather than the stdio transport itself failing.

## Testing Strategy (TDD)

### Unit tests (`tests/test_mcp_server.py`)

**Scanning — which commands get exposed**
1. `ssh`/`repl`/`console`/`edit`/`drivemount`/`mcp`/`help` never appear as tools.
2. Every `hidden=True` command in the real CLI is excluded.
3. Ordinary scriptable commands (`new`, `stop`, `status`, `sessions`, `adopt`, `exec`,
   `run`) are included.
4. Every tool has a non-empty description.
5. `--help` never appears as a tool parameter.

**Schema generation**
6. `run`'s schema types are correct (`script`: string, `keep`: boolean with default
   `False`, `timeout`: number with default `30.0`, `required == ["script"]`) — a
   regression guard for the vendored-types `isinstance` bug (Behavior #3).
7. `run`'s variadic `script_args` and repeatable `--env` both produce `{"type":
   "array", "items": {"type": "string"}}`, not a plain string.
8. `adopt`'s schema matches its Click definition (`endpoint`/`name`: string,
   `orphanage`: boolean, no `required` key since every parameter is optional).

**Dispatch**
9. A successful call (`adopt` with a matching endpoint) returns `ok=True` and the
   command's stdout; the underlying store mutation happens exactly once.
10. A call that fails via `typer.echo(err=True)` + `typer.Exit` (`adopt` with neither
    `ENDPOINT` nor `--orphanage`) returns `ok=False` with the actual stderr message —
    not just a bare exit code.
11. A call missing a required argument (`run` with no `script`) returns `ok=False`
    mentioning the missing parameter.
12. Omitting an optional argument (`adopt` without `--name`) falls back to the
    command's own Click default rather than crashing or passing `None`.
13. Excluded commands (`ssh`) are absent from the dispatch table entirely, alongside
    unknown tool names.

**Synthetic command (decoupled from CLI business logic)**
14. A minimal `@click.command()` with an `int` option, a flag, and a `multiple=True`
    option exercises schema generation and dispatch end to end without depending on any
    real subcommand's behavior.

### Integration test (`integration/repro_mcp_server/test.sh`)
Per the "Integration Testing" mandate in `AGENTS.md`, exercises the live protocol
against a real Colab backend — not just mocked unit tests — using the `mcp` SDK's own
client (`mcp.client.stdio`), spawning `mighty-colab mcp` as a subprocess exactly as a
real MCP client (Claude Desktop, etc.) would:

1. `list_tools()` — exclusions and the expected command set (Behavior #1–2).
2. `call_tool("new", ...)` — allocates a real CPU VM.
3. `call_tool("exec", ...)` — runs a script file on it, asserting the output round trip.
4. `call_tool("status", ...)` — reports the session.
5. `call_tool("stop", ...)` then `call_tool("sessions", {})` — tears the VM down and
   confirms no orphan remains, cross-checked with a direct CLI `sessions` call
   independent of the MCP dispatch path under test.
6. `call_tool("adopt", {})` with neither `ENDPOINT` nor `--orphanage` — asserts the
   response is `is_error: True` carrying the actual validation message (Behavior #5–6),
   not an opaque bare exit code.

Run with `uv run bash integration/repro_mcp_server/test.sh`.
