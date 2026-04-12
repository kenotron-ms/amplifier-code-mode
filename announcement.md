TITLE: [FEATURE] tool_code_mode Python pipelines (20x less context, zero drift)

---

Multi-stage tool work now runs as a Python program — 1 result instead of 40 messages, no LLM roundtrips between stages, no orchestrator drift.

What changed:
• gather_limited(coros, limit=N) pre-injected helper — controlled concurrency in one line instead of 8+ lines of Semaphore boilerplate
• In-process unused import removal via AST — generated code runs cleanly without subprocess or ruff at runtime (8 new tests)
• 8 named pipeline patterns documented — sequential, conditional, fan-out/fan-in, pagination, full DAG, error recovery, data transformation, controlled concurrency

Why it matters: parallel tool calls dump all N raw results into context (20 tool_use + 20 tool_result = 40 messages). tool_code_mode filters in Python before returning (1 + 1 = 2). Dependent stages stay inside the program — no extra LLM calls. And the logic runs exactly as written. Ken had a run where the LLM explored every .md in a vault for 17 minutes. A program doesn't do that.

Try it: In any session with the code-mode bundle, use tool_code_mode — e.g., results = await gather_limited([read_file(f) for f in files], limit=5). Only the final print() output enters context.

More info:
• Repo: https://github.com/kenotron-ms/amplifier-code-mode
• Commits: 58acd33, c4bb641, 7866f5c, ac7bb18, a7949ef
• Key files: modules/tool-code-mode/__init__.py, context/instructions.md
