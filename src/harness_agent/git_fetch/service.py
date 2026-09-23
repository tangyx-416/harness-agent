"""Service layer for remote Git fetch operations.

Handles the actual network fetch execution with revalidation, isolated temporary
ref, and atomic tracking ref update.
"""

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .broker import GitFetchBroker
from .models import FetchState, GitFetchPlan, GitFetchResult


def apply_fetch(
    plan: GitFetchPlan,
    repo_path: str,
    broker: GitFetchBroker,
) -> GitFetchResult:
    """Apply an approved fetch plan with revalidation and atomic tracking update.

    This function performs network operations ONLY after the user has approved
    the plan. It implements a multi-phase protocol:
    1. Revalidate local state hasn't changed since approval
    2. Fetch remote branch into isolated temporary Harness ref
    3. Validate fetched commit
    4. Determine fast-forward relationship
    5. Atomically update tracking ref with compare-and-swap
    6. Clean up temporary ref

    Args:
        plan: The approved fetch plan.
        repo_path: Absolute path to the Git repository.
        broker: The broker managing this plan.

    Returns:
        A GitFetchResult with the outcome.
    """
    # Atomically claim the plan for application
    should_record_result = broker.take_for_apply(plan.plan_id)

    if not should_record_result:
        # Plan is not in approved state or already claimed
        state = broker.get_state(plan.plan_id)
        if state in (FetchState.APPLIED, FetchState.CONFLICT, FetchState.FAILED):
            # Return cached result
            existing_result = broker.get_result(plan.plan_id)
            if existing_result:
                return existing_result

        return GitFetchResult(
            plan_id=plan.plan_id,
            state=FetchState.FAILED,
            remote_name=plan.remote_name,
            remote_branch=plan.remote_branch,
            tracking_ref=plan.tracking_ref,
            previous_tracking_oid=plan.expected_tracking_oid,
            observed_remote_oid=None,
            updated_tracking_oid=None,
            changed=False,
            message=f"Fetch plan {plan.plan_id} is not approved or already processed",
            created_at=datetime.now(timezone.utc),
        )

    # We successfully claimed the plan - now perform the fetch
    temp_ref = f"refs/harness-agent/fetch/{plan.plan_id}"

    try:
        repo_path = Path(repo_path).resolve()

        # Phase 1: Revalidate local state hasn't changed
        validation_error = _revalidate_local_state(repo_path, plan)
        if validation_error:
            result = GitFetchResult(
                plan_id=plan.plan_id,
                state=FetchState.CONFLICT,
                remote_name=plan.remote_name,
                remote_branch=plan.remote_branch,
                tracking_ref=plan.tracking_ref,
                previous_tracking_oid=plan.expected_tracking_oid,
                observed_remote_oid=None,
                updated_tracking_oid=None,
                changed=False,
                message=validation_error,
                created_at=datetime.now(timezone.utc),
            )
            broker.record_result(result)
            return result

        # Phase 2: Fetch remote branch into temporary ref
        fetch_result = _perform_fetch(repo_path, plan, temp_ref)
        if fetch_result is not None:
            # Fetch failed
            broker.record_result(fetch_result)
            _cleanup_temp_ref(repo_path, temp_ref)
            return fetch_result

        # Phase 3: Resolve fetched commit
        observed_remote_oid = _resolve_temp_ref(repo_path, temp_ref)
        if observed_remote_oid is None:
            result = GitFetchResult(
                plan_id=plan.plan_id,
                state=FetchState.FAILED,
                remote_name=plan.remote_name,
                remote_branch=plan.remote_branch,
                tracking_ref=plan.tracking_ref,
                previous_tracking_oid=plan.expected_tracking_oid,
                observed_remote_oid=None,
                updated_tracking_oid=None,
                changed=False,
                message=f"Failed to resolve fetched commit in temporary ref {temp_ref}",
                created_at=datetime.now(timezone.utc),
            )
            broker.record_result(result)
            _cleanup_temp_ref(repo_path, temp_ref)
            return result

        # Phase 4: Check if update is needed
        if observed_remote_oid == plan.expected_tracking_oid:
            # No-op fetch: remote is already up-to-date
            result = GitFetchResult(
                plan_id=plan.plan_id,
                state=FetchState.APPLIED,
                remote_name=plan.remote_name,
                remote_branch=plan.remote_branch,
                tracking_ref=plan.tracking_ref,
                previous_tracking_oid=plan.expected_tracking_oid,
                observed_remote_oid=observed_remote_oid,
                updated_tracking_oid=plan.expected_tracking_oid,
                changed=False,
                message=f"Fetch complete; {plan.tracking_ref} already up-to-date at {observed_remote_oid[:7]}",
                created_at=datetime.now(timezone.utc),
            )
            broker.record_result(result)
            _cleanup_temp_ref(repo_path, temp_ref)
            return result

        # Phase 5: Verify fast-forward relationship
        ff_check = _check_fast_forward(
            repo_path, plan.expected_tracking_oid, observed_remote_oid
        )
        if ff_check is not None:
            # Not a fast-forward: conflict
            result = GitFetchResult(
                plan_id=plan.plan_id,
                state=FetchState.CONFLICT,
                remote_name=plan.remote_name,
                remote_branch=plan.remote_branch,
                tracking_ref=plan.tracking_ref,
                previous_tracking_oid=plan.expected_tracking_oid,
                observed_remote_oid=observed_remote_oid,
                updated_tracking_oid=None,
                changed=False,
                message=ff_check,
                created_at=datetime.now(timezone.utc),
            )
            broker.record_result(result)
            _cleanup_temp_ref(repo_path, temp_ref)
            return result

        # Phase 6: Atomically update tracking ref with CAS
        cas_result = _atomic_tracking_update(
            repo_path, plan.tracking_ref, plan.expected_tracking_oid, observed_remote_oid
        )
        if cas_result is not None:
            # CAS failed: tracking ref changed concurrently
            result = GitFetchResult(
                plan_id=plan.plan_id,
                state=FetchState.CONFLICT,
                remote_name=plan.remote_name,
                remote_branch=plan.remote_branch,
                tracking_ref=plan.tracking_ref,
                previous_tracking_oid=plan.expected_tracking_oid,
                observed_remote_oid=observed_remote_oid,
                updated_tracking_oid=None,
                changed=False,
                message=cas_result,
                created_at=datetime.now(timezone.utc),
            )
            broker.record_result(result)
            _cleanup_temp_ref(repo_path, temp_ref)
            return result

        # Phase 7: Cleanup temporary ref
        _cleanup_temp_ref(repo_path, temp_ref)

        # Success: tracking ref updated
        result = GitFetchResult(
            plan_id=plan.plan_id,
            state=FetchState.APPLIED,
            remote_name=plan.remote_name,
            remote_branch=plan.remote_branch,
            tracking_ref=plan.tracking_ref,
            previous_tracking_oid=plan.expected_tracking_oid,
            observed_remote_oid=observed_remote_oid,
            updated_tracking_oid=observed_remote_oid,
            changed=True,
            message=(
                f"Successfully fetched {plan.remote_name}/{plan.remote_branch}; "
                f"{plan.tracking_ref} updated {plan.expected_tracking_oid[:7]} → {observed_remote_oid[:7]}"
            ),
            created_at=datetime.now(timezone.utc),
        )
        broker.record_result(result)
        return result

    except Exception as e:
        result = GitFetchResult(
            plan_id=plan.plan_id,
            state=FetchState.FAILED,
            remote_name=plan.remote_name,
            remote_branch=plan.remote_branch,
            tracking_ref=plan.tracking_ref,
            previous_tracking_oid=plan.expected_tracking_oid,
            observed_remote_oid=None,
            updated_tracking_oid=None,
            changed=False,
            message=f"Unexpected error: {e}",
            created_at=datetime.now(timezone.utc),
        )
        broker.record_result(result)
        _cleanup_temp_ref(repo_path, temp_ref)
        return result


