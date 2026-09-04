"""Service layer for remote Git push operations.

Handles the actual network push execution with preflight, lease, and verification.
"""

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .broker import GitRemoteBroker
from .models import GitPushPlan, GitPushResult, PushState


def apply_push(
    plan: GitPushPlan,
    repo_path: str,
    broker: GitRemoteBroker,
) -> GitPushResult:
    """Apply an approved push plan with preflight, lease, and verification.

    This function performs network operations ONLY after the user has approved
    the plan. It implements a three-phase protocol:
    1. Remote preflight: verify remote ref equals expected OID
    2. Lease-bound push: push with exact compare-and-swap
    3. Post-verification: verify remote ref equals pushed HEAD

    Args:
        plan: The approved push plan.
        repo_path: Absolute path to the Git repository.
        broker: The broker managing this plan.

    Returns:
        A GitPushResult with the outcome.
    """
    # Atomically claim the plan for application
    should_record_result = broker.take_for_apply(plan.plan_id)

    if not should_record_result:
        # Plan is not in approved state or already claimed
        state = broker.get_state(plan.plan_id)
        if state in (PushState.APPLIED, PushState.CONFLICT, PushState.FAILED):
            # Re-validation: check if remote state matches cached result
            # For push, we just return the cached result
            existing_result = broker.get_result(plan.plan_id)
            if existing_result:
                return existing_result

        return GitPushResult(
            plan_id=plan.plan_id,
            state=PushState.FAILED,
            remote_name=plan.remote_name,
            remote_branch=plan.remote_branch,
            head_oid=plan.head_oid,
            remote_oid_after=None,
            message=f"Push plan {plan.plan_id} is not approved or already processed",
            created_at=datetime.now(timezone.utc),
        )

    # We successfully claimed the plan - now perform the push
    try:
        repo_path = Path(repo_path).resolve()

        # Re-validate local state hasn't changed
        validation_error = _validate_local_state(repo_path, plan)
        if validation_error:
            result = GitPushResult(
                plan_id=plan.plan_id,
                state=PushState.CONFLICT,
                remote_name=plan.remote_name,
                remote_branch=plan.remote_branch,
                head_oid=plan.head_oid,
                remote_oid_after=None,
                message=validation_error,
                created_at=datetime.now(timezone.utc),
            )
            if should_record_result:
                broker.record_result(result)
            return result

        # Re-validate remote URL hasn't changed
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", plan.remote_name],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise Exception(f"Remote {plan.remote_name} not found")

            current_url = result.stdout.strip()
            if current_url != plan.approved_remote_url:
                result = GitPushResult(
                    plan_id=plan.plan_id,
                    state=PushState.CONFLICT,
                    remote_name=plan.remote_name,
                    remote_branch=plan.remote_branch,
                    head_oid=plan.head_oid,
                    remote_oid_after=None,
                    message=f"Remote URL changed from {plan.approved_remote_url!r} to {current_url!r}",
                    created_at=datetime.now(timezone.utc),
                )
                if should_record_result:
                    broker.record_result(result)
                return result
        except Exception as e:
            result = GitPushResult(
                plan_id=plan.plan_id,
                state=PushState.FAILED,
                remote_name=plan.remote_name,
                remote_branch=plan.remote_branch,
                head_oid=plan.head_oid,
                remote_oid_after=None,
                message=f"Failed to verify remote: {e}",
                created_at=datetime.now(timezone.utc),
            )
            if should_record_result:
                broker.record_result(result)
            return result

        # Phase 1: Remote preflight
        preflight_result = _remote_preflight(repo_path, plan)
        if preflight_result is not None:
            # Preflight failed
            if should_record_result:
                broker.record_result(preflight_result)
            return preflight_result

        # Phase 2: Lease-bound push
        push_result = _perform_push(repo_path, plan)
        if push_result.state != PushState.APPLIED:
            # Push failed
            if should_record_result:
                broker.record_result(push_result)
            return push_result

        # Phase 3: Post-verification
        verify_result = _post_verify(repo_path, plan, push_result)
        if should_record_result:
            broker.record_result(verify_result)
        return verify_result

    except Exception as e:
        result = GitPushResult(
            plan_id=plan.plan_id,
            state=PushState.FAILED,
            remote_name=plan.remote_name,
            remote_branch=plan.remote_branch,
            head_oid=plan.head_oid,
            remote_oid_after=None,
            message=f"Unexpected error: {e}",
            created_at=datetime.now(timezone.utc),
        )
        if should_record_result:
            broker.record_result(result)
        return result


