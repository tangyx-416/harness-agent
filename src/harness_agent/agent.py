"""Agent factory for creating and configuring the Harness Agent."""

from pathlib import Path
from typing import Any

from strands import Agent, tool
from strands.models.openai import OpenAIModel

from .config import AgentConfig, get_prompts_dir
from .tools.execution_tools import get_execution_result, prepare_command
from .tools.git_tools import (
    git_branches_tool,
    git_diff_tool,
    git_log_tool,
    git_status_tool,
)
from .tools.project_tools import inspect_project
from .tools.repository_tools import (
    analyze_dependencies,
    list_directory,
    read_file,
    search_code,
)


def load_system_prompt() -> str:
    """Load the system prompt from the prompts directory.

    Returns:
        str: The system prompt content
    """
    prompt_path = get_prompts_dir() / "system.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"System prompt not found at {prompt_path}")

    return prompt_path.read_text(encoding="utf-8")


def create_agent(config: AgentConfig | None = None) -> Agent:
    """Create and configure a Harness Agent.

    This factory function:
    1. Loads configuration from environment variables
    2. Initializes the OpenAI model
    3. Loads the system prompt
    4. Registers available tools
    5. Creates and returns the Agent instance

    Args:
        config: Optional configuration. If not provided, loads from environment.

    Returns:
        Agent: Configured agent ready to process requests

    Raises:
        ValueError: If required configuration is missing
        FileNotFoundError: If system prompt file is not found
    """
    # Load configuration
    if config is None:
        config = AgentConfig.from_env()

    # Initialize OpenAI model
    # Build client_args for OpenAI client initialization
    client_args: dict[str, Any] = {
        "api_key": config.api_key,
    }

    if config.base_url:
        client_args["base_url"] = config.base_url

    # Create model with model_id in model_config and client settings in client_args
    model = OpenAIModel(
        client_args=client_args,
        model_id=config.model_id,
    )

    # Load system prompt
    system_prompt = load_system_prompt()

    # Create tool - Strands @tool decorator makes functions into tools.
    # The public model-facing name is 'inspect_project' (matches docs and
    # the other repository tools); the Python wrapper keeps a distinct name.
    # v0.3.0: inspect_project now enforces repository-root confinement via
    # path_utils, identical to the other repository tools.
    @tool(name="inspect_project")
    def inspect_project_tool(directory: str = ".") -> dict[str, Any]:
        """Inspect a directory inside the repository and return basic information.

        This tool analyzes the project directory structure, checks for common
        project files, and provides an overview of the repository. Requests
        outside the repository root are refused.

        Args:
            directory: Directory to inspect relative to the repository root
                (default: repository root)

        Returns:
            Dictionary containing project information including working directory,
            whether it's a git repo, presence of common files, and directory structure.
        """
        return inspect_project(directory)

    # Create agent
    # v0.3.0: prepare_command / get_execution_result are the only execution
    # related tools exposed to the model. They can never run anything;
    # approval and subprocess execution stay in the trusted host layer.
    # v0.4.0: git_status / git_diff / git_log / git_branches add READ-ONLY
    # Git awareness -- fixed introspection argv, no mutation, no network.
    agent = Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[
            inspect_project_tool,
            list_directory,
            read_file,
            search_code,
            analyze_dependencies,
            prepare_command,
            get_execution_result,
            git_status_tool,
            git_diff_tool,
            git_log_tool,
            git_branches_tool,
        ],
        name="HarnessAgent",
        description="A project repository assistant agent",
    )

    return agent
