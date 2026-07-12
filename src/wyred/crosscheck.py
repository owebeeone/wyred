#!/usr/bin/env python3
"""wyred crosscheck — the cross-path differential, re-run FROM DISK.

``python3 -m wyred.crosscheck --dir <artifact-dir> --artifact <name>``
                              re-read one emitted artifact set from disk
                              exactly as ga019's runner did —
                              <name>.l2.json, <name>.bom.json,
                              <name>.pinmap.json, <name>.records.json,
                              with <name>.l1.json as the records path's
                              provenance anchor — and run
                              ``wyred.paths.crosscheck`` over it. Where
                              the emit fired a connector-pinout freeze
                              (it wrote <name>.connlock.json), the
                              ``wyred.paths.check_connector_locks`` gate
                              also re-runs from disk: the alloc artifact's
                              connector rows + series and the l1's
                              ``forked_from``, against the artifact's own
                              retained <name>.baseline.json and — for a
                              fork (<name>.lifecycle.json names the
                              parent) — against the parent's baseline.
``... --dir <artifact-dir> --all``
                              every artifact set (*.l1.json) in the dir.
                              A set without a netlist fails at layer 1 by
                              design and has no paths to differ; it is
                              noted on stderr, not checked.

Output: one line per failure code found (``<artifact> <CODE>: <msg>``) on
stdout; exit 0 iff none fired (2 on a missing or unreadable artifact set).
This CLI exists so the harness gate — which never imports the engine —
can invoke the engine's own differential as a SUBPROCESS over the shared
artifact directory: composition at the process level, per the composition
rule in wyred-wz/dev-docs/RunnerSplit.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from wyred import paths as datapaths


def _read_json(out_dir: Path, name: str, kind: str):
    """``json.loads`` of <dir>/<name>.<kind>.json — the runner's re-read."""
    return json.loads((out_dir / ("%s.%s.json" % (name, kind))).read_text())


def check_artifact(out_dir: Path, name: str):
    """Every structured failure {"code", "msg"} the from-disk gates raise
    for one artifact set, in the order the runner raised them."""
    fails = []
    l1 = _read_json(out_dir, name, "l1")
    l2 = _read_json(out_dir, name, "l2")

    # cross-path differential (the architectural oracle, Gen4 section
    # 1.5): re-read every path FROM DISK and assert the netlist, BOM,
    # pin-map, records AND the emitted l1 (the provenance anchor for the
    # records path) describe one model.
    fails.extend(datapaths.crosscheck(
        l2,
        _read_json(out_dir, name, "bom"),
        _read_json(out_dir, name, "pinmap"),
        _read_json(out_dir, name, "records"),
        l1=l1))

    # SPICE structural oracle (WyredPlanSpice 1.3): the emit fired a ``.cir``
    # data path for this set exactly when the intent was fully-modelled or
    # requested emission — the deck's on-disk EXISTENCE is that frozen gating
    # decision (§0/§6). Where the deck exists, its ``.cir.json`` confession
    # sidecar must too; ``crosscheck_cir`` re-reads both FROM DISK and asserts
    # the third denotation agrees with the L2 (XCIR_* codes). A set with no
    # ``.cir`` owes no spice differential — absence was made honest at emit.
    if (out_dir / ("%s.cir" % name)).exists():
        deck_text = (out_dir / ("%s.cir" % name)).read_text()
        sidecar_path = out_dir / ("%s.cir.json" % name)
        if not sidecar_path.exists():
            fails.append({
                "code": "XCIR_CONFESSION",
                "msg": "%s.cir exists but its %s.cir.json confession sidecar "
                       "is missing" % (name, name)})
        else:
            fails.extend(datapaths.crosscheck_cir(
                l2, deck_text, json.loads(sidecar_path.read_text())))

    # connector-pinout lock gate, from disk. The runner ran it exactly for
    # the emits that fired a group covering connector_pinout — the emits
    # that wrote a .connlock.json — with the alloc artifact's rows and
    # series, the l1's forked_from, and the retained external baselines.
    if (out_dir / ("%s.connlock.json" % name)).exists():
        alloc = _read_json(out_dir, name, "alloc")
        rows = alloc.get("connector_pinout", [])
        series = alloc.get("series", "")
        ff = l1.get("forked_from")
        base_path = out_dir / ("%s.baseline.json" % name)
        if base_path.exists():
            fails.extend(datapaths.check_connector_locks(
                json.loads(base_path.read_text()), rows, series, ff))
        # a fork additionally verifies its rows against the PARENT's
        # external baseline (the lifecycle record names the parent).
        life_path = out_dir / ("%s.lifecycle.json" % name)
        if life_path.exists():
            parent = json.loads(life_path.read_text())["forked_from"]
            pb_path = out_dir / ("%s.baseline.json" % parent)
            if pb_path.exists():
                fails.extend(datapaths.check_connector_locks(
                    json.loads(pb_path.read_text()), rows, series, ff))
            else:
                fails.append({
                    "code": "(no parent baseline)",
                    "msg": "fork parent %r retained no external lock "
                           "baseline (%s.baseline.json missing)"
                           % (parent, parent)})
    return fails


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", required=True, metavar="ARTIFACT_DIR",
                    help="artifact directory (a wyred.emit --out dir)")
    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument("--artifact", metavar="NAME",
                       help="one artifact set (<NAME>.l1.json and friends)")
    which.add_argument("--all", action="store_true",
                       help="every artifact set (*.l1.json) in the dir")
    args = ap.parse_args(argv)

    out_dir = Path(args.dir)
    if not out_dir.is_dir():
        print("not a directory: %s" % out_dir, file=sys.stderr)
        return 2

    if args.all:
        names = sorted(p.name[:-len(".l1.json")]
                       for p in out_dir.glob("*.l1.json"))
        if not names:
            print("no *.l1.json artifact sets in %s" % out_dir,
                  file=sys.stderr)
            return 2
    else:
        if not (out_dir / ("%s.l1.json" % args.artifact)).exists():
            print("no artifact set %r in %s (%s.l1.json missing)"
                  % (args.artifact, out_dir, args.artifact),
                  file=sys.stderr)
            return 2
        names = [args.artifact]

    checked = 0
    fired = 0
    for name in names:
        if not (out_dir / ("%s.l2.json" % name)).exists():
            # a declared-failing layer 1 emits no netlist: there are no
            # secondary paths to differ (the runner never crosschecked it)
            print("%s: no netlist (fails at layer 1 by design); nothing "
                  "to crosscheck" % name, file=sys.stderr)
            continue
        try:
            fails = check_artifact(out_dir, name)
        except (OSError, ValueError, KeyError) as exc:
            print("%s: unreadable artifact set: %r" % (name, exc),
                  file=sys.stderr)
            return 2
        checked += 1
        for f in fails:
            print("%s %s: %s" % (name, f["code"], f["msg"]))
        fired += len(fails)

    print("crosscheck: %d artifact set(s) checked, %d failure code(s)"
          % (checked, fired), file=sys.stderr)
    return 0 if fired == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
