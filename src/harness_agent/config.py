"""Configuration management for Harness Agent."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


@dataclass
class AgentConfig:
    """Configuration for the Harness Agent."""

    api_key: str
    model_id: str
    base_url: Optional[str] = None

    @classmethod
    def from_env(cls) -> "AgentConfig":
        """Load configuration from environment variables.

        Returns:
            AgentConfig: Configuration instance

        Raises:
            ValueError: If required environment variables are missing
        """
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY is not configured.\n"
                "Copy .env.example to .env and configure your API key."
            )

        model_id = os.getenv("MODEL_ID", "gpt-4")
        if not model_id:
            raise ValueError("MODEL_ID must be specified")

        base_url = os.getenv("OPENAI_BASE_URL")

        return cls(
            api_key=api_key,
            model_id=model_id,
            base_url=base_url,
        )


def get_prompts_dir() -> Path:
    """Get the prompts directory path."""
    return Path(__file__).parent / "prompts"
