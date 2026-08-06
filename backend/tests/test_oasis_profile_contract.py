"""Phase 4 Step 3 — the profiles OASIS will actually load.

The highest-value test in the phase. A schema mismatch here does not fail
validation; it raises somewhere inside agent generation, hours into a run,
with a traceback that points at OASIS rather than at us.

So this asserts against the real thing. It imports the loaders, accesses every
column and key exactly as they do, and hands the finished files to OASIS's own
``UserInfo.to_system_message()``. A schema written from memory would only prove
the emitter agrees with the memory.

That import costs ~4 s. It runs in the default suite anyway, deliberately: a
check you have to remember to ask for is one nobody asks for, and this is the
one worth paying for.
"""

from __future__ import annotations

import csv
import json

import pytest

from app.services.oasis_profiles import (
    REDDIT_REQUIRED_KEYS,
    TWITTER_REQUIRED_COLUMNS,
    UNSTATED_GENDER,
    SchemaViolation,
    derive_mbti,
    make_username,
    validate_reddit_json,
    validate_twitter_csv,
    write_profiles,
)


@pytest.fixture(scope="module")
def user_info_class():
    """OASIS's own agent-initialisation type. Imported once per module."""
    from oasis.social_platform.config.user import UserInfo

    return UserInfo


@pytest.fixture
def population(make_persona):
    return [
        make_persona(name="Dawn Mercer", gender="female", country="United Kingdom"),
        make_persona(name="Ray Nkemelu", occupation="bus driver", activity_level="high",
                     gender="male", country="United Kingdom"),
        make_persona(name="Jane Doe", occupation="councillor", activity_level="low",
                     provenance="named"),
        make_persona(name="Ursula Ferreira", occupation="full-time carer", age=67,
                     gender="female", country="United Kingdom"),
    ]


@pytest.fixture
def bundle(population, tmp_path):
    return write_profiles(population, tmp_path / "profiles",
                          default_country="United Kingdom")


# --------------------------------------------------------------------------
# Formats
# --------------------------------------------------------------------------


def test_twitter_is_csv_and_reddit_is_json(bundle):
    """`pd.read_csv` on a JSON file raises inside agent generation."""
    assert bundle.twitter_csv.suffix == ".csv"
    assert bundle.reddit_json.suffix == ".json"
    assert bundle.twitter_csv.is_file() and bundle.reddit_json.is_file()


def test_our_own_file_keeps_what_oasis_drops(bundle):
    """The OASIS files are lossy; Phase 8 and the UI need the rest."""
    ours = json.loads(bundle.profiles_json.read_text())
    assert "personality" in ours[0]
    assert {"provenance", "source_entity_uuid"} <= set(ours[0])
    assert ours[0]["username"]


# --------------------------------------------------------------------------
# Twitter: exactly what pandas consumers index
# --------------------------------------------------------------------------


def test_every_required_twitter_column_is_present(bundle):
    rows = list(csv.DictReader(bundle.twitter_csv.open(encoding="utf-8")))
    missing = [c for c in TWITTER_REQUIRED_COLUMNS if c not in rows[0]]
    assert missing == []


def test_prompt_bearing_columns_are_never_empty(bundle):
    rows = list(csv.DictReader(bundle.twitter_csv.open(encoding="utf-8")))
    for row in rows:
        for column in ("username", "description", "user_char"):
            assert row[column].strip(), f"{column} becomes part of a system prompt"


def test_follower_counts_are_integers_and_scale_with_activity(bundle):
    rows = list(csv.DictReader(bundle.twitter_csv.open(encoding="utf-8")))
    assert all(int(r["following_count"]) >= 0 for r in rows)
    assert int(rows[1]["followers_count"]) > int(rows[2]["followers_count"])


@pytest.mark.parametrize("column", ["username", "description", "user_char"])
def test_oasis_twitter_access_pattern_works(bundle, column):
    """Exactly how `generate_twitter_agent_graph` reads it."""
    import pandas as pd

    agent_info = pd.read_csv(bundle.twitter_csv)
    assert str(agent_info[column][0]).strip()


# --------------------------------------------------------------------------
# Reddit: exactly what the JSON loader indexes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", REDDIT_REQUIRED_KEYS)
def test_oasis_reddit_access_pattern_works(bundle, key):
    """Exactly how `generate_reddit_agent_graph` reads it."""
    with bundle.reddit_json.open() as handle:
        agent_info = json.load(handle)
    assert key in agent_info[0]


def test_age_is_an_integer(bundle):
    entries = json.loads(bundle.reddit_json.read_text())
    assert all(isinstance(entry["age"], int) for entry in entries)


def test_synthetic_agents_carry_an_invented_gender(bundle):
    entries = json.loads(bundle.reddit_json.read_text())
    assert entries[0]["gender"] == "female"


def test_a_named_persons_gender_is_not_invented(bundle):
    """The document did not say, so neither do we."""
    entries = json.loads(bundle.reddit_json.read_text())
    named = next(e for e in entries if e["provenance"] == "named")
    assert named["gender"] == UNSTATED_GENDER


# --------------------------------------------------------------------------
# MBTI
# --------------------------------------------------------------------------


