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

SCHEMA_VERSION = "1"
