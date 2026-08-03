# Mighty-Colab: A Mightier Interface for Colab

A command-line interface for Google Colab with quality-of-life improvements for humans and AI agents.
Provision high-performance CPU, GPU, and TPU runtimes, execute local code, manage remote files, and orchestrate automated cloud pipelines — directly from your terminal, or via embedded MCP server.

Designed to support seamless developer productivity, headless automation, and AI agent integrations.
This CLI can co-exist with the official `colab` CLI.

[Demo](https://github.com/user-attachments/assets/656226a9-af13-4fdb-8eda-d7de747336a2)

> [!NOTE]
> **Platform support:** the Colab CLI currently supports **Linux and macOS** only. Windows is not supported at this time.

> [!TIP]
> Looking for in-notebook, interactive agent-assisted coding instead of a terminal workflow? See the [Official Colab MCP Server](https://github.com/googlecolab/colab-mcp).

> [!TIP]
> This project embeds an MCP Server wrapper around automation-friendly CLI commands.

---

> [!NOTE]
> What problem does this project solve?
> 1) `mighty-colab adopt ENDPOINT` brings a Colab runtime that was started outside the CLI (e.g. from the Colab web UI) under local session tracking, so `stop`/`status`/`exec` etc. can manage it.
> 2) `mighty-colab adopt --orphanage` does the same for every such orphaned runtime at once.
> 3) Embeds an MCP server (`mighty-colab mcp`) so AI agents can call these commands as tools directly, without shelling out.

---

## Key Features

* **Instant VM Provisioning:** Spin up CPU, GPU (T4, L4, G4, H100, A100), or TPU (v5e1, v6e1) runtimes in seconds.
* **Robust Code Execution:** Run local Python scripts, Jupyter Notebooks (`.ipynb`), or piped `stdin` code; launch interactive REPLs or raw TTY console shells.
* **Ephemeral Job Runner (`mighty-colab run`):** Provision a fresh VM, execute a local script with forwarded arguments, retrieve output files, and automatically tear down the runtime in a single command.
* **Automatic Keep-Alive:** Built-in background daemon automatically prevents idle VM termination, keeping resource allocations active without requiring open browser tabs.
* **Seamless Workspace Automation:** Mount Google Drive, authenticate Google Cloud Platform (GCP) credentials, and install dependencies with high-performance `uv` package management.
* **State & Log Archival:** Inspect local session states or export interactive history logs to standard Jupyter Notebooks, Markdown, or structured JSONL.
* **Orphan Recovery (`mighty-colab adopt`):** Bring a Colab runtime started outside the CLI (e.g. from the web UI) under local session tracking, one at a time or all at once with `--orphanage`.
* **Embedded MCP Server (`mighty-colab mcp`):** Expose the CLI's own commands as MCP tools over stdio, so AI agents can call them directly instead of shelling out.

---

## Installation

