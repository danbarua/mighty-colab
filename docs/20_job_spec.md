---
log:
2026-09-13: First version. Field list and everyday examples for a `job` spec, written for a reader who has not opened `docs/08_job.md`. Schema and plan refusals match `src/colab_cli/job/models.py` and `planner.py`. Signed HTTPS URLs are the data plane. The CLI does not mint them.
---

# Job spec files

A spec is a YAML file that describes one unattended Colab job.

`mighty-colab job plan` reads that file. It writes a local plan. It does not allocate a VM.

`docs/08_job.md` is the design record. `docs/09_job_usage.md` is the command guide. This file is the spec itself.

Unknown fields are rejected. Extra keys fail validation.

## Commands

```bash
mighty-colab job plan SPEC_FILE [--out PATH] [--no-probe]
mighty-colab job apply --job-id JOB_ID
mighty-colab --json job status JOB_ID
mighty-colab job destroy JOB_ID
```

`plan` mints the job id. Read it from `--json` output. Do not scrape the human line.

```bash
JID=$(mighty-colab --json job plan spec.yaml | jq -r .job_id)
mighty-colab job apply --job-id "$JID"
```

`apply` is the only command that spends. `destroy` is safe to run twice.

## Smallest spec that can plan

The entry file must exist on disk next to the spec, or under `code.root`.

```yaml
name: hello-cpu

accelerator:
  accept_cpu: true

code:
  kind: file
  entry: train.py

budgets:
  wall_clock: 600
```

`name` must not be empty. It must not contain `/`. It must not start with `.`.

`code.entry` must be a relative path. It must not contain `..`. It must not start with `/`.

Paths in `code` resolve relative to the spec file, not the working directory.

A shipped example lives at `examples/job/train_cls.yaml`.

## Fields

Required:

| Field | Meaning |
|---|---|
| `name` | Job name. It becomes a directory component on the laptop and on the VM. |
| `code.entry` | Script that `runpy` executes as `__main__`. |

Common optional fields. Defaults apply when you omit them.

| Field | Default | Meaning |
|---|---|---|
| `code.kind` | `file` | `file` uploads only the entry. `bundle` uploads `code.root` (no `.git`, no `.venv`, no `__pycache__`). |
| `code.root` | Directory of the spec | Local directory that becomes remote `src/` and `sys.path[0]`. |
| `code.args` | `[]` | `sys.argv[1:]` on the VM. Must not contain URLs with query credentials. |
| `accelerator.prefer` | `[T4]` | Tried in order. Unknown names are plan errors. Nothing is substituted in silence. |
| `accelerator.accept_cpu` | `false` | Set `true` if a CPU VM is acceptable. |
| `deps` | `[]` | `pip` pins. The kernel restarts after install. Must not contain URLs with query credentials. |
| `data` | `[]` | HTTPS GET inputs. The VM downloads them before your script starts. |
| `artifacts` | `[]` | HTTPS PUT outputs. The runner uploads them after the script exits. |
| `control.result` | omitted | Optional off-VM copy of `result.json`. |
| `control.log` | omitted | Not implemented. Presence is a plan error. |
| `budgets.wall_clock` | `3600` | Watchdog kill in seconds. There is no stall kill. |
| `retry.when` | `[retry_same]` | Must stay this value. Other values are plan errors. |
| `retry.max_attempts` | `1` | Must stay `1`. |
| `retry.mode` | `recreate` | Must stay `recreate`. |
| `retry.budget_seconds` | `14400` | Used to check control-URL expiry. It does not run a retry loop. |
| `on_run_fail` | `offload_anyway` | Must stay this value. `skip` is a plan error. |
| `on_offload_fail` | `leave_up` | `leave_up` or `destroy`. |
| `ignore_warnings` | `false` | `apply` refuses plan warnings unless this is `true`. |

Accepted accelerator names: `T4`, `L4`, `G4`, `H100`, `A100`, `v5e1`, `v6e1`.

Relative `data[].dest` and `artifacts[].path` values resolve under `/content/jobs/<id>`. Absolute paths must stay under `/content`. Do not target `mighty_runtime` or supervisor files such as `result.json`.

