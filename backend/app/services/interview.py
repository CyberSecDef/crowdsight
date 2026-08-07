"""Phase 7 Step 3 — asking an agent a question.

The spec calls this the feature that turns a simulation into an instrument you
can probe, and it is right: everything else reports what the population *did*,
and this is the only way to ask why.

**An interview observes; it does not intervene.** OASIS's ``perform_interview``
reads the agent's memory to build the prompt and then calls the model directly,
deliberately sidestepping ``astep`` so nothing is written back. Upstream's own
comment says as much. The agent's later behaviour is therefore unchanged by
having been questioned, which is what separates an instrument from a nudge — and
it is a property worth stating plainly, because a reader would reasonably assume
the opposite.

**It needs a live process.** The agents and their accumulated memory exist only
inside the running worker; a finished run has profiles and posts but nobody to
ask. Interviewing one is refused rather than answered from a reconstruction,
because a reconstructed persona has none of the memory that makes the answer
worth having and would be indistinguishable from the real thing in the response.

**Interviews already conducted survive the run.** OASIS records each one to the
trace table as an ``interview`` action carrying both prompt and response, so
history is readable long after the process is gone — from the same database
every other endpoint reads.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Sequence

from app.services.run_reader import RunReader
from app.services.simulation_ipc import IPCError

logger = logging.getLogger(__name__)

__all__ = [
    "INTERVIEW_ACTION",
    "InterviewError",
    "NoSuchAgent",
    "SimulationNotLive",
    "conduct",
    "history",
    "interviewable",
]

#: What OASIS writes to the trace table for an interview.
INTERVIEW_ACTION = "interview"

#: An interview shares the GPU with a round in progress, so it is given a
#: generous window: under load a single completion can take minutes.
INTERVIEW_TIMEOUT = 600.0

MAX_QUESTION = 2_000


class InterviewError(RuntimeError):
    """The interview could not be conducted."""


class SimulationNotLive(InterviewError):
    """There is no running process holding the agents."""


class NoSuchAgent(InterviewError):
    """Nobody in this simulation has that id."""


def interviewable(sim_dir: Any) -> dict[int, str]:
    """Agent ids that can be interviewed, mapped to their usernames.

    The broadcaster is excluded: it is a synthetic news account with no
    persona and nothing to say about how it feels. Resolved from the population
    file rather than by asking the worker, so an unknown id is a clean 404
    instead of a round trip that comes back as a transport failure.
    """
    reader = RunReader(sim_dir)
    return {agent_id: identity.username
            for agent_id, identity in reader.identities().items()
            if identity.population}


def conduct(
    manager: Any,
    sim_id: str,
    question: str,
    *,
    agents: Sequence[int] | None = None,
    concurrency: int | None = None,
    timeout: float = INTERVIEW_TIMEOUT,
) -> dict[str, Any]:
    """Put a question to one or more agents inside the running simulation.

    Blocks until every answer is back. The caller decides whether that happens
    on a request thread — a single interview — or in a background task, which
    is what the bulk endpoints do.
    """
    question = (question or "").strip()
    if not question:
        raise InterviewError("An interview needs a question")
    if len(question) > MAX_QUESTION:
        raise InterviewError(
            f"That question is {len(question)} characters; the limit is {MAX_QUESTION}")

    if not manager.is_running(sim_id):
        raise SimulationNotLive(
            f"Simulation {sim_id} is not running. Agents and their memory live in "
            f"the running process, so there is nobody to ask; past interviews are "
            f"still readable through the history endpoint."
        )

    client = manager.client(sim_id)
    client.timeout = timeout
    payload: dict[str, Any] = {"question": question}
    if agents is not None:
        payload["agents"] = list(agents)
    if concurrency:
        payload["concurrency"] = int(concurrency)

    try:
        return client.request("interview", **payload)
    except IPCError as exc:
        raise InterviewError(f"The simulation did not answer: {exc}") from exc


def history(
    sim_dir: Any,
    *,
    agent: int | None = None,
    limit: int = 50,
    offset: int = 0,
    order: str = "newest",
) -> dict[str, Any]:
    """Interviews already conducted, from the run's own database.

    Readable whether or not the run is still going, because OASIS writes each
    interview to the trace table as it happens.
    """
    reader = RunReader(sim_dir)
    if not reader.exists:
        return {"sim_id": reader.sim_dir.name, "total": 0, "count": 0,
                "limit": limit, "offset": offset, "has_more": False,
                "next_offset": None, "interviews": []}

    where = ["LOWER(action) = ?"]
    params: list[Any] = [INTERVIEW_ACTION]
    if agent is not None:
        where.append("user_id = ?")
        params.append(int(agent))

    reader.ensure_indexes()
    rows, total = reader._page(  # noqa: SLF001 - same package, one paging path
        table="trace", columns="user_id, created_at, action, info",
        where=where, params=params, limit=limit, offset=offset, order=order)

    identities = reader.identities()
    bounds = reader._round_bounds("trace")  # noqa: SLF001
    interviews = []
    for row in rows:
        prompt, response = _split(row["info"])
        identity = identities.get(int(row["user_id"]))
        interviews.append({
            "user_id": int(row["user_id"]),
            "username": identity.username if identity else "",
            "name": identity.name if identity else "",
            "question": prompt,
            "response": response,
            "round": reader._round_of(int(row["rid"]), bounds),  # noqa: SLF001
            "created_at": row["created_at"],
        })
    return reader._envelope(  # noqa: SLF001
        rows=interviews, total=total, limit=limit, offset=offset,
        order=order, interviews=interviews)


def _split(info: Any) -> tuple[str, str]:
    """Pull the question and answer out of a trace row.

    OASIS stores the pair as JSON, but the column is free text and a row
    written by a different version may not be, so this never raises.
    """
    if isinstance(info, dict):
        data = info
    elif isinstance(info, str) and info:
        try:
            data = json.loads(info)
        except ValueError:
            return "", info
    else:
        return "", ""
    if not isinstance(data, dict):
        return "", str(data)
    return str(data.get("prompt") or ""), str(data.get("response") or "")


def count_for(database_path: Any) -> int:
    """How many interviews a run holds. Cheap enough for a listing."""
    try:
        with sqlite3.connect(database_path) as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM trace WHERE LOWER(action) = ?",
                (INTERVIEW_ACTION,)).fetchone()[0])
    except sqlite3.Error:
        return 0
