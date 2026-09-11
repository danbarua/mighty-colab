#!/usr/bin/env python3
"""Diagnose the two local failures: process-group separation and /proc identity."""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mighty_runtime import ident  # noqa: E402

print(f"platform={sys.platform}")
print(f"/proc exists: {os.path.isdir('/proc')}")

me = os.getpid()
print(f"self pid={me} pgid={os.getpgid(me)}")
print(f"self starttime={ident.starttime(me)!r} boot_id={ident.boot_id()!r}")
print(f"self alive-check={ident.alive(me, ident.starttime(me), ident.boot_id())}")

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(5)"], start_new_session=True
)
time.sleep(0.3)
print(f"child pid={child.pid} pgid={os.getpgid(child.pid)} runner_pgid={os.getpgid(me)}")
print(f"SEPARATE GROUPS: {os.getpgid(child.pid) != os.getpgid(me)}")
child.kill()
child.wait()
