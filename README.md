# Harness Project Agent

**Version: 0.7.0** | **Status: User-Approved Git Stage & Commit**

A Single-Agent MVP built on the Strands Agents SDK, designed to analyze code repositories, explain local Git state (read-only), request whitelisted command execution under explicit human approval, propose precise user-approved single-file source edits, propose user-approved Git stage and commit operations, and track structured progress for multi-step tasks within one CLI process.

## Overview

This project implements a conversational AI agent that can safely browse, read, search, and understand a code repository. v0.3.0 added requests for controlled execution, v0.4.0 added local read-only Git awareness, v0.5.0 adds bounded, structured task progress in ephemeral memory, v0.6.0 adds User-Approved Source Editing, and v0.7.0 adds User-Approved Git Stage & Commit. The core security model remains:

> LLM proposes. Policy validates. User approves. Host executes.

Source Editing uses the same boundary:

> LLM proposes. Patch Policy validates. User approves. Host applies.

Git Mutation uses the same boundary:

> LLM proposes. Git Policy validates. User approves. Host mutates.

Git Awareness is NOT Git Authority:

> Repository read: YES. Git read: YES. Controlled test run: YES (with approval).
> Source write: YES (with approval). Git stage/commit: YES (with approval). Git push/tag: NO. Git network: NO.

Task State is NOT Permission:

> Plans organize work. They cannot approve or execute it, enable Git/source mutation, or bypass any tool policy.

Patch Broker is NOT Write Authority:

> `prepare_patch` only proposes immutable edits; only the host applies after user approval.

Git Mutation Broker is NOT Git Authority:

> `prepare_git_stage` and `prepare_git_commit` only propose immutable plans; only the host mutates Git after user approval.

## Architecture

```text
User
 ↓
Strands Agent
 ├── Repository Tools
 │    ├── inspect_project
 │    ├── list_directory
 │    ├── read_file
 │    ├── search_code
 │    └── analyze_dependencies
 ├── Git Awareness Tools (READ-ONLY)
 │    ├── git_status
 │    ├── git_diff
 │    ├── git_log
 │    └── git_branches
 │         ↓
 │    Git Read Service (hardened fixed argv)
 │         ↓
 │    Local Repository Metadata
 ├── Structured Task State (IN MEMORY ONLY)
 │    ├── create_task_plan
 │    ├── get_task_state
 │    ├── update_task_step
 │    └── add_task_steps
 ├── prepare_command
 │    ↓
 │ Execution Policy (strict allowlist)
 │    ↓
 │ Pending Execution Plan
 │    ↓
 │ User Approval (CLI prompt)          ← trust boundary
 │    ↓
 │ Host Execution Service (shell=False)
 │    ↓
 │ Execution Result
 ├── prepare_patch
 │    ↓
 │ Patch Policy (immutable single-file validation)
 │    ↓
 │ Pending PatchPlan (COMPLETE diff)
 │    ↓
 │ User Approval (CLI prompt)           ← trust boundary
 │    ↓
 │ Host Patch Service (atomic apply)
 │    ↓
 │ Patch Result
 └── prepare_git_stage / prepare_git_commit
      ↓
  Git Mutation Policy (path safety, text-only, no filters)
      ↓
  Pending GitStagePlan / GitCommitPlan (COMPLETE diff)
      ↓
  User Approval (CLI prompt)           ← trust boundary
      ↓
  Host Git Service (exact blob/commit, no hooks/signing/network)
      ↓
  Git Mutation Result
  PatchResult
```

