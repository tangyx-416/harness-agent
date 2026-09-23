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

v0.6.0: one explicit PatchBroker is added. The agent may PREPARE single-file
source-edit proposals; the CLI shows the COMPLETE diff and the user approves
or rejects each one. Only the host applies an approved patch via the patch
service. Patch plans, results and host events stay isolated to this CLI.

v0.7.0: one explicit GitMutationBroker is added. The agent may PREPARE Git
stage (single-file) and commit (entire staged snapshot) proposals; the CLI
shows the COMPLETE diff and the user approves or rejects each one. Only the
host mutates the Git index or creates local commits. No push.

v0.8.0: one explicit GitRemoteBroker is added. The agent may PREPARE remote
Git push proposals; the CLI shows the COMPLETE outgoing commit list and diff,
and the user approves or rejects each one. Only the host performs network
operations and pushes to the remote. HTTPS only.

v0.9.0: one explicit GitFetchBroker is added. The agent may PREPARE remote
Git fetch proposals; the CLI shows the COMPLETE fetch scope (remote URL, branch,
tracking ref) and the user approves or rejects each one. Only the host performs
network operations and fetches from the remote. HTTPS only.
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
from harness_agent.git_mutation import GitMutationBroker
from harness_agent.git_mutation.service import (
    apply_approved_commit,
    apply_approved_stage,
)
from harness_agent.git_fetch import GitFetchBroker
from harness_agent.git_fetch.service import apply_fetch
from harness_agent.git_remote import GitRemoteBroker
from harness_agent.git_remote.service import apply_push
from harness_agent.patch import (
    PatchBroker,
    apply_approved,
    render_untrusted_terminal_text,
)
from harness_agent.tools.path_utils import find_repo_root
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
    print("Harness Agent v0.9")
    print("Session state: ephemeral (cleared when this CLI exits).")
    print("Source edits: proposed by the agent, applied only after you approve.")
    print("Git stage/commit: proposed by the agent, applied only after you approve.")
    print("Git push: proposed by the agent, applied only after you approve.")
    print("Git fetch: proposed by the agent, applied only after you approve.")
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


def _show_patch_proposal(plan) -> None:
    """Render the COMPLETE diff of one pending patch plan for user review.

    The diff, summaries and path are untrusted data (the agent controls the
    proposed source text). We render them through
    :func:`render_untrusted_terminal_text` so any ANSI escape / control / bidi
    characters are shown as visible escaped notation and can never manipulate
    the terminal or hide the real change. This is a display-only transform:
    the proposed content held in the immutable plan is never altered.
    """
    print("-" * 60)
    print("Patch proposal required")
    print()
    print(f"Operation: {plan.operation}")
    print(f"File:      {render_untrusted_terminal_text(plan.repo_path)}")
    for line in plan.summaries:
        print(f"Summary:   {render_untrusted_terminal_text(line)}")
    print()
    print("Complete diff:")
    print(render_untrusted_terminal_text(plan.diff) if plan.diff else "(no diff)")
    print("-" * 60)


def _request_patch_approval(plan) -> bool:
    """Ask the user to approve one patch plan. Only explicit y/yes approves."""
    _show_patch_proposal(plan)
    try:
        answer = input("Apply this patch? [y/N]: ")
    except (KeyboardInterrupt, EOFError):
        print("\n(Cancelled)")
        return False
    return answer.strip().lower() in _APPROVAL_WORDS


def process_pending_patches(
    patch_broker: PatchBroker,
    session_state: SessionState | None = None,
    root=None,
) -> None:
    """Show pending patch plans to the user and apply the approved ones.

    This is the trusted host layer: the user approves/rejects each COMPLETE
    diff here (never the model). Approval authorizes exactly one apply attempt,
    performed by :func:`harness_agent.patch.service.apply_approved`.
    """
    if root is None:
        root = find_repo_root()
    for plan in patch_broker.pending():
        if not _request_patch_approval(plan):
            patch_broker.reject(plan.id)
            if session_state is not None:
                session_state.record_patch_rejected(plan.id)
            print("Patch cancelled: ZERO file writes occurred.\n")
            continue
        try:
            if session_state is not None:
                session_state.record_patch_approved(plan.id)
            patch_broker.approve(plan.id)
            result = apply_approved(patch_broker, plan.id, root)
        except Exception as exc:
            print(f"Patch apply error: {exc}\n")
            continue
        if session_state is not None:
            if result.status == "applied":
                session_state.record_patch_applied(plan.id)
            elif result.status == "conflict":
                session_state.record_patch_conflict(plan.id)
            else:
                session_state.record_patch_failed(plan.id)
        print(f"Patch result: {result.status} -- {result.message}\n")


