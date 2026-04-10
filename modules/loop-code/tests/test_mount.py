"""
Protocol compliance + unit tests for the loop-code orchestrator module.

Covers:
  - mount() Iron Law: coordinator.mount() MUST be called
  - mount() returns a non-None metadata dict
  - CodeModeOrchestrator has an execute() coroutine
  - _extract_python_code() finds / ignores code blocks
  - _generate_tool_interfaces() produces correct Python signatures
  - _generate_bridge_module() produces valid-looking Python source
  - _build_system_prompt() mentions available tools
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from amplifier_module_loop_code import (
    CodeModeOrchestrator,
    _build_system_prompt,
    _extract_python_code,
    _generate_bridge_module,
    _generate_tool_interfaces,
    mount,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fake_tool(name: str = "bash", description: str = "Run a shell command.") -> MagicMock:
    """Return a minimal mock tool object."""
    tool = MagicMock()
    tool.name = name
    tool.description = description
    tool.input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
        },
        "required": ["command"],
    }
    return tool


def _fake_coordinator() -> MagicMock:
    coordinator = MagicMock()
    coordinator.mount = AsyncMock()
    coordinator.register_contributor = MagicMock(return_value=None)
    return coordinator


# ---------------------------------------------------------------------------
# mount() — protocol compliance (the Iron Law)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_calls_coordinator_mount():
    """mount() MUST call coordinator.mount() — the Iron Law."""
    coordinator = _fake_coordinator()
    await mount(coordinator)
    coordinator.mount.assert_called_once()


@pytest.mark.asyncio
async def test_mount_registers_to_session_slot():
    """mount() must register to the 'session' slot (first positional arg)."""
    coordinator = _fake_coordinator()
    await mount(coordinator)

    call_args = coordinator.mount.call_args
    assert call_args[0][0] == "session", (
        f"Expected first arg to coordinator.mount() to be 'session', got {call_args[0][0]!r}"
    )


@pytest.mark.asyncio
async def test_mount_returns_metadata_dict():
    """mount() must return a non-None metadata dict (not None)."""
    coordinator = _fake_coordinator()
    result = await mount(coordinator)

    assert result is not None, "mount() must return a dict, not None"
    assert isinstance(result, dict)
    assert "name" in result
    assert "provides" in result


@pytest.mark.asyncio
async def test_mount_metadata_provides_orchestrator():
    """mount() metadata must list 'orchestrator' in 'provides'."""
    coordinator = _fake_coordinator()
    result = await mount(coordinator)
    assert "orchestrator" in result["provides"]


@pytest.mark.asyncio
async def test_mount_name_is_loop_code():
    """mount() metadata 'name' must be 'loop-code'."""
    coordinator = _fake_coordinator()
    result = await mount(coordinator)
    assert result["name"] == "loop-code"


@pytest.mark.asyncio
async def test_mount_accepts_config():
    """mount() must accept a config dict without raising."""
    coordinator = _fake_coordinator()
    result = await mount(coordinator, config={"max_iterations": 5, "timeout": 30})
    assert result is not None


@pytest.mark.asyncio
async def test_mount_handles_missing_register_contributor():
    """mount() must not crash when register_contributor is absent."""
    coordinator = _fake_coordinator()
    del coordinator.register_contributor  # simulate older coordinator without this method
    result = await mount(coordinator)
    assert result is not None


# ---------------------------------------------------------------------------
# CodeModeOrchestrator
# ---------------------------------------------------------------------------


def test_orchestrator_has_execute_coroutine():
    """CodeModeOrchestrator.execute must exist and be a coroutine function."""
    import asyncio

    orch = CodeModeOrchestrator()
    assert callable(orch.execute)
    assert asyncio.iscoroutinefunction(orch.execute)


def test_orchestrator_config_defaults():
    """Default config values should be applied when config=None."""
    orch = CodeModeOrchestrator()
    assert orch._max_iterations == 10
    assert orch._timeout == 60


def test_orchestrator_config_override():
    """Custom config values should override defaults."""
    orch = CodeModeOrchestrator(config={"max_iterations": 3, "timeout": 15})
    assert orch._max_iterations == 3
    assert orch._timeout == 15


# ---------------------------------------------------------------------------
# _extract_python_code
# ---------------------------------------------------------------------------


def test_extract_python_code_finds_python_block():
    code = _extract_python_code("Here:\n```python\nprint('hi')\n```\nDone.")
    assert code == "print('hi')"


def test_extract_python_code_finds_py_block():
    code = _extract_python_code("```py\nx = 1\n```")
    assert code == "x = 1"


def test_extract_python_code_returns_none_when_absent():
    assert _extract_python_code("Just plain text.") is None


def test_extract_python_code_strips_whitespace():
    code = _extract_python_code("```python\n\n  print('hi')  \n\n```")
    assert code == "print('hi')"


def test_extract_python_code_multiline():
    text = "```python\na = 1\nb = 2\nprint(a + b)\n```"
    code = _extract_python_code(text)
    assert code == "a = 1\nb = 2\nprint(a + b)"


# ---------------------------------------------------------------------------
# _generate_tool_interfaces
# ---------------------------------------------------------------------------


def test_generate_tool_interfaces_produces_async_def():
    tools = {"bash": _fake_tool("bash")}
    result = _generate_tool_interfaces(tools)
    assert "async def bash(" in result


def test_generate_tool_interfaces_includes_param():
    tools = {"bash": _fake_tool("bash")}
    result = _generate_tool_interfaces(tools)
    assert "command: str" in result


def test_generate_tool_interfaces_includes_description():
    tools = {"bash": _fake_tool("bash", "Run a shell command.")}
    result = _generate_tool_interfaces(tools)
    assert "Run a shell command" in result


def test_generate_tool_interfaces_empty_tools():
    result = _generate_tool_interfaces({})
    assert "No tools" in result


def test_generate_tool_interfaces_multiple_tools():
    tools = {
        "bash": _fake_tool("bash"),
        "read_file": _fake_tool("read_file", "Read a file."),
    }
    result = _generate_tool_interfaces(tools)
    assert "async def bash(" in result
    assert "async def read_file(" in result


# ---------------------------------------------------------------------------
# _generate_bridge_module
# ---------------------------------------------------------------------------


def test_generate_bridge_module_contains_call_tool():
    tools = {"bash": _fake_tool("bash")}
    src = _generate_bridge_module(tools)
    assert "_call_tool" in src


def test_generate_bridge_module_contains_bridge_port():
    tools = {"bash": _fake_tool("bash")}
    src = _generate_bridge_module(tools)
    assert "AMPLIFIER_BRIDGE_PORT" in src


def test_generate_bridge_module_contains_tool_function():
    tools = {"bash": _fake_tool("bash")}
    src = _generate_bridge_module(tools)
    assert "async def bash(" in src


def test_generate_bridge_module_is_valid_python():
    """The generated bridge module must compile without syntax errors."""
    tools = {"bash": _fake_tool("bash")}
    src = _generate_bridge_module(tools)
    # compile() raises SyntaxError on bad Python
    compile(src, "<bridge>", "exec")


# ---------------------------------------------------------------------------
# _build_system_prompt
# ---------------------------------------------------------------------------


def test_build_system_prompt_mentions_code_mode():
    prompt = _build_system_prompt("async def bash(...): ...")
    assert "Code Mode" in prompt


def test_build_system_prompt_includes_interfaces():
    interfaces = "async def bash(command: str) -> str: ..."
    prompt = _build_system_prompt(interfaces)
    assert "async def bash" in prompt
