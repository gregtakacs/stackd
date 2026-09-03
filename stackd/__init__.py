"""stackd — two-device model stack/profile orchestrator.

See ``DESIGN.md`` / the design note for the architecture. This package is at
Phase 1 (declarative core): config schema, engine adapter interface, the
two-pool fit validator, and delta-reconciliation planning. Engine lifecycle
(spawn/stop/health) and the reactive reconciler land in Phase 2.
"""

__version__ = "0.0.1"
