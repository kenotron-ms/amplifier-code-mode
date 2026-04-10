# tool_code_mode — Code Mode for Amplifier

You have access to a **`tool_code_mode`** tool that runs Python code in-process.
Every other tool available in this session is accessible inside that code as an
`async` function with the same name and parameters — no imports needed.

## The Core Idea: Python IS Your Pipeline

The code block is a **complete workflow orchestrator**. Python's natural control
flow expresses any pipeline shape — no separate job queue needed:

| Pattern | Python construct |
|---|---|
| Step B depends on step A | `await` sequentially |
| Steps A and B are independent | `asyncio.gather()` in parallel |
| Branch based on a result | `if / elif / else` |
| Repeat until done | `while` loop |
| Process a list of items | `for` loop or list comprehension |
| Recover from failure | `try / except` |
| Parse and transform data | `json`, `yaml`, `re`, `pathlib`, etc. |
| Parallel but max N at a time | `asyncio.Semaphore(N)` |

**The full Python standard library is available.** Import anything at the top of
your block: `json`, `yaml`, `os`, `re`, `pathlib`, `collections`, `itertools`,
`textwrap` — all fair game. This is real Python, not a sandbox.

## When to use `tool_code_mode`

**Default to this tool whenever a task needs 2 or more tool calls.** The entire
workflow executes in one LLM round-trip — no context bloat from intermediate results.

Use it when you need to:
- Chain tools where the output of one feeds the next
- Fan out to parallel work, then collect and aggregate
- Loop over a list and call a tool for each item
- Branch logic based on what a tool returns
- Parse JSON or YAML from tool output and act on it
- Run several independent operations in parallel with `asyncio.gather()`

Only call tools directly (without code_mode) for a single, isolated operation.

## Pipeline Patterns

### 1. Sequential pipeline — B depends on A

```python
# Each step uses the previous result. Just await in order.
result = await bash("gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name'")
branch = result['stdout'].strip()

commits = await bash(f"git log origin/{branch}..HEAD --oneline")
print(f"Unpushed commits on {branch}:\n{commits['stdout'] or '(none)'}")
```

### 2. Conditional branching — decide at runtime

```python
import json

check = await bash("cat pyproject.toml 2>/dev/null || echo __NOT_FOUND__")

if "__NOT_FOUND__" in check['stdout']:
    # Bootstrap a new project
    await write_file("pyproject.toml", "[project]\nname = 'myproject'\nversion = '0.1.0'\n")
    print("Created pyproject.toml")
else:
    # Parse and report
    deps = await bash("python -m pip list --format=json 2>/dev/null")
    packages = json.loads(deps['stdout']) if deps['returncode'] == 0 else []
    print(f"Project has {len(packages)} installed packages")
```

### 3. Fan-out → Fan-in — parallel independent work, then aggregate

```python
import asyncio

# Fire all reads at once — they don't depend on each other
paths = ["src/auth.py", "src/api.py", "src/models.py", "src/utils.py"]
results = await asyncio.gather(*[read_file(p) for p in paths])

# Aggregate after all complete
total = sum(r['total_lines'] for r in results)
print(f"Total: {total} lines across {len(paths)} files")
for path, r in zip(paths, results):
    print(f"  {path}: {r['total_lines']} lines")
```

### 4. Loop with accumulation — pagination, batching, retry

```python
import json

# Paginate through all GitHub issues
all_issues = []
page = 1
while True:
    result = await bash(
        f"gh issue list --limit 100 --json number,title,state,labels "
        f"--jq 'map(select(.state == \"open\"))' "
        f"2>/dev/null || echo '[]'"
    )
    batch = json.loads(result['stdout'])
    if not batch:
        break
    all_issues.extend(batch)
    if len(batch) < 100:
        break  # last page
    page += 1

# Now process the full set
by_label = {}
for issue in all_issues:
    for label in issue.get('labels', []):
        by_label.setdefault(label['name'], []).append(issue['number'])

print(f"Found {len(all_issues)} open issues")
for label, nums in sorted(by_label.items(), key=lambda x: -len(x[1]))[:5]:
    print(f"  {label}: {len(nums)} issues")
```

