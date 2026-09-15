"""Spike-only prototype of the on-VM job runtime (docs/job/design.md).

NOT the shipping implementation. This exists to answer the empirical
questions in that doc's Spike section against a real Colab VM:
detached runner/shim/watchdog survival, result.json fidelity across
exit shapes, cancellation, and descendant escape.
"""

SCHEMA_VERSION = "spike-1"
