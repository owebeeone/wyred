"""ga019 modeller — the ElecScad gen-4 intent-modelling substrate (M1).

One ``Module`` base closed under composition; marker declarations harvested
from class bodies; concept-typed ports verified by total structural
``accepts()`` checks; scoped rails/grounds with a nearest-wins cascade;
declared pools with symbolic, deterministically-allocated units; lock groups
and series; layer-2 refinements that never edit layer 1; and a generic
elaboration engine emitting layer-1 JSON per rl/harness/EMIT_CONTRACT.md.

Surface re-exports — a corpus file needs only::

    from wyred import (Module, Refinement, param, demand, provide, rail,
                       ground, bond, bus, pool, mutual_exclusion,
                       lock_group, use, late, pin, bind)
"""

from .concepts import (Accepted, Concept, Rejected, family_provides,
                       signature_for, VTOL)
from .core import (Module, ModellerError, Refinement, bind, bond, bus,
                   demand, ground, late, lock_group, mutual_exclusion, param,
                   pin, pool, provide, rail, use,
                   DECISION_CLASSES, INTENTS, MODULES, REFINEMENTS)
from .engine import (Diagnostic, EmitResult, SOLVER_VERSION, elaborate,
                     exemplify)
from .resolve import ResolveResult, resolve

__all__ = [
    "Accepted", "Concept", "Rejected", "family_provides", "signature_for",
    "VTOL",
    "Module", "ModellerError", "Refinement", "bind", "bond", "bus", "demand",
    "ground", "late", "lock_group", "mutual_exclusion", "param", "pin",
    "pool", "provide", "rail", "use",
    "DECISION_CLASSES", "INTENTS", "MODULES", "REFINEMENTS",
    "Diagnostic", "EmitResult", "SOLVER_VERSION", "elaborate", "exemplify",
    "ResolveResult", "resolve",
]
