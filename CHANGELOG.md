# Changelog

All notable changes to the Harness Project Agent will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-09-01

### Added

- Ephemeral, per-process `SessionState` with an isolated session id and UTC timestamps
- Frozen `TaskPlan`, `TaskStep` and `SessionEvent` models
- Structured multi-step task planning and progress tracking through `create_task_plan`, `get_task_state`, `update_task_step` and `add_task_steps`
- Fixed task-step status machine, automatically derived task status and atomic plan revisions
- Dynamic append-only plan refinement with stable step ids and no history deletion/reordering
- Bounded state: 20 tasks, 20 steps per task, 200 retained events, bounded text fields and at most 20 recent events per default snapshot
- Thread-safe state mutation with `RLock`, immutable replacement updates and snapshot isolation
- Host-verified `execution_approved`, `execution_rejected` and `execution_completed` session events
- Deterministic session-planning smoke script and state/tool/concurrency/authority integration tests
- Agent factory support for an explicit `session_state`, with a fresh private state by default
- Test coverage increased from the 344-test v0.4.0 baseline to 423 deterministic tests

### Safety

- Task state is descriptive metadata and grants no execution, approval, Git, filesystem or network authority
- The v0.3.0 host-side approval boundary remains unchanged: model proposes, policy validates, user approves, host executes
- Git remains read-only; task text cannot enable Git mutation or bypass the execution policy
- Session state is in memory only: no files, database, cross-process persistence, background execution or separate Planner Agent
- Task notes are concise user-visible records, never private chain-of-thought or hidden reasoning
- Session events never retain raw stdout/stderr, environment variables, credentials, raw source, or full diffs
- Goals, steps, notes and events are treated as untrusted session data, never instructions or authorization

### Fixed (Release Audit 指令7)

- **ExecutionBroker isolation**: the agent factory's `prepare_command` / `get_execution_result` tools are now closure-bound to a single per-agent `ExecutionBroker` (`make_execution_tools`) instead of the process-global `get_default_broker()`. Two Agent instances in one process now share no execution plans, results, or pending queues - zero cross-session visibility, and each CLI runtime only approves/processes its own plans. `create_agent` accepts an optional `execution_broker`; the CLI threads one explicit broker/state pair end to end. The `get_default_broker()` compatibility helper is retained for existing v0.3/v0.4 callers, but `create_agent` no longer depends on it.
- **Completed task is terminal for `add_task_steps`**: a plan whose derived status is `completed` cannot be silently reopened by appending steps; new work must be a new task plan. `blocked` tasks may still accept added steps.
- **Active-task semantics documented**: `active_task_id` is the most recently created/selected task and may still point at a terminal task; `get_task_state` reports `active_task.status == "completed"` explicitly rather than implying it is unfinished. No auto-scheduling was added.
- **Step-transition atomicity regression test**: two competing same-step transitions (e.g. `pending→completed` vs `pending→blocked`) are serialized under the lock - only one legal transition based on the true current state succeeds.
- **Revision atomicity regression tests**: a successful mutation bumps the revision by exactly +1; a failed mutation (invalid transition, unknown step, too many steps, blank note) leaves the revision unchanged - revision is never incremented before validation.
- **Event sequence monotonicity regression tests**: >200 events still yield strictly-increasing, unique `seq` values (an independent `_next_event_seq` counter, not `len(events)+1`), with correct retained/dropped counts.
- **Context-bound `get_task_state`**: the default view returns the active task, task *summaries* (no full 20-step bodies for every task) and at most 20 recent events; a full step list is returned only for an explicit `task_id`. Stress test (20 tasks x 20 steps x 200 events) stays well under a bound on the default serialized view. A `RuntimeContext` abstraction was deliberately **not** added.
- **Secret semantics made precise**: README now states the framework "does not automatically ingest or retain host credentials" (a caller may still *explicitly* place arbitrary text into a goal/step/note/event) instead of the over-broad "never stores API keys".
- Test coverage increased from 423 to 454 with session-isolation, bounds, and audit regression tests.

## [0.4.0] - 2026-09-01

### Added

