"""Phase 4 Step 1 — coping with what models actually return.

A repair round trip costs 30-90 s of local inference. Rejecting `"thirty-four"`
when the intended reading is unambiguous spends that for nothing, so drift is
coerced where it can be read and rejected only where it cannot.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.profile_generator import (
    PersonaProfile,
    normalise_age,
    normalise_unit_interval,
)


# --------------------------------------------------------------------------
# Age
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (34, 34),
        ("34", 34),
        (41.7, 41),
        ("thirty-four", 34),
        ("seventy", 70),
        ("34 years old", 34),
        ("age 52", 52),
        ("mid-thirties", 35),
        ("early forties", 42),
        ("late sixties", 68),
    ],
)
def test_age_drift_is_read(raw, expected):
    assert normalise_age(raw) == expected


@pytest.mark.parametrize("raw", ["", "unknown", None, True, 7, 150])
def test_unreadable_or_implausible_ages_are_rejected(raw):
    with pytest.raises((ValueError, TypeError)):
        normalise_age(raw)


# --------------------------------------------------------------------------
# Personality scores
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.7, 0.7), ("0.7", 0.7), (0.0, 0.0), (1.0, 1.0),
        (70, 0.7), ("70%", 0.7), (95, 0.95),        # percentages
        (7, 0.7), (2, 0.2), (8, 0.8),               # 1-10 scale
        ("high", 0.75), ("low", 0.25), ("very high", 0.9),
        (1.4, 1.0),                                  # overshoot of 0..1
        (-3, 0.0),                                   # clamped
    ],
)
def test_score_scales_are_disambiguated(raw, expected):
    """Above 1.0 the intended scale is ambiguous; shape decides."""
    assert normalise_unit_interval(raw) == pytest.approx(expected)


def test_an_unreadable_score_is_rejected():
    with pytest.raises(ValueError):
        normalise_unit_interval("banana")


# --------------------------------------------------------------------------
# Field-name drift
# --------------------------------------------------------------------------


@pytest.fixture
def drifted() -> dict:
    """A response using every alias a model has been seen to pick."""
    return {
        "name": "Ray Nkemelu",
        "age": "thirty-eight",
        "job": "bus driver",
        "bio": "Drives the 42 route.",
        "big_five": {"openness": 80, "extraversion": "high"},
        "personality_traits": "patient, dry humour",
        "hobbies": "allotment; darts",
        "political_leaning": "left",
        "posting_frequency": 0.9,
        "tone": "chatty",
    }


def test_renamed_fields_are_mapped(drifted):
    profile = PersonaProfile.model_validate(drifted)
    assert profile.occupation == "bus driver"
    assert profile.background.startswith("Drives")
    assert profile.writing_style == "chatty"
    assert profile.leanings == "left"


def test_nested_scores_are_coerced(drifted):
    profile = PersonaProfile.model_validate(drifted)
    assert profile.personality.openness == pytest.approx(0.8)
    assert profile.personality.extraversion == pytest.approx(0.75)


@pytest.mark.parametrize(
    ("field", "expected"),
    [("traits", ["patient", "dry humour"]), ("interests", ["allotment", "darts"])],
)
def test_string_lists_are_split(drifted, field, expected):
    assert getattr(PersonaProfile.model_validate(drifted), field) == expected


def test_numeric_activity_maps_to_a_level(drifted):
    assert PersonaProfile.model_validate(drifted).activity_level == "high"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("low", "low"), ("medium", "moderate"), ("very high", "high"),
     ("lurker", "low"), (0.1, "low"), (0.5, "moderate"), (0.9, "high"),
     ("sporadic", "moderate")],
)
def test_activity_level_variants(persona_data, raw, expected):
    profile = PersonaProfile.model_validate({**persona_data, "activity_level": raw})
    assert profile.activity_level == expected


# --------------------------------------------------------------------------
# Defaults and rejection
# --------------------------------------------------------------------------


def test_missing_personality_defaults_to_mid_range(persona_data):
    persona_data.pop("personality")
    assert PersonaProfile.model_validate(persona_data).personality.openness == 0.5


def test_missing_activity_defaults_to_moderate(persona_data):
    persona_data.pop("activity_level")
    assert PersonaProfile.model_validate(persona_data).activity_level == "moderate"


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", ""), ("occupation", "  "), ("age", "unknowable")],
)
def test_unusable_values_are_rejected_cleanly(persona_data, field, value):
    with pytest.raises(ValidationError):
        PersonaProfile.model_validate({**persona_data, field: value})


def test_list_valued_prose_fields_are_flattened(persona_data):
    profile = PersonaProfile.model_validate(
        {**persona_data, "leanings": ["pro-housing", "anti-traffic"]}
    )
    assert profile.leanings == "pro-housing, anti-traffic"
