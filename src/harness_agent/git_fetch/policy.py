"""Policy validation for remote Git fetch operations.

Validates fetch preconditions and generates immutable fetch plans using subprocess
Git commands (no pygit2 dependency). ZERO NETWORK operations during preparation.
"""

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .models import GitFetchPlan


class RemoteFetchPolicyError(Exception):
    """Policy validation failure for remote fetch."""

    pass


def validate_and_prepare_fetch(
    repo_path: str,
    summary: str,
    plan_id: str,
) -> GitFetchPlan:
    """Validate fetch preconditions and create an immutable fetch plan.

    This function performs ZERO network operations. It uses only local
    Git state including configuration and existing refs.

    Core security invariant:
    PREPARE FETCH == LOCAL-ONLY PROPOSAL

    Args:
        repo_path: Absolute path to the Git repository.
        summary: User-provided summary of the fetch operation.
        plan_id: Unique plan identifier.

    Returns:
        An immutable GitFetchPlan ready for user approval.

    Raises:
        RemoteFetchPolicyError: If any policy validation fails.
    """
    repo_path = Path(repo_path).resolve()
    if not repo_path.is_dir():
        raise RemoteFetchPolicyError(f"Repository path does not exist: {repo_path}")

    if not (repo_path / ".git").exists():
        raise RemoteFetchPolicyError(f"Not a Git repository: {repo_path}")

    # 1. Check if HEAD is detached
    result = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RemoteFetchPolicyError(
            "HEAD is detached; v0.9 fetch requires an attached HEAD on a branch"
        )

    # 2. Get current branch
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=True,
    )
    local_branch = result.stdout.strip()
    if not local_branch:
        raise RemoteFetchPolicyError("Cannot determine current branch")

    # 3. Get HEAD OID
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=True,
    )
    local_head_oid = result.stdout.strip()

    # 4. Get upstream branch
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", f"{local_branch}@{{upstream}}"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RemoteFetchPolicyError(
            f"Branch {local_branch} has no configured upstream; "
            f"v0.9 fetch requires an existing configured upstream"
        )

    upstream_name = result.stdout.strip()

    # 5. Parse upstream: <remote>/<branch>
    if "/" not in upstream_name:
        raise RemoteFetchPolicyError(
            f"Cannot parse upstream {upstream_name}; expected <remote>/<branch> format"
        )

    parts = upstream_name.split("/", 1)
    remote_name = parts[0]
    remote_branch = parts[1]

    # 6. Verify the upstream is a remote-tracking branch
    tracking_ref = f"refs/remotes/{remote_name}/{remote_branch}"
    result = subprocess.run(
        ["git", "rev-parse", "--verify", tracking_ref],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RemoteFetchPolicyError(
            f"Remote-tracking ref {tracking_ref} does not exist locally; "
            f"v0.9 does not create new tracking refs"
        )

    expected_tracking_oid = result.stdout.strip()

    # 7. Get remote URL (raw configured URL)
    result = subprocess.run(
        ["git", "config", "--get", f"remote.{remote_name}.url"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RemoteFetchPolicyError(f"Remote {remote_name} has no configured URL")

    raw_remote_url = result.stdout.strip()
    if not raw_remote_url:
        raise RemoteFetchPolicyError(f"Remote {remote_name} has empty URL")

    # 8. Validate remote URL (HTTPS only)
    remote_url = _validate_remote_url(raw_remote_url)
    remote_host = _extract_remote_host(remote_url)

    # 9. Reject any configured URL redirection that could rewrite the approved
    # fetch destination. v0.9 applies the same security model as v0.8 push:
    # url.*.insteadOf can redirect even explicit fetch URLs, so we refuse to
    # prepare when any such rule affects this destination.
    _reject_url_redirection(repo_path, remote_name, remote_url)

    # 10. Check for unsupported repository operation states
    _check_repository_state(repo_path)

    # 11. Create immutable plan
    return GitFetchPlan(
        plan_id=plan_id,
        kind="fetch",
        summary=summary,
        local_branch=local_branch,
        local_head_oid=local_head_oid,
        remote_name=remote_name,
        remote_branch=remote_branch,
        tracking_ref=tracking_ref,
        expected_tracking_oid=expected_tracking_oid,
        approved_remote_url=remote_url,
        remote_host=remote_host,
        created_at=datetime.now(timezone.utc),
    )


def _validate_remote_url(url: str) -> str:
    """Validate and normalize remote URL.

    v0.9 supports HTTPS only.

    Args:
        url: The remote URL to validate.

    Returns:
        The validated URL.

    Raises:
        RemoteFetchPolicyError: If the URL is invalid or unsupported.
    """
    url = url.strip()

    # Reject SSH and other non-HTTPS schemes
    if url.startswith("ssh://") or url.startswith("git@") or url.startswith("git://"):
        raise RemoteFetchPolicyError(
            f"Only HTTPS remotes are supported in v0.9; URL {url!r} uses unsupported scheme"
        )
    if url.startswith("file://") or url.startswith("ext::"):
        raise RemoteFetchPolicyError(f"URL scheme not supported: {url!r}")
    if url.startswith("http://"):
        raise RemoteFetchPolicyError(
            f"Only HTTPS remotes are supported in v0.9; insecure HTTP not allowed: {url!r}"
        )

    # Require HTTPS
    if not url.startswith("https://"):
        raise RemoteFetchPolicyError(
            f"Only HTTPS remotes are supported in v0.9; URL {url!r} is not HTTPS"
        )

    # Parse URL
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise RemoteFetchPolicyError(f"Invalid URL {url!r}: {e}")

    # Reject embedded credentials
    if parsed.username or parsed.password:
        raise RemoteFetchPolicyError(
            f"Remote URL must not contain embedded credentials: {url!r}"
        )

    # Reject query string and fragment
    if parsed.query:
        raise RemoteFetchPolicyError(f"Remote URL must not contain query string: {url!r}")
    if parsed.fragment:
        raise RemoteFetchPolicyError(f"Remote URL must not contain fragment: {url!r}")

    # Validate hostname exists
    if not parsed.hostname:
        raise RemoteFetchPolicyError(f"Remote URL has no hostname: {url!r}")

    return url


def _extract_remote_host(url: str) -> str:
    """Extract hostname from remote URL.

    Args:
        url: The validated HTTPS URL.

    Returns:
        The hostname.
    """
    parsed = urlparse(url)
    return parsed.hostname or ""


def _reject_url_redirection(repo_path: Path, remote_name: str, approved_url: str) -> None:
    """Reject configured Git URL redirection affecting the approved destination.

    `git fetch <explicit-url>` still honours url.*.insteadOf rewrites, which can
    silently send an approved github.com fetch to another host. v0.9 does not
    attempt to neutralize them at fetch time; instead it refuses to prepare a
    fetch whenever such a rule could rewrite the approved URL.

    For fetch specifically, we check:
      - url.<base>.insteadOf       (rewrites all URLs, incl. explicit fetch)
      - raw configured URL vs effective fetch URL

    Unlike push, there is no fetchInsteadOf or remote.<name>.fetchurl, so the
    attack surface is smaller, but url.*.insteadOf still applies.

    Args:
        repo_path: Path to the Git repository.
        remote_name: The configured remote name (e.g. "origin").
        approved_url: The validated HTTPS URL the user will approve.

    Raises:
        RemoteFetchPolicyError: If any redirection rule affects this destination.
    """
    # Authoritative check: compare the RAW configured URL against the EFFECTIVE
    # fetch URL that Git resolves. For fetch, `git remote get-url` (without --push)
    # returns the effective fetch URL after applying url.*.insteadOf.
    raw = subprocess.run(
        ["git", "config", "--get", f"remote.{remote_name}.url"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    fetch_effective = subprocess.run(
        ["git", "remote", "get-url", remote_name],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    raw_url = raw.stdout.strip() if raw.returncode == 0 else ""
    effective_fetch_url = (
        fetch_effective.stdout.strip() if fetch_effective.returncode == 0 else ""
    )
    if raw_url and effective_fetch_url and raw_url != effective_fetch_url:
        raise RemoteFetchPolicyError(
            f"Configured Git URL redirection detected for remote {remote_name!r}: "
            f"the raw URL {raw_url!r} resolves to a different fetch destination "
            f"{effective_fetch_url!r}. v0.9 refuses to fetch because the actual fetch "
            f"destination would differ from what the user approved (url.*.insteadOf)."
        )

    # Check if there are any configured url.*.insteadOf rules at all that could
    # match this URL. This is defense in depth.
    result = subprocess.run(
        ["git", "config", "--get-regexp", "^url\\..*\\.insteadOf$"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        # There are insteadOf rules configured. Check if any could match our URL.
        # If the effective URL differs from raw, we already rejected above.
        # This is an additional check to be extra cautious.
        lines = result.stdout.strip().split("\n")
        for line in lines:
            # Format: url.<base>.insteadOf <prefix>
            parts = line.split(None, 1)
            if len(parts) == 2:
                prefix = parts[1]
                # Check if our approved URL could be affected
                # (this is conservative; we already checked effective != raw above)
                if approved_url.startswith(prefix) or prefix in approved_url:
                    # We already caught this in the effective != raw check above
                    # But if somehow we missed it, reject here
                    if raw_url != effective_fetch_url:
                        raise RemoteFetchPolicyError(
                            f"URL rewrite rule detected that could affect fetch destination: "
                            f"{line!r}"
                        )


def _check_repository_state(repo_path: Path) -> None:
    """Check for unsupported in-progress repository operations.

    v0.9 rejects fetch during merge, rebase, cherry-pick, revert, or other
    operations that make repository state ambiguous.

    Args:
        repo_path: Path to the Git repository.

    Raises:
        RemoteFetchPolicyError: If an unsupported operation is in progress.
    """
    git_dir = repo_path / ".git"

    # Check for merge in progress
    if (git_dir / "MERGE_HEAD").exists():
        raise RemoteFetchPolicyError(
            "Merge in progress; v0.9 does not fetch during merge operations"
        )

    # Check for rebase in progress
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        raise RemoteFetchPolicyError(
            "Rebase in progress; v0.9 does not fetch during rebase operations"
        )

    # Check for cherry-pick in progress
    if (git_dir / "CHERRY_PICK_HEAD").exists():
        raise RemoteFetchPolicyError(
            "Cherry-pick in progress; v0.9 does not fetch during cherry-pick operations"
        )

    # Check for revert in progress
    if (git_dir / "REVERT_HEAD").exists():
        raise RemoteFetchPolicyError(
            "Revert in progress; v0.9 does not fetch during revert operations"
        )

    # Check for bisect in progress
    if (git_dir / "BISECT_LOG").exists():
        raise RemoteFetchPolicyError(
            "Bisect in progress; v0.9 does not fetch during bisect operations"
        )
