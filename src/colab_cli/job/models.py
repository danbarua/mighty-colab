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

"""Job spec and envelope schemas (`docs/job/design.md`).

Two shapes live here and they are deliberately different:

* the **spec** is what a caller writes -- declarative, validated at `plan`;
* the **envelope** is what the supervisor reports -- four orthogonal fields
  whose terminal values decide `done`, plus a separate `ok` predicate.

The orthogonality matters. Collect/teardown must never overwrite the
workload's verdict: a job that raised and then tore down cleanly is
`failed` + `released`, which is a *healthy failure*, while `succeeded` +
`cleanup: failed` is a leaked VM that happens to have produced results.
Folding those into one status field loses the distinction an agent needs.
"""

import re
import urllib.parse
from enum import Enum
from typing import Dict, List, Literal, Optional

from packaging.requirements import InvalidRequirement, Requirement
from pydantic import BaseModel, ConfigDict, Field, field_validator

from colab_cli.job import RESULT_SCHEMA_VERSION, SCHEMA_VERSION


_HTTP_URL = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def _contains_query_url(values: List[str]) -> bool:
    for value in values:
        for candidate in _HTTP_URL.findall(value):
            candidate = candidate.rstrip("'\")]}>,;")
            try:
                if urllib.parse.urlsplit(candidate).query:
                    return True
            except ValueError:
                if "?" in candidate:
                    return True
    return False


