"""Phase 8 Step 1 — scoring what the population felt.

A sentiment trajectory is the one part of a report that has to be a
*measurement* rather than an impression. "Opinion hardened between rounds three
and five" is either backed by a number per round or it is the model's prior
assumption wearing a chart, and the whole point of Phase 8's grounding rule is
to tell those apart.

**Scored once, stored with the run.** Each post is scored by the local model and
the score written to the run's own database, so it is paid for once and reused by
every later report, export and re-read. Cost scales with posts, not with how
often somebody asks — and a report regenerated a month later gets the same
numbers, which a re-scored one would not.

**A lexicon was the obvious cheaper option and is the wrong one here.** These
runs produce hedged civic language — *"I appreciate the need for housing but am
concerned about the consultation period"* — which word-counting scores at
roughly zero. That is precisely the nuance the report exists to surface.

**A post that cannot be scored is recorded as unscored, not as neutral.** Zero
means "this was balanced"; absent means "we do not know". Averaging the second
into the first would quietly pull every trajectory toward the middle.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel, Field

from app.config import Config, get_config
from app.utils.llm_client import LLMClient, LLMJSONError

logger = logging.getLogger(__name__)

__all__ = [
    "SENTIMENT_TABLE",
    "PostSentiment",
    "SentimentScorer",
    "round_trajectory",
]

SENTIMENT_TABLE = "crowdsight_sentiment"

CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SENTIMENT_TABLE} (
    post_id   INTEGER PRIMARY KEY,
    score     REAL,
    stance    TEXT,
    rationale TEXT,
    scored_at TEXT
)
"""

#: How many posts go to the model at once. Large enough that a run of a few
#: hundred posts is a handful of calls, small enough that one bad batch costs
#: little and the response stays inside a sensible output budget.
BATCH_SIZE = 12

#: Posts a batch failed to score are retried in smaller pieces, where a small
#: model is far more reliable about answering for every item.
RETRY_BATCH_SIZE = 4

SYSTEM = """\
You score how a social media post feels about the event it discusses.

For each post return:
- id: the post's id, exactly as given
- score: -1.0 (strongly opposed or hostile) to 1.0 (strongly supportive or \
enthusiastic). 0.0 means genuinely balanced or neutral, not "unsure".
- stance: one of "supportive", "opposed", "mixed", "neutral"
- rationale: at most 12 words, quoting what decided it

Judge the writer's attitude to the event, not whether the post is polite. \
Hedged criticism is still criticism. A post that is only factual reporting is \
neutral.

Return JSON: {"scores": [{"id": 1, "score": -0.4, "stance": "opposed", \
"rationale": "..."}]}"""

USER = """\
Event under discussion: {event}

Posts:
{posts}"""


class _Score(BaseModel):
    id: int
    score: float = Field(ge=-1.0, le=1.0)
    stance: str = ""
    rationale: str = ""


class _Scores(BaseModel):
    scores: list[_Score] = Field(default_factory=list)


@dataclass
class PostSentiment:
    post_id: int
    score: float
    stance: str = ""
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"post_id": self.post_id, "score": round(self.score, 3),
                "stance": self.stance, "rationale": self.rationale}


