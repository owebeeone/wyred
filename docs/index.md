# wyred — engine CLI reference

`wyred` is the declarative electronics engine: the authoring surface, the
elaborator, and the data-path emitters. It has exactly three command-line
entry points, and this section is their reference.

The engine's one job is to turn a *corpus* of declared intent into
**artifacts on disk**. It renders no verdicts and imports no checker — the
gate, the audit, and every other consumer meet the engine only at the
artifacts it writes. That process-level boundary (never an import across the
engine/checker fence) is what keeps the engine independent of the things that
judge it; it is described in `dev-docs/RunnerSplit.md`.

## The three CLIs

| CLI | one line | page |
|---|---|---|
| [`wyred.emit`](emit.md) | discover a corpus, elaborate it, write the netlist + data-path + lifecycle artifacts | [emit](emit.md) |
| [`wyred.crosscheck`](crosscheck.md) | re-read an emitted artifact set from disk and re-run the cross-path differential | [crosscheck](crosscheck.md) |
| [`wyred.rebuild`](rebuild.md) | re-derive every secondary data path from the on-disk primaries and byte-compare | [rebuild](rebuild.md) |

`emit` runs first and produces the tree. `crosscheck` and `rebuild` are the
engine's own from-disk re-runs of two of its properties — they exist so a
consumer that never imports the engine (the harness gate, `wyred-audit`, a
suspicious human) can invoke them as **subprocesses** over the shared artifact
directory. Neither reads engine memory; both take only a directory of
artifacts.

## What lands on disk

`emit` writes a *set* of files per artifact — always an `*.l1.json`, and, for
a declared-clean intent, an `*.l2.json` netlist plus its data paths. Several
kinds are written **only when the intent declares the corresponding feature**
(a lock freeze, a fork, a SPICE model, …). That only-when-declared behavior,
and the table of which kind appears when, is on its own page:
[Emitted artifacts — the only-when-declared rule](artifacts.md).

The artifact kinds are **normatively defined by the contract**, not by these
pages: each `*.<kind>.json` validates against
`wyred-contract/schemas/<kind>.schema.json`, indexed in **Part E** of
`wyred-contract/EMIT_CONTRACT.md`. These reference pages explain the CLI
behavior and name the contract files; where a page and a contract file
disagree, the page is wrong.

## Running the engine

The engine is pure-stdlib and ships as a `src/` layout package
(`wyred/pyproject.toml`), so put `wyred/src` on `PYTHONPATH`
(or `pip install -e` the package) before invoking a module:

<!-- cwd: wyred-examples -->
<!-- pythonpath: wyred/src -->
```console
$ python3 -c "import wyred; print(wyred.__name__)"
wyred
# expect: wyred
```

The canonical end-to-end wiring — emit, then the harness gate, then the audit,
then the board-agreement probes — lives in `wyred-examples/run_gate.py`; its
stage 1 is exactly a `wyred.emit` invocation over `wyred-examples/corpus`. The
examples on these pages use that same reference corpus.
