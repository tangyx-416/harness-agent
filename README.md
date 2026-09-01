# Harness Project Agent

**Version: 0.3.0** | **Status: User-Approved Constrained Execution**

A Single-Agent MVP built on the Strands Agents SDK, designed to help analyze and understand code repositories through natural language interaction, and to request (never perform) whitelisted command execution under explicit human approval.

## Overview

This project implements a conversational AI agent that can safely browse, read, search, and understand a code repository. In v0.3.0 the agent gained the ability to *request* controlled execution of a tiny development-command allowlist. The core security model is:

> LLM proposes. Policy validates. User approves. Host executes.

## Architecture

```text
User
 ↓
Strands Agent
 ├── Repository Understanding Tools
 │    ├── inspect_project
 │    ├── list_directory
 │    ├── read_file
 │    ├── search_code
 │    └── analyze_dependencies
 └── prepare_command
      ↓
 Execution Policy (strict allowlist)
      ↓
 Pending Execution Plan
      ↓
 User Approval (CLI prompt)          ← trust boundary
      ↓
 Host Execution Service (shell=False)
      ↓
 Execution Result
```

The agent uses a simple but extensible architecture:
- **Agent Harness**: Strands Agents SDK manages the agent loop, tool calling, and conversation flow
- **Model**: OpenAI-compatible language model for understanding and generation
- **Repository Tools**: Read-only Python functions; every path is confined to the repository root via a shared safety module (`path_utils`)
- **Execution Layer**: The model can only *prepare* execution plans (`prepare_command`); validation, approval and subprocess execution live in the trusted host layer (`harness_agent.execution` + CLI). Execution is never automatically approved.
- **System Prompt**: Defines the agent's behavior, tool strategy, execution state semantics, and anti-hallucination rules

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
Harness Agent v0.3
Type 'exit' or 'quit' to stop, Ctrl+C to interrupt.
============================================================

You > Where is the Agent initialized?

Agent > [searches create_agent, reads agent.py, answers with file:line refs]

You > What dependencies does this repository use?

Agent > [calls analyze_dependencies and summarizes manifests]

You > Run the tests.

Agent > A test execution has been prepared and requires approval.

------------------------------------------------------------
Execution approval required

Command:
  python -m pytest -q

Working directory:
  E:\Harness Agent

Risk (HIGH):
  Executes repository Python code with the current operating-system
  user's privileges.

Timeout:
  30 seconds

Approve? [y/N]: y
------------------------------------------------------------
Execution finished: python -m pytest -q -> exit code 0 (4123 ms)
------------------------------------------------------------
```

Denied example:
```text
You > Run pip install requests.

Agent > Denied: package installation is not allowed in v0.3.0.
        The command allowlist only covers python --version,
        python -m pytest ... and python -m ruff check ...
