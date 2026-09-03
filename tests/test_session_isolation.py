"""v0.5.0 audit: cross-session execution + task-state isolation.

Proves that two Agent/factory runtimes in the same process keep their
ExecutionBroker and SessionState fully separate -- zero cross visibility
of plans, results, and host events.
"""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from harness_agent.agent import create_agent
from harness_agent.config import AgentConfig

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from harness_agent.execution import (  # noqa: E402
    ExecutionBroker,
    ExecutionResult,
    get_default_broker,
)
from harness_agent.session import SessionState  # noqa: E402
from harness_agent.tools.execution_tools import (  # noqa: E402
    make_execution_tools,
    prepare_command_core,
)

REPO_ROOT = Path(__file__).parent.parent.resolve()


def fake_execution_result(plan_id: str) -> ExecutionResult:
    return ExecutionResult(
        plan_id=plan_id,
        command="python --version --secret-do-not-store",
        cwd=str(REPO_ROOT),
        risk_level="LOW",
        approved=True,
        exit_code=0,
        stdout="secret-stdout",
        stderr="secret-stderr",
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=False,
        duration_ms=12,
    )


# ---------------------------------------------------------------------------
# 3. Cross-Session Execution Tests
# ---------------------------------------------------------------------------


def test_broker_b_never_sees_plan_or_result_from_broker_a():
    """Agent B cannot read a plan/result created by Agent A by id."""
    broker_a = ExecutionBroker()
    broker_b = ExecutionBroker()

    prepped = prepare_command_core(
        "python", ["--version"], broker=broker_a, root=REPO_ROOT
    )
    plan_a_id = prepped["plan_id"]

    # B lookups: unknown plan id, no result, not in pending.
    assert broker_b.get_plan(plan_a_id) is None
    assert broker_b.get_result(plan_a_id) is None
    assert broker_b.status(plan_a_id) is None
    assert plan_a_id not in [p.id for p in broker_b.pending()]

    # B's tool layer also reports it as unknown.
    from harness_agent.tools.execution_tools import get_execution_result_core

    looked = get_execution_result_core(plan_a_id, broker=broker_b)
    assert looked["ok"] is False
    assert "unknown" in looked["error"].lower()


def test_cross_session_factory_runtimes_are_isolated():
    """Two factory-default agents share neither task state nor broker."""
    config = AgentConfig(api_key="test-key", model_id="gpt-4", base_url=None)

    with patch("harness_agent.agent.OpenAIModel"), patch(
        "harness_agent.agent.Agent"
    ) as mock_agent_class:
        mock_agent_class.return_value = Mock()
        create_agent(config)
        first_tools = mock_agent_class.call_args.kwargs["tools"]
        create_agent(config)
        second_tools = mock_agent_class.call_args.kwargs["tools"]

    first = {
        getattr(item, "tool_name", getattr(item, "__name__", "")): item
        for item in first_tools
    }
    second = {
        getattr(item, "tool_name", getattr(item, "__name__", "")): item
        for item in second_tools
    }

    # A prepares a plan; B must not see it.
    prep_a = first["prepare_command"]("python", ["--version"])
    assert prep_a["ok"] is True
    looked_b = second["get_execution_result"](prep_a["plan_id"])
    assert looked_b["ok"] is False
    assert "unknown" in looked_b["error"].lower()

    # B prepares its own plan; A must not see it either.
    prep_b = second["prepare_command"]("python", ["--version"])
    assert prep_b["ok"] is True
    assert prep_b["plan_id"] != prep_a["plan_id"]
    looked_a = first["get_execution_result"](prep_b["plan_id"])
    assert looked_a["ok"] is False

    # Task states are distinct too.
    first["create_task_plan"]("Only A", ["Step"])
    assert first["get_task_state"]()["session"]["task_count"] == 1
    assert second["get_task_state"]()["session"]["task_count"] == 0


