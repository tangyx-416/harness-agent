# Changelog

All notable changes to the Harness Project Agent will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.0] - 2026-09-23

### Added

- **User-Approved Remote Git Fetch**: the agent can now propose fetching remote commits to refresh local tracking refs, following the same security model: `LLM proposes -> Git Fetch Policy validates -> User approves -> Host fetches`
- Two new agent-visible tools: `prepare_git_fetch` (propose a fetch with zero network operations) and `get_git_fetch_result` (check plan status)
- Immutable `GitFetchPlan` model with remote URL, remote branch, tracking ref, and current tracking OID
- Git fetch policy layer enforcing: HTTPS-only remotes, existing configured remote only, existing remote branch only, dangerous config rejection (url.*.insteadOf, repository-local credential.helper, http.extraHeader, http.proxy)
- Git fetch service layer with transport isolation boundary: fetch to temporary Harness ref (`refs/harness-agent/fetch/<plan-id>`), validate fetched commit, fast-forward tracking ref via CAS update-ref, cleanup temp ref
- Workspace preservation invariants: HEAD, local branches, index, working tree, FETCH_HEAD, tags, and other tracking refs remain unchanged
- History rewrite detection: if fetched commit is not a descendant of current tracking ref → CONFLICT state, no tracking ref update
- Tracking ref CAS semantics: concurrent tracking ref changes detected via compare-and-swap update-ref, prevents lost updates
- CLI approval UX showing remote URL, remote branch, tracking ref, and current/observed OIDs
- Session state now records 4 Git fetch events (fetch_prepare_proposed, fetch_prepare_approved, fetch_applied, fetch_conflict/failed)
- `GitFetchBroker` isolated per agent (closure-bound tools, no cross-session visibility), bounded at 200 plans per broker
- Comprehensive test coverage: 45 Git fetch tests added (models, broker, policy, service, conflict scenarios, workspace preservation), bringing total to 960 tests
- Smoke test script (`scripts/smoke_git_fetch.py`) validating broker isolation, exactly-once semantics, policy enforcement, and tool surface

### Security

- **URL redirection protection**: `url.*.insteadOf` config can silently redirect an approved fetch to an attacker-controlled host. v0.9.0 compares raw configured URL against effective fetch URL and rejects any difference at prepare time
- **Repository-local credential.helper rejection**: repository-controlled shell credential helpers could gain arbitrary execution authority. v0.9.0 checks credential.helper scope and rejects repository-local shell helpers
- **Repository-local http.extraHeader rejection**: repository-controlled headers could inject Authorization or other sensitive headers. v0.9.0 rejects repository-local http.extraHeader configuration
- **Repository-local proxy rejection**: repository-controlled http.proxy and remote.*.proxy can alter network routing. v0.9.0 rejects repository-local proxy settings
- **Apply-time revalidation**: all config security checks run both at prepare time and immediately before network fetch, preventing injection attacks between approval and apply
- Zero-network prepare: `validate_and_prepare_fetch()` performs no DNS/TCP/HTTP operations, no credential helper execution
- Credential boundary: No credentials in GitFetchPlan, GitFetchResult, SessionState, CLI preview, or logs
- Tag suppression: `--no-tags` prevents tag fetching even when remote advertises tags
- Submodule suppression: `--no-recurse-submodules`, `-c fetch.recurseSubmodules=false`, `-c submodule.recurse=false` prevent submodule updates
- FETCH_HEAD preservation: `--no-write-fetch-head` prevents modifying FETCH_HEAD
- Exact ref isolation: Only the approved tracking ref may be updated, all other refs preserved
- Exactly-once network read: Single fetch attempt, no automatic retry on any failure class

### Limitations (By Design)

- HTTPS only - No SSH, file://, git@, or other schemes
- Existing configured remote only - No arbitrary remote URL
- Existing remote branch only - No arbitrary refspec
- Fast-forward only - No force-fetch, history rewrites rejected
- No tags - Tracking ref only
- No submodules - Submodule recursion suppressed
- No pull/merge/rebase - Fetch updates tracking refs only, not local branches
- No automatic retry - Single network read attempt
- Trusted user/global config only - Repository-local dangerous config rejected
- Does not control: OS-level proxy/DNS, trusted user credential helpers, server-side behavior