```

## Execution Safety Model

Every execution request passes through these layers:

- **Strict allowlist**: only `python --version`, `python -m pytest [...]` and `python -m ruff check [...]` (bare `pytest`/`ruff` aliases are normalized). Everything else - pip, git, shell binaries, package managers - is denied.
- **No arbitrary Python**: `-c`, stdin scripts, script paths, `pip` and arbitrary modules are refused; the policy understands Python arguments.
- **shell=False, argv-based execution**: commands are never passed through a shell; `stdin` is connected to `DEVNULL`. What the user approves (the validated plan) is byte-for-byte what the host executes; `display_command` is a pure rendering and is never parsed.
- **Shell metacharacter rejection**: `; && || | > <` backticks, `$()` and newlines are refused in every token.
- **Repository cwd confinement**: the working directory and every path argument must resolve inside the repository root.
- **Explicit user approval**: only an explicit `y`/`yes` at the CLI prompt executes; Enter, `n`, random input and Ctrl+C all decline. There are no bypass flags. Every pending plan is approved independently - one `yes` never approves anything else.
- **Secret scrubbing**: API keys, tokens, passwords and credential variables (`OPENAI_API_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`, `*_API_KEY`, `*_CREDENTIAL*` ...) never reach the child environment.
- **Behavior-injection scrubbing**: `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, `PYTHONINSPECT`, `PYTHONUSERBASE`, `PYTEST_ADDOPTS`, `PYTEST_PLUGINS` and `PYTEST_DEBUG` are removed so the environment cannot silently reshape the approved command. Child processes also get `PYTHONDONTWRITEBYTECODE=1`, `PYTHONNOUSERSITE=1` and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` (deterministic runs; this repository's own test suite passes with plugin autoload disabled).
- **Timeout**: 30 seconds by default, clamped to a 60-second maximum. On timeout the directly managed subprocess is killed and any bounded partial output is returned.
- **Bounded output capture**: reader threads drain stdout/stderr while the child runs; only the first 64 KB per stream is retained and the excess is discarded, so execution output retained in memory is bounded regardless of how much the child prints.
- **Single-use plans**: each approved plan executes exactly once; prepared-but-rejected plans can never run. Plans only transition `pending → approved → executed` or `pending → rejected`.

Risk levels (`LOW`/`MEDIUM`/`HIGH`) are informational in v0.3.0. All execution still requires explicit approval.

### Note on Strands HITL

Strands Agents 1.53.0 provides native HITL/interruption facilities (`strands.interrupt`, `AgentResult.interrupts`, `HumanInTheLoop`). v0.3.0 deliberately uses host-side approval instead: the Agent-visible tool only prepares an inert execution plan, and the privileged subprocess step lives outside the Agent tool layer - so approving tool calls inside the SDK would not guard the operation that actually needs guarding.

## Important Limitation

This is **not** an OS-level sandbox.

> Commands that execute repository code (e.g. pytest) still run with the
> current operating-system user's privileges. Repository code could
> theoretically access files available to the current user, the network,
> or spawn child processes unless restricted by an external
> OS/container sandbox. User confirmation is the trust boundary.

Timeout terminates the directly managed subprocess, but v0.3.0 does not
provide an OS-level guarantee that every descendant process is terminated.

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

## Current Features (v0.3.0)

- ✅ **Single Strands Agent**: One conversational agent with clear responsibilities
- ✅ **OpenAI-Compatible Models**: Works with OpenAI, local vLLM, and other compatible endpoints
- ✅ **Project Inspection**: Top-level repository overview (`inspect_project`, root-confined)
- ✅ **Directory Browsing**: Recursive, depth-limited listing (`list_directory`)
- ✅ **Safe File Reading**: Line-numbered reads with line ranges (`read_file`)
- ✅ **Repository Code Search**: Pure-Python substring/regex search (`search_code`)
- ✅ **Dependency Analysis**: pyproject.toml / requirements*.txt / package.json (`analyze_dependencies`)
- ✅ **User-Approved Execution**: `prepare_command` requests, policy validates, user approves, host executes
- ✅ **Execution Policy**: Strict allowlist, per-flag validation, shell-syntax rejection
- ✅ **Bounded Streaming Capture**: stdout/stderr drained while the child runs; only the first 64 KB per stream is retained (memory-bounded)
- ✅ **Read-Only Repository Boundary**: Path confinement, sensitive file blocking, binary detection, output limits
- ✅ **CLI Interface**: Interactive terminal-based conversation with an execution approval prompt
- ✅ **Configuration Management**: Environment-based config with validation
- ✅ **Deterministic Tests**: 222 passing tests with mocked/fake dependencies

### Verification Status

- **Tests**: 222/222 deterministic/mock tests passing
- **SDK Integration**: Verified locally with Strands Agents 1.53.0
- **Execution Smoke**: `scripts/smoke_execution.py` - 33/33 checks passing (host-level, no API key needed)
- **Repository Smoke**: `scripts/smoke_repo_tools.py` passing (v0.2 regression)
- **Real LLM Execution**: Requires a valid OpenAI API key
- **Live API Smoke Test**: Has not yet been performed in this environment

This is a **stable development baseline**, not a production-ready system.  

### Safety Features

- **Read-only repository tools**: inspection tools never modify anything
- **Repository-root confinement**: All repository-facing tools (including `inspect_project`) refuse paths outside the project root
- **Sensitive file filtering**: `.env`, keys, credentials are blocked; `.env.example` is allowed
- **Ignored directories**: `.git`, `.venv`, `__pycache__`, `node_modules`, `dist`, `build` are never touched
- **Binary detection**: By extension and content sniffing
- **Output limits**: Depth/entry/line/result caps with `truncated` flags
- **Constrained execution**: allowlist + approval + argv-based subprocess + timeout + output caps + secret scrubbing
- **No source mutation**: no write/edit/delete tools, no `ruff --fix`, no patch/apply tools

## Project Structure

```text
Harness Agent/
├── src/
│   └── harness_agent/
│       ├── __init__.py
│       ├── agent.py                    # Agent factory and initialization
│       ├── config.py                   # Configuration management
│       ├── prompts/
│       │   └── system.md               # System prompt (understanding + execution semantics)
│       ├── execution/
│       │   ├── __init__.py             # Package exports + default broker
│       │   ├── models.py               # ExecutionPlan / ExecutionResult / PolicyDecision
│       │   ├── policy.py               # Strict allowlist validation
│       │   ├── broker.py               # Pending plans, single-use lifecycle
│       │   └── service.py              # Host subprocess service (shell=False)
│       └── tools/
│           ├── __init__.py
│           ├── path_utils.py           # Shared path-safety module
│           ├── project_tools.py        # inspect_project (root-confined)
│           ├── repository_tools.py     # list_directory / read_file / search_code / analyze_dependencies
│           └── execution_tools.py      # prepare_command / get_execution_result
├── scripts/
│   ├── run_agent.py                    # CLI entry point + approval prompt
│   ├── smoke_repo_tools.py             # Manual tool/security smoke script (v0.2)
│   └── smoke_execution.py              # Execution/security smoke script (v0.3)
├── tests/
│   ├── test_agent_factory.py           # Agent factory + tool registration tests
│   ├── test_cli.py                     # CLI tests
│   ├── test_cli_execution.py           # Approval UX + execution tool tests
│   ├── test_config.py                  # Configuration tests
│   ├── test_execution_broker.py        # Plan lifecycle tests
│   ├── test_execution_policy.py        # Allowlist policy tests
│   ├── test_execution_service.py       # Host service tests (fake subprocess)
│   ├── test_repository_tools.py        # Repository tool tests
│   └── test_tools.py                   # inspect_project tests
├── .env.example                        # Example environment configuration
├── .gitignore                          # Git ignore patterns
├── pyproject.toml                      # Project metadata and dependencies
├── README.md                           # This file
├── CHANGELOG.md                        # Version history
└── LICENSE                             # MIT License
```

## Roadmap

v0.2.0 delivered the Repository Understanding layer; v0.3.0 delivered User-Approved Safe Execution. Future versions will add:

- **v0.4**: Git awareness (status, diff, log - read-only)
- **v0.5**: Memory system (conversation history, learned facts)
- **v0.6**: Planning capabilities (multi-step task decomposition)
- **v0.7**: Multi-agent coordination (specialist agents for different tasks)
- **v1.0**: Full Agent Harness (orchestration, monitoring, evaluation)

## Development Status

**v0.3.0 is the current development baseline.**

### Implemented

- Single Strands Agent
- OpenAI-compatible model provider
- Project inspection tool (read-only)
- Directory browsing, safe file reading, code search, dependency analysis
- Shared repository path-safety module
- Read-only safety boundary
- User-approved constrained execution (allowlist + policy + approval + host service)
- CLI interface with execution approval prompt
- Environment configuration
- 222 deterministic tests

### Not Implemented

- File writing/modification
- Source editing / patch tools
- Package installation
- Git operations
- OS-level sandboxing
- Memory system
- Planning capabilities
- Multi-Agent orchestration
- Web UI
- Persistent audit log

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

2. **Repository understanding**:
   ```
   You > Find where create_agent is implemented and explain how the Agent is configured.
   ```

3. **Dependency analysis**:
   ```
   You > What dependencies does this project use?
   ```

4. **User-approved execution**:
   ```
   You > Run the test suite and tell me whether it passes.
   ```
   Expected: the agent prepares `python -m pytest` via `prepare_command`,
   the CLI shows the approval prompt, and nothing runs until you type
   `y`. Declining must result in zero subprocess execution.

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

**Version**: 0.3.0 | **Status**: Active Development (User-Approved Constrained Execution) | **Python**: 3.10+
