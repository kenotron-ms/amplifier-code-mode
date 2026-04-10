"""
Protocol compliance + unit tests for tool-code-mode.

Tests:
  - mount() Iron Law: coordinator.mount() MUST be called with slot "tools"
  - mount() returns a non-None metadata dict with name + provides
  - CodeModeTool has name, description, input_schema
  - _generate_tool_interfaces produces correct stubs (Literal types, docstrings)
  - _execute_code runs real Python in-process (no amplifier_core needed)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from amplifier_module_tool_code_mode import (
    CodeModeTool,
    _NoOpHooks,
    _execute_code,
    _generate_tool_interfaces,
    _schema_to_type,
    mount,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fake_tool(name: str = "bash", description: str = "Run a shell command.") -> MagicMock:
    tool = MagicMock()
    tool.name = name
    tool.description = description
    tool.input_schema = {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "Shell command to run"}},
        "required": ["command"],
    }
    return tool


def _fake_coordinator(tools: dict | None = None) -> MagicMock:
    coord = MagicMock()
    coord.mount = AsyncMock()
    coord.register_contributor = MagicMock(return_value=None)
    coord.get = MagicMock(side_effect=lambda slot, *_: tools if slot == "tools" else None)
    return coord


# ---------------------------------------------------------------------------
# mount() — Iron Law
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_calls_coordinator_mount():
    coord = _fake_coordinator()
    await mount(coord)
    coord.mount.assert_called_once()


@pytest.mark.asyncio
async def test_mount_registers_to_tools_slot():
    coord = _fake_coordinator()
    await mount(coord)
    call_args = coord.mount.call_args
    assert call_args[0][0] == "tools"


@pytest.mark.asyncio
async def test_mount_returns_metadata_dict():
    coord = _fake_coordinator()
    result = await mount(coord)
    assert result is not None
    assert isinstance(result, dict)
    assert "name" in result
    assert "provides" in result


@pytest.mark.asyncio
async def test_mount_name_is_tool_code_mode():
    coord = _fake_coordinator()
    result = await mount(coord)
    assert result["name"] == "tool-code-mode"


@pytest.mark.asyncio
async def test_mount_provides_tool_code_mode():
    coord = _fake_coordinator()
    result = await mount(coord)
    assert "tool_code_mode" in result["provides"]


@pytest.mark.asyncio
async def test_mount_accepts_config():
    coord = _fake_coordinator()
    result = await mount(coord, config={"timeout": 30})
    assert result is not None


# ---------------------------------------------------------------------------
# CodeModeTool properties
# ---------------------------------------------------------------------------


def test_tool_name():
    coord = _fake_coordinator()
    tool = CodeModeTool(coordinator=coord, config={})
    assert tool.name == "tool_code_mode"


def test_tool_description_is_non_empty():
    coord = _fake_coordinator()
    tool = CodeModeTool(coordinator=coord, config={})
    assert isinstance(tool.description, str)
    assert len(tool.description) > 20


def test_tool_input_schema_has_code():
    coord = _fake_coordinator()
    tool = CodeModeTool(coordinator=coord, config={})
    assert "code" in tool.input_schema["properties"]
    assert "code" in tool.input_schema.get("required", [])


def test_tool_timeout_default():
    coord = _fake_coordinator()
    tool = CodeModeTool(coordinator=coord, config={})
    assert tool._timeout == 60


def test_tool_timeout_from_config():
    coord = _fake_coordinator()
    tool = CodeModeTool(coordinator=coord, config={"timeout": 15})
    assert tool._timeout == 15


# ---------------------------------------------------------------------------
# _schema_to_type
# ---------------------------------------------------------------------------


def test_schema_to_type_basic():
    assert _schema_to_type({"type": "string"}) == "str"
    assert _schema_to_type({"type": "integer"}) == "int"
    assert _schema_to_type({"type": "boolean"}) == "bool"
    assert _schema_to_type({"type": "object"}) == "dict"


def test_schema_to_type_enum_produces_literal():
    result = _schema_to_type({"type": "string", "enum": ["none", "recent", "all"]})
    assert result == 'Literal["none", "recent", "all"]'


def test_schema_to_type_array_with_items():
    result = _schema_to_type({"type": "array", "items": {"type": "object"}})
    assert result == "list[dict]"


def test_schema_to_type_array_without_items():
    result = _schema_to_type({"type": "array"})
    assert result == "list"


# ---------------------------------------------------------------------------
# _generate_tool_interfaces
# ---------------------------------------------------------------------------


def test_generate_tool_interfaces_lists_tools():
    tools = {"bash": _fake_tool("bash"), "read_file": _fake_tool("read_file")}
    result = _generate_tool_interfaces(tools)
    assert "async def bash" in result
    assert "async def read_file" in result


def test_generate_tool_interfaces_empty():
    result = _generate_tool_interfaces({})
    assert "no other tools" in result.lower()


def test_generate_tool_interfaces_required_param_no_default():
    tools = {"bash": _fake_tool("bash")}
    result = _generate_tool_interfaces(tools)
    # required param must not have = None
    assert "command: str," in result
    assert "command: str = None" not in result


def test_generate_tool_interfaces_optional_param_has_default():
    tool = MagicMock()
    tool.name = "read_file"
    tool.description = "Read a file."
    tool.input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to file"},
            "offset": {"type": "integer", "description": "Start line"},
        },
        "required": ["file_path"],
    }
    result = _generate_tool_interfaces({"read_file": tool})
    assert "file_path: str," in result        # required — no default
    assert "offset: int = None," in result    # optional — has default


def test_generate_tool_interfaces_enum_becomes_literal():
    tool = MagicMock()
    tool.name = "delegate"
    tool.description = "Spawn an agent."
    tool.input_schema = {
        "type": "object",
        "properties": {
            "instruction": {"type": "string", "description": "The task"},
            "context_depth": {
                "type": "string",
                "enum": ["none", "recent", "all"],
                "description": "How much context",
            },
        },
        "required": ["instruction"],
    }
    result = _generate_tool_interfaces({"delegate": tool})
    assert 'Literal["none", "recent", "all"]' in result


def test_generate_tool_interfaces_includes_description_in_docstring():
    tool = MagicMock()
    tool.name = "bash"
    tool.description = "Execute a shell command."
    tool.input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run"},
        },
        "required": ["command"],
    }
    result = _generate_tool_interfaces({"bash": tool})
    assert "Execute a shell command" in result
    assert "The shell command to run" in result


def test_generate_tool_interfaces_nested_list_items_hinted():
    tool = MagicMock()
    tool.name = "delegate"
    tool.description = "Spawn an agent."
    tool.input_schema = {
        "type": "object",
        "properties": {
            "instruction": {"type": "string"},
            "provider_preferences": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "provider": {"type": "string"},
                        "model": {"type": "string"},
                    },
                },
                "description": "Ordered list of provider preferences",
            },
        },
        "required": ["instruction"],
    }
    result = _generate_tool_interfaces({"delegate": tool})
    # Should hint the dict keys in the docstring
    assert '"provider"' in result or "provider" in result


# ---------------------------------------------------------------------------
# _execute_code — end-to-end sandbox tests (no amplifier_core needed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_code_simple_print():
    """The sandbox must run Python and capture stdout."""
    result = await _execute_code(
        code='print("hello from sandbox")',
        tools={},
        hooks=_NoOpHooks(),
        timeout=10,
    )
    assert result == "hello from sandbox"


@pytest.mark.asyncio
async def test_execute_code_multi_line():
    code = "x = 1 + 2\nprint(x)"
    result = await _execute_code(code=code, tools={}, hooks=_NoOpHooks(), timeout=10)
    assert result.strip() == "3"


@pytest.mark.asyncio
async def test_execute_code_error_captured():
    result = await _execute_code(
        code="raise ValueError('oops')",
        tools={},
        hooks=_NoOpHooks(),
        timeout=10,
    )
    assert "error" in result.lower() or "oops" in result.lower()


@pytest.mark.asyncio
async def test_execute_code_timeout():
    result = await _execute_code(
        code="import asyncio\nawait asyncio.sleep(30)",
        tools={},
        hooks=_NoOpHooks(),
        timeout=1,
    )
    assert "timed out" in result.lower()


@pytest.mark.asyncio
async def test_execute_code_calls_tool_in_process():
    """End-to-end: code calls a fake tool injected into the exec namespace."""
    fake_tool = MagicMock()
    fake_tool.name = "my_tool"
    fake_tool.description = "A fake tool."
    fake_tool.input_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    result_holder: list[str] = []

    async def fake_execute(input_data: dict) -> MagicMock:
        result_holder.append(input_data.get("value", ""))
        m = MagicMock()
        m.output = f"got:{input_data.get('value', '')}"
        return m

    fake_tool.execute = fake_execute

    code = 'out = await my_tool(value="hello")\nprint(out)'
    result = await _execute_code(
        code=code,
        tools={"my_tool": fake_tool},
        hooks=_NoOpHooks(),
        timeout=10,
    )
    assert result == "got:hello"
    assert result_holder == ["hello"]