def _show_git_stage_proposal(plan) -> None:
    """Render the COMPLETE diff of one pending stage plan for user review."""
    print("-" * 60)
    print("Git stage proposal")
    print()
    print(f"Plan ID:  {plan.id}")
    print(f"Branch:   {plan.branch}")
    print(f"HEAD:     {plan.head_oid[:8]}")
    print(f"File:     {render_untrusted_terminal_text(plan.repo_path)}")
    print(f"Summary:  {render_untrusted_terminal_text(plan.summary)}")
    print()
    print("Complete staged diff:")
    print(render_untrusted_terminal_text(plan.diff) if plan.diff else "(no diff)")
    print("-" * 60)


def _request_stage_approval(plan) -> bool:
    """Ask the user to approve one stage plan. Only explicit y/yes approves."""
    _show_git_stage_proposal(plan)
    try:
        answer = input("Stage this file? [y/N]: ")
    except (KeyboardInterrupt, EOFError):
        print("\n(Cancelled)")
        return False
    return answer.strip().lower() in _APPROVAL_WORDS


def _show_git_commit_proposal(plan) -> None:
    """Render the COMPLETE diff of one pending commit plan for user review."""
    print("-" * 60)
    print("Git commit proposal")
    print()
    print(f"Plan ID:  {plan.id}")
    print(f"Branch:   {plan.branch}")
    print(f"Parent:   {plan.head_oid[:8]}")
    print(f"Author:   {render_untrusted_terminal_text(plan.author_name)} <{render_untrusted_terminal_text(plan.author_email)}>")
    print()
    print("Commit message:")
    print(render_untrusted_terminal_text(plan.message))
    print()
    print(f"Staged files ({len(plan.staged_paths)}):")
    for path in plan.staged_paths[:10]:  # Show first 10
        print(f"  {render_untrusted_terminal_text(path)}")
    if len(plan.staged_paths) > 10:
        print(f"  ... and {len(plan.staged_paths) - 10} more")
    print()
    print("Complete commit diff:")
    print(render_untrusted_terminal_text(plan.diff) if plan.diff else "(no diff)")
    print()
    print("NOTE: This does NOT push to any remote.")
    print("-" * 60)


def _request_commit_approval(plan) -> bool:
    """Ask the user to approve one commit plan. Only explicit y/yes approves."""
    _show_git_commit_proposal(plan)
    try:
        answer = input("Create this local commit? [y/N]: ")
    except (KeyboardInterrupt, EOFError):
        print("\n(Cancelled)")
        return False
    return answer.strip().lower() in _APPROVAL_WORDS


def process_pending_git_mutations(
    git_mutation_broker: GitMutationBroker,
    session_state: SessionState | None = None,
    root=None,
) -> None:
    """Show pending Git mutation plans to the user and apply the approved ones.

    This is the trusted host layer: the user approves/rejects each COMPLETE
    diff here (never the model). Approval authorizes exactly one apply attempt.
    """
    if root is None:
        root = find_repo_root()

    # Process stage plans first
    for plan in git_mutation_broker.pending_stage_plans():
        if not _request_stage_approval(plan):
            git_mutation_broker.reject(plan.id)
            if session_state is not None:
                session_state.record_git_stage_rejected(plan.id)
            print("Stage cancelled: ZERO Git index mutation occurred.\n")
            continue
        try:
            if session_state is not None:
                session_state.record_git_stage_approved(plan.id)
            git_mutation_broker.approve(plan.id)
            result = apply_approved_stage(git_mutation_broker, plan.id, root)
        except Exception as exc:
            print(f"Stage apply error: {exc}\n")
            continue
        if session_state is not None:
            if result.status == "applied":
                session_state.record_git_stage_applied(plan.id, result.path)
            elif result.status == "conflict":
                session_state.record_git_stage_conflict(plan.id)
            else:
                session_state.record_git_stage_failed(plan.id)
        print(f"Stage result: {result.status} -- {result.message}\n")

    # Process commit plans after stage plans
    for plan in git_mutation_broker.pending_commit_plans():
        if not _request_commit_approval(plan):
            git_mutation_broker.reject(plan.id)
            if session_state is not None:
                session_state.record_git_commit_rejected(plan.id)
            print("Commit cancelled: ZERO local commit created.\n")
            continue
        try:
            if session_state is not None:
                session_state.record_git_commit_approved(plan.id)
            git_mutation_broker.approve(plan.id)
            result = apply_approved_commit(git_mutation_broker, plan.id, root)
        except Exception as exc:
            print(f"Commit apply error: {exc}\n")
            continue
        if session_state is not None:
            if result.status == "applied":
                session_state.record_git_commit_applied(plan.id, result.commit_oid)
            elif result.status == "conflict":
                session_state.record_git_commit_conflict(plan.id)
            else:
                session_state.record_git_commit_failed(plan.id)
        print(f"Commit result: {result.status} -- {result.message}\n")


