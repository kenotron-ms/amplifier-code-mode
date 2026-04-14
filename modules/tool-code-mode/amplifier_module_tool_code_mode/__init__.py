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
import ast
import textwrap
import uuid
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Known return schemas — fallback when tools don't declare output_schema
# ---------------------------------------------------------------------------
# Keys must match the actual dict keys the tool returns at runtime.
# This dict drives two features:
#   1. Stub docstrings: _generate_tool_interfaces() emits "Returns: dict — keys: 'x', 'y'"
#   2. describe() helper: pre-injected into exec() namespace so code can inspect at runtime.

_KNOWN_OUTPUT_SCHEMAS: dict[str, list[str]] = {
    "bash": ["stdout", "stderr", "returncode"],
    "read_file": ["content", "file_path", "total_lines", "lines_read", "offset"],
    "write_file": ["file_path", "bytes"],
    "edit_file": ["file_path", "success"],
    "web_fetch": ["url", "content", "content_type", "truncated", "total_bytes"],
    "web_search": ["results"],
    # glob: actual runtime keys — iterate result['matches'], not result directly.
    # Iterating over the dict gives string keys, causing TypeError when subscripted.
    "glob": ["matches", "count", "total_files"],
    # grep: data key varies by output_mode: 'results' (content), 'files' (files_with_matches), 'counts' (count).
    # 'matches' is NOT a valid key — iterating result directly gives string keys, causing TypeError.
    "grep": ["results", "total_matches", "matches_count"],
    "delegate": ["response", "session_id", "status", "turn_count"],
    "todo": ["count", "status", "todos"],
}


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
        tool_desc = (raw_desc.split("\n")[0].split(". ")[0].strip() + ".") if raw_desc else ""
        tool_desc = tool_desc.rstrip(".").rstrip(".")  # clean up doubled periods
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

        # ---- return shape (output_schema > _KNOWN_OUTPUT_SCHEMAS > generic) ----
        output_schema = getattr(tool, "output_schema", None)
        if output_schema and isinstance(output_schema, dict):
            out_props = output_schema.get("properties", {})
            if out_props:
                keys_str = ", ".join(f"'{k}'" for k in out_props)
                doc_lines.append(f"    Returns: dict — keys: {keys_str}")
            else:
                doc_lines.append("    Returns: dict")
        elif name in _KNOWN_OUTPUT_SCHEMAS:
            keys_str = ", ".join(f"'{k}'" for k in _KNOWN_OUTPUT_SCHEMAS[name])
            doc_lines.append(f"    Returns: dict — keys: {keys_str}")
        else:
            doc_lines.append("    Returns: dict — use result['key'] to access values")

        # ---- assemble stub ----
        sig = "(\n" + "\n".join(param_lines) + "\n) -> dict" if param_lines else "() -> dict"

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

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        for i, val in enumerate(args):
            if i < len(param_names):
                kwargs.setdefault(param_names[i], val)
        call_id = str(uuid.uuid4())
        # Use prefixed event names so the orchestrator's trace_collector_post
        # (which tracks LLM-dispatched tool calls only) does not try to correlate
        # these inner calls against its in-flight registry and crash.
        # As a bonus, this prevents logging-hook stdout output from leaking into
        # the captured exec() buffer.
        await hooks.emit(
            "code_mode:tool:pre",
            {"call_id": call_id, "tool_name": tool_name, "tool_input": kwargs},
        )
        result = await tool_obj.execute(kwargs)
        # Explicit None check — avoids silently replacing falsy-but-valid outputs
        # (empty dict, empty string, False, 0) with str(result).
        output = getattr(result, "output", None)
        if output is None:
            output = str(result)
        await hooks.emit(
            "code_mode:tool:post",
            {
                "call_id": call_id,
                "tool_name": tool_name,
                "tool_input": kwargs,
                "tool_result": output,
            },
        )
        return output

    wrapper.__name__ = tool_name
    return wrapper


# ---------------------------------------------------------------------------
# Ruff lint + auto-fix (deterministic, no LLM)
# ---------------------------------------------------------------------------

def _remove_unused_imports(code: str) -> str:
        """Remove unused imports from code using Python's ast module.

        Walks the AST to find every Name/Attribute root referenced outside import
        statements, then drops any import whose bound names are entirely absent.
        Returns the original code unchanged if parsing fails (SyntaxError is left
        for compile() to surface with a cleaner message).
        Completely in-process — no subprocess, no temp files, no external tools.
        """
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return code

        # Collect every name referenced in non-import nodes.
        used: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                root = node
                while isinstance(root, ast.Attribute):
                    root = root.value  # type: ignore[assignment]
                if isinstance(root, ast.Name):
                    used.add(root.id)

        # Find line ranges of import statements whose bound names are all unused.
        drop: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if all((a.asname or a.name.split(".")[0]) not in used for a in node.names):
                    drop.update(range(node.lineno, node.end_lineno + 1))
            elif isinstance(node, ast.ImportFrom):
                if all((a.asname or a.name) not in used for a in node.names):
                    drop.update(range(node.lineno, node.end_lineno + 1))

        if not drop:
            return code

        kept = [ln for i, ln in enumerate(code.splitlines(keepends=True), 1) if i not in drop]
        return "".join(kept).strip()


async def _gather_limited(coros: Any, limit: int = 10) -> list[Any]:
    """
    Like asyncio.gather() but caps concurrent execution to `limit` at a time.

    Usage (pre-injected as gather_limited — no import needed):
        results = await gather_limited([tool(x) for x in items], limit=20)
    """
    sem = asyncio.Semaphore(limit)

    async def _one(coro: Any) -> Any:
        async with sem:
            return await coro

    return list(await asyncio.gather(*[_one(c) for c in coros]))


