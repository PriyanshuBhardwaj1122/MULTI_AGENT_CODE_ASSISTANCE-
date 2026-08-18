"""
tools/test_runner_tool.py — LangChain tools for the test runner MCP server.

WHICH AGENTS USE THESE:
    - Static Analysis Agent: failing tests signal where bugs likely exist
    - Performance Agent: slow test durations hint at hot paths

SANDBOXING REMINDER:
    The test runner MCP server executes uploaded code. In v1, it runs in a
    sandboxed subprocess with a timeout. In v2, it runs in a Docker container
    with no network access and strict CPU/memory limits. Never call this tool
    outside a sandboxed environment.
"""

TEST_RUNNER_TOOL_NAMES = ["run_tests"]
# Tool instances created per-job by ToolManager.test_runner_tools()
