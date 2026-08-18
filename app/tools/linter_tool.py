"""
tools/linter_tool.py — LangChain tools for the linter MCP server.

WHICH AGENTS USE THESE:
    - Static Analysis Agent: interprets correctness-related rule codes
      (F-series = pyflakes, B-series = bugbear)
    - Style Agent: interprets style-related rule codes
      (E/W-series = pycodestyle, I-series = isort, N-series = naming, D-series = docstrings)

    Both agents call the SAME run_linter tool. The raw linter output is identical.
    The difference is in how each agent's system prompt instructs Claude to
    interpret and prioritize the findings.
"""

LINTER_TOOL_NAMES = ["run_linter"]
# Tool instances created per-job by ToolManager.linter_tools()
