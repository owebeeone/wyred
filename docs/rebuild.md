# rebuild

```
python3 -m wyred.rebuild --dir <artifact-dir> (--artifact <name> | --all)
```

`wyred.rebuild` re-derives every **secondary** data path of an emitted
artifact set from its on-disk **primary** artifacts alone — `*.l2.json` +
`*.alloc.json` + `*.l1.json`, with no corpus and no in-memory engine state —
and byte-compares each re-derived document against the emitted file. It is the
from-primaries disk-honesty check: the secondary paths are *pure functions of
the primaries on disk*, and this proves it directly. Source:
`wyred/src/wyred/rebuild.py`.

The secondary paths are the BOM, the pin-map, and the records path; a set that
also emitted a `*.placement.json`, `*.testplan.json`, or `*.cir` deck has those
re-derived and compared too (each is only-when-declared — see
[artifacts](artifacts.md)). The re-derivation uses the engine's one canonical
byte form, so "byte-identical" is exact.

## Why it exists

Like [`crosscheck`](crosscheck.md), `rebuild` lets a consumer that never
imports the engine — the harness gate, `wyred-audit`, a suspicious human —
prove a property **directly on an artifact tree, as a subprocess**, per
`dev-docs/RunnerSplit.md`. It needs *only* the
artifact directory: no `--corpus-dir`. (That distinguishes it from
`wyred-audit`'s whole-corpus re-emit, which re-runs the engine from source to
prove a different property.) The primary/secondary split and the artifact
kinds are the contract's, in **Part E** of `wyred-contract/EMIT_CONTRACT.md`.

## Flags

| flag | required | meaning |
|---|---|---|
| `--dir <artifact-dir>` | yes | an artifact directory (a `wyred.emit --out` tree) |
| `--artifact <name>` | one of | rebuild one set's secondary paths |
| `--all` | one of | rebuild every artifact set (`*.l1.json`) in the directory |

`--artifact` and `--all` are mutually exclusive and exactly one is required.

## Output and exit status

One line per mismatch, `FAIL <artifact> <path> does not rebuild
byte-identically from on-disk inputs`, on **stdout**; a `rebuild: N artifact
set(s) checked, M path(s) mismatched` summary on **stderr**. Exit **0 iff
every secondary path of every checked set is byte-identical**, **1** on any
mismatch, and **2** on a missing or unreadable artifact set.

A set that failed at layer 1 by design has no netlist and therefore no
secondary paths to rebuild; it is **noted on stderr and skipped**.

## Examples

Emit the reference corpus, then rebuild every set from its primaries:

<!-- cwd: wyred-examples -->
<!-- pythonpath: wyred/src -->
```console
$ OUT=$(mktemp -d)
$ python3 -m wyred.emit --corpus-dir corpus --out "$OUT" >/dev/null
$ python3 -m wyred.rebuild --dir "$OUT" --all
intent_03_addr_collision: no netlist (fails at layer 1 by design); nothing to rebuild
intent_07_voltage_mismatch: no netlist (fails at layer 1 by design); nothing to rebuild
rebuild: 18 artifact set(s) checked, 0 path(s) mismatched
# expect: 0 path(s) mismatched
```

Every secondary path re-derived byte-identically from the on-disk primaries,
so the exit is 0. Rebuild a single set — here the SPICE-modelled intent, whose
`*.cir` deck and `*.cir.json` sidecar are rebuilt alongside the BOM/pin-map/
records:

<!-- cwd: wyred-examples -->
<!-- pythonpath: wyred/src -->
```console
$ OUT=$(mktemp -d)
$ python3 -m wyred.emit --corpus-dir corpus --out "$OUT" >/dev/null
$ python3 -m wyred.rebuild --dir "$OUT" --artifact intent_10_spice_divider
rebuild: 1 artifact set(s) checked, 0 path(s) mismatched
# expect: 0 path(s) mismatched
```

A directory that is not an artifact tree exits 2:

<!-- pythonpath: wyred/src -->
<!-- expect-fail: a missing --dir exits 2 -->
```console
$ python3 -m wyred.rebuild --dir /no/such/artifact/dir --all
```