The agent uses a simple but extensible architecture:
- **Agent Harness**: Strands Agents SDK manages the agent loop, tool calling, and conversation flow
- **Model**: OpenAI-compatible language model for understanding and generation
- **Repository Tools**: Read-only Python functions; every path is confined to the repository root via a shared safety module (`path_utils`)
- **Git Awareness Tools**: Read-only Git introspection with fixed hardened argv; the model provides semantic parameters (scope/path/limit), never a Git subcommand
- **Execution Layer**: The model can only *prepare* execution plans (`prepare_command`); validation, approval and subprocess execution live in the trusted host layer (`harness_agent.execution` + CLI). Execution is never automatically approved.
- **Patch Layer**: The model can only *propose* immutable single-file edits/creates (`prepare_patch`); validation, approval and file application live in the trusted host layer (`harness_agent.patch` + CLI). The Agent has no direct file-write tool.
- **Session State**: One explicit, thread-safe `SessionState` per CLI process stores immutable task/step snapshots and bounded metadata events. It is separate from Strands conversation history and never persists.
- **System Prompt**: Defines the agent's behavior, tool strategy, Git evidence rules, execution state semantics, source-edit semantics, and anti-hallucination rules

## Requirements

- **Python**: >= 3.10 (tested with Python 3.11)
- **Strands Agents SDK**: 1.53.0+
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
Harness Agent v0.5
Session state: ephemeral (cleared when this CLI exits).
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

Git examples:
```text
You > What files have changed?

Agent > [calls git_status and summarizes staged/unstaged/untracked]

You > Explain the current changes in agent.py.

Agent > [git_status → git_diff(path="src/harness_agent/agent.py") → answer]

You > What happened in the last five commits?

Agent > [git_log(limit=5) → answer]

You > Commit these changes.

Agent > Git mutation is not supported in v0.5.0.
        I can inspect the status and diff instead.
```

## Session State

Each CLI process owns an isolated in-memory `SessionState`. Strands still maintains conversation history as user/assistant/tool messages; task state is separate structured metadata describing the current goal and observable progress.

Session state stores:

- task goals and ordered task steps
- fixed progress statuses (`pending`, `in_progress`, `completed`, `blocked`, `skipped`)
- concise user-visible notes
- bounded task and host-verified execution lifecycle events
- host-verified source-edit lifecycle events (`patch_approved`, `patch_rejected`, `patch_applied`, `patch_conflict`, `patch_failed`) as metadata only

It intentionally does **not automatically ingest or retain** host credentials, environment variables, raw file contents, full diffs, raw execution stdout/stderr, complete chat transcripts, private chain-of-thought, user profiles, embeddings, or long-term memory. (Because goals, steps, notes and events are user/model-supplied text, a caller could *explicitly* put arbitrary text in them; the guarantee is that the framework never *automatically* captures host internals such as environment, command output, or credentials into session state.) Restarting `python scripts/run_agent.py` creates a new empty task session; state does not survive CLI restart.

Each CLI runtime owns **one explicit `SessionState`, one explicit `ExecutionBroker` and one explicit `PatchBroker`**; the agent factory binds its task tools to the state, its execution-request tools to the execution broker, and its patch-proposal tools to the patch broker. Two agents in the same process therefore share no task state, no execution plans/results, and no patch proposals - there is zero cross-session visibility, and each CLI only approves/processes its own pending plans and patches.

Limits are explicit: 20 tasks per session, 20 steps per task, 200 retained events, 500 characters per goal, 300 per step, 1000 per note, and 500 per event summary. `get_task_state` returns no more than 20 recent events. When the event deque evicts old entries, the snapshot exposes `events_truncated` and `dropped_event_count`; tasks are never silently deleted.

## Task Planning Strategy

Simple one-step questions should use the necessary tool directly. Multi-step work, work that can become blocked, or work interrupted by execution approval should use a concise task plan. Plan status is derived from step status; the Agent cannot directly declare an incomplete task completed.

Example:

```text
You > Inspect the current changes, verify the implementation,
      run focused tests if needed, and tell me what remains.

Agent creates a plan:
1. Inspect Git state
2. Review relevant diff
3. Inspect affected source
4. Run focused tests
5. Summarize remaining issues

git_status                 -> step 1 completed
git_diff / read_file       -> steps 2 and 3 completed
prepare_command(pytest ...) -> step 4 in_progress; awaiting approval
user approval              -> host executes
next Agent turn            -> get_execution_result, then update step 4
```

Planning remains descriptive. Only the host-side `[y/N]` flow approves a prepared command. A plan or note saying “approved” has no authority, and a plan step saying “commit changes” cannot enable Git mutation.