### Changed

## [0.8.0] - 2026-09-04

### Added

- **User-Approved Remote Git Push**: the agent can now propose pushing local commits to the configured remote repository, following the same security model: `LLM proposes -> Git Remote Policy validates -> User approves -> Host mutates`
- Two new agent-visible tools: `prepare_git_push` (propose a push with zero network operations) and `get_git_push_result` (check plan status)
- Immutable `GitPushPlan` model with complete diff preview, commit list, and destination URL
- Git remote policy layer enforcing: HTTPS-only, current configured upstream only, existing remote branch only, fast-forward-only (no force-push authority), clean working tree, no merge commits in outgoing range, bounded commit count (max 20), bounded diff size (max 100KB)
- Git remote service layer with three-phase protocol: preflight read (ls-remote), lease-bound push (--force-with-lease=<ref>:<oid>), post-push verification (ls-remote again)
- Exact lease/CAS semantics preventing preflight-to-push race conditions
- Post-push verification: remote OID must match approved local HEAD, mismatch → FAILED state
- Destination mutation detection: URL/branch changes between approval and apply → CONFLICT state with zero network attempts
- CLI approval UX showing destination URL, outgoing commits, and complete diff
- Session state now records 4 Git remote events (prepare_proposed, prepare_approved, push_applied, push_conflict/failed)
- `GitRemoteBroker` isolated per agent (closure-bound tools, no cross-session visibility), bounded at 200 plans per broker
- Comprehensive test coverage: 101 Git remote tests added (broker, policy, service, tools, integration, isolation, race conditions), bringing total to 915 tests (906 passing)
- Smoke test script (`scripts/smoke_git_remote.py`) with 6 end-to-end checks validating broker isolation, exactly-once semantics, policy enforcement, and tool surface

### Security

- **URL redirection protection**: `url.*.insteadOf`, `url.*.pushInsteadOf`, and `remote.*.pushurl` config rules can silently redirect an approved push to an attacker-controlled host. v0.8.0 compares the raw configured URL against the effective push URL and rejects any difference at prepare time, ensuring APPROVED DESTINATION == ACTUAL PUSH DESTINATION
- Zero-network prepare: `validate_and_prepare_push()` performs no DNS/TCP/HTTP operations, no credential helper execution
- Credential boundary: No credentials in GitPushPlan, GitPushResult, SessionState, CLI preview, or logs
- Hook/signer suppression: pre-push hooks and external signers not executed via `-c core.hooksPath=/dev/null -c gpg.program=false`
- Tag suppression: `--tags` and `--follow-tags` never used, `-c push.followTags=false` override present
- Submodule recursion suppression: `-c push.recurseSubmodules=no` override present even when config enables it
- Exactly-once network write: Single push attempt, no automatic retry on any failure class
- No force-push authority: `--force-with-lease=<ref>:<oid>` is a CAS mechanism, not force-push authority; behind/diverged history denied at prepare time

### Limitations (By Design)

- HTTPS only - No SSH, file://, git@, or other schemes
- Current configured upstream only - No arbitrary remote/branch selection
- Existing remote branch only - No branch creation
- Fast-forward only - No force-push authority
- No tags - Branch-only push
- No remote deletion - No ref deletion
- No fetch/pull - Push-only capability
- No automatic retry - Single network write attempt
- Local commit ≠ remote success - Must verify with get_git_push_result

### Changed

- Agent tool count increased from 20 to 22 (added 2 Git remote tools)
- System prompt updated to v0.8.0, role now "User-Approved Remote Git Push"
- CLI banner updated to v0.8.0 with Git remote push disclosure
- Version bumped to 0.8.0 in `pyproject.toml` and `__init__.py`

## [0.7.0] - 2026-09-04

### Added

