"""Agent-facing tools for remote Git push operations.

Provides prepare_git_push and get_git_push_result tools.
"""

import uuid
from pathlib import Path

from ..git_remote.broker import GitRemoteBroker
from ..git_remote.models import PushState
from ..git_remote.policy import RemotePushPolicyError, validate_and_prepare_push


def make_git_remote_tools(repo_path: str, broker: GitRemoteBroker):
    """Create Git remote push tools bound to a specific broker.

    Args:
        repo_path: Absolute path to the Git repository.
        broker: The GitRemoteBroker instance.

    Returns:
        Tuple of (prepare_git_push, get_git_push_result) callables.
    """
    repo_path = Path(repo_path).resolve()

    def prepare_git_push(summary: str) -> dict:
        """Propose a remote Git push operation.

        This tool performs ZERO network operations. It validates local Git state
        and creates an immutable push plan for user approval.

        The push destination is determined automatically from the current branch's
        configured upstream remote. The user cannot specify an arbitrary remote URL.

        Args:
            summary: Brief description of what is being pushed.

        Returns:
            Dict with plan_id, state, and human-readable message, or error.
        """
        if not summary or not summary.strip():
            return {
                "ok": False,
                "error": "Summary is required and cannot be empty",
            }

        summary = summary.strip()
        plan_id = f"push-{uuid.uuid4().hex[:12]}"

        try:
            plan = validate_and_prepare_push(
                repo_path=str(repo_path),
                summary=summary,
                plan_id=plan_id,
            )

            broker.register(plan)

            return {
                "ok": True,
                "plan_id": plan.plan_id,
                "state": "pending",
                "message": (
                    f"Push plan created. Waiting for user approval to push "
                    f"{plan.commit_count} commit(s) from {plan.local_branch} to "
                    f"{plan.remote_name}/{plan.remote_branch}."
                ),
                "local_branch": plan.local_branch,
                "remote_name": plan.remote_name,
                "remote_branch": plan.remote_branch,
                "remote_url": plan.approved_remote_url,
                "head_oid": plan.head_oid[:7],
                "expected_remote_oid": plan.expected_remote_oid[:7],
                "commit_count": plan.commit_count,
            }

        except RemotePushPolicyError as e:
            return {
                "ok": False,
                "error": f"Push validation failed: {e}",
            }
        except Exception as e:
            return {
                "ok": False,
                "error": f"Failed to prepare push: {e}",
            }

    def get_git_push_result(plan_id: str) -> dict:
        """Retrieve the result of a push operation.

        Args:
            plan_id: The push plan identifier.

        Returns:
            Dict with plan status and result information.
        """
        if not plan_id:
            return {
                "ok": False,
                "error": "plan_id is required",
            }

        state = broker.get_state(plan_id)
        if state is None:
            return {
                "ok": False,
                "error": f"Unknown push plan {plan_id}",
            }

        if state == PushState.PENDING:
            return {
                "ok": True,
                "plan_id": plan_id,
                "state": "pending",
                "message": "Push is awaiting user approval",
            }

        if state == PushState.APPROVED:
            return {
                "ok": True,
                "plan_id": plan_id,
                "state": "approved",
                "message": "Push approved; host is applying it",
            }

        if state == PushState.APPLYING:
            return {
                "ok": True,
                "plan_id": plan_id,
                "state": "applying",
                "message": "Push is in progress",
            }

        result = broker.get_result(plan_id)
        if result is None:
            return {
                "ok": True,
                "plan_id": plan_id,
                "state": state.value,
                "message": f"Push is in state {state.value} but no result available",
            }

        response = {
            "ok": True,
            "plan_id": plan_id,
            "state": result.state.value,
            "message": result.message,
            "remote_name": result.remote_name,
            "remote_branch": result.remote_branch,
        }

        if result.state == PushState.APPLIED:
            response["remote_oid"] = result.remote_oid_after[:7] if result.remote_oid_after else None

        return response

    # Set tool metadata for Agent registration
    prepare_git_push.tool_name = "prepare_git_push"
    prepare_git_push.__doc__ = """Propose a remote Git push operation.

Validates local state and creates a push plan for the current branch to its
configured upstream remote. Performs ZERO network operations.

Args:
    summary (str): Brief description of what is being pushed

Returns:
    dict: Plan details including plan_id, or error if validation fails
"""

    get_git_push_result.tool_name = "get_git_push_result"
    get_git_push_result.__doc__ = """Retrieve the result of a push operation.

Args:
    plan_id (str): The push plan identifier

Returns:
    dict: Push result with state (pending/approved/applied/rejected/conflict/failed)
"""

    return prepare_git_push, get_git_push_result