def _make_describe_fn(tools: dict[str, Any]) -> Any:
    """Return a describe() function pre-loaded with the available tools.

    The returned function is pre-injected into the exec() namespace as `describe`.
    It lets code inspect a tool's return keys before using them — a runtime safety
    net that mirrors what the stub docstrings already show at prompt time.

    Usage inside a code block (no import needed):
        print(describe("bash"))
        # → {'keys': ['stdout', 'stderr', 'returncode'], 'source': 'known'}
    """

    def describe(tool_name: str) -> dict[str, Any]:
        """Return the documented return shape for a tool.

        Args:
            tool_name: Name of the tool to describe (e.g. "bash", "read_file").

        Returns:
            dict with 'keys' (list[str]) and 'source' ('output_schema' | 'known' | 'unknown').
            On unknown tool: {'error': str, 'available': list[str]}.
        """
        tool = tools.get(tool_name)

        # Priority 1: authoritative output_schema on the mounted tool object
        if tool is not None:
            output_schema = getattr(tool, "output_schema", None)
            if output_schema and isinstance(output_schema, dict):
                props = output_schema.get("properties", {})
                if props:
                    return {"keys": list(props.keys()), "source": "output_schema"}

        # Priority 2: module-level known schemas (works even if the tool is not mounted,
        # so describe("bash") is useful as a reference even before the first call)
        if tool_name in _KNOWN_OUTPUT_SCHEMAS:
            return {"keys": _KNOWN_OUTPUT_SCHEMAS[tool_name], "source": "known"}

        # Priority 3: tool is mounted but has no documented schema
        if tool is not None:
            return {
                "keys": [],
                "source": "unknown",
                "note": "keys not documented — call the tool and print(list(result.keys()))",
            }

        # Priority 4: not mounted, not known
        return {"error": f"unknown tool '{tool_name}'", "available": sorted(tools.keys())}

    return describe


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
        "asyncio": asyncio,  # pre-injected: asyncio.gather(), asyncio.sleep(), etc.
        "gather_limited": _gather_limited,  # pre-injected: cap concurrency, e.g. await gather_limited([...], limit=10)
        "describe": _make_describe_fn(tools),  # pre-injected: describe("bash") → {'keys': ['stdout', 'stderr', 'returncode']}
        **{name: _make_wrapper(name, tool, hooks) for name, tool in tools.items()},
    }

    # Remove unused imports in-process via ast analysis — no subprocess, no LLM.
    # Falls back to original code if the code has a SyntaxError (caught later by compile()).
    code = _remove_unused_imports(code)

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
            "tool_name": {
                "type": "string",
                "description": (
                    "For a SINGLE tool call — name of the tool to invoke directly "
                    "(faster: no Python exec overhead). Use with tool_args."
                ),
            },
            "tool_args": {
                "type": "object",
                "description": "Arguments for the single tool call (used with tool_name).",
            },
        },
        # No required — either code OR tool_name+tool_args
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
            "Two calling conventions — pick the right one:\n\n"
            "  SINGLE operation:  tool_name='bash', tool_args={'command': 'ls'}  (direct call, no exec overhead)\n"
            "  2+ operations:     code='...' with asyncio.gather()               (parallel, keeps data out of context)\n\n"
            "For the code path: all session tools are available as awaitable async functions —\n"
            "use await to call them. Use print() to return results.\n"
            "Tool functions return dicts — access values with result['key'].\n"
            "If you're unsure of a tool's keys, use print(list(result.keys())).\n\n"
            f"Available functions (already in scope — use await):\n{interfaces}\n\n"
            "Standard library (json, os, re, pathlib, etc.) is also available.\n"
            "asyncio is pre-injected — use asyncio.gather() directly without import.\n"
            "gather_limited(coros, limit=10) is also pre-injected — cap concurrent tool calls:\n"
            "  results = await gather_limited([tool(x) for x in items], limit=20)\n"
            "describe(tool_name) is also pre-injected — check a tool's return keys before using:\n"
            "  print(describe('bash'))  # → {'keys': ['stdout', 'stderr', 'returncode'], 'source': 'known'}"
        )

    async def execute(self, input_data: dict[str, Any]) -> Any:
        from amplifier_core import ToolResult  # type: ignore[import]  # peer dep

        all_tools: dict[str, Any] = self._coordinator.get("tools") or {}
        run_tools = {k: v for k, v in all_tools.items() if k != self.name}

        # Try to get real hooks; fall back to no-op
        hooks: Any = _NoOpHooks()
        with contextlib.suppress(Exception):
            candidate = self._coordinator.get("hooks")
            if candidate is not None and hasattr(candidate, "emit"):
                hooks = candidate

        # Fast path: single tool call, no exec overhead
        tool_name: str = (input_data.get("tool_name") or "").strip()
        if tool_name:
            tool = run_tools.get(tool_name)
            if tool is None:
                return ToolResult(success=False, output=f"Error: unknown tool '{tool_name}'")
            tool_args: dict[str, Any] = input_data.get("tool_args") or {}
            call_id = str(uuid.uuid4())
            await hooks.emit(
                "code_mode:tool:pre",
                {"call_id": call_id, "tool_name": tool_name, "tool_input": tool_args},
            )
            result = await tool.execute(tool_args)
            output = getattr(result, "output", None)
            if output is None:
                output = str(result)
            await hooks.emit(
                "code_mode:tool:post",
                {
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "tool_input": tool_args,
                    "tool_result": output,
                },
            )
            return ToolResult(success=True, output=output)

        # Python path: multi-step code execution
        code: str = (input_data.get("code") or "").strip()
        if not code:
            return ToolResult(
                success=False,
                output="Error: provide 'code' for multi-step execution or 'tool_name'+'tool_args' for a single call",
            )

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
