"""
tools/tool_manager.py — Manages MCP server subprocess lifecycle per review job.

THE PROBLEM THIS SOLVES:
-------------------------
Each review job has a different repo_path. The MCP servers need to operate on
that specific repo. We need to:
  1. Start the MCP server subprocesses when a job begins
  2. Keep them alive for the duration of the job (reusing the connection is
     much cheaper than starting a new subprocess per tool call)
  3. Shut them down and clean up when the job ends

ToolManager is an async context manager — you use it with `async with`:

    async with ToolManager(repo_path) as tm:
        git_tools = tm.git_reader_tools()
        lint_tools = tm.linter_tools()
        # pass these to the agent factory in M3

HOW IT WORKS:
-------------
1. __aenter__: starts each MCP server as a subprocess, creates a ClientSession
   for each, and calls session.initialize() to complete the MCP handshake.

2. During the job: each tool call goes through ClientSession.call_tool(), which
   sends a JSON-RPC request over stdin to the subprocess and reads the response
   from stdout. The subprocess stays alive between calls.

3. __aexit__: closes sessions and lets the subprocess terminate cleanly.

THE CLOSURE PATTERN FOR REPO_PATH:
------------------------------------
Claude (the LLM) decides what arguments to pass to each tool. We do NOT want
Claude to decide the repo_path — it's an internal path that should be invisible
to the model. So repo_path is captured in the closure when we create the tool
functions. Claude only provides the other arguments (e.g., file_path, extensions).

    def list_files(extensions: list[str] = None) -> str:
        # repo_path NOT in the signature — Claude never sees it
        return call_mcp("list_files", {"repo_path": self._repo_path, "extensions": extensions})
"""
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

logger = logging.getLogger(__name__)

# Absolute paths to each MCP server script
_BASE = Path(__file__).parent.parent  # app/
GIT_READER_SCRIPT = str(_BASE / "mcp_servers" / "git_reader" / "server.py")
LINTER_SCRIPT = str(_BASE / "mcp_servers" / "linter" / "server.py")
TEST_RUNNER_SCRIPT = str(_BASE / "mcp_servers" / "test_runner" / "server.py")


