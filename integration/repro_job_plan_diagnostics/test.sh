#!/bin/bash
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

# `job plan` against real URLs says why each input or source is unusable,
# and `job apply` refuses the plan with the same diagnostics, before any VM
# is assigned. No VM is used. One plan covers:
#   - an input whose measured size differs from size_bytes (fix_code);
#   - an input with no size_bytes, measured by the ranged GET (info);
#   - a 404 (fix_code) and a GCS 403 SignatureDoesNotMatch (refresh_urls),
#     each with the start of the response body;
#   - a host that does not resolve (fix_code);
#   - a signed URL that has already expired (refresh_urls);
#   - a symbolic link in a bundle (source_unreadable, fix_code).
# A second pair of plans shows the install allowance: the same URL passes
# without deps and fails with them. A fabricated signature appears in no
# output and no record but the secrets sidecar.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
TMP_DIR=$(cd "$(mktemp -d)" && pwd -P)
SESSION_FILE="$TMP_DIR/sessions.json"
SENTINEL="PLAN_DIAGNOSTICS_SIGNATURE_SENTINEL_$$"
PINNED="https://raw.githubusercontent.com/danbarua/mighty-colab/011b7978bab3d9356bae8a10f8253411c95ad8dd"
GCS_SIGNED="https://storage.googleapis.com/gcp-public-data-landsat/index.csv.gz?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Credential=nobody%40example.iam.gserviceaccount.com%2F20261005%2Fauto%2Fstorage%2Fgoog4_request&X-Goog-Date=20261005T000000Z&X-Goog-Expires=604800&X-Goog-SignedHeaders=host&X-Goog-Signature=$SENTINEL"

mc() {
    uv run mighty-colab --auth=adc --config "$SESSION_FILE" "$@"
}

