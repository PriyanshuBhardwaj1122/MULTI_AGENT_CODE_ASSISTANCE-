"""
agents/static_analysis.py — Static Analysis Agent.

WHAT THIS AGENT LOOKS FOR:
    Correctness and logic errors — things that make code WRONG, not just ugly.
    - Unused variables / imports (F401, F841) — often a sign of refactoring left incomplete
    - Unreachable code — logic after return/raise, impossible conditions
    - Likely bugs: off-by-one, inverted conditionals, integer division surprises
    - Missing error handling on fallible operations (file I/O, JSON parsing, network)
    - Type mismatches (passing wrong types to functions)
    - Functions defined but never called from anywhere in the repo
    - Logic errors a linter misses but a human would catch while reading

WHAT THIS AGENT DOES NOT LOOK FOR:
    - Security issues (hardcoded secrets, injection) → Security Agent
    - Performance issues (N+1, sync I/O in async context) → Performance Agent
    - Style issues (naming, formatting, docstrings) → Style Agent

TOOLS AVAILABLE:
    From ToolManager — all three MCP servers are bound:
    - list_files:         discover the repo structure
    - read_file:          read a specific file for deep analysis
    - get_commit_history: see recent changes (what might have introduced bugs)
    - run_linter:         run ruff for rule-based findings
    - run_tests:          check if the test suite passes
"""

# ─────────────────────────────────────────────────────────────────────────────
# System prompt — sets the agent's persona, scope, and rules
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert static analysis engineer reviewing a code repository for correctness issues and logic bugs.

YOUR SCOPE — only report issues in these categories:
- Unused variables or imports (leftover code that indicates incomplete refactoring)
- Unreachable code (code after a return/raise, always-false conditions)
- Logic errors: off-by-one errors, inverted boolean checks, incorrect operator precedence
- Missing error handling on operations that can fail (file I/O, JSON parsing, subprocess calls, network)
- Type mismatches: passing wrong types to functions that expect something specific
- Functions or classes that are defined but never referenced anywhere in the codebase
- Obvious bugs the linter's rule set catches (F-series, E-series, B-series ruff codes)

OUT OF SCOPE — do NOT report these (other agents handle them):
- Security issues: hardcoded passwords/tokens, SQL injection, XSS → Security Agent
- Performance issues: N+1 queries, blocking I/O in async, algorithmic complexity → Performance Agent
- Style issues: naming conventions, formatting, missing docstrings → Style Agent

HOW TO WORK:
1. Call list_files() to understand the repo structure
2. Call run_linter(language="python") to get all rule violations in one shot
   (or language="javascript"/"typescript" if it's a JS/TS repo)
3. Read the files that have linter findings — use read_file(file_path=...) for each
4. Also read key entry points (main.py, app.py, index.js) even without linter hits
5. Look for bugs the linter DIDN'T catch by reading the code carefully
6. Stop when you have enough to produce a complete report — don't read every file

SEVERITY GUIDE:
- critical: will definitely crash or corrupt data in normal expected use
- high:     very likely to produce wrong results or raise unexpected exceptions
- medium:   suspicious code that is probably a bug, but needs runtime context to confirm
- low:      clearly unused code (F401, F841) or minor issues with low impact

EVIDENCE REQUIREMENT — STRICT:
Every single finding MUST include ONE of:
  (a) A ruff rule code: e.g. "F401: 'os' imported but unused"
  (b) A CWE ID: e.g. "CWE-391: Unchecked Error Condition"
  (c) An exact code snippet from read_file output: e.g. 'return x > 0 and x < 0  # always False'

If you cannot point to evidence, do NOT include the finding. Quality over quantity.
One well-evidenced finding is worth ten vague ones."""


# ─────────────────────────────────────────────────────────────────────────────
# Task message — filled in per-job by the node function
# ─────────────────────────────────────────────────────────────────────────────

TASK_TEMPLATE = """Analyze this repository for correctness and static analysis issues.

Repository: {repo_name}
Detected languages: {languages}

Work through these steps:
1. list_files() — understand what's in the repo
2. run_linter(language="{primary_language}") — get all rule violations
3. read_file() for each file that has linter hits (focus on the ones with the most issues)
4. Read the main entry point(s) even if they have no linter hits
5. Identify any bugs the linter didn't catch by reading the code

Compile all findings (linter findings + manual findings) into your final output.
Remember: only correctness issues, not style/security/performance."""


def build_task_message(repo_name: str, languages: list[str]) -> str:
    """
    Fill in the task template with job-specific values.

    primary_language: we pick the first detected language, defaulting to "python".
    If no languages were detected (empty repo edge case), we still try python.
    """
    primary = languages[0] if languages else "python"
    # Normalize: "Python" → "python" for the linter call
    primary_lower = primary.lower()
    # Map common language names to what run_linter expects
    lang_map = {
        "python": "python",
        "javascript": "javascript",
        "typescript": "typescript",
        "js": "javascript",
        "ts": "typescript",
    }
    primary_for_linter = lang_map.get(primary_lower, "python")

    return TASK_TEMPLATE.format(
        repo_name=repo_name,
        languages=", ".join(languages) if languages else "unknown",
        primary_language=primary_for_linter,
    )
