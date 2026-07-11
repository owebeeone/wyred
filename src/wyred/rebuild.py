#!/usr/bin/env python3
"""wyred rebuild — the FROM-PRIMARIES rebuild honesty check, re-run FROM DISK.

``python3 -m wyred.rebuild --dir <artifact-dir> --artifact <name>``
                              re-derive one emitted artifact set's SECONDARY
                              data paths from its on-disk PRIMARY artifacts
                              alone — <name>.l2.json + <name>.alloc.json +
                              <name>.l1.json, no corpus, no in-memory engine
                              state — exactly as ga019's runner did (the M4
                              disk-honesty check): ``wyred.paths.build_bom``
                              from (l2, alloc), ``wyred.paths.build_pinmap``
                              from (l2, alloc) — the pin-map's wiring view
                              is persisted in the alloc artifact, so it is
                              held to the same standard as bom/records —
                              and ``wyred.paths.build_records`` from
                              (l1, alloc); then byte-compare each re-derived
                              document (``wyred.paths.json_str``, the one
                              canonical byte form) against the on-disk
                              <name>.bom.json / <name>.pinmap.json /
                              <name>.records.json.
``... --dir <artifact-dir> --all``
                              every artifact set (*.l1.json) in the dir.
                              A set without a netlist fails at layer 1 by
                              design and has no secondary paths to rebuild;
                              it is noted on stderr, not checked.

Output: one line per mismatch (``FAIL <artifact> <path> does not rebuild
byte-identically from on-disk inputs``) on stdout; exit 0 iff every
secondary path of every checked set is byte-identical (2 on a missing or
unreadable artifact set). This CLI exists so a consumer that never imports
the engine — the harness gate, wyred-audit, a suspicious human — can prove
the property DIRECTLY on an artifact tree as a SUBPROCESS: the secondary
paths are pure functions of the primary artifacts on disk, not of anything
the emit knew in memory. Composition at the process level, per the
composition rule in wyred-wz/dev-docs/RunnerSplit.md. (wyred-audit's
whole-corpus re-emit proves a different property and needs --corpus-dir;
this check needs only the artifact dir.)
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
    """The secondary path names that do NOT rebuild byte-identically from
    one artifact set's on-disk primaries, in the order the runner checked
    them (bom, pinmap, records)."""
    # disk honesty (M4, closes the M3 pin-map asymmetry): every secondary
    # path must rebuild BYTE-IDENTICALLY from the on-disk primary artifacts
    # alone (l2 + alloc + l1) — the pin-map's wiring view is persisted in
    # the alloc artifact, so it is held to the same standard as bom/records.
    g_d = _read_json(out_dir, name, "l2")
    a_d = _read_json(out_dir, name, "alloc")
    l_d = _read_json(out_dir, name, "l1")
    rebuilt = {
        "bom": datapaths.json_str(datapaths.build_bom(name, g_d, a_d)),
        "pinmap": datapaths.json_str(
            datapaths.build_pinmap(name, g_d, a_d)),
        "records": datapaths.json_str(
            datapaths.build_records(name, l_d, a_d)),
    }
    mismatched = []
    for path_name, want in rebuilt.items():
        got = (out_dir / ("%s.%s.json" % (name, path_name))).read_text()
        if want != got:
            mismatched.append(path_name)
    return mismatched


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
    mismatches = 0
    for name in names:
        if not (out_dir / ("%s.l2.json" % name)).exists():
            # a declared-failing layer 1 emits no netlist: there are no
            # secondary paths to rebuild (the runner never checked it)
            print("%s: no netlist (fails at layer 1 by design); nothing "
                  "to rebuild" % name, file=sys.stderr)
            continue
        try:
            bad = check_artifact(out_dir, name)
        except (OSError, ValueError, KeyError) as exc:
            print("%s: unreadable artifact set: %r" % (name, exc),
                  file=sys.stderr)
            return 2
        checked += 1
        for path_name in bad:
            print("FAIL %-28s %s does not rebuild byte-identically "
                  "from on-disk inputs" % (name, path_name))
        mismatches += len(bad)

    print("rebuild: %d artifact set(s) checked, %d path(s) mismatched"
          % (checked, mismatches), file=sys.stderr)
    return 0 if mismatches == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