JOB_ID=""
# Apply must refuse before assignment; if a regression let it through,
# release whatever it assigned.
cleanup() {
    if [ -n "$JOB_ID" ]; then
        mc --json job destroy "$JOB_ID" --wait 0 >/dev/null 2>&1 || true
    fi
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT
cd "$REPO_ROOT"

mkdir -p "$TMP_DIR/src"
echo "print(1)" >"$TMP_DIR/src/train.py"
ln -s train.py "$TMP_DIR/src/alias.py"

cat >"$TMP_DIR/inputs.yaml" <<YAML
name: plan-diagnostics
accelerator: {prefer: [], accept_cpu: true}
code: {kind: bundle, root: $TMP_DIR/src, entry: train.py}
data:
  - {url: "$PINNED/LICENSE", dest: a.bin, size_bytes: 1}
  - {url: "$PINNED/LICENSE", dest: b.bin}
  - {url: "$PINNED/no-such-file", dest: c.bin, size_bytes: 1}
  - {url: "$GCS_SIGNED", dest: d.bin, size_bytes: 1}
  - {url: "https://no-such-host.invalid/x", dest: e.bin, size_bytes: 1}
  - {url: "$PINNED/LICENSE?Expires=1767225600&Signature=$SENTINEL", dest: f.bin, size_bytes: 11358}
YAML

mc --json job plan "$TMP_DIR/inputs.yaml" >"$TMP_DIR/plan.json" || true
PLAN_JSON="$TMP_DIR/plan.json" uv run python - <<'PY'
import json, os
plan = json.load(open(os.environ["PLAN_JSON"]))
by_field = {}
for d in plan["diagnostics"]:
    print(f'{d["severity"]:5s} {d["code"]} [{d["retry_class"]}]: {d["message"][:220]}')
    by_field.setdefault(d["code"], []).append(d)
def one(code, needle):
    hits = [d for d in by_field.get(code, []) if needle in d["message"]]
    assert len(hits) == 1, (code, needle, by_field.get(code))
    return hits[0]
assert one("data_size_mismatch", "data[0]")["retry_class"] == "fix_code"
assert "is 11358 bytes at the URL but size_bytes declares 1" in one("data_size_mismatch", "data[0]")["message"]
assert one("data_size_probed", "data[1]")["severity"] == "info"
assert one("ranged_get_failed", "data[2].url")["retry_class"] == "fix_code"
assert "response body: 404: Not Found" in one("ranged_get_failed", "data[2].url")["message"]
gcs = one("ranged_get_failed", "data[3].url")
assert gcs["retry_class"] == "refresh_urls", gcs
assert "SignatureDoesNotMatch" in gcs["message"], gcs
unresolved = one("url_host_unresolved", "data[4].url")
assert unresolved["retry_class"] == "fix_code"
assert "no-such-host.invalid: gaierror" in unresolved["message"]
expired = one("url_expiry_too_soon", "data[5].url")
assert "has already expired" in expired["message"] and expired["retry_class"] == "refresh_urls"
source = by_field["source_unreadable"][0]
assert "symbolic links" in source["message"] and "alias.py" in source["message"], source
assert plan["status"] == "error"
print("plan: every unusable input and the source layout named, classified")
PY

JOB_ID=$(PLAN_JSON="$TMP_DIR/plan.json" uv run python -c "import json, os; print(json.load(open(os.environ['PLAN_JSON']))['job_id'])")
mc --json job apply --job-id "$JOB_ID" >"$TMP_DIR/apply.json" || true
APPLY_JSON="$TMP_DIR/apply.json" JOB_DIR="$TMP_DIR/jobs/$JOB_ID" uv run python - <<'PY'
import json, os
refused = json.load(open(os.environ["APPLY_JSON"]))
assert refused["reason"] == "plan_refused", refused
codes = sorted({d["code"] for d in refused["diagnostics"]})
assert "ranged_get_failed" in codes and "url_host_unresolved" in codes, codes
assert not os.path.exists(os.path.join(os.environ["JOB_DIR"], "envelope.json")), "apply got past its preflight"
print("apply: refused before assignment with", len(refused["diagnostics"]), "diagnostics:", ", ".join(codes))
PY

# The install allowance: the same URL, valid for wall_clock + 15 min + 20 min.
EXPIRES=$(( $(date +%s) + 600 + 900 + 1200 ))
for deps in "" "deps: [six]"; do
    name=$([ -n "$deps" ] && echo with-deps || echo no-deps)
    cat >"$TMP_DIR/$name.yaml" <<YAML
name: allowance-$name
accelerator: {prefer: [], accept_cpu: true}
code: {kind: file, root: $TMP_DIR/src, entry: train.py}
budgets: {wall_clock: 600}
data:
  - {url: "$PINNED/LICENSE?Expires=$EXPIRES&Signature=$SENTINEL", dest: a.bin, size_bytes: 11358}
$deps
YAML
    mc --json job plan "$TMP_DIR/$name.yaml" >"$TMP_DIR/$name.json" || true
done
NO_DEPS="$TMP_DIR/no-deps.json" WITH_DEPS="$TMP_DIR/with-deps.json" uv run python - <<'PY'
import json, os
no_deps = json.load(open(os.environ["NO_DEPS"]))
with_deps = json.load(open(os.environ["WITH_DEPS"]))
assert no_deps["status"] == "ok", no_deps["diagnostics"]
[expiry] = [d for d in with_deps["diagnostics"] if d["code"] == "url_expiry_too_soon"]
assert "55m for installing deps" in expiry["message"], expiry
print("allowance: without deps ok; with deps:", expiry["message"][:200])
PY

# The fabricated signature is in the secrets sidecars and nowhere else.
TMP_DIR="$TMP_DIR" SENTINEL="$SENTINEL" uv run python - <<'PY'
import os
from pathlib import Path
sentinel = os.environ["SENTINEL"].encode()
root = Path(os.environ["TMP_DIR"])
leaks = [
    str(p) for p in root.rglob("*")
    if p.is_file() and p.suffix != ".yaml"
    and not p.name.endswith(".mighty-colab-secrets.json")
    and sentinel in p.read_bytes()
]
assert not leaks, leaks
print("redaction: the signature is in no output or record but the secrets sidecars")
PY

echo "[SUCCESS] plan names each unusable input and source problem; apply refuses with the same diagnostics before assignment"
