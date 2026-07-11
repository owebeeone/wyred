"""The modeller substrate: ONE Module base, marker declarations, registries.

Authoring model (the surface):

  * Everything is a subclass of the ONE ``Module`` base (composition law 4 —
    closure): a composite declares child modules with ``use(Cls, ...)`` in its
    class body and is itself usable anywhere a primitive is.
  * A class body is a set of MARKER DECLARATIONS (yidl marker-harvest style):
    ``param`` / ``demand`` / ``provide`` / ``rail`` / ``ground`` / ``bond`` /
    ``bus`` / ``pool`` / ``mutual_exclusion`` / ``lock_group`` / ``use``.
    Declaring the class IS registering it (law 1) — there are no build() call
    sites anywhere; the engine walks the declarations.
  * Say only the non-default: every ``param`` has a default, so every module
    instantiates with ZERO arguments (law 2); an author states only
    deviations (``use(TempSensor, addr=0x49)``).
  * ``late("field")`` is the injection hook (anchorscad datatrees ``Node``
    analog): a declaration value that resolves against the declaring
    instance's params at elaboration time — how a parent's parameter cascades
    into a child or into a demand's attributes with zero threading (law 3).
  * Identity is MINTED by the substrate (law 8): role ids, demand ids and
    pool names are dotted instance paths derived from attribute names
    (``drive.bridge1.hs.cmd``) — an author never writes a refdes and two
    instances of one class can never collide.

Two-layer authoring:

  * A ``Module`` subclass declared with ``intent="..."`` is a LAYER-1 intent
    document root; the common runner discovers and emits every one.
  * A ``Refinement`` subclass is the LAYER-2 authoring object: an ordered
    list of ops (``pin`` an allocation, ``bind`` a role to a part) that only
    NARROW layer-1 semantics — a refinement never edits an intent class, and
    the emitted L1 roles/rails/pools are byte-identical with or without it
    (only the allocation record and, at M2, the bound netlist differ).

Pure Python 3 stdlib. No imports from the harness — the modeller is
independent of its checkers; only the runner's self-check touches the oracle.
"""

from __future__ import annotations

import itertools
import re
from typing import Any, Dict, List, Optional, Tuple, Type

from .concepts import Concept

_ORDER = itertools.count()

# Decision classes a LockGroup may cover (Gen4 section 2.5; mirrors the emit
# contract's vocabulary — declared here so lock declarations are validated at
# compose time without importing the harness).
DECISION_CLASSES = frozenset({
    "pool_allocation", "part_binding", "pin_map", "footprint",
    "connector_pinout", "design_rule",
})

# Part-definition PIN vocabulary that must never be used as a design rail
# name (Gen4 section 2.1): rail names are design vocabulary.
_PIN_NAME_VOCAB = frozenset({
    "VCC", "VDD", "AVDD", "DVDD", "VDDA", "VDDIO", "VSS", "VEE", "GND",
})


class ModellerError(Exception):
    """A STRUCTURED authoring/elaboration error — the load-error channel
    (composition law 10: no silent defaults, every failure is explicit)."""

    def __init__(self, code: str, msg: str):
        super().__init__("%s: %s" % (code, msg))
        self.code = code
        self.msg = msg


# ---------------------------------------------------------------------------
# Late binding — the injection hook (parent params cascade into declarations)
# ---------------------------------------------------------------------------

class Late:
    """A declaration value resolved against the DECLARING instance's params
    at elaboration time (``late("addr")`` inside TempSensor resolves against
    that TempSensor instance; ``late("supply_v")`` inside a ``use()``
    override resolves against the parent whose body contains the use)."""

    __slots__ = ("field",)

    def __init__(self, field: str):
        self.field = field

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "late(%r)" % self.field


def late(field: str) -> Late:
    return Late(field)