class Workload(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def terminal(self) -> bool:
        return self in {
            Workload.SUCCEEDED,
            Workload.FAILED,
            Workload.CANCELLED,
            Workload.UNKNOWN,
        }


class Offload(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    # "the spec declared no artifacts" -- distinct from SKIPPED, because a job
    # with nothing to upload must still be able to reach `ok`.
    NOT_REQUIRED = "not_required"
    # "artifacts were declared and we deliberately did not upload them"
    SKIPPED = "skipped"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {Offload.OK, Offload.NOT_REQUIRED, Offload.SKIPPED, Offload.FAILED}


class Cleanup(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    RELEASED = "released"
    ALREADY_ABSENT = "already_absent"
    LEFT_UP = "left_up"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {
            Cleanup.RELEASED,
            Cleanup.ALREADY_ABSENT,
            Cleanup.LEFT_UP,
            Cleanup.FAILED,
        }


class Supervisor(str, Enum):
    RUNNING = "running"
    # Transport is failing but the job is not known dead. A 401/404 that
    # survives one token refresh lands here -- never straight to
    # `session_lost`, which would recreate a VM alongside a healthy one.
    DEGRADED = "degraded"
    FINISHED = "finished"
    # The local supervisor died (laptop slept, agent killed). Without this
    # the tuple has no representable state for "nobody is driving", and
    # `done` would stay false forever while a VM billed.
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {Supervisor.FINISHED, Supervisor.INTERRUPTED}


class RetryClass(str, Enum):
    """What the *next agent turn* should do. Keyed off the failure reason,
    never off the phase -- a 429 from a package index and a bad version pin
    both fail `install`, and they need opposite responses."""

    FIX_CODE = "fix_code"
    FIX_HUMAN = "fix_human"
    RETRY_SAME = "retry_same"
    RETRY_DIFFERENT = "retry_different"
    REFRESH_URLS = "refresh_urls"
    DO_NOT_RETRY = "do_not_retry"


class Phase(str, Enum):
    PLAN = "plan"
    PROVISION = "provision"
    INSTALL = "install"
    RESTART = "restart"
    VERIFY = "verify"
    STAGE = "stage"
    RUN = "run"
    OFFLOAD = "offload"
    CLEANUP = "cleanup"


# --------------------------------------------------------------------------
# Spec
# --------------------------------------------------------------------------


class Accelerator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Ordered preference. Apply walks this list; it never silently
    # substitutes. An unrecognised name is a plan error, because upstream
    # `new` maps unknown accelerators to A100 and "silently got something
    # else" is how you publish chance-level science.
    prefer: List[str] = Field(default_factory=lambda: ["T4"])
    accept_cpu: bool = False


class CodeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["file", "bundle"] = "file"
    # `entry` is ALWAYS relative to the remote `src/` root, in both kinds --
    # it is what gets handed to `runpy` on the VM, so it cannot be a local
    # absolute path.
    #
    # `root` names the local directory that becomes that remote root. For
    # `kind: bundle` it is packed and uploaded whole. For `kind: file` only
    # `entry` itself is uploaded. When `root` is omitted it defaults to the
    # directory containing the spec file, which makes a spec relocatable:
    # the same YAML works from any working directory.
    #
    # Made explicit rather than inferred from `entry`'s parent because
    # "which directory is the project" decides what `sys.path[0]` will be
    # on the VM, and therefore whether the script's sibling imports
    # resolve. Inferring it is how you ship a bundle that imports fine
    # locally and dies on the first `from . import utils` remotely.
    root: Optional[str] = None
    entry: str
    args: List[str] = Field(default_factory=list)

    @field_validator("args")
    @classmethod
    def _args_do_not_embed_signed_urls(cls, values: List[str]) -> List[str]:
        if _contains_query_url(values):
            raise ValueError("code arguments must not contain URLs with query credentials")
        return values

    @field_validator("entry")
    @classmethod
    def _entry_is_relative_for_bundles(cls, v: str) -> str:
        if v.startswith("/") or ".." in v.split("/"):
            raise ValueError(
                "entry must be a relative path without '..' -- it is joined "
                "against the remote bundle root"
            )
        return v


class DataItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    dest: str
    sha256: Optional[str] = None
    size_bytes: Optional[int] = None

    @field_validator("sha256")
    @classmethod
    def _sha256_is_full_hex_digest(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if len(value) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value):
            raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
        return value.lower()


class ArtifactItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    url: str
    required: bool = True
    # Optional, and usually genuinely unknown before the run -- nobody can
    # say how big a checkpoint will be at epoch 12. Declared when the
    # caller does know, so `verify`'s disk arithmetic can account for the
    # output as well as the input. Its absence is a warning, not an error,
    # because pretending to know is worse than admitting the gap.
    size_bytes: Optional[int] = None


class ControlChannel(BaseModel):
    """Signed URLs for the same object, one per HTTP method.

    Signed URLs are method-specific: a PUT-signed URL used for a GET is a
    403, not an IAM problem. Modelling them as one field invites exactly
    that mistake, so they are two.
    """

    model_config = ConfigDict(extra="forbid")

    put_url: str
    get_url: str


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: Optional[ControlChannel] = None
    log: Optional[ControlChannel] = None


class Budgets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The only kill that requires nothing from the consumer's code.
    # There is deliberately no stall budget in v0: "stdout went quiet" is
    # what `exec --timeout` does, and it kills healthy JAX/XLA.
    wall_clock: int = 3600

    # Unset (the default) preserves today's behavior: artifacts[] upload
    # exactly once, after `run` finishes. Set this to also re-PUT each
    # declared artifact on this cadence *during* run, so a VM that
    # disappears before offload (destroyed, preempted, wall_clock kill)
    # only loses the interval since the last periodic sync, not
    # everything. Requires the consumer to keep writing to the same
    # declared path each time (a growing log, an overwritten "latest"
    # checkpoint) -- a new path each write is never picked up mid-run,
    # only at the final offload.
    artifact_sync_interval_seconds: Optional[int] = None



class Retry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Named `when`, NOT `on`: YAML 1.1 (which PyYAML implements) resolves a
    # bare `on:` key to the boolean True, so `retry.on` silently becomes a
    # non-string key and the spec fails to validate with a message that
    # points nowhere near the real problem. Caught by round-tripping the
    # shipped example through the real loader.
    #
    # An explicit allow-list, never "anything that failed": retrying a
    # traceback three times is how you burn a four-hour budget on a typo.
    when: List[RetryClass] = Field(default_factory=lambda: [RetryClass.RETRY_SAME])
    max_attempts: int = 1
    budget_seconds: int = 14400
    mode: Literal["recreate", "resume"] = "recreate"


class JobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    code: CodeSpec
    accelerator: Accelerator = Field(default_factory=Accelerator)
    deps: List[str] = Field(default_factory=list)
    data: List[DataItem] = Field(default_factory=list)
    artifacts: List[ArtifactItem] = Field(default_factory=list)
    control: Control = Field(default_factory=Control)
    budgets: Budgets = Field(default_factory=Budgets)
    retry: Retry = Field(default_factory=Retry)
    ignore_warnings: bool = False
    on_offload_fail: Literal["leave_up", "destroy"] = "leave_up"
    on_run_fail: Literal["offload_anyway", "skip"] = "offload_anyway"

    @field_validator("deps")
    @classmethod
    def _deps_are_valid_requirement_specifiers(cls, values: List[str]) -> List[str]:
        """Each dep must be a valid pip requirement specifier (PEP 508)."""
        for dep in values:
            dep = dep.strip()
            if not dep:
                raise ValueError("empty dependency string is not allowed")
            try:
                Requirement(dep)
            except InvalidRequirement as e:
                raise ValueError(f"invalid dependency {repr(dep)}: {e}") from e
        return values

    @field_validator("deps")
    @classmethod
    def _deps_do_not_embed_signed_urls(cls, values: List[str]) -> List[str]:
        if _contains_query_url(values):
            raise ValueError("dependencies must not contain URLs with query credentials")
        return values

    @field_validator("name")
    @classmethod
    def _name_is_path_safe(cls, v: str) -> str:
        if not v or "/" in v or v.startswith("."):
            raise ValueError(
                "name must be non-empty, contain no '/', and not start with '.' "
                "-- it becomes a directory component on both sides"
            )
        return v


# --------------------------------------------------------------------------
# Diagnostics and plan
# --------------------------------------------------------------------------


class Diagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `info` reports what plan measured or assumed; only `error` and `warn`
    # stop apply.
    severity: Literal["error", "warn", "info"]
    code: str
    message: str
    retry_class: Optional[RetryClass] = None
    hint: Optional[str] = None


class SourceFileLock(BaseModel):
    """One source file covered by a plan, identified without absolute paths."""

    model_config = ConfigDict(extra="forbid")

    path: str
    size_bytes: int
    sha256: str

    @field_validator("path")
    @classmethod
    def _relative_posix_path(cls, value: str) -> str:
        if not value or value.startswith("/") or "\\" in value:
            raise ValueError("source path must be a relative POSIX path")
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("source path must not contain empty or parent segments")
        return value

    @field_validator("size_bytes")
    @classmethod
    def _non_negative_size(cls, value: int) -> int:
        if value < 0:
            raise ValueError("size_bytes must be >= 0")
        return value

    @field_validator("sha256")
    @classmethod
    def _sha256_is_full_hex_digest(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value):
            raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
        return value.lower()


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    job_id: str
    spec_hash: str
    created_at: str
    spec: JobSpec
    diagnostics: List[Diagnostic] = Field(default_factory=list)
    # Parsed signed-URL deadlines, so `apply` can revalidate without
    # re-probing. A durable plan applied hours later may carry URLs that
    # have since expired -- cheaper to catch before `assign` than after.
    url_expiry: Dict[str, Optional[str]] = Field(default_factory=dict)
    # Sizes the plan's ranged GET measured for inputs with no declared
    # `size_bytes`, keyed `data[i]`. Used for disk planning only: staging
    # checks declared sizes, so an object replaced after planning is not a
    # failure.
    probed_size_bytes: Dict[str, int] = Field(default_factory=dict)
    source_spec_path: Optional[str] = None
    source_files: List[SourceFileLock] = Field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(d.severity == "error" for d in self.diagnostics)

    @property
    def has_warnings(self) -> bool:
        return any(d.severity == "warn" for d in self.diagnostics)

    def input_bytes(self) -> int:
        """Bytes the declared inputs will stage: each `size_bytes`, or the
        size the plan measured when none is declared."""
        return sum(
            item.size_bytes
            if item.size_bytes is not None
            else self.probed_size_bytes.get(f"data[{index}]", 0)
            for index, item in enumerate(self.spec.data)
        )


# --------------------------------------------------------------------------
# Envelope
# --------------------------------------------------------------------------


# What kind of failure a transfer hit, as the runner classified it on the
# VM: `http` (a response with a non-2xx status), `network` (no response:
# DNS, refused, reset, timeout, TLS, upload cut short), `checksum` and
# `size` (the bytes received do not match the plan), `local` (a file or
# disk error on the VM), `blocked` (the URL resolved to a non-public
# address), `setup` (the runner's own manifest or credential channel is
# unusable) and `error` (anything else).
TransferCategory = Literal[
    "http", "network", "checksum", "size", "local", "blocked", "setup", "error"
]


class TransferError(BaseModel):
    """Why one data download or artifact upload failed, as the runner
    observed it."""

    model_config = ConfigDict(extra="forbid")

    exception: str
    reason: str
    http_status: Optional[int] = None
    # First 300 bytes of the response body, query strings removed.
    body: Optional[str] = None
    category: Optional[TransferCategory] = None

    @property
    def summary(self) -> str:
        """One line: the HTTP status line when there was a response,
        otherwise the exception type and message."""
        if self.http_status is not None:
            return self.reason
        return f"{self.exception}: {self.reason}"


class ArtifactResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    # Query strings carry the signature; never serialise them. `url_id` is a
    # stable, non-secret handle for the same object.
    url_id: str
    status: Literal["ok", "failed", "missing"]
    sha256: Optional[str] = None
    bytes: Optional[int] = None
    error: Optional[TransferError] = None


class InputResult(BaseModel):
    """One `data[]` input as the runner staged it: the bytes the run
    consumed, or why staging stopped there."""

    model_config = ConfigDict(extra="forbid")

    dest: str
    url_id: str
    status: Literal["ok", "failed"]
    sha256: Optional[str] = None
    bytes: Optional[int] = None
    error: Optional[TransferError] = None


class ProvisionAttempt(BaseModel):
    """One accelerator candidate tried during provision."""

    model_config = ConfigDict(extra="forbid")

    accelerator: str
    # `refused_cpu`: a GPU request answered with a CPU VM, released because
    # the spec does not accept CPU.
    outcome: Literal["granted", "failed", "refused_cpu"]
    granted: Optional[str] = None
    http_status: Optional[int] = None
    # Exception type and message, query strings and URL userinfo removed.
    error: Optional[str] = None
    # A JSON response body's first characters, or a note saying a body of
    # another type was not kept (Colab answers some failures with an HTML
    # page that carries no detail).
    body: Optional[str] = None
    retry_class: Optional[RetryClass] = None

    @property
    def summary(self) -> str:
        if self.outcome == "granted":
            return f"{self.accelerator}: granted as {self.granted}"
        if self.outcome == "refused_cpu":
            return (
                f"{self.accelerator}: granted as {self.granted} and released, "
                "because accept_cpu is false"
            )
        return f"{self.accelerator}: {self.error}"


InstallFailure = Literal["resolution", "build", "auth", "transient", "timeout", "unknown"]


class InstallAttempt(BaseModel):
    """One installer run during `install`, as recorded on the VM.

    Kept in the envelope when the install failed or fell back from uv to
    pip; the full output is in the job directory's install.log.
    """

    model_config = ConfigDict(extra="forbid")

    installer: Literal["uv", "pip"]
    version: str
    command: List[str]
    # The index configuration this installer reads (its own environment
    # variables, and pip.conf for pip), redacted. `deps` cannot carry index
    # flags: the planner accepts only PEP 508 requirements.
    index: Dict[str, str] = Field(default_factory=dict)
    exit_code: Optional[int] = None
    timed_out: bool = False
    seconds: float
    failure: Optional[InstallFailure] = None
    key_lines: List[str] = Field(default_factory=list)


class JobEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = RESULT_SCHEMA_VERSION
    cli_version: str = ""
    runtime_payload_version: str = ""
    job_id: str
    phase: Phase = Phase.PLAN
    # The phase whose failure decided the outcome, set once at the first
    # failure; None when nothing failed. `phase` is the last phase reached,
    # which is `cleanup` once the VM has been released.
    failed_phase: Optional[Phase] = None

    workload: Workload = Workload.PENDING
    offload: Offload = Offload.PENDING
    cleanup: Cleanup = Cleanup.PENDING
    supervisor: Supervisor = Supervisor.RUNNING

    session: Optional[str] = None
    # Always carried, even on teardown failure: without it the caller has no
    # handle to retry `unassign` against a VM that is still billing.
    endpoint: Optional[str] = None
    requested_accelerator: Optional[str] = None
    actual_accelerator: Optional[str] = None

    exit_code: Optional[int] = None
    signal: Optional[int] = None
    exception: Optional[Dict[str, str]] = None
    surviving_descendants: List[int] = Field(default_factory=list)

    # Staged inputs up to the first failure; left out when empty.
    inputs: List[InputResult] = Field(
        default_factory=list, exclude_if=lambda inputs: not inputs
    )
    artifacts: List[ArtifactResult] = Field(default_factory=list)
    # Left out when empty, so envelopes of jobs whose install succeeded on
    # the first try stay readable by older CLIs sharing the job store.
    install_attempts: List[InstallAttempt] = Field(
        default_factory=list, exclude_if=lambda attempts: not attempts
    )
    # Each accelerator tried, kept when any of them was not granted;
    # left out when the first candidate was granted.
    provision_attempts: List[ProvisionAttempt] = Field(
        default_factory=list, exclude_if=lambda attempts: not attempts
    )
    retry_class: Optional[RetryClass] = None
    reason: Optional[str] = None
    hints: List[str] = Field(default_factory=list)
    attempt: int = 1
    next_poll_after: int = 15

    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def record_failure(self, phase: Phase) -> None:
        """Record where the job failed. The first failure decides the
        outcome; later ones (a failed release after a failed run) keep it."""
        if self.failed_phase is None:
            self.failed_phase = phase

    @property
    def done(self) -> bool:
        """All four fields terminal. Process exit is *not* done: a workload
        can be finished while offload and teardown are still outstanding."""
        return (
            self.workload.terminal
            and self.offload.terminal
            and self.cleanup.terminal
            and self.supervisor.terminal
        )

    @property
    def ok(self) -> bool:
        """Stricter than `done`, and deliberately separate.

        `left_up` counts as an acceptable cleanup because the caller asked
        for it -- but it is still billing, which is why the envelope always
        carries `endpoint`.
        """
        return (
            self.workload is Workload.SUCCEEDED
            and self.offload in {Offload.OK, Offload.NOT_REQUIRED}
            and self.cleanup
            in {Cleanup.RELEASED, Cleanup.ALREADY_ABSENT, Cleanup.LEFT_UP}
        )
