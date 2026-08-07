"""Phase 5 Step 3 — where a scenario lives between generation and the run.

A generated scenario is frequently *almost* right, so the config is written to
``data/simulations/<sim_id>/config.json`` and handed to an operator before the
engine touches it. Three properties make that safe rather than merely possible.

**An edit is re-verified exactly like generated output.** An operator can
attribute an invented sentence to a real named person just as easily as a model
can — more easily, since they can also type in ``source_start`` and
``source_end`` by hand and make a fabrication look evidenced. Edits therefore
run through the same :func:`verify_scenario`, offsets are recomputed rather than
believed, and an unlocatable quote is demoted to the broadcaster. What changed
is reported back, so a correction is never silent.

**A started run's config is frozen.** Editing the file a run is executing from
would leave the report describing conditions that never held. Editing a started
simulation forks it: the original is preserved untouched and the edit lands in a
new ``sim_id`` that records what it came from.

**Run state lives outside the operator's file.** ``config.json`` is the
scenario and nothing else; ``meta.json`` holds lifecycle. An operator editing
the scenario cannot corrupt run state, and a diff of two configs shows only
what a human actually changed.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field

from app.services.simulation_config_generator import (
    SimulationConfig,
    verify_scenario,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIG_FILE",
    "META_FILE",
    "PROFILES_DIR",
    "REQUEST_FILE",
    "EditResult",
    "SimulationMeta",
    "SimulationNotFound",
    "SimulationState",
    "SimulationStore",
    "new_sim_id",
]

DEFAULT_SIM_DIR = Path("data/simulations")
CONFIG_FILE = "config.json"
META_FILE = "meta.json"
REQUEST_FILE = "request.json"
PROFILES_DIR = "profiles"

#: ``sim-20260805-143022-a1b2c3``. Sorts chronologically in a directory
#: listing, which is how an operator actually finds a run, and the random tail
#: keeps two simulations started in the same second apart.
SIM_ID_PATTERN = re.compile(r"^sim-\d{8}-\d{6}-[0-9a-f]{6}$")

State = Literal["draft", "running", "complete", "failed"]


class SimulationState:
    DRAFT: State = "draft"
    RUNNING: State = "running"
    COMPLETE: State = "complete"
    FAILED: State = "failed"

    #: States in which the config is frozen. A run's report has to describe the
    #: conditions the run actually executed under.
    LOCKED: frozenset[str] = frozenset({"running", "complete", "failed"})


class SimulationNotFound(LookupError):
    """No simulation with that id."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_sim_id(*, now: datetime | None = None, rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return f"sim-{stamp}-{rng.randrange(16 ** 6):06x}"


class SimulationMeta(BaseModel):
    """Lifecycle, kept out of the file the operator edits."""

    sim_id: str
    graph_id: str = ""
    platform: str = "twitter"
    state: State = SimulationState.DRAFT
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    started_at: str = ""
    finished_at: str = ""
    #: Set when this simulation was created by editing a locked one.
    forked_from: str = ""
    #: How many times an operator has saved the config.
    edits: int = 0
    #: Corrections applied the last time the config was edited.
    last_edit_changes: list[str] = Field(default_factory=list)

    @property
    def locked(self) -> bool:
        return self.state in SimulationState.LOCKED