- **User-Approved Git Stage & Commit**: the agent can now propose Git index mutations (staging files) and local commit creation, following the same security model as source editing: `LLM proposes -> Git Policy validates -> User approves -> Host mutates`
- Three new agent-visible tools: `prepare_git_stage` (propose single-file staging), `prepare_git_commit` (propose commit from entire staged snapshot), and `get_git_mutation_result` (check plan status)
- Immutable `GitStagePlan` and `GitCommitPlan` models with complete diff preview (never truncated) and exactly-once apply semantics via `GitMutationBroker`
- Git mutation policy layer enforcing: safe path validation (reuses patch policy hardening), text-only changes (no binary/delete/rename), no external filters (clean filters DENIED), one file per stage plan, entire staged snapshot per commit, no initial/amend/merge commits, message validation (UTF-8, no control/bidi spoofing)
- Git mutation service layer with revalidation before mutation, exact approved blob writing, index-only updates (no hooks/signing/editor/pager/maintenance), post-verification of index entries and diffs, and compensation on failure
- CLI approval UX for stage and commit with complete diff preview and safe terminal rendering
- Session state now records 10 Git mutation events (proposal/approval/rejection/applied/conflict for both stage and commit) as metadata-only (no diffs stored)
- `GitMutationBroker` isolated per agent (closure-bound tools, no cross-session visibility), bounded at 200 plans per broker
- Comprehensive test coverage: 107 new tests added (31 for models/broker, plus policy/service/tools/integration tests), bringing total to 771 tests (658 passed baseline + 107 new + 6 updated)
- Smoke test script (`scripts/smoke_git_mutation.py`) with 6 end-to-end checks validating broker isolation, exactly-once semantics, policy validation, and agent integration

### Safety

- No arbitrary Git command interface exposed to the agent
- No `git add .` (one file per stage plan only)
- No file deletion, rename, or binary staging
- No amend, tag, or push operations
- No network operations (local commits only)
- All Git mutations require explicit user approval with complete diff preview
- External filters (clean/smudge) are detected and DENIED
- Hooks, signing, editor, pager, and maintenance are disabled during commit
- Stage and commit approvals are separate (staging ≠ auto-commit)
- Exactly-once apply semantics prevent double-mutation
- HEAD/branch/index state revalidated before every mutation (conflict detection)
- Post-verification ensures exact blob OID and diff match approval

### Changed

- Agent tool count increased from 17 to 20 (added 3 Git mutation tools)
- System prompt updated to v0.7.0, role now "User-Approved Git Stage & Commit"
- CLI banner updated to v0.7.0 with Git mutation disclosure
- Version bumped to 0.7.0 in `pyproject.toml` and `__init__.py`

## [0.6.0] - 2026-09-03

### Added

- User-Approved Source Editing via a Patch Broker (`prepare_patch` / `get_patch_result`): the agent proposes immutable, single-file source edits or creates, and the host asks the user before applying any write
- Four-stage security model documented and enforced: `LLM proposes -> Patch Policy validates -> User approves -> Host applies`
- Patch policy (`prepare_patch`) validating exact byte-for-byte `old_text` matches, single unique occurrence, overlapping-replacement detection, path/repo-root confinement, sensitive and ignored-path blocking, binary/encoding guards, and per-file size limits
- Immutable `PatchPlan` / `PatchReplacement` / `PatchResult` models and a single-use lifecycle: `pending -> approved -> applied|conflict|failed` or `pending -> rejected` (`PatchBroker`)
- Host-only patch service (`apply_approved`) with atomic replace for edits, `O_CREAT|O_EXCL` for creates, SHA-256 conflict detection, and temp cleanup on failure - the only module that writes files
- COMPLETE unified diff returned with every proposal (never truncated) for host and user review
- CLI approval prompt and `process_pending_patches`: only `y`/`yes` approves (Enter, `n`, other input, EOF and Ctrl+C all reject with ZERO file write); approved plans apply exactly once
- `PatchBroker` isolated per agent factory call (private broker when the host supplies none; closure-bound `make_patch_tools`)
- Patch session events (`patch_approved`, `patch_rejected`, `patch_applied`, `patch_conflict`, `patch_failed`) recorded as metadata-only - the actual code content never enters durable session state
- System prompt updated: role now "User-Approved Source Editing", a Source Editing Strategy section, and a No same-turn dependent execution rule (never claim an edit until `status == 'applied'`)
- Source-editing smoke script (`scripts/smoke_source_editing.py`) with 24 checks running with no LLM API access
- Test coverage increased from 454 to 568 deterministic tests (final v0.6.0 audit suite: 658 passed, 6 skipped)