class ToolManager:
    """
    Async context manager that owns MCP server subprocesses for one review job.

    Usage (in M3, inside _run_review):
        async with ToolManager(repo_path) as tm:
            agent = build_agent(
                system_prompt=STATIC_ANALYSIS_PROMPT,
                tools=tm.git_reader_tools() + tm.linter_tools() + tm.test_runner_tools(),
            )
            result = await agent.ainvoke(...)
    """

    def __init__(self, repo_path: str):
        self._repo_path = repo_path
        self._sessions: dict[str, ClientSession] = {}
        self._client_contexts: list[Any] = []
        self._session_contexts: list[Any] = []

    async def __aenter__(self) -> "ToolManager":
        await self._connect("git_reader", GIT_READER_SCRIPT)
        await self._connect("linter", LINTER_SCRIPT)
        await self._connect("test_runner", TEST_RUNNER_SCRIPT)
        logger.info("ToolManager: all MCP servers connected for repo %s", self._repo_path)
        return self

    async def __aexit__(self, *args) -> None:
        # Close sessions then client contexts in reverse order
        for ctx in reversed(self._session_contexts):
            try:
                await ctx.__aexit__(*args)
            except Exception as e:
                logger.debug("Error closing MCP session: %s", e)
        for ctx in reversed(self._client_contexts):
            try:
                await ctx.__aexit__(*args)
            except Exception as e:
                logger.debug("Error closing MCP client: %s", e)
        logger.info("ToolManager: all MCP servers disconnected")

    async def _connect(self, name: str, script_path: str) -> None:
        """Start an MCP server subprocess and establish a ClientSession."""
        params = StdioServerParameters(
            command=sys.executable,  # use the same Python interpreter as the API
            args=[script_path],
        )
        client_ctx = stdio_client(params)
        read, write = await client_ctx.__aenter__()
        self._client_contexts.append(client_ctx)

        session = ClientSession(read, write)
        session_ctx = session
        await session.__aenter__()
        await session.initialize()
        self._session_contexts.append(session_ctx)

        self._sessions[name] = session
        logger.debug("ToolManager: connected to %s MCP server", name)

    async def _call(self, server: str, tool_name: str, arguments: dict) -> str:
        """Call a tool on an MCP server and return the text result."""
        session = self._sessions[server]
        result = await session.call_tool(tool_name, arguments)
        if result.content:
            return result.content[0].text
        return ""

    # ── LangChain tool factories ───────────────────────────────────────────────

    def git_reader_tools(self) -> list[StructuredTool]:
        """
        Return LangChain tools backed by the git reader MCP server.
        repo_path is captured in the closure — invisible to the LLM.
        """
        repo_path = self._repo_path
        call = self._call  # capture the bound method

        async def list_files(
            extensions: list[str] | None = None,
            exclude_patterns: list[str] | None = None,
        ) -> str:
            """
            List source files in the repository.
            Optionally filter by file extension (e.g. [".py", ".ts"]).
            Returns a JSON list of relative file paths.
            """
            return await call("git_reader", "list_files", {
                "repo_path": repo_path,
                "extensions": extensions,
                "exclude_patterns": exclude_patterns,
            })

        async def read_file(file_path: str) -> str:
            """
            Read the contents of a file in the repository.
            file_path is relative to the repo root (e.g. "src/app.py").
            Returns the file contents as a string (truncated if over 500 KB).
            """
            return await call("git_reader", "read_file", {
                "repo_path": repo_path,
                "file_path": file_path,
            })

        async def get_commit_history(limit: int = 10) -> str:
            """
            Return the last N git commits for the repository as JSON.
            Returns an empty list if .git directory is not present.
            """
            return await call("git_reader", "get_commit_history", {
                "repo_path": repo_path,
                "limit": limit,
            })

        return [
            StructuredTool.from_function(coroutine=list_files,    name="list_files"),
            StructuredTool.from_function(coroutine=read_file,     name="read_file"),
            StructuredTool.from_function(coroutine=get_commit_history, name="get_commit_history"),
        ]

    def linter_tools(self) -> list[StructuredTool]:
        """Return LangChain tools backed by the linter MCP server."""
        repo_path = self._repo_path
        call = self._call

        async def run_linter(
            language: str,
            path: str | None = None,
        ) -> str:
            """
            Run the linter for the given language against the repository.
            language: "python", "javascript", or "typescript".
            path: optional relative path to a specific file or directory.
            Returns JSON with a 'findings' list (file, line, rule, message, severity).
            """
            return await call("linter", "run_linter", {
                "repo_path": repo_path,
                "language": language,
                "path": path,
            })

        return [
            StructuredTool.from_function(coroutine=run_linter, name="run_linter"),
        ]

    def test_runner_tools(self) -> list[StructuredTool]:
        """Return LangChain tools backed by the test runner MCP server."""
        repo_path = self._repo_path
        call = self._call

        async def run_tests(
            framework: str | None = None,
            timeout: int = 60,
        ) -> str:
            """
            Execute the repository's test suite and return results.
            framework: "pytest", "jest", or None to auto-detect.
            timeout: maximum seconds to allow (default 60).
            Returns JSON with passed/failed/errors counts and truncated output.
            """
            return await call("test_runner", "run_tests", {
                "repo_path": repo_path,
                "framework": framework,
                "timeout": timeout,
            })

        return [
            StructuredTool.from_function(coroutine=run_tests, name="run_tests"),
        ]

    def all_tools(self) -> list[StructuredTool]:
        """Convenience: return all tools from all three servers."""
        return (
            self.git_reader_tools()
            + self.linter_tools()
            + self.test_runner_tools()
        )
