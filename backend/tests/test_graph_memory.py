"""Phase 6 Step 4 — simulation outcomes into the graph, and back into prompts.

The spec marks this optional and warns it is hard, so the tests concentrate on
the two ways it could do real damage rather than on its happy path.

**Contamination.** Simulated posts are fabricated. The graph holds facts
extracted from a real document. If the two are ever confused, a report can cite
an invented statement as something the source said — the failure Phases 4 and 5
exist to prevent. Several tests below assert the line holds, including against
a live Neo4j.

**Cost.** Closing the loop naively means a Neo4j round trip per agent per round.
The context is fetched once per round and shared, and a test asserts that
sharing actually happens rather than being an intention in a docstring.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.services.graph_memory_updater import (
    GraphMemoryUpdater,
    RoundOutcome,
    SimPost,
)
from app.services.simulation_runner import attach_graph_memory


class FakeStorage:
    """Records the Cypher it is given, and answers with what a test wants."""

    def __init__(self, rows: list[dict] | None = None):
        self.writes: list[tuple[str, dict]] = []
        self.reads: list[tuple[str, dict]] = []
        self._rows = rows or []

    async def write(self, cypher: str, /, **params):
        self.writes.append((cypher, params))
        return [{"n": 1}]

    async def read(self, cypher: str, /, **params):
        self.reads.append((cypher, params))
        return self._rows

    async def aclose(self):
        return None


@pytest.fixture
def feedback_config(config):
    return config.model_copy(update={
        "GRAPH_MEMORY_FEEDBACK": True,
        "GRAPH_MEMORY_MIN_ENGAGEMENT": 1,
        "GRAPH_MEMORY_TOP_N": 3,
    })


@pytest.fixture
def run_db(tmp_path):
    """A run database with OASIS's real schema and some activity."""
    from oasis.social_platform.database import create_db

    path = tmp_path / "simulation.db"
    connection, _ = create_db(str(path))
    connection.close()

    with sqlite3.connect(path) as connection:
        for user_id, name in ((0, "dawn"), (1, "ray"), (2, "wire")):
            connection.execute(
                "INSERT INTO user (user_id, agent_id, user_name, name, bio,"
                " created_at, num_followings, num_followers)"
                " VALUES (?, ?, ?, ?, '', '0', 0, 0)", (user_id, user_id, name, name))
        connection.execute(
            "INSERT INTO post (user_id, content, created_at, num_likes, num_shares)"
            " VALUES (0, 'Councillor Jane Doe ignored the consultation', '0', 4, 2)")
        connection.execute(
            "INSERT INTO post (user_id, content, created_at, num_likes)"
            " VALUES (1, 'a quiet remark', '0', 1)")
        connection.execute(
            "INSERT INTO post (user_id, content, created_at)"
            " VALUES (1, 'nobody noticed this', '0')")
        connection.execute(
            "INSERT INTO comment (post_id, user_id, content, created_at)"
            " VALUES (1, 1, 'agreed', '0')")
        connection.execute(
            "INSERT INTO follow (follower_id, followee_id, created_at)"
            " VALUES (1, 0, '0')")
    return path


# --------------------------------------------------------------------------
# The flag
# --------------------------------------------------------------------------


def test_it_is_off_by_default(config):
    """It changes what the simulation measures, so it must be asked for."""
    assert config.GRAPH_MEMORY_FEEDBACK is False
    assert not GraphMemoryUpdater(FakeStorage(), config=config).enabled


def test_it_reports_itself_on_when_enabled(feedback_config):
    assert GraphMemoryUpdater(FakeStorage(), config=feedback_config).enabled


# --------------------------------------------------------------------------
# What counts as significant
# --------------------------------------------------------------------------


def test_engagement_sums_every_kind_of_reaction():
    post = SimPost(post_id=1, user_id=0, username="dawn", content="x", round=1,
                   likes=3, dislikes=1, reposts=2, comments=4)
    assert post.engagement == 10


def test_posts_are_ranked_by_engagement(feedback_config, run_db):
    updater = GraphMemoryUpdater(FakeStorage(), config=feedback_config)
    outcome = updater.collect(run_db, round_index=1, post_ids=[1, 2, 3],
                              usernames={0: "dawn", 1: "ray"})

    assert [p.post_id for p in outcome.posts] == [1, 2]
    assert outcome.posts[0].engagement == 7


