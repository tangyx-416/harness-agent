"""Agent factory for creating and configuring the Harness Agent."""

from pathlib import Path
from typing import Any

from strands import Agent, tool
from strands.models.openai import OpenAIModel

from .config import AgentConfig, get_prompts_dir
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
    @tool(name="inspect_project")
    def inspect_project_tool(directory: str = ".") -> dict[str, Any]:
        """Inspect the current project structure and return basic information.

        This tool analyzes the project directory structure, checks for common
        project files, and provides an overview of the repository.

        Args:
            directory: Directory to inspect (default: current directory)

        Returns:
            Dictionary containing project information including working directory,
            whether it's a git repo, presence of common files, and directory structure.
        """
        return inspect_project(directory)

    # Create agent
    agent = Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[
            inspect_project_tool,
            list_directory,
            read_file,
            search_code,
            analyze_dependencies,
        ],
        name="HarnessAgent",
        description="A project repository assistant agent",
    )

    return agent
