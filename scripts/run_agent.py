#!/usr/bin/env python
"""CLI interface for the Harness Agent.

This script provides an interactive terminal interface for communicating
with the Harness Agent.

v0.3.0: after every agent turn, any pending execution plans prepared by
the agent are shown to the user with an explicit approval prompt. The
CLI is the trusted host layer -- the model can never approve or execute
commands on its own.

v0.5.0: each CLI process owns one explicit ephemeral SessionState and one
explicit ExecutionBroker. Host-verified approval/rejection/completion
metadata is recorded in that same state, without retaining stdout, stderr,
environment data or secrets. The agent's execution-request tools are bound
to that same broker, so task state, execution plans and execution results
all belong to one CLI runtime.
"""

import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from harness_agent.agent import create_agent
from harness_agent.config import AgentConfig
from harness_agent.execution import (
    ExecutionBroker,
    execute_approved,
    get_default_broker,
)
from harness_agent.session import SessionState

#: Input lines that count as explicit approval. Everything else -- empty
#: input (Enter), 'n', 'no', random strings -- is a NO. There is no flag
#: or environment variable that bypasses this prompt.
_APPROVAL_WORDS = frozenset({"y", "yes"})

#: How much captured output is echoed after an execution.
_RESULT_ECHO_CHARS = 2000


def print_banner():
    """Print welcome banner."""
    print("=" * 60)
    print("Harness Agent v0.5")
    print("Session state: ephemeral (cleared when this CLI exits).")
    print("Type 'exit' or 'quit' to stop, Ctrl+C to interrupt.")
    print("=" * 60)
    print()


def show_approval_request(plan) -> None:
    """Render the approval block for one pending execution plan."""
    print("-" * 60)
    print("Execution approval required")
    print()
    print("Command:")
    print(f"  {plan.display_command}")
    print()
    print("Working directory:")
    print(f"  {plan.cwd}")
    print()
    print(f"Risk ({plan.risk_level}):")
    print(f"  {plan.risk_reason}")
    print()
    print("Note:")
    print("  Execution runs with your current OS-user privileges.")
    print("  This is not an OS-level sandbox.")
    print()
    print("Timeout:")
    print(f"  {plan.timeout_seconds} seconds")
    print()


def request_approval(plan) -> bool:
    """Ask the user to approve one plan. Only explicit y/yes approves."""
    show_approval_request(plan)
    try:
        answer = input("Approve? [y/N]: ")
    except (KeyboardInterrupt, EOFError):
        print("\n(Cancelled)")
        return False
    return answer.strip().lower() in _APPROVAL_WORDS


def _echo_result(result) -> None:
    """Print a compact summary of an ExecutionResult."""
    print("-" * 60)
    status = "timed out" if result.timed_out else f"exit code {result.exit_code}"
    print(
        f"Execution finished: {result.command} -> {status} "
        f"({result.duration_ms} ms)"
    )
    if result.stdout_truncated:
        print(f"[stdout truncated to {len(result.stdout)} characters]")
    if result.stderr_truncated:
        print(f"[stderr truncated to {len(result.stderr)} characters]")

    for stream_name, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        body = text.strip()
        if not body:
            continue
        print(f"{stream_name}:")
        if len(body) > _RESULT_ECHO_CHARS:
            body = body[-_RESULT_ECHO_CHARS:]
            print(f"{body}\n[... tail only, {stream_name} was truncated ...]")
        else:
            print(body)
    print("-" * 60)


def process_pending_executions(
    broker: ExecutionBroker | None = None,
    session_state: SessionState | None = None,
) -> None:
    """Show pending plans to the user and run the approved ones.

    This is the trusted host layer: approval decisions are made here by
    the human user, never by the model.
    """
    broker = broker if broker is not None else get_default_broker()
    for plan in broker.pending():
        if not request_approval(plan):
            broker.reject(plan.id)
            if session_state is not None:
                session_state.record_execution_rejected(plan.id)
            print("Execution cancelled: nothing was run.\n")
            continue
        try:
            broker.approve(plan.id)
            if session_state is not None:
                session_state.record_execution_approved(plan.id)
            result = execute_approved(broker, plan.id)
        except Exception as exc:  # broker/service guard; never crash the CLI
            print(f"Execution error: {exc}\n")
            continue
        if session_state is not None:
            session_state.record_execution_completed(
                plan.id,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                duration_ms=result.duration_ms,
                stdout_truncated=result.stdout_truncated,
                stderr_truncated=result.stderr_truncated,
            )
        _echo_result(result)


def main():
    """Run the interactive agent CLI."""
    # Load configuration and create agent
    try:
        config = AgentConfig.from_env()
        session_state = SessionState()
        execution_broker = ExecutionBroker()
        agent = create_agent(
            config,
            session_state=session_state,
            execution_broker=execution_broker,
        )
    except ValueError as e:
        print(f"Configuration Error: {e}", file=sys.stderr)
        print("\nPlease ensure your .env file is configured correctly.", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Failed to initialize agent: {e}", file=sys.stderr)
        return 1

    print_banner()

    # Interactive loop
    while True:
        try:
            # Get user input
            user_input = input("You > ").strip()

            # Check for exit commands
            if user_input.lower() in ("exit", "quit", "q"):
                print("\nGoodbye!")
                break

            # Skip empty input
            if not user_input:
                continue

            # Process the request
            try:
                # Call agent with prompt - returns AgentResult
                result = agent(user_input)

                # AgentResult has __str__ that returns the final text response
                print(f"\nAgent > {str(result)}\n")

                # v0.3.0: the agent may have prepared execution plans.
                # The user approves/rejects them here, in the host layer.
                # v0.5.0: this CLI's own broker is used, never a shared one.
                process_pending_executions(
                    broker=execution_broker, session_state=session_state
                )

            except KeyboardInterrupt:
                print("\n\n(Interrupted)")
                continue
            except Exception as e:
                print(f"\nError processing request: {e}", file=sys.stderr)
                print("The agent encountered an error. You can try again.\n")
                continue

        except KeyboardInterrupt:
            print("\n\nGoodbye!")
            break
        except EOFError:
            print("\n\nGoodbye!")
            break

    return 0


if __name__ == "__main__":
    sys.exit(main())