Legal step transitions are:

- `pending → in_progress | completed | blocked | skipped`
- `in_progress → pending | completed | blocked | skipped`
- `blocked → pending | in_progress | skipped`
- `completed` and `skipped` are terminal

`task_created`, `task_steps_added` and `task_step_updated` events describe Agent-managed task metadata. `execution_approved`, `execution_rejected` and `execution_completed` are recorded only by the CLI host. Execution events never change a task step automatically because the host does not guess which execution belongs to which step.

The **active task is by definition the most recently created/selected task**, not necessarily an unfinished one: `active_task_id` keeps pointing at the last selected task even after it becomes `terminal`. `get_task_state` reports `active_task.status == "completed"` plainly rather than implying it is still underway. There is no background scheduler that auto-switches to another task.

A **completed task is terminal for `add_task_steps`**: once its derived status is `completed` (every unfinished step is `completed` or `skipped`), appending new steps is refused so a "Task plan completed" claim stays stable - newly discovered work belongs in a *new* task plan. A task with `blocked` steps is never `completed`, so a `blocked` task may still accept added steps (e.g. to investigate the blocker).

Task-plan state is **in-memory and ephemeral** in the same way as Strands conversation messages: both exist only for the current Agent/CLI process and are gone when it exits. There is no persistent SessionManager or message store in this project.

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
- **Bounded output capture**: reader threads drain stdout/stderr while the child runs; only the first 64 KB per stream is retained and the excess is discarded, so execution output retained in memory is bounded regardless of how much the child prints. Invalid UTF-8 bytes are decoded with `errors="replace"` (U+FFFD) - a chatty or binary-emitting child can never crash a reader, deadlock a pipe, or lose the structured result.
- **Deterministic child encoding**: child Python processes run with `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`, pinning stdio to UTF-8 so captured output always matches the runner's decoding (a Windows Python child under a pipe would otherwise use the legacy locale codec).
- **Single-use plans**: each approved plan executes exactly once; prepared-but-rejected plans can never run. Plans only transition `pending → approved → executed` or `pending → rejected`.

Risk levels (`LOW`/`MEDIUM`/`HIGH`) are informational in v0.3.0. All execution still requires explicit approval.

### Note on Strands HITL

Strands Agents 1.53.0 provides native HITL/interruption facilities (`strands.interrupt`, `AgentResult.interrupts`, `HumanInTheLoop`). v0.3.0 deliberately uses host-side approval instead: the Agent-visible tool only prepares an inert execution plan, and the privileged subprocess step lives outside the Agent tool layer - so approving tool calls inside the SDK would not guard the operation that actually needs guarding.

## Git Awareness Safety Model (v0.4.0)

Git Awareness exposes read-only Git operations:

- **Fixed read-only Git operations**: only `rev-parse`, `status`, `diff`, `log` and `for-each-ref` are ever composed - no other subcommand exists in the code path
- **No arbitrary Git argv**: the model passes semantic parameters (`scope`, `path`, `limit`); revisions, ranges, refs and options are not part of the tool surface
- **shell=False**: Git is spawned as an argv list through the shared bounded process runner; `stdin=DEVNULL`
- **No pager**: `--no-pager` plus `GIT_PAGER=cat` / `PAGER=cat`
- **Optional locks disabled**: `--no-optional-locks` and `GIT_OPTIONAL_LOCKS=0`
- **fsmonitor disabled**: `core.fsmonitor=false`
- **External diff/textconv disabled**: `--no-ext-diff --no-textconv` (repo-configured diff helpers can never run)
- **No submodule recursion**: `--ignore-submodules=all`
- **Signature verification disabled**: `log.showSignature=false` (never `--show-signature`)
- **No network operations**: no fetch/pull/push/clone/ls-remote is ever composed; `include_remote` reads locally stored `refs/remotes` metadata only
- **Git environment scrub**: every inherited `GIT_*` variable (plus `SSH_ASKPASS`) is removed from the child environment; only host-set safe values are applied - `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=<devnull>`, `GIT_TERMINAL_PROMPT=0`, `GIT_ATTR_NOSYSTEM=1` (repository-local Git config remains readable so ordinary Git semantics work)
- **Repository-root confinement**: the real Git toplevel (`rev-parse --show-toplevel`) must equal the agent repository root; mismatches are refused instead of widening the boundary
- **Bounded output**: Git stdout retained is capped at 128 KB and stderr at 32 KB with truncation flags; status entries cap at 500, branches at 200, log at 50 commits, diff context at 10 lines. Output is decoded as UTF-8 with `errors="replace"`: filenames that are not valid UTF-8 display with replacement characters instead of crashing, deadlocking, or destroying the result.
- **Literal pathspecs**: `--literal-pathspecs` plus lexical validation; pathspec magic (`:(glob)**`) cannot be injected; paths stay repository-relative (deleted tracked paths remain diffable)
- **Untrusted-data-safe framing**: `git_log` uses NUL-terminated fields (`%H%x00%h%x00%an%x00%aI%x00%s%x00`) - NUL is the only byte the Git CLI can never place inside a commit subject or author name, so untrusted commit data cannot collide with the parser framing; `for-each-ref` uses `\x1f` fields, safe because `git check-ref-format` forbids control characters inside refnames. Malformed or truncated machine output degrades gracefully (records are skipped, never IndexError).

