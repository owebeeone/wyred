# Emitted artifacts — the only-when-declared rule

[`emit`](emit.md) writes a *set* of files per artifact. Some are always
present, some appear only for a declared-clean intent, and several appear
**only when the intent or refinement declares the feature that produces
them** — a lock freeze, an incumbent-seeded ECO, a series fork, a SPICE model.
Absence is not an omission: it is the recorded fact that the intent declared
no such thing, and it is what [`crosscheck`](crosscheck.md) and
[`rebuild`](rebuild.md) key on when they decide what an artifact set *owes*.

This page describes that CLI-observable behavior. The artifact **kinds**
themselves — their fields and their meaning — are normatively defined by their
schemas under `wyred-contract/schemas/` (the `schema` column below), indexed
in **Part E** of `wyred-contract/EMIT_CONTRACT.md`; where this page and a
schema disagree, this page is wrong. The producer is the engine's data-path
module, `wyred/src/wyred/paths.py`, driven by `wyred/src/wyred/emit.py`.

## Primary artifacts

| kind | file | emitted when | schema |
|---|---|---|---|
| l1 | `<name>.l1.json` | **always** — every discovered intent and refinement | `schemas/l1.schema.json` |
| l2 | `<name>.l2.json` | the layer 1 is **declared clean** (no expected codes) | `schemas/l2.schema.json` |
| alloc | `<name>.alloc.json` | declared clean | `schemas/alloc.schema.json` |
| bom | `<name>.bom.json` | declared clean | `schemas/bom.schema.json` |
| pinmap | `<name>.pinmap.json` | declared clean | `schemas/pinmap.schema.json` |
| records | `<name>.records.json` | declared clean | `schemas/records.schema.json` |

An intent that **declares** it fails at layer 1 (a non-empty `expected_l1`)
gets its `<name>.l1.json` and nothing else — no netlist, so no data paths. Its
`.l1.json` still carries the failing intent to the checker; whether the oracle
*agrees* the layer 1 is bad is the harness gate's verdict, not emit's.

## Conditional artifacts

Each of these is written **only** for an artifact whose declaration calls for
it:

| kind | file | emitted when the intent/refinement… | schema |
|---|---|---|---|
| baseline | `<name>.baseline.json` | declares a `freeze` that locks a group (any locked emit retains an external baseline) | `schemas/baseline.schema.json` |
| connlock | `<name>.connlock.json` | freezes a group that **covers `connector_pinout`** (a subset of locked emits) | `schemas/connlock.schema.json` |
| pinmapdiff | `<name>.pinmapdiff.json` | declares `incumbents` — an incumbent-seeded ECO re-solve | `schemas/pinmapdiff.schema.json` |
| lifecycle | `<name>.lifecycle.json` | declares a `fork` (a legal series fork off a locked parent) | `schemas/lifecycle.schema.json` |
| cir | `<name>.cir` + `<name>.cir.json` | is **fully SPICE-modelled** or set `emit_spice` (the `.cir` deck is raw ngspice text and carries no schema by design; the `.cir.json` confession sidecar does) | `schemas/cir.schema.json` |

Two further data paths are only-when-declared but are **not yet part of the
JSON schema set** (they have no `schemas/*.schema.json` file; their producers
are the contract-of-record for now):

- `<name>.placement.json` — emitted when the elaborated doc carries a
  `placement` section (producer `paths.build_placement`). The reference corpus
  declares no placement, so **no `.placement.json` is emitted** by it today.
- `<name>.testplan.json` — emitted when the intent declared `expect_*` tests
  (producer `paths.build_testplan`); its authored `declarations` block is the
  primary and its `checks` block the secondary.

## Seeing it on the reference corpus

Emit the corpus and list the conditional artifacts — each belongs to exactly
the intent that declared its feature:

<!-- cwd: wyred-examples -->
<!-- pythonpath: wyred/src -->
```console
$ OUT=$(mktemp -d)
$ python3 -m wyred.emit --corpus-dir corpus --out "$OUT" >/dev/null
$ ls "$OUT" | grep -E '\.(baseline|connlock|pinmapdiff|lifecycle|cir|testplan)' | sort
intent_05a_pinned.baseline.json
intent_10_spice_divider.cir
intent_10_spice_divider.cir.json
mppt_2420_hc_reva.baseline.json
mppt_2420_hc_reva.connlock.json
watchy_v1_bench.testplan.json
watchy_v1_draft_btn3.pinmapdiff.json
watchy_v1_reva.baseline.json
watchy_v1_reva.connlock.json
watchy_v1_revb.baseline.json
watchy_v1_revb.connlock.json
watchy_v1_revb.lifecycle.json
# expect: intent_10_spice_divider.cir
# expect: watchy_v1_revb.lifecycle.json
# expect: watchy_v1_draft_btn3.pinmapdiff.json
```

Note the nesting: `intent_05a_pinned` is *locked* (it has a `baseline`) but its
freeze covers only firmware-facing pins, so it has **no** `connlock`; the MPPT
and Watchy revisions freeze a connector-pinout group and so have both. Only
`watchy_v1_revb` forks, so only it has a `lifecycle`. Only the SPICE-modelled
`intent_10_spice_divider` has a `.cir` deck.

And a declared-failing intent gets its `.l1.json` alone:

<!-- cwd: wyred-examples -->
<!-- pythonpath: wyred/src -->
```console
$ OUT=$(mktemp -d)
$ python3 -m wyred.emit --corpus-dir corpus --out "$OUT" >/dev/null
$ ls "$OUT"/intent_03_addr_collision.*
/tmp/.../intent_03_addr_collision.l1.json
$ test $(ls "$OUT"/intent_03_addr_collision.* | wc -l) -eq 1
# expect: intent_03_addr_collision.l1.json
```

## Why absence is load-bearing

Because a conditional artifact exists **iff** its feature was declared, its
on-disk presence is a frozen gating decision that the from-disk checkers rely
on:

- [`crosscheck`](crosscheck.md) runs the SPICE `XCIR_*` differential for a set
  **iff** a `.cir` deck is present, and the connector-lock gate **iff** a
  `.connlock.json` is present. A set with neither owes neither check — and that
  "owes nothing" was made honest at emit time.
- [`rebuild`](rebuild.md) re-derives `placement`, `testplan`, and the `.cir`
  deck **only** for the sets that emitted them, holding each to the same
  byte-identity standard as the BOM, pin-map, and records.