def test_an_ignored_post_is_not_worth_remembering(feedback_config, run_db):
    updater = GraphMemoryUpdater(FakeStorage(), config=feedback_config)
    outcome = updater.collect(run_db, round_index=1, post_ids=[1, 2, 3])
    assert 3 not in [p.post_id for p in outcome.posts]


def test_the_top_n_bound_is_respected(config, run_db):
    settings = config.model_copy(update={
        "GRAPH_MEMORY_FEEDBACK": True, "GRAPH_MEMORY_MIN_ENGAGEMENT": 0,
        "GRAPH_MEMORY_TOP_N": 1})
    updater = GraphMemoryUpdater(FakeStorage(), config=settings)
    assert len(updater.collect(run_db, round_index=1, post_ids=[1, 2, 3]).posts) == 1


def test_a_zero_threshold_keeps_everything(config, run_db):
    settings = config.model_copy(update={
        "GRAPH_MEMORY_FEEDBACK": True, "GRAPH_MEMORY_MIN_ENGAGEMENT": 0,
        "GRAPH_MEMORY_TOP_N": 10})
    updater = GraphMemoryUpdater(FakeStorage(), config=settings)
    assert len(updater.collect(run_db, round_index=1, post_ids=[1, 2, 3]).posts) == 3


def test_only_this_rounds_posts_are_considered(feedback_config, run_db):
    """Attribution comes from the ledger; OASIS records no round itself."""
    updater = GraphMemoryUpdater(FakeStorage(), config=feedback_config)
    outcome = updater.collect(run_db, round_index=2, post_ids=[2])
    assert [p.post_id for p in outcome.posts] == [2]


def test_a_round_with_no_posts_yields_nothing(feedback_config, run_db):
    updater = GraphMemoryUpdater(FakeStorage(), config=feedback_config)
    assert updater.collect(run_db, round_index=5, post_ids=[]).posts == []


def test_a_missing_database_is_not_an_error(feedback_config, tmp_path):
    updater = GraphMemoryUpdater(FakeStorage(), config=feedback_config)
    assert updater.collect(tmp_path / "gone.db", round_index=1, post_ids=[1]).posts == []


def test_the_follow_graph_is_collected(feedback_config, run_db):
    updater = GraphMemoryUpdater(FakeStorage(), config=feedback_config)
    assert updater.collect(run_db, round_index=1, post_ids=[1]).follows == [(1, 0)]


# --------------------------------------------------------------------------
# Linking to what a post discusses
# --------------------------------------------------------------------------


def test_a_post_is_linked_to_the_entity_it_names(feedback_config, run_db):
    updater = GraphMemoryUpdater(FakeStorage(), config=feedback_config)
    outcome = updater.collect(run_db, round_index=1, post_ids=[1, 2],
                              entity_names=["Councillor Jane Doe", "Riverbend Gazette"])
    assert outcome.posts[0].mentions == ["jane doe"]


def test_a_post_naming_nobody_links_to_nothing(feedback_config, run_db):
    updater = GraphMemoryUpdater(FakeStorage(), config=feedback_config)
    outcome = updater.collect(run_db, round_index=1, post_ids=[2],
                              entity_names=["Councillor Jane Doe"])
    assert outcome.posts[0].mentions == []


def test_very_short_entity_names_are_not_matched(feedback_config, run_db):
    """A two-letter name would match half the corpus."""
    updater = GraphMemoryUpdater(FakeStorage(), config=feedback_config)
    outcome = updater.collect(run_db, round_index=1, post_ids=[1],
                              entity_names=["ed", "a"])
    assert outcome.posts[0].mentions == []


# --------------------------------------------------------------------------
# Isolation from the document's graph
# --------------------------------------------------------------------------