### Safety

- The agent has no direct file-write tool; `patch_tools.py` and all non-service modules are statically free of `open("w")`, `write_text`, `write_bytes`, `os.replace` and `unlink`
- File writes are confined to `patch/service.py`; patch tools only validate and register pending proposals
- No file deletion, move or rename API anywhere in the patch subsystem
- Source edits are never applied autonomously; only the host, after explicit user approval, may write
- The `patch_broker` never grants execution, Git, network, or approval authority; execution and Git boundaries are unchanged
- Patch apply revalidates the target path and file hash at write time, refusing conflicts and symlink/escapes introduced after preparation

### Fixed (Release Audit 指令9)

- **CRLF-aware exact matching**: `read_file` returned raw LF text for a CRLF file that the policy could not match for editing. Prepare now normalizes each line to `\n` for matching and reconstructs the exact original newline (`\r\n` or `\n`) on write, so LF, CRLF and BOM source files edit correctly (byte-for-byte `proposed_sha256` still holds).
- **Mixed newlines explicitly REJECTED**: a file mixing `\r\n` and `\n` (or CR-only) is refused and left byte-identical - the policy never silently normalizes.
- **`No newline at end of file` markers**: the approval diff now shows a git-style marker whenever a final newline is added or removed, so a newline-only change is never invisible.
- **Safe terminal rendering**: the CLI renders untrusted patch paths, summaries and diffs through `render_untrusted_terminal_text`, which escapes C0/C1/DEL control bytes and Unicode bidi / *Trojan Source* (U+202A-202E, U+2066-2069, U+206A-206F) codepoints - display-only, the proposed content bytes are untouched.
- **Structured lone-surrogate rejection**: a surrogate in a path, summary or replacement no longer escapes as an uncaught `UnicodeEncodeError`; it is rejected with a `PatchPolicyError` before any write.
- **Windows path hardening**: reserved device-name stems (`con`, `prn`, `aux`, `nul`, `clock$`, `com1..9`, `lpt1..9`), trailing dot/space components, and control characters in path components are all rejected; drive-relative/UNC and NTFS ADS (`:`) paths were already refused.
- **Junction / reparse escape**: path-resolution re-validation refuses a junction or symlink whose resolved target escapes the repository root, covered by contract tests even where Windows forbids creating a real symlink.
- **Create content byte cap + proposed-result byte cap**: encoded-byte budget is enforced in addition to character caps, so a create/edit can never produce a file over the byte limit (defense-in-depth on top of the char caps).
- **Explicit `applying` claim state**: an approved plan is claimed exactly once via `take_for_apply` (`approved → applying`) under a single lock before any apply; a second (even concurrent) caller is refused, and `record_application` finalises only from `applying`. Concurrent apply/create is exactly-once at both the broker and the filesystem level.
- **Per-process plan-history bound**: a broker retains at most `MAX_PATCH_PLANS` (200) plans; every plan ever registered counts (terminal plans are still counted) and a boundary apply-at-limit is not auto-granted.
- Test coverage: 91 new regression tests added in `tests/test_release_audit.py` (~450 lines) covering deep immutability, approval-to-write integrity, exactly-once concurrency, safe rendering, newline/BOM semantics, Windows path safety, size bounds, replacement semantics, injection rejection, CLI preview/per-plan approval, broker bounds, hard-link semantics and apply-time revalidation.



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