`data[].sha256`, when present, must be exactly 64 hexadecimal characters.

## Everyday specs

### CPU script, no data plane

Use this when the script and its inputs are small enough to upload as source.

```yaml
name: summarize
accelerator:
  accept_cpu: true
code:
  kind: file
  entry: summarize.py
budgets:
  wall_clock: 900
```

### GPU training with pinned deps

`kind: bundle` is required when the entry imports sibling modules.

```yaml
name: train-cls
accelerator:
  prefer: [A100, L4, T4]
  accept_cpu: false
code:
  kind: bundle
  root: ./src
  entry: train.py
  args: ["--epochs", "50"]
deps:
  - torch==2.4.1
  - numpy==1.26.4
budgets:
  wall_clock: 7200
```

If Colab cannot grant a name in `prefer`, provision fails with `retry_class: retry_different`. It does not run on CPU unless `accept_cpu: true`.

### Large inputs and outputs

Do not put large arrays in the source bundle. The Contents upload path has a 250 MB per-file ceiling. Plan reports an error for any source file over that size. Put the bytes in object storage. Give the VM HTTPS URLs.

```yaml
name: train-cls
accelerator:
  prefer: [T4]
  accept_cpu: false
code:
  kind: bundle
  root: ./src
  entry: train.py
data:
  - url: https://storage.googleapis.com/YOUR-BUCKET/cls/x.npy?X-Goog-Signature=...
    dest: /content/data/x.npy
    sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    size_bytes: 104857600
artifacts:
  - path: /content/out/model.pt
    url: https://storage.googleapis.com/YOUR-BUCKET/runs/model.pt?X-Goog-Signature=...
    required: true
    size_bytes: 52428800
```

The runner streams each data GET and each artifact PUT while it computes SHA-256.

Omit `size_bytes` on an artifact only when you do not know the size. That omission is a plan warning. `apply` then refuses the plan unless `ignore_warnings: true`.

A missing optional artifact (`required: false`) does not fail offload. A failed PUT currently fails scalar offload even when the artifact is optional.

### Off-VM result backstop

`control.result` is optional. The runner PUTs terminal `result.json` to an object that remains readable if the VM later becomes unreachable.

Signed URLs are method-specific. A PUT URL used for GET returns 403.

For GCS, create the object before you sign GET.

```bash
OBJECT=gs://YOUR-BUCKET/runs/$JOB_ID/result.json
SIGNER=your-signer@YOUR-PROJECT.iam.gserviceaccount.com
REGION=US

PUT_URL="$(gcloud storage sign-url "$OBJECT" \
  --impersonate-service-account="$SIGNER" --region="$REGION" \
  --http-verb=PUT --duration=8h --format='value(signed_url)')"
printf '{}\n' | curl --fail --silent --show-error -X PUT \
  -H 'Content-Type: application/octet-stream' --data-binary @- "$PUT_URL"
GET_URL="$(gcloud storage sign-url "$OBJECT" \
  --impersonate-service-account="$SIGNER" --region="$REGION" \
  --http-verb=GET --duration=8h --format='value(signed_url)')"
```

Put those two values under `control.result.put_url` and `control.result.get_url`. They must name one bucket and one object.

Do not pass `--headers` to `sign-url`. The runner sends `Content-Type: application/octet-stream`. A signed header that does not match that value returns 403.

`{}` is a placeholder. It is not a verdict.

## Signed URLs

The VM has no GCS client. It has no service-account key. It uses `urllib` against HTTPS.

A signed URL is an HTTPS URL with a time-limited signature in the query string. You create the object in a bucket the VM can reach. You sign the URL on your laptop. You paste the URL into the spec.

The CLI does not mint signed URLs from ordinary user ADC.

Treat every signed URL as a credential. Generated `spec.json`, `plan.json`, remote manifests, envelopes, and kernel history store query-free identities only. Full URLs remain in your source spec and in the mode-0600 `.mighty-colab-secrets.json` sidecar next to the plan. Keep those files private.

### What must exist before `job plan` accepts data URLs

