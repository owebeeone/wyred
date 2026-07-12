"""The elaboration engine: declarations -> resolution ladder -> layer-1 JSON.

The engine is the generic half of the (Δ, C) bargain: authors declare intent
(roles, demands, rails, pools, invariants); the ENGINE walks the composition
tree, mints identities, cascades scopes, runs the resolution ladder for every
demand, allocates pool units deterministically, generates rung-4 escalations
with UNSAT-core explanations, and emits the layer-1 document per
rl/harness/EMIT_CONTRACT.md. One engine, all intents — per-intent lookups are
Δ=0 and do not exist here.

The resolution ladder (Gen4 section 2.4), implemented in ``_resolve_signal``
and ``_resolve_power``:

  rung 1  unique match            -> wire (concept-verified, recorded)
  rung 2  unmet, no default       -> structured load-error diagnostic
  rung 3  ambiguous WITHIN a pool -> ALLOCATION (deterministic, sticky,
                                     recorded, pinnable)
  rung 4  ambiguous across
          non-equivalent classes  -> ESCALATE with conflict + relaxation
                                     (a declared default is the author's
                                     policy and resolves it instead)

No silent defaults anywhere: every resolution is recorded in the emitted
document (``attrs.resolutions``), every failure is a structured Diagnostic
(still emitted — a layer-1 document is valid and scoreable standalone, and
intent-level inconsistencies like an address collision are the ORACLE's
verdict to give), and API misuse (an equivalence-violating pin, an unknown
decision class, a dotted rail name) raises a structured ModellerError before
anything is emitted.

Determinism: the walk order is declaration order, the solver is canonically
tie-broken (demands sorted by minted id, lowest free unit first, author pins
honored before the solver runs), and the allocation record serializes sorted
by (pool, unit, demand) — two elaborations of one document are byte-identical.

Pure Python 3 stdlib. No harness imports — the emitted JSON is the contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Type

from .concepts import Concept, signature_for
from .core import (
    BondDecl, BusDecl, ChildDecl, DemandDecl, GroundDecl, LockDecl,
    ModellerError, Module, MutexDecl, ParamDecl, PinOp, BindOp, PoolDecl,
    ProvideDecl, RailDecl, DECISION_CLASSES, MODULES, resolve_value,
    validate_rail_name, use,
    NearDecl, KeepoutDecl, EdgeDecl, ThermalDecl, SeparationDecl,
    PLACEMENT_SIDES,
    ExpectRailDecl, ExpectI2cScanDecl, ExpectCurrentDecl, ExpectSignalDecl,
)

SOLVER_VERSION = "ga019-trivial-1"


# ---------------------------------------------------------------------------
# Harvested facts (the model IR the ladder and the emitter share)
# ---------------------------------------------------------------------------

@dataclass
class CapFact:
    iface: str
    volts: Optional[float]
    rail: Optional[str]
    attrs: Dict[str, Any]


@dataclass
class DemFact:
    id: str
    iface: str
    volts: Optional[float]
    bus: Optional[str]
    qty: int
    default: Optional[str]
    attrs: Dict[str, Any]


@dataclass
class RoleFact:
    id: str
    kind: str
    scope: str
    capabilities: List[CapFact] = field(default_factory=list)
    demands: List[DemFact] = field(default_factory=list)


@dataclass
class PoolFact:
    name: str
    role: str
    provides: str
    unit_count: int
    port_signature: Tuple[str, ...]
    scope: str
    attrs: Dict[str, Any]


@dataclass
class Diagnostic:
    """A structured engine finding (severity 'error' blocks nothing at emit
    time — layer-1 docs are scoreable standalone — but the runner surfaces
    every one; nothing is silent)."""

    code: str
    msg: str
    subjects: Tuple[str, ...] = ()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "Diagnostic(%s: %s)" % (self.code, self.msg)


@dataclass
class EmitResult:
    """One elaboration's full output: the layer-1 document (plain JSON-safe
    dict per the emit contract) plus the engine's own records."""

    name: str
    doc: Dict[str, Any]
    diagnostics: List[Diagnostic]
    resolutions: Dict[str, str]
    allocation_entries: List[Dict[str, Any]]
    spares: Dict[str, List[int]]
    bindings: List[Tuple[str, str]]      # (role_id, part) — M2 input only
    test_declarations: List[Dict[str, Any]] = field(default_factory=list)
    # the elaborated ``expect_*`` records (proposal section 3): self-contained
    # authored form (subject + derived nominal/addresses + tolerances), carried
    # HERE and never in ``doc`` — the testplan emitter derives its checks block
    # from these + records + pin-map; the l1 document stays byte-identical.

    def to_json_str(self) -> str:
        return json.dumps(self.doc, indent=2, sort_keys=True) + "\n"


class _Ctx:
    def __init__(self) -> None:
        self.scopes: List[Dict[str, str]] = []       # {"id","parent"}
        self.roles: List[RoleFact] = []
        self.rails: List[Dict[str, Any]] = []        # name volts scope attrs
        self.grounds: List[Dict[str, Any]] = []
        self.bonds: List[Dict[str, Any]] = []
        self.buses: List[Dict[str, Any]] = []
        self.pools: List[PoolFact] = []
        self.invariants: List[Dict[str, Any]] = []
        self.placement: List[Dict[str, Any]] = []
        self.tests: List[Dict[str, Any]] = []
        self.locks: List[Dict[str, Any]] = []

    def bus_named(self, name: str) -> Optional[Dict[str, Any]]:
        for b in self.buses:
            if b["name"] == name:
                return b
        return None

    def rail_named(self, name: str) -> Optional[Dict[str, Any]]:
        for r in self.rails:
            if r["name"] == name:
                return r
        return None

    def pool_named(self, name: str) -> Optional[PoolFact]:
        for p in self.pools:
            if p.name == name:
                return p
        return None

    def scope_chain(self, scope_id: str) -> List[str]:
        parents = {s["id"]: s["parent"] for s in self.scopes}
        chain = [scope_id]
        seen = {scope_id}
        cur = scope_id
        while cur != "":
            cur = parents.get(cur, "")
            if cur in seen:
                break
            chain.append(cur)
            seen.add(cur)
        return chain


# ---------------------------------------------------------------------------
# The walk: instantiate the composition tree, mint identity, harvest facts
# ---------------------------------------------------------------------------

def _attrs_with_companions(attrs: Dict[str, Any],
                           companions: Tuple[str, ...]) -> Dict[str, Any]:
    out = dict(attrs)
    if companions:
        out["companions"] = list(companions)
    return out