def _request_push_approval(plan) -> bool:
    """Show a push approval prompt and return True if user approves."""
    print("-" * 70)
    print("REMOTE GIT PUSH PROPOSAL")
    print("-" * 70)
    print(f"Plan ID: {plan.plan_id}")
    print(f"Summary: {render_untrusted_terminal_text(plan.summary)}")
    print()
    print(f"Local branch:  {plan.local_branch}")
    print(f"Local HEAD:    {plan.head_oid[:7]}")
    print()
    print(f"Remote:        {plan.remote_name}")
    print(f"Remote URL:    {render_untrusted_terminal_text(plan.approved_remote_url)}")
    print(f"Remote branch: {plan.remote_branch}")
    print(f"Expected remote OID: {plan.expected_remote_oid[:7]}")
    print()
    print(f"Outgoing commits: {plan.commit_count}")
    print()
    for i, commit in enumerate(plan.outgoing_commits, 1):
        subject = render_untrusted_terminal_text(commit.subject)
        print(f"  {i}. {commit.short_oid} {subject}")
    print()
    print("COMPLETE OUTGOING DIFF:")
    print("-" * 70)
    safe_diff = render_untrusted_terminal_text(plan.diff)
    print(safe_diff)
    print("-" * 70)
    print()
    print("This operation will contact the remote Git server over HTTPS.")
    print("This operation creates a remote repository mutation.")
    print("This does NOT push tags.")
    print("This does NOT force-push.")
    print("This does NOT create a remote branch.")
    print()
    print("Push this exact local HEAD to this exact existing remote branch? [y/N]")

    try:
        answer = input("> ").strip().lower()
        return answer in _APPROVAL_WORDS
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def process_pending_git_pushes(
    git_remote_broker: GitRemoteBroker,
    session_state: SessionState | None = None,
    root=None,
) -> None:
    """Show pending Git push plans to the user and apply the approved ones.

    This is the trusted host layer: the user approves/rejects each COMPLETE
    push preview here (never the model). Approval authorizes exactly one remote
    push attempt with preflight, lease, and post-verification.
    """
    if root is None:
        root = find_repo_root()

    for plan in git_remote_broker.pending_plans():
        if not _request_push_approval(plan):
            git_remote_broker.reject(plan.plan_id)
            if session_state is not None:
                session_state.record_git_push_rejected(plan.plan_id)
            print("Push cancelled: ZERO network operation performed.\n")
            continue

        try:
            if session_state is not None:
                session_state.record_git_push_approved(
                    plan.plan_id, plan.remote_name, plan.remote_branch
                )
            git_remote_broker.approve(plan.plan_id)
            result = apply_push(plan, str(root), git_remote_broker)
        except Exception as exc:
            print(f"Push apply error: {exc}\n")
            continue

        if session_state is not None:
            if result.state.value == "applied":
                session_state.record_git_push_applied(
                    plan.plan_id, result.remote_name, result.remote_branch, result.head_oid
                )
            elif result.state.value == "conflict":
                session_state.record_git_push_conflict(plan.plan_id, result.message)
            else:
                session_state.record_git_push_failed(plan.plan_id, result.message)

        print(f"Push result: {result.state.value} -- {result.message}\n")


