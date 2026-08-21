#!/usr/bin/env python
"""CLI interface for the Harness Agent.

This script provides an interactive terminal interface for communicating
with the Harness Agent.
"""

import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from harness_agent.agent import create_agent
from harness_agent.config import AgentConfig


def print_banner():
    """Print welcome banner."""
    print("=" * 60)
    print("Harness Agent v0.1")
    print("Type 'exit' or 'quit' to stop, Ctrl+C to interrupt.")
    print("=" * 60)
    print()


def main():
    """Run the interactive agent CLI."""
    # Load configuration and create agent
    try:
        config = AgentConfig.from_env()
        agent = create_agent(config)
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
