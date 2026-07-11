"""Concept layer — the typed contracts that verify every connection.

A ``Concept`` is a structural interface contract (astichi-style): matching is
decided by STRUCTURE (interface family + parameters such as voltage), never by
identifier equality, and the check is TOTAL — every attempt returns either an
``Accepted`` (with the proof text) or a ``Rejected`` (with the reason naming
what failed). There is no third outcome and no exception path for a mere
mismatch, so the engine can enumerate candidates, keep every rejection reason,
and surface them verbatim inside an UNSAT-core-style escalation.

Composition law 9: concept checks are total compose-time VALUE checks — a
``Concept(iface="power", volts=3.3)`` demand against a 5 V rail is rejected by
``accepts()`` at elaboration time, not discovered at runtime by a string
compare, and not merely encoded in static types that would evaporate before
the emitted artifact (the gen-2 do-not-repeat #3).

Interface families: a provider satisfies a demand when its iface is the same
string OR a qualified member of the family — ``i2c_master`` provides ``i2c``,
``uart_bitbang`` provides ``uart``. This is the same convention the layer-1
oracle applies, but it is OUR compose-time rule, not a call into the oracle.

Pure Python 3 stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple, Union

# Voltage comparison tolerance (volts): absorbs float noise only, never a real
# rail difference. Matches the published oracle policy.
VTOL = 0.05


@dataclass(frozen=True)
class Accepted:
    """A successful structural match, carrying its proof text."""

    reason: str = "ok"

    def __bool__(self) -> bool:
        return True


@dataclass(frozen=True)
class Rejected:
    """A failed structural match, carrying the reason (never silent)."""

    reason: str

    def __bool__(self) -> bool:
        return False


CheckResult = Union[Accepted, Rejected]


def family_provides(wanted: str, provided: str) -> bool:
    """Interface-family rule: exact match, or a qualified provider of the
    same family (``i2c_master`` provides ``i2c``)."""
    return provided == wanted or provided.startswith(wanted + "_")


@dataclass(frozen=True)
class Concept:
    """One interface contract: an iface family plus optional parameters.

    ``iface``  the interface family name ("power", "uart", "i2c", "nand",
               "pwm", "oscillator", ... — open vocabulary).
    ``volts``  for power-like concepts: the contracted voltage. ``None``
               means unconstrained.
    """

    iface: str
    volts: Optional[float] = None

    def accepts(self, supply: "Concept") -> CheckResult:
        """TOTAL structural check: does ``supply`` satisfy this (demand-side)
        concept? Returns Accepted(proof) or Rejected(reason) — never raises
        for a mismatch."""
        if not family_provides(self.iface, supply.iface):
            return Rejected(
                "iface %r does not provide %r (not the same family)"
                % (supply.iface, self.iface))
        if (self.volts is not None and supply.volts is not None
                and abs(self.volts - supply.volts) > VTOL):
            return Rejected(
                "voltage contract violated: demand needs %gV, supply is %gV"
                % (self.volts, supply.volts))
        if self.volts is not None and supply.volts is None:
            return Rejected(
                "demand contracts %gV but the supply declares no voltage"
                % self.volts)
        proof = "%r provides %r" % (supply.iface, self.iface)
        if self.volts is not None:
            proof += " at %gV (within %gV)" % (supply.volts, VTOL)
        return Accepted(proof)


# ---------------------------------------------------------------------------
# Canonical typed port signatures per iface family — the typed port set of ONE
# pool unit (say-only-the-non-default: a pool derives its signature from its
# concept unless the author overrides). "name:type" strings per the emit
# contract; bundles swap whole, never split.
# ---------------------------------------------------------------------------

PORT_SIGNATURES = {
    "uart": ("tx:out", "rx:in"),
    "nand": ("a:in", "b:in", "y:out"),
    "pwm": ("pwm:out",),
    "i2c": ("sda:bidir", "scl:in"),
    "spi": ("sclk:in", "mosi:in", "miso:out", "cs:in"),
    "gpio": ("io:bidir",),
    "oscillator": ("osc:out",),
}


def signature_for(iface: str) -> Optional[Tuple[str, ...]]:
    """The canonical typed port signature of one unit of ``iface``, or None
    when the family has no canonical signature (the author must then declare
    one — never silently defaulted)."""
    return PORT_SIGNATURES.get(iface)


__all__ = [
    "Accepted",
    "Rejected",
    "CheckResult",
    "Concept",
    "family_provides",
    "signature_for",
    "PORT_SIGNATURES",
    "VTOL",
]
