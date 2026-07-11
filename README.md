# wyred — the declarative electronics engine (authoring surface + elaborator + data-path emitters). Port of elecscad ga019 `modeller/`.

## CLIs (BUILT)

```sh
# emit: corpus -> artifacts on disk (l1/l2/alloc/bom/pinmap/records, plus the
# retained external baseline/lifecycle/connlock artifacts — emit-time CONTENT;
# see dev-docs/DecisionLog.md 2026-07-12 item a). Also --list / --exemplify.
python3 -m wyred.emit --corpus-dir <dir> --out <dir>

# crosscheck: the engine's own from-disk cross-path differential +
# connector-lock gate; invoked by the harness gate and wyred-audit as a
# SUBPROCESS (the sanctioned composition mechanism — never imported).
python3 -m wyred.crosscheck --dir <artifact-dir> --all

# rebuild: from-primaries disk honesty — re-derive every SECONDARY path
# (bom/pinmap/records) from the on-disk PRIMARY artifacts alone
# (l2 + alloc + l1; no corpus needed) and byte-compare against the emitted
# files; one line per mismatch, exit 0 iff all byte-identical.
python3 -m wyred.rebuild --dir <artifact-dir> [--artifact NAME | --all]
```

Everything that only GATES a run lives elsewhere and reads these artifacts
from disk: `wyred-harness/harness/gate.py` (verdicts) and `wyred-audit`
(consumer trust) — see `wyred-wz/dev-docs/RunnerSplit.md`.
