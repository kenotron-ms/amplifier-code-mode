"""
Code Mode Orchestrator for Amplifier.

Replaces native tool-call format with executable Python code generation.
The LLM writes Python code that calls tool functions, which is executed in a
sandboxed subprocess connected back to real tools via a lightweight TCP bridge.

Architecture:
    LLM  ──→  writes Python code  ──→  subprocess sandbox
                                              ↕  TCP bridge (127.0.0.1:random)
                                        actual tool execution
                                              │
                                        stdout  ──→  LLM context (final result only)

Why Code Mode is better:
- LLMs have vastly more training data on real code than on synthetic tool-call schemas.
- Multiple tool calls happen inside one code block; only the final aggregated output
  comes back to the LLM context — reducing token usage and round-trips.
- Full Python logic (loops, conditions, data processing) between tool calls.

Reference: https://blog.cloudflare.com/code-mode/
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import tempfile
import textwrap
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TCP bridge server
# ---------------------------------------------------------------------------


class _ToolBridgeServer:
    """
    Minimal async TCP server that routes subprocess tool calls to real Amplifier tools.

    Line-delimited JSON protocol:
        Request  (subprocess → server):  {"id": "1", "tool": "bash", "input": {...}}\\n
        Response (server → subprocess):  {"id": "1", "result": "...", "error": null}\\n

    A new TCP connection is opened per tool call (keeps the protocol simple).
    """

    def __init__(self, tools: dict[str, Any], hooks: Any) -> None:
        self._tools = tools
        self._hooks = hooks
        self._server: asyncio.Server | None = None
        self.port: int = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client,
            host="127.0.0.1",
            port=0,  # let the OS pick a free port
        )
        self.port = self._server.sockets[0].getsockname()[1]
        logger.debug("Code Mode bridge server started on port %d", self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        call_id: str | None = None
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    req: dict[str, Any] = json.loads(raw.decode())
                    call_id = req.get("id")
                    result = await self._dispatch(req)
                    resp = json.dumps({"id": call_id, "result": result, "error": None})
                except Exception as exc:  # noqa: BLE001
                    resp = json.dumps({"id": call_id, "result": None, "error": str(exc)})
                writer.write((resp + "\n").encode())
                await writer.drain()
        finally:
            writer.close()

    async def _dispatch(self, req: dict[str, Any]) -> Any:
        tool_name: str = req["tool"]
        tool_input: dict[str, Any] = req.get("input", {})

        tool = self._tools.get(tool_name)
        if tool is None:
            raise ValueError(f"Unknown tool: {tool_name!r}")

        await self._hooks.emit("tool:pre", {
            "tool_name": tool_name,
            "tool_input": tool_input,
        })

        result = await tool.execute(tool_input)
        output: str = getattr(result, "output", None) or str(result)

        await self._hooks.emit("tool:post", {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_result": result,
        })

        return output


# ---------------------------------------------------------------------------
# Code generation helpers
# ---------------------------------------------------------------------------

# _BRIDGE_MODULE_TEMPLATE is written as a .py file into the temp sandbox dir.
# It is formatted with .format(tool_functions=...) — all literal { } are doubled.
_BRIDGE_MODULE_TEMPLATE = '''\
"""Amplifier Code Mode tool bridge — auto-generated, do not edit."""
import asyncio
import json
import os

_HOST = "127.0.0.1"
_PORT = int(os.environ["AMPLIFIER_BRIDGE_PORT"])
_CALL_ID = 0


async def _call_tool(tool_name: str, **kwargs: object) -> str:
    global _CALL_ID
    _CALL_ID += 1
    call_id = str(_CALL_ID)

    reader, writer = await asyncio.open_connection(_HOST, _PORT)
    try:
        payload = json.dumps({{"id": call_id, "tool": tool_name, "input": kwargs}}) + "\\n"
        writer.write(payload.encode())
        await writer.drain()
        raw = await reader.readline()
        resp = json.loads(raw.decode())
        if resp.get("error"):
            err = resp["error"]
            raise RuntimeError(f"Tool {{tool_name!r}} failed: {{err}}")
        return str(resp["result"] or "")
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=5)
        except asyncio.TimeoutError:
            pass


{tool_functions}
'''

# Wrapper script run by the subprocess (imports from the bridge module).
_WRAPPER_TEMPLATE = '''\
"""Code Mode sandbox wrapper — auto-generated."""
import sys
import asyncio
import traceback
from _amplifier_bridge import {tool_imports}


async def _user_code() -> None:
{indented_code}


async def _main() -> None:
    try:
        await _user_code()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


asyncio.run(_main())
'''

# Same but without tool imports (used when the session has no tools).
_WRAPPER_TEMPLATE_NO_TOOLS = '''\
"""Code Mode sandbox wrapper (no tools) — auto-generated."""
import sys
import asyncio
import traceback


async def _user_code() -> None:
{indented_code}


async def _main() -> None:
    try:
        await _user_code()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


asyncio.run(_main())
'''


def _json_type_to_python(json_type: str) -> str:
    """Map a JSON Schema type string to a Python type annotation string."""
    return {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "array": "list",
        "object": "dict",
    }.get(json_type, "Any")


def _build_params(schema: dict[str, Any]) -> list[tuple[str, str, bool]]:
    """
    Return ``(name, python_type, is_required)`` tuples for a JSON Schema object.
    """
    props: dict[str, Any] = schema.get("properties", {}) or {}
    required: list[str] = schema.get("required", []) or []
    return [
        (pname, _json_type_to_python(pschema.get("type", "string")), pname in required)
        for pname, pschema in props.items()
    ]


def _generate_tool_interfaces(tools: dict[str, Any]) -> str:
    """
    Convert Amplifier tool instances → Python async function signatures.

    Shown to the LLM inside the system prompt so it knows what functions
    are available to call in its generated code.
    """
    if not tools:
        return "# No tools available in this session."

    lines: list[str] = [
        "# Available tool functions (all async, already in scope — do not import):",
        "",
    ]
    for name, tool in tools.items():
        schema: dict[str, Any] = getattr(tool, "input_schema", {}) or {}
        description: str = (getattr(tool, "description", "") or "").strip()
        params = _build_params(schema)

        param_strs = [
            f"{pname}: {py_type}" + ("" if req else " = None")
            for pname, py_type, req in params
        ]
        short_desc = (description.split("\n")[0][:120]) if description else f"Call the {name} tool."
        lines += [
            f"async def {name}({', '.join(param_strs)}) -> str:",
            f'    """{short_desc}"""',
            "    ...",
            "",
        ]
    return "\n".join(lines)


def _generate_bridge_module(tools: dict[str, Any]) -> str:
    """Generate the ``_amplifier_bridge.py`` source for the sandbox subprocess."""
    func_parts: list[str] = []
    for name, tool in tools.items():
        schema: dict[str, Any] = getattr(tool, "input_schema", {}) or {}
        description: str = (getattr(tool, "description", "") or "").strip()
        params = _build_params(schema)

        param_strs = [
            f"{pname}: {py_type}" + ("" if req else " = None")
            for pname, py_type, req in params
        ]
        kwarg_fwds = [f"{pname}={pname}" for pname, _, _ in params]
        kwargs_str = (", " + ", ".join(kwarg_fwds)) if kwarg_fwds else ""
        short_desc = (description.split("\n")[0][:120]) if description else f"Call the {name} tool."

        func_parts.append(
            f"async def {name}({', '.join(param_strs)}) -> str:\n"
            f'    """{short_desc}"""\n'
            f"    return await _call_tool({name!r}{kwargs_str})\n"
        )

    return _BRIDGE_MODULE_TEMPLATE.format(tool_functions="\n".join(func_parts))


def _build_system_prompt(tool_interfaces: str) -> str:
    return f"""\
