"""On-VM job runtime package.

This package is the `mighty_runtime` module shipped to Colab VMs and executed
standalone. It coordinates job setup, execution, monitoring, and verdict collection.

Key modules:
  - runner: parent process, manages lifecycle and writes verdict
  - shim: spawns user code with proper environment setup
  - ident: PID identity tracking (survives PID reuse via starttime+boot_id)
  - watchdog: monitors resources and enforces wall_clock timeout

All modules use only stdlib and relative imports (no colab_cli).
"""

from hashlib import sha256
from pathlib import Path


def _payload_version() -> str:
    root = Path(__file__).parent
    digest = sha256()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


SCHEMA_VERSION = "1"
RESULT_SCHEMA_VERSION = "2"
RUNTIME_PAYLOAD_VERSION = _payload_version()
