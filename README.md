# Harness Project Agent

**Version: 0.1.1**  
**Status: Frozen Baseline / Runnable Single-Agent MVP**

A Single-Agent MVP built on the Strands Agents SDK, designed to help analyze and understand code repositories through natural language interaction.

## Overview

This project implements a conversational AI agent that can inspect and analyze project structures. It serves as a foundation for building more advanced repository management and development assistance tools.

## Architecture

```text
User Input
    ↓
Agent Harness (Strands)
    ├─ OpenAI Model
    ├─ System Prompt
    └─ Tools
        └─ inspect_project
            ↓
    Project Analysis
```

The agent uses a simple but extensible architecture:
- **Agent Harness**: Strands Agents SDK manages the agent loop, tool calling, and conversation flow
- **Model**: OpenAI-compatible language model for understanding and generation
- **Tools**: Python functions decorated with `@tool` that the agent can call
- **System Prompt**: Defines the agent's behavior and guidelines

## Requirements

- **Python**: >= 3.10 (tested with Python 3.11)
- **Strands Agents SDK**: 1.52.0+
- **OpenAI SDK**: For model provider

## Installation

1. **Clone the repository**:
   ```bash
   git clone <your-repo-url>
   cd Harness Agent
   ```

2. **Create a virtual environment**:
   ```bash
   python -m venv .venv
   ```

3. **Activate the virtual environment**:
   
   **Windows (PowerShell)**:
   ```powershell
   .venv\Scripts\Activate.ps1
   ```
   
   **Windows (Git Bash)**:
   ```bash
   source .venv/Scripts/activate
   ```
   
   **Linux/macOS**:
   ```bash
   source .venv/bin/activate
   ```

4. **Install the project**:
   ```bash
   pip install -e .
   ```

5. **Install development dependencies** (optional):
   ```bash
   pip install -e ".[dev]"
   ```

## Configuration

1. **Copy the example environment file**:
   
   **Windows (PowerShell)**:
   ```powershell
   Copy-Item .env.example .env
   ```
   
   **Linux/macOS/Git Bash**:
   ```bash
   cp .env.example .env
   ```

2. **Edit `.env` and configure your settings**:
   ```env
   OPENAI_API_KEY=your_actual_api_key_here
   MODEL_ID=gpt-4
   # OPENAI_BASE_URL=http://localhost:8000/v1  # Optional: for local models
   ```

### Configuration Options

- **OPENAI_API_KEY** (required): Your OpenAI API key or compatible API key
- **MODEL_ID** (optional, default: `gpt-4`): Model identifier (e.g., `gpt-4`, `gpt-3.5-turbo`)
- **OPENAI_BASE_URL** (optional): Custom endpoint for OpenAI-compatible APIs (useful for local models like vLLM)

## Usage

Run the interactive CLI:

```bash
python scripts/run_agent.py
```

Example interaction:
```text
============================================================
Harness Agent v0.1
Type 'exit' or 'quit' to stop, Ctrl+C to interrupt.
============================================================

You > What's the structure of this project?

Agent > [analyzes and responds]

You > exit
Goodbye!
```

## Testing

Run the test suite:

```bash
pytest
```

Run with verbose output:

```bash
pytest -v
```

Run with coverage:

```bash
pytest --cov=harness_agent
```

## Current Features (v0.1.1)

✅ **Single Agent**: One conversational agent with clear responsibilities  
✅ **OpenAI-Compatible Models**: Works with OpenAI, local vLLM, and other compatible endpoints  
✅ **System Prompt**: Configurable agent behavior through markdown prompt files  
✅ **Project Inspection Tool**: Read-only analysis of repository structure  
✅ **CLI Interface**: Interactive terminal-based conversation  
✅ **Configuration Management**: Environment-based config with validation  
✅ **Deterministic Tests**: 27 passing tests with mocked dependencies  

### Verification Status

- **Tests**: 27/27 deterministic/mock tests passing
- **SDK Integration**: Verified locally with Strands Agents 1.52.0
- **Real LLM Execution**: Requires a valid OpenAI API key
- **Live API Smoke Test**: Has not yet been performed in this environment

