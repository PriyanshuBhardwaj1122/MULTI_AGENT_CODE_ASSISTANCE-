"""
mcp_servers/git_reader/server.py — Git repo reader MCP server.

HOW TO RUN STANDALONE (for testing):
    python app/mcp_servers/git_reader/server.py

HOW IT GETS USED:
    The ToolManager in app/tools/tool_manager.py starts this as a subprocess.
    The LangChain tool wrappers communicate with it via stdin/stdout using the
    MCP JSON-RPC protocol. Claude never talks to this directly — it goes through
    the LangChain tool layer.

WHY A SEPARATE PROCESS?
    - Isolation: if this server crashes, it doesn't take down the API process
    - In v2, the test runner server needs OS-level resource limits (no network,
      CPU caps). Running in a separate process makes that possible with ulimit
      or Docker. We establish the pattern here even for the git reader.
    - Standard interface: MCP is a protocol Claude understands — any LLM that
      supports MCP can use these servers without changing the server code.

WHY FastMCP?
    FastMCP is the high-level interface from the official MCP Python SDK.
    Without it, you'd write ~100 lines of JSON-RPC protocol boilerplate.
    With FastMCP, you write Python functions with type hints and docstrings,
    and it handles tool registration, argument parsing, and transport automatically.

TOOLS EXPOSED:
    list_files  — filtered file tree (skips noise dirs, filters by extension)
    read_file   — file contents, size-capped at 500 KB
    get_commit_history — recent git commits (optional, only if .git present)
"""
import json
import os
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("git-reader")

# Directories that are always noise — never traverse into these
NOISE_DIRS = {
    "node_modules", ".venv", "venv", "env", "__pycache__", ".git", ".hg",
    "dist", "build", ".next", ".nuxt", "coverage", ".coverage",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
}

# Cap file reads at 500 KB — large files (minified JS, lock files) add noise
MAX_FILE_SIZE_BYTES = 500 * 1024


# ─────────────────────────────────────────────────────────────────────────────
# Tool: list_files
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def list_files(
    repo_path: str,
    extensions: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> str:
    """
    Return a JSON list of relative file paths in the repository.

    Args:
        repo_path: Absolute path to the extracted repository directory.
        extensions: If provided, only include files with these extensions
                    (e.g. [".py", ".ts"]). Includes all source files if omitted.
        exclude_patterns: Additional directory names to skip beyond the defaults.

    Returns:
        JSON string: list of relative file path strings.
    """
    extra_noise = set(exclude_patterns or [])
    skip_dirs = NOISE_DIRS | extra_noise

    result: list[str] = []

    for root, dirs, files in os.walk(repo_path):
        # Prune noise dirs — modifying dirs in-place stops os.walk from descending
        dirs[:] = [d for d in dirs if d not in skip_dirs]

        for filename in sorted(files):
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, repo_path)

            if extensions:
                ext = Path(filename).suffix.lower()
                if ext not in extensions:
                    continue

            result.append(rel_path)

    return json.dumps(result)


# ─────────────────────────────────────────────────────────────────────────────
# Tool: read_file
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def read_file(repo_path: str, file_path: str) -> str:
    """
    Return the contents of a single file in the repository.

    Args:
        repo_path: Absolute path to the extracted repository directory.
        file_path: Relative path to the file within the repo (e.g. "src/app.py").

    Returns:
        The file contents as a string. Truncated with a notice if over 500 KB.
        Returns an error string if the file doesn't exist or is binary.
    """
    # Security: resolve the full path and verify it stays inside repo_path
    full_path = Path(repo_path) / file_path
    try:
        resolved = full_path.resolve()
        repo_resolved = Path(repo_path).resolve()
        if not str(resolved).startswith(str(repo_resolved)):
            return f"ERROR: path traversal attempt detected: {file_path}"
    except Exception:
        return f"ERROR: invalid path: {file_path}"

    if not resolved.exists():
        return f"ERROR: file not found: {file_path}"

    if not resolved.is_file():
        return f"ERROR: not a file: {file_path}"

    size = resolved.stat().st_size
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            if size <= MAX_FILE_SIZE_BYTES:
                return f.read()
            else:
                content = f.read(MAX_FILE_SIZE_BYTES)
                truncated_kb = size // 1024
                return (
                    content
                    + f"\n\n[TRUNCATED — file is {truncated_kb} KB, "
                    f"showing first {MAX_FILE_SIZE_BYTES // 1024} KB]"
                )
    except Exception as exc:
        return f"ERROR reading {file_path}: {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# Tool: get_commit_history
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def get_commit_history(repo_path: str, limit: int = 10) -> str:
    """
    Return recent git commit history for the repository (if .git is present).

    Args:
        repo_path: Absolute path to the extracted repository directory.
        limit: Maximum number of commits to return (default 10).

    Returns:
        JSON list of commit dicts with hash, author, date, message.
        Returns empty list JSON if .git is not present.
    """
    git_dir = Path(repo_path) / ".git"
    if not git_dir.exists():
        return json.dumps([])

    try:
        result = subprocess.run(
            [
                "git", "-C", repo_path, "log",
                f"-{limit}",
                "--pretty=format:%H|%an|%ae|%ad|%s",
                "--date=short",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return json.dumps([])

        commits = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split("|", 4)
            if len(parts) == 5:
                commits.append({
                    "hash": parts[0][:8],   # short hash
                    "author": parts[1],
                    "email": parts[2],
                    "date": parts[3],
                    "message": parts[4],
                })

        return json.dumps(commits)

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return json.dumps([])


# ─────────────────────────────────────────────────────────────────────────────
# Entrypoint — run as standalone stdio MCP server
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # mcp.run() starts the stdio transport — reads JSON-RPC from stdin,
    # writes responses to stdout. This is what the MCP client (ToolManager)
    # connects to when it starts this script as a subprocess.
    mcp.run()