1. The object exists in the bucket.
2. The URL uses `https`.
3. The host is a public address. Private and link-local hosts are rejected.
4. The URL is signed for GET. A PUT signature fails the ranged probe.
5. Expiry covers `budgets.wall_clock` plus 15 minutes.
6. `sha256` is 64 hexadecimal characters when you set it.

Plan performs a one-byte ranged GET on each `data[]` URL unless you pass `--no-probe`. HTTP 403 or 404 is a plan error.

`--no-probe` skips that laptop GET. The VM still downloads the object during `apply`. A bad URL then fails on the VM, after allocation.

### What must exist before `job plan` accepts artifact and control URLs

1. The URL uses `https`.
2. The host is public.
3. Artifact and data expiry cover `wall_clock` plus 15 minutes.
4. Control expiry covers `retry.budget_seconds` plus 15 minutes.
5. GCS `control.result` PUT and GET identify the same object.

Plan does not mutate artifact or control destinations. It does not probe them with GET.

### How to sign without a key file

Use a dedicated service account. Grant it object admin on one bucket. Grant yourself `roles/iam.serviceAccountTokenCreator` on that service account. Impersonate it.

```bash
gcloud storage sign-url gs://YOUR-BUCKET/path/object \
  --impersonate-service-account=your-signer@YOUR-PROJECT.iam.gserviceaccount.com \
  --region=YOUR-LOCATION \
  --http-verb=GET \
  --duration=8h \
  --format='value(signed_url)'
```

Pass `--region`. `objectAdmin` does not grant `storage.buckets.get`. Auto-detect of the region then fails and looks like a missing IAM role.

Enable `iamcredentials.googleapis.com` before impersonation. A disabled API also looks like a missing role.

IAM changes can take about one minute. If `get-iam-policy` already shows the binding, wait and retry.

Prove the path before you depend on it:

1. Sign PUT.
2. Upload with `curl`.
3. Sign GET.
4. Read the object.
5. Compare bytes.
6. Delete the test object.

A GET-signed URL returns 403 on PUT and on HEAD.

## What `job plan` requires

Work through this list when plan exits non-zero.

1. The file is YAML. The top level is a mapping.
2. It is your source spec, not a redacted `spec.json` from the job store.
3. `name` and `code.entry` are set. The entry file exists on disk.
4. For `kind: bundle`, `entry` stays under `root`.
5. `prefer` names only known accelerators.
6. `retry` uses the defaults above, or you omit the block.
7. `control.log` is absent. `on_run_fail` is `offload_anyway` or omitted.
8. Every URL is `https` and public.
9. Data objects exist and accept a GET-signed ranged read, unless `--no-probe`.
10. Signed expiry covers the budgets plus 15 minutes.
11. GCS control PUT and GET name one object.
12. A source file over 250 MB is a plan error. A source tree whose total size exceeds 250 MB is a plan warning. Move large files to `data[]`.
13. `data[].dest` and `artifacts[].path` stay under `/content` and do not collide.
14. Missing `size_bytes` on data or artifacts is a warning. Set `ignore_warnings: true` only if you accept that gap.

Plan still writes records when diagnostics contain errors. `apply` refuses a plan that has errors.

Re-run `job plan` after every source change. The plan locks each source file path, size, and SHA-256. `apply` refuses added, removed, renamed, or changed files before assignment.

## After a clean plan

```bash
mighty-colab job apply --job-id "$JID"
mighty-colab --json job status "$JID" | jq '{done, ok}'
mighty-colab job destroy "$JID"
mighty-colab sessions
```

`job apply` exits non-zero when the job did not succeed. `job status` exits zero when the query succeeded, even if the workload failed. Branch on `.ok`.

Limits that still apply:

- No GPU run completed through the approximately 60-minute proxy refresh.
- Retry, resume, and `control.log` are not implemented.
- Caller-owned specs and secret sidecars still contain full signed URLs.
- After launch, a dropped laptop session does not kill the consumer. `job status --poll` recovers an orphaned supervisor. It does not implement full supervisor takeover.

Use `mighty-colab sessions` after every interrupted run. Destroy any endpoint you no longer need.
