#!/usr/bin/env python
"""Read-only Git Awareness smoke tests against the current repository.

Runs WITHOUT any LLM API access. Verifies that the v0.4.0 Git tools
work on the real Harness Agent repository and remain strictly
read-only (nothing in the repository changes during the smoke).
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from harness_agent.execution import ExecutionBroker  # noqa: E402
from harness_agent.git_awareness import (  # noqa: E402
    build_git_environment,
    validate_git_pathspec,
)
from harness_agent.tools.execution_tools import prepare_command_core  # noqa: E402
from harness_agent.tools.git_tools import (  # noqa: E402
    git_branches_core,
    git_diff_core,
    git_log_core,
    git_status_core,
)

REPO_ROOT = Path(__file__).parent.parent.resolve()

PASSED = []
FAILED = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}  {detail}")


def repo_state_snapshot() -> bytes:
    result = subprocess.run(
        ["git", "status", "--porcelain=v2", "-z"],
        cwd=str(REPO_ROOT), capture_output=True,
    )
    return result.stdout


def main() -> int:
    print("=" * 64)
    print("Harness Agent v0.4.0 - Git Awareness Smoke (read-only, no LLM)")
    print("=" * 64)

    before = repo_state_snapshot()

    # -- git_status ---------------------------------------------------------
    print("\n[1] git_status on the real repository")
    status = git_status_core(root=REPO_ROOT)
    check("1.1 status ok", status.get("ok") is True)
    check("1.2 branch is main", status.get("branch") == "main", str(status.get("branch")))
    untracked = [entry["path"] for entry in status.get("untracked", [])]
    instruction_files = [p for p in untracked if p.startswith("指令")]
    check(
        "1.3 untracked 指令*.txt visible (reported only)",
        len(instruction_files) > 0,
        ", ".join(instruction_files),
    )
    print(f"        branch={status.get('branch')} clean={status.get('clean')} "
          f"upstream={status.get('upstream')} ahead={status.get('ahead')} behind={status.get('behind')}")
    print(f"        untracked_total={len(untracked)}")

    # -- git_log ------------------------------------------------------------
    print("\n[2] git_log(limit=5)")
    log = git_log_core(5, root=REPO_ROOT)
    check("2.1 log ok", log.get("ok") is True)
    check("2.2 has commits", len(log.get("commits", [])) >= 1)
    if log.get("commits"):
        newest = log["commits"][0]
        print(f"        newest: {newest['short_hash']} {newest['subject']}")
        check("2.3 ISO date format", len(newest["date"]) >= 19)

    # -- git_branches ---------------------------------------------------------
    print("\n[3] git_branches")
    branches = git_branches_core(root=REPO_ROOT)
    check("3.1 branches ok", branches.get("ok") is True)
    names = {b["name"] for b in branches.get("branches", [])}
    check("3.2 main is a local branch", "main" in names)
    check("3.3 current is main", branches.get("current") == "main")
    remote = git_branches_core(include_remote=True, root=REPO_ROOT)
    remote_names = {b["name"] for b in remote.get("branches", []) if b["kind"] == "remote"}
    check("3.4 remote-tracking refs are local metadata only", "origin/main" in remote_names)

    # -- git_diff -------------------------------------------------------------
    print("\n[4] git_diff (may be non-empty: uncommitted v0.4 work exists)")
    diff = git_diff_core(root=REPO_ROOT)
    check("4.1 diff ok", diff.get("ok") is True)
    print(f"        scope=working empty={diff.get('empty')} output_chars={diff.get('output_chars')}")
    staged = git_diff_core(scope="staged", root=REPO_ROOT)
    check("4.2 staged diff ok", staged.get("ok") is True)

    # -- security ---------------------------------------------------------------
    print("\n[5] security checks")
    broker = ExecutionBroker()
    for args in (["status"], ["diff"], ["push"]):
        prep = prepare_command_core("git", args=args, broker=broker, root=REPO_ROOT)
        check(f"5.x prepare_command('git {args[0]}') DENIED",
              prep.get("denied") is True and prep.get("ok") is False)
        break  # one representative check; full matrix lives in pytest
    prep = prepare_command_core("git", args=["status"], broker=broker, root=REPO_ROOT)
    check("5.1 prepare_command git DENIED", prep.get("denied") is True)

    bad_paths = ["../", "..\\outside", "/etc/passwd", ":(glob)**"]
    denial_results = [validate_git_pathspec(p, REPO_ROOT) for p in ("../", "/etc/passwd")]
    check("5.2 ../ pathspec denied", all(d is not None for d in denial_results))
    magic = validate_git_pathspec(":(glob)**", REPO_ROOT)
    check("5.3 pathspec magic passes as literal (git-side literal flag)", magic is None)

    env = build_git_environment({
        "GIT_DIR": "injected", "GIT_EXTERNAL_DIFF": "evil", "OPENAI_API_KEY": "fake",
        "PATH": "kept",
    })
    check("5.4 GIT_DIR scrubbed", "GIT_DIR" not in env)
    check("5.5 GIT_EXTERNAL_DIFF scrubbed", "GIT_EXTERNAL_DIFF" not in env)
    check("5.6 OPENAI_API_KEY scrubbed", "OPENAI_API_KEY" not in env)
    check("5.7 PATH preserved", env.get("PATH") == "kept")

    # -- read-only proof ---------------------------------------------------------
    print("\n[6] repository unchanged by the smoke")
    after = repo_state_snapshot()
    check("6.1 git status byte-identical before/after", before == after)

    # -- summary -------------------------------------------------------------------
    print("\n" + "=" * 64)
    print(f"Passed: {len(PASSED)}   Failed: {len(FAILED)}")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print("Git Awareness smoke: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
