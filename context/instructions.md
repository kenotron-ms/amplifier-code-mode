    # tool_code_mode — Code Mode for Amplifier

    You have access to a **`tool_code_mode`** tool that runs Python code in-process.
    Every other tool available in this session is accessible inside that code as an
    `async` function with the same name and parameters — no imports needed.

    ## When to use `tool_code_mode`

    Use it whenever a task requires **two or more tool calls** — especially when you'd
    need to pass the result of one call into another, loop over results, filter data,
    or aggregate multiple outputs into one answer.

    Don't use it for a single, simple tool call — call the tool directly instead.

    ## How to write the code

    ```python
    # Example: find Python files and read the most recently modified one
    files_output = await bash("find src/ -name '*.py' | sort -r | head -5")
    files = [f.strip() for f in files_output.splitlines() if f.strip()]

    if files:
        content = await read_file(files[0])
        print(f"Most recent: {files[0]}\n\n{content}")
    else:
        print("No Python files found.")
    ```

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
    required vs optional params, and docstrings. Read the stubs — they tell you exactly
    what to pass.

    ## Why this is better than sequential tool calls

    | Sequential tool calls | `tool_code_mode` |
    |---|---|
    | N tool calls = N round-trips through the LLM | All N calls happen inside one execution |
    | Each intermediate result appears in context | Only the final aggregated result appears |
    | Logic between calls requires another LLM turn | Any Python logic runs instantly |
    