def _validate_local_state(repo_path: Path, plan: GitPushPlan) -> Optional[str]:
    """Validate that local state matches the approved plan.

    Args:
        repo_path: Path to the Git repository.
        plan: The approved push plan.

    Returns:
        Error message if validation fails, None if valid.
    """
    # Check HEAD
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return "Failed to read HEAD"
        current_head = result.stdout.strip()
        if current_head != plan.head_oid:
            return f"HEAD changed from {plan.head_oid[:7]} to {current_head[:7]}"
    except Exception as e:
        return f"Failed to check HEAD: {e}"

    # Check branch
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return "HEAD is no longer on a branch"

        branch_ref = result.stdout.strip()
        if not branch_ref.startswith("refs/heads/"):
            return "HEAD is no longer on a branch"
        current_branch = branch_ref[len("refs/heads/"):]
        if current_branch != plan.local_branch:
            return f"Branch changed from {plan.local_branch} to {current_branch}"
    except Exception as e:
        return f"Failed to check branch: {e}"

    # Check local tracking ref
    try:
        tracking_ref_name = f"refs/remotes/{plan.remote_name}/{plan.remote_branch}"
        result = subprocess.run(
            ["git", "rev-parse", tracking_ref_name],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return f"Remote-tracking ref {tracking_ref_name} was deleted"
        current_remote_oid = result.stdout.strip()
        if current_remote_oid != plan.expected_remote_oid:
            return (
                f"Local tracking ref changed from {plan.expected_remote_oid[:7]} "
                f"to {current_remote_oid[:7]}; fetch may have updated it"
            )
    except Exception as e:
        return f"Failed to check tracking ref: {e}"

    return None


def _remote_preflight(repo_path: Path, plan: GitPushPlan) -> Optional[GitPushResult]:
    """Perform remote preflight to verify remote ref matches expected OID.

    Args:
        repo_path: Path to the repository.
        plan: The push plan.

    Returns:
        GitPushResult with CONFLICT if preflight fails, None if preflight succeeds.
    """
    try:
        # Use ls-remote to query the exact remote branch ref
        env = _build_safe_git_env()

        refspec = f"refs/heads/{plan.remote_branch}"

        result = subprocess.run(
            [
                "git",
                "ls-remote",
                "--heads",
                plan.approved_remote_url,
                refspec,
            ],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            shell=False,
        )

        if result.returncode != 0:
            stderr = result.stderr[:500] if result.stderr else ""
            return GitPushResult(
                plan_id=plan.plan_id,
                state=PushState.FAILED,
                remote_name=plan.remote_name,
                remote_branch=plan.remote_branch,
                head_oid=plan.head_oid,
                remote_oid_after=None,
                message=f"Remote preflight failed: {stderr}",
                created_at=datetime.now(timezone.utc),
            )

        # Parse ls-remote output
        stdout = result.stdout.strip()
        if not stdout:
            return GitPushResult(
                plan_id=plan.plan_id,
                state=PushState.CONFLICT,
                remote_name=plan.remote_name,
                remote_branch=plan.remote_branch,
                head_oid=plan.head_oid,
                remote_oid_after=None,
                message=f"Remote branch {plan.remote_branch} does not exist; v0.8 does not create remote branches",
                created_at=datetime.now(timezone.utc),
            )

        lines = stdout.split("\n")
        if not lines:
            return GitPushResult(
                plan_id=plan.plan_id,
                state=PushState.CONFLICT,
                remote_name=plan.remote_name,
                remote_branch=plan.remote_branch,
                head_oid=plan.head_oid,
                remote_oid_after=None,
                message=f"Remote branch {plan.remote_branch} not found",
                created_at=datetime.now(timezone.utc),
            )

        # Parse: <oid>\t<ref>
        parts = lines[0].split("\t", 1)
        if len(parts) < 1:
            return GitPushResult(
                plan_id=plan.plan_id,
                state=PushState.FAILED,
                remote_name=plan.remote_name,
                remote_branch=plan.remote_branch,
                head_oid=plan.head_oid,
                remote_oid_after=None,
                message="Failed to parse ls-remote output",
                created_at=datetime.now(timezone.utc),
            )

        remote_oid = parts[0].strip()

        if remote_oid != plan.expected_remote_oid:
            return GitPushResult(
                plan_id=plan.plan_id,
                state=PushState.CONFLICT,
                remote_name=plan.remote_name,
                remote_branch=plan.remote_branch,
                head_oid=plan.head_oid,
                remote_oid_after=remote_oid,
                message=(
                    f"Remote branch changed from {plan.expected_remote_oid[:7]} to {remote_oid[:7]}; "
                    f"refresh local tracking ref before preparing a new push"
                ),
                created_at=datetime.now(timezone.utc),
            )

        # Preflight successful
        return None

    except subprocess.TimeoutExpired:
        return GitPushResult(
            plan_id=plan.plan_id,
            state=PushState.FAILED,
            remote_name=plan.remote_name,
            remote_branch=plan.remote_branch,
            head_oid=plan.head_oid,
            remote_oid_after=None,
            message="Remote preflight timed out",
            created_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        return GitPushResult(
            plan_id=plan.plan_id,
            state=PushState.FAILED,
            remote_name=plan.remote_name,
            remote_branch=plan.remote_branch,
            head_oid=plan.head_oid,
            remote_oid_after=None,
            message=f"Remote preflight error: {e}",
            created_at=datetime.now(timezone.utc),
        )


def _perform_push(repo_path: Path, plan: GitPushPlan) -> GitPushResult:
    """Perform the actual Git push with lease.

    Args:
        repo_path: Path to the repository.
        plan: The push plan.

    Returns:
        GitPushResult with APPLIED on success or FAILED/CONFLICT on failure.
    """
    try:
        env = _build_safe_git_env()

        # Build exact refspec with lease
        refspec = f"refs/heads/{plan.local_branch}:refs/heads/{plan.remote_branch}"

        result = subprocess.run(
            [
                "git",
                "push",
                "--porcelain",
                "--no-verify",  # Disable pre-push hooks
                "-c", "push.followTags=false",  # Do not push tags
                "-c", "push.recurseSubmodules=no",  # No submodule recursion
                "-c", "push.gpgSign=false",  # No signed push
                "--force-with-lease=" + f"refs/heads/{plan.remote_branch}:{plan.expected_remote_oid}",
                plan.approved_remote_url,
                refspec,
            ],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            shell=False,
        )

        stdout = result.stdout[:2000] if result.stdout else ""
        stderr = result.stderr[:2000] if result.stderr else ""

        if result.returncode != 0:
            # Check if it's a lease conflict
            if "stale info" in stderr.lower() or "rejected" in stderr.lower():
                return GitPushResult(
                    plan_id=plan.plan_id,
                    state=PushState.CONFLICT,
                    remote_name=plan.remote_name,
                    remote_branch=plan.remote_branch,
                    head_oid=plan.head_oid,
                    remote_oid_after=None,
                    message=f"Push rejected: remote changed during operation",
                    created_at=datetime.now(timezone.utc),
                )

            return GitPushResult(
                plan_id=plan.plan_id,
                state=PushState.FAILED,
                remote_name=plan.remote_name,
                remote_branch=plan.remote_branch,
                head_oid=plan.head_oid,
                remote_oid_after=None,
                message=f"Push failed: {stderr}",
                created_at=datetime.now(timezone.utc),
            )

        # Push command succeeded
        return GitPushResult(
            plan_id=plan.plan_id,
            state=PushState.APPLIED,
            remote_name=plan.remote_name,
            remote_branch=plan.remote_branch,
            head_oid=plan.head_oid,
            remote_oid_after=plan.head_oid,  # Will be verified in post-verification
            message="Push completed",
            created_at=datetime.now(timezone.utc),
        )

    except subprocess.TimeoutExpired:
        return GitPushResult(
            plan_id=plan.plan_id,
            state=PushState.FAILED,
            remote_name=plan.remote_name,
            remote_branch=plan.remote_branch,
            head_oid=plan.head_oid,
            remote_oid_after=None,
            message="Push operation timed out",
            created_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        return GitPushResult(
            plan_id=plan.plan_id,
            state=PushState.FAILED,
            remote_name=plan.remote_name,
            remote_branch=plan.remote_branch,
            head_oid=plan.head_oid,
            remote_oid_after=None,
            message=f"Push error: {e}",
            created_at=datetime.now(timezone.utc),
        )


def _post_verify(
    repo_path: Path, plan: GitPushPlan, push_result: GitPushResult
) -> GitPushResult:
    """Verify remote ref after successful push.

    Args:
        repo_path: Path to the repository.
        plan: The push plan.
        push_result: The result from the push operation.

    Returns:
        Updated GitPushResult with verification status.
    """
    try:
        env = _build_safe_git_env()

        refspec = f"refs/heads/{plan.remote_branch}"

        result = subprocess.run(
            [
                "git",
                "ls-remote",
                "--heads",
                plan.approved_remote_url,
                refspec,
            ],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            shell=False,
        )

        if result.returncode != 0:
            return GitPushResult(
                plan_id=plan.plan_id,
                state=PushState.FAILED,
                remote_name=plan.remote_name,
                remote_branch=plan.remote_branch,
                head_oid=plan.head_oid,
                remote_oid_after=None,
                message="Push command completed but post-verification failed; inspect repository manually",
                created_at=datetime.now(timezone.utc),
            )

        stdout = result.stdout.strip()
        if not stdout:
            return GitPushResult(
                plan_id=plan.plan_id,
                state=PushState.FAILED,
                remote_name=plan.remote_name,
                remote_branch=plan.remote_branch,
                head_oid=plan.head_oid,
                remote_oid_after=None,
                message="Push command completed but remote branch disappeared; inspect repository manually",
                created_at=datetime.now(timezone.utc),
            )

        lines = stdout.split("\n")
        parts = lines[0].split("\t", 1)
        remote_oid = parts[0].strip()

        if remote_oid != plan.head_oid:
            return GitPushResult(
                plan_id=plan.plan_id,
                state=PushState.FAILED,
                remote_name=plan.remote_name,
                remote_branch=plan.remote_branch,
                head_oid=plan.head_oid,
                remote_oid_after=remote_oid,
                message=(
                    f"Push command completed but remote ref verification did not match "
                    f"(expected {plan.head_oid[:7]}, got {remote_oid[:7]}); "
                    f"inspect repository manually"
                ),
                created_at=datetime.now(timezone.utc),
            )

        # Verification successful
        return GitPushResult(
            plan_id=plan.plan_id,
            state=PushState.APPLIED,
            remote_name=plan.remote_name,
            remote_branch=plan.remote_branch,
            head_oid=plan.head_oid,
            remote_oid_after=remote_oid,
            message=f"Successfully pushed to {plan.remote_name}/{plan.remote_branch}",
            created_at=datetime.now(timezone.utc),
        )

    except Exception as e:
        return GitPushResult(
            plan_id=plan.plan_id,
            state=PushState.FAILED,
            remote_name=plan.remote_name,
            remote_branch=plan.remote_branch,
            head_oid=plan.head_oid,
            remote_oid_after=None,
            message=f"Post-verification error: {e}",
            created_at=datetime.now(timezone.utc),
        )


def _build_safe_git_env() -> dict[str, str]:
    """Build a safe environment for Git network operations.

    Returns:
        Environment dict with security hardening.
    """
    env = os.environ.copy()

    # Disable interactive prompts
    env["GIT_TERMINAL_PROMPT"] = "0"

    # Disable pager
    env["GIT_PAGER"] = ""

    return env
