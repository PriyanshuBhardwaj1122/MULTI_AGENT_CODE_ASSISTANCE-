"""
tools/git_reader_tool.py — LangChain tools for the git reader MCP server.

These tools are created per-job via ToolManager. See tool_manager.py for how
the MCP subprocess connection is managed and how repo_path is injected via closure.

This file's public interface:
    from app.tools.git_reader_tool import GIT_READER_TOOL_NAMES

    # Used in M3 when building agents:
    async with ToolManager(repo_path) as tm:
        tools = tm.git_reader_tools()
        # tools is a list of StructuredTool with these names:
        #   list_files, read_file, get_commit_history

WHICH AGENTS USE THESE:
    - Static Analysis Agent: reads files to supplement linter output (logic bugs
      linters miss, e.g. missing edge case handling)
    - Security Agent: reads files for injection pattern analysis; reads
      requirements.txt / package.json for dependency checking
    - Performance Agent: reads files to identify anti-patterns (N+1, sync I/O
      in async context, nested loops)
    - Style Agent: does NOT use git reader — linter output is sufficient
    - Summary Agent: does NOT use any tools — reads graph state only
"""

# Tool names exposed by this module — used in M3 to document which tools
# each agent binds to.
GIT_READER_TOOL_NAMES = ["list_files", "read_file", "get_commit_history"]

# Tool instances are created per-job by ToolManager.git_reader_tools().
# There is no module-level tool instance because the tools are stateful
# (they capture repo_path for a specific job).
