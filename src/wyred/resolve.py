"""The elaboration RESOLVER — the first data path (Gen4 section 1.5 tier 3).

``resolve(l1_doc, ...)`` consumes the emitted LAYER-1 JSON document (the
stable model IR — never the engine's internals, so any tool emitting the
contract shape could feed this path) plus the layer-2 refinement inputs
(role->part binds, pins already applied at L1, freeze declarations) and
produces the bound netlist: a ``CanonicalGraph``-shaped dict per
rl/harness/EMIT_CONTRACT.md Part A, plus the allocation-record artifact
(stamped with (series, lock-group versions)).

Design commitments (each one a do-not-repeat item made structural):

* FIXPOINT resolution, not ordered passes. All wiring/generation work is an
  OBLIGATION on a single worklist; the loop runs every obligation whose
  prerequisites (published nets/nodes) exist and repeats until stable. A
  companion generated late (the bootstrap network needs the half-bridge
  template's source net; the interlock needs the allocation's command nets)
  is wired by the same loop that wired everything else. Obligations left
  unready after a stable pass are a structured RESOLVE_STUCK error.
* NO DANGLING GENERATED LEG. After the fixpoint, every terminal of every
  authored=False component must sit on a net — a hard load error here,
  before the oracle ever sees the graph.
* GUARANTEES SURVIVE LOWERING. Layer-1 mutual-exclusion declarations are
  lowered to concrete ``Invariant`` records anchored on the DRIVEN physical
  gate nodes (the mosfet gate pins, reachable from the MCU command pins
  through real generated logic), and the interlock itself is synthesized as
  real logic_gate components — the safety property is model-checkable from
  the emitted graph alone.
* DEMAND-DRIVEN GENERATION ONLY (law 7). Every generated component is
  produced by a DECLARED companion (demand/capability/bus attrs) or a
  DECLARED invariant/pool policy, and carries ``attrs["for_demand"]``
  naming the L1 demand/role that asked for it. There is no generator that
  is not keyed to a declaration.
* NO SILENT DEFAULTS. Every derived value (pull-up ohms from the i2c_speed
  lever, crystal load caps from CL) is recorded on the generated part
  (``attrs["derived"]``); binding decisions are recorded with provenance
  (chosen_by solver/author) in the emitted allocation artifact; an
  unresolved demand (no L1 resolution, no allocation, no escalation) is a
  structured load error, never a partial netlist.
* NO PER-INTENT CODE. One resolver, keyed only by the L1 vocabulary (role
  kinds, ifaces, companion names, invariant kinds).

Freeze points (Gen4 section 2.5): netlist emit is a sync point. ``freeze``
names lock groups to fire AT THIS EMIT: covered allocation entries are
batch-promoted to pinned with ``locked_by="<group>@<version>"``, the group
version bumps and its snapshot freezes — and the returned layer-1 document
carries the locked record, so every artifact of this emit is stamped by the
same freeze.

Pure Python 3 stdlib. No harness imports — the emitted JSON is the contract.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .core import ModellerError
from . import parts as lib


_VTOL = 0.05


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

@dataclass
class ResolveResult:
    """One resolution's full output: the (possibly freeze-updated) layer-1
    document, the layer-2 graph dict, the allocation artifact dict, and
    the allocation-wiring view (where each allocation entry LANDED — the
    pin-map data path's enrichment; the representative node is the entry's
    canonically-first signal port)."""

    name: str
    l1: Dict[str, Any]
    graph: Dict[str, Any]
    alloc: Dict[str, Any]
    alloc_wiring: List[Dict[str, Any]] = field(default_factory=list)

    def graph_json_str(self) -> str:
        return json.dumps(self.graph, indent=2, sort_keys=True) + "\n"

    def alloc_json_str(self) -> str:
        return json.dumps(self.alloc, indent=2, sort_keys=True) + "\n"

    def l1_json_str(self) -> str:
        return json.dumps(self.l1, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Document view (plain-data accessors over the L1 JSON dict)
# ---------------------------------------------------------------------------

class _DocView:
    def __init__(self, doc: Dict[str, Any]):
        self.doc = doc
        self.series: str = doc.get("series", "A")
        self.roles: List[Dict[str, Any]] = doc.get("roles", [])
        self.role_by_id = {r["id"]: r for r in self.roles}
        self.demand_owner: Dict[str, Tuple[Dict, Dict]] = {}
        for r in self.roles:
            for d in r.get("demands", []) or []:
                self.demand_owner[d["id"]] = (r, d)
        self.rails = {r["name"]: r for r in doc.get("rails", [])}
        self.grounds: List[Dict[str, Any]] = doc.get("grounds", [])
        self.bond_of: Dict[str, str] = {}
        for b in doc.get("bonds", []) or []:
            for g in b.get("joins", []):
                self.bond_of[g] = b["name"]
        self.buses = {b["name"]: b for b in doc.get("buses", []) or []}
        self.pools = {p["name"]: p for p in doc.get("pools", []) or []}
        self.pools_of_role: Dict[str, List[Dict[str, Any]]] = {}
        for p in doc.get("pools", []) or []:
            self.pools_of_role.setdefault(p["role"], []).append(p)
        self.resolutions: Dict[str, str] = (
            doc.get("attrs", {}) or {}).get("resolutions", {}) or {}
        self.allocation: Dict[str, Any] = doc.get("allocation", {}) or {}
        self.entries: List[Dict[str, Any]] = list(
            self.allocation.get("entries", []) or [])
        self.entries_by_demand: Dict[str, List[Dict[str, Any]]] = {}
        for e in sorted(self.entries,
                        key=lambda e: (e["pool"], e["unit"], e["demand"])):
            self.entries_by_demand.setdefault(e["demand"], []).append(e)
        self.invariants: List[Dict[str, Any]] = doc.get("invariants", []) or []
        self.escalations: List[Dict[str, Any]] = (
            doc.get("escalations", []) or [])
        self._parents = {s["id"]: s.get("parent", "")
                         for s in doc.get("scopes", []) or []}

    def chain(self, scope: str) -> List[str]:
        out, seen, cur = [scope], {scope}, scope
        while cur != "":
            cur = self._parents.get(cur, "")
            if cur in seen:
                break
            out.append(cur)
            seen.add(cur)
        return out

    def nearest_ground(self, scope: str) -> Dict[str, Any]:
        for s in self.chain(scope):
            here = [g for g in self.grounds if g.get("scope", "") == s]
            if len(here) == 1:
                return here[0]
            if len(here) > 1:
                raise ModellerError(
                    "RESOLVE_GROUND_AMBIGUOUS",
                    "scope %r declares %d grounds (%s); the resolver refuses "
                    "to guess a return path" % (
                        s, len(here), ", ".join(g["name"] for g in here)))
        raise ModellerError(
            "RESOLVE_NO_GROUND",
            "no ground is in scope for %r — a return path cannot be "
            "silently invented" % scope)

    def nearest_rail(self, scope: str, volts: float) -> Optional[Dict]:
        for s in self.chain(scope):
            ok = [r for r in self.rails.values()
                  if r.get("scope", "") == s
                  and abs(float(r["volts"]) - volts) <= _VTOL]
            if len(ok) == 1:
                return ok[0]
        return None

    def split_ref(self, ref: str) -> Tuple[str, str]:
        """An intent-level reference "<role_id>.<label>": role ids may
        contain dots, so take the LONGEST declared role-id prefix."""
        parts = ref.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            rid = ".".join(parts[:cut])
            if rid in self.role_by_id:
                return rid, ".".join(parts[cut:])
        raise ModellerError(
            "RESOLVE_BAD_REF",
            "reference %r names no declared role prefix" % ref)

    def escalated(self, role_id: str, demand_id: str) -> bool:
        for e in self.escalations:
            for s in e.get("subjects", []):
                if s == demand_id or s == role_id:
                    return True
                try:
                    rid, _ = self.split_ref(s)
                except ModellerError:
                    continue
                if rid == role_id:
                    return True
        return False


# ---------------------------------------------------------------------------
# The builder: components, nets, published facts, the obligation worklist
# ---------------------------------------------------------------------------

class _Part:
    """One instantiated component under construction."""

    def __init__(self, refdes: str, proto: lib.PartProto,
                 attrs: Dict[str, Any]):
        self.refdes = refdes
        self.proto = proto
        self.comp: Dict[str, Any] = {
            "refdes": refdes,
            "kind": proto.kind,
            "value": proto.value,
            "authored": bool(attrs.get("l1_role")),
            "terminals": [dict(t) for t in proto.terminals],
            "attrs": dict(proto.attrs),
            "logic_fn": proto.logic_fn,
        }
        self.comp["attrs"].update(attrs)


class _Builder:
    def __init__(self, dv: _DocView):
        self.dv = dv
        self.components: List[Dict[str, Any]] = []
        self.parts_of_role: Dict[str, List[_Part]] = {}
        self.nets: Dict[str, Dict[str, Any]] = {}       # name -> net dict
        self.net_order: List[str] = []
        self.node_net: Dict[Tuple[str, str], str] = {}
        self.published: Dict[str, Any] = {}             # cross-obligation
        self._refdes_seq: Dict[str, int] = {}
        self._used_refdes: set = set()
        self.lowered_invariants: List[Dict[str, Any]] = []

    # -- components ------------------------------------------------------
    def mint(self, proto: lib.PartProto, attrs: Dict[str, Any]) -> _Part:
        if proto.refdes is not None:
            # explicit identity (library back-annotation of a real board):
            # honored verbatim, uniqueness enforced, never counter-minted.
            refdes = proto.refdes
        else:
            n = self._refdes_seq.get(proto.prefix, 0) + 1
            self._refdes_seq[proto.prefix] = n
            refdes = "%s%d" % (proto.prefix, n)
        if refdes in self._used_refdes:
            raise ModellerError(
                "RESOLVE_REFDES_CLASH",
                "refdes %r minted/declared twice — component identity must "
                "be unique" % refdes)
        self._used_refdes.add(refdes)
        part = _Part(refdes, proto, attrs)
        self.components.append(part.comp)
        return part

    def generate(self, proto: lib.PartProto, for_demand: str,
                 derived: str = "") -> _Part:
        attrs: Dict[str, Any] = {"for_demand": for_demand}
        if derived:
            attrs["derived"] = derived
        part = self.mint(proto, attrs)
        part.comp["authored"] = False
        return part

    # -- nets --------------------------------------------------------------
    def _new_net(self, name: str, kind: str,
                 voltage: Optional[float]) -> Dict[str, Any]:
        if name in self.nets:
            raise ModellerError(
                "RESOLVE_NET_CLASH", "net name %r minted twice" % name)
        net: Dict[str, Any] = {"name": name, "kind": kind,
                               "voltage": voltage, "nodes": []}
        self.nets[name] = net
        self.net_order.append(name)
        return net

    def rail_net(self, rail_name: str) -> str:
        if rail_name not in self.nets:
            rail = self.dv.rails.get(rail_name)
            if rail is None:
                raise ModellerError(
                    "RESOLVE_UNKNOWN_RAIL",
                    "no declared rail named %r" % rail_name)
            self._new_net(rail_name, "power", float(rail["volts"]))
        return rail_name

    def ground_net(self, ground: Dict[str, Any]) -> str:
        name = ground["name"]
        if name not in self.nets:
            net = self._new_net(name, "ground", 0.0)
            net["ground_kind"] = ground.get("kind", "ground")
            net["ground_role"] = ground.get("role", "none")
            bond = self.dv.bond_of.get(name)
            if bond is not None:
                net["bond"] = bond
        return name

    def bus_net(self, bus_name: str, member: str) -> str:
        name = "%s_%s" % (bus_name, member.upper())
        if name not in self.nets:
            self._new_net(name, "signal", None)
        return name

    def signal_net(self, name: str) -> str:
        if name not in self.nets:
            self._new_net(name, "signal", None)
        return name

    def attach(self, net_name: str, node: Tuple[str, str]) -> None:
        cur = self.node_net.get(node)
        if cur == net_name:
            return
        if cur is not None:
            raise ModellerError(
                "RESOLVE_NODE_CONFLICT",
                "%s.%s is already on net %r; refusing to also join %r"
                % (node[0], node[1], cur, net_name))
        self.nets[net_name]["nodes"].append([node[0], node[1]])
        self.node_net[node] = net_name

    def connect(self, a: Tuple[str, str], b: Tuple[str, str],
                hint: str) -> str:
        """Join two pins, reusing whichever net either already sits on —
        the order-independence primitive the fixpoint relies on."""
        na, nb = self.node_net.get(a), self.node_net.get(b)
        if na is not None and nb is not None:
            if na != nb:
                raise ModellerError(
                    "RESOLVE_NODE_CONFLICT",
                    "cannot connect %s.%s (net %r) to %s.%s (net %r)"
                    % (a[0], a[1], na, b[0], b[1], nb))
            return na
        name = na or nb or self.signal_net(hint)
        self.attach(name, a)
        self.attach(name, b)
        return name


# ---------------------------------------------------------------------------
# The resolver proper
# ---------------------------------------------------------------------------

def _iface_family(wanted: str, provided: str) -> bool:
    return provided == wanted or provided.startswith(wanted + "_")


class _Resolver:
    def __init__(self, doc: Dict[str, Any], name: str,
                 bindings: Sequence[Tuple[str, str]],
                 freeze: Sequence[str]):
        self.dv = _DocView(doc)
        self.name = name
        self.author_binds = dict(bindings)
        self.freeze = tuple(freeze)
        self.b = _Builder(self.dv)
        self.obligations: List[Tuple[str, Callable[[], bool],
                                     Callable[[], None]]] = []
        self.bind_provenance: Dict[str, str] = {}   # role -> solver|author
        # role-level realization wiring (M3): typed port sets that may span
        # a multi-part realization, and its declared internal edges.
        self.role_demand_ports: Dict[str, Dict[str, List[Dict[str,
                                     Tuple[str, str]]]]] = {}
        self.role_provide_ports: Dict[str, Dict[str, List[Dict[str,
                                      Tuple[str, str]]]]] = {}
        self.realization_edges: List[Tuple[str, str,
                                     List[Tuple[str, str]]]] = []
        # (pool, unit, demand) -> representative provider node — recorded
        # at allocation-wiring time, materialized (with nets) after the
        # fixpoint for the pin-map data path.
        self.alloc_nodes: Dict[Tuple[str, int, str], Tuple[str, str]] = {}

    # -- obligation plumbing ----------------------------------------------
    def _ob(self, label: str, run: Callable[[], None],
            ready: Optional[Callable[[], bool]] = None) -> None:
        self.obligations.append((label, ready or (lambda: True), run))

    def _run_fixpoint(self) -> None:
        """The FIXPOINT loop (do-not-repeat #1: no ordered passes). Every
        obligation runs as soon as its prerequisites exist; obligations
        created by other obligations (companions of companions, the
        interlock's dependence on the allocation's command nets) join the
        same loop. Stable-with-leftovers is a structured load error."""
        pending = self.obligations
        self.obligations = []       # becomes the inbox for new obligations
        while pending:
            progressed = False
            still: List[Tuple[str, Callable, Callable]] = []
            for label, ready, run in pending:
                if ready():
                    run()          # may append NEW obligations to the inbox
                    progressed = True
                else:
                    still.append((label, ready, run))
            pending = still + self.obligations
            self.obligations = []
            if not progressed and pending:
                raise ModellerError(
                    "RESOLVE_STUCK",
                    "fixpoint stalled with %d unready obligation(s): %s"
                    % (len(pending),
                       "; ".join(label for label, _, _ in pending)))

    # -- gate: the allocation record is TRUSTED INPUT — validate it -------
    def _check_allocation_record(self) -> None:
        """Structured rejection of a malformed/tampered/foreign allocation
        record BEFORE any wiring keys off it. The resolver consumes the L1
        JSON as an any-tool IR (any tool emitting the contract shape may
        feed this path), so a record naming an unknown pool/demand, an
        out-of-range unit, an iface-incompatible pairing, or MORE entries
        for a demand than its declared qty must be a structured
        ModellerError — never a bare IndexError/KeyError mid-wire, and
        never a silently-unwired entry."""
        for e in self.dv.entries:
            pool = self.dv.pools.get(e.get("pool"))
            if pool is None:
                raise ModellerError(
                    "RESOLVE_ALLOC_INVALID",
                    "allocation entry names pool %r, which the document "
                    "does not declare" % e.get("pool"))
            try:
                unit = int(e.get("unit"))
            except (TypeError, ValueError):
                raise ModellerError(
                    "RESOLVE_ALLOC_INVALID",
                    "allocation entry for pool %r carries non-integer "
                    "unit %r" % (e.get("pool"), e.get("unit")))
            if not 0 <= unit < int(pool["unit_count"]):
                raise ModellerError(
                    "RESOLVE_ALLOC_INVALID",
                    "allocation entry names unit %d of pool %r, which "
                    "declares units 0..%d"
                    % (unit, pool["name"], int(pool["unit_count"]) - 1))
            owner = self.dv.demand_owner.get(e.get("demand"))
            if owner is None:
                raise ModellerError(
                    "RESOLVE_ALLOC_INVALID",
                    "allocation entry serves demand %r, which no declared "
                    "role owns — a foreign entry cannot be wired (and "
                    "would otherwise be silently dropped)"
                    % e.get("demand"))
            _r, d = owner
            if not _iface_family(d["iface"], pool["provides"]):
                raise ModellerError(
                    "RESOLVE_ALLOC_INVALID",
                    "allocation entry pairs demand %r (iface %r) with pool "
                    "%r (provides %r) — not a declared-equivalent match"
                    % (d["id"], d["iface"], pool["name"], pool["provides"]))
        for did, ents in sorted(self.dv.entries_by_demand.items()):
            _r, d = self.dv.demand_owner[did]
            limit = max(1, int(d.get("qty", 1)))
            if len(ents) > limit:
                raise ModellerError(
                    "RESOLVE_ALLOC_OVERSERVED",
                    "demand %r declares qty %d but the allocation record "
                    "carries %d entries for it — an over-served demand has "
                    "no unit ports for the extra entries and the record is "
                    "not a legal bijection" % (did, limit, len(ents)))

    # -- gate: every demand accounted for ---------------------------------
    def _gate(self) -> None:
        self._check_allocation_record()
        for r in self.dv.roles:
            for d in r.get("demands", []) or []:
                did = d["id"]
                if did in self.dv.resolutions:
                    continue
                ents = self.dv.entries_by_demand.get(did, [])
                if len(ents) >= max(1, int(d.get("qty", 1))):
                    continue
                if self.dv.escalated(r["id"], did):
                    continue
                raise ModellerError(
                    "RESOLVE_UNRESOLVED",
                    "demand %r has no resolution, no (full) allocation and "
                    "no escalation — the layer-1 ladder left it open; a "
                    "netlist cannot be emitted for an unresolved intent"
                    % did)
        for g in self.freeze:
            if not any(lg["name"] == g for lg in
                       self.dv.allocation.get("lock_groups", []) or []):
                raise ModellerError(
                    "FREEZE_UNKNOWN_GROUP",
                    "freeze names lock group %r, which is not declared in "
                    "the document" % g)

    # -- binding (the ladder applied to parts) -----------------------------
    def _bind_roles(self) -> None:
        for role in self.dv.roles:
            rid = role["id"]
            pools = sorted(self.dv.pools_of_role.get(rid, []),
                           key=lambda p: p["name"])
            realization = lib.parts_for(role, pools)
            author = self.author_binds.pop(rid, None)
            if realization is None:
                if author is not None:
                    raise ModellerError(
                        "BIND_UNBINDABLE",
                        "role %r (kind %r) has no part in the library; "
                        "bind(%r) cannot be honored"
                        % (rid, role.get("kind"), author))
                continue
            protos = realization.parts
            if author is not None:
                names = [p.value for p in protos]
                if author not in names:
                    raise ModellerError(
                        "BIND_INCOMPATIBLE",
                        "bind(%r, part=%r) rejected: the library part(s) "
                        "structurally covering role %r are %s — an author "
                        "bind must name a covering part"
                        % (rid, author, rid, names))
                self.bind_provenance[rid] = "author"
            else:
                self.bind_provenance[rid] = "solver"
            parts = []
            for p in protos:
                if p.generated:
                    # a library-realization companion: authored=False with
                    # for_demand provenance + a derivation confession (law 7
                    # + law 10 — nothing generated is unexplained).
                    attrs: Dict[str, Any] = {
                        "for_demand": rid,
                        "derived": p.derived
                        or "library realization of role %s" % rid,
                    }
                    part = self.b.mint(p, attrs)
                    part.comp["authored"] = False
                else:
                    part = self.b.mint(p, {"l1_role": rid})
                parts.append(part)
            self.b.parts_of_role[rid] = parts

            # role-level typed port sets (a port set may span the parts of
            # a multi-part realization) — indexes resolve to minted refdes.
            def _node(ref: Tuple[int, str]) -> Tuple[str, str]:
                idx, pin = ref
                if not 0 <= idx < len(parts):
                    raise ModellerError(
                        "RESOLVE_BAD_REALIZATION",
                        "realization of role %r references part index %d "
                        "(has %d parts)" % (rid, idx, len(parts)))
                return (parts[idx].refdes, pin)

            if realization.demand_ports:
                self.role_demand_ports[rid] = {
                    key: [{sig: _node(ref) for sig, ref in unit.items()}
                          for unit in units]
                    for key, units in realization.demand_ports.items()}
            if realization.provide_ports:
                self.role_provide_ports[rid] = {
                    key: [{sig: _node(ref) for sig, ref in unit.items()}
                          for unit in units]
                    for key, units in realization.provide_ports.items()}
            for hint, nodes in realization.edges:
                self.realization_edges.append(
                    (rid, hint, [_node(ref) for ref in nodes]))
        if self.author_binds:
            raise ModellerError(
                "BIND_UNKNOWN_ROLE",
                "bind targets unknown role(s): %s"
                % ", ".join(sorted(self.author_binds)))

    # -- helpers -----------------------------------------------------------
    def _role_parts(self, rid: str) -> List[_Part]:
        return self.b.parts_of_role.get(rid, [])

    def _part_with(self, rid: str, pred) -> Optional[_Part]:
        for p in self._role_parts(rid):
            if pred(p):
                return p
        return None

    def _power_pins(self, rid: str,
                    volts: Optional[float]) -> List[Tuple[str, str]]:
        """EVERY matching demand-facing power pin across the role's bound
        parts (multi-domain parts and multi-part realizations wire all
        their supply pins — no hidden power pins, Gen4 §2.1)."""
        nodes: List[Tuple[str, str]] = []
        for p in self._role_parts(rid):
            for pin, req in p.proto.power_pins:
                if volts is None or req is None \
                        or abs(req - volts) <= _VTOL:
                    nodes.append((p.refdes, pin))
        if not nodes:
            raise ModellerError(
                "RESOLVE_NO_PIN",
                "no bound part of role %r exposes a power pin for %sV"
                % (rid, "any" if volts is None else "%g" % volts))
        return nodes

    def _ground_for_role(self, role: Dict[str, Any]) -> str:
        g = self.dv.nearest_ground(role.get("scope", ""))
        return self.b.ground_net(g)

    # -- obligations: one bound role --------------------------------------
    def _plan_role(self, role: Dict[str, Any]) -> None:
        rid = role["id"]
        parts = self._role_parts(rid)

        for part in parts:
            for pin in part.proto.ground_pins:
                self._ob("ground %s.%s" % (part.refdes, pin),
                         self._mk_attach_ground(role, part, pin))
            for pin, volts, rail in part.proto.rail_pins:
                self._ob("rail %s.%s" % (part.refdes, pin),
                         self._mk_attach_rail(part, pin, rail))

        for cap in role.get("capabilities", []) or []:
            iface = cap["iface"]
            if iface == "i2c_master" or iface == "i2c":
                self._ob("i2c-master %s" % rid,
                         self._mk_attach_i2c_cap(role))
            for k, comp in enumerate(
                    (cap.get("attrs", {}) or {}).get("companions", [])):
                self._plan_cap_companion(role, cap, comp, k)

        for d in role.get("demands", []) or []:
            self._plan_demand(role, d)

    def _mk_attach_ground(self, role, part, pin):
        def run():
            self.b.attach(self._ground_for_role(role), (part.refdes, pin))
        return run

    def _mk_attach_rail(self, part, pin, rail):
        def run():
            self.b.attach(self.b.rail_net(rail), (part.refdes, pin))
        return run

    def _mk_attach_i2c_cap(self, role):
        def run():
            chain = self.dv.chain(role.get("scope", ""))
            buses = [b for b in self.dv.buses.values()
                     if b.get("iface") == "i2c"
                     and b.get("scope", "") in chain]
            if not buses:
                return                      # a master with no bus: nothing owed
            if len(buses) > 1:
                raise ModellerError(
                    "RESOLVE_BUS_AMBIGUOUS",
                    "role %r provides i2c but %d i2c buses are in scope; "
                    "the capability must name one"
                    % (role["id"], len(buses)))
            self._attach_i2c(role["id"], buses[0]["name"])
        return run

    def _attach_i2c(self, rid: str, bus_name: str) -> None:
        parts = [p for p in self._role_parts(rid) if p.proto.i2c_pins]
        if not parts:
            raise ModellerError(
                "RESOLVE_NO_PIN",
                "role %r attaches to bus %r but its bound part has no "
                "i2c pins" % (rid, bus_name))
        for part in parts:
            for member, pin in sorted(part.proto.i2c_pins.items()):
                self.b.attach(self.b.bus_net(bus_name, member),
                              (part.refdes, pin))

    # -- obligations: one demand -------------------------------------------
    def _plan_demand(self, role: Dict[str, Any], d: Dict[str, Any]) -> None:
        rid, did, iface = role["id"], d["id"], d["iface"]
        res = self.dv.resolutions.get(did)
        ents = self.dv.entries_by_demand.get(did, [])

        if res is not None and res.startswith("rail:"):
            rail = res.split(":", 1)[1].split(" ")[0]
            if self._role_parts(rid):
                self._ob("power %s" % did,
                         self._mk_wire_power(rid, d, rail))
            # unbound composite roles (e.g. a half-bridge's vbus) are wired
            # by their module template.
        elif res is not None and res.startswith("bus:"):
            bus = res.split(":", 1)[1].split(" ")[0]
            self._ob("bus %s" % did, self._mk_wire_bus(rid, bus))
        elif res is not None and res.startswith("role:"):
            provider = res.split(":", 1)[1].split(" ")[0]
            self._ob("wire %s" % did,
                     self._mk_wire_provider(rid, d, provider))
        if ents:
            self._ob("alloc %s" % did, self._mk_wire_alloc(rid, d, ents))

        for k, comp in enumerate(
                (d.get("attrs", {}) or {}).get("companions", [])):
            self._plan_demand_companion(role, d, comp, k)

    def _mk_wire_power(self, rid, d, rail):
        def run():
            for node in self._power_pins(rid, d.get("volts")):
                self.b.attach(self.b.rail_net(rail), node)
        return run

    def _mk_wire_bus(self, rid, bus):
        def run():
            self._attach_i2c(rid, bus)
        return run

    def _demand_unit_nodes(self, rid: str, d: Dict[str, Any]
                           ) -> Optional[List[Dict[str, Tuple[str, str]]]]:
        """The demander-side typed port sets for one demand: role-level
        realization ports first (keyed ``"<iface>#<label>"`` for roles with
        several demands of one iface, bare ``"<iface>"`` otherwise), then
        the M1/M2 proto-level ``demand_units`` fallback. None == the demand
        has no demander-side ports (a bare command — the published-net
        path)."""
        iface, did = d["iface"], d["id"]
        label = did[len(rid) + 1:] if did.startswith(rid + ".") else did
        role_ports = self.role_demand_ports.get(rid, {})
        units = role_ports.get("%s#%s" % (iface, label))
        if units is None:
            units = role_ports.get(iface)
        if units is not None:
            return units
        part = self._part_with(rid, lambda p: iface in p.proto.demand_units)
        if part is None:
            return None
        return [{sig: (part.refdes, pin) for sig, pin in unit.items()}
                for unit in part.proto.demand_units[iface]]

    def _provide_unit_nodes(self, rid: str, iface: str
                            ) -> Optional[List[Dict[str, Tuple[str, str]]]]:
        """Provider-side typed port sets of a role for ``iface`` (family
        match): role-level realization ports first, proto-level
        ``provide_units`` fallback."""
        role_ports = self.role_provide_ports.get(rid, {})
        for piface in sorted(role_ports):
            if _iface_family(iface, piface):
                return role_ports[piface]
        found = None
        for p in self._role_parts(rid):
            for piface in p.proto.provide_units:
                if _iface_family(iface, piface):
                    found = (p, piface)
        if found is None:
            return None
        part, piface = found
        return [{sig: (part.refdes, pin) for sig, pin in unit.items()}
                for unit in part.proto.provide_units[piface]]

    def _mk_wire_provider(self, rid, d, provider):
        """Rung-1 wiring: demander unit ports <-> provider unit ports,
        paired per the library's iface convention."""
        def run():
            iface = d["iface"]
            dunits = self._demand_unit_nodes(rid, d)
            punits = self._provide_unit_nodes(provider, iface)
            if dunits is None or punits is None:
                raise ModellerError(
                    "RESOLVE_NO_PIN",
                    "demand %r resolved to role %r but the bound parts "
                    "expose no matching %r unit ports" % (d["id"], provider,
                                                          iface))
            pairing = lib.PAIRING.get(iface, {})
            for k, dunit in enumerate(dunits):
                punit = punits[k % len(punits)]
                for sig in sorted(punit):
                    peer = pairing.get(sig, sig)
                    self.b.connect(punit[sig], dunit[peer],
                                   "%s.%s" % (d["id"], sig))
        return run

    def _mk_wire_alloc(self, rid, d, ents):
        """Rung-3 wiring: the allocation record's unit choice becomes the
        edge — the k-th entry serves the demander's k-th unit port set."""
        def run():
            iface = d["iface"]
            dunits = self._demand_unit_nodes(rid, d)
            pairing = lib.PAIRING.get(iface, {})
            for k, e in enumerate(ents):
                pool = self.dv.pools[e["pool"]]
                owner = self._part_with(
                    pool["role"],
                    lambda p: pool["provides"] in p.proto.pool_units)
                if owner is None:
                    raise ModellerError(
                        "RESOLVE_NO_PIN",
                        "pool %r allocated to %r but its owner role %r has "
                        "no bound part exposing the pool's unit ports"
                        % (e["pool"], d["id"], pool["role"]))
                punit = owner.proto.pool_units[pool["provides"]][e["unit"]]
                alloc_key = (e["pool"], int(e["unit"]), e["demand"])
                if dunits is None:
                    # No demander-side port set (e.g. a pwm command): the
                    # provider pin becomes the published command node/net.
                    sig = sorted(punit)[0]
                    node = (owner.refdes, punit[sig])
                    net = self.b.node_net.get(node)
                    if net is None:
                        net = self.b.signal_net(
                            "CMD.%s" % d["id"] if len(ents) == 1
                            else "CMD.%s.%d" % (d["id"], k))
                        self.b.attach(net, node)
                    self.b.published["cmdnet:%s#%d" % (d["id"], k)] = net
                    self.b.published["cmdnode:%s#%d" % (d["id"], k)] = node
                    self.alloc_nodes[alloc_key] = node
                    continue
                dunit = dunits[k]
                self.alloc_nodes[alloc_key] = (
                    owner.refdes, punit[sorted(punit)[0]])
                for sig in sorted(punit):
                    peer = pairing.get(sig, sig)
                    self.b.connect((owner.refdes, punit[sig]),
                                   dunit[peer],
                                   "%s.%d.%s" % (d["id"], k, sig))
        return run

    # -- companions (demand-driven generation, law 7) ------------------------
    def _plan_demand_companion(self, role, d, comp: str, k: int) -> None:
        rid, did = role["id"], d["id"]
        if comp == "decoupling_cap":
            def run():
                res = self.dv.resolutions.get(did, "")
                if not res.startswith("rail:"):
                    raise ModellerError(
                        "RESOLVE_COMPANION_UNANCHORED",
                        "decoupling companion on %r has no resolved power "
                        "rail to decouple" % did)
                rail = res.split(":", 1)[1].split(" ")[0]
                cap = self.b.generate(_cap_proto(lib.DECOUPLING_VALUE),
                                      for_demand=did,
                                      derived="decoupling for %s" % did)
                self.b.attach(self.b.rail_net(rail), (cap.refdes, "1"))
                self.b.attach(self._ground_for_role(role), (cap.refdes, "2"))
            self._ob("decoupling %s" % did, run)
        elif comp == "bootstrap":
            self._ob("bootstrap %s" % did, self._mk_bootstrap(role, d),
                     ready=lambda: ("srcnet:%s" % rid) in self.b.published)
        elif comp == "gate_driver":
            raise ModellerError(
                "RESOLVE_UNKNOWN_COMPANION",
                "companion 'gate_driver' on %r: gate drivers are BOUND "
                "parts of a bridge_leg role (multi-part binding), not "
                "generated companions — declare 'bootstrap' instead" % did)
        else:
            raise ModellerError(
                "RESOLVE_UNKNOWN_COMPANION",
                "demand %r declares companion %r, which the library does "
                "not define (nothing may be generated without a declared, "
                "known generator)" % (did, comp))

    def _mk_bootstrap(self, role, d):
        """Bootstrap network for a gate driver: diode rail->VB, cap VB->VS
        (the leg's source node published by the half-bridge template)."""
        rid, did = role["id"], d["id"]

        def run():
            drv = self._part_with(
                rid, lambda p: any(t["name"] == "VB"
                                   for t in p.proto.terminals))
            if drv is None:
                raise ModellerError(
                    "RESOLVE_COMPANION_UNANCHORED",
                    "bootstrap companion on %r: no bound part with a VB "
                    "pin" % did)
            res = self.dv.resolutions.get(did, "")
            rail = res.split(":", 1)[1].split(" ")[0]
            src_net = self.b.published["srcnet:%s" % rid]
            boot = self.b.signal_net("BOOT.%s" % rid)
            self.b.attach(boot, (drv.refdes, "VB"))
            dio = self.b.generate(
                _diode_proto(lib.BOOTSTRAP_DIODE_VALUE), for_demand=did,
                derived="bootstrap diode for %s" % did)
            self.b.attach(self.b.rail_net(rail), (dio.refdes, "A"))
            self.b.attach(boot, (dio.refdes, "K"))
            cap = self.b.generate(
                _cap_proto(lib.BOOTSTRAP_CAP_VALUE), for_demand=did,
                derived="bootstrap cap for %s" % did)
            self.b.attach(boot, (cap.refdes, "1"))
            self.b.attach(src_net, (cap.refdes, "2"))
            # the driver's VS pin rides the same source node
            self.b.attach(src_net, (drv.refdes, "VS"))
        return run

    def _plan_cap_companion(self, role, cap, comp: str, k: int) -> None:
        rid = role["id"]
        if comp == "input_cap":
            def run_in():
                dem = None
                for d in role.get("demands", []) or []:
                    if d["iface"] == "power":
                        dem = d
                if dem is None:
                    raise ModellerError(
                        "RESOLVE_COMPANION_UNANCHORED",
                        "input_cap companion on %r: the role has no power "
                        "demand to decouple" % rid)
                res = self.dv.resolutions.get(dem["id"], "")
                rail = res.split(":", 1)[1].split(" ")[0]
                c = self.b.generate(_cap_proto(lib.LDO_INPUT_CAP_VALUE),
                                    for_demand=rid,
                                    derived="input cap for %s" % rid)
                self.b.attach(self.b.rail_net(rail), (c.refdes, "1"))
                self.b.attach(self._ground_for_role(role), (c.refdes, "2"))
            self._ob("input_cap %s" % rid, run_in)
        elif comp == "output_cap":
            def run_out():
                c = self.b.generate(_cap_proto(lib.LDO_OUTPUT_CAP_VALUE),
                                    for_demand=rid,
                                    derived="output cap for %s" % rid)
                self.b.attach(self.b.rail_net(cap["rail"]), (c.refdes, "1"))
                self.b.attach(self._ground_for_role(role), (c.refdes, "2"))
            self._ob("output_cap %s" % rid, run_out)
        elif comp == "load_cap":
            def run_load():
                part = self._part_with(
                    rid, lambda p: cap["iface"] in p.proto.provide_units)
                if part is None:
                    raise ModellerError(
                        "RESOLVE_COMPANION_UNANCHORED",
                        "load_cap companion on %r: no bound part provides "
                        "%r unit ports" % (rid, cap["iface"]))
                unit = part.proto.provide_units[cap["iface"]][0]
                sig = sorted(unit)[k % len(unit)]
                pf = lib.crystal_load_cap_pf(part.proto.attrs)
                c = self.b.generate(
                    _cap_proto("%gpF" % pf), for_demand=rid,
                    derived="load cap %d for %s: 2*(CL-Cstray) = "
                            "2*(%g-%g) = %gpF" % (
                                k, rid,
                                float(part.proto.attrs["cl_pf"]),
                                float(part.proto.attrs["cstray_pf"]), pf))
                node = (part.refdes, unit[sig])
                net = self.b.node_net.get(node)
                if net is None:
                    net = self.b.signal_net("%s.%s" % (rid, sig))
                    self.b.attach(net, node)
                self.b.attach(net, (c.refdes, "1"))
                self.b.attach(self._ground_for_role(role), (c.refdes, "2"))
            self._ob("load_cap %s#%d" % (rid, k), run_load)
        else:
            raise ModellerError(
                "RESOLVE_UNKNOWN_COMPANION",
                "capability on %r declares companion %r, which the library "
                "does not define" % (rid, comp))

    # -- bus companions -----------------------------------------------------
    def _plan_bus(self, bus: Dict[str, Any]) -> None:
        for comp in (bus.get("attrs", {}) or {}).get("companions", []):
            if comp != "i2c_pullup_pair":
                raise ModellerError(
                    "RESOLVE_UNKNOWN_COMPANION",
                    "bus %r declares companion %r, which the library does "
                    "not define" % (bus["name"], comp))
            self._ob("pullups %s" % bus["name"], self._mk_pullups(bus))

    def _mk_pullups(self, bus):
        def run():
            bname = bus["name"]
            attached = sorted(
                did for did, (r, d) in self.dv.demand_owner.items()
                if d.get("bus") == bname)
            if not attached:
                raise ModellerError(
                    "RESOLVE_COMPANION_UNANCHORED",
                    "bus %r declares pull-ups but no demand attaches to it"
                    % bname)
            # the pull-up rail is the bus's logic level: the common rail the
            # attached devices' power demands resolved to (never a guess).
            rails = set()
            for did in attached:
                r, _d = self.dv.demand_owner[did]
                for d2 in r.get("demands", []) or []:
                    if d2["iface"] != "power":
                        continue
                    res = self.dv.resolutions.get(d2["id"], "")
                    if res.startswith("rail:"):
                        rails.add(res.split(":", 1)[1].split(" ")[0])
            if len(rails) != 1:
                raise ModellerError(
                    "RESOLVE_COMPANION_UNANCHORED",
                    "bus %r pull-up rail is not derivable: attached "
                    "devices resolve to rails %s (need exactly one common "
                    "logic rail)" % (bname, sorted(rails) or "none"))
            rail = rails.pop()
            speed = (bus.get("attrs", {}) or {}).get(
                "i2c_speed", lib.I2C_SPEED_DEFAULT)
            if speed not in lib.I2C_PULLUP_OHMS:
                raise ModellerError(
                    "RESOLVE_BAD_LEVER",
                    "bus %r declares i2c_speed=%r; known speeds: %s"
                    % (bname, speed, sorted(lib.I2C_PULLUP_OHMS)))
            ohms = lib.I2C_PULLUP_OHMS[speed]
            declared = "i2c_speed" in (bus.get("attrs", {}) or {})
            derived = ("pull-up %s for i2c_speed=%s (%s) on bus %s, rail %s"
                       % (ohms, speed,
                          "author lever" if declared else "library default",
                          bname, rail))
            for member in lib.BUS_MEMBERS.get(bus.get("iface", ""), ()):
                r = self.b.generate(_res_proto(ohms), for_demand=attached[0],
                                    derived=derived)
                self.b.attach(self.b.bus_net(bname, member), (r.refdes, "1"))
                self.b.attach(self.b.rail_net(rail), (r.refdes, "2"))
        return run

    # -- module templates (kind-keyed library realizations) ------------------
    def _plan_templates(self) -> None:
        for role in self.dv.roles:
            if role.get("kind") == "half_bridge_module":
                self._ob("half-bridge %s" % role["id"],
                         self._mk_half_bridge(role))

    def _mk_half_bridge(self, bridge):
        """The library realization of one half-bridge: first declared leg is
        the high side (drain on the bridge's resolved bus rail), second the
        low side (source on the island's power ground); the shared phase
        node joins them. Publishes each leg's SOURCE net for the bootstrap
        companion."""
        bid = bridge["id"]

        def run():
            legs = [r for r in self.dv.roles
                    if r["id"].startswith(bid + ".")
                    and r.get("kind") == "bridge_leg"]
            if len(legs) != 2:
                raise ModellerError(
                    "RESOLVE_TEMPLATE",
                    "half_bridge_module %r needs exactly 2 bridge_leg "
                    "children, found %d" % (bid, len(legs)))
            vbus_rail = None
            for d in bridge.get("demands", []) or []:
                if d["iface"] == "power":
                    res = self.dv.resolutions.get(d["id"], "")
                    if res.startswith("rail:"):
                        vbus_rail = res.split(":", 1)[1].split(" ")[0]
            if vbus_rail is None:
                raise ModellerError(
                    "RESOLVE_TEMPLATE",
                    "half_bridge_module %r has no resolved bus-rail power "
                    "demand" % bid)
            gnd = self.b.ground_net(
                self.dv.nearest_ground(bridge.get("scope", "")))
            phase = self.b.signal_net("PHASE.%s" % bid)
            hs, ls = legs[0], legs[1]
            for leg, d_net, s_net in ((hs, self.b.rail_net(vbus_rail),
                                       phase), (ls, phase, gnd)):
                fet = self._part_with(
                    leg["id"], lambda p: p.proto.kind == "mosfet")
                drv = self._part_with(
                    leg["id"], lambda p: p.proto.logic_fn == "buf")
                if fet is None or drv is None:
                    raise ModellerError(
                        "RESOLVE_TEMPLATE",
                        "bridge_leg %r is not bound to a mosfet + driver "
                        "pair" % leg["id"])
                self.b.attach(d_net, (fet.refdes, "D"))
                self.b.attach(s_net, (fet.refdes, "S"))
                gate = self.b.signal_net("GATE.%s" % leg["id"])
                self.b.attach(gate, (drv.refdes, "OUT"))
                self.b.attach(gate, (fet.refdes, "G"))
                self.b.published["srcnet:%s" % leg["id"]] = s_net
                # driver input joins the leg's drive-source net (published
                # by the interlock synthesis, or the raw command net when no
                # invariant claims this leg).
                self._ob("drive-in %s" % leg["id"],
                         self._mk_drive_in(leg["id"], drv),
                         ready=self._mk_pub_ready("drivesrc:%s" % leg["id"]))
        return run

    def _mk_pub_ready(self, key):
        return lambda: key in self.b.published

    def _mk_drive_in(self, leg_id, drv):
        def run():
            self.b.attach(self.b.published["drivesrc:%s" % leg_id],
                          (drv.refdes, "IN"))
        return run

    # -- interlock synthesis (#8) --------------------------------------------
    def _plan_invariants(self) -> None:
        covered: set = set()
        for inv in self.dv.invariants:
            if inv.get("kind") != "mutual_exclusion":
                raise ModellerError(
                    "RESOLVE_UNKNOWN_INVARIANT",
                    "no lowering is defined for invariant kind %r"
                    % inv.get("kind"))
            subj_roles = [self.dv.split_ref(s)[0]
                          for s in inv.get("subjects", [])]
            covered.update(subj_roles)
            self._ob("interlock %s" % "+".join(subj_roles),
                     self._mk_interlock(inv),
                     ready=self._mk_inv_ready(inv))
        # legs never claimed by an invariant drive straight from the command
        for role in self.dv.roles:
            if role.get("kind") != "bridge_leg" or role["id"] in covered:
                continue
            self._ob("passthrough %s" % role["id"],
                     self._mk_passthrough(role),
                     ready=self._mk_cmd_ready(role))

    def _cmd_demand(self, role_id: str) -> str:
        role = self.dv.role_by_id[role_id]
        for d in role.get("demands", []) or []:
            if d["iface"] != "power" and \
                    self.dv.entries_by_demand.get(d["id"]):
                return d["id"]
        raise ModellerError(
            "RESOLVE_BAD_REF",
            "invariant input on role %r matches no allocated command "
            "demand" % role_id)

    def _mk_inv_ready(self, inv):
        def ready():
            try:
                for ref in inv.get("inputs", []):
                    rid, _label = self.dv.split_ref(ref)
                    did = self._cmd_demand(rid)
                    if ("cmdnet:%s#0" % did) not in self.b.published:
                        return False
            except ModellerError:
                return False
            return True
        return ready

    def _mk_cmd_ready(self, role):
        def ready():
            try:
                did = self._cmd_demand(role["id"])
            except ModellerError:
                return False
            return ("cmdnet:%s#0" % did) in self.b.published
        return ready

    def _mk_passthrough(self, role):
        def run():
            did = self._cmd_demand(role["id"])
            self.b.published["drivesrc:%s" % role["id"]] = \
                self.b.published["cmdnet:%s#0" % did]
        return run

    def _signal_node(self, role_id: str, label: str) -> Tuple[str, str]:
        """Lower an abstract signal label of a role (M1 deferral closed at
        the netlist layer too): a label is either a demand of the role
        (-> the allocated provider's command node) or a declared signal of
        one of its bound parts (-> that pin)."""
        did = "%s.%s" % (role_id, label)
        if did in self.dv.demand_owner:
            node = self.b.published.get("cmdnode:%s#0" % did)
            if node is None:
                raise ModellerError(
                    "RESOLVE_BAD_REF",
                    "signal %r of role %r is a demand with no lowered "
                    "command node" % (label, role_id))
            return node
        for part in self._role_parts(role_id):
            if label in part.proto.signals:
                return (part.refdes, part.proto.signals[label])
        raise ModellerError(
            "RESOLVE_UNKNOWN_SIGNAL",
            "signal label %r resolves to neither a demand of role %r nor "
            "a declared signal of its bound part(s)" % (label, role_id))

    def _mk_interlock(self, inv):
        """Cross-inhibit synthesis: for guarded pair (A, B) with commands
        (IN_A, IN_B): drive_A = AND(IN_A, NOT(IN_B)) and symmetrically —
        even the (1,1) command yields never-both-high gate drives. The
        lowered Invariant anchors on the physical gate nodes the drivers
        DRIVE (reachable from the command inputs through these real gates:
        non-vacuous by construction)."""
        def run():
            subjects = [self.dv.split_ref(s)
                        for s in inv.get("subjects", [])]
            inputs = [self.dv.split_ref(s) for s in inv.get("inputs", [])]
            in_by_role = {rid: label for rid, label in inputs}
            if len(subjects) != 2 or set(in_by_role) != \
                    {rid for rid, _ in subjects}:
                raise ModellerError(
                    "RESOLVE_TEMPLATE",
                    "mutual_exclusion over %s needs one command input per "
                    "guarded role" % [s for s in inv.get("subjects", [])])
            # the deepest common ancestor role carries the declaration
            segsets = [rid.split(".") for rid, _ in subjects]
            common = []
            for a, bseg in zip(*segsets):
                if a != bseg:
                    break
                common.append(a)
            anchor_role = ".".join(common)
            if anchor_role not in self.dv.role_by_id:
                anchor_role = subjects[0][0]
            cmd_net = {}
            cmd_node = {}
            for rid, _label in subjects:
                did = self._cmd_demand(rid)
                cmd_net[rid] = self.b.published["cmdnet:%s#0" % did]
                cmd_node[rid] = self.b.published["cmdnode:%s#0" % did]
            for k, (rid, _label) in enumerate(subjects):
                other = subjects[1 - k][0]
                ngate = self.b.generate(
                    _gate_proto("not", "SN74LVC1G04"),
                    for_demand=anchor_role,
                    derived="interlock NOT for %s" % rid)
                agate = self.b.generate(
                    _gate_proto("and", "SN74LVC1G08"),
                    for_demand=anchor_role,
                    derived="interlock AND for %s" % rid)
                self.b.attach(cmd_net[other], (ngate.refdes, "A"))
                ncmd = self.b.signal_net("NCMD.%s" % rid)
                self.b.attach(ncmd, (ngate.refdes, "Y"))
                self.b.attach(cmd_net[rid], (agate.refdes, "A"))
                self.b.attach(ncmd, (agate.refdes, "B"))
                drv = self.b.signal_net("DRV.%s" % rid)
                self.b.attach(drv, (agate.refdes, "Y"))
                self.b.published["drivesrc:%s" % rid] = drv
            # lower the declaration onto the physical gate nodes
            self._ob("lower-invariant %s" % anchor_role,
                     self._mk_lower_inv(inv, subjects, cmd_node),
                     ready=self._mk_lower_ready(subjects))
        return run

    def _mk_lower_ready(self, subjects):
        def ready():
            try:
                for rid, label in subjects:
                    self._signal_node(rid, label)
            except ModellerError:
                return False
            return True
        return ready

    def _mk_lower_inv(self, inv, subjects, cmd_node):
        def run():
            a = self._signal_node(subjects[0][0], subjects[0][1])
            b = self._signal_node(subjects[1][0], subjects[1][1])
            self.b.lowered_invariants.append({
                "kind": "mutual_exclusion",
                "a": [a[0], a[1]],
                "b": [b[0], b[1]],
                "inputs": [[n[0], n[1]] for n in
                           (cmd_node[subjects[0][0]],
                            cmd_node[subjects[1][0]])],
            })
        return run

    # -- realization edges (multi-part role internal topology, M3) -----------
    def _plan_realization_edges(self) -> None:
        """Wire every declared internal edge of every bound realization.
        Planned LAST so demand/bus/rail wiring lands first: an edge joins
        the net one of its nodes already sits on (a divider tapping an
        allocated port, a pull-up joining a bus wire). Two nodes already on
        two DIFFERENT nets is a structured load error — a realization edge
        never silently merges nets."""
        for rid, hint, nodes in self.realization_edges:
            self._ob("edge %s %s" % (rid, hint),
                     self._mk_edge(rid, hint, nodes))

    def _mk_edge(self, rid, hint, nodes):
        def run():
            existing = sorted({self.b.node_net[n] for n in nodes
                               if n in self.b.node_net})
            if len(existing) > 1:
                raise ModellerError(
                    "RESOLVE_NODE_CONFLICT",
                    "realization edge %r of role %r spans nets %s — an "
                    "internal edge may extend one net, never merge several"
                    % (hint, rid, existing))
            net = existing[0] if existing \
                else self.b.signal_net("%s.%s" % (rid, hint))
            for n in nodes:
                if self.b.node_net.get(n) != net:
                    self.b.attach(net, n)
        return run

    # -- pool spare policy ----------------------------------------------------
    def _plan_spares(self) -> None:
        for pname in sorted(self.dv.pools):
            pool = self.dv.pools[pname]
            policy = (pool.get("attrs", {}) or {}).get("spare_handling")
            used = {e["unit"] for e in self.dv.entries
                    if e["pool"] == pname}
            spares = [u for u in range(int(pool["unit_count"]))
                      if u not in used]
            if not spares:
                continue
            if policy is None:
                continue    # visible symbolic spare capacity, no wiring owed
            if policy != "tie_inputs":
                raise ModellerError(
                    "RESOLVE_BAD_LEVER",
                    "pool %r declares spare_handling=%r; the library "
                    "defines only 'tie_inputs'" % (pname, policy))
            self._ob("spares %s" % pname,
                     self._mk_tie_spares(pool, spares))

    def _mk_tie_spares(self, pool, spares):
        def run():
            owner_role = self.dv.role_by_id[pool["role"]]
            owner = self._part_with(
                pool["role"],
                lambda p: pool["provides"] in p.proto.pool_units)
            if owner is None:
                raise ModellerError(
                    "RESOLVE_NO_PIN",
                    "pool %r declares a spare policy but its owner has no "
                    "bound part" % pool["name"])
            in_sigs = [s.split(":", 1)[0]
                       for s in pool.get("port_signature", [])
                       if s.endswith(":in")]
            gnd = self.b.ground_net(
                self.dv.nearest_ground(owner_role.get("scope", "")))
            for u in spares:
                unit = owner.proto.pool_units[pool["provides"]][u]
                for sig in in_sigs:
                    self.b.attach(gnd, (owner.refdes, unit[sig]))
        return run

    # -- final structural checks ----------------------------------------------
    def _check_generated_legs(self) -> None:
        for c in self.b.components:
            if c["authored"]:
                continue
            dangling = [t["name"] for t in c["terminals"]
                        if (c["refdes"], t["name"]) not in self.b.node_net]
            if dangling:
                raise ModellerError(
                    "RESOLVE_DANGLING_LEG",
                    "generated component %s (%s, for %s) has unwired "
                    "pin(s) %s — every terminal of every generated part "
                    "must be connected" % (
                        c["refdes"], c["value"],
                        c["attrs"].get("for_demand"), dangling))

    def _check_mandatory_pins(self) -> None:
        for c in self.b.components:
            for t in c["terminals"]:
                if t["role"] in ("power_in", "ground") and \
                        (c["refdes"], t["name"]) not in self.b.node_net:
                    raise ModellerError(
                        "RESOLVE_DANGLING_LEG",
                        "%s.%s (%s) is mandatory but landed on no net"
                        % (c["refdes"], t["name"], t["role"]))

    # -- connector-pinout rows (M4: REAL lockable record rows) -----------------
    def _connector_rows(self) -> List[Dict[str, Any]]:
        """The external-interface decision rows: for every bound connector
        component, pin -> landed net (None == deliberately unwired position).
        These are the ICD decisions the ``connector_pinout`` decision class
        covers; a freeze of a group covering that class stamps THESE rows
        (M3 carry-item: the freeze covers actual rows, never an
        empty-snapshot handshake)."""
        rows: List[Dict[str, Any]] = []
        for c in self.b.components:
            if c.get("kind") != "connector":
                continue
            for t in c["terminals"]:
                rows.append({
                    "connector": c["refdes"],
                    "pin": t["name"],
                    "function": t.get("function", ""),
                    "net": self.b.node_net.get((c["refdes"], t["name"])),
                    "locked_by": None,
                })
        rows.sort(key=lambda r: (r["connector"], r["pin"]))
        return rows

    # -- freeze (netlist emit is a sync point) ---------------------------------
    def _fire_freeze(self, allocation: Dict[str, Any],
                     connector_rows: List[Dict[str, Any]]) -> None:
        for gname in self.freeze:
            group = next(g for g in allocation["lock_groups"]
                         if g["name"] == gname)
            group["version"] = int(group.get("version", 0)) + 1
            tag = "%s@%d" % (gname, group["version"])
            if "pool_allocation" in group.get("covers", []):
                for e in allocation["entries"]:
                    e["state"] = "pinned"
                    e["locked_by"] = tag
                group["snapshot"] = [
                    dict(e) for e in sorted(
                        allocation["entries"],
                        key=lambda e: (e["pool"], e["unit"], e["demand"]))]
            else:
                # non-allocation decision classes never materialize in the
                # ALLOCATION record (the read-only harness's documented
                # stance); their rows live in the record artifacts instead.
                group["snapshot"] = []
            if "connector_pinout" in group.get("covers", []):
                if not connector_rows:
                    raise ModellerError(
                        "FREEZE_NO_ROWS",
                        "lock group %r covers connector_pinout but the bound "
                        "design has no connector rows — an external-interface "
                        "freeze over zero rows is the empty-snapshot "
                        "handshake this build rejects" % gname)
                for row in connector_rows:
                    row["locked_by"] = tag

    # -- top level ---------------------------------------------------------------
    def run(self) -> ResolveResult:
        self._gate()
        self._bind_roles()
        for role in self.dv.roles:
            self._plan_role(role)
        for bname in sorted(self.dv.buses):
            self._plan_bus(self.dv.buses[bname])
        self._plan_templates()
        self._plan_invariants()
        self._plan_spares()
        self._plan_realization_edges()
        self._run_fixpoint()
        self._check_generated_legs()
        self._check_mandatory_pins()

        # ---- emit: the layer-2 graph -------------------------------------
        graph = {
            "components": self.b.components,
            "nets": [self._net_json(self.b.nets[n])
                     for n in self.b.net_order],
            "escalations": ["%s: %s" % (e.get("code", "ESCALATE"),
                                        e.get("msg", ""))
                            for e in self.dv.escalations],
            "invariants": list(self.b.lowered_invariants),
        }

        # ---- emit: the allocation artifact (freeze fires HERE) ------------
        connector_rows = self._connector_rows()
        l1_out = copy.deepcopy(self.dv.doc)
        allocation = l1_out.setdefault("allocation", {})
        allocation.setdefault("entries", [])
        allocation.setdefault("lock_groups", [])
        self._fire_freeze(allocation, connector_rows)
        allocation["entries"] = sorted(
            allocation["entries"],
            key=lambda e: (e["pool"], e["unit"], e["demand"]))
        allocation["lock_groups"] = sorted(
            allocation["lock_groups"], key=lambda g: g["name"])

        bindings = {}
        bound_parts = {}
        for rid, parts in sorted(self.b.parts_of_role.items()):
            bound_parts[rid] = [
                {"refdes": p.refdes, "part": p.comp["value"],
                 "chosen_by": self.bind_provenance.get(rid, "solver")}
                for p in parts]
            if len(parts) == 1:
                bindings[rid] = parts[0].refdes

        # allocation-wiring view (pin-map enrichment): where each entry
        # LANDED — node + net, canonical order, deterministic. PERSISTED in
        # the alloc artifact (M3 carry-item: the pin-map rebuilds
        # byte-identically from on-disk inputs, like bom/records).
        alloc_wiring = []
        for (pool, unit, demand) in sorted(self.alloc_nodes):
            node = self.alloc_nodes[(pool, unit, demand)]
            alloc_wiring.append({
                "pool": pool, "unit": unit, "demand": demand,
                "node": "%s.%s" % node,
                "net": self.b.node_net.get(node),
            })

        alloc = {
            "series": self.dv.series,
            "solver_version": allocation.get("solver_version", ""),
            "stamp": {"series": self.dv.series,
                      "locks": {g["name"]: g.get("version", 0)
                                for g in allocation["lock_groups"]}},
            "allocation": allocation,
            "bindings": bindings,
            "bound_parts": bound_parts,
            "wiring": alloc_wiring,
            "connector_pinout": connector_rows,
        }
        return ResolveResult(name=self.name, l1=l1_out, graph=graph,
                             alloc=alloc, alloc_wiring=alloc_wiring)

    @staticmethod
    def _net_json(net: Dict[str, Any]) -> Dict[str, Any]:
        d = {"name": net["name"], "kind": net["kind"],
             "voltage": net["voltage"], "nodes": list(net["nodes"])}
        for opt in ("ground_kind", "ground_role", "bond"):
            if net.get(opt) is not None:
                d[opt] = net[opt]
        return d


# ---------------------------------------------------------------------------
# Generated-part prototypes (companion vocabulary)
# ---------------------------------------------------------------------------

def _cap_proto(value: str) -> lib.PartProto:
    return lib.PartProto(
        value=value, kind="capacitor", prefix="C",
        terminals=[lib.term("1", "passive"), lib.term("2", "passive")])


def _res_proto(value: str) -> lib.PartProto:
    return lib.PartProto(
        value=value, kind="resistor", prefix="R",
        terminals=[lib.term("1", "passive"), lib.term("2", "passive")])


def _diode_proto(value: str) -> lib.PartProto:
    return lib.PartProto(
        value=value, kind="diode", prefix="D",
        terminals=[lib.term("A", "passive"), lib.term("K", "passive")])


def _gate_proto(fn: str, value: str) -> lib.PartProto:
    if fn == "not":
        terms = [lib.term("A", "logic_in"), lib.term("Y", "logic_out")]
    else:
        terms = [lib.term("A", "logic_in"), lib.term("B", "logic_in"),
                 lib.term("Y", "logic_out")]
    return lib.PartProto(value=value, kind="logic_gate", logic_fn=fn,
                         prefix="U", terminals=terms)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def resolve(l1_doc: Dict[str, Any], name: str,
            bindings: Sequence[Tuple[str, str]] = (),
            freeze: Sequence[str] = ()) -> ResolveResult:
    """Resolve one layer-1 document (the emitted JSON dict — the model IR)
    into its bound layer-2 netlist + allocation artifact. ``bindings`` are
    the layer-2 refinement's role->part binds (author policy; the solver
    binds every other bindable role); ``freeze`` names lock groups fired at
    this netlist emit. Deterministic: two calls on the same inputs produce
    byte-identical artifacts."""
    return _Resolver(l1_doc, name, bindings, freeze).run()


__all__ = ["resolve", "ResolveResult"]
