"""Agent factory for creating and configuring the Harness Agent."""

from pathlib import Path
from typing import Any

from strands import Agent, tool
from strands.models.openai import OpenAIModel

from .config import AgentConfig, get_prompts_dir
from .execution import ExecutionBroker
from .git_fetch import GitFetchBroker
from .git_mutation import GitMutationBroker
from .git_remote import GitRemoteBroker
from .patch import PatchBroker
from .session import SessionState
from .tools.execution_tools import make_execution_tools
from .tools.git_fetch_tools import make_git_fetch_tools
from .tools.git_mutation_tools import make_git_mutation_tools
from .tools.git_remote_tools import make_git_remote_tools
from .tools.git_tools import (
    git_branches_tool,
    git_diff_tool,
    git_log_tool,
    git_status_tool,
)
from .tools.patch_tools import make_patch_tools
from .tools.project_tools import inspect_project
from .tools.repository_tools import (
    analyze_dependencies,
    list_directory,
    read_file,
    search_code,
)
from .tools.task_tools import make_task_tools


def load_system_prompt() -> str:
    """Load the system prompt from the prompts directory.

    Returns:
        str: The system prompt content
    """
    prompt_path = get_prompts_dir() / "system.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"System prompt not found at {prompt_path}")

    return prompt_path.read_text(encoding="utf-8")


def create_agent(
    config: AgentConfig | None = None,
    session_state: SessionState | None = None,
    execution_broker: ExecutionBroker | None = None,
    patch_broker: PatchBroker | None = None,
    git_mutation_broker: GitMutationBroker | None = None,
    git_remote_broker: GitRemoteBroker | None = None,
    git_fetch_broker: GitFetchBroker | None = None,
) -> Agent:
    """Create and configure a Harness Agent.

    This factory function:
    1. Loads configuration from environment variables
    2. Initializes the OpenAI model
    3. Loads the system prompt
    4. Registers available tools
    5. Binds task-state tools to one isolated in-memory session
    6. Binds execution-request tools to one isolated in-memory broker
    7. Binds patch-request tools to one isolated in-memory broker
    8. Binds Git mutation-request tools to one isolated in-memory broker
    9. Binds Git remote push-request tools to one isolated in-memory broker
    10. Binds Git remote fetch-request tools to one isolated in-memory broker
    11. Creates and returns the Agent instance

    Args:
        config: Optional configuration. If not provided, loads from environment.
        session_state: Optional explicit ephemeral task state. A fresh private
            state is created when omitted.
        execution_broker: Optional explicit execution broker. A fresh private
            broker is created when omitted.
        patch_broker: Optional explicit patch broker. A fresh private broker is
            created when omitted.
        git_mutation_broker: Optional explicit Git mutation broker. A fresh
            private broker is created when omitted.
        git_remote_broker: Optional explicit Git remote broker. A fresh private
            broker is created when omitted.
        git_fetch_broker: Optional explicit Git fetch broker. A fresh private
            broker is created when omitted.

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

    # v0.5.0: every factory call owns an isolated task state unless the host
    # explicitly supplies one. This is intentionally separate from Strands'
    # conversation history and persistence-oriented SessionManager APIs.
    bound_session_state = session_state or SessionState()
    task_tools = make_task_tools(bound_session_state)

    # v0.5.0: every factory call owns an isolated execution broker unless the
    # host explicitly supplies one. This prevents cross-agent plan leakage
    # when two Agent instances coexist in the same process.
    bound_broker = execution_broker or ExecutionBroker()
    exec_prepare, exec_result = make_execution_tools(bound_broker)

    # v0.6.0: every factory call owns an isolated patch broker unless the host
    # explicitly supplies one. It holds only pending/approved patch proposals
    # for THIS agent; source edits are applied only by the host layer.
    bound_patch_broker = patch_broker or PatchBroker()
    patch_prepare, patch_result = make_patch_tools(bound_patch_broker)

    # v0.7.0: every factory call owns an isolated Git mutation broker unless the
    # host explicitly supplies one. It holds only pending/approved Git mutation
    # proposals for THIS agent; Git mutations are applied only by the host layer.
    bound_git_mutation_broker = git_mutation_broker or GitMutationBroker()
    git_stage, git_commit, git_mutation_result = make_git_mutation_tools(
        bound_git_mutation_broker
    )

    # v0.8.0: every factory call owns an isolated Git remote broker unless the
    # host explicitly supplies one. It holds only pending/approved Git remote push
    # proposals for THIS agent; remote pushes are applied only by the host layer.
    bound_git_remote_broker = git_remote_broker or GitRemoteBroker()
    git_push, git_push_result = make_git_remote_tools(
        Path.cwd(), bound_git_remote_broker
    )

    # v0.9.0: every factory call owns an isolated Git fetch broker unless the
    # host explicitly supplies one. It holds only pending/approved Git remote fetch
    # proposals for THIS agent; remote fetches are applied only by the host layer.
    bound_git_fetch_broker = git_fetch_broker or GitFetchBroker()
    git_fetch, git_fetch_result = make_git_fetch_tools(
        Path.cwd(), bound_git_fetch_broker, bound_session_state
    )

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
    # v0.5.0: four closure-bound task tools and two closure-bound execution
    # tools mutate only the explicit, process-local SessionState and broker.
    # They add no execution or mutation authority.
    # v0.6.0: two closure-bound patch tools validate + register pending source
    # edit proposals in the explicit patch broker. They never write files.
    # v0.7.0: three closure-bound Git mutation tools validate + register pending
    # Git stage/commit proposals in the explicit Git mutation broker. They never
    # mutate the Git index or create commits.
    # v0.8.0: two closure-bound Git remote push tools validate + register pending
    # remote push proposals in the explicit Git remote broker. They perform zero
    # network operations during prepare.
    # v0.9.0: two closure-bound Git remote fetch tools validate + register pending
    # remote fetch proposals in the explicit Git fetch broker. They perform zero
    # network operations during prepare.
    agent = Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[
            inspect_project_tool,
            list_directory,
            read_file,
            search_code,
            analyze_dependencies,
            exec_prepare,
            exec_result,
            git_status_tool,
            git_diff_tool,
            git_log_tool,
            git_branches_tool,
            *task_tools,
            patch_prepare,
            patch_result,
            git_stage,
            git_commit,
            git_mutation_result,
            git_push,
            git_push_result,
            git_fetch,
            git_fetch_result,
        ],
        name="HarnessAgent",
        description="A project repository assistant agent",
    )

    return agent
