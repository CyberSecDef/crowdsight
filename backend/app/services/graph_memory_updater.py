"""Phase 6 Step 4 — simulation outcomes back into the graph, and back out again.

**Off by default, and the spec is right to be wary of it.** It roughly doubles
graph writes, adds a query per round, and — because it closes the loop into the
agents' prompts — changes what the simulation measures. A run with
``GRAPH_MEMORY_FEEDBACK`` on is not comparable with one without it, so the flag
is not a performance knob but a statement about what experiment is being run.

**Simulated content is fabricated, and is kept visibly apart from the document.**
Everything written here carries its own ``Sim*`` labels and a ``sim_id``, and is
never merged into the ``:Entity`` nodes extracted from the source. A simulated
post may link *to* a real entity — a narrative is about Councillor Doe — through
an edge named for exactly that, so an analysis can relate the two without any
query for document facts silently picking up invented ones. This is the same
line Phases 4 and 5 spent their effort drawing: reproducing what a document says
is not fabrication, and inventing statements for a named person is.

**Significance is engagement, computed rather than judged.** Asking the model
each round which narratives emerged would be richer, but it puts another
inference in the critical path of a GPU the agents have already saturated, and
makes the result unreproducible. Likes, reposts and replies are already counted
in the run's own database, cost nothing, and give the same round twice the same
answer.

**The loop is closed with one query per round, not one per agent.** The naive
reading — each agent consults the graph on its turn — is a Neo4j round trip per
agent per round, three hundred of them in a round that is already the expensive
part. Instead the round's context is fetched once and handed to every agent, so
closing the loop costs one query rather than a population's worth.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from app.config import Config, get_config
from app.storage.ner_extractor import normalise_name

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.storage.neo4j_storage import Neo4jStorage

logger = logging.getLogger(__name__)

__all__ = [
    "GraphMemoryUpdater",
    "RoundOutcome",
    "SimPost",
    "engagement_of",
]


@dataclass
class SimPost:
    """One post that drew enough attention to be worth remembering."""

    post_id: int
    user_id: int
    username: str
    content: str
    round: int
    likes: int = 0
    dislikes: int = 0
    reposts: int = 0
    comments: int = 0
    mentions: list[str] = field(default_factory=list)

    @property
    def engagement(self) -> int:
        return self.likes + self.dislikes + self.reposts + self.comments

    def to_dict(self) -> dict[str, Any]:
        return {
            "post_id": self.post_id, "user_id": self.user_id,
            "username": self.username, "content": self.content,
            "round": self.round, "likes": self.likes, "dislikes": self.dislikes,
            "reposts": self.reposts, "comments": self.comments,
            "engagement": self.engagement, "mentions": self.mentions,
        }


@dataclass
class RoundOutcome:
    """What one round contributed to the graph."""

    round: int
    posts: list[SimPost] = field(default_factory=list)
    follows: list[tuple[int, int]] = field(default_factory=list)
    nodes_written: int = 0
    edges_written: int = 0

    def summary(self) -> str:
        return (f"round {self.round}: {len(self.posts)} notable post(s), "
                f"{len(self.follows)} follow(s), {self.nodes_written} node(s), "
                f"{self.edges_written} edge(s)")


def engagement_of(row: Any) -> int:
    return int(row["num_likes"] or 0) + int(row["num_dislikes"] or 0) + \
        int(row["num_shares"] or 0)


class GraphMemoryUpdater:
    """Reads a round's outcomes from SQLite, writes them to Neo4j, reads back."""

    def __init__(
        self,
        storage: "Neo4jStorage",
        *,
        config: Config | None = None,
    ) -> None:
        self.config = config or get_config()
        self.storage = storage
        self.top_n = self.config.GRAPH_MEMORY_TOP_N
        self.min_engagement = self.config.GRAPH_MEMORY_MIN_ENGAGEMENT
        self._entity_names: dict[str, list[str]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.config.GRAPH_MEMORY_FEEDBACK)

    async def entity_names(self, graph_id: str) -> list[str]:
        """The document's entity names, fetched once per run and remembered.

        Read from the graph rather than a file so there is one source of truth,
        and cached because it does not change while a run is in flight.
        """
        if graph_id in self._entity_names:
            return self._entity_names[graph_id]
        try:
            rows = await self.storage.read(
                "MATCH (e:Entity {graph_id: $graph_id}) "
                "RETURN e.name AS name LIMIT 1000",
                graph_id=graph_id,
            )
            names = [str(row["name"]) for row in rows if row.get("name")]
        except Exception:  # noqa: BLE001 - linking is a nicety, not the run
            logger.exception("Could not read entity names for graph %r", graph_id)
            names = []
        self._entity_names[graph_id] = names
        return names

    # -- reading the round --------------------------------------------------

    def collect(
        self,
        database_path: str | Path,
        *,
        round_index: int,
        post_ids: Sequence[int],
        usernames: dict[int, str] | None = None,
        entity_names: Sequence[str] = (),
    ) -> RoundOutcome:
        """Rank one round's posts by engagement, from the run's own database.

        ``post_ids`` comes from the ledger's round attribution: OASIS records no
        round anywhere, so which posts belong to this round is knowable only
        from the recorded boundaries.
        """
        outcome = RoundOutcome(round=round_index)
        path = Path(database_path)
        if not path.is_file() or not post_ids:
            return outcome

        usernames = usernames or {}
        lookup = {normalise_name(name): name for name in entity_names if name}

        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            placeholders = ",".join("?" * len(post_ids))
            rows = connection.execute(
                f"SELECT post_id, user_id, content, num_likes, num_dislikes, "
                f"num_shares FROM post WHERE post_id IN ({placeholders})",
                tuple(post_ids),
            ).fetchall()
            comment_counts = {
                int(row["post_id"]): int(row["n"]) for row in connection.execute(
                    f"SELECT post_id, COUNT(*) AS n FROM comment "
                    f"WHERE post_id IN ({placeholders}) GROUP BY post_id",
                    tuple(post_ids))
            }
            follows = [
                (int(row["follower_id"]), int(row["followee_id"]))
                for row in connection.execute(
                    "SELECT follower_id, followee_id FROM follow")
            ]
        except sqlite3.Error as exc:
            logger.warning("Could not read round %d outcomes: %s", round_index, exc)
            return outcome
        finally:
            connection.close()

        posts: list[SimPost] = []
        for row in rows:
            content = str(row["content"] or "")
            post = SimPost(
                post_id=int(row["post_id"]), user_id=int(row["user_id"]),
                username=usernames.get(int(row["user_id"]), ""),
                content=content, round=round_index,
                likes=int(row["num_likes"] or 0),
                dislikes=int(row["num_dislikes"] or 0),
                reposts=int(row["num_shares"] or 0),
                comments=comment_counts.get(int(row["post_id"]), 0),
                mentions=_mentioned_entities(content, lookup),
            )
            if post.engagement >= self.min_engagement:
                posts.append(post)

        posts.sort(key=lambda p: (-p.engagement, p.post_id))
        outcome.posts = posts[:self.top_n]
        outcome.follows = follows
        return outcome

    # -- writing ------------------------------------------------------------

    async def write_round(
        self,
        outcome: RoundOutcome,
        *,
        sim_id: str,
        graph_id: str,
    ) -> RoundOutcome:
        """Persist one round's outcomes as their own clearly-labelled subgraph."""
        if not outcome.posts and not outcome.follows:
            return outcome

        await self.storage.write(
            "MERGE (r:SimRun {sim_id: $sim_id}) "
            "SET r.graph_id = $graph_id",
            sim_id=sim_id, graph_id=graph_id,
        )

        rows = [post.to_dict() for post in outcome.posts]
        if rows:
            written = await self.storage.write(
                "UNWIND $rows AS row "
                "MERGE (p:SimPost {sim_id: $sim_id, post_id: row.post_id}) "
                "SET p.content = row.content, p.round = row.round, "
                "    p.likes = row.likes, p.dislikes = row.dislikes, "
                "    p.reposts = row.reposts, p.comments = row.comments, "
                "    p.engagement = row.engagement, p.graph_id = $graph_id "
                "MERGE (a:SimAgent {sim_id: $sim_id, user_id: row.user_id}) "
                "SET a.username = row.username, a.graph_id = $graph_id "
                "MERGE (p)-[:POSTED_BY]->(a) "
                "WITH p, row "
                "MATCH (run:SimRun {sim_id: $sim_id}) "
                "MERGE (p)-[:IN_RUN]->(run) "
                "RETURN count(p) AS n",
                rows=rows, sim_id=sim_id, graph_id=graph_id,
            )
            outcome.nodes_written += int(written[0]["n"]) if written else 0

            # The only edge that touches document-derived data, and it is named
            # for what it means: this invented post is *about* a real entity.
            # It never writes to the entity, only points at it.
            linked = await self.storage.write(
                "UNWIND $rows AS row "
                "UNWIND row.mentions AS mention "
                "MATCH (p:SimPost {sim_id: $sim_id, post_id: row.post_id}) "
                "MATCH (e:Entity {graph_id: $graph_id, normalised: mention}) "
                "MERGE (p)-[:ABOUT]->(e) "
                "RETURN count(*) AS n",
                rows=rows, sim_id=sim_id, graph_id=graph_id,
            )
            outcome.edges_written += int(linked[0]["n"]) if linked else 0

        if outcome.follows:
            followed = await self.storage.write(
                "UNWIND $pairs AS pair "
                "MERGE (a:SimAgent {sim_id: $sim_id, user_id: pair[0]}) "
                "MERGE (b:SimAgent {sim_id: $sim_id, user_id: pair[1]}) "
                "MERGE (a)-[f:FOLLOWED]->(b) "
                "SET f.round = $round "
                "RETURN count(f) AS n",
                pairs=[list(pair) for pair in outcome.follows],
                sim_id=sim_id, round=outcome.round,
            )
            outcome.edges_written += int(followed[0]["n"]) if followed else 0

        logger.info("Graph memory %s", outcome.summary())
        return outcome

    # -- reading back -------------------------------------------------------

    async def context_for(
        self, *, sim_id: str, graph_id: str, before_round: int, limit: int = 5
    ) -> str:
        """What the population will be told it remembers.

        One query for the whole round. The obvious design — every agent asks
        the graph on its own turn — is a round trip per agent per round, and a
        round is already the expensive part of a run.
        """
        rows = await self.storage.read(
            "MATCH (p:SimPost {sim_id: $sim_id}) "
            "WHERE p.round < $before_round "
            "OPTIONAL MATCH (p)-[:ABOUT]->(e:Entity) "
            "WITH p, collect(DISTINCT e.name) AS about "
            "RETURN p.content AS content, p.round AS round, "
            "       p.engagement AS engagement, about "
            "ORDER BY p.engagement DESC, p.round DESC LIMIT $limit",
            sim_id=sim_id, before_round=before_round, limit=limit,
        )
        if not rows:
            return ""

        lines = []
        for row in rows:
            about = ", ".join(name for name in (row.get("about") or []) if name)
            suffix = f" (about {about})" if about else ""
            lines.append(
                f"- \"{_truncate(str(row['content']))}\" — round {row['round']}, "
                f"{row['engagement']} reaction(s){suffix}"
            )
        return (
            "What has been circulating in this community so far:\n"
            + "\n".join(lines)
        )

    async def delete_run(self, sim_id: str) -> int:
        """Remove one simulation's subgraph, leaving the document's graph intact."""
        rows = await self.storage.write(
            "MATCH (n) WHERE n.sim_id = $sim_id "
            "WITH n, count(n) AS ignored DETACH DELETE n RETURN count(*) AS n",
            sim_id=sim_id,
        )
        return int(rows[0]["n"]) if rows else 0


def _mentioned_entities(content: str, lookup: dict[str, str]) -> list[str]:
    """Which document entities a post talks about, by name match.

    Deterministic on purpose: an LLM would be better at this and would also put
    another inference in the critical path of a saturated GPU.
    """
    if not content or not lookup:
        return []
    haystack = normalise_name(content)
    if not haystack:
        return []
    return sorted(
        normalised for normalised in lookup
        if normalised and len(normalised) > 3 and normalised in haystack
    )


def _truncate(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
