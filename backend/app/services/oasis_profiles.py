"""Emit personas in the exact shapes OASIS reads.

The formats are **not** symmetrical, and the spec's assumption that both are
JSON is wrong. Read from `camel-oasis` 0.2.5:

* ``generate_twitter_agent_graph`` calls ``pd.read_csv(profile_path)`` and
  indexes columns ``username``, ``description`` and ``user_char``. A JSON file
  here raises inside agent generation.
* ``generate_reddit_agent_graph`` calls ``json.load`` and indexes keys
  ``username``, ``bio``, ``persona``, ``mbti``, ``gender``, ``age`` and
  ``country``. A missing key is a ``KeyError`` several frames deep.
* ``generate_agents`` — the fuller CSV path — additionally reads ``name`` and
  ``following_agentid_list``.

So Twitter gets CSV and Reddit gets JSON, and the CSV carries the union of the
columns any loader touches. This is the failure the spec warns about: a
mismatch surfaces as an opaque error deep inside the engine, hours into a run,
so the shapes are derived by reading the loaders rather than by assuming.

``profiles.json`` is written alongside them. The OASIS files are lossy — no
Big Five, no provenance, no link back to the graph entity — and Phase 8's
report and the Phase 9 UI need all three. Nothing in OASIS reads it.

**MBTI is derived, not asked for.** OASIS interpolates it into the Reddit
agent's own prompt, so it must exist; asking the model for it separately would
let personality and type contradict each other. The Big Five map onto the four
axes directly, and the mapping is written down here rather than being folklore.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.services.profile_generator import PersonaProfile

logger = logging.getLogger(__name__)

__all__ = [
    "REDDIT_REQUIRED_KEYS",
    "UNSTATED_GENDER",
    "TWITTER_REQUIRED_COLUMNS",
    "ProfileBundle",
    "SchemaViolation",
    "derive_mbti",
    "make_username",
    "to_reddit_entry",
    "to_twitter_row",
    "validate_reddit_json",
    "validate_twitter_csv",
    "write_profiles",
]

#: Columns some OASIS loader indexes directly. Missing any one is a KeyError
#: inside agent generation, not a validation message.
TWITTER_REQUIRED_COLUMNS: tuple[str, ...] = (
    "user_id", "name", "username", "description", "user_char",
    "following_count", "followers_count", "following_agentid_list",
)

#: Keys ``generate_reddit_agent_graph`` indexes on every record.
REDDIT_REQUIRED_KEYS: tuple[str, ...] = (
    "username", "bio", "persona", "mbti", "gender", "age", "country",
)

#: Big Five above this reads as the first pole of its axis.
MBTI_THRESHOLD = 0.5

#: Used when the source does not state a named person's gender. Phrased to read
#: correctly inside OASIS's "You are a {gender}, {age} years old" sentence.
UNSTATED_GENDER = "person of unstated gender"

_HANDLE_STRIP = re.compile(r"[^a-z0-9_]+")


class SchemaViolation(ValueError):
    """An emitted profile file would not load in OASIS."""


def derive_mbti(profile: PersonaProfile) -> str:
    """Map Big Five scores onto an MBTI code.

    Not psychometrics — a deterministic projection so the type OASIS shows the
    agent cannot contradict the personality the rest of the system uses:

    * **E/I** from extraversion, directly.
    * **N/S** from openness: openness to experience is the closest Big Five
      analogue of intuition over sensing.
    * **T/F** from agreeableness, inverted: low agreeableness reads as thinking.
    * **J/P** from conscientiousness, directly.

    Neuroticism has no MBTI axis and is deliberately unused.
    """
    scores = profile.personality
    return (
        ("E" if scores.extraversion >= MBTI_THRESHOLD else "I")
        + ("N" if scores.openness >= MBTI_THRESHOLD else "S")
        + ("F" if scores.agreeableness >= MBTI_THRESHOLD else "T")
        + ("J" if scores.conscientiousness >= MBTI_THRESHOLD else "P")
    )


def make_username(name: str, index: int, taken: set[str] | None = None) -> str:
    """A handle that is unique, stable and shaped like a social handle."""
    slug = _HANDLE_STRIP.sub("_", name.strip().lower()).strip("_")
    slug = slug[:24] or f"agent_{index}"
    candidate = slug
    if taken is not None:
        suffix = 1
        while candidate in taken:
            suffix += 1
            candidate = f"{slug}_{suffix}"
        taken.add(candidate)
    return candidate


def _persona_text(profile: PersonaProfile) -> str:
    """The self-description OASIS puts in the agent's system prompt.

    Everything the agent needs to act in character, as prose. OASIS drops it
    into a sentence, so it must read as one.
    """
    parts = [
        f"{profile.name} is {profile.age} and works as a {profile.occupation}.",
        profile.background.strip(),
    ]
    if profile.traits:
        parts.append("They come across as " + ", ".join(profile.traits) + ".")
    if profile.interests:
        parts.append("They are interested in " + ", ".join(profile.interests) + ".")
    if profile.leanings:
        parts.append(f"Their view on the matter at hand: {profile.leanings}.")
    if profile.writing_style:
        parts.append(f"They write like this: {profile.writing_style}.")
    parts.append(f"They post {profile.activity_level} amounts of the time.")
    return " ".join(part for part in parts if part)


def _followers_for(profile: PersonaProfile, index: int) -> tuple[int, int]:
    """Plausible follower counts, scaled by how active the persona is.

    Deterministic from the persona rather than random, so regenerating a
    population twice produces the same graph.
    """
    base = {"low": 40, "moderate": 180, "high": 900}[profile.activity_level]
    spread = (hash((profile.name, index)) % 100) / 100.0
    followers = int(base * (0.5 + spread))
    following = int(followers * (0.6 + spread * 0.8))
    return following, followers


def to_twitter_row(
    profile: PersonaProfile, index: int, *, username: str
) -> dict[str, Any]:
    """One CSV row, in the shape ``pd.read_csv`` consumers index."""
    following, followers = _followers_for(profile, index)
    return {
        "user_id": index,
        "name": profile.name,
        "username": username,
        "description": profile.background.strip() or profile.occupation,
        "user_char": _persona_text(profile),
        "following_count": following,
        "followers_count": followers,
        # OASIS reads this column; the platform builds the real follow graph
        # during the run, so an empty list is the correct starting state.
        "following_agentid_list": "[]",
        # Ours, ignored by OASIS, but the file is also read by humans.
        "provenance": profile.provenance,
        "activity_level": profile.activity_level,
    }


def to_reddit_entry(
    profile: PersonaProfile, index: int, *, username: str, default_country: str
) -> dict[str, Any]:
    """One JSON record, in the shape ``generate_reddit_agent_graph`` indexes."""
    return {
        "user_id": index,
        "username": username,
        "name": profile.name,
        "bio": profile.background.strip() or profile.occupation,
        "persona": _persona_text(profile),
        "mbti": derive_mbti(profile),
        # Invented for synthetic agents; left unstated for real named people
        # unless the document said otherwise. The wording matters: OASIS
        # interpolates this into "You are a {gender}, {age} years old", and
        # "You are a unspecified" is bad English going straight into the
        # agent's own prompt.
        "gender": profile.gender or UNSTATED_GENDER,
        "age": profile.age,
        "country": profile.country or default_country,
        "provenance": profile.provenance,
    }


@dataclass
class ProfileBundle:
    """What was written, and where."""

    twitter_csv: Path
    reddit_json: Path
    profiles_json: Path
    count: int = 0
    named: int = 0
    synthetic: int = 0

    def summary(self) -> str:
        return (
            f"{self.count} profiles ({self.named} named, {self.synthetic} "
            f"synthetic) -> {self.twitter_csv.name}, {self.reddit_json.name}, "
            f"{self.profiles_json.name}"
        )


def write_profiles(
    profiles: Sequence[PersonaProfile],
    directory: str | Path,
    *,
    default_country: str = "the local area",
) -> ProfileBundle:
    """Write both OASIS files and our own full record.

    Validates what it wrote before returning. A file that will not load is
    better discovered here than three hours into a simulation.
    """
    if not profiles:
        raise SchemaViolation("Cannot write an empty population.")

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    taken: set[str] = set()
    usernames = [
        make_username(profile.name, index, taken)
        for index, profile in enumerate(profiles)
    ]

    twitter_path = directory / "twitter.csv"
    with twitter_path.open("w", newline="", encoding="utf-8") as handle:
        rows = [
            to_twitter_row(profile, index, username=username)
            for index, (profile, username) in enumerate(zip(profiles, usernames))
        ]
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    reddit_path = directory / "reddit.json"
    reddit_path.write_text(
        json.dumps(
            [
                to_reddit_entry(profile, index, username=username,
                                default_country=default_country)
                for index, (profile, username) in enumerate(zip(profiles, usernames))
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    profiles_path = directory / "profiles.json"
    profiles_path.write_text(
        json.dumps(
            [
                {**profile.model_dump(), "username": username, "user_id": index,
                 "mbti": derive_mbti(profile)}
                for index, (profile, username) in enumerate(zip(profiles, usernames))
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    validate_twitter_csv(twitter_path)
    validate_reddit_json(reddit_path)

    bundle = ProfileBundle(
        twitter_csv=twitter_path, reddit_json=reddit_path,
        profiles_json=profiles_path, count=len(profiles),
        named=sum(1 for p in profiles if p.provenance == "named"),
        synthetic=sum(1 for p in profiles if p.provenance == "synthetic"),
    )
    logger.info("Wrote %s", bundle.summary())
    return bundle


# --------------------------------------------------------------------------
# Conformance
# --------------------------------------------------------------------------


def validate_twitter_csv(path: str | Path) -> int:
    """Check a Twitter profile file against what OASIS indexes.

    Mirrors the loader's actual accesses, not a schema written from memory.
    """
    path = Path(path)
    if not path.is_file():
        raise SchemaViolation(f"{path} does not exist")
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or ())
        missing = [c for c in TWITTER_REQUIRED_COLUMNS if c not in columns]
        if missing:
            raise SchemaViolation(
                f"{path.name} is missing column(s) {missing}. OASIS indexes "
                f"these directly, so the failure would be a KeyError inside "
                f"agent generation."
            )
        count = 0
        for number, row in enumerate(reader, start=2):
            for column in ("username", "description", "user_char"):
                if not (row.get(column) or "").strip():
                    raise SchemaViolation(
                        f"{path.name} line {number}: {column} is empty; it "
                        f"becomes part of the agent's system prompt."
                    )
            for column in ("following_count", "followers_count"):
                try:
                    int(row[column])
                except (TypeError, ValueError) as exc:
                    raise SchemaViolation(
                        f"{path.name} line {number}: {column} is not an integer"
                    ) from exc
            count += 1
    if not count:
        raise SchemaViolation(f"{path.name} has no rows")
    return count


def validate_reddit_json(path: str | Path) -> int:
    path = Path(path)
    if not path.is_file():
        raise SchemaViolation(f"{path} does not exist")
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaViolation(f"{path.name} is not valid JSON: {exc}") from exc

    if not isinstance(records, list) or not records:
        raise SchemaViolation(
            f"{path.name} must be a non-empty JSON list; OASIS indexes it by "
            f"position."
        )
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise SchemaViolation(f"{path.name}[{index}] is not an object")
        missing = [key for key in REDDIT_REQUIRED_KEYS if key not in record]
        if missing:
            raise SchemaViolation(
                f"{path.name}[{index}] is missing {missing}. OASIS indexes "
                f"these directly and interpolates gender, age and country into "
                f"the agent's own system prompt."
            )
        for key in ("username", "bio", "persona", "mbti", "gender", "country"):
            if not str(record[key]).strip():
                raise SchemaViolation(f"{path.name}[{index}]: {key} is empty")
        if not isinstance(record["age"], int):
            raise SchemaViolation(
                f"{path.name}[{index}]: age must be an integer, got "
                f"{type(record['age']).__name__}"
            )
    return len(records)
