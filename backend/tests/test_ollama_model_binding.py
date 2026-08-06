"""Phase 6 Step 1 — the privacy property, at the unit level.

Named in the spec under Step 6, written here because Step 1 says to verify the
binding before building anything on top of it, and because this is the check
that the whole "sealed by design" claim reduces to. Every agent turn in a run
goes through :func:`build_model`. If it ever returns a cloud-backed model, the
document, the personas and every generated post leave the machine — and nothing
else in the system would notice, because a cloud model answers perfectly well.

The strongest assertion here is not that the right platform is passed. It is
that there is no parameter through which a caller could pass a different one.
"""

from __future__ import annotations

import inspect

import pytest

from app.config import Config
from app.services.simulation_runner import ModelBindingError, build_model


@pytest.fixture
def local_config(config: Config) -> Config:
    return config


# --------------------------------------------------------------------------
# What gets built
# --------------------------------------------------------------------------


def test_the_model_is_ollama_backed(local_config):
    assert type(build_model(local_config)).__name__ == "OllamaModel"


def test_the_platform_is_ollama_not_openai(local_config):
    from camel.types import ModelPlatformType

    model = build_model(local_config)
    assert model.model_type == local_config.LLM_MODEL_NAME
    # The platform is not stored on the model, so assert what identifies one:
    # an OpenAI-backed model would not be an OllamaModel at all.
    assert ModelPlatformType.OLLAMA.value == "ollama"
    assert "openai" not in type(model).__name__.lower()


def test_it_points_at_the_configured_local_url(local_config):
    assert build_model(local_config)._url == local_config.LLM_BASE_URL


def test_the_temperature_comes_from_configuration(local_config):
    assert (build_model(local_config).model_config_dict["temperature"]
            == local_config.SIMULATION_TEMPERATURE)


def test_the_temperature_can_be_overridden_per_call(local_config):
    assert build_model(local_config, temperature=0.1).model_config_dict["temperature"] == 0.1


def test_a_different_local_model_can_be_named(local_config):
    assert build_model(local_config, model_name="qwen2.5:32b").model_type == "qwen2.5:32b"


# --------------------------------------------------------------------------
# What cannot be built
# --------------------------------------------------------------------------


def test_NO_CODE_PATH_CAN_ASK_FOR_ANOTHER_PLATFORM():
    """The guarantee is structural, not a convention someone must remember."""
    parameters = set(inspect.signature(build_model).parameters)
    assert "model_platform" not in parameters
    assert "platform" not in parameters
    assert parameters == {"config", "temperature", "model_name"}


@pytest.mark.parametrize(("url", "why"), [
    ("https://api.openai.com/v1", "the OpenAI API"),
    ("https://api.anthropic.com/v1", "another vendor"),
    ("http://8.8.8.8:11434/v1", "a public IP"),
    ("http://ollama.example.com:11434/v1", "a public hostname"),
])
def test_a_public_endpoint_is_refused(config, url, why):
    """Config refuses these too; this proves the runner does not rely on that."""
    with pytest.raises(ModelBindingError, match="public host"):
        build_model(config.model_copy(update={"LLM_BASE_URL": url}))


def test_an_empty_url_is_refused_with_the_reason(config):
    """camel would fall back to starting its own server via subprocess."""
    with pytest.raises(ModelBindingError, match="start its own Ollama"):
        build_model(config.model_copy(update={"LLM_BASE_URL": ""}))


def test_configuration_refuses_a_cloud_endpoint_as_well():
    """Defence in depth: the perimeter check and the binding check are separate."""
    from app.config import ConfigError

    with pytest.raises(ConfigError):
        Config(_env_file=None, NEO4J_PASSWORD="x", ALLOWED_HOSTS=[],
               LLM_BASE_URL="https://api.openai.com/v1")


@pytest.mark.parametrize("url", [
    "http://localhost:11434/v1",
    "http://127.0.0.1:11434/v1",
    "http://ollama:11434/v1",
    "http://192.168.1.50:11434/v1",
])
def test_every_permitted_local_form_still_works(config, url):
    """A guard that refuses valid deployments would be worked around."""
    assert build_model(config.model_copy(update={"LLM_BASE_URL": url}))._url == url