### 5. Full DAG — mixed sequential and parallel stages

```python
import asyncio, json

# Stage 1: fetch independent sources in parallel
repo_json, prs_json, test_output = await asyncio.gather(
    bash("gh repo view --json name,description,pushedAt"),
    bash("gh pr list --state open --json number,title,author --limit 20"),
    bash("python -m pytest tests/ -q --tb=no 2>&1 | tail -5"),
)

# Stage 2: parse (depends on stage 1 completing)
repo = json.loads(repo_json['stdout'])
prs = json.loads(prs_json['stdout'])

# Stage 3: conditional fan-out (depends on stage 2 data)
if prs:
    pr_reviews = await asyncio.gather(*[
        bash(f"gh pr view {p['number']} --json reviewDecision,reviews")
        for p in prs[:5]  # cap to avoid rate limits
    ])
    review_data = [json.loads(r['stdout']) for r in pr_reviews]
    approved = sum(1 for r in review_data if r.get('reviewDecision') == 'APPROVED')
else:
    approved = 0

# Final: synthesize
print(f"Repo: {repo['name']}")
print(f"Open PRs: {len(prs)} ({approved} approved)")
print(f"Tests:\n{test_output['stdout']}")
```

### 6. Error recovery — fallback chains, safe defaults

```python
import json

# Try config sources in priority order
config = None
for path in ["config.local.json", "config.json", ".config/app.json"]:
    result = await bash(f"cat {path} 2>/dev/null")
    if result['returncode'] == 0 and result['stdout'].strip():
        try:
            config = json.loads(result['stdout'])
            print(f"Loaded config from {path}")
            break
        except json.JSONDecodeError:
            print(f"Warning: {path} is not valid JSON, skipping")

if config is None:
    config = {"debug": False, "port": 8080, "env": "development"}
    print("Using default config")

print(f"Config: env={config['env']}, port={config['port']}")
```

### 7. Data transformation — parse, reshape, write back

```python
import json, yaml

# Read a bundle manifest, add a new tool, write it back
result = await read_file("bundle.md")
content = result['content']

# Extract YAML frontmatter between --- delimiters
import re
match = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
if match:
    frontmatter = yaml.safe_load(match.group(1))
    
    # Add the new tool if not already present
    tools = frontmatter.setdefault('tools', [])
    tool_modules = [t if isinstance(t, str) else t.get('module', '') for t in tools]
    
    if 'tool-new-capability' not in tool_modules:
        tools.append({'module': 'tool-new-capability'})
        
        # Reconstruct the file
        new_frontmatter = yaml.dump(frontmatter, default_flow_style=False).strip()
        new_content = f"---\n{new_frontmatter}\n---" + content[match.end():]
        await write_file("bundle.md", new_content)
        print("Added tool-new-capability to bundle.md")
    else:
        print("tool-new-capability already present")
```

### 8. Controlled concurrency — limit how many run at once

Plain `asyncio.gather()` over a large list launches **everything simultaneously**.
That can exceed API rate limits, overwhelm file descriptors, or produce
thundering-herd pressure. Use `asyncio.Semaphore` to cap concurrent operations
while still processing the whole list in one block.

**Semaphore (recommended)** — always keeps exactly N running; as one finishes the
next starts immediately:

```python
import asyncio

async def run_limited(items, tool_fn, limit=10):
    sem = asyncio.Semaphore(limit)

    async def one(item):
        async with sem:
            return await tool_fn(item)

    return await asyncio.gather(*[one(item) for item in items])

# Example: read 500 files, but only 20 at a time
ls = await bash("find src/ -name '*.py' -not -path '*/__pycache__/*'")
py_files = [f.strip() for f in ls['stdout'].splitlines() if f.strip()]

results = await run_limited(
    py_files,
    lambda f: read_file(f),
    limit=20,
)
print(f"Read {len(results)} files")
```

Or inline, without a helper function:

```python
import asyncio

sem = asyncio.Semaphore(5)  # max 5 GitHub API calls at once

async def fetch_pr(number):
    async with sem:
        return await bash(f"gh pr view {number} --json title,reviews,files")

pr_numbers = list(range(1, 51))  # PRs 1–50
results = await asyncio.gather(*[fetch_pr(n) for n in pr_numbers])
print(f"Fetched {len(results)} PRs with max 5 concurrent")
```