Verified empirically on Git 2.48.1 (and enforced by argv/env contract tests): repository-local config **cannot** re-enable the disabled surface - `core.pager`, `core.fsmonitor`, `diff.external`, `diff.<driver>.textconv` markers are never executed, same-name aliases (`alias.status` etc.) never override builtin subcommands, and none of the five read-only operations invoke Git hooks.

Residual Git config semantics (documented honestly): repository-local config - including `[include]` / `includeIf` expansion - is still read by Git for ordinary read-operation behavior. Because the hardened command-line `-c` values and flags take precedence over every config source and no mutation/network subcommand can ever be composed, includes cannot breach the mutation/network boundary; they remain a residual *semantic* influence on display formatting only.

**No Git mutation tools are available in v0.5.0.** Git data (commit messages, diffs, filenames, branch names) is treated as untrusted repository data - never as instructions or authorization.

## Source Editing Safety Model (v0.6.0)

Source editing is the only way to change a repository file, and it goes through the same host-approval trust boundary as execution:

```text
LLM proposes -> Patch Policy validates -> User approves -> Host applies
```

- **Only `prepare_patch` can propose changes**: the model supplies `path`, `operation` (`edit` or `create`), and exact `old_text`/`new_text` replacement blocks (or full `content` for creates). The Agent has no direct write tool.
- **Immutable `PatchPlan`**: a validated proposal is frozen at creation (path, operation, replacement blocks, summary, and the COMPLETE unified diff). A plan is never silently mutated after the model proposes it.
- **Exact-match policy**: each `old_text` must match a single unique occurrence in the file, byte-for-byte, in the order listed; overlapping replacements are refused. You must `read_file` the current bytes before proposing.
- **COMPLETE diff**: every proposal returns the full unified diff (never truncated) so the user sees exactly what will change before deciding.
- **Explicit newline semantics**: a source file must use a single newline convention. Mixed newlines (some `\r\n` and some `\n` in one file) are **REJECTED** - the policy never silently normalizes them, and the target file is left byte-identical. LF-only and CRLF-only files are preserved exactly, and a `\ No newline at end of file` marker is shown in the approval preview whenever a final newline is added or removed, so a newline-only change is never invisible.
- **Host-only apply**: `patch/service.py` is the ONLY module that writes files. It applies atomically (temp file + `os.replace`) for edits and `O_CREAT|O_EXCL` for creates, revalidating the path and SHA-256 of the current content to detect conflicts and reject symlinks/escapes introduced after preparation. A failed write leaves no partial file.
- **Explicit user approval**: only `y`/`yes` at the CLI prompt applies; Enter, `n`, other input, EOF and Ctrl+C all reject with ZERO filesystem writes. A `prepare_patch` proposal, or a `pending`/`approved` result, is never the same as the file having changed - only an `applied` PatchResult is.
- **No deletion/move/rename API**: the patch subsystem supports edit and create only; there is no `delete_file`, `remove_file`, `rename_file` or `move_file` anywhere.
- **Static write confinement**: an AST audit proves `tools/patch_tools.py` and every non-service module contain no actual file-write calls (`open("w")`, `write_text`, `write_bytes`, `os.replace`, `unlink`).
- **Isolation**: each agent factory call owns a private `PatchBroker`; two agents in one process share no patch plans, and `patch_broker` grants no execution, Git, network or approval authority.
- **Metadata-only session events**: approvals/apply results (`patch_approved`, `patch_rejected`, `patch_applied`, `patch_conflict`, `patch_failed`) are recorded as short metadata; the actual code content (old/new text, diff) never enters durable session state.
- **No same-turn dependent execution**: the model must never edit a file and then verify it in the same turn, and must never claim an edit succeeded until `get_patch_result` reports `status == 'applied'`.

