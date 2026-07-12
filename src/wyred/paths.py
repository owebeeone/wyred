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
import re
from typing import Any, Dict, List, Optional, Tuple

from .core import ModellerError


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
# Placement (WyredPlacementSemantics section 5): the declared-intent artifact
# ---------------------------------------------------------------------------

def build_placement(name: str, l1: Dict[str, Any]) -> Dict[str, Any]:
    """The ``.placement.json`` artifact: the L1 ``placement`` section, stamped
    like the pin-map and with its constraint rows sorted by minted id
    (semantics section 5). A PURE function of the on-disk layer-1 document
    alone — no netlist, no allocation artifact — so it rebuilds
    byte-identically from primaries like bom/records (there is no L2 lowering
    of placement in v0: every measurement is checker-side).

    The stamp derives from the L1 document itself: ``series`` and the
    lock-group versions the document carries (placement joins no lock class in
    v0, semantics section 7, so for today's placement corpus this is
    ``{}``). Rows pass through verbatim (id, kind, declared_by, subjects,
    resolved params) — all numbers are authored/late-resolved, deterministic
    from source, so there is nothing to re-round here."""
    lock_groups = l1.get("allocation", {}).get("lock_groups", []) or []
    stamp = {"series": l1.get("series", ""),
             "locks": {g["name"]: int(g.get("version", 0))
                       for g in lock_groups}}
    constraints = sorted((dict(r) for r in l1.get("placement", []) or []),
                         key=lambda r: r["id"])
    return {
        "artifact": name,
        "path": "placement",
        "stamp": stamp,
        "constraints": constraints,
    }


# ---------------------------------------------------------------------------
# Testplan (WyredPlanTestplan step 1.3 / ProposalTestplanContract): the
# derived, self-contained acceptance artifact
# ---------------------------------------------------------------------------

def _round6(x: Any) -> float:
    """RATIFY-7 canonicalization for DERIVED numbers (computed bounds, derived
    nominals): a plain ``round(x, 6)`` JSON number. Authored numbers pass
    through verbatim (they never reach this)."""
    return round(float(x), 6)


