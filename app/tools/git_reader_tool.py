"""
tools/git_reader_tool.py — LangChain Tool wrapping the Git repo reader MCP server.

STATUS: STUB — implemented in M2.

THE BIG PICTURE: WHY MCP + LANGCHAIN TOOL WRAPPER?
----------------------------------------------------
There are three layers here. Understanding them is key:

LAYER 1 — MCP Server (app/mcp_servers/git_reader/)
  A standalone Python process that exposes three methods over the MCP protocol:
    list_files(extensions?, exclude_patterns?) → filtered file tree
    read_file(path)                            → file contents
    get_commit_history(limit?)                 → recent commits

  It runs as a separate process (stdio or local HTTP). The MCP protocol is
  what Claude natively understands for tool-calling — it's a standard that
  Anthropic designed so LLMs can discover and call tools uniformly.

  Why a separate process? Because in M3 (and definitely in v2), the test runner
  needs to execute untrusted code in a sandbox. Running that in a subprocess
  with restricted permissions (no network, limited CPU/memory) is much safer
  than running it in the same process as the FastAPI server.

LAYER 2 — LangChain Tool (this file)
  A thin wrapper that adapts the MCP server's interface to a LangChain Tool object.
  LangChain Agents use Tool objects to know what tools exist and how to call them.

    git_reader_tool = Tool(
        name="read_repo_files",
        description="List files or read file contents from the uploaded repository.",
        func=mcp_git_reader_client.call,
    )

LAYER 3 — LangChain Agent (agents/base.py → agents/static_analysis.py, etc.)
  The Claude LLM with git_reader_tool (and others) bound to it via llm.bind_tools().
  Claude sees the tool descriptions and decides when/how to call them.
  LangChain handles the tool-call → execute → result → back-to-LLM loop.

SO THE QUESTION "WHY NOT JUST FASTAPI FOR TOOLS?" ANSWERED:
-------------------------------------------------------------
If tools were FastAPI endpoints, the LLM would need the URL and argument format
hardcoded into its system prompt. There'd be no standard discovery mechanism.
MCP is a protocol Claude understands natively — the LLM can see "here are the
available tools and their signatures" and decide what to call. It's the difference
between giving someone a phone book (MCP) vs. making them memorize every number (hardcoding).
"""
# Implemented in M2 alongside the MCP server.