def _normalize_url_for_comparison(url: str) -> str:
    """Normalize URL for comparison to handle platform-specific Git behavior.

    Git on Windows may normalize file:// URLs by removing the scheme.
    For testing purposes only, we normalize both sides of the comparison.
    Production only uses HTTPS URLs which don't have this issue.

    Args:
        url: URL to normalize.

    Returns:
        Normalized URL string.
    """
    # Remove file:// prefix if present (Windows Git normalization)
    if url.startswith("file://"):
        return url[7:]  # Remove 'file://'
    return url


def _revalidate_local_state(repo_path: Path, plan: GitFetchPlan) -> Optional[str]:
    """Revalidate that local state matches the approved plan.

    Args:
        repo_path: Path to the Git repository.
        plan: The approved fetch plan.

    Returns:
        Error message if validation fails, None if valid.
    """
    # Check current branch
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return "Failed to read current branch"
        current_branch = result.stdout.strip()
        if current_branch != plan.local_branch:
            return f"Branch changed from {plan.local_branch} to {current_branch}"
    except Exception as e:
        return f"Failed to check branch: {e}"

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
        if current_head != plan.local_head_oid:
            return f"HEAD changed from {plan.local_head_oid[:7]} to {current_head[:7]}"
    except Exception as e:
        return f"Failed to check HEAD: {e}"

    # Check upstream configuration
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", f"{plan.local_branch}@{{upstream}}"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return f"Branch {plan.local_branch} upstream configuration removed"
        upstream = result.stdout.strip()
        expected_upstream = f"{plan.remote_name}/{plan.remote_branch}"
        if upstream != expected_upstream:
            return f"Upstream changed from {expected_upstream} to {upstream}"
    except Exception as e:
        return f"Failed to check upstream: {e}"

    # Check remote URL
    try:
        result = subprocess.run(
            ["git", "config", "--get", f"remote.{plan.remote_name}.url"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return f"Remote {plan.remote_name} URL removed"
        current_url = result.stdout.strip()

        # Normalize both URLs for comparison (handles file:// on Windows)
        normalized_current = _normalize_url_for_comparison(current_url)
        normalized_approved = _normalize_url_for_comparison(plan.approved_remote_url)

        if normalized_current != normalized_approved:
            return f"Remote URL changed from {plan.approved_remote_url!r} to {current_url!r}"
    except Exception as e:
        return f"Failed to check remote URL: {e}"

    # Check tracking ref still exists and matches expected OID
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", plan.tracking_ref],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return f"Tracking ref {plan.tracking_ref} was deleted"
        current_tracking_oid = result.stdout.strip()
        if current_tracking_oid != plan.expected_tracking_oid:
            return (
                f"Tracking ref changed from {plan.expected_tracking_oid[:7]} "
                f"to {current_tracking_oid[:7]}; another fetch may have occurred"
            )
    except Exception as e:
        return f"Failed to check tracking ref: {e}"

    return None


def _perform_fetch(
    repo_path: Path, plan: GitFetchPlan, temp_ref: str
) -> Optional[GitFetchResult]:
    """Perform the actual Git fetch into a temporary ref.

    Args:
        repo_path: Path to the repository.
        plan: The fetch plan.
        temp_ref: Temporary Harness-owned ref for the fetch.

    Returns:
        GitFetchResult with FAILED if fetch fails, None if fetch succeeds.
    """
    try:
        env = _build_safe_git_env()

        # Build exact refspec: fetch remote branch into temporary ref
        # Format: refs/heads/<remote-branch>:refs/harness-agent/fetch/<plan-id>
        refspec = f"refs/heads/{plan.remote_branch}:{temp_ref}"

        # Build argv with -c options BEFORE fetch subcommand
        result = subprocess.run(
            [
                "git",
                "-c", "fetch.recurseSubmodules=false",  # Global config override
                "-c", "submodule.recurse=false",  # Global config override
                "-c", "maintenance.auto=false",  # Disable auto-maintenance
                "fetch",
                "--no-tags",  # Do not fetch tags
                "--no-recurse-submodules",  # No submodule recursion
                "--no-write-fetch-head",  # Do not update FETCH_HEAD
                plan.approved_remote_url,  # Explicit HTTPS URL
                refspec,
            ],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            shell=False,
        )

        if result.returncode != 0:
            stderr = result.stderr[:1000] if result.stderr else ""
            return GitFetchResult(
                plan_id=plan.plan_id,
                state=FetchState.FAILED,
                remote_name=plan.remote_name,
                remote_branch=plan.remote_branch,
                tracking_ref=plan.tracking_ref,
                previous_tracking_oid=plan.expected_tracking_oid,
                observed_remote_oid=None,
                updated_tracking_oid=None,
                changed=False,
                message=f"Fetch failed: {stderr}",
                created_at=datetime.now(timezone.utc),
            )

        # Fetch succeeded
        return None

    except subprocess.TimeoutExpired:
        return GitFetchResult(
            plan_id=plan.plan_id,
            state=FetchState.FAILED,
            remote_name=plan.remote_name,
            remote_branch=plan.remote_branch,
            tracking_ref=plan.tracking_ref,
            previous_tracking_oid=plan.expected_tracking_oid,
            observed_remote_oid=None,
            updated_tracking_oid=None,
            changed=False,
            message="Fetch operation timed out",
            created_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        return GitFetchResult(
            plan_id=plan.plan_id,
            state=FetchState.FAILED,
            remote_name=plan.remote_name,
            remote_branch=plan.remote_branch,
            tracking_ref=plan.tracking_ref,
            previous_tracking_oid=plan.expected_tracking_oid,
            observed_remote_oid=None,
            updated_tracking_oid=None,
            changed=False,
            message=f"Fetch error: {e}",
            created_at=datetime.now(timezone.utc),
        )


def _resolve_temp_ref(repo_path: Path, temp_ref: str) -> Optional[str]:
    """Resolve the temporary fetch ref to a commit OID.

    Args:
        repo_path: Path to the repository.
        temp_ref: The temporary ref name.

    Returns:
        The commit OID if successful, None otherwise.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{temp_ref}^{{commit}}"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        oid = result.stdout.strip()
        # Validate it's a valid OID (40 hex chars for SHA-1)
        if len(oid) not in (40, 64) or not all(c in "0123456789abcdef" for c in oid):
            return None
        return oid
    except Exception:
        return None


def _check_fast_forward(
    repo_path: Path, old_oid: str, new_oid: str
) -> Optional[str]:
    """Check if new_oid is a fast-forward from old_oid.

    Args:
        repo_path: Path to the repository.
        old_oid: The expected old tracking ref OID.
        new_oid: The observed remote OID.

    Returns:
        Error message if not a fast-forward, None if fast-forward is valid.
    """
    try:
        # Get merge-base
        result = subprocess.run(
            ["git", "merge-base", old_oid, new_oid],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return (
                f"Cannot determine relationship between {old_oid[:7]} and {new_oid[:7]}; "
                f"histories may be unrelated"
            )

        merge_base = result.stdout.strip()

        if merge_base == old_oid:
            # old is ancestor of new: valid fast-forward
            return None
        elif merge_base == new_oid:
            # new is ancestor of old: remote moved backwards
            return (
                f"Remote branch moved backwards from {old_oid[:7]} to {new_oid[:7]}; "
                f"v0.9 does not accept remote history rewrites"
            )
        else:
            # Diverged histories
            return (
                f"Remote branch diverged from local tracking ref "
                f"(local: {old_oid[:7]}, remote: {new_oid[:7]}); "
                f"v0.9 does not accept non-fast-forward updates"
            )

    except Exception as e:
        return f"Failed to check fast-forward relationship: {e}"


def _atomic_tracking_update(
    repo_path: Path, tracking_ref: str, expected_old: str, new: str
) -> Optional[str]:
    """Atomically update tracking ref with compare-and-swap.

    Args:
        repo_path: Path to the repository.
        tracking_ref: The tracking ref to update.
        expected_old: Expected current value.
        new: New value to set.

    Returns:
        Error message if CAS fails, None if update succeeds.
    """
    try:
        # Use git update-ref with compare-and-swap semantics
        # Format: git update-ref <ref> <new> <expected-old>
        result = subprocess.run(
            ["git", "update-ref", tracking_ref, new, expected_old],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            stderr = result.stderr[:500] if result.stderr else ""
            return (
                f"Failed to update {tracking_ref}: tracking ref changed concurrently. "
                f"Expected {expected_old[:7]} but ref has different value. {stderr}"
            )

        return None

    except Exception as e:
        return f"Failed to atomically update tracking ref: {e}"


def _cleanup_temp_ref(repo_path: Path, temp_ref: str) -> None:
    """Clean up the temporary fetch ref.

    Args:
        repo_path: Path to the repository.
        temp_ref: The temporary ref to delete.
    """
    try:
        subprocess.run(
            ["git", "update-ref", "-d", temp_ref],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        # Cleanup failure is logged but not fatal
        pass


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
