"""Agent-visible Git mutation proposal tools (v0.7.0).

Exposes exactly three tools to the model:

* ``prepare_git_stage``      -- validate a single-file stage request into an
  immutable GitStagePlan and register it as ``pending``. NEVER mutates Git.
* ``prepare_git_commit``     -- validate a commit request (entire staged snapshot)
  into an immutable GitCommitPlan and register it as ``pending``. NEVER mutates Git.
* ``get_git_mutation_result`` -- read-only lookup of a plan's lifecycle status or
  its host-side apply result. NEVER mutates Git.

:func:`make_git_mutation_tools` produces closure-bound tool instances that share a
single :class:`GitMutationBroker`, isolating Git mutation state per agent session.
These tools call only the validating policy and the broker's ``register`` path:
they perform no Git add/commit/push.
"""

from __future__ import annotations

from typing import Any

from strands import tool

from ..git_mutation import GitMutationBroker, GitMutationPolicyError
from ..git_mutation.policy import prepare_commit_plan, prepare_stage_plan
from .path_utils import find_repo_root


def _prepare_stage_core(
    broker: GitMutationBroker,
    *,
    path: str,
    summary: str,
    root,
) -> dict[str, Any]:
    """Validate + register one pending GitStagePlan through an explicit broker."""
    try:
        plan = prepare_stage_plan(
            path=path,
            summary=summary,
            repo_root=root,
        )
    except GitMutationPolicyError as exc:
        return {"ok": False, "denied": True, "error": str(exc)}

    try:
        broker.register(plan)
    except Exception as exc:
        return {"ok": False, "denied": False, "error": str(exc)}

    return {
        "ok": True,
        "requires_approval": True,
        "status": "pending",
        "plan_id": plan.id,
        "kind": "stage",
        "path": plan.repo_path,
        "branch": plan.branch,
        "head": plan.head_oid,
        "diff": plan.diff,
        "message": (
            "Git stage proposal prepared but NOT applied to the index. The host "
            "will show the complete staged diff and ask the user to approve it; "
            "only after approval will the Git index be mutated. Never claim a file "
            "was staged unless you later retrieve a GitMutationResult with status "
            "'applied' for this plan_id."
        ),
    }


def _prepare_commit_core(
    broker: GitMutationBroker,
    *,
    message: str,
    root,
) -> dict[str, Any]:
    """Validate + register one pending GitCommitPlan through an explicit broker."""
    try:
        plan = prepare_commit_plan(
            message=message,
            repo_root=root,
        )
    except GitMutationPolicyError as exc:
        return {"ok": False, "denied": True, "error": str(exc)}

    try:
        broker.register(plan)
    except Exception as exc:
        return {"ok": False, "denied": False, "error": str(exc)}

    return {
        "ok": True,
        "requires_approval": True,
        "status": "pending",
        "plan_id": plan.id,
        "kind": "commit",
        "branch": plan.branch,
        "head": plan.head_oid,
        "author": f"{plan.author_name} <{plan.author_email}>",
        "staged_files": list(plan.staged_paths),
        "diff": plan.diff,
        "message": (
            "Git commit proposal prepared but NOT created. The host will show the "
            "complete commit diff (all staged files) and ask the user to approve it; "
            "only after approval will the local commit be created. Never claim a "
            "commit was created unless you later retrieve a GitMutationResult with "
            "status 'applied' and a commit_oid for this plan_id. This does NOT push "
            "to any remote."
        ),
    }


def _get_mutation_result_core(
    broker: GitMutationBroker, plan_id: str
) -> dict[str, Any]:
    """Read-only lookup (never mutates Git)."""
    plan = broker.get_plan(plan_id)
    if plan is None:
        return {"ok": False, "error": f"Unknown Git mutation plan id: {plan_id}"}

    result = broker.get_result(plan_id)
    if result is None:
        status = broker.status(plan_id)
        return {
            "ok": True,
            "plan_id": plan_id,
            "applied": False,
            "status": status,
            "kind": plan.kind,
            "message": f"Plan is {status}; not yet applied.",
        }

    return {
        "ok": True,
        "plan_id": result.plan_id,
        "applied": result.status == "applied",
        "status": result.status,
        "kind": result.kind,
        "path": result.path,
        "commit_oid": result.commit_oid,
        "message": result.message,
    }