**Batching (simpler, less efficient)** — waits for the slowest item in each
batch before starting the next. Fine for small lists or when ordering matters:

```python
import asyncio

items = list(range(100))
batch_size = 10
results = []

for i in range(0, len(items), batch_size):
    batch = items[i : i + batch_size]
    batch_results = await asyncio.gather(*[
        bash(f"process {item}") for item in batch
    ])
    results.extend(batch_results)
    print(f"Batch {i//batch_size + 1} done")

print(f"Processed {len(results)} items")
```

**Semaphore vs batching:**

| | Semaphore | Batching |
|---|---|---|
| Throughput | Higher — next item starts as soon as a slot frees | Lower — waits for slowest in batch |
| Simplicity | Requires a small helper | Straightforward loop |
| Use when | Rate limits, large lists, maximum speed | Ordered processing, simple scripts |

## Parallel execution with asyncio.gather()

Use `asyncio.gather()` **only when calls are truly independent** — no result from
one is needed by another before the gather completes. If B needs A's output,
just `await` them sequentially. For large lists, use a **Semaphore** (pattern 8)
to limit concurrency.

```python
import asyncio

# All fire simultaneously — one LLM round-trip total
file_result, test_result, pkg_result = await asyncio.gather(
    read_file("src/main.py"),
    bash("pytest tests/ -q 2>&1"),
    web_fetch("https://pypi.org/pypi/mypackage/json"),
)

import json
pkg = json.loads(pkg_result['content'])
print(f"File: {file_result['total_lines']} lines")
print(f"Tests:\n{test_result['stdout']}")
print(f"Latest version: {pkg['info']['version']}")
```

**The scatter pattern** — fan out over a dynamic list:

```python
import asyncio

# Get list first, then process all items in parallel
ls = await bash("find src/ -name '*.py' -not -path '*/__pycache__/*'")
py_files = [f.strip() for f in ls['stdout'].splitlines() if f.strip()]

# Check all files for type errors in parallel
diag_results = await asyncio.gather(*[
    LSP(operation="diagnostics", file_path=f)
    for f in py_files
])

# Collect only files with errors
errors = [
    (path, r['diagnostics'])
    for path, r in zip(py_files, diag_results)
    if r.get('diagnostics')
]
print(f"{len(errors)}/{len(py_files)} files have diagnostics")
for path, diags in errors[:5]:
    print(f"\n{path}:")
    for d in diags[:3]:
        print(f"  L{d['range']['start']['line']}: {d['message']}")
```

## Tool return shapes

All tool functions return **dicts**, not strings. Common shapes:

| Tool | Key return fields |
|---|---|
| `bash` | `stdout`, `stderr`, `returncode` |
| `read_file` | `content`, `file_path`, `total_lines`, `lines_read`, `offset` |
| `write_file` | `file_path`, `bytes` |
| `edit_file` | `file_path`, `success` |
| `web_fetch` | `url`, `content`, `content_type`, `truncated`, `total_bytes` |
| `glob` | `files`, `total_files` |
| `grep` | `matches`, `total_matches` |
| `LSP` | operation-specific (see LSP docs) |

If you're unsure of a tool's keys: `print(list(result.keys()))`.

## Rules inside the code block

1. **All session tools are in scope** — call them with `await tool_name(param=value, ...)`.
   Do not import them; they are already in the namespace.
2. **Use `print()`** for the final result you want to see. The last expression is NOT auto-printed.
3. **Regular Python imports work** — `import json`, `import yaml`, `import os`,
   `from pathlib import Path`, `from collections import defaultdict`, etc.
4. **Handle errors** — wrap uncertain operations in `try/except`.
5. **One logical block** — put the complete workflow in a single code argument.
6. **Sequential by default** — reach for `asyncio.gather()` only when you've confirmed
   operations are independent of each other.

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
| Pipelines expressed as separate assistant turns | Pipelines expressed as natural Python control flow |
| Loops require repeated tool invocations | A `while` loop handles pagination in one block |
| No data reshaping between calls | Full stdlib available between every step |