def test_mbti_is_derived_from_the_big_five(make_persona):
    """Asking the model separately lets type and personality contradict."""
    introvert = make_persona(personality={
        "openness": 0.7, "conscientiousness": 0.8, "extraversion": 0.3,
        "agreeableness": 0.4, "neuroticism": 0.5})
    extravert = make_persona(personality={
        "openness": 0.2, "conscientiousness": 0.1, "extraversion": 0.9,
        "agreeableness": 0.9, "neuroticism": 0.5})
    assert derive_mbti(introvert) == "INTJ"
    assert derive_mbti(extravert) == "ESFP"
    assert derive_mbti(introvert) == derive_mbti(introvert)


# --------------------------------------------------------------------------
# Usernames
# --------------------------------------------------------------------------


def test_usernames_are_unique_and_handle_shaped(bundle):
    rows = list(csv.DictReader(bundle.twitter_csv.open(encoding="utf-8")))
    handles = [r["username"] for r in rows]
    assert len(set(handles)) == len(handles)
    assert all(h == h.lower() and " " not in h for h in handles)


def test_duplicate_names_get_distinct_handles():
    taken: set[str] = set()
    assert make_username("Dawn Mercer", 0, taken) != make_username("Dawn Mercer", 1, taken)


def test_an_unusable_name_still_yields_a_handle():
    assert make_username("!!!", 5, set()).startswith("agent")


# --------------------------------------------------------------------------
# Validation catches what OASIS would only discover at runtime
# --------------------------------------------------------------------------


def test_a_written_bundle_validates(bundle):
    assert validate_twitter_csv(bundle.twitter_csv) == 4
    assert validate_reddit_json(bundle.reddit_json) == 4


def test_missing_csv_columns_rejected(tmp_path):
    path = tmp_path / "a.csv"
    path.write_text("username,description\nx,y\n")
    with pytest.raises(SchemaViolation, match="user_char"):
        validate_twitter_csv(path)


def test_empty_prompt_field_rejected(tmp_path):
    path = tmp_path / "b.csv"
    path.write_text(",".join(TWITTER_REQUIRED_COLUMNS) + "\n0,N,u,,text,1,2,[]\n")
    with pytest.raises(SchemaViolation, match="description"):
        validate_twitter_csv(path)


def test_non_integer_follower_count_rejected(tmp_path):
    path = tmp_path / "c.csv"
    path.write_text(",".join(TWITTER_REQUIRED_COLUMNS) + "\n0,N,u,d,text,nope,2,[]\n")
    with pytest.raises(SchemaViolation):
        validate_twitter_csv(path)


def test_missing_reddit_keys_rejected(tmp_path):
    path = tmp_path / "d.json"
    path.write_text(json.dumps([{"username": "u", "bio": "b", "persona": "p"}]))
    with pytest.raises(SchemaViolation, match="mbti"):
        validate_reddit_json(path)


def test_string_age_rejected(tmp_path):
    path = tmp_path / "e.json"
    path.write_text(json.dumps([{k: "x" for k in REDDIT_REQUIRED_KEYS}]))
    with pytest.raises(SchemaViolation, match="age"):
        validate_reddit_json(path)


def test_empty_files_rejected(tmp_path):
    path = tmp_path / "f.json"
    path.write_text("[]")
    with pytest.raises(SchemaViolation):
        validate_reddit_json(path)
    with pytest.raises(SchemaViolation):
        write_profiles([], tmp_path / "empty")


# ==========================================================================
# The contract itself: hand the files to OASIS
# ==========================================================================


def _reddit_profile(entry: dict) -> dict:
    """Rebuild exactly the dict `generate_reddit_agent_graph` constructs."""
    return {
        "nodes": [], "edges": [],
        "other_info": {
            "user_profile": entry["persona"], "mbti": entry["mbti"],
            "gender": entry["gender"], "age": entry["age"],
            "country": entry["country"],
        },
    }


def test_oasis_builds_a_reddit_system_message_from_our_file(bundle, user_info_class):
    entries = json.loads(bundle.reddit_json.read_text())
    info = user_info_class(
        name=entries[0]["username"], description=entries[0]["bio"],
        profile=_reddit_profile(entries[0]), recsys_type="reddit",
    )
    message = info.to_system_message()

    assert "Reddit user" in message
    assert "joinery" in message, "our persona reached the agent's prompt"
    assert "female" in message and "41 years old" in message
    assert "United Kingdom" in message


def test_oasis_builds_a_twitter_system_message_from_our_file(bundle, user_info_class):
    import pandas as pd

    agent_info = pd.read_csv(bundle.twitter_csv)
    info = user_info_class(
        name=agent_info["username"][0], description=agent_info["description"][0],
        profile={"nodes": [], "edges": [],
                 "other_info": {"user_profile": agent_info["user_char"][0]}},
        recsys_type="twitter",
    )
    message = info.to_system_message()

    assert "Twitter user" in message
    assert "carpenter" in message


def test_a_named_agents_prompt_states_no_gender(bundle, user_info_class):
    """OASIS interpolates this straight into the agent's own prompt."""
    entries = json.loads(bundle.reddit_json.read_text())
    named = next(e for e in entries if e["provenance"] == "named")
    message = user_info_class(
        name=named["username"], description=named["bio"],
        profile=_reddit_profile(named), recsys_type="reddit",
    ).to_system_message()

    assert f"You are a {UNSTATED_GENDER}" in message, "and it must read as English"