def _validate_invariant_ref(owner_cls: Type[Module], decl_name: str,
                            ref: Any) -> None:
    """Compose-time validation of a module-relative invariant signal
    reference (law 5 hardening): in ``"hs.gate"`` declared inside a
    HalfBridge, every path segment BEFORE the final signal label must name
    a declared child of the module it descends through (a dotless reference
    is a local signal label of the declaring module itself). A typo'd child
    name (``"hs2.gate"``, ``"s.nonexistent_sibling.gate"``) is therefore a
    structured load error HERE, at declaration walk time — never a dangling
    reference smuggled into the emitted document for M2 to trip over.

    M2 hardening (the M1 review's outstanding item 2): the FINAL signal
    label is validated too — it must name a declared ``demand`` or
    ``provide`` of the module it lands on, so an abstract signal has a
    declared existence at layer 1 and a lowering target at layer 2
    (``"hs.gate"`` is legal because BridgeLeg declares ``gate =
    provide(...)``; a fabricated ``"hs.blorp"`` is a structured load
    error)."""
    if not isinstance(ref, str) or not ref:
        raise ModellerError(
            "INVARIANT_BAD_REF",
            "invariant %r on %s has a non-string/empty signal reference %r"
            % (decl_name, owner_cls.__name__, ref))
    segs = ref.split(".")
    if any(not s for s in segs):
        raise ModellerError(
            "INVARIANT_BAD_REF",
            "invariant %r on %s reference %r has an empty path segment"
            % (decl_name, owner_cls.__name__, ref))
    cur = owner_cls
    for seg in segs[:-1]:
        kids = {n: cd.module_cls for n, cd in cur._decls
                if isinstance(cd, ChildDecl)}
        if seg not in kids:
            raise ModellerError(
                "INVARIANT_DANGLING_REF",
                "invariant %r on %s references %r, but %r is not a declared "
                "child of %s (declared children: %s) — every path segment "
                "before the final signal label must name a declared child"
                % (decl_name, owner_cls.__name__, ref, seg, cur.__name__,
                   ", ".join(sorted(kids)) or "none"))
        cur = kids[seg]
    label = segs[-1]
    signals = {n for n, cd in cur._decls
               if isinstance(cd, (DemandDecl, ProvideDecl))}
    if label not in signals:
        raise ModellerError(
            "INVARIANT_UNKNOWN_SIGNAL",
            "invariant %r on %s references %r, but %r is not a declared "
            "demand/provide of %s (declared signals: %s) — an abstract "
            "signal label must have a declared existence"
            % (decl_name, owner_cls.__name__, ref, label, cur.__name__,
               ", ".join(sorted(signals)) or "none"))


def _validate_placement_subject(owner_cls: Type[Module], decl_name: str,
                                ref: Any) -> None:
    """Compose-time validation of a placement subject reference (semantics
    section 1: a subject names a ROLE — a module-relative dotted instance
    path). Unlike an invariant signal reference, EVERY path segment names a
    declared child (a role is a module instance, ``use(...)``): ``near("pmu",
    "mcu")`` inside a composite is legal because ``pmu`` / ``mcu`` are
    declared children; ``near("pmu", "ghost")`` is a structured load error
    HERE, at declaration walk time — a placement subject that names no
    declared role in scope never reaches the emitted document (law 10, no
    silent defaults / no dangling references smuggled downstream)."""
    if not isinstance(ref, str) or not ref:
        raise ModellerError(
            "PLACEMENT_BAD_SUBJECT",
            "placement %r on %s has a non-string/empty subject reference %r"
            % (decl_name, owner_cls.__name__, ref))
    segs = ref.split(".")
    if any(not s for s in segs):
        raise ModellerError(
            "PLACEMENT_BAD_SUBJECT",
            "placement %r on %s subject %r has an empty path segment"
            % (decl_name, owner_cls.__name__, ref))
    cur = owner_cls
    for seg in segs:
        kids = {n: cd.module_cls for n, cd in cur._decls
                if isinstance(cd, ChildDecl)}
        if seg not in kids:
            raise ModellerError(
                "PLACEMENT_DANGLING_SUBJECT",
                "placement %r on %s references role %r, but %r is not a "
                "declared child of %s (declared children: %s) — a placement "
                "subject must name a declared role"
                % (decl_name, owner_cls.__name__, ref, seg, cur.__name__,
                   ", ".join(sorted(kids)) or "none"))
        cur = kids[seg]


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _elaborate_test_decl(owner_cls: Type[Module], name: str, minted_id: str,
                         declared_by: Dict[str, Any], d: Any,
                         inst: Module) -> Dict[str, Any]:
    """Elaborate ONE ``expect_*`` marker into its declaration record — the
    self-contained authored form (subject + resolved tolerances). ``late(...)``
    is resolved here (every argument); LOCAL tolerance well-formedness is
    validated here (``TEST_BAD_TOLERANCE`` — RATIFY-1/4/5); rail/bus/demand
    EXISTENCE and derived defaults (nominal, addresses) need the full L1
    vocabulary and so are finalized post-walk in ``_check_consistency``. The
    subject is ABSOLUTE L1 vocabulary — a rail/bus name (design vocabulary) or
    a minted demand-id path — never a module-relative role, so it is resolved
    but NOT namespaced by the declaring instance (unlike placement subjects)."""
    def bad(msg: str) -> "ModellerError":
        return ModellerError(
            "TEST_BAD_TOLERANCE",
            "%s %r on %s %s" % (d.__class__.__name__, name,
                                owner_cls.__name__, msg))

    row: Dict[str, Any] = {"id": minted_id, "declared_by": declared_by}
    if isinstance(d, ExpectRailDecl):
        volts = resolve_value(d.volts, inst)
        tol = resolve_value(d.tol, inst)
        tol_pct = resolve_value(d.tol_pct, inst)
        if (tol is None) == (tol_pct is None):
            raise bad("must declare EXACTLY one of tol / tol_pct "
                      "(RATIFY-1: tolerance is required, never the 0.05 V "
                      "float-noise absorber; got tol=%r tol_pct=%r)"
                      % (tol, tol_pct))
        given = tol if tol is not None else tol_pct
        if not _is_number(given) or given <= 0:
            raise bad("tolerance must be a positive number (got %r)" % given)
        if volts is not None and (not _is_number(volts) or volts <= 0):
            raise bad("volts (nominal) must be a positive number when "
                      "given (got %r)" % volts)
        row.update({"kind": "rail",
                    "subject": resolve_value(d.tp, inst),
                    "volts": volts})
        if tol is not None:
            row["tol"] = tol
        else:
            row["tol_pct"] = tol_pct
    elif isinstance(d, ExpectI2cScanDecl):
        addrs = resolve_value(d.addrs, inst)
        if addrs is not None:
            if not isinstance(addrs, (list, tuple)) or not all(
                    isinstance(a, int) and not isinstance(a, bool)
                    and a >= 0 for a in addrs):
                raise bad("addrs must be a list of non-negative integer "
                          "addresses when given (got %r)" % (addrs,))
            addrs = sorted(set(int(a) for a in addrs))
        row.update({"kind": "i2c_scan",
                    "subject": resolve_value(d.bus, inst),
                    "addrs": addrs})            # None -> derived post-walk
    elif isinstance(d, ExpectCurrentDecl):
        max_ma = resolve_value(d.max_ma, inst)
        state = resolve_value(d.state, inst)
        if not _is_number(max_ma) or max_ma <= 0:
            raise bad("max_ma must be a positive number (RATIFY-4, one-sided "
                      "upper bound; got %r)" % max_ma)
        if not isinstance(state, str) or not state:
            raise bad("state must be a non-empty string (RATIFY-4, the "
                      "operating-state vocabulary; got %r)" % (state,))
        row.update({"kind": "current",
                    "subject": resolve_value(d.rail, inst),
                    "max_ma": max_ma, "state": state})
    elif isinstance(d, ExpectSignalDecl):
        freq = resolve_value(d.freq, inst)
        f_ppm = resolve_value(d.freq_tol_ppm, inst)
        f_pct = resolve_value(d.freq_tol_pct, inst)
        duty = resolve_value(d.duty, inst)
        d_pts = resolve_value(d.duty_tol_pts, inst)
        if freq is None and duty is None:
            raise bad("declares neither freq nor duty — a signal check must "
                      "measure at least one quantity (RATIFY-5)")
        row.update({"kind": "signal", "subject": resolve_value(d.tp, inst)})
        if freq is not None:
            if not _is_number(freq) or freq <= 0:
                raise bad("freq must be a positive number (got %r)" % freq)
            if (f_ppm is None) == (f_pct is None):
                raise bad("freq needs EXACTLY one of freq_tol_ppm / "
                          "freq_tol_pct (RATIFY-5; got ppm=%r pct=%r)"
                          % (f_ppm, f_pct))
            given = f_ppm if f_ppm is not None else f_pct
            if not _is_number(given) or given <= 0:
                raise bad("freq tolerance must be a positive number "
                          "(got %r)" % given)
            row["freq"] = freq
            if f_ppm is not None:
                row["freq_tol_ppm"] = f_ppm
            else:
                row["freq_tol_pct"] = f_pct
        elif f_ppm is not None or f_pct is not None:
            raise bad("declares a freq tolerance without freq (RATIFY-5, no "
                      "cross-quantity defaults)")
        if duty is not None:
            if not _is_number(duty) or not (0 <= duty <= 100):
                raise bad("duty must be a number in [0, 100] percent "
                          "(got %r)" % duty)
            if d_pts is None:
                raise bad("duty needs duty_tol_pts (RATIFY-5; got None)")
            if not _is_number(d_pts) or d_pts <= 0:
                raise bad("duty_tol_pts must be a positive number "
                          "(got %r)" % d_pts)
            row["duty"] = duty
            row["duty_tol_pts"] = d_pts
        elif d_pts is not None:
            raise bad("declares duty_tol_pts without duty (RATIFY-5, no "
                      "cross-quantity defaults)")
    else:                                       # pragma: no cover - defensive
        raise ModellerError("UNKNOWN_DECL",
                            "unhandled test declaration %r" % (d,))
    return row