def _test_points(pinmap: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every ``test_point`` component's terminals as flat probe candidates
    (proposal section 2: testpoint-ness is INFERRED from realized
    ``kind == "test_point"`` — L1 stays untouched). Each carries the pinmap
    facts a probe point needs: ``{refdes, pad, net, role, function}``."""
    out: List[Dict[str, Any]] = []
    for c in pinmap.get("components", []):
        if c.get("kind") != "test_point":
            continue
        for t in c.get("terminals", []):
            out.append({"refdes": c["refdes"], "pad": t.get("name", ""),
                        "net": t.get("net"), "role": t.get("role", ""),
                        "function": t.get("function", "")})
    return out


def _probe(tp: Dict[str, Any]) -> Dict[str, Any]:
    return {"refdes": tp["refdes"], "pad": tp["pad"], "net": tp["net"]}


def _sorted_probes(tps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_probe(t) for t in
            sorted(tps, key=lambda t: (t["refdes"], t["pad"]))]


def _unprobeable(rid: str, kind: str, subject: str, why: str) -> ModellerError:
    return ModellerError(
        "TESTPLAN_UNPROBEABLE",
        "%s check %r (subject %r) %s — no probeable test_point (proposal "
        "section 2: a check with no probe point is a structured emit failure, "
        "never a silently thinner testplan)" % (kind, rid, subject, why))


def _check_for(decl: Dict[str, Any],
               tps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Derive ONE check from its declaration + the pin-map's test points
    (bounds, probe binding, provenance). The declaration is self-contained
    (nominal / addresses already resolved at elaboration), so this needs no
    L1 document — proposal section 3."""
    rid, kind, subject = decl["id"], decl["kind"], decl["subject"]
    declared_by = decl["declared_by"]
    prov: Dict[str, Any] = {"declaration": rid, "subject": subject}
    check: Dict[str, Any] = {"id": rid, "kind": kind,
                             "declared_by": declared_by, "subject": subject}

    if kind == "rail":
        nominal = decl["nominal"]
        if "tol" in decl:
            t = float(decl["tol"])
            prov["tolerance"] = {"tol": decl["tol"]}
        else:
            t = float(nominal) * float(decl["tol_pct"]) / 100.0
            prov["tolerance"] = {"tol_pct": decl["tol_pct"]}
        check["expect"] = {"nominal": nominal,
                           "low": _round6(nominal - t),
                           "high": _round6(nominal + t)}
        taps = [tp for tp in tps if tp["net"] == subject]
        grounds = [tp for tp in tps if tp["role"] == "ground"]
        if not taps:
            raise _unprobeable(rid, kind, subject,
                               "no test_point on the rail's net")
        if not grounds:
            raise _unprobeable(rid, kind, subject,
                               "no ground-reference test_point (a rail check "
                               "requires both the rail tap AND a GND tap)")
        gref = _sorted_probes(grounds)[0]
        check["probe"] = {"points": _sorted_probes(taps), "ground_ref": gref}
        prov["nominal_source"] = decl["nominal_source"]

    elif kind == "i2c_scan":
        check["expect"] = {"addrs": list(decl["addrs"])}
        points = [tp for tp in tps
                  if tp["net"] and tp["net"].startswith(subject + "_")]
        if not points:
            raise _unprobeable(rid, kind, subject,
                               "no test_point on the bus's %s_* nets" % subject)
        check["probe"] = {"points": _sorted_probes(points),
                          "ground_ref": None}
        prov["addrs_source"] = decl["addrs_source"]

    elif kind == "current":
        # RATIFY-4 / section 2: current needs no test_point (probe method is a
        # bench-card matter) — never TESTPLAN_UNPROBEABLE.
        check["expect"] = {"max_ma": decl["max_ma"], "state": decl["state"]}
        check["probe"] = {"points": [], "ground_ref": None}

    elif kind == "signal":
        expect: Dict[str, Any] = {}
        if "freq" in decl:
            freq = float(decl["freq"])
            if "freq_tol_ppm" in decl:
                ft = freq * float(decl["freq_tol_ppm"]) / 1_000_000.0
                prov["freq_tolerance"] = {"freq_tol_ppm": decl["freq_tol_ppm"]}
            else:
                ft = freq * float(decl["freq_tol_pct"]) / 100.0
                prov["freq_tolerance"] = {"freq_tol_pct": decl["freq_tol_pct"]}
            expect["freq"] = decl["freq"]
            expect["freq_low"] = _round6(freq - ft)
            expect["freq_high"] = _round6(freq + ft)
        if "duty" in decl:
            duty = float(decl["duty"])
            dp = float(decl["duty_tol_pts"])
            prov["duty_tolerance"] = {"duty_tol_pts": decl["duty_tol_pts"]}
            expect["duty"] = decl["duty"]
            expect["duty_low"] = _round6(duty - dp)
            expect["duty_high"] = _round6(duty + dp)
        check["expect"] = expect
        points = [tp for tp in tps if tp["net"]
                  and (tp["net"] == subject
                       or tp["net"].startswith(subject + "."))]
        if not points:
            raise _unprobeable(rid, kind, subject,
                               "no test_point on the demand's realized nets")
        check["probe"] = {"points": _sorted_probes(points),
                          "ground_ref": None}
    else:                                       # pragma: no cover - defensive
        raise ModellerError("TESTPLAN_UNKNOWN_KIND",
                            "test declaration %r has unknown kind %r"
                            % (rid, kind))

    check["provenance"] = prov
    return check


def build_testplan(name: str, decls: List[Dict[str, Any]],
                   records: Dict[str, Any],
                   pinmap: Dict[str, Any]) -> Dict[str, Any]:
    """The ``.testplan.json`` artifact (proposal section 5): the elaborated
    ``declarations`` block (authored intent, pass-through) plus a derived
    ``checks`` block — bounds from tolerances (RATIFY-1/5/7), probe points
    inferred from the pin-map's ``test_point`` components (section 2),
    provenance per check. A PURE function of ``(declarations, records,
    pin-map)`` — no L1 document — so it rebuilds byte-identically from those
    primaries (the checks re-derive from the on-disk testplan's own
    declarations + records + pin-map). Stamped like every artifact and tied to
    the specific frozen pin-map the measurement runs against (section 1.1
    point 5): the stamp is the pin-map's, and ``records`` (the other stamped
    secondary of this emit) must agree, else ``TESTPLAN_STAMP_MISMATCH`` — a
    testplan is never derived across an inconsistent record/pin-map pair.

    ``decls``  the elaborated ``expect_*`` records (``EmitResult
               .test_declarations`` at emit; the on-disk testplan's own
               ``declarations`` block at rebuild — the same bytes either way).
    Checks are sorted by minted id (section 5)."""
    rec_stamp = records.get("stamp")
    pm_stamp = pinmap.get("stamp")
    if rec_stamp != pm_stamp:
        raise ModellerError(
            "TESTPLAN_STAMP_MISMATCH",
            "records stamp %r != pin-map stamp %r for %r — the testplan cannot "
            "be attributed to a single frozen pin-map"
            % (rec_stamp, pm_stamp, name))
    declarations = sorted((dict(d) for d in decls), key=lambda d: d["id"])
    tps = _test_points(pinmap)
    checks = sorted((_check_for(d, tps) for d in declarations),
                    key=lambda c: c["id"])
    return {
        "artifact": name,
        "path": "testplan",
        "stamp": dict(pm_stamp or {}),
        "declarations": declarations,
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# SPICE .cir data path (WyredPlanSpice step 1.2 / WyredSpiceContract §1-§7):
# the THIRD denotation. ``build_cir`` consumes the SAME emitted documents (the
# layer-2 graph + the stamped alloc), classifies every component against the
# ratified model semantics, and produces a deterministic ngspice-dialect deck
# plus its machine-readable confession sidecar. Ported conventions from ga005's
# emit_spice.py (kind->element-letter table, ground->0, per-component element
# cards) but with the §3 name-preserving nodes instead of ga005's integers.
#
# GATING is the emit loop's call, never build_cir's: build_cir is a PURE
# function of (graph, alloc) that always renders the modelled subset + confesses
# the rest, so (a) rebuild re-derives it from primaries and (b) the on-disk
# deck's EXISTENCE is the recorded gating decision (fully-modelled -> always;
# partially-modelled -> only when the intent requested emit_spice, §6). The
# emit_spice request never needs to ride the artifacts: at rebuild time the
# decision is already frozen in the file's presence.
# ---------------------------------------------------------------------------

# The deck-format revision recorded in every deck header (§9). Distinct from
# the engine's solver_version (read from the alloc primary) and the contract
# rev; bumping it is a deliberate golden-affecting event.
SPICE_DECK_VERSION = "0"

# §2 passives auto-map: a CLOSED kind -> element-letter table. Value is taken
# verbatim from the L2 component and canonicalized (§2a); nothing here invents
# a value, and everything NOT tabled needs an explicit spice_model or lands in
# the confession.
_SPICE_AUTOMAP: Dict[str, str] = {
    "resistor": "R", "capacitor": "C", "inductor": "L", "diode": "D"}

# §2a value canonicalization: SPICE magnitude prefixes (case-insensitive in,
# canonical case out; mega only ever via "meg"/"MEG" per §2a, so a bare "m"/"M"
# stays milli — SPICE's own reading) and the unit letters stripped (F/H/Ω/R).
_SPICE_PREFIX = {"f": "f", "p": "p", "n": "n", "u": "u", "µ": "u",
                 "m": "m", "k": "k", "meg": "MEG", "g": "g", "t": "t"}
_SPICE_VALUE_RE = re.compile(
    r"\s*([+-]?(?:\d+\.?\d*|\.\d+))\s*"          # magnitude
    r"(MEG|meg|[fpnuµmkgtFPNUMKGT])?\s*"    # optional SI prefix
    r"(?:[FfHh]|[Rr]|Ω|[Oo]hms?)?\s*")      # optional unit, stripped


def _canon_spice_value(raw: Any) -> Optional[str]:
    """An L2 value string -> a SPICE-legal magnitude token, or None when it
    does not parse (§2a: a value that does not parse is never a guessed number
    — the part joins the confession)."""
    if raw is None:
        return None
    m = _SPICE_VALUE_RE.fullmatch(str(raw))
    if m is None:
        return None
    num, prefix = m.group(1), m.group(2)
    if prefix:
        return num + _SPICE_PREFIX[prefix.lower()]
    return num


def _sanitize_node(name: str) -> str:
    """§3 name-preserving node token: ``+`` -> ``P``, ``-`` -> ``M``, any other
    non-word char -> ``_``, digit-leading names prefixed ``N`` (``+3V3`` ->
    ``P3V3``, ``3V3`` -> ``N3V3``). Ground handling (kind ``ground`` -> ``0``)
    is the caller's, upstream of this."""
    out: List[str] = []
    for ch in name:
        if ch == "+":
            out.append("P")
        elif ch == "-":
            out.append("M")
        elif ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    s = "".join(out)
    if s and s[0].isdigit():
        s = "N" + s
    return s


def _spice_node_map(graph: Dict[str, Any]) -> Dict[str, str]:
    """net name -> deck node token (§3): every ground-kind net -> ``0``
    unconditionally (the intended many-to-one), every other net -> its
    sanitized name. Two distinct non-ground nets sanitizing to one token is a
    HARD emit error — the map is bijective-or-refused, never suffixed, so the
    partition differential (1.3) stays cheap."""
    node_of: Dict[str, str] = {}
    owner: Dict[str, str] = {}
    for net in graph.get("nets", []):
        name = net["name"]
        if net.get("kind") == "ground":
            node_of[name] = "0"
            continue
        tok = _sanitize_node(name)
        prev = owner.get(tok)
        if prev is not None and prev != name:
            raise ModellerError(
                "SPICE_NODE_COLLISION",
                "nets %r and %r both sanitize to deck node %r — the net->node "
                "map must stay bijective (§3: refused, never suffixed)"
                % (prev, name, tok))
        owner[tok] = name
        node_of[name] = tok
    return node_of


def _spice_model_of(comp: Dict[str, Any]
                    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """How to render one component in the deck, or why it cannot be. Returns
    ``(render, None)`` for a modelled component (``render`` a primitive or
    subckt descriptor) or ``(None, reason)`` for an unmodelled one
    (``"no_model"`` | ``"value_unparseable"``). An explicit ``spice_model``
    attr (§1) wins over the §2 auto-map; a malformed model type is a structured
    error, never a silent confession."""
    attrs = comp.get("attrs") or {}
    sm = attrs.get("spice_model")
    if isinstance(sm, dict):
        model = sm.get("model")
        if model == "primitive":
            return {"kind": "primitive", "letter": sm.get("letter", ""),
                    "value": str(sm.get("value", "")),
                    "params": dict(sm.get("params") or {})}, None
        if model == "subckt":
            return {"kind": "subckt", "name": sm.get("name", ""),
                    "text": sm.get("text", "")}, None
        raise ModellerError(
            "SPICE_BAD_MODEL",
            "component %r carries a spice_model with unknown model type %r "
            "(expected 'primitive' or 'subckt', §1)"
            % (comp.get("refdes"), model))
    kind = comp.get("kind", "")
    if kind in _SPICE_AUTOMAP:
        val = _canon_spice_value(comp.get("value"))
        if val is None:
            return None, "value_unparseable"
        return {"kind": "primitive", "letter": _SPICE_AUTOMAP[kind],
                "value": val, "params": {}}, None
    return None, "no_model"


def build_cir(name: str, graph: Dict[str, Any],
              alloc: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """The ngspice-dialect ``.cir`` deck + its ``.cir.json`` confession sidecar
    for one emitted netlist — a PURE, deterministic function of the on-disk
    primaries (l2 graph + alloc), so two resolves are byte-identical and the
    rebuild CLI re-derives it exactly.

    Every component is classified (§1/§2): modelled ones render as element cards
    (``<letter><refdes> <node...> <value>`` for primitives — the letter+refdes
    element name keeps the refdes recoverable by stripping one char, which the
    1.3 reader relies on; ``X<refdes> <node...> <subckt>`` for subckt refs, with
    the inline model text deduplicated by name, §4). Nodes are the §3
    name-preserving tokens (ground -> ``0``). The rest are confessed in
    ``not_simulated`` (§5), naming EVERY unmodelled refdes with its kind and
    reason — never a silently thinner deck. Element cards are sorted by refdes
    and subckt blocks by name, so the bytes are canonical.

    Returns ``(deck_text, sidecar_dict)`` unconditionally; whether either is
    WRITTEN is the emit loop's gating call (see ``spice_should_emit``)."""
    node_of = _spice_node_map(graph)
    term_net: Dict[Tuple[str, str], str] = {}
    for net in graph.get("nets", []):
        for ref, pin in net.get("nodes", []):
            term_net[(ref, pin)] = net["name"]

    def node_for(ref: str, pin: str) -> str:
        net = term_net.get((ref, pin))
        if net is not None:
            return node_of[net]
        # An unconnected terminal of a MODELLED component gets its own
        # deterministic floating node (ga005's convention) — distinct from the
        # net token namespace, so it never silently merges with a real net.
        return "%s_%s" % (_sanitize_node(ref), _sanitize_node(pin))

    modelled: Dict[str, str] = {}
    not_simulated: List[Dict[str, str]] = []
    cards: List[Tuple[str, str]] = []
    subckts: Dict[str, str] = {}
    for comp in graph.get("components", []):
        ref = comp["refdes"]
        render, reason = _spice_model_of(comp)
        if render is None:
            not_simulated.append({"refdes": ref, "kind": comp.get("kind", ""),
                                  "reason": reason})
            continue
        nodes = [node_for(ref, t["name"]) for t in comp.get("terminals", [])]
        if render["kind"] == "primitive":
            letter = render["letter"]
            modelled[ref] = letter
            tail = render["value"]
            for k in sorted(render["params"]):
                tail += " %s=%s" % (k, render["params"][k])
            cards.append((ref, "%s%s %s %s"
                          % (letter, ref, " ".join(nodes), tail)))
        else:
            sname = render["name"]
            modelled[ref] = "subckt:%s" % sname
            text = render["text"]
            if sname in subckts and subckts[sname] != text:
                raise ModellerError(
                    "SPICE_SUBCKT_CONFLICT",
                    "two subckt models named %r carry different inline text — "
                    "inline models must be name-unique (§4)" % sname)
            subckts[sname] = text
            cards.append((ref, "X%s %s %s" % (ref, " ".join(nodes), sname)))

    stamp = _stamp(alloc)
    header = [
        "* wyred spice deck: %s" % name,
        "* engine=%s deck_format=%s"
        % (alloc.get("solver_version", ""), SPICE_DECK_VERSION),
        "* stamp=%s" % json.dumps(stamp, sort_keys=True),
        "* not_simulated: %d (see %s.cir.json)" % (len(not_simulated), name),
    ]
    nonident = sorted((net, tok) for net, tok in node_of.items()
                      if tok != net)
    if nonident:
        header.append("* nodes:")
        for net, tok in nonident:
            header.append("*   %s -> %s" % (net, tok))
    subckt_lines = [subckts[s].rstrip("\n") for s in sorted(subckts)]
    body = [txt for _ref, txt in sorted(cards, key=lambda rc: rc[0])]
    deck = "\n".join(header + subckt_lines + body + [".end"]) + "\n"

    sidecar = {
        "artifact": name,
        "path": "cir_confession",
        "stamp": stamp,
        "modelled": dict(modelled),
        "not_simulated": sorted(not_simulated, key=lambda e: e["refdes"]),
        "node_map": dict(node_of),
    }
    return deck, sidecar


def spice_should_emit(graph: Dict[str, Any], emit_spice: bool) -> bool:
    """The §0/§6 gating decision, from the graph ALONE: a deck is written iff
    the netlist has at least one modelled element AND (it is FULLY modelled, or
    the intent explicitly requested emission). A partially-modelled intent that
    neither qualifies nor requests emits nothing — the *declared* behavior, so
    the gate never sees a silently smaller deck.

    Deliberately CHEAP and node-map-free: it classifies components only, so an
    intent that will emit no deck never pays for (and never trips) the §3
    node-token collision check inside ``build_cir`` — that bijection discipline
    is owed only by decks that are actually written. A malformed ``spice_model``
    still raises here (an author error is loud regardless of emission)."""
    n_modelled = n_unmodelled = 0
    for comp in graph.get("components", []):
        render, _reason = _spice_model_of(comp)
        if render is None:
            n_unmodelled += 1
        else:
            n_modelled += 1
    if n_modelled == 0:
        return False
    if n_unmodelled == 0:
        return True
    return bool(emit_spice)


# ---------------------------------------------------------------------------
# The SPICE structural oracle (WyredPlanSpice step 1.3 / WyredSpiceContract
# §5/§10): a minimal deck READER (the ga005 ``parse_spice`` subset — comments,
# ``+`` continuations, element dispatch, subckt-instance connectivity) plus
# ``crosscheck_cir``, the fourth data path in the cross-path differential. The
# ``.cir`` is CHECKED against the L2 (the model of record) + its ``.cir.json``
# confession sidecar, never trusted: a dropped/phantom element, a rewired node,
# a value/letter inconsistent with the kind-table, or a forged/stale confession
# each fires its ``XCIR_*`` code. The engine's own from-disk CLI runs it, and
# the harness gate's negative battery proves every code fires (the lobotomy
# verdict). Two independent implementations (emitter + reader) meeting at the
# differential — gen 1's ``roundtrip.agree()`` shape, ported.
# ---------------------------------------------------------------------------

# The v0 two-terminal primitives (§1: MOSFETs and anything wider are ``subckt``,
# read as an ``X`` instance). Element letter is the head's first char; the
# refdes is the rest (build_cir writes ``<letter><refdes>`` so a strip of one
# char recovers it).
_CIR_PRIMITIVES = frozenset("RCLDVI")


def _cir_logical_lines(text: str) -> List[str]:
    """A SPICE deck's logical lines: ``*`` full-line comments and blanks
    dropped, ``+`` continuations folded onto the previous line (the
    ``parse_spice`` subset). build_cir emits neither inline ``;`` comments nor
    continuations, but the reader honors both so a hand-written or re-wrapped
    conforming deck reads the same."""
    logical: List[str] = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("*"):
            continue
        if s.startswith("+"):
            cont = s[1:].strip()
            if logical:
                logical[-1] = logical[-1] + " " + cont
            else:
                logical.append(cont)
        else:
            logical.append(s)
    return logical


def _parse_cir(text: str) -> List[Dict[str, Any]]:
    """The minimal deck reader: element cards -> connectivity, INDEPENDENT of
    build_cir. Each element is ``{refdes, letter, nodes: [token...], value}``
    (subckt instances also carry ``subckt``). ``.subckt``/``.ends`` DEFINITION
    bodies are skipped (their inline model text is not connectivity); every
    other ``.``-card (``.end``, ``.model``, ...) is ignored. Two-terminal
    primitives take the first two post-head tokens as nodes and the rest as the
    value; a subckt instance ``X<refdes> <node...> <name>`` takes the trailing
    token as the subckt name and the middle tokens as nodes."""
    elements: List[Dict[str, Any]] = []
    in_subckt = False
    for line in _cir_logical_lines(text):
        toks = line.split()
        if not toks:
            continue
        head = toks[0]
        if head.startswith("."):
            low = head.lower()
            if low == ".subckt":
                in_subckt = True
            elif low == ".ends":
                in_subckt = False
            continue
        if in_subckt:
            continue
        letter = head[0].upper()
        refdes = head[1:]
        if letter == "X":
            if len(toks) < 3:
                continue
            elements.append({"refdes": refdes, "letter": "X",
                             "nodes": toks[1:-1], "value": toks[-1],
                             "subckt": toks[-1]})
        elif letter in _CIR_PRIMITIVES:
            if len(toks) < 3:
                continue
            elements.append({"refdes": refdes, "letter": letter,
                             "nodes": toks[1:3], "value": " ".join(toks[3:])})
        else:
            elements.append({"refdes": refdes, "letter": letter,
                             "nodes": toks[1:], "value": ""})
    return elements


def _spice_model_token(render: Dict[str, Any]) -> str:
    """The ``modelled`` map token build_cir records for a rendered component:
    the element letter for a primitive, ``subckt:<name>`` for a subckt ref."""
    if render["kind"] == "primitive":
        return render["letter"]
    return "subckt:%s" % render["name"]


def _cir_partition(mapping: Dict[Any, Any]) -> set:
    """The equivalence relation induced by ``{key: label}`` as a set of
    frozensets of keys — the labels themselves are irrelevant, only which keys
    share one. Comparing two such sets is the LVS-lite partition differential
    (gen 1's ``agree()``): ground on both sides collapses to one class (L2's
    ground nets -> one label, the deck's node ``0`` -> one label), so the
    induced key-groups match iff the connectivity does."""
    groups: Dict[Any, set] = {}
    for key, label in mapping.items():
        groups.setdefault(label, set()).add(key)
    return {frozenset(v) for v in groups.values()}


def crosscheck_cir(graph: Dict[str, Any], deck_text: str,
                   sidecar: Dict[str, Any]) -> List[Dict[str, str]]:
    """Assert the ``.cir`` deck + its ``.cir.json`` sidecar denote the SAME
    circuit as the L2 (the model of record). Every mismatch is a structured
    ``{"code", "msg"}``; an empty list means the third denotation agrees.

    Codes (WyredPlanSpice 1.3):

    * ``XCIR_CONFESSION`` — the sidecar (``modelled`` / ``not_simulated`` /
      ``node_map``) disagrees with what the L2 actually models under the §1/§2
      classifier. A forged or stale confession — a real part hidden in
      ``not_simulated``, a phantom confessed, a rewritten node map — is caught
      here (§5: the sidecar is compared against the L2 + the deck every run).
    * ``XCIR_COMPONENTS`` — the deck's refdes set != the L2 components minus the
      confessed ``not_simulated`` set. A confessed part absent is legal; an
      unconfessed absence or a phantom element is not.
    * ``XCIR_ELEMENT`` — an element's letter or value is inconsistent with the
      component's kind/model per the contract table (§1/§2a canonicalization).
    * ``XCIR_NET_PARTITION`` — the deck's node partition != the L2 net partition
      over the SIMULATED subgraph (the modelled components present in the deck).
    """
    fails: List[Dict[str, str]] = []

    def fail(code: str, msg: str) -> None:
        fails.append({"code": code, "msg": msg})

    comps = graph.get("components", [])
    comp_by_ref = {c["refdes"]: c for c in comps}

    # Recompute the §1/§2 classification straight from the L2 — the honest
    # answer the sidecar and deck are checked against.
    recomputed_modelled: Dict[str, Dict[str, Any]] = {}
    recomputed_ns: List[Dict[str, str]] = []
    for c in comps:
        ref = c["refdes"]
        try:
            render, reason = _spice_model_of(c)
        except ModellerError:
            render, reason = None, "bad_model"
        if render is None:
            recomputed_ns.append({"refdes": ref, "kind": c.get("kind", ""),
                                  "reason": reason})
        else:
            recomputed_modelled[ref] = render

    # --- XCIR_CONFESSION: the sidecar must not lie about the L2 --------------
    want_modelled = {r: _spice_model_token(rd)
                     for r, rd in recomputed_modelled.items()}
    if sidecar.get("modelled", {}) != want_modelled:
        fail("XCIR_CONFESSION",
             "sidecar 'modelled' %r disagrees with the L2 classification %r"
             % (sidecar.get("modelled", {}), want_modelled))
    sc_ns = sorted(sidecar.get("not_simulated", []),
                   key=lambda e: e.get("refdes", ""))
    want_ns = sorted(recomputed_ns, key=lambda e: e["refdes"])
    if sc_ns != want_ns:
        fail("XCIR_CONFESSION",
             "sidecar not_simulated %r disagrees with the L2's unmodelled "
             "parts %r (a forged or stale confession)" % (sc_ns, want_ns))
    node_map_ok = True
    try:
        want_node_map = _spice_node_map(graph)
    except ModellerError as exc:
        node_map_ok = False
        fail("XCIR_NET_PARTITION",
             "L2 net names collide under §3 sanitization: %s" % exc.msg)
    if node_map_ok and sidecar.get("node_map", {}) != want_node_map:
        fail("XCIR_CONFESSION",
             "sidecar node_map %r disagrees with the L2 net->node map %r"
             % (sidecar.get("node_map", {}), want_node_map))

    # --- XCIR_COMPONENTS: deck refdes == L2 minus the confessed set ----------
    elements = _parse_cir(deck_text)
    deck_by_ref: Dict[str, Dict[str, Any]] = {}
    deck_refs: List[str] = []
    for e in elements:
        deck_refs.append(e["refdes"])
        deck_by_ref[e["refdes"]] = e
    dupes = sorted({r for r in deck_refs if deck_refs.count(r) > 1})
    if dupes:
        fail("XCIR_COMPONENTS",
             "deck lists an element refdes more than once: %s" % dupes)
    deck_set = set(deck_refs)
    confessed = {e.get("refdes") for e in sidecar.get("not_simulated", [])}
    expected = {c["refdes"] for c in comps} - confessed
    if deck_set != expected:
        fail("XCIR_COMPONENTS",
             "deck refdes set != L2 components minus the confessed set "
             "(deck-only=%s, missing=%s)"
             % (sorted(deck_set - expected), sorted(expected - deck_set)))

    # --- XCIR_ELEMENT: letter + value vs the kind table ---------------------
    for ref in sorted(recomputed_modelled):
        e = deck_by_ref.get(ref)
        if e is None:
            continue        # absence is XCIR_COMPONENTS' finding, not this one
        render = recomputed_modelled[ref]
        if render["kind"] == "primitive":
            want_value = render["value"]
            for k in sorted(render["params"]):
                want_value += " %s=%s" % (k, render["params"][k])
            if e["letter"] != render["letter"]:
                fail("XCIR_ELEMENT",
                     "deck element %s has letter %r but its L2 kind/model "
                     "implies %r" % (ref, e["letter"], render["letter"]))
            elif e["value"] != want_value:
                fail("XCIR_ELEMENT",
                     "deck element %s carries value %r but its L2 model "
                     "canonicalizes to %r" % (ref, e["value"], want_value))
        else:
            if e["letter"] != "X":
                fail("XCIR_ELEMENT",
                     "deck element %s should be a subckt instance (X) but is "
                     "letter %r" % (ref, e["letter"]))
            elif e.get("subckt") != render["name"]:
                fail("XCIR_ELEMENT",
                     "deck element %s instantiates subckt %r but its L2 model "
                     "names %r" % (ref, e.get("subckt"), render["name"]))

    # --- XCIR_NET_PARTITION: deck node partition == L2 net partition ---------
    term_to_net: Dict[Tuple[str, str], Tuple[str, Optional[str]]] = {}
    for net in graph.get("nets", []):
        for ref, pin in net.get("nodes", []):
            term_to_net[(ref, pin)] = (net["name"], net.get("kind"))
    common = set(deck_by_ref) & set(recomputed_modelled)
    l2_map: Dict[Tuple[str, int], Any] = {}
    for ref in common:
        for i, t in enumerate(comp_by_ref[ref].get("terminals", [])):
            info = term_to_net.get((ref, t["name"]))
            if info is None:
                l2_map[(ref, i)] = ("float", ref, i)
            elif info[1] == "ground":
                l2_map[(ref, i)] = ("ground",)
            else:
                l2_map[(ref, i)] = ("net", info[0])
    deck_map: Dict[Tuple[str, int], Any] = {}
    for ref in common:
        for i, node in enumerate(deck_by_ref[ref]["nodes"]):
            deck_map[(ref, i)] = ("node", node)
    if _cir_partition(l2_map) != _cir_partition(deck_map):
        fail("XCIR_NET_PARTITION",
             "deck node partition disagrees with the L2 net partition over "
             "the simulated subgraph (%d modelled component(s))" % len(common))

    return fails


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


__all__ = ["build_bom", "build_pinmap", "build_records", "build_placement",
           "build_testplan", "build_cir", "spice_should_emit", "crosscheck",
           "crosscheck_cir", "check_connector_locks", "diff_pinmaps",
           "json_str"]
