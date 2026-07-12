#!/usr/bin/env python3
"""wyred emit — the EMIT half of ga019's runner (composition law 1, mechanical).

``python -m wyred.emit --corpus-dir <dir> --out <dir>``
                              discover every declared intent module in the
                              corpus dir (importing IS registering),
                              elaborate each (twice — the emits must be
                              byte-identical), write <out>/<intent>.l1.json
                              per EMIT_CONTRACT. For every intent whose
                              layer 1 is DECLARED clean (``expected_l1`` is
                              empty), the M2 RESOLVER then produces the
                              bound netlist — <out>/<name>.l2.json +
                              <out>/<name>.alloc.json (resolved twice,
                              byte-compared) — and the M3 data paths
                              (Gen4 section 1.5): <out>/<name>.bom.json,
                              <out>/<name>.pinmap.json (stamped with
                              (series, lock versions)) and
                              <out>/<name>.records.json. A refinement
                              declaring ``freeze`` fires its lock groups at
                              ITS netlist emit (the sync point); the emit
                              loop retains the external ``snapshot_locks``
                              baseline (<out>/<name>.baseline.json). A
                              refinement declaring ``incumbents`` re-solves
                              with the named artifact's allocation record
                              as incumbents (minimal disturbance) and gets
                              an <out>/<name>.pinmapdiff.json ECO view; a
                              refinement declaring ``fork`` is verified as
                              a LEGAL series fork against the parent's
                              external baseline, with tamper counter-probes
                              (locked-edit-without-fork must flag
                              LOCK_VIOLATION, hand-edited series must flag
                              SERIES_UNJUSTIFIED), recorded in
                              <out>/<name>.lifecycle.json; an emit that
                              fired a group covering connector_pinout gets
                              its lock gate recorded in
                              <out>/<name>.connlock.json.
``... --list``                show what discovery found (intents,
                              refinements, and every registered module).
``... --exemplify <ModuleName>``
                              render ANY registered module standalone with
                              zero args (law 2), fragment JSON to stdout.

There are ZERO per-intent mains and zero per-intent code paths here: one
generic loop imports <corpus>/*.py (importing IS registering), one generic
engine elaborates whatever was declared, one generic resolver binds it.

This module ends at artifacts-on-disk. Everything that only GATES a run
consumes these artifacts from disk; the engine never imports a checker
(see wyred-wz/dev-docs/RunnerSplit.md). The layer-1 oracle gate, the v3
stack (spec_satisfaction / erc / invariant / allocation), and the
cross-path differential gate — with its XPATH counter-probe battery and
the all-probes-must-fire lobotomy verdict — live in
wyred-harness/harness/gate.py; disk-rebuild honesty and external-baseline
tamper auditing live in wyred-audit. ``python3 -m wyred.crosscheck`` is
the engine's own from-disk re-run of the differential, invokable by those
gates as a subprocess.
"""

from __future__ import annotations

import argparse
import copy
import importlib
import json
import sys
from pathlib import Path

from wyred import (INTENTS, MODULES, REFINEMENTS, ModellerError,
                   elaborate, exemplify, resolve)
from wyred import paths as datapaths