async def test_SIMULATED_NODES_NEVER_CARRY_THE_ENTITY_LABEL(feedback_config):
    storage = FakeStorage()
    updater = GraphMemoryUpdater(storage, config=feedback_config)
    outcome = RoundOutcome(round=1, posts=[
        SimPost(post_id=1, user_id=0, username="dawn", content="x", round=1, likes=2)])

    await updater.write_round(outcome, sim_id="sim-1", graph_id="g-1")

    for cypher, _ in storage.writes:
        creating = cypher.split("MATCH")[0]
        assert ":Entity" not in creating, "simulated data must never become an Entity"


async def test_the_only_edge_to_a_real_entity_is_named_about(feedback_config):
    storage = FakeStorage()
    updater = GraphMemoryUpdater(storage, config=feedback_config)
    outcome = RoundOutcome(round=1, posts=[
        SimPost(post_id=1, user_id=0, username="dawn", content="x", round=1,
                likes=2, mentions=["jane doe"])])

    await updater.write_round(outcome, sim_id="sim-1", graph_id="g-1")
    touching = [c for c, _ in storage.writes if "Entity" in c]

    assert len(touching) == 1
    assert "MERGE (p)-[:ABOUT]->(e)" in touching[0]
    assert "SET e." not in touching[0], "an entity must never be written to"


async def test_every_simulated_node_carries_its_run(feedback_config):
    storage = FakeStorage()
    updater = GraphMemoryUpdater(storage, config=feedback_config)
    await updater.write_round(
        RoundOutcome(round=1, posts=[SimPost(post_id=1, user_id=0, username="d",
                                             content="x", round=1)]),
        sim_id="sim-1", graph_id="g-1")

    assert all("sim_id" in params or "$sim_id" in cypher
               for cypher, params in storage.writes)


async def test_an_empty_round_writes_nothing(feedback_config):
    storage = FakeStorage()
    updater = GraphMemoryUpdater(storage, config=feedback_config)
    await updater.write_round(RoundOutcome(round=1), sim_id="s", graph_id="g")
    assert storage.writes == []


# --------------------------------------------------------------------------
# Reading back
# --------------------------------------------------------------------------


async def test_the_context_is_fetched_once_for_the_whole_round(feedback_config):
    """Per agent it would be a Neo4j round trip per agent per round."""
    storage = FakeStorage(rows=[
        {"content": "the consultation was too short", "round": 1, "engagement": 5,
         "about": ["Councillor Jane Doe"]}])
    updater = GraphMemoryUpdater(storage, config=feedback_config)

    await updater.context_for(sim_id="s", graph_id="g", before_round=2)
    assert len(storage.reads) == 1


async def test_the_context_reads_as_recollection(feedback_config):
    storage = FakeStorage(rows=[
        {"content": "the consultation was too short", "round": 1, "engagement": 5,
         "about": ["Councillor Jane Doe"]}])
    text = await GraphMemoryUpdater(storage, config=feedback_config).context_for(
        sim_id="s", graph_id="g", before_round=2)

    assert "circulating" in text
    assert "the consultation was too short" in text
    assert "Councillor Jane Doe" in text


async def test_nothing_remembered_yields_no_context(feedback_config):
    text = await GraphMemoryUpdater(FakeStorage(rows=[]), config=feedback_config)\
        .context_for(sim_id="s", graph_id="g", before_round=2)
    assert text == ""


async def test_a_round_cannot_remember_itself(feedback_config):
    """Otherwise agents would react to posts made in the round they are in."""
    storage = FakeStorage(rows=[])
    await GraphMemoryUpdater(storage, config=feedback_config).context_for(
        sim_id="s", graph_id="g", before_round=3)
    assert storage.reads[0][1]["before_round"] == 3
    assert "p.round < $before_round" in storage.reads[0][0]


async def test_a_long_post_is_truncated_in_the_recollection(feedback_config):
    storage = FakeStorage(rows=[
        {"content": "x" * 500, "round": 1, "engagement": 2, "about": []}])
    text = await GraphMemoryUpdater(storage, config=feedback_config).context_for(
        sim_id="s", graph_id="g", before_round=2)
    assert len(text) < 400


# --------------------------------------------------------------------------
# Closing the loop into an agent's prompt
# --------------------------------------------------------------------------


class FakeEnvironment:
    def __init__(self):
        self.calls = 0

    async def to_text_prompt(self):
        self.calls += 1
        return "Here are the posts in your feed."