def _request_fetch_approval(plan) -> bool:
    """Show a fetch approval prompt and return True if user approves."""
    print("-" * 70)
    print("REMOTE GIT FETCH PROPOSAL")
    print("-" * 70)
    print(f"Plan ID: {plan.plan_id}")
    print(f"Summary: {render_untrusted_terminal_text(plan.summary)}")
    print()
    print(f"Local branch:  {plan.local_branch}")
    print(f"Local HEAD:    {plan.local_head_oid[:7]}")
    print()
    print(f"Remote:        {plan.remote_name}")
    print(f"Remote URL:    {render_untrusted_terminal_text(plan.approved_remote_url)}")
    print(f"Remote branch: {plan.remote_branch}")
    print()
    print(f"Tracking ref:         {plan.tracking_ref}")
    print(f"Current tracking OID: {plan.expected_tracking_oid[:7]}")
    print()
    print("-" * 70)
    print()
    print("This operation will:")
    print("  1. Contact the remote Git server over HTTPS (network READ)")
    print("  2. Transfer Git objects into the local repository")
    print("  3. Update ONLY the tracking ref above (if remote has new commits)")
    print()
    print("This operation will NOT:")
    print("  - Modify your working tree")
    print("  - Modify the Git index")
    print("  - Move HEAD or any local branch")
    print("  - Fetch or modify tags")
    print("  - Recurse into submodules")
    print("  - Merge, rebase, or pull")
    print("  - Modify the remote repository")
    print()
    print("Fetch from this remote and update this tracking ref? [y/N]")

    try:
        answer = input("> ").strip().lower()
        return answer in _APPROVAL_WORDS
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def process_pending_git_fetches(
    git_fetch_broker: GitFetchBroker,
    session_state: SessionState | None = None,
    root=None,
) -> None:
    """Show pending Git fetch plans to the user and apply the approved ones.

    This is the trusted host layer: the user approves/rejects each COMPLETE
    fetch preview here (never the model). Approval authorizes exactly one remote
    fetch attempt with revalidation and atomic tracking ref update.
    """
    if root is None:
        root = find_repo_root()

    for plan in git_fetch_broker.pending_plans():
        if not _request_fetch_approval(plan):
            git_fetch_broker.reject(plan.plan_id)
            if session_state is not None:
                session_state.add_event(
                    kind="git_fetch_rejected",
                    data={"plan_id": plan.plan_id},
                )
            print("Fetch cancelled: ZERO network operation performed.\n")
            continue

        try:
            if session_state is not None:
                session_state.add_event(
                    kind="git_fetch_approved",
                    data={
                        "plan_id": plan.plan_id,
                        "remote_name": plan.remote_name,
                        "remote_branch": plan.remote_branch,
                    },
                )
            git_fetch_broker.approve(plan.plan_id)
            result = apply_fetch(plan, str(root), git_fetch_broker)
        except Exception as exc:
            print(f"Fetch apply error: {exc}\n")
            continue

        if session_state is not None:
            if result.state.value == "applied":
                session_state.add_event(
                    kind="git_fetch_applied",
                    data={
                        "plan_id": plan.plan_id,
                        "remote_name": result.remote_name,
                        "remote_branch": result.remote_branch,
                        "tracking_ref": result.tracking_ref,
                        "changed": result.changed,
                    },
                )
            elif result.state.value == "conflict":
                session_state.add_event(
                    kind="git_fetch_conflict",
                    data={"plan_id": plan.plan_id, "message": result.message},
                )
            else:
                session_state.add_event(
                    kind="git_fetch_failed",
                    data={"plan_id": plan.plan_id, "message": result.message},
                )

        print(f"Fetch result: {result.state.value} -- {result.message}\n")


def main():
    """Run the interactive agent CLI."""
    # Load configuration and create agent
    try:
        config = AgentConfig.from_env()
        session_state = SessionState()
        execution_broker = ExecutionBroker()
        patch_broker = PatchBroker()
        git_mutation_broker = GitMutationBroker()
        git_remote_broker = GitRemoteBroker()
        git_fetch_broker = GitFetchBroker()
        agent = create_agent(
            config,
            session_state=session_state,
            execution_broker=execution_broker,
            patch_broker=patch_broker,
            git_mutation_broker=git_mutation_broker,
            git_remote_broker=git_remote_broker,
            git_fetch_broker=git_fetch_broker,
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

                # v0.6.0: the agent may also have prepared source-edit patches.
                # The user approves/rejects each COMPLETE diff here; only the
                # host applies anything.
                process_pending_patches(
                    patch_broker=patch_broker,
                    session_state=session_state,
                )

                # v0.7.0: the agent may also have prepared Git mutation plans
                # (stage and/or commit). The user approves/rejects each COMPLETE
                # diff here; only the host mutates Git.
                process_pending_git_mutations(
                    git_mutation_broker=git_mutation_broker,
                    session_state=session_state,
                )

                # v0.8.0: the agent may also have prepared Git remote push plans.
                # The user approves/rejects each COMPLETE outgoing commit list and
                # diff here; only the host performs network operations and pushes.
                process_pending_git_pushes(
                    git_remote_broker=git_remote_broker,
                    session_state=session_state,
                )

                # v0.9.0: the agent may also have prepared Git remote fetch plans.
                # The user approves/rejects each COMPLETE fetch scope here; only
                # the host performs network operations and fetches.
                process_pending_git_fetches(
                    git_fetch_broker=git_fetch_broker,
                    session_state=session_state,
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
