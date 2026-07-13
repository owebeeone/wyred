# emit

```
python3 -m wyred.emit --corpus-dir <dir> [--out <dir>] [--list] [--exemplify <Module>]
```

`wyred.emit` is the EMIT half of the runner. It **discovers** a corpus
(importing a module *is* registering it), **elaborates** every declared intent
and refinement, **resolves** each declared-clean layer 1 to a bound layer-2
netlist, and writes the resulting artifacts to `--out`. It renders no
verdicts — it ends at artifacts on disk. Source:
`wyred/src/wyred/emit.py`.

## Flags

| flag | required | default | meaning |
|---|---|---|---|
| `--corpus-dir <dir>` | yes | — | directory of corpus modules; every `*.py` in it is imported, and importing is registering |
| `--out <dir>` | no | `./out` | output directory for the emitted artifact tree |
| `--list` | no | — | print what discovery found (intents, refinements, every registered module) and exit; see [below](#list) |
| `--exemplify <Module>` | no | — | render one registered module standalone with zero args and print its fragment JSON; see [below](#exemplify) |

`--list` and `--exemplify` are inspection modes: each does its discovery,
prints, and exits **without** writing an artifact tree.

## What an emit run does

1. **Discover.** Every `*.py` under `--corpus-dir` is imported (sorted). The
   corpus dir is imported as a package named after its basename, so corpus
   files may cross-import their shared libraries.
2. **Law-2 sweep.** Every registered module is instantiated with zero
   arguments — self-exemplification is structural. A module that cannot be
   built with no args is a failure.
3. **Elaborate (twice).** Each intent/refinement is elaborated *twice* and the
   two emits are byte-compared; a difference is a determinism failure.
4. **Resolve the declared-clean.** An intent that declares expected layer-1
   codes fails at layer 1 *by design* and gets its `*.l1.json` only. An intent
   whose declared layer-1 is clean is resolved to `*.l2.json` +
   `*.alloc.json`, and the M3 data paths (`*.bom.json`, `*.pinmap.json`,
   `*.records.json`) plus any declared lifecycle/lock/SPICE artifacts are
   written. Which conditional kinds appear is the
   [only-when-declared rule](artifacts.md).

`emit` prints one `EMIT <name> …` line per artifact and a final `RESULT:`
line, then exits **0 iff there were zero failures**. A *failure* here is an
engine-level defect — non-determinism, the resolver refusing a declared-clean
layer 1, a broken law-2 sweep, a lifecycle/lock/incumbency inconsistency — not
an oracle verdict. Whether the layer-1 oracle *agrees* with an intent's
declaration is the harness gate's job, not emit's
(`wyred-harness/harness/gate.py`).

## Example

Emit the reference corpus into a scratch directory:

<!-- cwd: wyred-examples -->
<!-- pythonpath: wyred/src -->
```console
$ OUT=$(mktemp -d)
$ python3 -m wyred.emit --corpus-dir corpus --out "$OUT"
elaborated 20 document(s); artifacts land in /tmp/...

PASS law-2 sweep: all 67 registered modules instantiate with zero args

== emit: layer 1 -> layer 2 (resolver + data paths) ==
EMIT intent_01_sensor_node        declared codes=[]
EMIT intent_03_addr_collision     declared codes=['ADDR_COLLISION']  L2=(none: fails at L1 by design)
       engine: ADDR_COLLISION: address 0x48 on bus 'I2C0' is claimed by roles s1, s2
...
RESULT: PASS (20 artifact(s), 0 failure(s))
# expect: PASS law-2 sweep
# expect: RESULT: PASS
```

Reading the output:

- **`PASS law-2 sweep: all N registered modules …`** — the zero-arg
  instantiation check over every registered module.
- **`EMIT <name> declared codes=[]`** — a declared-clean intent; it earns a
  netlist and the data paths.
- **`… declared codes=['ADDR_COLLISION']  L2=(none: fails at L1 by design)`**
  — an intent that *declares* it fails at layer 1; it gets its `*.l1.json`
  only, and the following `engine:` lines echo the elaboration diagnostics.
- Indented notes such as `connector locks: … frozen by …`, `lifecycle: legal
  fork clean; …`, and `eco vs … row(s) changed, sticky survived` report the
  lock, fork, and ECO paths for the intents that declared them (see
  [artifacts](artifacts.md)).

The emitted set is what [`crosscheck`](crosscheck.md) and [`rebuild`](rebuild.md)
consume from disk.

## `--list` {#list}

`--list` runs discovery and prints the three registries — intents,
refinements (with their base intent, op count, and declared freeze groups),
and every registered module — then exits 0. It writes nothing.

<!-- cwd: wyred-examples -->
<!-- pythonpath: wyred/src -->
```console
$ python3 -m wyred.emit --corpus-dir corpus --list
intents (14):
  intent_01_sensor_node            SensorNode
  ...
refinements (6):
  intent_05a_pinned                PinUart2 of=intent_05a_uart_allocation ops=3 freeze=['firmware-facing']
  ...
registered modules (67):
  ...
# expect: intent_01_sensor_node
# expect: registered modules (
```

Use it to see what a corpus registered before committing to a full emit — a
refinement's `freeze=[…]` column, for instance, previews which artifacts its
emit will retain a [lock baseline](artifacts.md) for.

## `--exemplify` {#exemplify}

`--exemplify <Module>` renders *any* registered module standalone — with zero
arguments — and prints its layer-1 fragment JSON to **stdout** (exit 0). Every
module is renderable this way; that is law 2, the same property the emit-time
law-2 sweep enforces. Engine diagnostics, when a module has demands that need
a surrounding scope, go to **stderr**.

A self-contained passive renders cleanly:

<!-- cwd: wyred-examples -->
<!-- pythonpath: wyred/src -->
```console
$ python3 -m wyred.emit --corpus-dir corpus --exemplify Crystal
{
  "allocation": {
    "entries": [],
    "lock_groups": [],
    "solver_version": "ga019-trivial-1"
  },
  "grounds": [],
  "layer": 1,
  "rails": [],
  "roles": [
    {
      "capabilities": [
        {
          "attrs": {
            "companions": [
              "load_cap",
              "load_cap"
            ]
          },
          "iface": "oscillator"
        }
      ],
      "id": "example",
      "kind": "crystal"
    }
  ],
  "series": "A"
}
# expect: "kind": "crystal"
# expect: "layer": 1
```

A module that normally lives *inside* a scope still renders, but its
scope-dependent demands surface resolution-ladder diagnostics on stderr — for
example `--exemplify TempSensor` prints its fragment on stdout and a
`RAIL_SCOPE …` / `DEMAND_UNSATISFIABLE …` note on stderr, because a lone
sensor has no rail or bus in scope. The fragment is still emitted and the exit
is still 0: exemplification shows you the module's own contribution, out of
context.

An unknown module name is an error (exit 2); `--list` shows the valid names:

<!-- pythonpath: wyred/src -->
<!-- cwd: wyred-examples -->
<!-- expect-fail: unknown module name exits 2 -->
```console
$ python3 -m wyred.emit --corpus-dir corpus --exemplify NoSuchModule
no registered module named 'NoSuchModule'; --list shows them
```