- Read-only Git status awareness (`git_status`) via machine-readable `--porcelain=v2 --branch -z` (branch, HEAD, upstream, ahead/behind, staged/unstaged/untracked/conflicts; Unicode, spaces, renames with old_path, deleted files, unborn repositories, detached HEAD)
- Safe Git diff inspection (`git_diff`) with fixed scope enum (`working` / `staged` / `head`), literal repository-relative path filter, clamped context lines and bounded output
- Commit history inspection (`git_log`) - HEAD-only, structured commits (hash, author, ISO date, subject), 1-50 limit, robust delimiter parsing
- Local branch awareness (`git_branches`) via `for-each-ref` (locally stored remote-tracking refs only, never network)
- Git repository boundary validation: real `rev-parse --show-toplevel` must equal the agent repository root (supports `.git` dir and `.git` file / worktrees)
- Bounded Git subprocess output (128 KB stdout / 32 KB stderr) through the shared bounded process runner
- Git Awareness smoke script (`scripts/smoke_git_awareness.py`) with a before/after read-only proof
- Test coverage increased from 240 to 320 deterministic tests

### Security

- No arbitrary Git command tool: the model supplies semantic parameters only (scope/path/limit), never a Git subcommand or raw option
- No Git mutation operations: only rev-parse / status / diff / log / for-each-ref are ever composed
- No Git network operations: no fetch/push/pull/clone/ls-remote; ahead/behind are local tracking-ref metadata
- Git environment sanitization: all inherited `GIT_*` variables removed, then only host-set safe values applied (`GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL=devnull`, `GIT_TERMINAL_PROMPT=0`, `GIT_PAGER=cat`, `GIT_OPTIONAL_LOCKS=0`, `GIT_ATTR_NOSYSTEM=1`, `PAGER=cat`)
- Pager disabled (`--no-pager`), optional locks disabled (`--no-optional-locks`), fsmonitor disabled (`core.fsmonitor=false`), external diff/textconv disabled (`--no-ext-diff --no-textconv`), signature verification disabled (`log.showSignature=false`), pathspec magic disabled (`--literal-pathspecs`), Unicode paths unquoted (`core.quotepath=false`)
- No submodule recursion (`--ignore-submodules=all` on status/diff scope)
- prepare_command continues to deny git - Git tools and the execution allowlist are separate authority layers

### Fixed

- `git_log` framing hardened: NUL-terminated fields replace `\x1f`/`\x1e` delimiters that untrusted commit subjects/authors could collide with (verified creatable via the Git CLI); malformed/truncated machine output is skipped safely instead of misparsing
- Shared bounded runner decoding: invalid UTF-8 child output (stdout, stderr, or both) is decoded with `errors="replace"` - proven by real-process tests that it cannot crash a reader thread, deadlock a pipe, or lose the structured result (including under simultaneous truncation and timeout)
- Child Python stdio pinned to UTF-8 (`PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`) so captured output always matches the runner's decoding (a Windows Python child under a pipe otherwise used the legacy locale codec)
- `git status`/`for-each-ref` parsers hardened against malformed, truncated and garbage machine streams (no IndexError; structured results always returned)

## [0.3.0] - 2026-09-01

### Added

- User-approved command execution (`prepare_command`) - the agent can request, but never run, whitelisted commands
- Execution policy with a strict allowlist (`python --version`, `python -m pytest ...`, `python -m ruff check ...`) and per-flag argument validation
- Pending execution plans with immutable `ExecutionPlan` records and single-use lifecycle (`ExecutionBroker`)
- Explicit CLI approval prompt (only `y`/`yes` approves; Enter, other input and Ctrl+C all decline)
- Host-only execution service with timeout (30 s default, 60 s max), bounded streaming output capture (64 KB retained per stream, excess drained and discarded - child output can never grow parent memory without bound) and structured `ExecutionResult`
- Environment policy hardening for child processes: secrets scrubbed plus `PYTHONPATH`/`PYTHONHOME`/`PYTHONSTARTUP`/`PYTHONINSPECT`/`PYTHONUSERBASE`/`PYTEST_ADDOPTS`/`PYTEST_PLUGINS`/`PYTEST_DEBUG` removal and `PYTHONDONTWRITEBYTECODE=1` / `PYTHONNOUSERSITE=1` / `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` enforcement
- Environment secret scrubbing for every child process (`OPENAI_API_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`, `*_API_KEY`, `*_CREDENTIAL*` ...)
- Read-only result lookup tool (`get_execution_result`)
- Execution security/behavior smoke script (`scripts/smoke_execution.py`)
- Test coverage increased from 94 to 222 deterministic tests