**Atomic replacement vs. crash durability.** Applying makes the new file content
visible atomically: for edits the host writes a sibling temp file, `fsync`s it, and
`os.replace`s it into place (single directory entry swap), so any given moment
shows either the complete old file or the complete new file, never a torn mix; for
creates it uses `O_CREAT|O_EXCL`. This is an *atomic visibility* guarantee on
supported local filesystems - **not** a transaction-logging or crash-durability
commit. If the host (or the machine) crashes between the write and the directory
entry swap, or between apply and broker finalisation, the file may be left in the
old state and the broker in its claim state; the operating system cannot be
assumed to provide durable commit semantics, and no transaction journal is used.

### Windows Path Safety

Path validation depends on the host filesystem. On Windows the policy is stricter
and refuses the following before any file is touched (each non-target line keeps
its original bytes):

- **NTFS Alternate Data Streams (ADS)**: a path containing `:` (e.g. `file.py:secret`)
  is refused, so a patch can never write to a hidden data stream.
- **Reserved device names**: stems like `con`, `prn`, `aux`, `nul`, `clock$` and
  `com1..com9` / `lpt1..lpt9` (with or without an extension) are refused, because
  Windows would otherwise route the write to a device instead of a file.
- **Trailing dot / space**: components ending in `.` or ` ` are refused (Windows
  strips these and could alias to an unintended file).
- **Drive-relative paths**: a bare drive letter (`C:` or `C:foo`) and any
  leading-slash/drive-root traverse is refused; every path must stay inside the
  repository root.
- **Junction / reparse escape**: resolved paths are re-validated to be inside the
  repository root, refusing a junction or symlink whose target escapes the root,
  even where a real symlink cannot be created on Windows.

Source edits, like command execution, still run with the current operating-system
user's privileges once the host applies them - user approval is the trust boundary,
not an OS sandbox.

## Important Limitation

This is **not** an OS-level sandbox.

> Commands that execute repository code (e.g. pytest) still run with the
> current operating-system user's privileges. Repository code could
> theoretically access files available to the current user, the network,
> or spawn child processes unless restricted by an external
> OS/container sandbox. User confirmation is the trust boundary.

Timeout terminates the directly managed subprocess, but v0.3.0 does not
provide an OS-level guarantee that every descendant process is terminated.

Git read tools execute the local Git executable using fixed read-only argv
(no user approval gate like execution, because they are host-controlled
introspection APIs - this is NOT the same as allowing arbitrary Git).
Git read operations are in general read-only, but Git Awareness does not
claim that the Git process performs zero filesystem writes under every
Git implementation. The Agent itself has absolutely no Git mutation
commands.

`origin/main` and ahead/behind values are locally stored remote-tracking
information - not live GitHub state.

v0.6.0 state is not long-term memory. There is no cross-process persistence,
autonomous background execution, workflow engine, separate Planner Agent,
multi-agent system, Git mutation/network, package installation,
OS sandbox, guaranteed process-tree termination, persistent audit log, or web UI.

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

## Current Features (v0.6.0)

