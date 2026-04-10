    ---
    bundle:
      name: code-mode
      version: 1.0.0
      description: |
        Code Mode — adds a tool_code_mode tool so the LLM can write Python code
        to orchestrate multi-tool workflows instead of making individual tool calls.
        No orchestrator swap required. Based on https://blog.cloudflare.com/code-mode/

    includes:
      - bundle: git+https://github.com/microsoft/amplifier-foundation@main

    # Add tool_code_mode on top of foundation's defaults.
    # No orchestrator change — works with loop-basic, loop-streaming, or any orchestrator.
    tools:
      - module: tool-code-mode
        source: ./modules/tool-code-mode
        config:
          timeout: 60          # execution timeout in seconds (default: 60)
    ---

    # Code Mode

    @code-mode:context/instructions.md
    