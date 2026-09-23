"""Agent-facing tools for remote Git fetch operations.

Provides prepare_git_fetch and get_git_fetch_result tools.
"""

import uuid
from pathlib import Path

from ..git_fetch.broker import GitFetchBroker
from ..git_fetch.models import FetchState
from ..git_fetch.policy import RemoteFetchPolicyError, validate_and_prepare_fetch
from ..session.state import SessionState


def make_git_fetch_tools(
    repo_path: str, broker: GitFetchBroker, session_state: SessionState
):
    """Create Git remote fetch tools bound to a specific broker and session.

    Args:
        repo_path: Absolute path to the Git repository.
        broker: The GitFetchBroker instance.
        session_state: The SessionState instance.

    Returns:
        Tuple of (prepare_git_fetch, get_git_fetch_result) callables.
    """
    repo_path = Path(repo_path).resolve()

    def prepare_git_fetch(summary: str) -> dict:
        """Propose a remote Git fetch operation.

        This tool performs ZERO network operations. It validates local Git state
        and creates an immutable fetch plan for user approval.

        The fetch source is determined automatically from the current branch's
        configured upstream remote. The user cannot specify an arbitrary remote URL.

        Args:
            summary: Brief description of why the fetch is needed.

        Returns:
            Dict with plan_id, state, and human-readable message, or error.
        """
        if not summary or not summary.strip():
            return {
                "ok": False,
                "error": "Summary is required and cannot be empty",
            }

        summary = summary.strip()
        plan_id = f"fetch-{uuid.uuid4().hex[:12]}"

        try:
            plan = validate_and_prepare_fetch(
                repo_path=str(repo_path),
                summary=summary,
                plan_id=plan_id,
            )

            broker.register(plan)

            # Record event
            session_state.add_event(
                kind="git_fetch_prepared",
                data={
                    "plan_id": plan_id,
                    "remote_name": plan.remote_name,
                    "remote_branch": plan.remote_branch,
                    "tracking_ref": plan.tracking_ref,
                    "remote_host": plan.remote_host,
                },
            )

            return {
                "ok": True,
                "plan_id": plan.plan_id,
                "state": "pending",
                "message": (
                    f"Fetch plan created. Waiting for user approval to fetch "
                    f"{plan.remote_name}/{plan.remote_branch} into {plan.tracking_ref}."
                ),
                "local_branch": plan.local_branch,
                "remote_name": plan.remote_name,
                "remote_branch": plan.remote_branch,
                "tracking_ref": plan.tracking_ref,
                "remote_url": plan.approved_remote_url,
                "local_head_oid": plan.local_head_oid[:7],
                "expected_tracking_oid": plan.expected_tracking_oid[:7],
            }

        except RemoteFetchPolicyError as e:
            return {
                "ok": False,
                "error": f"Fetch validation failed: {e}",
            }
        except Exception as e:
            return {
                "ok": False,
                "error": f"Failed to prepare fetch: {e}",
            }

    def get_git_fetch_result(plan_id: str) -> dict:
        """Retrieve the result of a fetch operation.

        Args:
            plan_id: The fetch plan identifier.

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
                "error": f"Unknown fetch plan {plan_id}",
            }

        if state == FetchState.PENDING:
            return {
                "ok": True,
                "plan_id": plan_id,
                "state": "pending",
                "message": "Fetch is awaiting user approval",
            }

        if state == FetchState.APPROVED:
            return {
                "ok": True,
                "plan_id": plan_id,
                "state": "approved",
                "message": "Fetch approved; host is applying it",
            }

        if state == FetchState.APPLYING:
            return {
                "ok": True,
                "plan_id": plan_id,
                "state": "applying",
                "message": "Fetch is in progress",
            }

        result = broker.get_result(plan_id)
        if result is None:
            return {
                "ok": True,
                "plan_id": plan_id,
                "state": state.value,
                "message": f"Fetch is in state {state.value} but no result available",
            }

        # Record event
        session_state.add_event(
            kind=f"git_fetch_{result.state.value}",
            data={
                "plan_id": plan_id,
                "remote_name": result.remote_name,
                "remote_branch": result.remote_branch,
                "tracking_ref": result.tracking_ref,
                "previous_oid": result.previous_tracking_oid,
                "observed_oid": result.observed_remote_oid,
                "updated_oid": result.updated_tracking_oid,
                "changed": result.changed,
            },
        )

        response = {
            "ok": True,
            "plan_id": plan_id,
            "state": result.state.value,
            "message": result.message,
            "remote_name": result.remote_name,
            "remote_branch": result.remote_branch,
            "tracking_ref": result.tracking_ref,
        }

        if result.state == FetchState.APPLIED:
            response["previous_oid"] = result.previous_tracking_oid[:7]
            response["observed_remote_oid"] = (
                result.observed_remote_oid[:7] if result.observed_remote_oid else None
            )
            response["updated_oid"] = (
                result.updated_tracking_oid[:7] if result.updated_tracking_oid else None
            )
            response["changed"] = result.changed

        if result.state == FetchState.CONFLICT and result.observed_remote_oid:
            response["observed_remote_oid"] = result.observed_remote_oid[:7]

        return response

    # Set tool metadata for Agent registration
    prepare_git_fetch.tool_name = "prepare_git_fetch"
    prepare_git_fetch.__doc__ = """Propose a remote Git fetch operation.

Validates local state and creates a fetch plan for the current branch's
configured upstream remote. Performs ZERO network operations.

Args:
    summary (str): Brief description of why the fetch is needed

Returns:
    dict: Plan details including plan_id, or error if validation fails
"""

    get_git_fetch_result.tool_name = "get_git_fetch_result"
    get_git_fetch_result.__doc__ = """Retrieve the result of a fetch operation.

Args:
    plan_id (str): The fetch plan identifier

Returns:
    dict: Fetch result with state (pending/approved/applied/rejected/conflict/failed)
"""

    return prepare_git_fetch, get_git_fetch_result
