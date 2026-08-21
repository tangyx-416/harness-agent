"""Tests for agent factory."""

import os
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from harness_agent.agent import create_agent, load_system_prompt
from harness_agent.config import AgentConfig


def test_load_system_prompt_success():
    """Test successful system prompt loading."""
    prompt = load_system_prompt()

    assert isinstance(prompt, str)
    assert len(prompt) > 0
    assert "Project Repository Assistant" in prompt


def test_load_system_prompt_file_exists():
    """Test that system prompt file exists at expected location."""
    from harness_agent.config import get_prompts_dir

    prompt_path = get_prompts_dir() / "system.md"
    assert prompt_path.exists()
    assert prompt_path.is_file()


def test_create_agent_with_valid_config():
    """Test agent creation with valid configuration."""
    config = AgentConfig(
        api_key="test-key-123",
        model_id="gpt-4",
        base_url=None,
    )

    # Mock OpenAIModel to avoid actual API initialization
    with patch("harness_agent.agent.OpenAIModel") as mock_model_class:
        mock_model = Mock()
        mock_model_class.return_value = mock_model

        agent = create_agent(config)

        # Verify OpenAIModel was called with correct structure
        mock_model_class.assert_called_once()
        call_kwargs = mock_model_class.call_args.kwargs

        # Check client_args structure
        assert "client_args" in call_kwargs
        assert call_kwargs["client_args"]["api_key"] == "test-key-123"
        assert "base_url" not in call_kwargs["client_args"]

        # Check model_id is passed as kwarg
        assert "model_id" in call_kwargs
        assert call_kwargs["model_id"] == "gpt-4"

        # Verify agent was created
        assert agent is not None


def test_create_agent_with_base_url():
    """Test agent creation with custom base URL."""
    config = AgentConfig(
        api_key="test-key-456",
        model_id="gpt-3.5-turbo",
        base_url="http://localhost:8000/v1",
    )

    with patch("harness_agent.agent.OpenAIModel") as mock_model_class:
        mock_model = Mock()
        mock_model_class.return_value = mock_model

        agent = create_agent(config)

        # Verify base_url is in client_args when provided
        call_kwargs = mock_model_class.call_args.kwargs
        assert call_kwargs["client_args"]["api_key"] == "test-key-456"
        assert call_kwargs["client_args"]["base_url"] == "http://localhost:8000/v1"
        assert call_kwargs["model_id"] == "gpt-3.5-turbo"


def test_create_agent_loads_from_env():
    """Test agent creation loads config from environment when not provided."""
    with patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "env-key-789",
            "MODEL_ID": "gpt-4",
        },
        clear=True,
    ):
        with patch("harness_agent.agent.OpenAIModel") as mock_model_class:
            mock_model = Mock()
            mock_model_class.return_value = mock_model

            agent = create_agent()

            # Verify environment config was used
            call_kwargs = mock_model_class.call_args.kwargs
            assert call_kwargs["client_args"]["api_key"] == "env-key-789"
            assert call_kwargs["model_id"] == "gpt-4"


def test_create_agent_registers_tools():
    """Test that agent factory registers inspect_project tool."""
    config = AgentConfig(
        api_key="test-key",
        model_id="gpt-4",
        base_url=None,
    )

    with patch("harness_agent.agent.OpenAIModel") as mock_model_class:
        mock_model = Mock()
        mock_model_class.return_value = mock_model

        with patch("harness_agent.agent.Agent") as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent

            agent = create_agent(config)

            # Verify Agent was called with tools
            mock_agent_class.assert_called_once()
            call_kwargs = mock_agent_class.call_args.kwargs

            assert "tools" in call_kwargs
            assert len(call_kwargs["tools"]) > 0

            # Verify system_prompt was passed
            assert "system_prompt" in call_kwargs
            assert len(call_kwargs["system_prompt"]) > 0


def test_create_agent_with_missing_api_key():
    """Test that agent creation fails with missing API key."""
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="OPENAI_API_KEY is not configured"):
            create_agent()