class SentimentScorer:
    """Scores a run's posts and keeps the result in the run's database."""

    def __init__(self, config: Config | None = None, *,
                 llm: LLMClient | None = None) -> None:
        self.config = config or get_config()
        self.llm = llm or LLMClient(self.config)

    # -- storage ------------------------------------------------------------

    @staticmethod
    def _connect(database_path: str | Path) -> sqlite3.Connection:
        connection = sqlite3.connect(database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def ensure_schema(self, database_path: str | Path) -> None:
        with self._connect(database_path) as connection:
            connection.execute(CREATE_TABLE)

    def stored(self, database_path: str | Path) -> dict[int, PostSentiment]:
        """Scores already computed for this run."""
        path = Path(database_path)
        if not path.is_file():
            return {}
        try:
            with self._connect(path) as connection:
                if not connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                        (SENTIMENT_TABLE,)).fetchone():
                    return {}
                rows = connection.execute(
                    f"SELECT post_id, score, stance, rationale "
                    f"FROM {SENTIMENT_TABLE}").fetchall()
        except sqlite3.Error as exc:
            logger.warning("Could not read stored sentiment: %s", exc)
            return {}
        return {int(r["post_id"]): PostSentiment(
            post_id=int(r["post_id"]), score=float(r["score"]),
            stance=str(r["stance"] or ""), rationale=str(r["rationale"] or ""))
            for r in rows}

    def _save(self, database_path: str | Path,
              scores: Sequence[PostSentiment]) -> None:
        if not scores:
            return
        from datetime import datetime, timezone

        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect(database_path) as connection:
            connection.executemany(
                f"INSERT OR REPLACE INTO {SENTIMENT_TABLE} "
                f"(post_id, score, stance, rationale, scored_at) "
                f"VALUES (?, ?, ?, ?, ?)",
                [(s.post_id, s.score, s.stance, s.rationale[:200], stamp)
                 for s in scores])

    # -- scoring ------------------------------------------------------------

    async def score_run(
        self,
        database_path: str | Path,
        posts: Sequence[dict[str, Any]],
        *,
        event: str = "",
        rescore: bool = False,
        progress: Any = None,
    ) -> dict[int, PostSentiment]:
        """Score every post that has not been scored, and return them all."""
        path = Path(database_path)
        if not path.is_file():
            return {}
        self.ensure_schema(path)

        known = {} if rescore else self.stored(path)
        outstanding = [p for p in posts
                       if int(p["post_id"]) not in known and (p.get("content") or "").strip()]
        if not outstanding:
            return known

        logger.info("Scoring %d post(s) for sentiment (%d already known)",
                    len(outstanding), len(known))

        for index in range(0, len(outstanding), BATCH_SIZE):
            batch = outstanding[index:index + BATCH_SIZE]
            scored = await self._score_batch(batch, event=event)

            # A small model routinely answers for only part of a batch. Left
            # alone that silently leaves posts unscored for good, which shows up
            # much later as a round with no sentiment at all; retrying the
            # remainder in smaller pieces recovers nearly all of them.
            missing = [p for p in batch
                       if int(p["post_id"]) not in {s.post_id for s in scored}]
            if missing:
                logger.info("Retrying %d post(s) the batch did not score",
                            len(missing))
                for start in range(0, len(missing), RETRY_BATCH_SIZE):
                    scored.extend(await self._score_batch(
                        missing[start:start + RETRY_BATCH_SIZE], event=event))

            self._save(path, scored)
            known.update({s.post_id: s for s in scored})
            if progress is not None:
                progress(min(index + BATCH_SIZE, len(outstanding)), len(outstanding))

        known.update(self._inherit_for_amplification(path, posts, known))
        return known

    def _inherit_for_amplification(
        self, path: Path, posts: Sequence[dict[str, Any]],
        known: dict[int, PostSentiment],
    ) -> dict[int, PostSentiment]:
        """Give a repost the sentiment of the post it amplifies.

        OASIS writes a repost as a row with empty content pointing at the
        original, so there is nothing to score — but amplifying a post is not a
        neutral act, it is the clearest signal of agreement the platform offers.
        Leaving reposts unscored understates exactly the spread a sentiment
        trajectory exists to show. A quote carries its own text and is scored
        on that instead.
        """
        inherited: dict[int, PostSentiment] = {}
        for post in posts:
            post_id = int(post["post_id"])
            original = post.get("original_post_id")
            if post_id in known or original is None:
                continue
            if (post.get("content") or "").strip():
                continue          # a quote has its own words
            source = known.get(int(original))
            if source is None:
                continue
            inherited[post_id] = PostSentiment(
                post_id=post_id, score=source.score, stance=source.stance,
                rationale=f"repost of {original}: {source.rationale}"[:200])
        if inherited:
            self._save(path, list(inherited.values()))
            logger.info("%d repost(s) inherited the sentiment they amplified",
                        len(inherited))
        return inherited

    async def _score_batch(self, posts: Sequence[dict[str, Any]], *,
                           event: str) -> list[PostSentiment]:
        rendered = "\n".join(
            f"[{p['post_id']}] {_clean(p.get('content') or '')}" for p in posts)
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER.format(
                event=event or "an announcement", posts=rendered)},
        ]
        try:
            payload = await self.llm.complete_json(messages, _Scores, temperature=0.0)
        except LLMJSONError as exc:
            # Unscored, deliberately: a batch we could not read must not become
            # a row of neutral scores that drag every average toward zero.
            logger.warning("Sentiment batch failed (%d post(s)): %s", len(posts), exc)
            return []

        wanted = {int(p["post_id"]) for p in posts}
        out = []
        for item in payload.scores:
            if item.id not in wanted:
                # The model occasionally invents an id; a score for a post that
                # is not in the batch is not evidence of anything.
                continue
            out.append(PostSentiment(post_id=item.id, score=item.score,
                                     stance=item.stance, rationale=item.rationale))
        missing = wanted - {s.post_id for s in out}
        if missing:
            logger.info("Sentiment: %d post(s) left unscored in this batch",
                        len(missing))
        return out


def round_trajectory(
    scores: dict[int, PostSentiment],
    posts_by_round: dict[int, list[int]],
) -> list[dict[str, Any]]:
    """Mean sentiment per round, over the posts that were actually scored.

    Reports how many posts a round's figure rests on, because a mean over two
    posts and a mean over two hundred read identically otherwise.
    """
    trajectory = []
    for round_index in sorted(posts_by_round):
        ids = posts_by_round[round_index]
        scored = [scores[pid] for pid in ids if pid in scores]
        stances: dict[str, int] = {}
        for item in scored:
            stances[item.stance or "unknown"] = stances.get(item.stance or "unknown", 0) + 1
        trajectory.append({
            "round": round_index,
            "posts": len(ids),
            "scored": len(scored),
            "mean_score": round(sum(s.score for s in scored) / len(scored), 3)
            if scored else None,
            "stances": stances,
        })
    return trajectory


def _clean(text: str) -> str:
    """One line, bounded. A post is not a place for prompt instructions."""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:600]