def resolve_value(value: Any, inst: "Module") -> Any:
    """Resolve ``Late`` references (recursively through dicts/lists/tuples)
    against ``inst``'s parameters."""
    if isinstance(value, Late):
        if value.field not in inst._params:
            raise ModellerError(
                "UNKNOWN_PARAM",
                "late(%r) does not name a declared param of %s (declared: %s)"
                % (value.field, type(inst).__name__,
                   ", ".join(sorted(inst._params)) or "none"))
        return inst._params[value.field]
    if isinstance(value, dict):
        return {k: resolve_value(v, inst) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(resolve_value(v, inst) for v in value)
    return value


# ---------------------------------------------------------------------------
# Marker declarations (harvested from class bodies; creation order preserved)
# ---------------------------------------------------------------------------

class Decl:
    """Base of every marker declaration; carries a global creation order so
    harvesting is deterministic across inheritance."""

    def __init__(self) -> None:
        self._order = next(_ORDER)


class ParamDecl(Decl):
    def __init__(self, default: Any):
        super().__init__()
        self.default = default


def param(default: Any) -> ParamDecl:
    """A substitutable, sensibly-defaulted parameter (gen-3 R-B). Every param
    MUST default — that is what makes zero-arg self-exemplification (law 2)
    a structural property rather than a convention."""
    return ParamDecl(default)


class DemandDecl(Decl):
    def __init__(self, iface: str, volts: Any = None, bus: Any = None,
                 qty: Any = 1, default: Any = None,
                 companions: Tuple[str, ...] = (), **attrs: Any):
        super().__init__()
        self.iface = iface
        self.volts = volts
        self.bus = bus
        self.qty = qty
        self.default = default
        self.companions = tuple(companions)
        self.attrs = dict(attrs)


def demand(iface: str, volts: Any = None, bus: Any = None, qty: Any = 1,
           default: Any = None, companions: Tuple[str, ...] = (),
           **attrs: Any) -> DemandDecl:
    """A typed REQUIRE port — something this module needs, stated
    symbolically ("a uart", "power at 3.3V"), never a unit or a pin.
    ``companions`` declares the demand-driven generation hook (law 7): the
    support parts a layer-2 binding of this demand may expand — declared
    now, filled by the M2 elaborator, carried in the L1 doc."""
    return DemandDecl(iface, volts=volts, bus=bus, qty=qty, default=default,
                      companions=companions, **attrs)


class ProvideDecl(Decl):
    def __init__(self, iface: str, volts: Any = None, rail: Any = None,
                 companions: Tuple[str, ...] = (), **attrs: Any):
        super().__init__()
        self.iface = iface
        self.volts = volts
        self.rail = rail
        self.companions = tuple(companions)
        self.attrs = dict(attrs)


def provide(iface: str, volts: Any = None, rail: Any = None,
            companions: Tuple[str, ...] = (), **attrs: Any) -> ProvideDecl:
    """A typed PROVIDE port — a capability this module supplies. A power
    capability naming ``rail=`` DRIVES that design rail (rail-tree
    consistency is checked against the declaration)."""
    return ProvideDecl(iface, volts=volts, rail=rail, companions=companions,
                       **attrs)


class RailDecl(Decl):
    def __init__(self, name: str, volts: Any, **attrs: Any):
        super().__init__()
        self.name = name
        self.volts = volts
        self.attrs = dict(attrs)


def rail(name: str, volts: float, **attrs: Any) -> RailDecl:
    """A scoped power rail, FUNCTIONALLY named in design vocabulary
    ("+3V3", "VBUS", "VIN" — KiCad style, no dots, never a pin name)."""
    return RailDecl(name, volts, **attrs)


class GroundDecl(Decl):
    def __init__(self, name: str, kind: str = "ground", role: str = "none",
                 **attrs: Any):
        super().__init__()
        self.name = name
        self.kind = kind
        self.role = role
        self.attrs = dict(attrs)


def ground(name: str, kind: str = "ground", role: str = "none",
           **attrs: Any) -> GroundDecl:
    """One unified 0V return partitioned by ROLE tags (analog/digital/power/
    reference), never split nets; chassis/earth are distinct kinds."""
    return GroundDecl(name, kind=kind, role=role, **attrs)


class BondDecl(Decl):
    def __init__(self, name: str, joins: Tuple[str, ...], **attrs: Any):
        super().__init__()
        self.name = name
        self.joins = tuple(joins)
        self.attrs = dict(attrs)


def bond(name: str, *joins: str, **attrs: Any) -> BondDecl:
    """A first-class AUTHORED star point / net-tie joining ground names —
    grounds are never merged implicitly."""
    return BondDecl(name, joins, **attrs)


class BusDecl(Decl):
    def __init__(self, name: str, iface: str,
                 companions: Tuple[str, ...] = (), **attrs: Any):
        super().__init__()
        self.name = name
        self.iface = iface
        self.companions = tuple(companions)
        self.attrs = dict(attrs)


def bus(name: str, iface: str, companions: Tuple[str, ...] = (),
        **attrs: Any) -> BusDecl:
    """A shared multi-drop interface instance (one I2C bus). Demands attach
    by naming it; per-(bus, address) uniqueness is intent-level static."""
    return BusDecl(name, iface, companions=companions, **attrs)


class PoolDecl(Decl):
    def __init__(self, provides: str, units: Any,
                 ports: Optional[Tuple[str, ...]] = None, **attrs: Any):
        super().__init__()
        self.provides = provides
        self.units = units
        self.ports = None if ports is None else tuple(ports)
        self.attrs = dict(attrs)


def pool(provides: str, units: int,
         ports: Optional[Tuple[str, ...]] = None,
         **attrs: Any) -> PoolDecl:
    """A DECLARED equivalence class of interchangeable units on this role
    (the 7400's four NANDs, an MCU's UARTs). Equivalence is declared, never
    inferred; the pool's name is minted from the instance path; units are
    integer indices; uncommitted units remain visible spare capacity. The
    typed port signature of one unit derives from the concept's canonical
    signature unless ``ports`` overrides it (say-only-the-non-default)."""
    return PoolDecl(provides, units, ports=ports, **attrs)


class MutexDecl(Decl):
    def __init__(self, subjects: Tuple[str, str], inputs: Tuple[str, ...],
                 **attrs: Any):
        super().__init__()
        self.subjects = tuple(subjects)
        self.inputs = tuple(inputs)
        self.attrs = dict(attrs)


def mutual_exclusion(subjects: Tuple[str, str],
                     inputs: Tuple[str, ...] = (),
                     **attrs: Any) -> MutexDecl:
    """A layer-1 mutual-exclusion invariant DECLARATION over module-relative
    signal references ("hs.gate" inside a HalfBridge). The engine namespaces
    every reference with the instance path, so each instantiation of the
    declaring composite mints its own invariant — declare once, get one per
    bridge."""
    return MutexDecl(subjects, inputs, **attrs)


class LockDecl(Decl):
    def __init__(self, name: str, covers: Tuple[str, ...], owner: str = "",
                 sync_point: str = ""):
        super().__init__()
        self.name = name
        self.covers = tuple(covers)
        self.owner = owner
        self.sync_point = sync_point


def lock_group(name: str, covers: Tuple[str, ...], owner: str = "",
               sync_point: str = "") -> LockDecl:
    """A lock group declared UP FRONT as document metadata (Gen4 section
    2.5): the decision classes it covers, its owner, and its sync point.
    Unknown decision classes are a compose-time load error — a lock that
    silently protects nothing is rejected before emission."""
    return LockDecl(name, covers, owner=owner, sync_point=sync_point)


class ChildDecl(Decl):
    def __init__(self, module_cls: Type["Module"], overrides: Dict[str, Any]):
        super().__init__()
        self.module_cls = module_cls
        self.overrides = dict(overrides)


def use(module_cls: Type["Module"], **overrides: Any) -> ChildDecl:
    """Compose a child module (closure: any Module — primitive or composite —
    composes identically). Overrides name the child's declared params and may
    be ``late(...)`` references into the PARENT's params (the injection
    cascade, law 3). The child's instance identity is minted from the
    attribute name, namespaced by the parent path (law 8)."""
    if not (isinstance(module_cls, type) and issubclass(module_cls, Module)):
        raise ModellerError(
            "NOT_A_MODULE",
            "use() takes a Module subclass, got %r" % (module_cls,))
    return ChildDecl(module_cls, overrides)


# ---------------------------------------------------------------------------
# The ONE Module base (declaration = registration)
# ---------------------------------------------------------------------------

def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


MODULES: Dict[str, Type["Module"]] = {}      # every declared module
INTENTS: Dict[str, Type["Module"]] = {}      # intent-marked document roots


class Module:
    """The single base abstraction. A subclass's body is a set of marker
    declarations; subclassing registers it (law 1); instantiating with zero
    args yields its self-exemplifying default (law 2); a composite IS a
    Module (law 4); its ports (demands/provides) are the only boundary
    (law 5)."""

    kind: Optional[str] = None       # abstract capability class; defaults to
                                     # the snake_case class name
    series: str = "A"                # document series (intent roots only)
    expected_l1: Tuple[str, ...] = ()   # self-check: expected oracle codes
    expect_escalation: bool = False     # self-check: rung-4 escalation due

    _decls: List[Tuple[str, Decl]] = []
    _intent_name: Optional[str] = None

    def __init_subclass__(cls, intent: Optional[str] = None,
                          **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Harvest declarations across the MRO: base-class decls first (their
        # creation order is globally monotonic), subclass overrides replace
        # by name. Deterministic by construction.
        merged: Dict[str, Decl] = {}
        for klass in reversed(cls.__mro__):
            for name, val in vars(klass).items():
                if isinstance(val, Decl):
                    merged[name] = val
        cls._decls = sorted(merged.items(), key=lambda kv: kv[1]._order)

        if MODULES.get(cls.__name__) not in (None, cls):
            raise ModellerError(
                "DUPLICATE_MODULE",
                "a Module named %r is already declared" % cls.__name__)
        MODULES[cls.__name__] = cls

        cls._intent_name = intent
        if intent is not None:
            if intent in INTENTS:
                raise ModellerError(
                    "DUPLICATE_INTENT",
                    "an intent named %r is already declared" % intent)
            INTENTS[intent] = cls

    def __init__(self, **overrides: Any):
        self._params: Dict[str, Any] = {}
        for name, decl in type(self)._decls:
            if isinstance(decl, ParamDecl):
                self._params[name] = overrides.pop(name, decl.default)
        if overrides:
            raise ModellerError(
                "UNKNOWN_PARAM",
                "%s has no declared param(s) %s (declared: %s)" % (
                    type(self).__name__,
                    ", ".join(sorted(repr(k) for k in overrides)),
                    ", ".join(sorted(self._params)) or "none"))

    @classmethod
    def module_kind(cls) -> str:
        return cls.kind or _snake(cls.__name__)


def validate_rail_name(name: str) -> None:
    """Rail names are DESIGN vocabulary: no dots, never part-pin vocabulary
    (VCC/VDD/... belong to part definitions, not designs)."""
    if "." in name:
        raise ModellerError(
            "BAD_RAIL_NAME", "rail name %r contains a dot" % name)
    if name.upper() in _PIN_NAME_VOCAB:
        raise ModellerError(
            "BAD_RAIL_NAME",
            "rail name %r is part-definition PIN vocabulary; rails are "
            "design vocabulary (+3V3, VBUS, VIN, ...)" % name)


# ---------------------------------------------------------------------------
# Layer-2 refinements (ordered ops; never edit layer 1)
# ---------------------------------------------------------------------------

class PinOp:
    """Author-pins one unit of a pool to a demand (allocation ladder:
    promotes the entry to pinned, chosen_by=author; the solver must honor
    it). References layer-1 identities (demand ids), never module internals."""

    def __init__(self, demand_id: str, unit: int,
                 pool_name: Optional[str] = None):
        self.demand_id = demand_id
        self.unit = unit
        self.pool_name = pool_name

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "pin(%r, unit=%d)" % (self.demand_id, self.unit)


def pin(demand_id: str, unit: int, pool_name: Optional[str] = None) -> PinOp:
    return PinOp(demand_id, unit, pool_name)


class BindOp:
    """Binds a role to a concrete part. Recorded and carried through the
    result for the M2 elaborator; NEVER emitted into the layer-1 document
    (no part numbers exist at layer 1)."""

    def __init__(self, role_id: str, part: str):
        self.role_id = role_id
        self.part = part

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "bind(%r, part=%r)" % (self.role_id, self.part)


def bind(role_id: str, part: str) -> BindOp:
    return BindOp(role_id, part)


REFINEMENTS: Dict[str, Type["Refinement"]] = {}


class Refinement:
    """A LAYER-2 authoring object: an ordered op list refining a named
    intent. Declaring the subclass registers it (law 1); the runner emits
    ``emits`` as an additional artifact. A refinement narrows — it cannot
    add roles, rails, pools, or demands, and it touches zero layer-1 lines
    (the grader floor: the emitted L1 roles/rails/pools are identical with
    and without it)."""

    of: Optional[str] = None       # the intent this refines
    emits: Optional[str] = None    # the artifact name this refinement emits
    ops: Tuple[Any, ...] = ()
    freeze: Tuple[str, ...] = ()   # lock groups fired at THIS refinement's
                                   # netlist emit (the sync-point API: emit
                                   # is a freeze point, Gen4 section 2.5)
    incumbents: Optional[str] = None
                                   # name of a PRIOR artifact whose emitted
                                   # allocation record seeds the re-solve
                                   # (minimal-disturbance ECO: sticky
                                   # entries survive unless a pin/legality
                                   # change forces a move, Gen4 section 2.3)
    fork: Optional[Dict[str, str]] = None
                                   # {"of": <locked parent artifact>,
                                   #  "series": <new series>, "reason": ...}
                                   # — break_lock at authoring altitude: the
                                   # emit carries the new series + the
                                   # forked_from record, and the runner
                                   # verifies the fork against the parent's
                                   # EXTERNAL lock baseline (section 2.5)
    expected_l1: Optional[Tuple[str, ...]] = None   # None = inherit base's
    expect_escalation: Optional[bool] = None        # None = inherit base's

    def __init_subclass__(cls, of: Optional[str] = None,
                          emits: Optional[str] = None, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        cls.of = of or cls.of
        cls.emits = emits or cls.emits or _snake(cls.__name__)
        if cls.of is None:
            raise ModellerError(
                "REFINEMENT_UNANCHORED",
                "Refinement %r must declare of=<intent name>" % cls.__name__)
        if cls.fork is not None:
            missing = sorted({"of", "series"} - set(cls.fork))
            if missing:
                raise ModellerError(
                    "FORK_MALFORMED",
                    "Refinement %r declares fork without %s — a series fork "
                    "must name the locked parent artifact and the new series"
                    % (cls.__name__, ", ".join(missing)))
        if cls.emits in REFINEMENTS:
            raise ModellerError(
                "DUPLICATE_REFINEMENT",
                "a refinement emitting %r is already declared" % cls.emits)
        REFINEMENTS[cls.emits] = cls


__all__ = [
    "Module", "ModellerError", "Refinement",
    "param", "demand", "provide", "rail", "ground", "bond", "bus", "pool",
    "mutual_exclusion", "lock_group", "use", "late", "pin", "bind",
    "MODULES", "INTENTS", "REFINEMENTS", "DECISION_CLASSES",
    "resolve_value", "validate_rail_name",
    "ParamDecl", "DemandDecl", "ProvideDecl", "RailDecl", "GroundDecl",
    "BondDecl", "BusDecl", "PoolDecl", "MutexDecl", "LockDecl", "ChildDecl",
    "PinOp", "BindOp", "Late", "Decl",
]