@dataclass
class EditResult:
    """What happened to an operator's edit."""

    meta: SimulationMeta
    config: SimulationConfig
    #: Corrections verification made. Empty means the edit was taken as written.
    changes: list[str] = field(default_factory=list)
    #: True when the target was locked and the edit landed in a new simulation.
    forked: bool = False
    forked_from: str = ""

    @property
    def sim_id(self) -> str:
        return self.meta.sim_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "sim_id": self.sim_id,
            "forked": self.forked,
            "forked_from": self.forked_from,
            "changes": self.changes,
            "meta": self.meta.model_dump(),
            "config": self.config.model_dump(),
        }


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temporary file so a reader never sees half a config.

    The UI polls these files while an operator is saving them.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class SimulationStore:
    """The ``data/simulations`` directory."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else DEFAULT_SIM_DIR

    # -- paths --------------------------------------------------------------

    def sim_dir(self, sim_id: str) -> Path:
        if not SIM_ID_PATTERN.match(sim_id):
            # Also the path-traversal guard: sim_id arrives from the URL.
            raise SimulationNotFound(f"Not a simulation id: {sim_id!r}")
        return self.base_dir / sim_id

    def config_path(self, sim_id: str) -> Path:
        return self.sim_dir(sim_id) / CONFIG_FILE

    def meta_path(self, sim_id: str) -> Path:
        return self.sim_dir(sim_id) / META_FILE

    def exists(self, sim_id: str) -> bool:
        try:
            return self.config_path(sim_id).is_file()
        except SimulationNotFound:
            return False

    # -- create -------------------------------------------------------------

    def create(
        self,
        config: SimulationConfig,
        *,
        sim_id: str | None = None,
        forked_from: str = "",
    ) -> SimulationMeta:
        """Write a new simulation to disk and return its metadata."""
        sim_id = sim_id or self._unused_sim_id()
        meta = SimulationMeta(
            sim_id=sim_id, graph_id=config.graph_id, platform=config.platform,
            forked_from=forked_from,
        )
        self._write(sim_id, config, meta)
        logger.info(
            "Simulation %s created from graph %r (%s)",
            sim_id, config.graph_id or "-", config.summary(),
        )
        return meta

    def create_pending(
        self, *, graph_id: str, platform: str = "twitter", rounds: int | None = None,
        total_agents: int | None = None, named_ratio: float | None = None,
    ) -> SimulationMeta:
        """Reserve a simulation before there is anything to run.

        The API separates creating a simulation from preparing one, because
        preparing costs minutes of inference and an operator needs an id to
        watch it against. Only the request is recorded here; ``config.json``
        arrives when preparation finishes.
        """
        sim_id = self._unused_sim_id()
        meta = SimulationMeta(sim_id=sim_id, graph_id=graph_id, platform=platform)
        request = {
            "graph_id": graph_id, "platform": platform, "rounds": rounds,
            "total_agents": total_agents, "named_ratio": named_ratio,
        }
        _atomic_write(self.sim_dir(sim_id) / REQUEST_FILE,
                      json.dumps(request, indent=2))
        self.save_meta(meta)
        logger.info("Simulation %s reserved for graph %r", sim_id, graph_id)
        return meta

    def request(self, sim_id: str) -> dict[str, Any]:
        """What the operator asked for when the simulation was created."""
        path = self.sim_dir(sim_id) / REQUEST_FILE
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            logger.warning("Unreadable request for %s", sim_id)
            return {}

    def prepared(self, sim_id: str) -> bool:
        """True once there is a scenario to run."""
        try:
            return self.config_path(sim_id).is_file()
        except SimulationNotFound:
            return False

    def save_config(self, sim_id: str, config: SimulationConfig) -> None:
        """Attach a derived scenario to a simulation that was only reserved."""
        _atomic_write(self.config_path(sim_id), config.model_dump_json(indent=2))
        meta = self.load_meta(sim_id)
        meta.platform = config.platform
        meta.graph_id = config.graph_id or meta.graph_id
        meta.updated_at = _now()
        self.save_meta(meta)

    def profiles_dir(self, sim_id: str) -> Path:
        return self.sim_dir(sim_id) / PROFILES_DIR

    def _unused_sim_id(self, attempts: int = 8) -> str:
        for _ in range(attempts):
            candidate = new_sim_id()
            if not (self.base_dir / candidate).exists():
                return candidate
        raise RuntimeError("Could not allocate an unused simulation id")

    # -- read ---------------------------------------------------------------

    def load_config(self, sim_id: str) -> SimulationConfig:
        path = self.config_path(sim_id)
        if not path.is_file():
            raise SimulationNotFound(f"No simulation {sim_id!r}")
        return SimulationConfig.load(path)

    def load_meta(self, sim_id: str) -> SimulationMeta:
        path = self.meta_path(sim_id)
        if not path.is_file():
            if (self.sim_dir(sim_id) / REQUEST_FILE).is_file():
                raise SimulationNotFound(
                    f"Simulation {sim_id!r} has a request but no metadata")
            if self.config_path(sim_id).is_file():
                # A config written by hand, or metadata lost. The scenario is
                # the valuable half; rebuild the rest rather than 404.
                logger.warning("Simulation %s has no %s; rebuilding", sim_id, META_FILE)
                config = self.load_config(sim_id)
                meta = SimulationMeta(sim_id=sim_id, graph_id=config.graph_id,
                                      platform=config.platform)
                _atomic_write(path, meta.model_dump_json(indent=2))
                return meta
            raise SimulationNotFound(f"No simulation {sim_id!r}")
        return SimulationMeta.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self, *, graph_id: str | None = None, limit: int = 100) -> list[SimulationMeta]:
        """Newest first — the id sorts chronologically, so this is a reverse sort."""
        if not self.base_dir.is_dir():
            return []
        out: list[SimulationMeta] = []
        for entry in sorted(self.base_dir.iterdir(), reverse=True):
            if not entry.is_dir() or not SIM_ID_PATTERN.match(entry.name):
                continue
            try:
                meta = self.load_meta(entry.name)
            except (SimulationNotFound, ValueError) as exc:
                logger.warning("Skipping unreadable simulation %s: %s", entry.name, exc)
                continue
            if graph_id and meta.graph_id != graph_id:
                continue
            out.append(meta)
            if len(out) >= limit:
                break
        return out

    # -- edit ---------------------------------------------------------------

    def update_config(
        self,
        sim_id: str,
        payload: dict[str, Any] | SimulationConfig,
        *,
        document: str = "",
        named_entities: Sequence[str] = (),
    ) -> EditResult:
        """Apply an operator's edit, re-verified against the source.

        ``document`` and ``named_entities`` come from the graph the scenario was
        derived from. Without them the quote check cannot run, so a config
        carrying ``named_quote`` posts is refused rather than accepted
        unverified — an unverifiable edit must not be able to buy itself
        acceptance by withholding the evidence.

        A locked simulation is never modified. The edit is written to a new
        simulation instead, and ``EditResult.forked`` says so.
        """
        meta = self.load_meta(sim_id)

        if isinstance(payload, SimulationConfig):
            config = payload.model_copy(deep=True)
        else:
            # Trusted: an operator may narrow the action space, unlike the
            # generating model, whose action_space is discarded.
            config = SimulationConfig.model_validate(payload, context={"trusted": True})

        if meta.graph_id and config.graph_id != meta.graph_id:
            # The scenario belongs to the graph it was derived from. Allowing
            # this would verify quotes against a different document than the
            # one the simulation is about, and leave meta pointing elsewhere.
            raise ValueError(
                f"This simulation was derived from graph {meta.graph_id!r} and "
                f"cannot be repointed at {config.graph_id!r}; derive a new one instead"
            )

        quoted = [p for p in config.seed_posts if p.attribution == "named_quote"]
        if quoted and not document:
            raise ValueError(
                "This edit attributes a quote to a named person, but the source "
                "document is not available to check it against. Attach the "
                "document or use the broadcaster attribution."
            )

        changes = verify_scenario(config, document, named_entities)

        if meta.locked:
            forked = self.create(config, forked_from=sim_id)
            forked.edits = 1
            forked.last_edit_changes = changes
            self.save_meta(forked)
            logger.info(
                "Simulation %s is %s; edit forked into %s",
                sim_id, meta.state, forked.sim_id,
            )
            return EditResult(meta=forked, config=config, changes=changes,
                              forked=True, forked_from=sim_id)

        meta.edits += 1
        meta.last_edit_changes = changes
        meta.platform = config.platform
        meta.updated_at = _now()
        self._write(sim_id, config, meta)
        return EditResult(meta=meta, config=config, changes=changes)

    # -- lifecycle ----------------------------------------------------------

    def mark_started(self, sim_id: str) -> SimulationMeta:
        """Freeze the config. Called by the engine as a run begins."""
        meta = self.load_meta(sim_id)
        meta.state = SimulationState.RUNNING
        meta.started_at = _now()
        meta.updated_at = meta.started_at
        self.save_meta(meta)
        return meta

    def mark_finished(self, sim_id: str, *, failed: bool = False) -> SimulationMeta:
        meta = self.load_meta(sim_id)
        meta.state = SimulationState.FAILED if failed else SimulationState.COMPLETE
        meta.finished_at = _now()
        meta.updated_at = meta.finished_at
        self.save_meta(meta)
        return meta

    def save_meta(self, meta: SimulationMeta) -> None:
        _atomic_write(self.meta_path(meta.sim_id), meta.model_dump_json(indent=2))

    def _write(
        self, sim_id: str, config: SimulationConfig, meta: SimulationMeta
    ) -> None:
        _atomic_write(self.config_path(sim_id), config.model_dump_json(indent=2))
        _atomic_write(self.meta_path(sim_id), meta.model_dump_json(indent=2))

    def describe(self, sim_id: str) -> dict[str, Any]:
        """Everything the review UI needs for one simulation.

        A simulation that has been created but not yet prepared has no scenario
        yet, and saying so is more useful than a 404 for something that does
        exist.
        """
        meta = self.load_meta(sim_id)
        if not self.prepared(sim_id):
            return {
                "meta": meta.model_dump(), "config": None, "prepared": False,
                "request": self.request(sim_id), "summary": "not yet prepared",
                "warnings": [], "editable": False,
            }
        config = self.load_config(sim_id)
        return {
            "meta": meta.model_dump(),
            "config": config.model_dump(),
            "prepared": True,
            "summary": config.summary(),
            "warnings": config.warnings(),
            "editable": not meta.locked,
        }
