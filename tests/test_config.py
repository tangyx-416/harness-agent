"""Tests for configuration management."""

import os
from unittest.mock import patch

import pytest

from harness_agent.config import AgentConfig


def test_config_from_env_success():
    """Test successful configuration loading from environment."""
    with patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "test-api-key-123",
            "MODEL_ID": "gpt-4",
            "OPENAI_BASE_URL": "http://localhost:8000/v1",
        },
    ):
        config = AgentConfig.from_env()
        assert config.api_key == "test-api-key-123"
        assert config.model_id == "gpt-4"
        assert config.base_url == "http://localhost:8000/v1"


def test_config_from_env_missing_api_key():
    """Test that missing API key raises ValueError."""
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="OPENAI_API_KEY is not configured"):
            AgentConfig.from_env()


def test_config_from_env_default_model():
    """Test that MODEL_ID has a default value."""
    with patch.dict(
        os.environ,
        {"OPENAI_API_KEY": "test-key"},
        clear=True,
    ):
        config = AgentConfig.from_env()
        assert config.api_key == "test-key"
        assert config.model_id == "gpt-4"  # default value


def test_config_from_env_no_base_url():
    """Test that base_url is optional."""
    with patch.dict(
        os.environ,
        {"OPENAI_API_KEY": "test-key", "MODEL_ID": "gpt-3.5-turbo"},
        clear=True,
    ):
        config = AgentConfig.from_env()
        assert config.api_key == "test-key"
        assert config.model_id == "gpt-3.5-turbo"
        assert config.base_url is None


def test_config_from_env_empty_api_key():
    """Test that empty API key raises ValueError."""
    with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=True):
        with pytest.raises(ValueError, match="OPENAI_API_KEY is not configured"):
            AgentConfig.from_env()
