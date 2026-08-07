"""Process-wide clients shared by every request and every job.

The Neo4j driver owns a connection pool and the LLM client owns an HTTP pool;
constructing either per request defeats both. They are built once, lazily, and
handed out.

Flask handlers are synchronous while the whole storage and inference layer is
async, so the runtime also owns the bridge: one background event loop, used
both to run jobs and to await coroutines from a request handler. A second loop
would only add contention, since the Ollama concurrency gate already bounds
what actually runs.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Awaitable

from app.config import Config, get_config
from app.services.graph_builder import GraphBuilder
from app.services.simulation_config_generator import SimulationConfigGenerator
from app.services.simulation_manager import SimulationManager
from app.services.simulation_store import SimulationStore
from app.services.tasks import TaskRunner, TaskStore
from app.storage.embedding_service import EmbeddingService
from app.storage.graph_storage import GraphStorage
from app.storage.neo4j_storage import Neo4jStorage
from app.storage.search_service import SearchService
from app.utils.llm_client import LLMClient

logger = logging.getLogger(__name__)

__all__ = ["Runtime", "get_runtime", "reset_runtime"]


class Runtime:
    """Lazily-built shared services."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        data_dir: str | Path | None = None,
        task_db: str | Path | None = None,
        sim_dir: str | Path | None = None,
    ) -> None:
        self.config = config or get_config()
        self.data_dir = Path(data_dir) if data_dir else Path("data/graphs")
        self.sim_dir = Path(sim_dir) if sim_dir else Path("data/simulations")
        self.tasks = TaskStore(task_db or "data/tasks.db")
        # Anything left running belonged to a previous process.
        self.tasks.reap_orphans()
        self.runner = TaskRunner(self.tasks)

        self.storage = Neo4jStorage(self.config)
        self.embeddings = EmbeddingService(self.config)
        self.llm = LLMClient(self.config)
        self.graphs = GraphStorage(self.storage, self.config, data_dir=self.data_dir)
        self.search = SearchService(
            self.storage, self.config, embeddings=self.embeddings, graphs=self.graphs
        )
        self.sims = SimulationStore(self.sim_dir)
        self.manager = SimulationManager(self.sims, config=self.config)
        # Anything recorded as running belongs to a previous incarnation of
        # this process: adopt what still answers, bury what does not.
        self.manager.reap_orphans()
        self.scenarios = SimulationConfigGenerator(self.config, llm=self.llm)
        self.builder = GraphBuilder(
            self.storage, self.config, data_dir=self.data_dir,
            embeddings=self.embeddings,
        )

    def run(self, coro: Awaitable[Any], timeout: float | None = 60.0) -> Any:
        """Await a coroutine from synchronous request-handling code."""
        return self.runner.run_sync(coro, timeout)

    def close(self) -> None:  # pragma: no cover - process teardown
        try:
            self.run(self.storage.aclose(), timeout=10)
            self.run(self.embeddings.aclose(), timeout=10)
            self.run(self.llm.aclose(), timeout=10)
        finally:
            self.runner.shutdown()
            self.tasks.close()


_runtime: Runtime | None = None
_lock = threading.Lock()


def get_runtime(**kwargs: Any) -> Runtime:
    global _runtime
    with _lock:
        if _runtime is None:
            _runtime = Runtime(**kwargs)
            logger.info("Runtime initialised (data_dir=%s)", _runtime.data_dir)
        return _runtime


def reset_runtime() -> None:
    """Drop the shared runtime. For tests."""
    global _runtime
    with _lock:
        if _runtime is not None:
            try:
                _runtime.close()
            except Exception:  # noqa: BLE001 - teardown is best-effort
                logger.debug("Runtime teardown raised", exc_info=True)
        _runtime = None
