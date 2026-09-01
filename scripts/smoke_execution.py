#!/usr/bin/env python
"""Host-level smoke tests for v0.3.0 User-Approved Safe Execution.

Runs WITHOUT any LLM API access. It exercises the exact production path:
policy validation -> pending plan -> (fake) user approval -> host
subprocess execution -> structured result.

Checks:
  A. python --version      -> plan allowed, executes successfully
  B. python -c "..."       -> DENIED
  C. pip install requests  -> DENIED
  D. cwd "../"             -> DENIED
  E. shell sequence        -> DENIED
  F. OPENAI_API_KEY        -> scrubbed from the child environment
  G. approval = NO         -> ZERO subprocess execution
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from harness_agent.execution import (  # noqa: E402
    ExecutionBroker,
    build_child_environment,
    execute_approved,
)
from harness_agent.tools.execution_tools import prepare_command_core  # noqa: E402

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


def approve_all(broker: ExecutionBroker) -> None:
    for plan in broker.pending():
        broker.approve(plan.id)
        execute_approved(broker, plan.id)


def deny_all(broker: ExecutionBroker) -> None:
    for plan in broker.pending():
        broker.reject(plan.id)


def main() -> int:
    print("=" * 64)
    print("Harness Agent v0.3.0 - Execution Smoke (host-level, no LLM)")
    print("=" * 64)

    # -- A. python --version: allowed end-to-end ---------------------------
    print("\n[A] python --version -> plan created + approved + executed")
    broker = ExecutionBroker()
    prep = prepare_command_core("python", ["--version"], broker=broker, root=REPO_ROOT)
    check("A1 plan created", prep.get("ok") is True and prep.get("requires_approval") is True)
    check("A2 nothing executed yet", broker.get_result(prep["plan_id"]) is None)
    approve_all(broker)
    result = broker.get_result(prep["plan_id"])
    check(
        "A3 executed successfully",
        result is not None and result.exit_code == 0 and not result.timed_out,
        f"exit_code={result.exit_code if result else None}",
    )
    check(
        "A4 single-use enforced",
        broker.status(prep["plan_id"]) == "executed",
    )

    # -- B. python -c -------------------------------------------------------
    print("\n[B] python -c \"print('hello')\" -> DENIED")
    broker = ExecutionBroker()
    prep = prepare_command_core(
        "python", ["-c", "print('hello')"], broker=broker, root=REPO_ROOT
    )
    check("B1 denied", prep.get("ok") is False and prep.get("denied") is True)
    check("B2 no plan registered", not broker.pending())

    # -- C. pip install -----------------------------------------------------
    print("\n[C] pip install requests -> DENIED")
    broker = ExecutionBroker()
    prep = prepare_command_core("pip", ["install", "requests"], broker=broker, root=REPO_ROOT)
    check("C1 pip denied", prep.get("denied") is True)
    prep = prepare_command_core(
        "python", ["-m", "pip", "install", "requests"], broker=broker, root=REPO_ROOT
    )
    check("C2 python -m pip denied", prep.get("denied") is True)

    # -- D. cwd escape -------------------------------------------------------
    print("\n[D] cwd='../' -> DENIED")
    broker = ExecutionBroker()
    prep = prepare_command_core(
        "python", ["--version"], cwd="../", broker=broker, root=REPO_ROOT
    )
    check("D1 parent cwd denied", prep.get("denied") is True)

    # -- E. shell sequences ---------------------------------------------------
    print("\n[E] shell sequences -> DENIED")
    broker = ExecutionBroker()
    prep = prepare_command_core(
        "pytest", ["-q", "&&", "something"], broker=broker, root=REPO_ROOT
    )
    check("E1 '&&' chain denied", prep.get("denied") is True)
    prep = prepare_command_core(
        "pytest", [">", "result.txt"], broker=broker, root=REPO_ROOT
    )
    check("E2 redirect denied", prep.get("denied") is True)

    # -- F. environment scrubbing ---------------------------------------------
    print("\n[F] secrets and behavior-injection vars must not reach the child")
    poisoned = dict(os.environ)
    poisoned["OPENAI_API_KEY"] = "smoke-super-secret"  # fake value, never real
    poisoned["PYTHONPATH"] = "smoke-injected-path"
    poisoned["PYTEST_ADDOPTS"] = "-p smoke_evil_plugin"
    child_env = build_child_environment(poisoned)
    check("F1 secret scrubbed", "OPENAI_API_KEY" not in child_env)
    check("F2 behavior vars scrubbed",
          "PYTHONPATH" not in child_env and "PYTEST_ADDOPTS" not in child_env)
    check("F3 PATH preserved", "PATH" in child_env)
    check("F4 hardening flags set",
          child_env.get("PYTHONDONTWRITEBYTECODE") == "1"
          and child_env.get("PYTHONNOUSERSITE") == "1"
          and child_env.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1")

    probe_code = (
        "import os, sys; "
        "leaks = [k for k in ('OPENAI_API_KEY', 'PYTHONPATH', 'PYTEST_ADDOPTS') "
        "if k in os.environ]; "
        "sys.exit(0 if not leaks else 7)"
    )
    probe = subprocess.run(
        [sys.executable, "-c", probe_code],
        shell=False,
        cwd=str(REPO_ROOT),
        env=build_child_environment({"OPENAI_API_KEY": "smoke-super-secret",
                                     "PYTHONPATH": "smoke-injected-path",
                                     "PYTEST_ADDOPTS": "-p smoke_evil_plugin",
                                     "PATH": os.environ.get("PATH", "")}),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    check(
        "F5 child process cannot see secrets/behavior vars",
        probe.returncode == 0,
        f"rc={probe.returncode}",
    )

    # -- G. approval = NO -> zero execution ------------------------------------
    print("\n[G] user declines -> ZERO subprocess execution")
    broker = ExecutionBroker()
    prep = prepare_command_core("python", ["--version"], broker=broker, root=REPO_ROOT)
    check("G1 plan pending", broker.status(prep["plan_id"]) == "pending")
    deny_all(broker)
    check("G2 plan rejected", broker.status(prep["plan_id"]) == "rejected")
    check("G3 nothing stored", broker.get_result(prep["plan_id"]) is None)

    # -- Security matrix --------------------------------------------------------
    print("\n[Security matrix] policy decisions")
    matrix = [
        ("python --version", "python", ["--version"], ".", True),
        ("pytest -q", "pytest", ["-q"], ".", True),
        ("ruff check .", "ruff", ["check", "."], ".", True),
        ("python -c", "python", ["-c", "x"], ".", False),
        ("python malicious.py", "python", ["malicious.py"], ".", False),
        ("python -m pip install", "python", ["-m", "pip", "install", "x"], ".", False),
        ("pip install", "pip", ["install", "x"], ".", False),
        ("git status", "git", ["status"], ".", False),
        ("powershell", "powershell", [], ".", False),
        ("cmd", "cmd", [], ".", False),
        ("bash", "bash", [], ".", False),
        ("curl https://example.com", "curl", ["https://example.com"], ".", False),
        ("pytest && something", "pytest", ["&&", "something"], ".", False),
        ("pytest > result.txt", "pytest", [">", "result.txt"], ".", False),
        ("external cwd", "python", ["--version"], "../", False),
        ("external path arg", "pytest", ["-q", str(REPO_ROOT.parent)], ".", False),
    ]
    for label, program, args, cwd, expected_allowed in matrix:
        decision = prepare_command_core(
            program, args=args, cwd=cwd,
            broker=ExecutionBroker(), root=REPO_ROOT,
        )
        allowed = decision.get("ok") is True
        ok = allowed == expected_allowed
        check(
            f"{label} -> {'ALLOWED' if expected_allowed else 'DENIED'}",
            ok,
            "" if ok else f"unexpected result: {decision}",
        )

    # -- Summary -----------------------------------------------------------------
    print("\n" + "=" * 64)
    print(f"Passed: {len(PASSED)}   Failed: {len(FAILED)}")
    if FAILED:
        print("Failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print("Execution smoke: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
