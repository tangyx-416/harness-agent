"""Tests for CLI functionality."""

import sys
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


def test_cli_module_import():
    """Test that CLI module can be imported."""
    import run_agent

    assert hasattr(run_agent, "main")
    assert hasattr(run_agent, "print_banner")


def test_cli_initialization_without_api_key():
    """Test CLI handles missing API key gracefully."""
    import run_agent

    with patch.dict("os.environ", {}, clear=True):
        result = run_agent.main()

        # Should return error code 1
        assert result == 1


def test_cli_agent_invocation_uses_correct_api():
    """Test that CLI calls agent correctly (not agent.chat)."""
    import run_agent

    # Mock configuration
    with patch("run_agent.AgentConfig") as mock_config_class:
        mock_config = Mock()
        mock_config.api_key = "test-key"
        mock_config.model_id = "gpt-4"
        mock_config.base_url = None
        mock_config_class.from_env.return_value = mock_config

        # Mock agent creation
        with patch("run_agent.create_agent") as mock_create_agent:
            mock_agent_result = Mock()
            mock_agent_result.__str__ = Mock(return_value="Test response")

            # Create a callable mock agent
            mock_agent = Mock(return_value=mock_agent_result)
            mock_create_agent.return_value = mock_agent

            # Mock input/output
            with patch("builtins.input", side_effect=["test message", "exit"]):
                with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
                    result = run_agent.main()

                    # Verify agent was called (not agent.chat)
                    mock_agent.assert_called_once_with("test message")

                    # Verify result was converted to string
                    output = mock_stdout.getvalue()
                    assert "Test response" in output

                    # Should exit successfully
                    assert result == 0


def test_cli_handles_exit_command():
    """Test that CLI exits on 'exit' command."""
    import run_agent

    with patch("run_agent.AgentConfig") as mock_config_class:
        mock_config = Mock()
        mock_config_class.from_env.return_value = mock_config

        with patch("run_agent.create_agent") as mock_create_agent:
            mock_agent = Mock()
            mock_create_agent.return_value = mock_agent

            with patch("builtins.input", side_effect=["exit"]):
                with patch("sys.stdout", new_callable=StringIO):
                    result = run_agent.main()

                    # Should not call agent
                    mock_agent.assert_not_called()

                    # Should exit successfully
                    assert result == 0


def test_cli_handles_quit_command():
    """Test that CLI exits on 'quit' command."""
    import run_agent

    with patch("run_agent.AgentConfig") as mock_config_class:
        mock_config = Mock()
        mock_config_class.from_env.return_value = mock_config

        with patch("run_agent.create_agent") as mock_create_agent:
            mock_agent = Mock()
            mock_create_agent.return_value = mock_agent

            with patch("builtins.input", side_effect=["quit"]):
                with patch("sys.stdout", new_callable=StringIO):
                    result = run_agent.main()

                    mock_agent.assert_not_called()
                    assert result == 0


def test_cli_handles_empty_input():
    """Test that CLI skips empty input."""
    import run_agent

    with patch("run_agent.AgentConfig") as mock_config_class:
        mock_config = Mock()
        mock_config_class.from_env.return_value = mock_config

        with patch("run_agent.create_agent") as mock_create_agent:
            mock_agent = Mock()
            mock_create_agent.return_value = mock_agent

            with patch("builtins.input", side_effect=["", "  ", "exit"]):
                with patch("sys.stdout", new_callable=StringIO):
                    result = run_agent.main()

                    # Should not call agent for empty input
                    mock_agent.assert_not_called()
                    assert result == 0


def test_cli_handles_agent_errors_gracefully():
    """Test that CLI handles agent errors without crashing."""
    import run_agent

    with patch("run_agent.AgentConfig") as mock_config_class:
        mock_config = Mock()
        mock_config_class.from_env.return_value = mock_config

        with patch("run_agent.create_agent") as mock_create_agent:
            # Create mock agent that raises exception when called
            mock_agent = Mock(side_effect=Exception("Test error"))
            mock_create_agent.return_value = mock_agent

            with patch("builtins.input", side_effect=["test", "exit"]):
                with patch("sys.stdout", new_callable=StringIO):
                    with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
                        result = run_agent.main()

                        # Should handle error and continue
                        stderr_output = mock_stderr.getvalue()
                        assert "Error processing request" in stderr_output
                        assert result == 0


def test_cli_keyboard_interrupt():
    """Test that CLI handles Ctrl+C gracefully."""
    import run_agent

    with patch("run_agent.AgentConfig") as mock_config_class:
        mock_config = Mock()
        mock_config_class.from_env.return_value = mock_config

        with patch("run_agent.create_agent") as mock_create_agent:
            mock_agent = Mock()
            mock_create_agent.return_value = mock_agent

            with patch("builtins.input", side_effect=KeyboardInterrupt):
                with patch("sys.stdout", new_callable=StringIO):
                    result = run_agent.main()

                    # Should exit gracefully
                    assert result == 0