`mighty-colab` is [published on PyPI](https://pypi.org/project/mighty-colab/). Install it using `uv`
(recommended) or standard `pip`:

```bash
# Using uv (recommended)
uv tool install mighty-colab
# Using pip
pip install mighty-colab
```

---

## Quick Start

Run a CPU-based VM runtime, execute some code, and clean up:

```bash
# 1. Provision a new session
mighty-colab new

# 2. Execute code from stdin
echo "print('Hello from Google Colab!')" | mighty-colab exec

# 3. Stop and release the VM resource
mighty-colab stop
```

> [!NOTE]
> When only one session is active, you can omit the `-s, --session` option;
> the CLI automatically knows it.

---

## MCP Server Configuration

`mighty-colab` embeds an MCP (Model Context Protocol) server, exposing its commands as
tools for AI agents like Claude. Since the package is on PyPI, `uvx` can run it directly
without a separate install step:

```json
{
  "mcpServers": {
    "mighty-colab": {
      "command": "uvx",
      "args": [
        "mighty-colab",
        "mcp"
      ],
      "env": {
        "UV_WORKING_DIR": "/Optional/Path/To/Working_Dir"
      }
    }
  }
}
```

See [MCP Server Design](docs/07_mcp_server.md) for which commands are exposed as tools
and how global flags (`--auth`, `--config`) can be added to `args`.

---

## Command Index

Run `mighty-colab <command> --help` to view specific options, defaults, and detailed help.

### Session Management
| Command | Description |
| --- | --- |
| `mighty-colab new [-s NAME] [--gpu GPU] [--tpu TPU]` | Allocate a new CPU, GPU, or TPU VM runtime |
| `mighty-colab sessions` | List all active sessions currently active on the backend |
| `mighty-colab status [-s NAME]` | Display hardware, status, and local metadata for active sessions |
| `mighty-colab restart-kernel [-s NAME]` | Restart the active session's Jupyter kernel |
| `mighty-colab stop [-s NAME]` | Terminate a session VM and tear down its keep-alive daemon |
| `mighty-colab url [-s NAME] [--open]` | Print or open a browser URL connecting to the active session |
| `mighty-colab adopt ENDPOINT [-n NAME]` | Bring a runtime started outside the CLI under local session tracking |
| `mighty-colab adopt --orphanage` | Adopt every orphaned server-side assignment at once |

### Execution
| Command | Description |
| --- | --- |
| `mighty-colab run [--gpu GPU] [--tpu TPU] [--keep] SCRIPT [ARGS...]` | Run a local script on a fresh VM, forwarding arguments, then release it |
| `mighty-colab exec [-s NAME] [-f FILE] [--output-image PATH]` | Execute Python code from stdin, a local `.py` file, or a `.ipynb` notebook |
| `mighty-colab repl [-s NAME] [--output-image PATH]` | Start an interactive Python REPL on the VM (exits cleanly on piped EOF) |
| `mighty-colab console [-s NAME]` | Connect to a raw interactive TTY shell (tmux) on the remote VM |
| `mighty-colab ssh [-s NAME] [--proxy-mode] [-i KEY]` | Open an SSH shell to the runtime over WebSocket, or act as an OpenSSH `ProxyCommand` bridge for IDE remote-dev |

### File Operations
| Command | Description |
| --- | --- |
| `mighty-colab ls [-s NAME] [PATH]` | List remote files on the VM |
| `mighty-colab upload [-s NAME] LOCAL REMOTE` | Upload a local file to the VM filesystem |
| `mighty-colab download [-s NAME] REMOTE LOCAL` | Download a remote file from the VM filesystem |
| `mighty-colab rm [-s NAME] PATH` | Delete a remote file on the VM filesystem |
| `mighty-colab edit [-s NAME] PATH` | Edit a remote file in-place using your local `$EDITOR` |

### Automation & Utilities
| Command | Description |
| --- | --- |
| `mighty-colab auth [-s NAME]` | Authenticate the VM for GCP services (BigQuery, GCS, etc.) |
| `mighty-colab drivemount [-s NAME] [PATH]` | Mount Google Drive on the VM (default: `/content/drive`) |
| `mighty-colab install [-s NAME] [-r FILE \| PKG...]` | Install packages on the VM using `uv` (falls back to `pip`) |
| `mighty-colab reinstall [-s NAME] [-r FILE \| PKG...]` | Same as `install`, then restarts the kernel on success so an already-imported package's new version takes effect |
| `mighty-colab log [-s NAME] [-n N] [-o FILE]` | View or export session history (`.ipynb`, `.md`, `.txt`, `.jsonl`) |
| `mighty-colab pay` | Open the Colab subscription page to manage compute units |
| `mighty-colab version` | Print the installed version of the CLI |
| `mighty-colab update [--install]` | Check for a newer release (and optionally upgrade the CLI in place) |
| `mighty-colab mcp` | Start a stdio MCP server exposing these commands as tools for AI agents |

### Global Options
* `--auth {oauth2,adc}` — Authentication strategy for the Colab API (default: `adc`).
* `-c, --client-oauth-config PATH` — Path to public OAuth client credentials configuration (default: `~/.colab-cli-oauth-config.json`).
* `--config PATH` — Path to local session metadata storage (default: `~/.config/colab-cli/sessions.json`).
* `--logtostderr` — Direct debug logging output to stderr.

---

## Practical Examples

### Accelerator Training with Checkpoint Retrieval

Provision an A100 GPU, install requirements, run a local training script, retrieve the resulting model weights, and terminate the VM:

```bash
mighty-colab new -s trainer --gpu A100
mighty-colab install -s trainer torch transformers
mighty-colab exec -s trainer -f train.py
mighty-colab download -s trainer checkpoints/model.bin ./model.bin
mighty-colab stop -s trainer
```

### Workspace Notebook Execution with Drive Integration

Mount Google Drive, run a local notebook against the VM kernel (outputs are written back into `report_output.ipynb`), export a Markdown log of the execution, and clean up:

```bash
mighty-colab new -s analysis
mighty-colab drivemount -s analysis
mighty-colab exec -s analysis -f report.ipynb
mighty-colab log -s analysis -o execution_log.md
mighty-colab stop -s analysis
```

---

## Usage Notes

* **TTY Requirements:** The interactive commands `repl` and `console` require a local TTY. When running inside automated scripts or pipelines, make sure to pipe stdin (e.g., `echo "print(1)" | mighty-colab repl`) to trigger non-interactive execution modes.
* **Transparent Code Execution:** When calling `mighty-colab exec -f file.py`, the CLI reads the file locally and transmits its content to the remote kernel. You do not need to manually upload files before execution.
* **Storage & State Paths:** Session tokens and metadata are stored at `~/.config/colab-cli/sessions.json`. Global CLI settings are located at `~/.config/colab-cli/settings.json`. These can be customized or isolated via the global `--config` flag.

### Ephemeral Accelerator Jobs

Use `mighty-colab run` to run a local script on dedicated hardware without manual session lifecycle management. The CLI handles provisioning, script execution, and immediate VM teardown automatically:

```bash
# Run train.py on a T4 GPU and release the VM on completion
mighty-colab run --gpu T4 train.py
```

### Shebang Execution Support

To execute a local file directly on a remote accelerator, place the `mighty-colab run` interpreter in the shebang line:

```python
#!/usr/bin/env -S mighty-colab run --gpu L4 --keep
import torch

print("L4 GPU Available:", torch.cuda.is_available())
print("Device Name:", torch.cuda.get_device_name(0))
```

Make the script executable (`chmod +x script.py`) and run it: `./script.py`. The `--keep` option tells the CLI to preserve the session VM on completion so you can re-execute or inspect logs.

---

## Deep Dive Documentation

For comprehensive architectural overviews and deep-dives into specific CLI sub-systems, refer to the detailed documentation:

* [Session Management & Keep-Alive Architecture](docs/01_session_management.md)
* [Interactive & Non-Interactive Execution Design](docs/02_execution_and_interactive.md)
* [File Management & Jupyter Contents API](docs/03_file_management.md)
* [Authentication Providers & VM Automation](docs/04_automation_and_utility.md)
* [Ephemeral Job Runner Design](docs/05_run_command.md)
* [SSH-over-WebSocket Runtime Access](docs/06_ssh_access.md)
* [MCP Server Design](docs/07_mcp_server.md)

To view interactive walkthroughs of eleven real-world automated scenarios, check out the [Demo Walkthroughs](docs/demos.md).

---

## Contributing

Feedback and contributions are welcome! Please read [`CONTRIBUTING.md`](./CONTRIBUTING.md) for details.