# ---------------------------------------------------------------------------
# Closure factory (agent-session isolation)
# ---------------------------------------------------------------------------


def make_git_mutation_tools(broker: GitMutationBroker | None = None):
    """Produce closure-bound Git mutation tools sharing one GitMutationBroker.

    If *broker* is None, a fresh private broker is created for this agent session.
    This ensures two agents in one process share no Git mutation state.

    Returns:
        A tuple of three Strands tools:
        ``(prepare_git_stage, prepare_git_commit, get_git_mutation_result)``.
    """
    if broker is None:
        broker = GitMutationBroker()

    @tool
    def prepare_git_stage(path: str, summary: str) -> dict[str, Any]:
        """Propose staging one repository file to the Git index.

        ONE StagePlan = ONE FILE. To stage multiple files, call this multiple
        times and approve each independently.

        Args:
            path: Repository-relative path to the single file to stage (e.g.
                "src/agent.py"). Must name a concrete file, not a directory or
                ".", and must not be an absolute path or use "..".
            summary: Concise user-visible change summary (e.g., "Stage agent
                wiring changes for v0.7").

        Returns:
            A dict with:
            - ``ok`` (bool): True if the plan was prepared successfully.
            - ``denied`` (bool): True if the policy rejected the request.
            - ``requires_approval`` (bool): True (always) - staging requires
              host-side user approval before any Git index mutation occurs.
            - ``status`` (str): "pending" (awaiting user approval).
            - ``plan_id`` (str): Unique identifier for this stage proposal. Use
              this with ``get_git_mutation_result`` to check whether the user
              approved and the host applied it.
            - ``kind`` (str): "stage".
            - ``path`` (str): Repository-relative path.
            - ``branch`` (str): Current branch name.
            - ``head`` (str): Current HEAD commit OID.
            - ``diff`` (str): Complete unified diff from HEAD to the proposed
              staged state for this file (never truncated). This is what the
              user will see and approve.
            - ``error`` (str, if ok=False): Reason the request was denied.

        Important:
            - This does NOT stage the file. The Git index is NOT mutated until
              the user approves the proposal.
            - Never claim a file was staged until you call
              ``get_git_mutation_result`` and receive status="applied".
            - Supported: text add and text modify only.
            - NOT supported: file deletion, rename, copy, binary files, symlinks,
              submodules, conflicts, directory staging, git add ., partial hunks.
            - External clean filters and working-tree-encoding are rejected.

        Example:
            result = prepare_git_stage(
                path="src/harness_agent/agent.py",
                summary="Stage the reviewed v0.7 agent wiring changes."
            )
            if result["ok"]:
                print(f"Stage proposal {result['plan_id']} awaiting approval.")
                print(f"Diff:\\n{result['diff']}")
        """
        root = find_repo_root()
        return _prepare_stage_core(broker, path=path, summary=summary, root=root)

    @tool
    def prepare_git_commit(message: str) -> dict[str, Any]:
        """Propose creating a local commit from the entire currently staged snapshot.

        ONE CommitPlan = ENTIRE STAGED SNAPSHOT. The agent cannot choose which
        staged files to commit; the commit will include everything currently
        staged, and the user sees the complete staged diff before approval.

        Args:
            message: Commit message text (e.g., "Add v0.7 Git mutation tools").
                Must be non-blank, valid UTF-8, no NUL bytes, no control/bidi
                spoofing characters. CRLF/CR are normalized to LF. Max 4000 chars.

        Returns:
            A dict with:
            - ``ok`` (bool): True if the plan was prepared successfully.
            - ``denied`` (bool): True if the policy rejected the request.
            - ``requires_approval`` (bool): True (always) - committing requires
              host-side user approval before any local commit is created.
            - ``status`` (str): "pending" (awaiting user approval).
            - ``plan_id`` (str): Unique identifier for this commit proposal. Use
              this with ``get_git_mutation_result`` to check whether the user
              approved and the host created the commit.
            - ``kind`` (str): "commit".
            - ``branch`` (str): Current branch name.
            - ``head`` (str): Current HEAD commit OID (parent).
            - ``author`` (str): Git identity that will author the commit
              (user.name <user.email>).
            - ``staged_files`` (list): All staged changed file paths that will
              be included in the commit.
            - ``diff`` (str): Complete unified diff from HEAD to the entire
              staged snapshot (never truncated). This is what the user will see
              and approve.
            - ``error`` (str, if ok=False): Reason the request was denied.

        Important:
            - This does NOT create the commit. No local commit is created until
              the user approves the proposal.
            - Never claim a commit was created until you call
              ``get_git_mutation_result`` and receive status="applied" with a
              commit_oid.
            - A successful commit updates the LOCAL repository only. It does NOT
              push to any remote. Never claim "GitHub updated" or "remote updated".
            - Supported: commits with text add/modify staged files only.
            - NOT supported: initial/unborn commits, detached HEAD, commits with
              staged deletions/renames/binary files/symlinks/submodules, amend,
              allow-empty, merge commits, tag, push.
            - Git hooks, signing, editor, and pager are all disabled.
            - Identity (user.name and user.email) must be configured in Git config.

        Example:
            result = prepare_git_commit(
                message="Implement Git mutation broker and policy"
            )
            if result["ok"]:
                print(f"Commit proposal {result['plan_id']} awaiting approval.")
                print(f"Will commit {len(result['staged_files'])} files.")
                print(f"Diff:\\n{result['diff']}")
        """
        root = find_repo_root()
        return _prepare_commit_core(broker, message=message, root=root)

    @tool
    def get_git_mutation_result(plan_id: str) -> dict[str, Any]:
        """Check the status or outcome of a Git mutation plan (stage or commit).

        Use this after ``prepare_git_stage`` or ``prepare_git_commit`` to check
        whether the user approved the plan and whether the host successfully
        applied it.

        Args:
            plan_id: The unique plan ID returned by ``prepare_git_stage`` or
                ``prepare_git_commit``.

        Returns:
            A dict with:
            - ``ok`` (bool): True if the plan_id is known.
            - ``plan_id`` (str): The plan ID.
            - ``applied`` (bool): True only if status is "applied" (Git was
              successfully mutated).
            - ``status`` (str): Lifecycle status:
                - "pending": awaiting user approval.
                - "approved": user approved but not yet applied.
                - "applying": host is currently applying (transient).
                - "applied": successfully applied (stage or commit complete).
                - "rejected": user declined; no Git mutation occurred.
                - "conflict": state changed after approval; no mutation occurred.
                - "failed": apply was attempted but failed.
            - ``kind`` (str): "stage" or "commit".
            - ``path`` (str, if kind="stage"): Repository path that was staged.
            - ``commit_oid`` (str, if kind="commit" and applied): New commit OID
              (40-character hex SHA-1).
            - ``message`` (str): Human-readable outcome message.
            - ``error`` (str, if ok=False): Unknown plan_id.

        Important:
            - Only when status="applied" should you claim the operation succeeded.
            - For stage: status="applied" means the file is now staged in the index.
            - For commit: status="applied" means the commit was created locally;
              check commit_oid for the new commit hash. This does NOT mean the
              commit was pushed.

        Example:
            result = get_git_mutation_result(plan_id="<uuid>")
            if result["applied"]:
                if result["kind"] == "stage":
                    print(f"File {result['path']} is now staged.")
                elif result["kind"] == "commit":
                    print(f"Commit {result['commit_oid']} created locally.")
            elif result["status"] == "rejected":
                print("User declined the proposal.")
            elif result["status"] == "pending":
                print("Still awaiting user approval.")
        """
        return _get_mutation_result_core(broker, plan_id)

    return prepare_git_stage, prepare_git_commit, get_git_mutation_result
