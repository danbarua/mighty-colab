#!/usr/bin/env python3
"""Example `job` entry point.

Nothing here is special. There is no SDK to import, no heartbeat to emit,
no framework callback to register: the runner supervises this process from
the outside, so an ordinary script is a valid job.

`sys.path[0]` is this file's directory on the VM, exactly as it is when you
run `python train.py` locally, so sibling imports work.
"""

import argparse
import json
import os
import sys

from model import build_model  # sibling import; resolves because sys.path[0] is here


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=1)
    args = ap.parse_args(argv)

    print(f"argv={sys.argv}", flush=True)
    model = build_model()

    out = "/content/out"
    os.makedirs(out, exist_ok=True)
    for epoch in range(args.epochs):
        # A real job trains here. Printing is optional: the watchdog reports
        # liveness from outside, so a silent job is not a suspicious one.
        print(f"epoch {epoch}", flush=True)

    with open(os.path.join(out, "model.pt"), "w") as f:
        json.dump({"model": model, "epochs": args.epochs}, f)

    # Exit code is the verdict. Raise to fail; return 0 to succeed.
    return 0


if __name__ == "__main__":
    sys.exit(main())
