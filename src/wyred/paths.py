"""Secondary data paths over the model IR (Gen4 section 1.5, milestone M3).

Each consumable artifact is a pluggable resolver consuming the SAME emitted
documents (the layer-2 graph dict + the stamped allocation artifact + the
layer-1 doc) — never the modeller's internals:

  * ``build_bom``     — the grouped bill of materials: (kind, value,
                        authored) -> qty + refdes list, authored-vs-generated
                        marked, derived-value confessions carried.
  * ``build_pinmap``  — the firmware-facing artifact: per component,
                        terminal -> net + role, plus the allocation view with
                        each entry's landed provider node/net. STAMPED with
                        (series, lock-group versions) per Gen4 section 2.5:
                        versioned, denormalized, diffable.
  * ``build_records`` — allocation + lock + escalation records standalone.
  * ``crosscheck``    — the ARCHITECTURAL differential oracle (Gen4 section
                        1.5: the data paths are the denotations, so
                        netlist <-> BOM <-> pin-map consistency is checked
                        structurally): any mismatch is a structured failure.
  * ``diff_pinmaps``  — the ECO view: what changed between two pin-map
                        emits (allocation rows, terminal nets, stamps).

Pure Python 3 stdlib; no harness imports; every emitter is deterministic
(sorted keys, canonical entry order) so two resolves of one document yield
byte-identical artifacts on every path.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple


def json_str(obj: Dict[str, Any]) -> str:
    """The canonical byte form shared by every data path."""
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def _stamp(alloc: Dict[str, Any]) -> Dict[str, Any]:
    return {"series": alloc.get("series", ""),
            "locks": dict(alloc.get("stamp", {}).get("locks", {}))}


def _node_net(graph: Dict[str, Any]) -> Dict[Tuple[str, str], str]:
    out: Dict[Tuple[str, str], str] = {}
    for net in graph.get("nets", []):
        for ref, pin in net.get("nodes", []):
            out[(ref, pin)] = net["name"]
    return out


# ---------------------------------------------------------------------------
# BOM
# ---------------------------------------------------------------------------

def build_bom(name: str, graph: Dict[str, Any],
              alloc: Dict[str, Any]) -> Dict[str, Any]:
    """Grouped BOM from the layer-2 graph: one line item per
    (kind, value, authored) group; refdes listed; derivation confessions
    (``attrs.derived``) carried per refdes so a derived value never loses
    its arithmetic."""
    groups: Dict[Tuple[str, str, bool], List[Dict[str, Any]]] = {}
    for c in graph.get("components", []):
        key = (c.get("kind", ""), c.get("value", ""), bool(c.get("authored")))
        groups.setdefault(key, []).append(c)

    line_items = []
    for (kind, value, authored) in sorted(
            groups, key=lambda k: (k[0], k[1], not k[2])):
        comps = sorted(groups[(kind, value, authored)],
                       key=lambda c: c["refdes"])
        item: Dict[str, Any] = {
            "kind": kind,
            "value": value,
            "authored": authored,
            "qty": len(comps),
            "refdes": [c["refdes"] for c in comps],
        }
        derived = {c["refdes"]: c["attrs"]["derived"] for c in comps
                   if isinstance(c.get("attrs"), dict)
                   and c["attrs"].get("derived")}
        if derived:
            item["derived"] = derived
        line_items.append(item)

    n_auth = sum(1 for c in graph.get("components", []) if c.get("authored"))
    total = len(graph.get("components", []))
    return {
        "artifact": name,
        "path": "bom",
        "stamp": _stamp(alloc),
        "line_items": line_items,
        "component_total": total,
        "authored_total": n_auth,
        "generated_total": total - n_auth,
    }


# ---------------------------------------------------------------------------
# Pin-map
# ---------------------------------------------------------------------------

def build_pinmap(name: str, graph: Dict[str, Any], alloc: Dict[str, Any],
                 alloc_wiring: Optional[List[Dict[str, Any]]] = None
                 ) -> Dict[str, Any]:
    """The firmware-facing pin-map: per component, terminal -> net (null ==
    unwired) + structural role + the free-form pin label; plus the
    allocation view enriched with each entry's landed provider node/net
    (``alloc_wiring``; defaults to the PERSISTED ``alloc["wiring"]`` view —
    M4 carry-item close: the pin-map rebuilds byte-identically from the
    on-disk l2 + alloc artifacts, like bom/records). Stamped with
    (series, lock-group versions): a firmware<->board-spin compatibility
    question is a stamp comparison on this artifact."""
    if alloc_wiring is None:
        alloc_wiring = alloc.get("wiring")
    node_net = _node_net(graph)
    comps = []
    for c in sorted(graph.get("components", []), key=lambda c: c["refdes"]):
        terms = []
        for t in c.get("terminals", []):
            terms.append({
                "name": t["name"],
                "role": t.get("role", ""),
                "function": t.get("function", ""),
                "net": node_net.get((c["refdes"], t["name"])),
            })
        attrs = c.get("attrs") or {}
        entry: Dict[str, Any] = {
            "refdes": c["refdes"],
            "kind": c.get("kind", ""),
            "value": c.get("value", ""),
            "authored": bool(c.get("authored")),
            "terminals": terms,
        }
        if attrs.get("l1_role"):
            entry["l1_role"] = attrs["l1_role"]
        if attrs.get("for_demand"):
            entry["for_demand"] = attrs["for_demand"]
        comps.append(entry)

    wiring = {(w["pool"], w["unit"], w["demand"]): w
              for w in (alloc_wiring or [])}
    allocations = []
    for e in sorted(alloc.get("allocation", {}).get("entries", []),
                    key=lambda e: (e["pool"], e["unit"], e["demand"])):
        row = dict(e)
        w = wiring.get((e["pool"], e["unit"], e["demand"]))
        row["node"] = w["node"] if w else None
        row["net"] = w["net"] if w else None
        allocations.append(row)

    return {
        "artifact": name,
        "path": "pinmap",
        "stamp": _stamp(alloc),
        "solver_version": alloc.get("solver_version", ""),
        "components": comps,
        "allocations": allocations,
    }


# ---------------------------------------------------------------------------
# Records (allocation + locks + escalations, standalone)
# ---------------------------------------------------------------------------

def build_records(name: str, l1: Dict[str, Any],
                  alloc: Dict[str, Any]) -> Dict[str, Any]:
    """Allocation + lock + escalation records as ONE standalone artifact:
    everything a process/audit consumer needs without parsing the netlist
    or the intent document."""
    attrs = l1.get("attrs", {}) or {}
    out: Dict[str, Any] = {
        "artifact": name,
        "path": "records",
        "stamp": _stamp(alloc),
        "series": alloc.get("series", ""),
        "solver_version": alloc.get("solver_version", ""),
        "allocation": alloc.get("allocation", {}),
        "bindings": alloc.get("bindings", {}),
        "bound_parts": alloc.get("bound_parts", {}),
        "escalations": list(l1.get("escalations", []) or []),
        "resolutions": dict(attrs.get("resolutions", {}) or {}),
        "pool_spares": dict(attrs.get("pool_spares", {}) or {}),
        "connector_pinout": [dict(r) for r in
                             alloc.get("connector_pinout", []) or []],
    }
    if l1.get("forked_from") is not None:
        out["forked_from"] = dict(l1["forked_from"])
    return out


# ---------------------------------------------------------------------------
# The cross-path differential (the architectural oracle)
# ---------------------------------------------------------------------------

def crosscheck(graph: Dict[str, Any], bom: Dict[str, Any],
               pinmap: Dict[str, Any], records: Dict[str, Any],
               l1: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """Assert that netlist, BOM, pin-map and records describe ONE model.
    Every mismatch is a structured failure dict {"code", "msg"}; an empty
    list means the paths are consistent denotations of the same design.

    M4 (review P3 closure): allocation rows are compared on their FULL
    payload (state/chosen_by/locked_by — the lock-status fields a
    firmware-facing consumer trusts), connector-pinout rows on their full
    payload (function vs the netlist terminal, ``locked_by`` vs the
    artifact's own lock-group record — ``XPATH_CONNECTOR_LOCK``), and the
    stamp must agree with the lock-group versions it summarizes. When the
    emitted layer-1 document is supplied (``l1``), the records path's
    l1-sourced provenance fields (``forked_from``, ``escalations``,
    ``resolutions``, ``pool_spares``) are verified against it
    (``XPATH_RECORDS_L1``) — a forged lineage record crosschecks dirty
    from disk."""
    fails: List[Dict[str, str]] = []

    def fail(code: str, msg: str) -> None:
        fails.append({"code": code, "msg": msg})

    graph_refs = {c["refdes"] for c in graph.get("components", [])}

    # --- BOM <-> netlist ---------------------------------------------------
    bom_refs: List[str] = []
    for item in bom.get("line_items", []):
        if item.get("qty") != len(item.get("refdes", [])):
            fail("XPATH_BOM_COUNT",
                 "BOM line (%s, %s) declares qty %s but lists %d refdes"
                 % (item.get("kind"), item.get("value"), item.get("qty"),
                    len(item.get("refdes", []))))
        bom_refs.extend(item.get("refdes", []))
    if len(bom_refs) != len(set(bom_refs)):
        dupes = sorted({r for r in bom_refs if bom_refs.count(r) > 1})
        fail("XPATH_BOM_COUNT",
             "BOM lists refdes more than once: %s" % dupes)
    if set(bom_refs) != graph_refs:
        fail("XPATH_BOM_COMPONENTS",
             "BOM refdes set != netlist components (bom-only=%s, "
             "netlist-only=%s)" % (sorted(set(bom_refs) - graph_refs),
                                   sorted(graph_refs - set(bom_refs))))
    if bom.get("component_total") != len(graph_refs):
        fail("XPATH_BOM_COUNT",
             "BOM component_total %s != netlist component count %d"
             % (bom.get("component_total"), len(graph_refs)))
    authored_by_ref = {c["refdes"]: bool(c.get("authored"))
                       for c in graph.get("components", [])}
    for item in bom.get("line_items", []):
        for ref in item.get("refdes", []):
            if ref in authored_by_ref and \
                    bool(item.get("authored")) != authored_by_ref[ref]:
                fail("XPATH_BOM_AUTHORED",
                     "BOM marks %s authored=%s but the netlist says %s"
                     % (ref, item.get("authored"), authored_by_ref[ref]))

    # BOM line FIELDS: the line item a refdes sits under must carry that
    # component's netlist kind/value (the BOM's central payload — what part
    # to buy — is cross-checked per refdes, not trusted), and the line's
    # derivation confession per refdes must equal the netlist's
    # ``attrs.derived`` (a derived value never loses or rewrites its
    # arithmetic between paths).
    kv_by_ref = {c["refdes"]: (c.get("kind", ""), c.get("value", ""))
                 for c in graph.get("components", [])}
    derived_by_ref = {c["refdes"]: c["attrs"].get("derived")
                      for c in graph.get("components", [])
                      if isinstance(c.get("attrs"), dict)}
    for item in bom.get("line_items", []):
        for ref in item.get("refdes", []):
            if ref not in kv_by_ref:
                continue        # XPATH_BOM_COMPONENTS already flagged it
            kind, value = kv_by_ref[ref]
            if item.get("kind", "") != kind or \
                    item.get("value", "") != value:
                fail("XPATH_BOM_FIELDS",
                     "BOM line (%r, %r) lists %s but the netlist says that "
                     "component is (%r, %r)"
                     % (item.get("kind"), item.get("value"), ref,
                        kind, value))
            want = derived_by_ref.get(ref) or None
            got = (item.get("derived") or {}).get(ref)
            if want != got:
                fail("XPATH_BOM_FIELDS",
                     "BOM derivation confession for %s is %r but the "
                     "netlist carries %r" % (ref, got, want))

    # --- pin-map <-> netlist -----------------------------------------------
    pm_refs = {c["refdes"] for c in pinmap.get("components", [])}
    if pm_refs != graph_refs:
        fail("XPATH_PINMAP_COMPONENTS",
             "pin-map refdes set != netlist components (pinmap-only=%s, "
             "netlist-only=%s)" % (sorted(pm_refs - graph_refs),
                                   sorted(graph_refs - pm_refs)))
    node_net = _node_net(graph)
    pm_nodes: Dict[Tuple[str, str], Optional[str]] = {}
    for c in pinmap.get("components", []):
        for t in c.get("terminals", []):
            pm_nodes[(c["refdes"], t["name"])] = t.get("net")
    for node, net in sorted(node_net.items()):
        if node not in pm_nodes:
            fail("XPATH_PINMAP_NET",
                 "netlist node %s.%s (net %r) is missing from the pin-map"
                 % (node[0], node[1], net))
        elif pm_nodes[node] != net:
            fail("XPATH_PINMAP_NET",
                 "pin-map says %s.%s -> %r but the netlist says %r"
                 % (node[0], node[1], pm_nodes[node], net))
    for node, net in sorted(pm_nodes.items()):
        if net is not None and node not in node_net:
            fail("XPATH_PINMAP_NET",
                 "pin-map wires %s.%s to %r but the netlist has that node "
                 "on no net" % (node[0], node[1], net))

    # pin-map component FIELDS + TERMINAL SETS: the firmware-facing view of
    # every component must agree with the netlist on kind/value/authored,
    # carry EXACTLY the netlist's terminal set (a silently dropped unwired
    # terminal hides spare capacity; a phantom terminal invents it), and
    # agree per terminal on role and function.
    graph_by_ref = {c["refdes"]: c for c in graph.get("components", [])}
    for c in pinmap.get("components", []):
        gc = graph_by_ref.get(c["refdes"])
        if gc is None:
            continue        # XPATH_PINMAP_COMPONENTS already flagged it
        for field in ("kind", "value"):
            if c.get(field, "") != gc.get(field, ""):
                fail("XPATH_PINMAP_TERMS",
                     "pin-map says %s %s=%r but the netlist says %r"
                     % (c["refdes"], field, c.get(field), gc.get(field)))
        if bool(c.get("authored")) != bool(gc.get("authored")):
            fail("XPATH_PINMAP_TERMS",
                 "pin-map marks %s authored=%s but the netlist says %s"
                 % (c["refdes"], c.get("authored"), gc.get("authored")))
        g_terms = {t["name"]: t for t in gc.get("terminals", [])}
        p_terms = {t["name"]: t for t in c.get("terminals", [])}
        if len(p_terms) != len(c.get("terminals", [])):
            dupes = sorted({t["name"] for t in c.get("terminals", [])
                            if [x["name"] for x in
                                c.get("terminals", [])].count(t["name"]) > 1})
            fail("XPATH_PINMAP_TERMS",
                 "pin-map lists duplicate terminal name(s) on %s: %s"
                 % (c["refdes"], dupes))
        missing = sorted(set(g_terms) - set(p_terms))
        phantom = sorted(set(p_terms) - set(g_terms))
        if missing or phantom:
            fail("XPATH_PINMAP_TERMS",
                 "pin-map terminal set for %s != netlist (missing=%s, "
                 "phantom=%s)" % (c["refdes"], missing, phantom))
        for tname in sorted(set(g_terms) & set(p_terms)):
            for field in ("role", "function"):
                if p_terms[tname].get(field, "") != \
                        g_terms[tname].get(field, ""):
                    fail("XPATH_PINMAP_TERMS",
                         "pin-map says %s.%s %s=%r but the netlist says %r"
                         % (c["refdes"], tname, field,
                            p_terms[tname].get(field),
                            g_terms[tname].get(field)))

    # --- allocation view <-> records <-> netlist ----------------------------
    def rows(entries: Any) -> List[Tuple[Any, Any, Any]]:
        return sorted((e.get("pool"), e.get("unit"), e.get("demand"))
                      for e in (entries or []))

    pm_rows = rows(pinmap.get("allocations"))
    rec_rows = rows(records.get("allocation", {}).get("entries"))
    if pm_rows != rec_rows:
        fail("XPATH_ALLOC_MISMATCH",
             "pin-map allocation view != records allocation entries "
             "(pinmap=%s, records=%s)" % (pm_rows, rec_rows))
    # full row PAYLOAD, not just the key set (P3 closure): the pin-map's
    # allocation view is the records entry PLUS the landed node/net
    # enrichment — every other field (state, chosen_by, locked_by, …) must
    # be byte-equal between the two paths, or a consumer of one artifact
    # is being told a different lock story than a consumer of the other.
    rec_by_key = {(e.get("pool"), e.get("unit"), e.get("demand")): e
                  for e in records.get("allocation", {}).get("entries", [])}
    for row in pinmap.get("allocations", []):
        key = (row.get("pool"), row.get("unit"), row.get("demand"))
        rec = rec_by_key.get(key)
        if rec is None:
            continue        # the key-set check above already flagged it
        payload = {k: v for k, v in row.items() if k not in ("node", "net")}
        if payload != dict(rec):
            diff = sorted(k for k in set(payload) | set(rec)
                          if payload.get(k) != rec.get(k))
            fail("XPATH_ALLOC_MISMATCH",
                 "allocation row (%s, %s, %s) payload differs between "
                 "pin-map and records on %s (pinmap=%r, records=%r)"
                 % (key[0], key[1], key[2], diff,
                    {k: payload.get(k) for k in diff},
                    {k: rec.get(k) for k in diff}))
    for row in pinmap.get("allocations", []):
        node, net = row.get("node"), row.get("net")
        if node is None:
            continue
        ref, _, pin = node.partition(".")
        actual = node_net.get((ref, pin))
        if actual != net:
            fail("XPATH_ALLOC_NET",
                 "allocation (%s, %s, %s) claims node %s on net %r but the "
                 "netlist puts it on %r"
                 % (row.get("pool"), row.get("unit"), row.get("demand"),
                    node, net, actual))

    # --- connector-pinout rows <-> netlist (M4: the ICD rows are REAL and
    # cross-checked, not a stamp handshake): every row's claimed net must be
    # the netlist's, and every connector-component node the netlist wires
    # must appear as a row (a hidden pinout decision is a failure).
    conn_terms: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for c in graph.get("components", []):
        if c.get("kind") != "connector":
            continue
        for t in c.get("terminals", []):
            conn_terms[(c["refdes"], t["name"])] = t
    # the artifact's OWN lock record decides what the rows' lock status
    # must be (P3 closure — the lock-status payload is cross-checked, not
    # trusted): the legal ``locked_by`` tags are exactly the
    # connector_pinout-covering lock groups the record says are fired.
    conn_tags = sorted(
        "%s@%d" % (g.get("name"), int(g.get("version", 0)))
        for g in records.get("allocation", {}).get("lock_groups", []) or []
        if "connector_pinout" in (g.get("covers") or [])
        and int(g.get("version", 0)) >= 1)
    row_nets: Dict[Tuple[str, str], Optional[str]] = {}
    for row in records.get("connector_pinout", []) or []:
        key = (row.get("connector"), row.get("pin"))
        row_nets[key] = row.get("net")
        if key not in conn_terms:
            fail("XPATH_CONNECTOR_NET",
                 "connector-pinout row %s.%s names no connector node in "
                 "the netlist — a phantom ICD row" % (key[0], key[1]))
        else:
            actual = node_net.get(key)
            if actual != row.get("net"):
                fail("XPATH_CONNECTOR_NET",
                     "connector-pinout row %s.%s claims net %r but the "
                     "netlist says %r"
                     % (key[0], key[1], row.get("net"), actual))
            want_fn = conn_terms[key].get("function", "")
            if row.get("function", "") != want_fn:
                fail("XPATH_CONNECTOR_NET",
                     "connector-pinout row %s.%s claims function %r but "
                     "the netlist terminal says %r"
                     % (key[0], key[1], row.get("function"), want_fn))
        lb = row.get("locked_by")
        if conn_tags and lb not in conn_tags:
            fail("XPATH_CONNECTOR_LOCK",
                 "connector-pinout row %s.%s carries locked_by=%r but the "
                 "record's fired connector_pinout lock group(s) are %s — a "
                 "frozen ICD row's lock status was stripped or forged"
                 % (key[0], key[1], lb, conn_tags))
        elif not conn_tags and lb:
            fail("XPATH_CONNECTOR_LOCK",
                 "connector-pinout row %s.%s claims locked_by=%r but the "
                 "record has NO fired lock group covering connector_pinout "
                 "— a forged lock status" % (key[0], key[1], lb))
    for key in sorted(conn_terms):
        if key not in row_nets:
            fail("XPATH_CONNECTOR_NET",
                 "connector node %s.%s has no connector-pinout row — a "
                 "pinout decision was hidden from the record"
                 % (key[0], key[1]))

    # --- stamps agree across every path -------------------------------------
    stamps = {p: art.get("stamp") for p, art in
              (("bom", bom), ("pinmap", pinmap), ("records", records))}
    if len({json.dumps(s, sort_keys=True) for s in stamps.values()}) != 1:
        fail("XPATH_STAMP_MISMATCH",
             "artifact stamps disagree across paths: %s" % stamps)

    # … and the stamp must be an honest SUMMARY, not free text (P3
    # closure): its lock versions are the records' lock-group versions,
    # its series is the records' series — forging one without the other
    # is a payload mismatch even when all three paths carry the forgery.
    rec_stamp = records.get("stamp") or {}
    want_locks = {g.get("name"): int(g.get("version", 0))
                  for g in records.get("allocation", {}).get(
                      "lock_groups", []) or []}
    got_locks = {k: int(v) for k, v in
                 (rec_stamp.get("locks") or {}).items()}
    if got_locks != want_locks:
        fail("XPATH_STAMP_MISMATCH",
             "stamp lock versions %r != the records' lock-group versions "
             "%r" % (got_locks, want_locks))
    if records.get("series", "") != rec_stamp.get("series", ""):
        fail("XPATH_STAMP_MISMATCH",
             "records series %r != stamp series %r"
             % (records.get("series"), rec_stamp.get("series")))

    # --- records <-> the emitted layer-1 document (optional fifth input) ----
    # the records path's l1-sourced provenance payload must BE the l1's:
    # a forged ``forked_from`` (fake lineage legalizing a locked edit to a
    # downstream consumer) or doctored escalations/resolutions/spares in
    # the records artifact is a structured failure, not a trusted field.
    if l1 is not None:
        attrs = l1.get("attrs", {}) or {}
        want_ff = dict(l1["forked_from"]) \
            if l1.get("forked_from") is not None else None
        for field, want in (
                ("forked_from", want_ff),
                ("escalations", list(l1.get("escalations", []) or [])),
                ("resolutions", dict(attrs.get("resolutions", {}) or {})),
                ("pool_spares", dict(attrs.get("pool_spares", {}) or {}))):
            got = records.get(field)
            if got != want:
                fail("XPATH_RECORDS_L1",
                     "records %s is %r but the emitted layer-1 document "
                     "carries %r" % (field, got, want))
    return fails


# ---------------------------------------------------------------------------
# Connector-pinout lock gate (Gen4 section 2.5 applied to the
# ``connector_pinout`` decision class — M3 carry-item 2)
# ---------------------------------------------------------------------------

def check_connector_locks(baseline: Dict[str, Any],
                          rows: List[Dict[str, Any]],
                          series: str,
                          forked_from: Optional[Dict[str, Any]] = None
                          ) -> List[Dict[str, str]]:
    """The external-baseline gate for connector-pinout rows, mirroring the
    harness's ``check_lock_violations`` semantics for the decision class the
    (read-only) allocation oracle does not materialize: the baseline is the
    RETAINED external snapshot (``{"series": ..., "connector_pinout":
    [rows]}``); a LOCKED row (``locked_by`` set) whose net changed / that
    disappeared without a series bump is a violation; a differing series is
    legal only through a ``forked_from`` record naming the baseline series.

    Returns structured failure dicts {"code", "msg"}; empty == clean."""
    fails: List[Dict[str, str]] = []
    base_series = baseline.get("series")
    if series != base_series:
        if (isinstance(forked_from, dict)
                and forked_from.get("series") == base_series):
            return fails            # legal fork: locked edits permitted
        fails.append({
            "code": "CONNECTOR_SERIES_UNJUSTIFIED",
            "msg": "series %r differs from the baseline series %r without a "
                   "forked_from record naming it (forked_from=%r) — a "
                   "connector-pinout edit is never legalized by hand-editing "
                   "the series string" % (series, base_series, forked_from)})
        return fails
    cur = {(r.get("connector"), r.get("pin")): r for r in rows}
    for b in baseline.get("connector_pinout", []) or []:
        if not b.get("locked_by"):
            continue                # never locked; free to drift
        key = (b.get("connector"), b.get("pin"))
        c = cur.get(key)
        if c is None:
            fails.append({
                "code": "CONNECTOR_LOCK_VIOLATION",
                "msg": "locked connector-pinout row %s.%s (locked_by %s) "
                       "disappeared without a series bump (series %r)"
                       % (key[0], key[1], b.get("locked_by"), series)})
            continue
        if c.get("net") != b.get("net"):
            fails.append({
                "code": "CONNECTOR_LOCK_VIOLATION",
                "msg": "locked connector-pinout row %s.%s changed net %r -> "
                       "%r without a series bump (series %r, locked_by %s)"
                       % (key[0], key[1], b.get("net"), c.get("net"),
                          series, b.get("locked_by"))})
    return fails


# ---------------------------------------------------------------------------
# Pin-map diff (the ECO / lifecycle view)
# ---------------------------------------------------------------------------

def diff_pinmaps(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """What changed between two pin-map emits: allocation rows keyed by
    demand, per-terminal net changes, component set changes, and the two
    stamps. Empty ``allocation``/``terminals``/component lists == the maps
    agree (stamps may still differ — that is the point of stamping)."""
    def by_demand(pm: Dict[str, Any]) -> Dict[str, List[str]]:
        m: Dict[str, List[str]] = {}
        for e in pm.get("allocations", []):
            m.setdefault(str(e.get("demand")), []).append(json.dumps(
                {k: e.get(k) for k in ("pool", "unit", "state", "chosen_by",
                                       "locked_by", "node")},
                sort_keys=True))
        return {k: sorted(v) for k, v in m.items()}

    da, db = by_demand(a), by_demand(b)
    alloc_diff = {
        "added": sorted(k for k in db if k not in da),
        "removed": sorted(k for k in da if k not in db),
        "changed": sorted(k for k in db if k in da and da[k] != db[k]),
    }

    def nodes(pm: Dict[str, Any]) -> Dict[Tuple[str, str], Optional[str]]:
        out: Dict[Tuple[str, str], Optional[str]] = {}
        for c in pm.get("components", []):
            for t in c.get("terminals", []):
                out[(c["refdes"], t["name"])] = t.get("net")
        return out

    na, nb = nodes(a), nodes(b)
    term_changes = []
    for key in sorted(set(na) | set(nb)):
        va, vb = na.get(key), nb.get(key)
        if va != vb:
            term_changes.append({"refdes": key[0], "terminal": key[1],
                                 "a": va, "b": vb})

    refs_a = {c["refdes"] for c in a.get("components", [])}
    refs_b = {c["refdes"] for c in b.get("components", [])}
    return {
        "stamp_a": a.get("stamp"),
        "stamp_b": b.get("stamp"),
        "allocation": alloc_diff,
        "terminals": term_changes,
        "components_only_in_a": sorted(refs_a - refs_b),
        "components_only_in_b": sorted(refs_b - refs_a),
    }


__all__ = ["build_bom", "build_pinmap", "build_records", "crosscheck",
           "check_connector_locks", "diff_pinmaps", "json_str"]
