"""
tool_code_mode — Code Mode as an Amplifier tool.

The LLM writes Python code; this tool runs it in-process using exec().
Every other mounted tool is injected into the execution namespace as an
async wrapper function — no subprocess, no TCP bridge, no temp files.

    tools:
      - module: tool-code-mode
        source: ./modules/tool-code-mode

Reference: https://blog.cloudflare.com/code-mode/
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import textwrap
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# No-op hooks shim
# ---------------------------------------------------------------------------


class _NoOpHooks:
    async def emit(self, _event: str, _data: Any = None) -> None:
        return None


# ---------------------------------------------------------------------------
# Tool schema helpers
# ---------------------------------------------------------------------------


def _schema_to_type(prop_schema: dict[str, Any]) -> str:
    """Convert a JSON Schema property definition to a Python type annotation.

    Handles: enum → Literal[...], array → list[inner], basic scalars.
    """
    json_type = prop_schema.get("type", "string")

    # Enum → Literal["a", "b", "c"]
    if "enum" in prop_schema:
        vals = ", ".join(f'"{v}"' for v in prop_schema["enum"])
        return f"Literal[{vals}]"

    # Array — include item type when available
    if json_type == "array":
        items = prop_schema.get("items", {})
        if items:
            inner = _schema_to_type(items)
            return f"list[{inner}]"
        return "list"

    return {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "object": "dict",
        "array": "list",
    }.get(json_type, "Any")


def _generate_tool_interfaces(tools: dict[str, Any]) -> str:
    """Generate Python async function stubs for the LLM.

    Analogous to Cloudflare Code Mode injecting TypeScript interface declarations
    into the system prompt. Each stub includes:
    - Full parameter list with Literal[...] types for enum params
    - Required params first (no default), optional params with = None
    - Docstring: tool description (first sentence) + per-parameter descriptions
    - Nested object structure hints for list[dict] params
    """
    if not tools:
        return "(no other tools available)"

    stubs: list[str] = []
    for name, tool in tools.items():
        schema: dict[str, Any] = getattr(tool, "input_schema", {}) or {}
        raw_desc: str = getattr(tool, "description", "") or ""

        # First sentence only — avoid embedding recursive interface blocks
        tool_desc = (
            (raw_desc.split("\n")[0].split(". ")[0].strip() + ".") if raw_desc else ""
        )
        tool_desc = tool_desc.rstrip("..").rstrip(".")  # clean up doubled periods
        if tool_desc and not tool_desc.endswith("."):
            tool_desc += "."

        props: dict[str, Any] = schema.get("properties", {}) or {}
        required: set[str] = set(schema.get("required", []) or [])

        # ---- parameter list (required first, then optional) ----
        req_params: list[str] = []
        opt_params: list[str] = []
        for pname, pschema in props.items():
            py_type = _schema_to_type(pschema)
            if pname in required:
                req_params.append(f"    {pname}: {py_type},")
            else:
                opt_params.append(f"    {pname}: {py_type} = None,")
        param_lines = req_params + opt_params

        # ---- docstring body ----
        doc_lines: list[str] = []
        if tool_desc:
            doc_lines.append(f"    {tool_desc}")

        for pname, pschema in props.items():
            pdesc = (pschema.get("description") or "").strip()
            penums = pschema.get("enum", [])
            ptype = pschema.get("type", "")

            annotation_parts: list[str] = []
            if pdesc:
                # Trim to first sentence to keep docstrings scannable
                first_sentence = pdesc.split(". ")[0].rstrip(".")
                annotation_parts.append(first_sentence)
            if penums and "enum" not in _schema_to_type(pschema):
                # Only add raw values when Literal wasn't already in the signature
                annotation_parts.append(" | ".join(f"'{v}'" for v in penums))
            if ptype == "array" and "items" in pschema:
                items_props = pschema["items"].get("properties", {})
                if items_props:
                    keys_str = ", ".join(f'"{k}"' for k in items_props)
                    annotation_parts.append(f"each dict has keys: {keys_str}")

            if annotation_parts:
                doc_lines.append(f"    {pname}: {' — '.join(annotation_parts)}")

        # ---- assemble stub ----
        if param_lines:
            sig = "(\n" + "\n".join(param_lines) + "\n) -> str"
        else:
            sig = "() -> str"

        if doc_lines:
            inner = "\n".join(doc_lines)
            docstring = f'\n    """\n{inner}\n    """\n    ...'
        else:
            docstring = "\n    ..."

        stubs.append(f"async def {name}{sig}:{docstring}")

    return "\n\n".join(stubs)


# ---------------------------------------------------------------------------
# Tool wrapper
# ---------------------------------------------------------------------------


def _make_wrapper(tool_name: str, tool_obj: Any, hooks: Any) -> Any:
    """
    Return an async function that calls a real Amplifier tool in-process.

    Accepts both positional and keyword args, mapping positional args to
    parameter names in schema property order.
    """
    schema: dict[str, Any] = getattr(tool_obj, "input_schema", {}) or {}
    param_names = list((schema.get("properties") or {}).keys())

    async def wrapper(*args: Any, **kwargs: Any) -> str:
        for i, val in enumerate(args):
            if i < len(param_names):
                kwargs.setdefault(param_names[i], val)
        await hooks.emit("tool:pre", {"tool_name": tool_name, "tool_input": kwargs})
        result = await tool_obj.execute(kwargs)
        output = getattr(result, "output", None) or str(result)
        await hooks.emit(
            "tool:post",
            {
                "tool_name": tool_name,
                "tool_input": kwargs,
                "tool_result": output,
            },
        )
        return output

    wrapper.__name__ = tool_name
    return wrapper


# ---------------------------------------------------------------------------
# In-process execution
# ---------------------------------------------------------------------------


async def _execute_code(
    code: str,
    tools: dict[str, Any],
    hooks: Any,
    timeout: int = 60,
) -> str:
    """
    Execute LLM-generated Python in-process.

    Tool wrappers are injected directly into the exec() namespace.
    Each `await tool_name(...)` call goes straight to the real Amplifier tool.
    stdout is captured with redirect_stdout.
    """
    namespace: dict[str, Any] = {
        name: _make_wrapper(name, tool, hooks) for name, tool in tools.items()
    }

    # Wrap in async def so user code can use await anywhere at the top level
    indented = textwrap.indent(code, "    ")
    if not indented.strip():
        indented = "    pass"
    wrapped = f"async def _tool_code_mode_main():\n{indented}"

    buf = io.StringIO()
    try:
        exec(compile(wrapped, "<tool_code_mode>", "exec"), namespace)  # noqa: S102
        with contextlib.redirect_stdout(buf):
            await asyncio.wait_for(
                namespace["_tool_code_mode_main"](),
                timeout=float(timeout),
            )
    except TimeoutError:
        return f"Error: execution timed out after {timeout}s"
    except SyntaxError as exc:
        return f"Syntax error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"Error: {type(exc).__name__}: {exc}"

    return buf.getvalue().strip() or "(no output)"


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------


class CodeModeTool:
    """
    Amplifier tool that runs LLM-generated Python code in-process.

    Every other tool mounted in the session is available as an async function
    injected into the execution namespace — just use `await tool_name(...)`.
    """

    name = "tool_code_mode"
    input_schema = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "Python async code to execute. "
                    "All session tools are in scope as async functions. "
                    "Use await when calling them. Use print() for output."
                ),
            },
        },
        "required": ["code"],
    }

    def __init__(self, coordinator: Any, config: dict[str, Any]) -> None:
        self._coordinator = coordinator
        self._timeout: int = int(config.get("timeout", 60))

    @property
    def description(self) -> str:
        """
        Dynamic: emits full Python async stubs for every available tool,
        including Literal types for enum params and per-parameter docstrings.

        Analogous to Cloudflare Code Mode injecting TypeScript interface
        declarations into the system prompt — the LLM gets precise API
        surface before generating any code.
        """
        all_tools: dict[str, Any] = self._coordinator.get("tools") or {}
        run_tools = {k: v for k, v in all_tools.items() if k != self.name}
        interfaces = _generate_tool_interfaces(run_tools)
        return (
            "Execute Python async code in-process. "
            "Combine multiple tool calls with Python logic (loops, conditions, "
            "data processing) and return one aggregated result via print().\n\n"
            f"Available functions (already in scope — use await):\n{interfaces}\n\n"
            "Standard library (json, os, re, pathlib, etc.) is also available."
        )

    async def execute(self, input_data: dict[str, Any]) -> Any:
        from amplifier_core import ToolResult  # type: ignore[import]  # peer dep

        code: str = (input_data.get("code") or "").strip()
        if not code:
            return ToolResult(success=False, output="Error: no code provided")

        all_tools: dict[str, Any] = self._coordinator.get("tools") or {}
        run_tools = {k: v for k, v in all_tools.items() if k != self.name}

        # Try to get real hooks; fall back to no-op
        hooks: Any = _NoOpHooks()
        with contextlib.suppress(Exception):
            candidate = self._coordinator.get("hooks")
            if candidate is not None and hasattr(candidate, "emit"):
                hooks = candidate

        result = await _execute_code(
            code=code,
            tools=run_tools,
            hooks=hooks,
            timeout=self._timeout,
        )
        return ToolResult(success=True, output=result)


# ---------------------------------------------------------------------------
# mount()
# ---------------------------------------------------------------------------


async def mount(
    coordinator: Any,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mount the tool_code_mode tool. Captures coordinator for live tool discovery at call time."""
    tool = CodeModeTool(coordinator=coordinator, config=config or {})
    await coordinator.mount("tools", tool, name=tool.name)
    logger.info("tool-code-mode: mounted (in-process exec)")
    return {
        "name": "tool-code-mode",
        "version": "0.1.0",
        "provides": ["tool_code_mode"],
    }