- ✅ **Single Strands Agent**: One conversational agent with clear responsibilities
- ✅ **OpenAI-Compatible Models**: Works with OpenAI, local vLLM, and other compatible endpoints
- ✅ **Project Inspection**: Top-level repository overview (`inspect_project`, root-confined)
- ✅ **Directory Browsing**: Recursive, depth-limited listing (`list_directory`)
- ✅ **Safe File Reading**: Line-numbered reads with line ranges (`read_file`)
- ✅ **Repository Code Search**: Pure-Python substring/regex search (`search_code`)
- ✅ **Dependency Analysis**: pyproject.toml / requirements*.txt / package.json (`analyze_dependencies`)
- ✅ **Read-Only Git Awareness**: status, diff, log, branches with hardened fixed argv (`git_status` / `git_diff` / `git_log` / `git_branches`)
- ✅ **User-Approved Execution**: `prepare_command` requests, policy validates, user approves, host executes
- ✅ **User-Approved Source Editing**: `prepare_patch` proposes immutable single-file edits/creates; the host applies only after explicit user approval
- ✅ **Structured Task Planning**: Four state-only tools track multi-step progress without adding authority
- ✅ **Ephemeral Session State**: Per-process isolation, immutable models, `RLock`, bounded tasks/steps/events
- ✅ **Host-Verified Session Events**: Approval/rejection/completion metadata without raw execution output or code content
- ✅ **Execution Policy**: Strict allowlist, per-flag validation, shell-syntax rejection
- ✅ **Patch Policy**: Exact-match edits, overlapping detection, path/secret/ignored/binary guards, size limits, COMPLETE diff
- ✅ **Bounded Streaming Capture**: stdout/stderr drained while the child runs; only the first 64 KB per stream is retained (memory-bounded)
- ✅ **Read-Only Repository Boundary**: Path confinement, sensitive file blocking, binary detection, output limits
- ✅ **CLI Interface**: Interactive terminal-based conversation with execution and source-edit approval prompts
- ✅ **Configuration Management**: Environment-based config with validation
- ✅ **Deterministic Tests**: 568 passing tests with mocked/fake dependencies

### Verification Status

- **Tests**: 568/568 deterministic/mock tests passing (v0.4.0 baseline: 344/344, v0.5.0: 454/454)
- **SDK Integration**: Verified locally with Strands Agents 1.53.0
- **Execution Smoke**: `scripts/smoke_execution.py` - 35/35 checks passing (host-level, no API key needed)
- **Repository Smoke**: `scripts/smoke_repo_tools.py` passing (v0.2 regression)
- **Git Awareness Smoke**: `scripts/smoke_git_awareness.py` - 21/21 checks passing on the real repository (read-only before/after proof)
- **Session Planning Smoke**: `scripts/smoke_session_planning.py` - 13/13 checks passing (no LLM or API key) including broker isolation and completed-task terminal checks
- **Source Editing Smoke**: `scripts/smoke_source_editing.py` - 24/24 checks passing (host-level, no API key) across propose/approve/reject/conflict/denials
- **Real LLM Execution**: Requires a valid OpenAI API key
- **Live API Smoke Test**: Has not yet been performed in this environment

This is a **stable development baseline**, not a production-ready system.  

### Safety Features

- **Read-only repository tools**: inspection tools never modify anything
- **Read-only Git awareness**: fixed introspection argv, no mutation surface, no network
- **Repository-root confinement**: All repository-facing tools (including `inspect_project`) refuse paths outside the project root; Git toplevel must match the agent root
- **Sensitive file filtering**: `.env`, keys, credentials are blocked; `.env.example` is allowed
- **Ignored directories**: `.git`, `.venv`, `__pycache__`, `node_modules`, `dist`, `build` are never touched
- **Binary detection**: By extension and content sniffing
- **Output limits**: Depth/entry/line/result caps with `truncated` flags; Git output bounded at 128 KB / 32 KB
- **Constrained execution**: allowlist + approval + argv-based subprocess + timeout + bounded capture + secret scrubbing
- **User-approved source editing**: no direct write tools; edits go through `prepare_patch`, host approval and atomic host apply only
- **No source mutation without approval**: no direct write/edit/delete tools, no `ruff --fix`, no autonomous patch apply
- **No Git mutation**: no add/commit/push/checkout/... tools exist in any layer

