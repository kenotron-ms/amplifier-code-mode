# amplifier-bundle-code-mode

An Amplifier bundle that enables **Code Mode** — the LLM writes Python code to
orchestrate multi-tool workflows instead of making individual tool calls.

References: [Cloudflare blog post](https://blog.cloudflare.com/code-mode/) ·
[ryx2/claude-codemode](https://github.com/ryx2/claude-codemode)

---

## Two Flavours

| | `execute_python` **tool** (default) | `loop-code` **orchestrator** (advanced) |
|---|---|---|
| Integration | Add one tool | Swap orchestrator |
| Works with | Any orchestrator | Replaces orchestrator |
| LLM uses | `execute_python(code=...)` tool call | Free-form `python` code blocks |
| Adoption friction | **None** | High |
| Module | `modules/tool-execute-python/` | `modules/loop-code/` |

**Start with the tool.** Only switch to the orchestrator if you need the LLM to
write pure free-form code blocks without a wrapping tool call.

---

## Quick Start — Tool (Zero Friction)

### Add to any existing bundle

```yaml
tools:
  - module: tool-execute-python
    source: git+https://github.com/your-org/amplifier-bundle-code-mode@main#subdirectory=modules/tool-execute-python
```

Or use as a standalone bundle:

```yaml
bundle: git+https://github.com/your-org/amplifier-bundle-code-mode@main
```

### How it works

```
LLM calls: execute_python(code="...")
                │
                ▼
        subprocess sandbox (temp dir)
        ├─ _amplifier_bridge.py written at runtime
        │  (every session tool available as async function)
        ├─ code runs inside async def _user_code()
        └─ each await tool_fn() → TCP bridge → real Amplifier tool
                │
                ▼
           stdout → ToolResult → LLM context
```

### What the LLM writes

```python
# Find files and read the most relevant one — all in one tool call
files = await bash("find src/ -name '*.py' | sort -r | head -5")
names = [f.strip() for f in files.splitlines() if f.strip()]

if names:
    content = await read_file(names[0])
    print(f"Most recent: {names[0]}\n\n{content}")
else:
    print("No Python files found.")
```

---

## Advanced — Orchestrator (Full Code Mode)

The `loop-code` orchestrator replaces the session's default loop entirely. The
LLM **never** sees native tool schemas — it receives Python function signatures
and writes free-form code blocks instead of making tool_use calls.

```yaml
session:
  orchestrator:
    module: loop-code
    source: ./modules/loop-code
    config:
      max_iterations: 10
      timeout: 60
```

Use this when:
- The LLM should *always* write code, never native tool_use blocks
- You want the simplest possible provider call (`tools=[]`)
- Portability across providers matters (no schema variations)

---

## Repository Layout

```
code-mode/
├── bundle.md                          # Standalone bundle (adds execute_python)
├── behaviors/
│   └── code-mode.yaml                 # Composable behavior (add to any bundle)
├── agents/
│   └── code-executor.md               # Specialist agent for multi-tool work
├── context/
│   └── instructions.md                # When/how to use execute_python
└── modules/
    ├── tool-execute-python/           # PRIMARY: low-friction tool approach
    │   ├── pyproject.toml
    │   └── amplifier_module_tool_execute_python/
    │       └── __init__.py            # TCP bridge + tool + mount()
    └── loop-code/                     # ADVANCED: full Code Mode orchestrator
        ├── pyproject.toml
        └── amplifier_module_loop_code/
            └── __init__.py            # TCP bridge + orchestrator + mount()
```

## Running Tests

```bash
# Primary tool module (21 tests)
cd modules/tool-execute-python && pip install -e "." pytest pytest-asyncio && pytest

# Advanced orchestrator module (26 tests)
cd modules/loop-code && pip install -e "." pytest pytest-asyncio && pytest
```

## Security Note

The sandbox subprocess uses `sys.executable` with access to the host filesystem.
The TCP bridge prevents the subprocess from accessing Python objects in the parent
process directly — it can only call tools via the bridge protocol. For stronger
isolation, replace `_execute_code()` with a container-based executor.
