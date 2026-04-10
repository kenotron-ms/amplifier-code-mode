---
bundle:
  name: code-mode-dev
  version: 0.1.0
  description: |
    Dev bundle for working on amplifier-bundle-code-mode.
    Loads tool_code_mode from the local source tree so you can demo and iterate
    against the live module without publishing anything.

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main

tools:
  - module: tool-code-mode
    source: ../modules/tool-code-mode
    config:
      timeout: 60

agents:
  include:
    - code-mode-dev:agents/bundle-dev
---

# code-mode dev session

You are working on **amplifier-bundle-code-mode** — an Amplifier bundle that
gives the LLM a `tool_code_mode` tool for Code Mode workflows (LLM writes Python
to batch multi-tool operations instead of calling each tool separately).

## tool_code_mode is live

The tool is loaded from `modules/tool-code-mode/` in this repo.
Foundation tools (`bash`, `read_file`, `write_file`, `web_fetch`, `search`) are
all bridged into code running inside it.

Try it yourself right now — ask me:
- "Run the test suite and show me the results"
- "Find all Python files in the modules directory and count their lines"
- "Read pyproject.toml from both modules and compare their dependencies"

## Project map

```
code-mode/
├── bundle.md                              ← distributable bundle (don't edit during dev)
├── behaviors/code-mode.yaml               ← composable behavior
├── context/instructions.md               ← LLM instructions for using tool_code_mode
├── agents/code-executor.md               ← specialist agent
└── modules/
    ├── tool-code-mode/                ← ★ what's loaded in this session
    │   ├── pyproject.toml
    │   ├── amplifier_module_tool_code_mode/
    │   │   └── __init__.py              ← CodeModeTool + mount()
    │   └── tests/test_mount.py          ← tests
    └── loop-code/                        ← advanced: full Code Mode orchestrator
        ├── pyproject.toml
        ├── amplifier_module_loop_code/
        │   └── __init__.py
        └── tests/test_mount.py
```

## Running tests

```bash
# Primary tool (runs in ~1s)
cd modules/tool-code-mode && pytest tests/ -q

# Orchestrator
cd modules/loop-code && pytest tests/ -q
```

## Key design decisions (context for dev)

- `tool_code_mode.description` is a **dynamic @property** — reads `coordinator.get("tools")`
  at request time and injects full Python async stubs (with `Literal[...]` types,
  parameter descriptions, nested schema hints), so the LLM always sees exactly
  what's available in the session (analogous to Cloudflare's TypeScript declarations)
- Tool bridge uses **in-process exec()** — tools are injected directly as async wrappers
  into the exec() namespace, no subprocess or TCP bridge
- `coordinator.get("tools")` is the stable Amplifier API for discovering mounted tools
  at runtime without an orchestrator swap
