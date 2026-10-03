# Runs on a Colab VM. For uv and pip each, records how an install fails in
# each case `job` must classify, with the same flags `job apply` uses, and
# writes the outputs to /content/installer_failures.json.
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

HEAD = 2000
TAIL = 12000


class StatusIndex(http.server.BaseHTTPRequestHandler):
    """Answers /s<code>/... with that HTTP status, as a package index would."""

    def do_GET(self):
        code = int(self.path.strip("/").split("/")[0][1:])
        body = f"status {code}\n".encode()
        self.send_response(code)
        if code == 429:
            self.send_header("Retry-After", "1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def run(cmd):
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600
        )
        output, code = proc.stdout, proc.returncode
    except subprocess.TimeoutExpired as error:
        output, code = (error.stdout or ""), None
    return {
        "command": cmd,
        "exit_code": code,
        "seconds": round(time.time() - started, 1),
        "output_head": output[:HEAD],
        "output_tail": output[-TAIL:],
        "output_chars": len(output),
    }


server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), StatusIndex)
threading.Thread(target=server.serve_forever, daemon=True).start()
local = f"http://127.0.0.1:{server.server_address[1]}"

build_dir = tempfile.mkdtemp()
broken = os.path.join(build_dir, "mc_build_fails")
os.makedirs(broken)
with open(os.path.join(broken, "setup.py"), "w") as f:
    f.write("raise SystemExit('mc_build_fails: build failed on purpose')\n")

uv = shutil.which("uv")
NOT_INSTALLED = "pip-install-test==0.5"
cases = {
    "missing_package": ["mighty-colab-no-such-package==0.0.1"],
    "missing_version": ["requests==0.0.999"],
    "conflict": ["requests==2.31.0", "requests==2.32.3"],
    # A package Colab does not preinstall, so the installer must ask the index.
    "index_unresolvable": ["--index-url", "https://no-such-host.invalid/simple", NOT_INSTALLED],
    "index_refused": ["--index-url", "http://127.0.0.1:9/simple", NOT_INSTALLED],
    "index_401": ["--index-url", f"{local}/s401/simple", NOT_INSTALLED],
    "index_403": ["--index-url", f"{local}/s403/simple", NOT_INSTALLED],
    "index_429": ["--index-url", f"{local}/s429/simple", NOT_INSTALLED],
    "index_500": ["--index-url", f"{local}/s500/simple", NOT_INSTALLED],
    "build_failure": [broken],
}
installers = {
    "uv": lambda args: [uv, "pip", "install", "--system", *args],
    "pip": lambda args: [
        sys.executable, "-m", "pip", "install", "-v",
        "--upgrade-strategy", "only-if-needed",
        *(["--trusted-host", "127.0.0.1"] if any(local in a or "127.0.0.1:9" in a for a in args) else []),
        *args,
    ],
}

results = {
    "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "python": sys.version,
    "uv_path": uv,
    "uv_version": run([uv, "--version"])["output_tail"].strip() if uv else None,
    "pip_version": run([sys.executable, "-m", "pip", "--version"])["output_tail"].strip(),
    "pip_config": run([sys.executable, "-m", "pip", "config", "list"])["output_tail"],
    "installer_env": {k: v for k, v in os.environ.items() if k.startswith(("PIP_", "UV_"))},
    "cases": {},
}
for name, args in cases.items():
    results["cases"][name] = {}
    for installer, build in installers.items():
        if installer == "uv" and not uv:
            continue
        results["cases"][name][installer] = run(build(args))
        print(name, installer, results["cases"][name][installer]["exit_code"], flush=True)

with open("/content/installer_failures.json", "w") as f:
    json.dump(results, f, indent=1)
print("CAPTURED", len(results["cases"]), "cases")
