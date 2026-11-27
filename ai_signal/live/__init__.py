"""Live network adapters -- the deliberately non-deterministic edge.

Everything under `ai_signal/` (the parent package) is tested to be free
of AI models, randomness and network I/O -- see
`tests/test_determinism.py`, which asserts this structurally by parsing
each top-level module's imports. That test globs `ai_signal/*.py`
only, so it does not (and should not) reach into this subpackage:
`live/` exists specifically to talk to the outside world, and nothing
here is claimed to be repeatable.

The boundary is deliberate, not accidental: `BraveSearchProvider` and
`HttpPageFetcher` implement the `SearchProvider` / `PageFetcher`
protocols from `ai_signal.providers` and hand their output straight to
the deterministic core (`pipeline.run`). Once a candidate crosses that
boundary, everything downstream -- fusion, the verification gate, dedup,
the report -- is exactly as deterministic and tested as it was with the
`Static*` fixtures. Record a live run with `ai_signal.record` and it
becomes replayable too.
"""