You are operating in **Code Mode**.

Instead of calling tools directly, write a single executable Python code block.
Your code runs in a sandboxed subprocess; only the printed output is returned.

{tool_interfaces}

## Rules
1. Write exactly **one** ```python ... ``` block when you need tools.
2. Tool functions above are already in scope — do **not** import them.
3. All functions are `async` — use `await` when calling them.
4. Use `print()` to output the result you want to see.
5. When you have all information you need, respond in **plain text** (no code block).
6. Add brief comments on non-obvious steps.

## Example
```python
# Combine two tool calls in one pass
listing = await bash("ls -la src/")
content = await read_file("src/main.py")
print(f"Files:\\n{{listing}}\\n\\nMain module:\\n{{content}}")
```
"""


_CODE_BLOCK_RE = re.compile(
    r"```(?:python|py)\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def _extract_python_code(text: str) -> str | None:
    """Return the first Python code block in *text*, or ``None`` if absent."""
    m = _CODE_BLOCK_RE.search(text)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Sandbox execution
# ---------------------------------------------------------------------------


async def _execute_code(
    code: str,
    tools: dict[str, Any],
    hooks: Any,
    timeout: int = 60,
) -> str:
    """
    Execute LLM-generated Python in a subprocess with a TCP tool bridge.

    Steps:
    1. Start a ``_ToolBridgeServer`` bound to a random localhost port.
    2. Write ``_amplifier_bridge.py`` (with real tool bridge functions) to a temp dir.
    3. Wrap the user code in an ``async def _user_code()`` scaffold and run it.
    4. Route every tool call back through the bridge server to actual Amplifier tools.
    5. Return subprocess stdout as the result; stderr is captured for error messages.
    """
    bridge = _ToolBridgeServer(tools=tools, hooks=hooks)
    await bridge.start()

    try:
        with tempfile.TemporaryDirectory(prefix="amplifier_code_mode_") as tmpdir:
            # --- write bridge module ---
            if tools:
                bridge_src = _generate_bridge_module(tools)
                with open(os.path.join(tmpdir, "_amplifier_bridge.py"), "w", encoding="utf-8") as fh:
                    fh.write(bridge_src)

            # --- build wrapper script ---
            indented = textwrap.indent(code, "    ")
            if not indented.strip():
                indented = "    pass"

            if tools:
                wrapper_src = _WRAPPER_TEMPLATE.format(
                    tool_imports=", ".join(tools.keys()),
                    indented_code=indented,
                )
            else:
                wrapper_src = _WRAPPER_TEMPLATE_NO_TOOLS.format(indented_code=indented)

            with open(os.path.join(tmpdir, "_script.py"), "w", encoding="utf-8") as fh:
                fh.write(wrapper_src)

            # --- run subprocess ---
            env = {**os.environ, "AMPLIFIER_BRIDGE_PORT": str(bridge.port)}
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                os.path.join(tmpdir, "_script.py"),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmpdir,
                env=env,
            )

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=float(timeout),
                )
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return f"Error: code execution timed out after {timeout}s"

            stdout = stdout_b.decode(errors="replace").strip()
            stderr = stderr_b.decode(errors="replace").strip()

            if proc.returncode != 0:
                msg = f"Code execution failed (exit {proc.returncode})"
                if stderr:
                    msg += f":\n{stderr}"
                return msg

            return stdout or "(no output)"

    finally:
        await bridge.stop()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class CodeModeOrchestrator:
    """
    Code Mode orchestrator — implements the Amplifier ``Orchestrator`` protocol.

    Key differences from a standard loop orchestrator:

    * Tools are presented to the LLM as Python async function signatures
      (not JSON schemas).
    * Provider is called with ``tools=[]`` — no native tool-call format.
    * LLM responses are parsed for ``python`` code blocks.
    * Code blocks are executed in a sandboxed subprocess; only the final
      stdout comes back into the context (not each intermediate tool result).

    Configuration (passed via ``session.orchestrator.config`` in bundle YAML):

        max_iterations (int, default 10): maximum LLM ↔ code-exec turns.
        timeout (int, default 60):        subprocess timeout in seconds.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._max_iterations: int = int(cfg.get("max_iterations", 10))
        self._timeout: int = int(cfg.get("timeout", 60))

    # ------------------------------------------------------------------
    # Orchestrator protocol
    # ------------------------------------------------------------------

    async def execute(
        self,
        prompt: str,
        context: Any,
        providers: dict[str, Any],
        tools: dict[str, Any],
        hooks: Any,
        **kwargs: Any,
    ) -> str:
        """Entry point — called by the coordinator for each user turn."""
        await hooks.emit("execution:start", {"prompt": prompt})
        try:
            return await self._run(prompt, context, providers, tools, hooks)
        except asyncio.CancelledError:
            await hooks.emit("execution:end", {"response": "", "status": "cancelled"})
            raise
        except Exception:
            await hooks.emit("execution:end", {"response": "", "status": "error"})
            raise

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _run(
        self,
        prompt: str,
        context: Any,
        providers: dict[str, Any],
        tools: dict[str, Any],
        hooks: Any,
    ) -> str:
        # Peer dep — imported at runtime so tests don't need amplifier_core installed.
        from amplifier_core.models import ChatRequest  # type: ignore[import]

        tool_interfaces = _generate_tool_interfaces(tools)
        system_injection = _build_system_prompt(tool_interfaces)

        await context.add_message({"role": "user", "content": prompt})
        await context.add_message({"role": "system", "content": system_injection})

        provider_name: str = next(iter(providers))
        provider = providers[provider_name]

        final_response = ""
        iteration = 0

        for iteration in range(self._max_iterations):
            messages = await context.get_messages_for_request()

            await hooks.emit("provider:request", {
                "provider": provider_name,
                "messages": messages,
            })

            # KEY: pass no tool schemas — the LLM writes code instead of tool_use blocks
            response = await provider.complete(ChatRequest(messages=messages, tools=[]))

            await hooks.emit("provider:response", {
                "provider": provider_name,
                "response": response,
            })

            response_text: str = getattr(response, "content", "") or ""
            await context.add_message({"role": "assistant", "content": response_text})

            code = _extract_python_code(response_text)
            if code is None:
                # Pure text response — no more tool calls needed; we're done.
                final_response = response_text
                break

            await hooks.emit("loop-code:code_generated", {
                "code": code,
                "iteration": iteration,
            })

            sandbox_result = await _execute_code(
                code=code,
                tools=tools,
                hooks=hooks,
                timeout=self._timeout,
            )

            await hooks.emit("loop-code:sandbox_result", {
                "result": sandbox_result,
                "iteration": iteration,
            })

            # Only the final sandbox output enters context — not each intermediate step.
            await context.add_message({
                "role": "tool",
                "tool_call_id": f"code-exec-{iteration}",
                "content": f"Code execution output:\n{sandbox_result}",
            })

            # Keep track of the last LLM response in case the loop ends here.
            final_response = response_text

        await hooks.emit("orchestrator:complete", {
            "orchestrator": "loop-code",
            "turn_count": iteration + 1,
            "status": "success",
        })
        await hooks.emit("execution:end", {
            "response": final_response,
            "status": "completed",
        })
        return final_response


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------


async def mount(
    coordinator: Any,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Mount the Code Mode orchestrator into the Amplifier coordinator.

    Called automatically by the module loader when ``loop-code`` is listed
    in a bundle's ``session.orchestrator`` block.
    """
    orchestrator = CodeModeOrchestrator(config=config)
    await coordinator.mount("session", orchestrator, name="orchestrator")

    # Register custom observability events (optional — may not be available in all versions).
    with contextlib.suppress(AttributeError):
        coordinator.register_contributor(
            "observability.events",
            "loop-code",
            lambda: ["loop-code:code_generated", "loop-code:sandbox_result"],
        )

    logger.info("loop-code: Code Mode orchestrator mounted")
    return {
        "name": "loop-code",
        "version": "0.1.0",
        "provides": ["orchestrator"],
    }