class FakeAgent:
    def __init__(self):
        self.social_agent_id = 0
        self.env = FakeEnvironment()


async def test_the_recollection_reaches_the_prompt():
    agent = FakeAgent()
    context = {"text": "What has been circulating: something happened."}
    attach_graph_memory(agent, context)

    prompt = await agent.env.to_text_prompt()
    assert "posts in your feed" in prompt
    assert "something happened" in prompt


async def test_an_empty_recollection_leaves_the_prompt_untouched():
    agent = FakeAgent()
    attach_graph_memory(agent, {"text": ""})
    assert await agent.env.to_text_prompt() == "Here are the posts in your feed."


async def test_every_agent_sees_the_same_refreshed_context():
    """One dict, refreshed once a round, read by the whole population."""
    context: dict[str, str] = {"text": ""}
    agents = [FakeAgent() for _ in range(3)]
    for agent in agents:
        attach_graph_memory(agent, context)

    context["text"] = "round two recollection"
    prompts = [await agent.env.to_text_prompt() for agent in agents]
    assert all("round two recollection" in prompt for prompt in prompts)


async def test_attaching_twice_does_not_duplicate_the_context():
    agent = FakeAgent()
    context = {"text": "recalled"}
    attach_graph_memory(agent, context)
    attach_graph_memory(agent, context)

    assert (await agent.env.to_text_prompt()).count("recalled") == 1


# --------------------------------------------------------------------------
# Against a live Neo4j
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_line_between_simulated_and_documented_holds(integration_config,
                                                                graph_ns, run_db):
    """The contamination the whole design is arranged to prevent."""
    from app.storage.neo4j_storage import Neo4jStorage

    settings = integration_config.model_copy(update={
        "GRAPH_MEMORY_FEEDBACK": True, "GRAPH_MEMORY_MIN_ENGAGEMENT": 1,
        "GRAPH_MEMORY_TOP_N": 5})
    storage = Neo4jStorage(settings)
    updater = GraphMemoryUpdater(storage, config=settings)
    sim_id = f"sim-20260101-000000-{graph_ns[-6:]}"

    try:
        await storage.write(
            "CREATE (e:Entity {graph_id: $g, uuid: 'e-1',"
            " name: 'Councillor Jane Doe', normalised: 'jane doe', type: 'Person'})",
            g=graph_ns)

        names = await updater.entity_names(graph_ns)
        assert names == ["Councillor Jane Doe"]

        outcome = updater.collect(run_db, round_index=1, post_ids=[1, 2, 3],
                                  usernames={0: "dawn", 1: "ray"},
                                  entity_names=names)
        written = await updater.write_round(outcome, sim_id=sim_id, graph_id=graph_ns)
        assert written.nodes_written >= 1

        entity_labels = await storage.read(
            "MATCH (e:Entity {graph_id: $g}) RETURN labels(e) AS labels", g=graph_ns)
        assert entity_labels[0]["labels"] == ["Entity"], "the entity was modified"

        leaked = await storage.read(
            "MATCH (n:Entity) WHERE n.sim_id IS NOT NULL RETURN count(n) AS n")
        assert leaked[0]["n"] == 0, "simulated data became a document entity"

        linked = await storage.read(
            "MATCH (:SimPost {sim_id: $s})-[:ABOUT]->(e:Entity) RETURN e.name AS name",
            s=sim_id)
        assert [row["name"] for row in linked] == ["Councillor Jane Doe"]

        context = await updater.context_for(sim_id=sim_id, graph_id=graph_ns,
                                            before_round=2)
        assert "ignored the consultation" in context

        # Deleting the run must not touch the document's graph.
        await updater.delete_run(sim_id)
        surviving = await storage.read(
            "MATCH (e:Entity {graph_id: $g}) RETURN count(e) AS n", g=graph_ns)
        assert surviving[0]["n"] == 1
    finally:
        await storage.write("MATCH (n) WHERE n.graph_id = $g DETACH DELETE n",
                            g=graph_ns)
        await storage.write("MATCH (n) WHERE n.sim_id = $s DETACH DELETE n", s=sim_id)
        await storage.aclose()
