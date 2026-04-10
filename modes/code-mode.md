---
mode:
  name: code-mode
  description: Force all tool orchestration through tool_code_mode — individual tool calls are blocked
  shortcut: code-mode

  tools:
    safe:
      - tool_code_mode
      - todo
      - mode

  default_action: block
---

CODE MODE: All tool calls must go through `tool_code_mode`. Direct tool calls are blocked.

You have access to every tool in this session — but only through the `tool_code_mode` Python executor. Do not call `bash`, `read_file`, `write_file`, `grep`, `glob`, `delegate`, `recipes`, or any other tool directly. Write Python code that calls them instead.

## Why

Every direct tool call is a round-trip through the LLM. `tool_code_mode` collapses N tool calls into one execution — parallel where possible, sequential where required — with Python logic in between. It is strictly faster and uses less context.

## How to Work

**Single operation:**
```python
tool_code_mode(tool_name="read_file", tool_args={"file_path": "src/main.py"})
```

**Multiple operations (parallel):**
```python
tool_code_mode(code="""
import asyncio
a, b = await asyncio.gather(
    read_file("src/main.py"),
    bash("pytest tests/ -q 2>&1"),
)
print(a['content'])
print(b['stdout'])
""")
```

**With logic between calls:**
```python
tool_code_mode(code="""
result = await bash("find src/ -name '*.py' | sort -r | head -5")
files = [f.strip() for f in result['stdout'].splitlines() if f.strip()]
contents = await asyncio.gather(*[read_file(f) for f in files])
for f, c in zip(files, contents):
    print(f"### {f}")
    print(c['content'][:500])
""")
```

## Rules

1. **Never call tools directly** — always go through `tool_code_mode`
2. **Use `asyncio.gather()` for independent operations** — don't serialize what can run in parallel
3. **Use `print()` for output** — the last expression is NOT auto-printed
4. **All session tools are already in scope** — no imports needed to use them

Use `/mode off` to return to normal tool access.