### Security

- No `shell=True`: execution is argv-list based with `shell=False`, `stdin=DEVNULL`
- Strict command allowlist: pip, git, shell binaries, package managers and arbitrary programs are denied
- No arbitrary Python execution: `-c`, stdin scripts, script paths, `pip` and arbitrary modules are refused
- Repository working-directory confinement and path-argument validation (reuses `path_utils`)
- Shell metacharacters (`; && || | > <` backticks, `$()`, newlines) rejected in every token
- No automatic approval, no bypass flags (`AUTO_APPROVE`, `--yes`, `--force` do not exist)
- Execution is never automatically approved; risk levels are informational only
- Independent per-plan approval: one `yes` never approves other pending plans
- Explicit broker state machine: only `pending → approved → executed` or `pending → rejected`; rejected/executed plans can never be approved or re-executed

### Fixed

- `inspect_project` now uses the same repository-root confinement policy as the other repository tools

## [0.2.0] - 2026-08-31

### Added

- Safe directory browsing (`list_directory`) with depth clamping (1-3) and entry caps
- Safe file reading (`read_file`) with line ranges, numbered output, and 400-line call caps
- Repository code search (`search_code`) - pure Python, case-insensitive substring plus optional regex, no shell
- Dependency manifest analysis (`analyze_dependencies`) for pyproject.toml / requirements*.txt / package.json via stdlib tomllib
- Shared repository path-safety module (`path_utils.py`)
- Repository understanding workflow in the system prompt (locate → search → read → answer)
- Manual tool/security smoke script (`scripts/smoke_repo_tools.py`)
- Test coverage increased from 27 to 92 deterministic tests

### Safety

- Repository-root confinement: all resolved paths must stay inside the project root
- Path traversal protection (`../`, absolute paths, drive/UNC paths)
- Sensitive file blocking (.env, .env.*, keys, certificates, credentials) with `.env.example` explicitly allowed
- Ignored directories (.git, .venv, __pycache__, node_modules, dist, build, *.egg-info) excluded from browsing/search/reading
- Binary file blocking by extension and content sniffing (NUL-byte detection)
- Output size limits everywhere with `truncated` flags
- Read-only operations only: no writes, no shell, no network

### Verification

- 92/92 deterministic tests passing
- `python -m compileall src scripts` clean
- Manual tool smoke tests executed (list_directory / read_file / search_code / analyze_dependencies)
- Security smoke tests executed (traversal blocked, .env blocked, .env.example allowed, ignored dirs excluded)
- Live LLM smoke test not executed (no API key configured in this environment)

## [0.1.1] - 2026-08-21

### Fixed

- Correct Strands Agent invocation using `agent(prompt)` instead of non-existent `agent.chat()`
- Correct OpenAIModel initialization using `client_args` dict and `model_id` parameter
- Correct AgentResult handling using `str(result)` for text extraction
- Added explicit `openai>=3.0.0` dependency to pyproject.toml

### Added

- Agent factory tests (7 tests) verifying correct SDK API usage
- CLI tests (8 tests) verifying correct agent invocation
- Total test coverage increased from 12 to 27 tests

### Verification

- 27/27 deterministic tests passing
- SDK integration verified with Strands Agents 1.52.0
- Import verification successful
- CLI smoke test verified (without live API)

## [0.1.0] - 2026-08-21

### Added

- Single Agent architecture based on Strands Agents SDK
- Project inspection tool (`inspect_project`) for read-only repository analysis
- Configuration system with environment variable validation
- System prompt defining agent behavior
- Interactive CLI interface
- Deterministic unit tests (12 tests for config and tools)
- MIT License
- Comprehensive README documentation

### Security

- Read-only safety boundary (no file modification or shell execution)
- Sensitive file filtering (excludes .env, credentials, keys, etc.)
- API key validation and secure configuration management
