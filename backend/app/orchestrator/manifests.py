"""Agent manifests: a declarative description of every agent (config/agents.yaml), validated once at load.

Plain data and plain code. No model calls and no router import: the referee reads this to decide who may do what.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

ARTIFACT_TYPES = (
    "source_pack", "scope", "outline", "resource_pack", "notes", "slide_deck", "diagram", "code_flowchart", "video", "links", "images",
    "quiz", "practice", "summary", "mastery", "proposal",
)
SHARED_TYPES = ("proposal", "video", "links", "images", "diagram")   # several agents may legitimately produce these
RELEASE_GATES = ("none", "verified", "quality", "proposal")
GATE_STRICTNESS = {"none": 0, "verified": 1, "quality": 2, "proposal": 3}
DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "agents.yaml"


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class Manifest:
    name: str
    layer: int
    model_role: str
    needs: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    may_request: tuple[str, ...] = ()
    concurrency: int = 1
    timeout_s: int = 60
    max_retries: int = 1
    release_gate: str = "none"


@dataclass
class Registry:
    agents: dict[str, Manifest] = field(default_factory=dict)

    def get(self, name: str) -> Manifest:
        try:
            return self.agents[name]
        except KeyError:
            raise ManifestError(f"unknown agent {name!r}") from None

    def producers(self, artifact_type: str) -> list[Manifest]:
        return [m for m in self.agents.values() if artifact_type in m.produces]

    def may_request(self, frm: str, to: str) -> bool:
        return to in self.get(frm).may_request

    def release_gate(self, artifact_type: str) -> str:
        """The strictest gate among the producers of this artifact type (proposals are never auto-released)."""
        gates = [m.release_gate for m in self.producers(artifact_type)] or ["none"]
        return max(gates, key=lambda g: GATE_STRICTNESS.get(g, len(GATE_STRICTNESS)))     # an unknown gate counts as the strictest


def parse(raw: dict, known_roles: Optional[set[str]] = None) -> Registry:
    reg = Registry()
    for name, spec in (raw.get("agents") or {}).items():
        spec = dict(spec or {})
        for key in ("needs", "produces", "may_request"):
            spec[key] = tuple(spec.get(key) or ())
        try:
            reg.agents[name] = Manifest(name=name, **spec)
        except TypeError as e:
            raise ManifestError(f"{name}: {e}") from None
    problems: list[str] = []
    for m in reg.agents.values():
        for t in (*m.needs, *m.produces):
            if t not in ARTIFACT_TYPES:
                problems.append(f"{m.name}: unknown artifact type {t!r}")
        for peer in m.may_request:
            if peer not in reg.agents:
                problems.append(f"{m.name}: may_request names unknown agent {peer!r}")
            elif peer == m.name:
                problems.append(f"{m.name}: cannot request itself")
        if m.release_gate not in RELEASE_GATES:
            problems.append(f"{m.name}: release_gate {m.release_gate!r} is not one of {RELEASE_GATES}")
        if m.concurrency < 1 or m.timeout_s < 1 or m.max_retries < 0:
            problems.append(f"{m.name}: concurrency and timeout_s must be at least 1, max_retries at least 0")
        if known_roles is not None and m.model_role not in known_roles:
            problems.append(f"{m.name}: model_role {m.model_role!r} is not in config/models.yaml")
    for t in ARTIFACT_TYPES:
        owners = [m.name for m in reg.producers(t)]
        if len(owners) > 1 and t not in SHARED_TYPES:
            problems.append(f"artifact type {t!r} has several producers: {', '.join(owners)}")
    made = {t for m in reg.agents.values() for t in m.produces}
    for m in reg.agents.values():
        for t in m.needs:
            if t in ARTIFACT_TYPES and t not in made:
                problems.append(f"{m.name}: needs {t!r}, which no agent produces")
    problems += _cycles(reg)
    if problems:
        raise ManifestError("; ".join(problems))
    return reg


def _cycles(reg: Registry) -> list[str]:
    """Agents wait on the artifacts others produce, so a loop in needs -> produces would deadlock the board before it starts."""
    deps = {m.name: {p.name for t in m.needs for p in reg.producers(t) if p.name != m.name} for m in reg.agents.values()}
    state: dict[str, int] = {}
    out: list[str] = []

    def visit(n: str, path: list[str]) -> None:
        state[n] = 1
        for d in sorted(deps[n]):
            if state.get(d) == 1:
                out.append("needs/produces cycle: " + " -> ".join([*path[path.index(d):], d] if d in path else [n, d]))
            elif d not in state:
                visit(d, [*path, d])
        state[n] = 2
    for n in deps:
        if n not in state:
            visit(n, [n])
    return out


def load(path: Path | str = DEFAULT_PATH, known_roles: Optional[set[str]] = None) -> Registry:
    return parse(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}, known_roles)