def discover(corpus_dir: Path) -> None:
    """Import every corpus module (sorted): declaration = registration.

    The corpus dir is imported as a package named after its basename (its
    parent goes on sys.path), so corpus files may cross-import their shared
    libraries the way ga019's did (``from corpus.lib_parts import ...``)."""
    corpus_dir = Path(corpus_dir).resolve()
    parent = str(corpus_dir.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    for py in sorted(corpus_dir.glob("*.py")):
        if py.stem == "__init__":
            continue
        importlib.import_module("%s.%s" % (corpus_dir.name, py.stem))


def _jobs():
    """(name, intent_cls, ops, freeze, incumbents, fork) per artifact —
    refinements carry their op set, declared freeze groups, and the M3
    lifecycle declarations."""
    jobs = []
    for name, cls in sorted(INTENTS.items()):
        jobs.append((name, cls, (), (), None, None))
    for name, ref in sorted(REFINEMENTS.items()):
        base = INTENTS.get(ref.of)
        if base is None:
            raise ModellerError(
                "REFINEMENT_DANGLING",
                "refinement %r refines unknown intent %r" % (name, ref.of))
        jobs.append((name, base, tuple(ref.ops), tuple(ref.freeze),
                     ref.incumbents, ref.fork))
    return jobs


def _emit_all():
    """Elaborate every discovered intent and refinement twice (the emits
    must be byte-identical). Returns [(name, result, det_ok, freeze,
    incumbents, fork)]. Incumbent/fork references resolve against EARLIER
    artifacts in the canonical job order (a dangling or forward reference
    is a structured load error — lineage is explicit, never guessed)."""
    emitted = []
    by_name = {}
    for name, cls, ops, freeze, incumbents, fork in _jobs():
        inc_entries = None
        if incumbents is not None:
            parent = by_name.get(incumbents)
            if parent is None:
                raise ModellerError(
                    "INCUMBENTS_DANGLING",
                    "refinement %r declares incumbents=%r, which is not an "
                    "earlier artifact in the canonical order"
                    % (name, incumbents))
            inc_entries = list(
                parent.doc.get("allocation", {}).get("entries", []))
        fork_tuple = None
        if fork is not None:
            parent = by_name.get(fork["of"])
            if parent is None:
                raise ModellerError(
                    "FORK_DANGLING",
                    "refinement %r forks from %r, which is not an earlier "
                    "artifact in the canonical order" % (name, fork["of"]))
            fork_tuple = (parent.doc.get("series", "A"), fork["series"],
                          fork.get("reason", ""))
        res1 = elaborate(cls, doc_name=name, ops=ops,
                         incumbents=inc_entries, fork=fork_tuple)
        res2 = elaborate(cls, doc_name=name, ops=ops,
                         incumbents=inc_entries, fork=fork_tuple)
        # the l1 doc AND the (non-doc) elaborated test declarations must both
        # be byte-stable across two elaborations — test decls ride on the
        # EmitResult, not the doc, so they need their own compare.
        deterministic = (
            res1.to_json_str() == res2.to_json_str()
            and (json.dumps(res1.test_declarations, sort_keys=True)
                 == json.dumps(res2.test_declarations, sort_keys=True)))
        by_name[name] = res1
        emitted.append((name, res1, deterministic, freeze, incumbents, fork))
    return emitted


def _expectations(name: str):
    """An artifact's expected oracle codes + escalation flag come from the
    intent's (or refinement's) OWN declaration; a refinement inherits its
    base intent's expectations unless it overrides them."""
    if name in INTENTS:
        cls = INTENTS[name]
        return sorted(cls.expected_l1), bool(cls.expect_escalation)
    ref = REFINEMENTS[name]
    base = INTENTS[ref.of]
    exp = ref.expected_l1 if ref.expected_l1 is not None \
        else base.expected_l1
    esc = ref.expect_escalation if ref.expect_escalation is not None \
        else base.expect_escalation
    return sorted(exp), bool(esc)


def _emit_spice_requested(name: str) -> bool:
    """Whether the intent (or a refinement's base intent) requested SPICE
    emission for a partially-modelled netlist (WyredSpiceContract §6). A
    refinement inherits its base intent's request unless it overrides it — the
    same inheritance ``_expectations`` applies to ``expected_l1``."""
    if name in INTENTS:
        return bool(getattr(INTENTS[name], "emit_spice", False))
    ref = REFINEMENTS[name]
    val = getattr(ref, "emit_spice", None)
    if val is None:
        val = getattr(INTENTS[ref.of], "emit_spice", False)
    return bool(val)


def _check_incumbency(parent_entries, child_entries):
    """Minimal disturbance, checked mechanically: every SOLVER-chosen entry
    of the incumbent record must survive unchanged in the re-solve unless a
    change made that impossible (its unit taken by another demand, the
    demand author-pinned or gone). Returns violation strings."""
    out = []
    child_rows = {(e["pool"], e["unit"], e["demand"])
                  for e in child_entries}
    child_demands = {}
    unit_owner = {}
    for e in child_entries:
        child_demands.setdefault(e["demand"], []).append(e)
        unit_owner[(e["pool"], e["unit"])] = e["demand"]
    for e in parent_entries:
        if e.get("chosen_by") != "solver":
            continue
        row = (e["pool"], e["unit"], e["demand"])
        if row in child_rows:
            continue
        taken_by = unit_owner.get((e["pool"], e["unit"]))
        if taken_by is not None and taken_by != e["demand"]:
            continue    # unit taken by another demand: a forced move
        kids = child_demands.get(e["demand"], [])
        if not kids:
            continue    # demand gone: not a disturbance
        if all(k.get("chosen_by") == "author" for k in kids):
            continue    # author re-pinned it: authored change, not churn
        out.append("incumbent (pool=%r, unit=%r, demand=%r) was moved to %s "
                   "although its unit stayed free — the re-solve is not "
                   "minimal-disturbance"
                   % (e["pool"], e["unit"], e["demand"],
                      [(k["pool"], k["unit"]) for k in kids]))
    return out


# ---------------------------------------------------------------------------
# External lock baselines (emit-side). These are dict ports of the PUBLIC
# harness helpers ``allocation.snapshot_locks`` / ``check_lock_violations``
# operating directly on the emitted layer-1 JSON — the engine never imports
# a checker, but the baseline it RETAINS at each locked emit (and the fork
# record it writes) must be byte-for-byte what the harness gate consumes:
# canonical entry dicts, groups sorted by name, entries by
# (pool, unit, demand).
# ---------------------------------------------------------------------------

def _canonical_entry(d):
    """The canonical JSON form of one allocation entry (exactly what the
    harness's ``schema_l1._alloc_entry_to_json`` emits after a
    ``from_json`` round-trip)."""
    return {
        "pool": d["pool"],
        "unit": int(d["unit"]),
        "demand": d["demand"],
        "chosen_by": d.get("chosen_by", "solver"),
        "state": d.get("state", "free"),
        "locked_by": d.get("locked_by"),
    }


def _covered_entries(rec, group):
    """The canonical JSON forms of the record entries covered by ``group``,
    sorted by (pool, unit, demand) — exactly the shape a lock snapshot
    freezes. Empty when the group covers no allocation decision class."""
    if "pool_allocation" not in (group.get("covers") or []):
        return []
    ents = [_canonical_entry(e) for e in rec.get("entries", [])]
    return sorted(ents, key=lambda e: (e["pool"], e["unit"], e["demand"]))


def snapshot_locks(l1):
    """An external, JSON-safe snapshot of an emitted layer-1 document's
    lock-relevant state:

        {"series": ..., "solver_version": ...,
         "groups": {name: {"version": int, "covers": [...],
                           "entries": [canonical covered entry dicts]}}}

    Taken at a lock point (artifact emit) and retained EXTERNALLY — the
    embedded per-group snapshot travels with the document and is
    informational only."""
    rec = l1.get("allocation", {})
    groups = {}
    for g in sorted(rec.get("lock_groups", []), key=lambda g: g["name"]):
        groups[g["name"]] = {
            "version": int(g.get("version", 0)),
            "covers": sorted(g.get("covers", [])),
            "entries": _covered_entries(rec, g),
        }
    return {
        "series": l1.get("series", "A"),
        "solver_version": rec.get("solver_version", ""),
        "groups": groups,
    }


def _diff_entry_lists(before, after):
    """Diff two lists of canonical entry dicts, keyed by demand id.

    Returns {"added": [demand...], "removed": [demand...],
             "changed": [demand...]} with each list sorted; all three empty
    means no drift. A demand served by several entries (qty > 1) compares as
    the sorted tuple of its entries."""
    def by_demand(entries):
        m = {}
        for d in entries:
            key = str(d.get("demand"))
            m.setdefault(key, []).append(json.dumps(d, sort_keys=True))
        return {k: sorted(v) for k, v in m.items()}

    b, a = by_demand(before), by_demand(after)
    added = sorted(k for k in a if k not in b)
    removed = sorted(k for k in b if k not in a)
    changed = sorted(k for k in a if k in b and a[k] != b[k])
    return {"added": added, "removed": removed, "changed": changed}


def check_lock_violations(baseline, l1):
    """The section-2.5 gate against an EXTERNAL baseline snapshot, codes
    only (the lifecycle record stores sorted code lists): for every group
    that was LOCKED in the baseline (version >= 1), its covered decisions
    in ``l1`` must be identical unless the series was LEGITIMATELY forked
    (a ``forked_from`` record naming the baseline series). A version
    rollback or a deleted locked group is likewise flagged; a differing
    series WITHOUT the fork record is SERIES_UNJUSTIFIED."""
    out = []
    if l1.get("series", "A") != baseline.get("series"):
        ff = l1.get("forked_from")
        if (isinstance(ff, dict)
                and ff.get("series") == baseline.get("series")):
            return out  # legal fork (break_lock): locked edits permitted
        out.append("SERIES_UNJUSTIFIED")
        return out
    cur = snapshot_locks(l1)
    for name, bg in sorted(baseline.get("groups", {}).items()):
        if bg["version"] < 1:
            continue  # never locked; free to drift
        ag = cur["groups"].get(name)
        if ag is None:
            out.append("LOCK_VIOLATION")
            continue
        if ag["version"] < bg["version"]:
            out.append("LOCK_VIOLATION")
        ediff = _diff_entry_lists(bg["entries"], ag["entries"])
        if any(ediff.values()):
            out.append("LOCK_VIOLATION")
    return out


def emit_artifacts(emitted, out_dir: Path) -> int:
    """Resolve every DECLARED-clean layer 1 to layer 2 and write every
    emit-time artifact (netlist + the M3 BOM/pin-map/records data paths,
    external lock baselines, ECO pin-map diffs, fork lifecycle records,
    connector-lock records). Returns the number of failures. Verdicts are
    NOT rendered here — the oracle stack gates these artifacts from disk,
    in wyred-harness."""
    out_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    resolved = {}                 # artifact -> ResolveResult (rr1)
    baselines = {}                # artifact -> external snapshot_locks
    pinmaps = {}                  # artifact -> written pin-map dict

    # law 2 sweep: EVERY registered module (parts included) instantiates
    # with zero args — self-exemplification is structural, not a convention.
    broken = []
    for mname, mcls in sorted(MODULES.items()):
        try:
            mcls()
        except Exception as exc:            # noqa: BLE001 - report, don't die
            broken.append((mname, exc))
    if broken:
        failures += 1
        print("\nFAIL law-2 sweep: %d module(s) not zero-arg instantiable:"
              % len(broken))
        for mname, exc in broken:
            print("  %s: %s" % (mname, exc))
    else:
        print("\nPASS law-2 sweep: all %d registered modules instantiate "
              "with zero args" % len(MODULES))

    print("\n== emit: layer 1 -> layer 2 (resolver + data paths) ==")
    for name, res, deterministic, freeze, incumbents, fork in emitted:
        expected_codes, _expect_esc = _expectations(name)

        # ---- the M2 data path: only a DECLARED-clean layer 1 earns a ------
        # netlist (an intent declaring expected layer-1 codes fails at L1
        # by design and gets its L1 artifact only; whether the oracle
        # AGREES with the declaration is the harness's verdict)
        wants_l2 = not expected_codes
        if wants_l2:
            try:
                rr1 = resolve(res.doc, name, bindings=res.bindings,
                              freeze=freeze)
                rr2 = resolve(res.doc, name, bindings=res.bindings,
                              freeze=freeze)
            except ModellerError as exc:
                failures += 1
                print("FAIL %-28s resolver refused: %s" % (name, exc))
                (out_dir / ("%s.l1.json" % name)).write_text(
                    res.to_json_str())
                continue
            l2_det = (rr1.graph_json_str() == rr2.graph_json_str()
                      and rr1.alloc_json_str() == rr2.alloc_json_str()
                      and rr1.l1_json_str() == rr2.l1_json_str())

            # artifacts: the (possibly freeze-updated) L1, the netlist, the
            # stamped allocation record — all from THIS emit
            (out_dir / ("%s.l1.json" % name)).write_text(rr1.l1_json_str())
            (out_dir / ("%s.l2.json" % name)).write_text(
                rr1.graph_json_str())
            (out_dir / ("%s.alloc.json" % name)).write_text(
                rr1.alloc_json_str())

            # ---- M3 secondary data paths: BOM / pin-map / records --------
            # (the SAME emit, resolved twice: every path must be
            # byte-deterministic too)
            bom = datapaths.build_bom(name, rr1.graph, rr1.alloc)
            pinmap = datapaths.build_pinmap(name, rr1.graph, rr1.alloc,
                                            rr1.alloc_wiring)
            records = datapaths.build_records(name, rr1.l1, rr1.alloc)
            paths_det = (
                datapaths.json_str(bom) == datapaths.json_str(
                    datapaths.build_bom(name, rr2.graph, rr2.alloc))
                and datapaths.json_str(pinmap) == datapaths.json_str(
                    datapaths.build_pinmap(name, rr2.graph, rr2.alloc,
                                           rr2.alloc_wiring))
                and datapaths.json_str(records) == datapaths.json_str(
                    datapaths.build_records(name, rr2.l1, rr2.alloc)))
            l2_det = l2_det and paths_det
            (out_dir / ("%s.bom.json" % name)).write_text(
                datapaths.json_str(bom))
            (out_dir / ("%s.pinmap.json" % name)).write_text(
                datapaths.json_str(pinmap))
            (out_dir / ("%s.records.json" % name)).write_text(
                datapaths.json_str(records))

            # ---- placement data path: L1-derived, only-when-declared -------
            # (the .connlock.json/.baseline.json only-when-applicable
            # pattern). Emitted IFF the elaborated doc carries a placement
            # section, from the SAME layer-1 written to disk (rr1.l1), so the
            # rebuild-from-primaries check reproduces it byte-identically.
            # The current corpus declares no placement, so it emits none.
            if rr1.l1.get("placement"):
                placement = datapaths.build_placement(name, rr1.l1)
                if (datapaths.json_str(placement)
                        != datapaths.json_str(
                            datapaths.build_placement(name, rr2.l1))):
                    l2_det = False
                (out_dir / ("%s.placement.json" % name)).write_text(
                    datapaths.json_str(placement))

            # ---- testplan data path: derived checks, only-when-declared ----
            # (proposal section 3: the elaborated declarations ride on the
            # EmitResult, NEVER in the l1 document — l1.json stays
            # byte-identical and the harness schema_l1 is untouched — so the
            # testplan is written straight from res.test_declarations + the
            # SAME records + pin-map of this emit; its checks re-derive from
            # those primaries at rebuild time. Emitted IFF the intent declared
            # expect_* tests; today's golden corpus declares none.)
            if res.test_declarations:
                testplan = datapaths.build_testplan(
                    name, res.test_declarations, records, pinmap)
                records2 = datapaths.build_records(name, rr2.l1, rr2.alloc)
                pinmap2 = datapaths.build_pinmap(
                    name, rr2.graph, rr2.alloc, rr2.alloc_wiring)
                if (datapaths.json_str(testplan) != datapaths.json_str(
                        datapaths.build_testplan(
                            name, res.test_declarations,
                            records2, pinmap2))):
                    l2_det = False
                (out_dir / ("%s.testplan.json" % name)).write_text(
                    datapaths.json_str(testplan))

            # ---- SPICE .cir data path: gated, only-when-modelled-or- --------
            # requested (WyredSpiceContract §0/§6). build_cir is a PURE
            # function of (l2, alloc) — the deck + confession sidecar — and the
            # emit loop alone decides whether to WRITE it (fully-modelled ->
            # always; partially-modelled -> only when the intent set
            # emit_spice). No ga019 part carries a spice model, so today's
            # corpus emits ZERO .cir files and every existing golden stays
            # byte-identical; the deck is deterministic across two resolves like
            # every other path, and its on-disk EXISTENCE is the gating decision
            # the rebuild CLI keys on.
            if datapaths.spice_should_emit(rr1.graph,
                                           _emit_spice_requested(name)):
                deck, sidecar = datapaths.build_cir(name, rr1.graph, rr1.alloc)
                deck2, sidecar2 = datapaths.build_cir(
                    name, rr2.graph, rr2.alloc)
                if (deck != deck2 or datapaths.json_str(sidecar)
                        != datapaths.json_str(sidecar2)):
                    l2_det = False
                (out_dir / ("%s.cir" % name)).write_text(deck)
                (out_dir / ("%s.cir.json" % name)).write_text(
                    datapaths.json_str(sidecar))

            resolved[name] = rr1
            pinmaps[name] = pinmap

            # the retained EXTERNAL baseline (locked emits only)
            baseline = None
            locked = any(g.get("version", 0) >= 1
                         for g in rr1.alloc["allocation"]["lock_groups"])
            if locked:
                baseline = snapshot_locks(rr1.l1)
                # M4: the external baseline also retains the connector-pinout
                # rows (the decision class the harness's allocation record
                # does not materialize) — extra keys are ignored by the
                # harness gate, consumed by paths.check_connector_locks.
                baseline["connector_pinout"] = [
                    dict(r) for r in rr1.alloc.get("connector_pinout", [])]
                (out_dir / ("%s.baseline.json" % name)).write_text(
                    json.dumps(baseline, indent=2, sort_keys=True) + "\n")
                baselines[name] = baseline

            if not l2_det:
                failures += 1
                print("FAIL %-28s L2 NOT deterministic: two resolves differ"
                      % name)

            # ---- lifecycle path (a): incumbent-seeded ECO re-solve --------
            # the pin-map diff against the incumbent artifact IS the change
            # review; minimal disturbance is checked mechanically.
            if incumbents is not None and incumbents in pinmaps:
                pdiff = datapaths.diff_pinmaps(pinmaps[incumbents], pinmap)
                sticky_viols = _check_incumbency(
                    resolved[incumbents].alloc["allocation"]["entries"],
                    rr1.alloc["allocation"]["entries"])
                pdiff["incumbents"] = incumbents
                pdiff["minimal_disturbance_violations"] = sticky_viols
                (out_dir / ("%s.pinmapdiff.json" % name)).write_text(
                    datapaths.json_str(pdiff))
                if sticky_viols:
                    failures += 1
                    print("FAIL %-28s incumbent re-solve disturbed sticky "
                          "allocations:" % name)
                    for s in sticky_viols:
                        print("       eco: %s" % s)
                else:
                    print("       eco vs %s: %d allocation row(s) changed, "
                          "sticky survived (%s.pinmapdiff.json)"
                          % (incumbents, len(pdiff["allocation"]["changed"]),
                             name))
            elif incumbents is not None:
                failures += 1
                print("FAIL %-28s incumbents artifact %r has no emitted "
                      "pin-map" % (name, incumbents))

            # ---- lifecycle path (b): break_lock -> series fork ------------
            # verify the fork against the PARENT'S external baseline, and
            # counter-probe the two tamper shapes the gate must catch.
            if fork is not None:
                lifecycle = {"artifact": name, "forked_from": fork["of"],
                             "new_series": fork["series"]}
                parent_baseline = baselines.get(fork["of"])
                if parent_baseline is None:
                    failures += 1
                    print("FAIL %-28s fork parent %r emitted no external "
                          "lock baseline" % (name, fork["of"]))
                else:
                    legal = sorted(check_lock_violations(
                        parent_baseline, rr1.l1))
                    # tamper 1: same edit, forked_from record stripped
                    t1 = copy.deepcopy(rr1.l1)
                    t1.pop("forked_from", None)
                    t1_codes = sorted(check_lock_violations(
                        parent_baseline, t1))
                    # tamper 2: edit a LOCKED decision in the parent doc
                    # in place (series untouched, no fork)
                    t2 = copy.deepcopy(resolved[fork["of"]].l1)
                    t2_codes = ["(no locked entry to tamper)"]
                    for e in t2.get("allocation", {}).get("entries", []):
                        if e.get("locked_by"):
                            e["unit"] = int(e["unit"]) + 1
                            t2_codes = sorted(check_lock_violations(
                                parent_baseline, t2))
                            break
                    lifecycle.update({
                        "parent_stamp":
                            resolved[fork["of"]].alloc["stamp"],
                        "child_stamp": rr1.alloc["stamp"],
                        "legal_fork_codes": legal,
                        "tamper_series_hand_edit_codes": t1_codes,
                        "tamper_locked_edit_codes": t2_codes,
                    })
                    lifecycle["ok"] = (
                        legal == []
                        and "SERIES_UNJUSTIFIED" in t1_codes
                        and "LOCK_VIOLATION" in t2_codes)
                    (out_dir / ("%s.lifecycle.json" % name)).write_text(
                        datapaths.json_str(lifecycle))
                    if not lifecycle["ok"]:
                        failures += 1
                        print("FAIL %-28s lifecycle: legal=%s tamper1=%s "
                              "tamper2=%s" % (name, legal, t1_codes,
                                              t2_codes))
                    else:
                        print("       lifecycle: legal fork clean; "
                              "tamper-without-fork -> %s; hand-edited "
                              "series -> %s (%s.lifecycle.json)"
                              % (t2_codes, t1_codes, name))

            # ---- connector-pinout lock gate (M4: the external-interface ----
            # freeze covers REAL rows). For every emit that fired a group
            # covering connector_pinout: the rows must exist and be locked,
            # the retained external baseline must verify clean, and both
            # tamper shapes must be caught by the gate. A fork additionally
            # verifies its rows against the PARENT's external baseline.
            conn_groups = sorted(
                g["name"] for g in rr1.alloc["allocation"]["lock_groups"]
                if g["name"] in freeze
                and "connector_pinout" in g.get("covers", []))
            if conn_groups:
                rows = rr1.alloc.get("connector_pinout", [])
                series = rr1.alloc.get("series", "")
                ff = rr1.l1.get("forked_from")
                conn = {"artifact": name, "groups": conn_groups,
                        "rows": len(rows)}
                own_base = baselines.get(name)
                unlocked = sorted({"%s.%s" % (r["connector"], r["pin"])
                                   for r in rows if not r.get("locked_by")})
                clean = datapaths.check_connector_locks(
                    own_base, rows, series, ff) if own_base else None
                # tamper 1: one locked row's net rewritten, same series
                t_rows = copy.deepcopy(rows)
                for r in t_rows:
                    if r.get("net") is not None:
                        r["net"] = "TAMPERED_CONN_NET"
                        break
                t1c = sorted({f["code"] for f in
                              datapaths.check_connector_locks(
                                  own_base, t_rows, series, ff)}) \
                    if own_base else []
                # tamper 2: hand-edited series, no fork record
                t2c = sorted({f["code"] for f in
                              datapaths.check_connector_locks(
                                  own_base, t_rows, series + "Z", None)}) \
                    if own_base else []
                conn.update({
                    "unlocked_rows": unlocked,
                    "gate_clean": [f["code"] for f in (clean or [])],
                    "tamper_net_codes": t1c,
                    "tamper_series_codes": t2c,
                })
                parent_ok = True
                if fork is not None:
                    pb = baselines.get(fork["of"])
                    pcodes = sorted({f["code"] for f in
                                     datapaths.check_connector_locks(
                                         pb, rows, series, ff)}) \
                        if pb else ["(no parent baseline)"]
                    conn["fork_vs_parent_codes"] = pcodes
                    parent_ok = pcodes == []
                conn["ok"] = (bool(rows) and not unlocked
                              and own_base is not None and clean == []
                              and "CONNECTOR_LOCK_VIOLATION" in t1c
                              and "CONNECTOR_SERIES_UNJUSTIFIED" in t2c
                              and parent_ok)
                (out_dir / ("%s.connlock.json" % name)).write_text(
                    datapaths.json_str(conn))
                if not conn["ok"]:
                    failures += 1
                    print("FAIL %-28s connector-pinout lock gate: %s"
                          % (name, conn))
                else:
                    print("       connector locks: %d row(s) frozen by %s; "
                          "gate clean; tamper -> %s; hand-edited series -> "
                          "%s (%s.connlock.json)"
                          % (len(rows), ",".join(conn_groups), t1c, t2c,
                             name))
        else:
            # no netlist for a failing layer 1 — write the L1 artifact only
            (out_dir / ("%s.l1.json" % name)).write_text(res.to_json_str())
            # placement is an L1-only assertion (no L2 lowering in v0), so a
            # layer 1 that fails the oracle still carries its declared
            # placement intent to the checker — emit it from the same L1.
            if res.doc.get("placement"):
                (out_dir / ("%s.placement.json" % name)).write_text(
                    datapaths.json_str(
                        datapaths.build_placement(name, res.doc)))

        l2_note = "" if wants_l2 else "  L2=(none: fails at L1 by design)"
        print("EMIT %-28s declared codes=%s%s"
              % (name, expected_codes or "[]", l2_note))
        if not deterministic:
            failures += 1
            print("       L1 NOT deterministic: two emits differ")
        for d in res.diagnostics:
            print("       engine: %s: %s" % (d.code, d.msg))

    return failures


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus-dir", required=True,
                    help="directory of corpus modules (importing IS "
                         "registering)")
    ap.add_argument("--list", action="store_true",
                    help="show discovered intents/refinements/modules")
    ap.add_argument("--out", default="out",
                    help="output directory (default: ./out)")
    ap.add_argument("--exemplify", metavar="MODULE",
                    help="render one registered module standalone (law 2)")
    args = ap.parse_args(argv)

    discover(Path(args.corpus_dir))

    if args.list:
        print("intents (%d):" % len(INTENTS))
        for name, cls in sorted(INTENTS.items()):
            print("  %-32s %s" % (name, cls.__name__))
        print("refinements (%d):" % len(REFINEMENTS))
        for name, ref in sorted(REFINEMENTS.items()):
            print("  %-32s %s of=%s ops=%d freeze=%s"
                  % (name, ref.__name__, ref.of, len(ref.ops),
                     list(ref.freeze) or "-"))
        print("registered modules (%d):" % len(MODULES))
        for name in sorted(MODULES):
            print("  %s" % name)
        return 0

    if args.exemplify:
        cls = MODULES.get(args.exemplify)
        if cls is None:
            print("no registered module named %r; --list shows them"
                  % args.exemplify, file=sys.stderr)
            return 2
        res = exemplify(cls)
        print(res.to_json_str(), end="")
        for d in res.diagnostics:
            print("engine: %s: %s" % (d.code, d.msg), file=sys.stderr)
        return 0

    out_dir = Path(args.out)
    emitted = _emit_all()
    print("elaborated %d document(s); artifacts land in %s"
          % (len(emitted), out_dir))

    failures = emit_artifacts(emitted, out_dir)
    print("\nRESULT: %s (%d artifact(s), %d failure(s))"
          % ("PASS" if failures == 0 else "FAIL", len(emitted), failures))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
