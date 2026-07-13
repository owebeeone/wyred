# crosscheck

```
python3 -m wyred.crosscheck --dir <artifact-dir> (--artifact <name> | --all)
```

`wyred.crosscheck` re-reads an emitted artifact set **from disk** and re-runs
the engine's own cross-path differential over it: the layer-2 netlist, the
BOM, the pin-map, and the records path must all describe *one* model, with the
`*.l1.json` as the records path's provenance anchor. Where the emit wrote a
`*.cir` SPICE deck it also re-runs the SPICE structural differential, and where
it wrote a `*.connlock.json` it re-runs the connector-pinout lock gate — both
from disk. Source: `wyred/src/wyred/crosscheck.py`.

## Why it exists

The harness gate and `wyred-audit` never import the engine — the trust
boundary that keeps the checkers independent of the thing they check. This CLI
is how they invoke the engine's differential anyway: as a **subprocess** over
the shared artifact directory. Composition happens at the process level, per
`dev-docs/RunnerSplit.md`. The differential codes it emits (the `XPATH_*`
family for the data-path differential, the `XCIR_*` family for SPICE) are the
contract's, indexed in **Part E** of `wyred-contract/EMIT_CONTRACT.md`.

## Flags

| flag | required | meaning |
|---|---|---|
| `--dir <artifact-dir>` | yes | an artifact directory (a `wyred.emit --out` tree) |
| `--artifact <name>` | one of | check one set: `<name>.l1.json` and its siblings |
| `--all` | one of | check every artifact set (`*.l1.json`) in the directory |

`--artifact` and `--all` are mutually exclusive and exactly one is required.

## Output and exit status

One line per failure code found, `<artifact> <CODE>: <msg>`, on **stdout**; a
`crosscheck: N artifact set(s) checked, M failure code(s)` summary on
**stderr**. Exit **0 iff no code fired**, **1** if any did, and **2** on a
missing or unreadable artifact set (a bad `--dir`, a truncated JSON).

A set that failed at layer 1 by design has no netlist and therefore no
secondary paths to differ; it is **noted on stderr and skipped**, not treated
as a failure.

## Examples

Emit the reference corpus, then crosscheck the whole tree:

<!-- cwd: wyred-examples -->
<!-- pythonpath: wyred/src -->
```console
$ OUT=$(mktemp -d)
$ python3 -m wyred.emit --corpus-dir corpus --out "$OUT" >/dev/null
$ python3 -m wyred.crosscheck --dir "$OUT" --all
intent_03_addr_collision: no netlist (fails at layer 1 by design); nothing to crosscheck
intent_07_voltage_mismatch: no netlist (fails at layer 1 by design); nothing to crosscheck
crosscheck: 18 artifact set(s) checked, 0 failure code(s)
# expect: 0 failure code(s)
```

The two declared-failing intents are noted and skipped; the remaining sets are
checked and no differential code fires, so the exit is 0.

Check a single set with `--artifact`:

<!-- cwd: wyred-examples -->
<!-- pythonpath: wyred/src -->
```console
$ OUT=$(mktemp -d)
$ python3 -m wyred.emit --corpus-dir corpus --out "$OUT" >/dev/null
$ python3 -m wyred.crosscheck --dir "$OUT" --artifact watchy_v1_reva
crosscheck: 1 artifact set(s) checked, 0 failure code(s)
# expect: 1 artifact set(s) checked
```

A directory that is not an artifact tree exits 2:

<!-- pythonpath: wyred/src -->
<!-- expect-fail: a missing --dir exits 2 -->
```console
$ python3 -m wyred.crosscheck --dir /no/such/artifact/dir --all
```

See also [`rebuild`](rebuild.md), the sibling from-disk check that proves the
secondary paths re-derive byte-identically from the primaries.