This is a **stable development baseline**, not a production-ready system.  

### Safety Features

- **Read-only by default**: Current tools only inspect, never modify
- **Sensitive file filtering**: Automatically excludes `.env`, credentials, keys, etc.
- **No arbitrary execution**: Tools are explicitly defined, no shell access
- **Error handling**: Graceful degradation on failures

## Project Structure

```text
Harness Agent/
├── src/
│   └── harness_agent/
│       ├── __init__.py
│       ├── agent.py           # Agent factory and initialization
│       ├── config.py           # Configuration management
│       ├── prompts/
│       │   └── system.md       # System prompt definition
│       └── tools/
│           ├── __init__.py
│           └── project_tools.py  # Project inspection tools
├── scripts/
│   └── run_agent.py           # CLI entry point
├── tests/
│   ├── test_config.py         # Configuration tests
│   └── test_tools.py          # Tool tests
├── .env.example               # Example environment configuration
├── .gitignore                 # Git ignore patterns
├── pyproject.toml             # Project metadata and dependencies
├── README.md                  # This file
└── LICENSE                    # MIT License
```

## Roadmap

This is v0.1.1 - a minimal viable foundation. Future versions will add:

- **v0.2**: File reading tools (read source code, configuration files)
- **v0.3**: Safe shell execution tools (with user confirmation)
- **v0.4**: Git integration tools (status, diff, log)
- **v0.5**: Memory system (conversation history, learned facts)
- **v0.6**: Planning capabilities (multi-step task decomposition)
- **v0.7**: Multi-agent coordination (specialist agents for different tasks)
- **v1.0**: Full Agent Harness (orchestration, monitoring, evaluation)

## Development Status

**v0.1.1 is the frozen baseline.**

### Implemented

- Single Strands Agent
- OpenAI-compatible model provider
- Project inspection tool (read-only)
- Read-only safety boundary
- CLI interface
- Environment configuration
- 27 deterministic tests

### Not Implemented

- File reading/writing
- Code search
- Shell execution
- File modification
- Git operations
- Memory system
- Planning capabilities
- Multi-Agent orchestration
- Web UI
- Database persistence

## Live Smoke Test

After configuring a valid API key in `.env`:

```bash
python scripts/run_agent.py
```

Test the following interactions:

1. **Tool discovery**:
   ```
   You > What tools do you have?
   ```

2. **Project inspection**:
   ```
   You > Inspect this project and summarize its top-level structure.
   ```

**Note**: This is not part of the automated test suite as it requires a real API key and incurs API costs. All automated tests use mocked dependencies.

## Development

### Adding New Tools

1. Define a function in `src/harness_agent/tools/`:
   ```python
   def my_new_tool(param: str) -> dict:
       """Tool description.
       
       Args:
           param: Parameter description
           
       Returns:
           Result dictionary
       """
       # Implementation
       return {"result": "data"}
   ```

2. Register it in `agent.py`:
   ```python
   @tool
   def my_new_tool_wrapped(param: str) -> dict:
       """Tool description for the model."""
       return my_new_tool(param)
   
   agent = Agent(
       model=model,
       tools=[inspect_project_tool, my_new_tool_wrapped],
       ...
   )
   ```

### Modifying the System Prompt

Edit `src/harness_agent/prompts/system.md` to change agent behavior, add guidelines, or update capabilities documentation.

### Running in Development Mode

The project is installed in editable mode (`pip install -e .`), so code changes take effect immediately without reinstallation.

## License

MIT License - see LICENSE file for details.

## Contributing

This is a personal learning and development project. Feel free to fork and adapt for your own use.

## Acknowledgments

- Built with [Strands Agents SDK](https://strandsagents.com/)
- Inspired by the Agent Harness pattern from Anthropic's research
- Model providers: OpenAI and compatible APIs

---

**Version**: 0.1.0  
**Status**: Active Development  
**Python**: 3.10+
