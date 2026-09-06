from pathlib import Path

from aria.agent.aria import AriaAgent, register_deploy_coder_tool
from aria.agent.base import AgentEvent
from aria.agent.coder import CoderService
from aria.llm.base import AssistantMessage, ToolCall
from aria.memory import SessionMemory
from aria.prompts import build_coder_prompt, build_system_prompt
from aria.tools import ToolContext, ToolRegistry, register_filesystem_tools


class FakeProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools, on_text=None):
        self.calls += 1
        if self.calls == 1:
            return AssistantMessage(
                content="",
                tool_calls=[ToolCall("1", "write_file", {"path": "result.txt", "content": "done"})],
            )
        if on_text:
            on_text("Finished")
        return AssistantMessage(content="Finished")


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_filesystem_tools(registry)
    return registry


def test_aria_runs_tools_until_completion(tmp_path: Path) -> None:
    memory = SessionMemory(tmp_path)
    registry = make_registry()
    agent = AriaAgent(FakeProvider(), registry, ToolContext(tmp_path), memory, max_iterations=3)
    assert agent.run("create the result file") == "Finished"
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "done"
    assert len(memory.messages) == 5
    # The system prompt carries ARIA's persona and the tool protocol.
    assert "ARIA" in memory.messages[0]["content"]
    assert "CUSTOM TOOL PROTOCOL" in memory.messages[0]["content"]
    memory.cleanup()
    assert not memory.path.exists()


class NeverDoneProvider:
    def complete(self, messages, tools, on_text=None):
        return AssistantMessage(tool_calls=[ToolCall("1", "missing", {})])


class TextToolProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.messages = []

    def complete(self, messages, tools, on_text=None):
        self.calls += 1
        self.messages.append(list(messages))
        if self.calls == 1:
            return AssistantMessage(
                content=(
                    "I will create it.\n"
                    '<tool_call>{"name":"write_file","arguments":{'
                    '"path":"custom.txt","content":"created by text routing"}}</tool_call>'
                )
            )
        return AssistantMessage(content="Created custom.txt")


def test_aria_routes_text_tool_calls(tmp_path: Path) -> None:
    memory = SessionMemory(tmp_path)
    provider = TextToolProvider()
    agent = AriaAgent(provider, make_registry(), ToolContext(tmp_path), memory, max_iterations=3)

    displayed: list[str] = []
    assert agent.run("create a file", displayed.append) == "Created custom.txt"
    assert (tmp_path / "custom.txt").read_text(encoding="utf-8") == "created by text routing"
    assert provider.calls == 2
    assert "<tool_result>" in provider.messages[1][-1]["content"]
    assert displayed == ["I will create it.", "Created custom.txt"]
    memory.cleanup()


def test_aria_has_iteration_limit(tmp_path: Path) -> None:
    memory = SessionMemory(tmp_path)
    agent = AriaAgent(NeverDoneProvider(), make_registry(), ToolContext(tmp_path), memory, max_iterations=2)
    result = agent.run("keep going")
    assert "safety limit" in result
    memory.cleanup()


def test_agent_emits_chain_of_thought_events(tmp_path: Path) -> None:
    memory = SessionMemory(tmp_path)
    agent = AriaAgent(FakeProvider(), make_registry(), ToolContext(tmp_path), memory, max_iterations=3)

    events: list[AgentEvent] = []
    result = agent.run("create the result file", on_event=events.append)
    assert result == "Finished"

    kinds = [event.kind for event in events]
    assert kinds[0] == "round" and kinds[-1] == "text"
    assert "tool_call" in kinds and "tool_result" in kinds

    call = next(event for event in events if event.kind == "tool_call")
    assert call.name == "write_file"
    assert "result.txt" in call.detail

    tool_result = next(event for event in events if event.kind == "tool_result")
    assert tool_result.ok and tool_result.name == "write_file"
    assert not any(event.kind == "limit" for event in events)
    memory.cleanup()


def test_agent_events_work_without_listener(tmp_path: Path) -> None:
    memory = SessionMemory(tmp_path)
    agent = AriaAgent(FakeProvider(), make_registry(), ToolContext(tmp_path), memory, max_iterations=3)
    # on_event omitted entirely must behave exactly as before.
    assert agent.run("create the result file") == "Finished"
    memory.cleanup()


# --------------------------------------------------------------- deploy_coder


class SummarizingProvider:
    """Stands in for the coder's model: does nothing but report success."""

    def complete(self, messages, tools, on_text=None):
        return AssistantMessage(content="All done: created report.txt")


class ExplodingProvider:
    def complete(self, messages, tools, on_text=None):
        raise RuntimeError("provider exploded")


def test_deploy_coder_tool_runs_independent_agent(tmp_path: Path) -> None:
    memory = SessionMemory(tmp_path)

    class StubManager:
        def create(self, provider: str, model: str):
            return SummarizingProvider()

    service = CoderService(StubManager(), "stub", "stub-model", max_iterations=2)
    registry = make_registry()
    handler = register_deploy_coder_tool(registry, service)
    agent = AriaAgent(
        FakeProvider(), registry, ToolContext(tmp_path), memory, max_iterations=3
    )

    # ARIA's model asks for a deployment via the custom protocol.
    provider = TextToolDeploymentProvider()
    agent.provider = provider
    result = agent.run("please do the heavy work")
    assert provider.deployment_requested
    assert result  # ARIA answered after the coder report came back
    assert handler.last_report is not None and handler.last_report.ok
    memory.cleanup()


class TextToolDeploymentProvider:
    """First asks ARIA to deploy the coder, then wraps up."""

    def __init__(self) -> None:
        self.calls = 0
        self.deployment_requested = False

    def complete(self, messages, tools, on_text=None):
        self.calls += 1
        if self.calls == 1:
            self.deployment_requested = True
            return AssistantMessage(
                content=(
                    "Deploying the coder.\n"
                    '<tool_call>{"name":"deploy_coder","arguments":{'
                    '"task":"create report.txt with today\'s summary"}}</tool_call>'
                )
            )
        last = str(messages[-1]["content"])
        assert "All done" in last  # the coder report reached ARIA as tool output
        return AssistantMessage(content="The coding agent finished the task.")


def test_coder_service_reports_failures(tmp_path: Path) -> None:
    class StubManager:
        def create(self, provider: str, model: str):
            return ExplodingProvider()

    service = CoderService(StubManager(), "stub", "stub-model", max_iterations=2)
    report = service.deploy("do something", ToolContext(tmp_path))
    assert not report.ok
    assert "provider exploded" in report.summary


def test_coder_service_rejects_parallel_deployments(tmp_path: Path) -> None:
    """While a deployment holds the lock, a second deploy must be rejected."""

    class LockProbeProvider:
        """The 'model' inside the first deployment tries to deploy again."""

        def complete(self, messages, tools, on_text=None):
            nested = service.deploy("second task", ToolContext(tmp_path))
            assert not nested.ok
            assert nested.error == "busy"
            return AssistantMessage(content="first deployment done")

    class StubManager:
        def create(self, provider: str, model: str):
            return LockProbeProvider()

    service = CoderService(StubManager(), "stub", "stub-model", max_iterations=2)
    report = service.deploy("first task", ToolContext(tmp_path))
    assert report.ok


# ------------------------------------------------------------------- prompts


def test_prompts_carry_identity() -> None:
    system = build_system_prompt("jarvis", "Ali")
    assert "ARIA" in system
    assert "Adaptive Reasoning and Intelligence Assistant" in system
    assert "Ali" in system
    assert "deploy_coder" in system
    coder = build_coder_prompt("Ali")
    assert "independent specialist" in coder
