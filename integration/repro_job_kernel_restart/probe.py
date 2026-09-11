#!/usr/bin/env python3
import argparse
import json
import os
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=90)
    args = parser.parse_args()

    output = Path("/content/out/restart_probe.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    identity = {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "sid": os.getsid(0),
        "started_at": started_at,
    }
    final_tick = max(1, args.seconds // 2)

    for tick in range(final_tick + 1):
        state = {
            **identity,
            "tick": tick,
            "elapsed_seconds": time.time() - started_at,
            "completed": tick == final_tick,
        }
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, sort_keys=True))
        os.replace(temporary, output)
        if state["completed"]:
            break
        time.sleep(2)

    print(json.dumps(state, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