def test_a_approval_execution_produces_no_events_in_b_session():
    """Approving/executing in session A never writes to session B's state."""
    broker_a = ExecutionBroker()
    state_a = SessionState()
    state_b = SessionState()

    prepped = prepare_command_core(
        "python", ["--version"], broker=broker_a, root=REPO_ROOT
    )
    plan_id = prepped["plan_id"]

    # Host-side approval path for A only.
    import run_agent

    with patch("builtins.input", return_value="yes"), patch(
        "run_agent.execute_approved", return_value=fake_execution_result(plan_id)
    ), patch("sys.stdout", new_callable=StringIO):
        run_agent.process_pending_executions(broker_a, session_state=state_a)

    events_a = [e["type"] for e in state_a.snapshot()["recent_events"]]
    assert events_a == ["execution_approved", "execution_completed"]
    # B saw nothing.
    assert state_b.snapshot()["recent_events"] == []


def test_each_cli_only_processes_its_own_pending_plans():
    """Two brokers with their own pending plans process independently."""
    broker_a = ExecutionBroker()
    broker_b = ExecutionBroker()
    prep_a = prepare_command_core(
        "python", ["--version"], broker=broker_a, root=REPO_ROOT
    )
    prep_b = prepare_command_core(
        "python", ["--version"], broker=broker_b, root=REPO_ROOT
    )

    assert [p.id for p in broker_a.pending()] == [prep_a["plan_id"]]
    assert [p.id for p in broker_b.pending()] == [prep_b["plan_id"]]

    # CLI A rejects its own and never touches B's.
    import run_agent

    with patch("builtins.input", return_value="n"), patch(
        "sys.stdout", new_callable=StringIO
    ):
        run_agent.process_pending_executions(broker_a)

    assert broker_a.status(prep_a["plan_id"]) == "rejected"
    assert broker_b.status(prep_b["plan_id"]) == "pending"


# ---------------------------------------------------------------------------
# 4. Default Factory Isolation (no explicit broker/session passed)
# ---------------------------------------------------------------------------


def test_default_factory_creates_private_broker_and_session_binding():
    """create_agent() without explicit broker yields independent state."""
    config = AgentConfig(api_key="test-key", model_id="gpt-4", base_url=None)

    with patch("harness_agent.agent.OpenAIModel"), patch(
        "harness_agent.agent.Agent"
    ) as mock_agent_class:
        mock_agent_class.return_value = Mock()

        create_agent(config)
        tools_a = mock_agent_class.call_args.kwargs["tools"]
        create_agent(config)
        tools_b = mock_agent_class.call_args.kwargs["tools"]

    a = {t.tool_name: t for t in tools_a}
    b = {t.tool_name: t for t in tools_b}

    assert a["prepare_command"] is not b["prepare_command"]
    assert a["create_task_plan"] is not b["create_task_plan"]
    assert a["get_execution_result"] is not b["get_execution_result"]

    prep = a["prepare_command"]("python", ["--version"])
    assert prep["ok"] is True
    # b can't see it
    assert b["get_execution_result"](prep["plan_id"])["ok"] is False


# ---------------------------------------------------------------------------
# 2. Execution Broker Isolation: the audited bug
# ---------------------------------------------------------------------------


def test_factory_no_longer_uses_process_global_default_broker():
    """The default factory must NOT route to get_default_broker()."""
    config = AgentConfig(api_key="test-key", model_id="gpt-4", base_url=None)

    with patch("harness_agent.agent.OpenAIModel"), patch(
        "harness_agent.agent.Agent"
    ) as mock_agent_class:
        mock_agent_class.return_value = Mock()
        create_agent(config)
        tools = mock_agent_class.call_args.kwargs["tools"]

    tools_by_name = {getattr(t, "tool_name", ""): t for t in tools}
    prep = tools_by_name["prepare_command"]("python", ["--version"])
    plan = prep["plan_id"]

    # The process-global default broker must NOT contain this plan.
    assert get_default_broker().get_plan(plan) is None


def test_make_execution_tools_rejects_non_broker():
    import pytest

    with pytest.raises(TypeError):
        make_execution_tools(object())  # type: ignore[arg-type]


def test_make_execution_tools_bind_single_broker():
    broker = ExecutionBroker()
    prep_tool, result_tool = make_execution_tools(broker)

    prepared = prep_tool("python", ["--version"])
    assert prepared["ok"] is True
    plan_id = prepared["plan_id"]

    result = result_tool(plan_id)
    assert result["ok"] is True
    assert result["executed"] is False
    assert broker.status(plan_id) == "pending"
    assert get_default_broker().get_plan(plan_id) is None