## Project Structure

```text
Harness Agent/
├── src/
│   └── harness_agent/
│       ├── __init__.py
│       ├── agent.py                    # Agent factory and initialization
│       ├── config.py                   # Configuration management
│       ├── process.py                  # Shared bounded process runner (Popen + threads)
│       ├── prompts/
│       │   └── system.md               # System prompt (understanding + git + execution semantics)
│       ├── execution/
│       │   ├── __init__.py             # Package exports + default broker
│       │   ├── models.py               # ExecutionPlan / ExecutionResult / PolicyDecision
│       │   ├── policy.py               # Strict allowlist validation
│       │   ├── broker.py               # Pending plans, single-use lifecycle
│       │   └── service.py              # Host subprocess service (shell=False)
│       ├── git_awareness/
│       │   ├── __init__.py             # Package exports
│       │   ├── models.py               # GitStatus / GitCommit / GitBranch
│       │   ├── parsers.py              # porcelain v2 / log / for-each-ref parsers
│       │   └── service.py              # Hardened read-only Git service
│       ├── session/
│       │   ├── __init__.py             # Session model/state exports
│       │   ├── models.py               # Frozen TaskPlan / TaskStep / SessionEvent
│       │   └── state.py                # Thread-safe bounded in-memory SessionState
│       ├── patch/
│       │   ├── __init__.py             # Patch exports
│       │   ├── models.py               # PatchPlan / PatchReplacement / PatchResult
│       │   ├── policy.py               # Immutable single-file validation + COMPLETE diff
│       │   ├── broker.py               # Pending/approved patch proposals, lifecycle
│       │   └── service.py              # Host atomic apply (the ONLY file-writing module)
│       └── tools/
│           ├── __init__.py
│           ├── path_utils.py           # Shared path-safety module
│           ├── project_tools.py        # inspect_project (root-confined)
│           ├── repository_tools.py     # list_directory / read_file / search_code / analyze_dependencies
│           ├── execution_tools.py      # prepare_command / get_execution_result
│           ├── git_tools.py            # git_status / git_diff / git_log / git_branches
│           ├── task_tools.py           # Four SessionState-bound planning tools
│           └── patch_tools.py          # prepare_patch / get_patch_result (never write)
├── scripts/
│   ├── run_agent.py                    # CLI entry point + excution & patch approval prompts
│   ├── smoke_repo_tools.py             # Manual tool/security smoke script (v0.2)
│   ├── smoke_execution.py              # Execution/security smoke script (v0.3)
│   ├── smoke_git_awareness.py          # Git awareness/read-only smoke script (v0.4)
│   ├── smoke_session_planning.py       # Deterministic session planning smoke (v0.5)
│   └── smoke_source_editing.py         # Deterministic source-editing smoke (v0.6)
├── tests/
│   ├── test_agent_factory.py           # Agent factory + tool registration tests
│   ├── test_cli.py                     # CLI tests
│   ├── test_cli_execution.py           # Approval UX + execution tool tests
│   ├── test_config.py                  # Configuration tests
│   ├── test_execution_broker.py        # Plan lifecycle tests
│   ├── test_execution_policy.py        # Allowlist policy tests
│   ├── test_execution_service.py       # Host service tests (fake subprocess)
│   ├── test_git_branches.py            # Branch listing tests
│   ├── test_git_diff.py                # Diff scope/path/limits tests
│   ├── test_git_log.py                 # History parsing tests
│   ├── test_git_service.py             # Git env/boundary/argv tests
│   ├── test_git_status.py              # Status parsing/lifecycle tests
│   ├── test_git_tools.py               # Git tool surface tests
│   ├── test_patch_broker.py            # Patch broker lifecycle tests
│   ├── test_patch_policy.py            # Patch policy validation tests
│   ├── test_patch_service.py           # Host apply/conflict/atomicity tests
│   ├── test_patch_tools.py             # Patch tool surface + no-write/authority audits
│   ├── test_patch_cli.py               # CLI approval UX (yes/no/EOF/Ctrl+C) tests
│   ├── test_patch_session_events.py    # Patch session metadata + secret-boundary tests
│   ├── test_patch_isolation.py         # Patch isolation + task/exec/git regressions
│   ├── patch_test_helpers.py           # Disposable repo-like root helpers for patch tests
│   ├── test_repository_tools.py        # Repository tool tests
│   ├── test_session_execution_events.py # Host execution-event integration
│   ├── test_session_state.py            # State model, limits and concurrency tests
│   ├── test_task_planning.py             # Planning/authority integration tests
│   ├── test_task_tools.py                # Closure binding and task tool tests
│   ├── test_tools.py                   # inspect_project tests
│   └── git_test_utils.py               # Disposable tmp git repo helpers
├── .env.example                        # Example environment configuration
├── .gitignore                          # Git ignore patterns
├── pyproject.toml                      # Project metadata and dependencies
├── README.md                           # This file
├── CHANGELOG.md                        # Version history
└── LICENSE                             # MIT License
```

