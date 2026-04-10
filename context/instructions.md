# tool_code_mode — Code Mode for Amplifier

You have access to a **`tool_code_mode`** tool that runs Python code in-process.
Every other tool available in this session is accessible inside that code as an
`async` function with the same name and parameters — no imports needed.

## When to use `tool_code_mode`

**Default to this tool whenever a task needs 2 or more tool calls.** One
`asyncio.gather()` inside a single code_mode call replaces N sequential LLM
round-trips — faster execution and less context bloat.

Use it when you need to:
- Call multiple tools and aggregate their output
- Pass the result of one tool into another
- Loop over results, filter data, or apply any Python logic between calls
- Run several independent operations in parallel with `asyncio.gather()`

Only call tools directly (without code_mode) for a single, isolated operation.

## How to write the code

```python
# Example: find Python files and read the most recently modified one
# Note: bash() returns a dict — access stdout with result['stdout']
files_result = await bash("find src/ -name '*.py' | sort -r | head -5")
files = [f.strip() for f in files_result['stdout'].splitlines() if f.strip()]

if files:
    content_result = await read_file(files[0])
    print(f"Most recent: {files[0]}\n\n{content_result['content']}")
else:
    print("No Python files found.")
```

## Parallel execution with asyncio.gather()

When operations are independent, run them concurrently:

```python
import asyncio

# All three fire simultaneously — one LLM round-trip total
file_result, test_result, pkg_result = await asyncio.gather(
    read_file("src/main.py"),
    bash("pytest tests/ -q 2>&1"),
    web_fetch("https://pypi.org/pypi/mypackage/json"),
)

print("File lines:", file_result['total_lines'])
print("Tests:", test_result['stdout'])

import json
pkg = json.loads(pkg_result['content'])
print("Latest version:", pkg['info']['version'])
```

## Tool return shapes

All tool functions return **dicts**, not strings. Common shapes:

| Tool | Key return fields |
|---|---|
| `bash` | `stdout`, `stderr`, `returncode` |
| `read_file` | `content`, `file_path`, `total_lines`, `lines_read`, `offset` |
| `write_file` | `file_path`, `bytes` |
| `web_fetch` | `url`, `content`, `content_type`, `truncated`, `total_bytes` |

If you're unsure of a tool's keys: `print(list(result.keys()))`.

## Rules inside the code block

1. **All session tools are in scope** — call them with `await tool_name(param=value, ...)`.
   Do not import them; they are already in the namespace.
2. **Use `print()`** for the final result you want to see. The last expression is NOT auto-printed.
3. **Regular Python imports** (`import json`, `import os`, etc.) work normally.
4. **Handle errors** — wrap uncertain operations in `try/except`.
5. **One logical block** — put the complete workflow in a single code argument.

## The tool description shows exact signatures

The `tool_code_mode` description dynamically lists every available function as a
Python `async def` stub with full parameter types (including `Literal[...]` enums),
required vs optional params, and `-> dict` return types. Read the stubs — they tell
you exactly what to pass.

## Why this is better than sequential tool calls

| Sequential tool calls | `tool_code_mode` |
|---|---|
| N tool calls = N round-trips through the LLM | All N calls happen inside one execution |
| Each intermediate result appears in context | Only the final aggregated result appears |
| Logic between calls requires another LLM turn | Any Python logic runs instantly |
| Independent calls run one at a time | `asyncio.gather()` runs them in parallel |
