"""The builtin part library (layer 2) — the locked gen-2 decision.

Concrete parts with STRUCTURED terminals per rl/harness/EMIT_CONTRACT.md:
every electrical meaning lives in ``role`` / ``req_v`` / ``prov_v`` /
``iface`` / ``iface_member`` — the oracle reads only structured fields, so a
part definition here IS its contract, not a label.

A library entry is a builder keyed by the abstract ROLE KIND ("supply",
"regulator", "mcu", "bridge_leg", ...). Builders are FACT-DRIVEN: they take
the layer-1 role's own facts (demands, capabilities, owned pools) and mint
the concrete part(s) — one MCU builder yields "MCU-3UART" for a role owning
a 3-unit uart pool and "MCU-PWM6" for one owning a 6-channel pwm pool. This
is what keeps the library small and the resolver generic: the part adapts to
the declared intent, never to the intent FILE (do-not-repeat #7).

A role may bind to MULTIPLE parts (Gen4 section 4.2.1 blesses multi-part
roles): a bridge_leg binds a MOSFET plus its gate driver — the refinement
"binds MOSFETs/driver/logic parts" (Gen4 section 3, intent #8). The driver
is modelled as kind="logic_gate", logic_fn="buf" so the invariant layer's
combinational evaluator can trace THROUGH it to the physical mosfet gate
node — the guarantee lives in the emitted graph (do-not-repeat #3).

Companion VALUE POLICY also lives here (one place, all intents): decoupling /
LDO cap values, the i2c_speed -> pull-up table with its declared library
default, and the crystal load-cap derivation from the part's own load
capacitance attrs. Every derived value is recorded on the generated
component (``attrs["derived"]``) — no silent defaults.

Pure Python 3 stdlib. No harness imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .core import ModellerError


# ---------------------------------------------------------------------------
# Terminal / part-prototype shapes (plain dicts matching the emit contract)
# ---------------------------------------------------------------------------

def term(name: str, role: str, function: str = "",
         req_v: Optional[float] = None, prov_v: Optional[float] = None,
         iface: Optional[str] = None,
         iface_member: Optional[str] = None) -> Dict[str, Any]:
    """One structured Terminal dict, all contract fields present."""
    return {"name": name, "role": role, "function": function,
            "req_v": req_v, "prov_v": prov_v,
            "iface": iface, "iface_member": iface_member}


@dataclass
class PartProto:
    """One concrete part awaiting instantiation (refdes minted by the
    resolver; ``attrs`` gains provenance there).

    Wiring metadata (all read by the generic resolver):

    ``power_pins``    demand-facing power_in pins: [(pin, req_v)] — a power
                      demand's rail resolution lands on EVERY matching pin
                      (multi-domain parts wire all their supply pins; no
                      hidden power pins).
    ``ground_pins``   pins wired to the nearest in-scope ground.
    ``rail_pins``     capability-facing power_out pins: [(pin, volts, rail)].
    ``i2c_pins``      bus-member map {"sda": pin, "scl": pin} or None.
    ``demand_units``  consumer-side typed port sets per demanded iface:
                      {iface: [ {sig_port: pin} per demanded unit ]}.
    ``provide_units`` provider-side (non-pool) typed port sets per iface.
    ``pool_units``    pool-unit port sets: {provides_iface: [per-unit map]}.
    ``signals``       abstract signal label -> pin (invariant lowering:
                      the layer-1 ``provide`` this part realizes).

    Identity / provenance (M3, real-board encodings):

    ``refdes``        optional EXPLICIT identity. The default (None) keeps
                      substrate minting (law 8: the L1 author never writes a
                      refdes). A LIBRARY realization of an existing physical
                      board may carry the board's own refdes — that is
                      back-annotation data of the L2 binding (aligning the
                      emitted netlist with a real artifact), never an L1
                      authoring channel. Uniqueness is enforced at resolve.
    ``generated``     True marks a library-realization companion (the part's
                      application circuit — dividers, load caps, pull-ups):
                      it lands ``authored=false`` with
                      ``attrs["for_demand"] = <role id>`` (law 7: every
                      generated part traces to the declaration that produced
                      it — here, the role whose binding expanded it).
    ``derived``       the confession string for a ``generated`` proto (how
                      its value/topology was derived); required discipline,
                      defaulted by the resolver to name the realization.
    """

    value: str
    kind: str
    prefix: str
    logic_fn: Optional[str] = None
    terminals: List[Dict[str, Any]] = field(default_factory=list)
    attrs: Dict[str, Any] = field(default_factory=dict)
    power_pins: List[Tuple[str, Optional[float]]] = field(default_factory=list)
    ground_pins: List[str] = field(default_factory=list)
    rail_pins: List[Tuple[str, float, str]] = field(default_factory=list)
    i2c_pins: Optional[Dict[str, str]] = None
    demand_units: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)
    provide_units: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)
    pool_units: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)
    signals: Dict[str, str] = field(default_factory=dict)
    refdes: Optional[str] = None
    generated: bool = False
    derived: str = ""


@dataclass
class Realization:
    """A role's full multi-part library realization (M3).

    ``parts``          the PartProto list (primaries + ``generated``
                       application-circuit companions), minted in order.
    ``edges``          internal wiring: [(net_hint, [(part_index, pin), ...])]
                       — the intra-realization topology (a boost converter's
                       switch node, a divider's tap). Edges join whatever
                       nets their nodes already landed on (rails, bus nets,
                       allocated ports) — two nodes on two DIFFERENT
                       existing nets are a structured load error, never a
                       silent merge.
    ``demand_ports``   role-level typed port sets for the role's demands:
                       {"<iface>" or "<iface>#<demand label>":
                        [ {sig: (part_index, pin)} per demanded unit ]}.
                       The labelled form serves roles with SEVERAL demands
                       of one iface (a display's busy/dc/res/cs gpios); the
                       bare form is the single-demand default. Ports may
                       span parts (a crystal behind its series resistor) —
                       that is why they are role-level, not proto-level.
    ``provide_ports``  role-level provider port sets: {iface: [unit maps]}.

    A builder returning a plain ``List[PartProto]`` (the M1/M2 shape) is
    equivalent to ``Realization(parts=<list>)``.
    """

    parts: List[PartProto] = field(default_factory=list)
    edges: List[Tuple[str, List[Tuple[int, str]]]] = field(default_factory=list)
    demand_ports: Dict[str, List[Dict[str, Tuple[int, str]]]] = \
        field(default_factory=dict)
    provide_ports: Dict[str, List[Dict[str, Tuple[int, str]]]] = \
        field(default_factory=dict)


# ---------------------------------------------------------------------------
# Interface conventions shared by the resolver
# ---------------------------------------------------------------------------

# Cross-wiring per iface: a provider's sig-port connects to the demander's
# PAIRED sig-port (uart crosses tx<->rx; everything else wires straight).
PAIRING: Dict[str, Dict[str, str]] = {
    "uart": {"tx": "rx", "rx": "tx"},
}

# Bus member wires per bus iface.
BUS_MEMBERS: Dict[str, Tuple[str, ...]] = {
    "i2c": ("sda", "scl"),
}

# The i2c_speed lever -> pull-up value (declared LIBRARY policy; the bus's
# attrs["i2c_speed"] is the author's lever, the default below is the
# library's DECLARED default — recorded on the generated part either way).
I2C_PULLUP_OHMS: Dict[str, str] = {
    "standard": "4.7k",
    "fast": "2.2k",
    "fast_plus": "1k",
}
I2C_SPEED_DEFAULT = "standard"

# Companion value policy (one place; recorded on every generated part).
DECOUPLING_VALUE = "100nF"
LDO_INPUT_CAP_VALUE = "1uF"
LDO_OUTPUT_CAP_VALUE = "2.2uF"
BOOTSTRAP_CAP_VALUE = "100nF"
BOOTSTRAP_DIODE_VALUE = "1N4148"


def crystal_load_cap_pf(attrs: Dict[str, Any]) -> float:
    """Derived load-cap value: C = 2*(CL - Cstray) from the crystal part's
    own declared load capacitance — a derivation, never a magic number. A
    crystal part that omits either attr is a structured error (law 10: a
    load capacitance is never silently defaulted)."""
    missing = sorted(k for k in ("cl_pf", "cstray_pf") if k not in attrs)
    if missing:
        raise ModellerError(
            "RESOLVE_MISSING_ATTR",
            "crystal load-cap derivation needs the part's declared "
            "cl_pf/cstray_pf attrs; %s missing — the library refuses to "
            "invent a load capacitance" % ", ".join(missing))
    return 2.0 * (float(attrs["cl_pf"]) - float(attrs["cstray_pf"]))


# ---------------------------------------------------------------------------
# Per-kind builders (the library proper). Each takes the layer-1 role dict
# plus the pools it owns and returns the part prototype list, or None when
# the kind is COMPOSITE (realized by its children, no part of its own).
# ---------------------------------------------------------------------------

def _demands(role: Dict[str, Any]) -> List[Dict[str, Any]]:
    return role.get("demands", []) or []


def _caps(role: Dict[str, Any]) -> List[Dict[str, Any]]:
    return role.get("capabilities", []) or []


def _power_demand(role: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for d in _demands(role):
        if d["iface"] == "power":
            return d
    return None


def _build_supply(role, pools) -> List[PartProto]:
    cap = next(c for c in _caps(role) if c["iface"] == "power")
    v = float(cap["volts"])
    return [PartProto(
        value="SRC-%gV" % v, kind="source", prefix="PS",
        terminals=[term("VOUT", "power_out", prov_v=v),
                   term("GND", "ground")],
        attrs={"voltage": v},
        rail_pins=[("VOUT", v, cap["rail"])],
        ground_pins=["GND"])]


def _build_regulator(role, pools) -> List[PartProto]:
    dem = _power_demand(role)
    cap = next(c for c in _caps(role) if c["iface"] == "power")
    vin = float(dem["volts"])
    vout = float(cap["volts"])
    return [PartProto(
        value="LDO-%g-%gV" % (vin, vout), kind="ldo", prefix="U",
        terminals=[term("VIN", "power_in", req_v=vin),
                   term("VOUT", "power_out", prov_v=vout),
                   term("GND", "ground")],
        power_pins=[("VIN", vin)],
        rail_pins=[("VOUT", vout, cap["rail"])],
        ground_pins=["GND"])]


def _build_mcu(role, pools) -> List[PartProto]:
    """FACT-DRIVEN: pins/value derive from what the role declares."""
    dem = _power_demand(role)
    vdd = float(dem["volts"]) if dem and dem.get("volts") is not None else None
    terminals = [term("VDD", "power_in", req_v=vdd), term("GND", "ground")]
    proto = PartProto(
        value="MCU", kind="mcu", prefix="U", terminals=terminals,
        power_pins=[("VDD", vdd)], ground_pins=["GND"])
    suffix = []

    if any(c["iface"] == "i2c_master" or c["iface"] == "i2c"
           for c in _caps(role)):
        terminals.append(term("SDA", "signal", iface="i2c",
                              iface_member="sda"))
        terminals.append(term("SCL", "signal", iface="i2c",
                              iface_member="scl"))
        proto.i2c_pins = {"sda": "SDA", "scl": "SCL"}
        suffix.append("I2C")

    for p in pools:
        if p["provides"] == "uart":
            units = []
            for u in range(int(p["unit_count"])):
                tx, rx = "U%d_TX" % u, "U%d_RX" % u
                terminals.append(term(tx, "signal", iface="uart",
                                      iface_member="provide"))
                terminals.append(term(rx, "signal", iface="uart",
                                      iface_member="provide"))
                units.append({"tx": tx, "rx": rx})
            proto.pool_units["uart"] = units
            suffix.append("%dUART" % len(units))
        elif p["provides"] == "pwm":
            units = []
            for u in range(int(p["unit_count"])):
                pin = "PWM%d" % u
                terminals.append(term(pin, "signal", iface="pwm",
                                      iface_member="provide"))
                units.append({"pwm": pin})
            proto.pool_units["pwm"] = units
            suffix.append("PWM%d" % len(units))
        elif p["provides"] == "nand":
            units = []
            for u in range(int(p["unit_count"])):
                a, b, y = "A%d" % (u + 1), "B%d" % (u + 1), "Y%d" % (u + 1)
                terminals.append(term(a, "logic_in"))
                terminals.append(term(b, "logic_in"))
                terminals.append(term(y, "logic_out", iface="nand",
                                      iface_member="provide"))
                units.append({"a": a, "b": b, "y": y})
            proto.pool_units["nand"] = units

    if any(d["iface"] == "oscillator" for d in _demands(role)):
        terminals.append(term("OSC1", "passive", iface="oscillator",
                              iface_member="require"))
        terminals.append(term("OSC2", "passive", iface="oscillator",
                              iface_member="require"))
        proto.demand_units["oscillator"] = [{"p1": "OSC1", "p2": "OSC2"}]
        suffix.append("XTAL")

    proto.value = "MCU" + ("-" + "-".join(suffix) if suffix else "-GEN")
    return [proto]


def _build_sensor(role, pools) -> List[PartProto]:
    dem = _power_demand(role)
    vdd = float(dem["volts"])
    attrs: Dict[str, Any] = {}
    for d in _demands(role):
        if "i2c_addr" in d.get("attrs", {}):
            attrs["i2c_addr"] = d["attrs"]["i2c_addr"]
    return [PartProto(
        value="TMP-I2C", kind="sensor", prefix="U",
        terminals=[term("VDD", "power_in", req_v=vdd),
                   term("GND", "ground"),
                   term("SDA", "signal", iface="i2c", iface_member="sda"),
                   term("SCL", "signal", iface="i2c", iface_member="scl")],
        attrs=attrs,
        power_pins=[("VDD", vdd)], ground_pins=["GND"],
        i2c_pins={"sda": "SDA", "scl": "SCL"})]


def _build_uart_device(role, pools) -> List[PartProto]:
    dem = _power_demand(role)
    vdd = float(dem["volts"])
    return [PartProto(
        value="uart-peripheral", kind="uart_device", prefix="U",
        terminals=[term("VDD", "power_in", req_v=vdd),
                   term("GND", "ground"),
                   term("TX", "signal", iface="uart",
                        iface_member="require"),
                   term("RX", "signal", iface="uart",
                        iface_member="require")],
        power_pins=[("VDD", vdd)], ground_pins=["GND"],
        demand_units={"uart": [{"tx": "TX", "rx": "RX"}]})]


def _build_gpio_bitbang(role, pools) -> List[PartProto]:
    """The 5b alternative provider: present, honestly labelled, never
    silently wired to the demand it is NOT declared equivalent for."""
    return [PartProto(
        value="SW-UART", kind="uart_device", prefix="U",
        terminals=[term("TXO", "signal", iface="uart_bitbang",
                        iface_member="provide"),
                   term("RXI", "signal", iface="uart_bitbang",
                        iface_member="provide")])]


def _build_load(role, pools) -> List[PartProto]:
    dem = _power_demand(role)
    v = float(dem["volts"])
    return [PartProto(
        value="RLOAD", kind="resistor", prefix="R",
        terminals=[term("A", "power_in", req_v=v), term("B", "ground")],
        power_pins=[("A", v)], ground_pins=["B"])]


def _build_crystal(role, pools) -> List[PartProto]:
    return [PartProto(
        value="XTAL-8MHz", kind="crystal", prefix="Y",
        terminals=[term("X1", "passive", iface="oscillator",
                        iface_member="provide"),
                   term("X2", "passive", iface="oscillator",
                        iface_member="provide")],
        attrs={"cl_pf": 12.5, "cstray_pf": 2.5},
        provide_units={"oscillator": [{"p1": "X1", "p2": "X2"}]})]


def _build_quad_nand(role, pools) -> List[PartProto]:
    dem = _power_demand(role)
    vdd = float(dem["volts"])
    pool = next(p for p in pools if p["provides"] == "nand")
    n = int(pool["unit_count"])
    terminals = [term("VDD", "power_in", req_v=vdd), term("GND", "ground")]
    units = []
    for u in range(n):
        a, b, y = "A%d" % (u + 1), "B%d" % (u + 1), "Y%d" % (u + 1)
        terminals.append(term(a, "logic_in"))
        terminals.append(term(b, "logic_in"))
        terminals.append(term(y, "logic_out", iface="nand",
                              iface_member="provide"))
        units.append({"a": a, "b": b, "y": y})
    return [PartProto(
        value="74HC00" if n == 4 else "NANDx%d" % n,
        kind="logic_gate", logic_fn="nand", prefix="U",
        terminals=terminals,
        power_pins=[("VDD", vdd)], ground_pins=["GND"],
        pool_units={"nand": units})]


def _build_glue_logic(role, pools) -> List[PartProto]:
    """A consumer of NAND units: per demanded unit it drives the gate's
    inputs and consumes its output (the typed port set of one unit, seen
    from the demand side)."""
    total = sum(int(d.get("qty", 1)) for d in _demands(role)
                if d["iface"] == "nand")
    terminals: List[Dict[str, Any]] = []
    units = []
    for k in range(total):
        a, b, y = "N%d_A" % k, "N%d_B" % k, "N%d_Y" % k
        terminals.append(term(a, "logic_out"))
        terminals.append(term(b, "logic_out"))
        terminals.append(term(y, "logic_in", iface="nand",
                              iface_member="require"))
        units.append({"a": a, "b": b, "y": y})
    return [PartProto(
        value="GLUE-%dN" % total, kind="logic_gate", prefix="U",
        terminals=terminals,
        demand_units={"nand": units})]


def _build_bridge_leg(role, pools) -> List[PartProto]:
    """The multi-part binding (Gen4 section 3, #8: "bind MOSFETs/driver/
    logic parts"): one power MOSFET + one gate driver per leg. The driver is
    a logic_gate/buf so the mutual-exclusion model checker traces through it
    to the physical gate node the invariant anchors on."""
    dem = _power_demand(role)   # gate_pwr
    vdrv = float(dem["volts"])
    nfet = PartProto(
        value="NFET-100V", kind="mosfet", prefix="Q",
        terminals=[term("G", "signal"),
                   term("D", "power_in"),
                   term("S", "passive")],
        signals={"gate": "G"})
    gdrv = PartProto(
        value="GDRV-BUF", kind="logic_gate", logic_fn="buf", prefix="U",
        terminals=[term("VCC", "power_in", req_v=vdrv),
                   term("GND", "ground"),
                   term("IN", "logic_in"),
                   term("OUT", "logic_out"),
                   term("VB", "passive", iface="bootstrap",
                        iface_member=None),
                   term("VS", "passive")],
        power_pins=[("VCC", vdrv)], ground_pins=["GND"])
    return [nfet, gdrv]


_BUILDERS = {
    "supply": _build_supply,
    "regulator": _build_regulator,
    "mcu": _build_mcu,
    "sensor": _build_sensor,
    "uart_device": _build_uart_device,
    "gpio_bitbang": _build_gpio_bitbang,
    "load": _build_load,
    "crystal": _build_crystal,
    "logic": _build_quad_nand,
    "glue_logic": _build_glue_logic,
    "bridge_leg": _build_bridge_leg,
}

# Role kinds that are COMPOSITES: realized entirely by their children (plus
# the module templates below); they never bind a part of their own.
COMPOSITE_KINDS = frozenset({
    "half_bridge_module", "three_phase_bridge", "sensor_module",
})


def register_builder(kind: str, builder) -> None:
    """Extend the library: declaration = registration (law 1) for part
    builders too. A corpus vocabulary module (e.g. the Watchy library)
    registers its kind-keyed builders on import; the ONE generic resolver
    then binds those kinds with zero resolver edits. Re-registering a kind
    is a structured load error — libraries never silently shadow each
    other."""
    if kind in _BUILDERS:
        raise ModellerError(
            "DUPLICATE_BUILDER",
            "a part builder for role kind %r is already registered" % kind)
    _BUILDERS[kind] = builder


def parts_for(role: Dict[str, Any],
              pools: List[Dict[str, Any]]) -> Optional[Realization]:
    """The library realization of one layer-1 role, or None when the kind
    is a composite (or simply unknown — the resolver decides whether that
    role needed a binding at all). Builders may return the M1/M2
    ``List[PartProto]`` shape or a full ``Realization``; both normalize to
    a Realization here."""
    builder = _BUILDERS.get(role.get("kind", ""))
    if builder is None:
        return None
    built = builder(role, pools)
    if built is None:
        return None
    if isinstance(built, Realization):
        return built
    return Realization(parts=list(built))


def bindable_kinds() -> Tuple[str, ...]:
    return tuple(sorted(_BUILDERS))


__all__ = [
    "PartProto", "Realization", "term", "parts_for", "bindable_kinds",
    "register_builder",
    "PAIRING", "BUS_MEMBERS", "COMPOSITE_KINDS",
    "I2C_PULLUP_OHMS", "I2C_SPEED_DEFAULT",
    "DECOUPLING_VALUE", "LDO_INPUT_CAP_VALUE", "LDO_OUTPUT_CAP_VALUE",
    "BOOTSTRAP_CAP_VALUE", "BOOTSTRAP_DIODE_VALUE",
    "crystal_load_cap_pf",
]
