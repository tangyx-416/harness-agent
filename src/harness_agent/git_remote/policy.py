"""Policy validation for remote Git push operations.

Validates push preconditions and generates immutable push plans using subprocess
Git commands (no pygit2 dependency).
"""

import hashlib
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .models import GitPushPlan, PushCommit

# Maximum outgoing commits allowed in a single push
MAX_PUSH_COMMITS = 20

# Maximum diff size for approval preview (100KB)
MAX_PUSH_APPROVAL_DIFF_CHARS = 100000


class RemotePushPolicyError(Exception):
    """Policy validation failure for remote push."""

    pass


def validate_and_prepare_push(
    repo_path: str,
    summary: str,
    plan_id: str,
) -> GitPushPlan:
    """Validate push preconditions and create an immutable push plan.

    This function performs ZERO network operations. It uses only local
    Git state including the local remote-tracking ref.

    Args:
        repo_path: Absolute path to the Git repository.
        summary: User-provided summary of the push operation.
        plan_id: Unique plan identifier.

    Returns:
        An immutable GitPushPlan ready for user approval.

    Raises:
        RemotePushPolicyError: If any policy validation fails.
    """
    repo_path = Path(repo_path).resolve()
    if not repo_path.is_dir():
        raise RemotePushPolicyError(f"Repository path does not exist: {repo_path}")

    if not (repo_path / ".git").exists():
        raise RemotePushPolicyError(f"Not a Git repository: {repo_path}")

    # 1. Check if HEAD is detached
    result = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RemotePushPolicyError("HEAD is detached; cannot determine push destination")

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
        raise RemotePushPolicyError("Cannot determine current branch")

    # 3. Get HEAD OID
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=True,
    )
    head_oid = result.stdout.strip()

    # 4. Get HEAD tree OID
    result = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=True,
    )
    head_tree_oid = result.stdout.strip()

    # 5. Get upstream branch
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", f"{local_branch}@{{upstream}}"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RemotePushPolicyError(
            f"Branch {local_branch} has no configured upstream; "
            f"cannot determine push destination"
        )

    upstream_name = result.stdout.strip()

    # 6. Parse upstream: <remote>/<branch>
    if "/" not in upstream_name:
        raise RemotePushPolicyError(f"Cannot parse upstream {upstream_name}")

    parts = upstream_name.split("/", 1)
    remote_name = parts[0]
    remote_branch = parts[1]

    # 7. Get remote URL
    result = subprocess.run(
        ["git", "remote", "get-url", remote_name],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RemotePushPolicyError(f"Remote {remote_name} does not exist")

    remote_url = result.stdout.strip()
    if not remote_url:
        raise RemotePushPolicyError(f"Remote {remote_name} has no URL")

    # 8. Validate remote URL (HTTPS only)
    remote_url = _validate_remote_url(remote_url)
    remote_host = _extract_remote_host(remote_url)

    # 8b. Reject any configured URL redirection that could rewrite the approved
    # destination. `git push <explicit-url>` still applies url.*.insteadOf,
    # url.*.pushInsteadOf, and remote.<name>.pushurl rewrites, so an approved
    # github.com URL could be silently redirected to another host. v0.8 refuses
    # to prepare a push while any such rule can affect this destination, so that
    # APPROVED DESTINATION == ACTUAL PUSH DESTINATION always holds.
    _reject_url_redirection(repo_path, remote_name, remote_url)

    # 9. Get expected remote OID from local tracking ref
    tracking_ref = f"refs/remotes/{remote_name}/{remote_branch}"
    result = subprocess.run(
        ["git", "rev-parse", tracking_ref],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RemotePushPolicyError(
            f"Remote-tracking ref {tracking_ref} does not exist locally"
        )

    expected_remote_oid = result.stdout.strip()

    # 10. Get expected tree OID
    result = subprocess.run(
        ["git", "rev-parse", f"{expected_remote_oid}^{{tree}}"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=True,
    )
    expected_tree_oid = result.stdout.strip()

    # 11. Require local ahead of remote
    if head_oid == expected_remote_oid:
        raise RemotePushPolicyError("Local branch is up-to-date; nothing to push")

    # 12. Verify fast-forward relationship
    result = subprocess.run(
        ["git", "merge-base", expected_remote_oid, head_oid],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RemotePushPolicyError(
            "Local and remote branches have unrelated history"
        )

    merge_base_oid = result.stdout.strip()

    if merge_base_oid != expected_remote_oid:
        # Remote is not an ancestor of local HEAD
        if merge_base_oid == head_oid:
            raise RemotePushPolicyError(
                "Local branch is behind remote; cannot push"
            )
        else:
            raise RemotePushPolicyError(
                "Local and remote branches have diverged; cannot push"
            )

    # 13. Require clean working tree (no tracked unstaged/staged changes)
    # Check unstaged changes
    result = subprocess.run(
        ["git", "diff", "--quiet"],
        cwd=str(repo_path),
    )
    if result.returncode != 0:
        raise RemotePushPolicyError(
            "Working tree has uncommitted changes; "
            "commit or stash them before pushing"
        )

    # Check staged changes
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(repo_path),
    )
    if result.returncode != 0:
        raise RemotePushPolicyError(
            "Working tree has uncommitted changes; "
            "commit or stash them before pushing"
        )

    # 14. Generate outgoing commit list
    result = subprocess.run(
        ["git", "rev-list", f"{expected_remote_oid}..{head_oid}"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=True,
    )

    outgoing_oids = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]

    if not outgoing_oids:
        raise RemotePushPolicyError("No outgoing commits found")

    if len(outgoing_oids) > MAX_PUSH_COMMITS:
        raise RemotePushPolicyError(
            f"Too many outgoing commits ({len(outgoing_oids)} > {MAX_PUSH_COMMITS}) "
            f"for a single v0.8 approval"
        )

    # 15. Check for merge commits in outgoing range
    for oid in outgoing_oids:
        result = subprocess.run(
            ["git", "rev-list", "--no-walk", "--parents", oid],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=True,
        )
        parts = result.stdout.strip().split()
        if len(parts) > 2:  # commit has more than one parent
            raise RemotePushPolicyError(
                f"Outgoing range contains merge commit {oid[:7]}; "
                f"v0.8 does not support pushing merge commits"
            )

    # 16. Build outgoing commit objects with subject
    outgoing_commits = []
    for oid in reversed(outgoing_oids):  # Reverse to show oldest first
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s", oid],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=True,
        )
        subject = result.stdout.strip()[:72]

        outgoing_commits.append(
            PushCommit(
                oid=oid,
                short_oid=oid[:7],
                subject=subject,
            )
        )

    # 17. Generate complete diff
    result = subprocess.run(
        ["git", "diff", expected_remote_oid, head_oid],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=True,
    )

    diff_text = result.stdout

    if len(diff_text) > MAX_PUSH_APPROVAL_DIFF_CHARS:
        raise RemotePushPolicyError(
            f"Push diff is too large ({len(diff_text)} > {MAX_PUSH_APPROVAL_DIFF_CHARS} chars); "
            f"v0.8 cannot approve such large changes"
        )

    diff_sha256 = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()

    # 18. Create immutable plan
    return GitPushPlan(
        plan_id=plan_id,
        kind="push",
        summary=summary,
        local_branch=local_branch,
        head_oid=head_oid,
        head_tree_oid=head_tree_oid,
        remote_name=remote_name,
        remote_branch=remote_branch,
        approved_remote_url=remote_url,
        remote_host=remote_host,
        expected_remote_oid=expected_remote_oid,
        expected_tree_oid=expected_tree_oid,
        outgoing_commits=tuple(outgoing_commits),
        commit_count=len(outgoing_commits),
        diff=diff_text,
        diff_sha256=diff_sha256,
        created_at=datetime.now(timezone.utc),
    )


def _validate_remote_url(url: str) -> str:
    """Validate and normalize remote URL.

    Args:
        url: The remote URL to validate.

    Returns:
        The validated URL.

    Raises:
        RemotePushPolicyError: If the URL is invalid or unsupported.
    """
    url = url.strip()

    # Reject SSH and other non-HTTPS schemes
    if url.startswith("ssh://") or url.startswith("git@") or url.startswith("git://"):
        raise RemotePushPolicyError(
            f"Only HTTPS remotes are supported in v0.8; URL {url!r} uses unsupported scheme"
        )
    if url.startswith("file://") or url.startswith("ext::"):
        raise RemotePushPolicyError(f"URL scheme not supported: {url!r}")

    # Require HTTPS
    if not url.startswith("https://"):
        raise RemotePushPolicyError(
            f"Only HTTPS remotes are supported in v0.8; URL {url!r} is not HTTPS"
        )

    # Parse URL
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise RemotePushPolicyError(f"Invalid URL {url!r}: {e}")

    # Reject embedded credentials
    if parsed.username or parsed.password:
        raise RemotePushPolicyError(
            f"Remote URL must not contain embedded credentials: {url!r}"
        )

    # Reject query string and fragment
    if parsed.query:
        raise RemotePushPolicyError(f"Remote URL must not contain query string: {url!r}")
    if parsed.fragment:
        raise RemotePushPolicyError(f"Remote URL must not contain fragment: {url!r}")

    # Validate hostname exists
    if not parsed.hostname:
        raise RemotePushPolicyError(f"Remote URL has no hostname: {url!r}")

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

    `git push <explicit-url>` still honours these rewrite rules:
      - url.<base>.insteadOf       (rewrites all URLs, incl. explicit fetch+push)
      - url.<base>.pushInsteadOf   (rewrites push URLs)
      - remote.<name>.pushurl      (overrides the push destination for the remote)

    Any of these can silently send an approved github.com push to another host.
    v0.8 does not attempt to neutralize them at push time (insteadOf is
    multi-valued and cannot be reliably cleared via -c); instead it refuses to
    prepare a push whenever such a rule could rewrite the approved URL.

    Args:
        repo_path: Path to the Git repository.
        remote_name: The configured remote name (e.g. "origin").
        approved_url: The validated HTTPS URL the user will approve.

    Raises:
        RemotePushPolicyError: If any redirection rule affects this destination.
    """
    # Authoritative check: compare the RAW configured URL against the EFFECTIVE
    # push URL that Git resolves. `git config --get remote.<name>.url` returns the
    # un-rewritten value, while `git remote get-url --push` applies every
    # url.*.insteadOf / url.*.pushInsteadOf / remote.<name>.pushurl rule. If they
    # differ, some rule silently redirects the destination and we must refuse.
    # This is ground truth for where the push would land, so it catches all three
    # redirection vectors in a single comparison regardless of prefix form.
    raw = subprocess.run(
        ["git", "config", "--get", f"remote.{remote_name}.url"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    push_effective = subprocess.run(
        ["git", "remote", "get-url", "--push", remote_name],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    raw_url = raw.stdout.strip() if raw.returncode == 0 else ""
    effective_push_url = (
        push_effective.stdout.strip() if push_effective.returncode == 0 else ""
    )
    if raw_url and effective_push_url and raw_url != effective_push_url:
        raise RemotePushPolicyError(
            f"Configured Git URL redirection detected for remote {remote_name!r}: "
            f"the raw URL {raw_url!r} resolves to a different push destination "
            f"{effective_push_url!r}. v0.8 refuses to push because the actual push "
            f"destination would differ from what the user approved "
            f"(url.*.insteadOf / url.*.pushInsteadOf / remote.<name>.pushurl)."
        )

    # Defense in depth: remote.<name>.pushurl is a separate push destination even
    # when it happens to resolve identically. Refuse if one is configured at all.
    result = subprocess.run(
        ["git", "config", "--get-all", f"remote.{remote_name}.pushurl"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        raise RemotePushPolicyError(
            f"Remote {remote_name!r} has a configured pushurl; v0.8 refuses to push "
            f"while a separate push destination is configured, because it could "
            f"redirect the approved destination {approved_url!r}"
        )