def _walk(inst: Module, path: str, enclosing_scope: str, ctx: _Ctx) -> None:
    cls = type(inst)
    decls = cls._decls

    opens_scope = any(isinstance(d, (RailDecl, GroundDecl, BusDecl))
                      for _, d in decls)
    my_scope = enclosing_scope
    if opens_scope and path:
        ctx.scopes.append({"id": path, "parent": enclosing_scope})
        my_scope = path

    caps: List[CapFact] = []
    dems: List[DemFact] = []
    children: List[Tuple[str, ChildDecl]] = []

    for name, d in decls:
        if isinstance(d, ParamDecl):
            continue
        if isinstance(d, ChildDecl):
            children.append((name, d))
            continue
        if isinstance(d, DemandDecl):
            if not path:
                raise ModellerError(
                    "ROOT_DEMAND",
                    "the intent root %r declares demand %r directly; demands "
                    "live on roles — wrap it in a child module"
                    % (cls.__name__, name))
            volts = resolve_value(d.volts, inst)
            dems.append(DemFact(
                id="%s.%s" % (path, name),
                iface=d.iface,
                volts=None if volts is None else float(volts),
                bus=resolve_value(d.bus, inst),
                qty=int(resolve_value(d.qty, inst)),
                default=resolve_value(d.default, inst),
                attrs=_attrs_with_companions(
                    resolve_value(d.attrs, inst), d.companions)))
            continue
        if isinstance(d, ProvideDecl):
            volts = resolve_value(d.volts, inst)
            caps.append(CapFact(
                iface=d.iface,
                volts=None if volts is None else float(volts),
                rail=resolve_value(d.rail, inst),
                attrs=_attrs_with_companions(
                    resolve_value(d.attrs, inst), d.companions)))
            continue
        if isinstance(d, RailDecl):
            rname = resolve_value(d.name, inst)
            validate_rail_name(rname)
            if any(r["name"] == rname and r["scope"] == my_scope
                   for r in ctx.rails):
                raise ModellerError(
                    "DUPLICATE_RAIL",
                    "rail %r declared twice in scope %r" % (rname, my_scope))
            ctx.rails.append({
                "name": rname,
                "volts": float(resolve_value(d.volts, inst)),
                "scope": my_scope,
                "attrs": resolve_value(d.attrs, inst)})
            continue
        if isinstance(d, GroundDecl):
            gname = resolve_value(d.name, inst)
            if any(g["name"] == gname for g in ctx.grounds):
                raise ModellerError(
                    "DUPLICATE_GROUND", "ground %r declared twice" % gname)
            ctx.grounds.append({
                "name": gname,
                "kind": resolve_value(d.kind, inst),
                "role": resolve_value(d.role, inst),
                "scope": my_scope, "attrs": resolve_value(d.attrs, inst)})
            continue
        if isinstance(d, BondDecl):
            ctx.bonds.append({
                "name": resolve_value(d.name, inst),
                "joins": [resolve_value(j, inst) for j in d.joins],
                "attrs": resolve_value(d.attrs, inst)})
            continue
        if isinstance(d, BusDecl):
            bname = resolve_value(d.name, inst)
            if ctx.bus_named(bname) is not None:
                raise ModellerError(
                    "DUPLICATE_BUS", "bus %r declared twice" % bname)
            ctx.buses.append({
                "name": bname, "iface": resolve_value(d.iface, inst),
                "scope": my_scope,
                "attrs": _attrs_with_companions(
                    resolve_value(d.attrs, inst), d.companions)})
            continue
        if isinstance(d, PoolDecl):
            if not path:
                raise ModellerError(
                    "ROOT_POOL",
                    "the intent root %r declares pool %r directly; pools "
                    "live on roles" % (cls.__name__, name))
            ports = d.ports if d.ports is not None else \
                signature_for(d.provides)
            if ports is None:
                raise ModellerError(
                    "POOL_NO_SIGNATURE",
                    "pool %r provides %r, which has no canonical port "
                    "signature — declare ports=(...) explicitly (a unit "
                    "without a typed port set cannot swap)"
                    % (name, d.provides))
            ctx.pools.append(PoolFact(
                name="%s.%s" % (path, name),
                role=path,
                provides=d.provides,
                unit_count=int(resolve_value(d.units, inst)),
                port_signature=tuple(ports),
                scope=my_scope,
                attrs=resolve_value(d.attrs, inst)))
            continue
        if isinstance(d, MutexDecl):
            subjects = tuple(resolve_value(s, inst) for s in d.subjects)
            inputs = tuple(resolve_value(s, inst) for s in d.inputs)
            for ref in subjects + inputs:
                _validate_invariant_ref(cls, name, ref)
            prefix = path + "." if path else ""
            ctx.invariants.append({
                "kind": "mutual_exclusion",
                "subjects": [prefix + s for s in subjects],
                "inputs": [prefix + s for s in inputs],
                "attrs": resolve_value(d.attrs, inst)})
            continue
        if isinstance(d, (NearDecl, KeepoutDecl, EdgeDecl, ThermalDecl,
                          SeparationDecl)):
            # minted id + subject namespacing follow the mutual_exclusion
            # precedent (semantics section 2): id = <instance path>.<attr>,
            # role subjects prefixed by the instance path — declare once,
            # mint one per instantiation. late() resolves inside params.
            prefix = path + "." if path else ""
            minted_id = prefix + name
            declared_by = {"module": cls.__name__, "instance": path}
            if isinstance(d, NearDecl):
                for ref in d.subjects:
                    _validate_placement_subject(cls, name, ref)
                row = {
                    "id": minted_id, "kind": "near",
                    "declared_by": declared_by,
                    "subjects": [prefix + s for s in d.subjects],
                    "params": {"max_mm": resolve_value(d.max_mm, inst)}}
            elif isinstance(d, KeepoutDecl):
                for ref in d.roles:
                    _validate_placement_subject(cls, name, ref)
                row = {
                    "id": minted_id, "kind": "keepout",
                    "declared_by": declared_by,
                    "subjects": [prefix + s for s in d.roles],
                    "params": {"zone": resolve_value(d.zone, inst)}}
            elif isinstance(d, EdgeDecl):
                _validate_placement_subject(cls, name, d.connector)
                side = resolve_value(d.side, inst)
                if side not in PLACEMENT_SIDES:
                    raise ModellerError(
                        "PLACEMENT_BAD_SIDE",
                        "edge %r on %s names side %r, which is not one of %s "
                        "— the side vocabulary is a closed enum (a lock that "
                        "silently protects nothing is rejected the same way)"
                        % (name, cls.__name__, side, sorted(PLACEMENT_SIDES)))
                row = {
                    "id": minted_id, "kind": "edge",
                    "declared_by": declared_by,
                    "subjects": [prefix + d.connector],
                    "params": {"side": side,
                               "tol_mm": resolve_value(d.tol_mm, inst)}}
            elif isinstance(d, ThermalDecl):
                _validate_placement_subject(cls, name, d.role)
                row = {
                    "id": minted_id, "kind": "thermal",
                    "declared_by": declared_by,
                    "subjects": [prefix + d.role],
                    "params": {"copper_mm2":
                               resolve_value(d.copper_mm2, inst)}}
            else:                                       # SeparationDecl
                # net-class NAMES (declared rails/grounds), NOT namespaced
                # role ids — design vocabulary. Existence is validated against
                # the declared rail/ground set AFTER the walk (only then is
                # the full vocabulary known); see _check_consistency.
                row = {
                    "id": minted_id, "kind": "separation",
                    "declared_by": declared_by,
                    "subjects": [resolve_value(d.class_a, inst),
                                 resolve_value(d.class_b, inst)],
                    "params": {"min_mm": resolve_value(d.min_mm, inst)}}
            ctx.placement.append(row)
            continue
        if isinstance(d, (ExpectRailDecl, ExpectI2cScanDecl,
                          ExpectCurrentDecl, ExpectSignalDecl)):
            # minted id + declared_by follow the mutual_exclusion / placement
            # precedent (id = <instance path>.<attr>, law 8). Subjects are
            # ABSOLUTE L1 vocabulary, resolved but NOT namespaced. Vocabulary
            # existence + derived defaults are finalized in _check_consistency
            # (only there is the full rail/bus/demand set known); the test
            # records are carried on the EmitResult, NEVER emitted into the
            # layer-1 document (proposal section 3: l1.json byte-identical, no
            # schema_l1 change).
            prefix = path + "." if path else ""
            minted_id = prefix + name
            declared_by = {"module": cls.__name__, "instance": path}
            ctx.tests.append(_elaborate_test_decl(
                cls, name, minted_id, declared_by, d, inst))
            continue
        if isinstance(d, LockDecl):
            bad = sorted(set(d.covers) - DECISION_CLASSES)
            if bad:
                raise ModellerError(
                    "LOCK_UNKNOWN_CLASS",
                    "lock group %r covers unknown decision class(es) %s "
                    "(known: %s) — a lock that silently protects nothing is "
                    "rejected" % (d.name, bad, sorted(DECISION_CLASSES)))
            if any(g["name"] == d.name for g in ctx.locks):
                raise ModellerError(
                    "DUPLICATE_LOCK",
                    "lock group %r declared twice" % d.name)
            ctx.locks.append({
                "name": d.name, "covers": list(d.covers),
                "owner": d.owner, "sync_point": d.sync_point})
            continue
        raise ModellerError(   # pragma: no cover - defensive
            "UNKNOWN_DECL", "unhandled declaration %r" % (d,))

    if path:
        ctx.roles.append(RoleFact(
            id=path, kind=cls.module_kind(), scope=my_scope,
            capabilities=caps, demands=dems))

    for name, d in children:
        overrides = {k: resolve_value(v, inst) for k, v in d.overrides.items()}
        child = d.module_cls(**overrides)
        child_path = "%s.%s" % (path, name) if path else name
        _walk(child, child_path, my_scope, ctx)


