#!/usr/bin/env python3
"""A workload that misbehaves on demand, so the supervisor's verdict can
be checked against a KNOWN answer instead of a plausible one.

Every mode maps to exactly one expected terminal state. If the runner
reports something else, the runner is wrong -- that is the whole point.

  ok          count, print, exit 0                 -> succeeded, exit 0
  sysexit0    sys.exit(0) after work               -> succeeded, NO exception.json
  exitcode    sys.exit(N)                          -> failed, exit N
  crash       raise ValueError at depth            -> failed, exception.json ValueError
  crash_early raise at module scope (no main)      -> failed, exception.json
  quiet       no output at all for --seconds       -> succeeded (silence != stall)
  osexit      os._exit(7): no interpreter unwind   -> failed exit 7, NO exception.json
  sigkill     SIGKILL self                         -> failed signal 9, NOT cancelled
  doublefork  detached grandchild outlives us      -> exit 0 BUT survivors non-empty
  sibling     import a sibling module              -> succeeded only if sys.path[0] is right
  hog         allocate until OOM-killed            -> failed by signal, NOT cancelled
"""

import argparse
import os
import signal
import sys
import time


def _work(seconds, quiet, label):
    deadline = time.time() + seconds
    n = 0
    while time.time() < deadline:
        n += 1
        if not quiet:
            print(f"[payload:{label}] tick {n}", flush=True)
        time.sleep(1)
    return n


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", default="ok")
    p.add_argument("--seconds", type=float, default=5)
    p.add_argument("--code", type=int, default=3)
    args = p.parse_args()

    print(f"[payload] mode={args.mode} pid={os.getpid()} argv={sys.argv}", flush=True)

    if args.mode == "ok":
        _work(args.seconds, False, "ok")
        print("[payload] done", flush=True)
        return

    if args.mode == "sysexit0":
        _work(args.seconds, False, "sysexit0")
        sys.exit(0)

    if args.mode == "exitcode":
        _work(args.seconds, False, "exitcode")
        sys.exit(args.code)

    if args.mode == "crash":
        _work(args.seconds, False, "crash")

        def inner():
            raise ValueError("payload crashed on purpose")

        inner()
        return

    if args.mode == "quiet":
        _work(args.seconds, True, "quiet")
        return

    if args.mode == "osexit":
        _work(args.seconds, False, "osexit")
        sys.stdout.flush()
        os._exit(7)

    if args.mode == "sigkill":
        _work(args.seconds, False, "sigkill")
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGKILL)

    if args.mode == "doublefork":
        # Escape the process group: the classic way a "finished" job
        # leaves something holding the GPU.
        if os.fork() == 0:
            os.setsid()
            if os.fork() == 0:
                with open("/tmp/mighty_spike_escapee.log", "w") as f:
                    f.write(f"escapee pid={os.getpid()} pgid={os.getpgid(0)}\n")
                time.sleep(60)
                os._exit(0)
            os._exit(0)
        time.sleep(2)
        print("[payload] parent exiting, grandchild still alive", flush=True)
        return

    if args.mode == "sibling":
        import sibling_helper  # noqa: F401  - must resolve via sys.path[0]

        print(f"[payload] sibling says {sibling_helper.MARKER}", flush=True)
        return

    if args.mode == "hog":
        blocks = []
        while True:
            blocks.append(bytearray(256 * 1024 * 1024))
            print(f"[payload] allocated {len(blocks) * 256}MB", flush=True)
            time.sleep(0.5)

    raise SystemExit(f"unknown mode {args.mode}")


if __name__ == "__main__":
    main()