## Roadmap

v0.2.0 delivered Repository Understanding; v0.3.0 delivered User-Approved Safe Execution; v0.4.0 delivered Read-Only Git Awareness; v0.5.0 delivered ephemeral Structured Task Planning; v0.6.0 delivers User-Approved Source Editing. Possible future work includes:

- opt-in persistence with an explicit privacy and retention design
- richer planning UX without autonomous execution
- optional coordination only after authority boundaries are designed
- **v1.0**: Full Agent Harness (orchestration, monitoring, evaluation)

## Development Status

**v0.6.0 is the current development baseline.**

### Implemented

- Single Strands Agent
- OpenAI-compatible model provider
- Project inspection tool (read-only)
- Directory browsing, safe file reading, code search, dependency analysis
- Shared repository path-safety module
- Read-only safety boundary
- Read-only Git awareness (status/diff/log/branches, hardened argv, no network)
- User-approved constrained execution (allowlist + policy + approval + host service)
- User-approved source editing (immutable patch plans + policy + approval + host apply)
- Ephemeral structured task planning with bounded progress events
- Per-agent ExecutionBroker and PatchBroker isolation with zero cross-session leakage
- CLI interface with execution and source-edit approval prompts
- Environment configuration
- 568 deterministic tests

### Not Implemented

- Autonomous source edits (no apply without explicit user approval)
- File deletion / move / rename
- Package installation
- Git mutation (add/commit/push/checkout/reset/merge/rebase/clean)
- Git network operations (fetch/pull/push/clone/ls-remote)
- OS-level sandboxing
- Persistent cross-session memory
- Autonomous/separate Planner Agent or workflow engine
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

5. **Read-only Git awareness**:
   ```
   You > What files have changed and what branch am I on?
   ```
   Expected: the agent uses `git_status` (and optionally `git_diff`),
    reports local Git state, and never mutates anything.

6. **Structured planning**:
   ```
   You > Inspect the latest repository changes, make a short plan,
         run a focused test if needed, and tell me what remains.
   ```
   Expected: the Agent creates a concise plan, updates observable inspection
   steps, prepares (but does not run) a test command, and waits for host-side
   approval. Rejection must never mark the test step completed.

7. **User-approved source editing**:
   ```
   You > Change the default port in src/app.py from 8000 to 9000.
   ```
   Expected: the agent `read_file`s the current content, calls `prepare_patch`
   to propose the exact edit, the CLI shows the complete diff with an approval
   prompt, and nothing changes until you type `y`. Declining must result in
   zero filesystem writes; the agent must not claim the file changed until
   `get_patch_result` reports `applied`.

8. **Task-plan-only authority check**:
   ```
   You > Add a "commit changes" step to the plan.
   ```
   Expected: the agent keeps the step `blocked` (Git mutation is unsupported)
   and does not treat the plan as authorization.

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

**Version**: 0.6.0 | **Status**: Active Development (User-Approved Source Editing) | **Python**: 3.10+