# ---------------------------------------------------------------------------
# Static consistency diagnostics (engine-side mirrors of intent-level checks:
# statically checkable from the same facts the document will carry)
# ---------------------------------------------------------------------------

def _check_consistency(ctx: _Ctx, diags: List[Diagnostic]) -> None:
    # authored bonds must join declared grounds
    gnames = {g["name"] for g in ctx.grounds}
    for b in ctx.bonds:
        missing = sorted(set(b["joins"]) - gnames)
        if missing:
            raise ModellerError(
                "BOND_DANGLING",
                "bond %r joins undeclared ground(s) %s" % (b["name"], missing))
    # separation net classes must name a DECLARED rail or ground (semantics
    # section 3: net classes are design vocabulary, validated here where the
    # full rail/ground set is known — a name matching neither is a
    # compose-time load error, never a silently unverifiable constraint).
    netclasses = ({r["name"] for r in ctx.rails}
                  | {g["name"] for g in ctx.grounds})
    for row in ctx.placement:
        if row["kind"] != "separation":
            continue
        for cls_name in row["subjects"]:
            if cls_name not in netclasses:
                raise ModellerError(
                    "PLACEMENT_UNKNOWN_NETCLASS",
                    "separation %r names net class %r, which is neither a "
                    "declared rail nor a declared ground (declared: %s) — a "
                    "separation class must name the L1 rail/ground vocabulary"
                    % (row["id"], cls_name,
                       ", ".join(sorted(netclasses)) or "none"))
    # test declarations: finalize the ``expect_*`` records now the full L1
    # vocabulary is known (proposal sections 1-4). Reference EXISTENCE is a
    # structured load error (law 10, TEST_UNKNOWN_*); derived defaults —
    # nominal voltage (RATIFY-2) and expected addresses (RATIFY-6) — are baked
    # into the self-contained declaration here (the testplan emitter never sees
    # the L1 doc), derived numbers canonicalized as round(x, 6) (RATIFY-7).
    rail_names = {r["name"] for r in ctx.rails}
    rail_volts = {r["name"]: r["volts"] for r in ctx.rails}
    bus_names = {b["name"] for b in ctx.buses}
    demand_ids = {dem.id for role in ctx.roles for dem in role.demands}
    bus_addrs: Dict[str, List[int]] = {}
    for role in ctx.roles:
        for dem in role.demands:
            if dem.bus and "i2c_addr" in dem.attrs:
                a = dem.attrs["i2c_addr"]
                if isinstance(a, int) and not isinstance(a, bool):
                    bus_addrs.setdefault(dem.bus, []).append(int(a))
    for row in ctx.tests:
        kind, subject, rid = row["kind"], row["subject"], row["id"]
        if kind in ("rail", "current"):
            if subject not in rail_names:
                raise ModellerError(
                    "TEST_UNKNOWN_RAIL",
                    "%s check %r names rail %r, which is not a declared rail "
                    "(declared: %s)" % (kind, rid, subject,
                                        ", ".join(sorted(rail_names)) or "none"))
        elif kind == "i2c_scan":
            if subject not in bus_names:
                raise ModellerError(
                    "TEST_UNKNOWN_BUS",
                    "i2c_scan check %r names bus %r, which is not a declared "
                    "bus (declared: %s)" % (rid, subject,
                                            ", ".join(sorted(bus_names))
                                            or "none"))
        elif kind == "signal":
            if subject not in demand_ids:
                raise ModellerError(
                    "TEST_UNKNOWN_DEMAND",
                    "signal check %r names demand %r, which is not a declared "
                    "demand id (a minted instance path); declared: %s"
                    % (rid, subject, ", ".join(sorted(demand_ids)) or "none"))
        if kind == "rail":
            volts = row.pop("volts")
            if volts is None:                   # RATIFY-2: derive one-way
                row["nominal"] = round(float(rail_volts[subject]), 6)
                row["nominal_source"] = "derived"
            else:
                row["nominal"] = volts
                row["nominal_source"] = "authored"
        elif kind == "i2c_scan":
            if row["addrs"] is None:            # RATIFY-6: derive the set
                row["addrs"] = sorted(set(bus_addrs.get(subject, [])))
                row["addrs_source"] = "derived"
            else:
                row["addrs_source"] = "authored"
    # rail-tree consistency: a capability driving a rail must drive a
    # DECLARED rail at ITS voltage
    for role in ctx.roles:
        for cap in role.capabilities:
            if cap.iface != "power" or cap.rail is None:
                continue
            r = ctx.rail_named(cap.rail)
            if r is None:
                diags.append(Diagnostic(
                    "RAIL_SCOPE",
                    "role %r drives rail %r, which is not declared"
                    % (role.id, cap.rail), (role.id,)))
            elif cap.volts is not None:
                chk = Concept("power", cap.volts).accepts(
                    Concept("power", r["volts"]))
                if not chk:
                    diags.append(Diagnostic(
                        "VOLTAGE_MISMATCH",
                        "role %r drives rail %r at %gV but the rail is "
                        "declared at %gV" % (role.id, cap.rail, cap.volts,
                                             r["volts"]), (role.id,)))
    # per-(bus, address) uniqueness — intent-level static
    buckets: Dict[Tuple[str, Any], List[str]] = {}
    for role in ctx.roles:
        for dem in role.demands:
            if "i2c_addr" in dem.attrs:
                key = (dem.bus or "", dem.attrs["i2c_addr"])
                buckets.setdefault(key, []).append(role.id)
    for (busname, addr), claimants in sorted(
            buckets.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        distinct = sorted(set(claimants))
        if len(distinct) > 1:
            addr_s = hex(addr) if isinstance(addr, int) else str(addr)
            diags.append(Diagnostic(
                "ADDR_COLLISION",
                "address %s on bus %r is claimed by roles %s"
                % (addr_s, busname or "<default>", ", ".join(distinct)),
                tuple(distinct)))


# ---------------------------------------------------------------------------
# The resolution ladder
# ---------------------------------------------------------------------------

def _resolve_power(ctx: _Ctx, role: RoleFact, dem: DemFact,
                   chain: List[str], diags: List[Diagnostic],
                   escalations: List[Dict[str, Any]],
                   resolutions: Dict[str, str]) -> None:
    want = Concept("power", dem.volts)
    for s in chain:                                     # nearest wins
        here = [r for r in ctx.rails if r["scope"] == s]
        ok = [r for r in here
              if want.accepts(Concept("power", r["volts"]))]
        if not ok:
            continue
        if len(ok) > 1:
            if dem.default and any(r["name"] == dem.default for r in ok):
                resolutions[dem.id] = "rail:%s" % dem.default
                return
            escalations.append({
                "code": "AMBIGUOUS_NONEQUIV",
                "msg": "power demand %r matches %d rails in scope %r; rails "
                       "are not equivalent units and no policy is declared"
                       % (dem.id, len(ok), s),
                "subjects": [dem.id],
                "conflict": ["%s demands power at %s" % (
                                 dem.id, "%gV" % dem.volts
                                 if dem.volts is not None else "any voltage")]
                            + ["rail %r (%gV) is compatible"
                               % (r["name"], r["volts"]) for r in ok],
                "relaxation": "declare default=<rail name> on demand %r, or "
                              "scope the extra rail away" % dem.id})
            return
        resolutions[dem.id] = "rail:%s" % ok[0]["name"]
        return
    # no compatible rail anywhere in the chain: a declared default rail?
    if dem.default:
        drail = ctx.rail_named(dem.default)
        if drail is not None:
            chk = want.accepts(Concept("power", drail["volts"]))
            if chk:
                resolutions[dem.id] = "rail:%s" % dem.default
                return
            diags.append(Diagnostic(
                "VOLTAGE_MISMATCH",
                "power demand %r declares default rail %r, rejected by the "
                "concept check: %s" % (dem.id, dem.default, chk.reason),
                (dem.id,)))
            return
        diags.append(Diagnostic(
            "DEFAULT_INCOMPATIBLE",
            "power demand %r declares default %r, which is not a declared "
            "rail — a power demand's default must be a rail"
            % (dem.id, dem.default), (dem.id,)))
        return
    in_scope = [r for r in ctx.rails if r["scope"] in chain]
    if in_scope:
        seen = ", ".join("%s=%gV" % (r["name"], r["volts"])
                         for r in in_scope)
        diags.append(Diagnostic(
            "VOLTAGE_MISMATCH",
            "power demand %r requires %gV but no in-scope rail is "
            "compatible (in scope: %s)" % (dem.id, dem.volts, seen),
            (dem.id,)))
    else:
        diags.append(Diagnostic(
            "RAIL_SCOPE",
            "power demand %r (role %r, scope %r) has no rail in scope and "
            "no declared default — resolution-ladder rung 2 load error"
            % (dem.id, role.id, role.scope), (dem.id,)))


def _resolve_signal(ctx: _Ctx, role: RoleFact, dem: DemFact,
                    chain: List[str], diags: List[Diagnostic],
                    escalations: List[Dict[str, Any]],
                    resolutions: Dict[str, str],
                    requests: List[Tuple[DemFact, PoolFact]]) -> None:
    want = Concept(dem.iface, dem.volts)

    # explicit bus attachment is an AUTHORED, deliberate resolution (law 6a)
    if dem.bus is not None:
        b = ctx.bus_named(dem.bus)
        if b is not None and b["iface"] == dem.iface and b["scope"] in chain:
            resolutions[dem.id] = "bus:%s" % b["name"]
            return
        why = ("no bus named %r is declared" % dem.bus if b is None
               else "bus %r is iface %r / scope %r, not an in-scope %r bus"
               % (dem.bus, b["iface"], b["scope"], dem.iface))
        diags.append(Diagnostic(
            "DEMAND_UNSATISFIABLE",
            "demand %r attaches to bus %r but %s" % (dem.id, dem.bus, why),
            (dem.id,)))
        return

    # candidate provider classes (each is structurally concept-verified)
    cap_hits: List[Tuple[str, CapFact, str]] = []   # (role_id, cap, proof)
    for other in ctx.roles:
        if other.scope not in chain:
            continue
        for cap in other.capabilities:
            chk = want.accepts(Concept(cap.iface, cap.volts))
            if chk:
                cap_hits.append((other.id, cap, chk.reason))
    pool_hits: List[Tuple[PoolFact, str]] = []
    for p in ctx.pools:
        if p.scope not in chain:
            continue
        chk = want.accepts(Concept(p.provides))
        if chk:
            pool_hits.append((p, chk.reason))

    cap_roles = sorted({rid for rid, _, _ in cap_hits})
    n_classes = len(cap_roles) + len(pool_hits)

    def default_pick() -> Optional[str]:
        """A declared default IS the author's policy: if it names a
        concept-compatible candidate, it resolves the ladder."""
        name = dem.default
        if not name:
            return None
        p = ctx.pool_named(name)
        if p is not None and want.accepts(Concept(p.provides)):
            requests.append((dem, p))
            return "pool:%s (declared default)" % p.name
        for other in ctx.roles:
            if other.id != name or other.scope not in chain:
                continue
            for cap in other.capabilities:
                if want.accepts(Concept(cap.iface, cap.volts)):
                    return "role:%s (declared default)" % name
        b = ctx.bus_named(name)
        if b is not None and b["iface"] == dem.iface:
            return "bus:%s (declared default)" % name
        return None

    if n_classes == 0:
        picked = default_pick()
        if picked is not None:
            resolutions[dem.id] = picked
            return
        extra = (" (declared default %r is not a compatible satisfier)"
                 % dem.default) if dem.default else ""
        diags.append(Diagnostic(
            "DEMAND_UNSATISFIABLE",
            "demand %r requires iface %r but has no in-scope satisfier and "
            "no declared default%s — resolution-ladder rung 2 load error"
            % (dem.id, dem.iface, extra), (dem.id,)))
        return

    if n_classes == 1:
        if pool_hits:
            requests.append((dem, pool_hits[0][0]))     # rung 3: ALLOCATION
        else:
            resolutions[dem.id] = "role:%s" % cap_roles[0]   # rung 1: wire
        return

    # rung 4: ambiguity across NON-equivalent provider classes.
    picked = default_pick()
    if picked is not None:
        resolutions[dem.id] = picked
        return
    conflict = ["%s demands %r (qty %d)" % (dem.id, dem.iface, dem.qty)]
    for p, proof in sorted(pool_hits, key=lambda t: t[0].name):
        conflict.append("pool %r provides it (%d unit(s); %s)"
                        % (p.name, p.unit_count, proof))
    for rid in cap_roles:
        proofs = [pr for r2, _, pr in cap_hits if r2 == rid]
        conflict.append("role %r capability provides it (%s)"
                        % (rid, "; ".join(proofs)))
    conflict.append(
        "the %d candidate classes are not declared equivalent (no single "
        "pool spans them) and no selection policy or default is declared"
        % n_classes)
    escalations.append({
        "code": "AMBIGUOUS_NONEQUIV",
        "msg": "demand %r is satisfiable by %d non-equivalent provider "
               "classes; the engine refuses to guess" % (dem.id, n_classes),
        "subjects": [dem.id],
        "conflict": conflict,
        "relaxation": "declare default=<candidate> on demand %r (the "
                      "policy), or pin one provider via a layer-2 "
                      "refinement" % dem.id})


# ---------------------------------------------------------------------------
# The allocator: deterministic, sticky, pin-honoring, spares stay visible
# ---------------------------------------------------------------------------

def _allocate(ctx: _Ctx, requests: List[Tuple[DemFact, PoolFact]],
              pins: List[PinOp], diags: List[Diagnostic],
              incumbents: Optional[List[Dict[str, Any]]] = None
              ) -> Tuple[List[Dict[str, Any]], Dict[str, List[int]]]:
    entries: List[Dict[str, Any]] = []
    used: Dict[str, set] = {p.name: set() for p in ctx.pools}
    served: Dict[str, int] = {}
    req_by_demand: Dict[str, Tuple[DemFact, PoolFact]] = {
        dem.id: (dem, p) for dem, p in requests}

    # incumbent allocations (minimal-disturbance ECO, Gen4 section 2.3):
    # a prior emit's record, keyed demand -> ordered (pool, unit) list.
    # The solver PREFERS an incumbent unit when it is still legal and free
    # — sticky survival is charged only when a pin/legality change forces
    # a move. Deterministic: the incumbent record is part of the input.
    incumbent_units: Dict[str, List[Tuple[str, int]]] = {}
    for e in sorted(incumbents or [],
                    key=lambda e: (str(e.get("demand")), str(e.get("pool")),
                                   str(e.get("unit")))):
        try:
            unit = int(e["unit"])
        except (KeyError, TypeError, ValueError):
            continue
        incumbent_units.setdefault(str(e.get("demand")), []).append(
            (str(e.get("pool")), unit))

    # author pins FIRST (they outrank the solver), canonically ordered
    for op in sorted(pins, key=lambda o: (o.demand_id, o.unit)):
        if op.demand_id not in req_by_demand:
            raise ModellerError(
                "PIN_NOT_ALLOCATABLE",
                "pin targets demand %r, which is not a pool-allocated "
                "demand of this document" % op.demand_id)
        dem, p = req_by_demand[op.demand_id]
        if op.pool_name is not None and op.pool_name != p.name:
            target = ctx.pool_named(op.pool_name)
            if target is None:
                raise ModellerError(
                    "PIN_UNKNOWN_POOL",
                    "pin names pool %r, which is not declared" % op.pool_name)
            chk = Concept(dem.iface).accepts(Concept(target.provides))
            if not chk:
                raise ModellerError(
                    "PIN_CONCEPT_REJECTED",
                    "pin of demand %r to pool %r rejected by the concept "
                    "check: %s" % (op.demand_id, op.pool_name, chk.reason))
            raise ModellerError(
                "PIN_POOL_MISMATCH",
                "pin names pool %r but demand %r resolved to pool %r"
                % (op.pool_name, op.demand_id, p.name))
        chk = Concept(dem.iface).accepts(Concept(p.provides))
        if not chk:      # unreachable via the ladder; a direct-API guard
            raise ModellerError(
                "PIN_CONCEPT_REJECTED",
                "pin of demand %r to pool %r rejected by the concept "
                "check: %s" % (op.demand_id, p.name, chk.reason))
        if not (isinstance(op.unit, int) and 0 <= op.unit < p.unit_count):
            raise ModellerError(
                "PIN_UNKNOWN_UNIT",
                "pin of demand %r names unit %r of pool %r (valid: 0..%d)"
                % (op.demand_id, op.unit, p.name, p.unit_count - 1))
        if op.unit in used[p.name]:
            raise ModellerError(
                "PIN_DOUBLE_BOOK",
                "unit %d of pool %r is already taken" % (op.unit, p.name))
        if served.get(dem.id, 0) >= dem.qty:
            raise ModellerError(
                "PIN_OVERSERVED",
                "demand %r (qty %d) is already fully served"
                % (dem.id, dem.qty))
        used[p.name].add(op.unit)
        served[dem.id] = served.get(dem.id, 0) + 1
        entries.append({"pool": p.name, "unit": op.unit,
                        "demand": dem.id, "chosen_by": "author",
                        "state": "pinned", "locked_by": None})

    # the trivial-deterministic solver: demands in canonical (sorted-id)
    # order, INCUMBENT unit first when legal-and-free (prefer incumbents —
    # every changed binding is charged), else lowest free unit — stable,
    # re-derivable, sticky
    for dem, p in sorted(requests, key=lambda t: t[0].id):
        pending_incumbents = [
            u for pool_name, u in incumbent_units.get(dem.id, [])
            if pool_name == p.name and 0 <= u < p.unit_count]
        while served.get(dem.id, 0) < dem.qty:
            pick = None
            while pending_incumbents:
                cand = pending_incumbents.pop(0)
                if cand not in used[p.name]:
                    pick = cand
                    break
            if pick is None:
                free = [u for u in range(p.unit_count)
                        if u not in used[p.name]]
                if not free:
                    diags.append(Diagnostic(
                        "POOL_INSUFFICIENT",
                        "pool %r exhausted while serving demand %r (qty %d, "
                        "served %d)" % (p.name, dem.id, dem.qty,
                                        served.get(dem.id, 0)), (dem.id,)))
                    break
                pick = free[0]
            used[p.name].add(pick)
            served[dem.id] = served.get(dem.id, 0) + 1
            entries.append({"pool": p.name, "unit": pick,
                            "demand": dem.id, "chosen_by": "solver",
                            "state": "sticky", "locked_by": None})

    # uncommitted units remain VISIBLE spare capacity — never dropped
    spares = {}
    for p in ctx.pools:
        free = sorted(set(range(p.unit_count)) - used[p.name])
        if free:
            spares[p.name] = free
    return entries, spares


# ---------------------------------------------------------------------------
# Document assembly (EMIT_CONTRACT Part B/C shapes; optionals only when set)
# ---------------------------------------------------------------------------

def _put(d: Dict[str, Any], key: str, value: Any, default: Any) -> None:
    if value != default:
        d[key] = value


def _emit_doc(ctx: _Ctx, series: str, escalations: List[Dict[str, Any]],
              entries: List[Dict[str, Any]], spares: Dict[str, List[int]],
              resolutions: Dict[str, str]) -> Dict[str, Any]:
    roles_json = []
    for r in ctx.roles:
        rj: Dict[str, Any] = {"id": r.id, "kind": r.kind}
        _put(rj, "scope", r.scope, "")
        caps_json = []
        for c in r.capabilities:
            cj: Dict[str, Any] = {"iface": c.iface}
            _put(cj, "volts", c.volts, None)
            _put(cj, "rail", c.rail, None)
            _put(cj, "attrs", c.attrs, {})
            caps_json.append(cj)
        _put(rj, "capabilities", caps_json, [])
        dems_json = []
        for dm in r.demands:
            dj: Dict[str, Any] = {"id": dm.id, "iface": dm.iface}
            _put(dj, "volts", dm.volts, None)
            _put(dj, "bus", dm.bus, None)
            _put(dj, "qty", dm.qty, 1)
            _put(dj, "default", dm.default, None)
            _put(dj, "attrs", dm.attrs, {})
            dems_json.append(dj)
        _put(rj, "demands", dems_json, [])
        roles_json.append(rj)

    rails_json = []
    for r in ctx.rails:
        j: Dict[str, Any] = {"name": r["name"], "volts": r["volts"]}
        _put(j, "scope", r["scope"], "")
        _put(j, "attrs", r["attrs"], {})
        rails_json.append(j)

    grounds_json = []
    for g in ctx.grounds:
        j = {"name": g["name"]}
        _put(j, "kind", g["kind"], "ground")
        _put(j, "role", g["role"], "none")
        _put(j, "scope", g["scope"], "")
        _put(j, "attrs", g["attrs"], {})
        grounds_json.append(j)

    scopes_json = []
    for s in ctx.scopes:
        j = {"id": s["id"]}
        _put(j, "parent", s["parent"], "")
        scopes_json.append(j)

    bonds_json = []
    for b in ctx.bonds:
        j = {"name": b["name"], "joins": list(b["joins"])}
        _put(j, "attrs", b["attrs"], {})
        bonds_json.append(j)

    buses_json = []
    for b in ctx.buses:
        j = {"name": b["name"], "iface": b["iface"]}
        _put(j, "scope", b["scope"], "")
        _put(j, "attrs", b["attrs"], {})
        buses_json.append(j)

    pools_json = []
    for p in ctx.pools:
        j = {"name": p.name, "role": p.role, "provides": p.provides,
             "unit_count": p.unit_count,
             "port_signature": list(p.port_signature)}
        _put(j, "attrs", p.attrs, {})
        pools_json.append(j)

    invs_json = []
    for i in ctx.invariants:
        j = {"kind": i["kind"], "subjects": list(i["subjects"])}
        _put(j, "inputs", list(i["inputs"]), [])
        _put(j, "attrs", i["attrs"], {})
        invs_json.append(j)

    escs_json = []
    for e in escalations:
        j = {"code": e["code"], "msg": e["msg"]}
        _put(j, "subjects", list(e["subjects"]), [])
        _put(j, "conflict", list(e["conflict"]), [])
        _put(j, "relaxation", e["relaxation"], "")
        escs_json.append(j)

    lock_json = []
    for g in sorted(ctx.locks, key=lambda g: g["name"]):
        j = {"name": g["name"], "covers": list(g["covers"]),
             "version": 0, "snapshot": None}
        _put(j, "owner", g["owner"], "")
        _put(j, "sync_point", g["sync_point"], "")
        lock_json.append(j)

    allocation = {
        "entries": sorted(entries, key=lambda e: (e["pool"], e["unit"],
                                                  e["demand"])),
        "lock_groups": lock_json,
        "solver_version": SOLVER_VERSION,
    }

    doc: Dict[str, Any] = {
        "layer": 1,
        "series": series,
        "roles": roles_json,
        "rails": rails_json,
        "grounds": grounds_json,
        "allocation": allocation,
    }
    _put(doc, "scopes", scopes_json, [])
    _put(doc, "bonds", bonds_json, [])
    _put(doc, "buses", buses_json, [])
    _put(doc, "pools", pools_json, [])
    _put(doc, "invariants", invs_json, [])
    # placement: positive assertions only (semantics section 4, say-only-the-
    # delta) — a document with no placement declarations omits the section
    # entirely (the omit-when-empty pattern), so today's corpus emits no
    # placement section and no .placement.json.
    _put(doc, "placement", [dict(r) for r in ctx.placement], [])
    _put(doc, "escalations", escs_json, [])

    # scope-resolved-and-RECORDED (Gen4 2.1 "no hidden power pins") + spare
    # capacity stays a visible symbolic resource: both live in the artifact.
    attrs: Dict[str, Any] = {}
    if resolutions:
        attrs["resolutions"] = {k: resolutions[k] for k in sorted(resolutions)}
    if spares:
        attrs["pool_spares"] = {k: list(spares[k]) for k in sorted(spares)}
    _put(doc, "attrs", attrs, {})
    return doc


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def elaborate(intent_cls: Type[Module], doc_name: Optional[str] = None,
              ops: Tuple[Any, ...] = (),
              incumbents: Optional[List[Dict[str, Any]]] = None,
              fork: Optional[Tuple[str, str, str]] = None) -> EmitResult:
    """Elaborate one intent class (plus optional layer-2 refinement ops)
    into its layer-1 document. Deterministic: two calls on the same inputs
    produce byte-identical JSON.

    ``incumbents``  a PRIOR emit's allocation entries (plain dicts): the
                    solver prefers each demand's incumbent unit when it is
                    still legal and free — the minimal-disturbance ECO
                    re-solve (Gen4 section 2.3). Part of the deterministic
                    input.
    ``fork``        ``(parent_series, new_series, reason)`` — the
                    break_lock escape hatch at authoring altitude: the
                    emitted document carries ``series=new_series`` and the
                    ``forked_from`` record naming the parent series, so
                    editing decisions locked in the parent series is legal
                    (Gen4 section 2.5). A fork that does not actually
                    change the series is a structured load error.
    """
    root = intent_cls()                       # law 2: zero-arg default
    ctx = _Ctx()
    _walk(root, "", "", ctx)

    diags: List[Diagnostic] = []
    escalations: List[Dict[str, Any]] = []
    resolutions: Dict[str, str] = {}
    requests: List[Tuple[DemFact, PoolFact]] = []

    _check_consistency(ctx, diags)
    for role in ctx.roles:
        chain = ctx.scope_chain(role.scope)
        for dem in role.demands:
            if dem.iface == "power":
                _resolve_power(ctx, role, dem, chain, diags, escalations,
                               resolutions)
            else:
                _resolve_signal(ctx, role, dem, chain, diags, escalations,
                                resolutions, requests)

    pins = [op for op in ops if isinstance(op, PinOp)]
    bindings = [(op.role_id, op.part) for op in ops
                if isinstance(op, BindOp)]
    role_ids = {r.id for r in ctx.roles}
    for rid, _part in bindings:
        if rid not in role_ids:
            raise ModellerError(
                "BIND_UNKNOWN_ROLE",
                "bind targets role %r, which does not exist" % rid)
    stray = [op for op in ops if not isinstance(op, (PinOp, BindOp))]
    if stray:
        raise ModellerError(
            "UNKNOWN_REFINEMENT_OP", "unknown refinement op(s): %r" % stray)

    entries, spares = _allocate(ctx, requests, pins, diags,
                                incumbents=incumbents)

    series = root.series
    forked_from: Optional[Dict[str, str]] = None
    if fork is not None:
        parent_series, new_series, reason = fork
        if new_series == parent_series:
            raise ModellerError(
                "FORK_SAME_SERIES",
                "a series fork must fork a NEW series (got %r == parent)"
                % new_series)
        series = new_series
        forked_from = {"series": parent_series,
                       "reason": reason or "break_lock"}

    doc = _emit_doc(ctx, series, escalations, entries, spares,
                    resolutions)
    if forked_from is not None:
        doc["forked_from"] = forked_from
    return EmitResult(
        name=doc_name or intent_cls._intent_name or intent_cls.__name__,
        doc=doc,
        diagnostics=diags,
        resolutions=resolutions,
        allocation_entries=doc["allocation"]["entries"],
        spares=spares,
        bindings=bindings,
        test_declarations=sorted(ctx.tests, key=lambda t: t["id"]),
    )


_EXEMPLAR_SEQ = [0]


def exemplify(module_cls: Type[Module]) -> EmitResult:
    """Law 2 made mechanical: ANY registered module renders standalone with
    zero args, as a synthetic one-child intent fragment. Unresolved demands
    surface as structured diagnostics — a fragment is honest about what its
    enclosing scope must provide.

    The synthetic Exemplar class is SCAFFOLDING, not a declaration: it is
    unregistered from the global MODULES registry before returning (even on
    error), so the runner's law-2 sweep and ``--list`` see only what the
    corpus actually declared, in any call order within one process."""
    _EXEMPLAR_SEQ[0] += 1
    name = "Exemplar%d_%s" % (_EXEMPLAR_SEQ[0], module_cls.__name__)
    exemplar = type(name, (Module,), {"example": use(module_cls)})
    try:
        return elaborate(exemplar, doc_name="exemplar_%s" % module_cls.__name__)
    finally:
        MODULES.pop(name, None)


__all__ = ["elaborate", "exemplify", "EmitResult", "Diagnostic",
           "SOLVER_VERSION"]
