"""Milestone 4 foundation: the shared board, the agent bus, manifests, the referee and the decision emitter.

Plain code. Nothing here calls a model or imports the model router; agents (which do) plug in later through the board and the bus.
"""
from .board import REFEREE, Board, BoardError, StaleTransition  # noqa: F401
from .bus import MESSAGE_TYPES, Bus, LoopStopped, new_correlation_id  # noqa: F401
from .decisions import DecisionEmitter  # noqa: F401
from .manifests import Manifest, ManifestError, Registry, load as load_manifests  # noqa: F401
from .referee import Budgets, Referee, orchestrator_mode  # noqa: F401
