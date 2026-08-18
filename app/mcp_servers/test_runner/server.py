"""
mcp_servers/test_runner/server.py — Test runner MCP server.

HOW TO RUN STANDALONE:
    python app/mcp_servers/test_runner/server.py

WHAT IT DOES:
    Executes the repo's existing test suite (if present) in a sandboxed subprocess
    and returns pass/fail/count results. This gives agents a signal about repo
    health beyond static analysis:
      - Static Analysis Agent: failing tests = likely bugs in the flagged area
      - Performance Agent: slow tests = potential hot paths to investigate

SANDBOXING — CRITICAL:
    This server actually executes uploaded code. That's an attack surface.
    v1 sandboxing: subprocess with a timeout + working directory isolation.
    v2 sandboxing: Docker container per job with no network, CPU/memory limits.

    NEVER call this with untrusted code outside a sandboxed environment.
    In production, this server should run inside a container, not in the
    same process or VM as the API server.

FRAMEWORK DETECTION:
    pytest:  detected by pytest.ini, setup.cfg [tool:pytest], pyproject.toml
             [tool.pytest.ini_options], or presence of test_*.py files
    jest:    detected by jest.config.{js,ts,json} or "jest" in package.json
    If neither is detected, returns a "no tests found" result rather than failing.
"""
import json
import os
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("test-runner")

DEFAULT_TIMEOUT = 60  # seconds — overridable per call


@mcp.tool()
def run_tests(
    repo_path: str,
    framework: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """
    Execute the repository's test suite and return results.

    Args:
        repo_path: Absolute path to the extracted repository directory.
        framework: "pytest", "jest", or None to auto-detect.
        timeout: Maximum seconds to allow tests to run (default 60).

    Returns:
        JSON string with keys:
          framework: which test runner was used (or None)
          passed: int, failed: int, errors: int, total: int
          duration_seconds: float
          output: last N lines of test output (truncated)
          error: null on success, error message on failure
          test_files_found: bool
    """
    detected = framework or _detect_framework(repo_path)

    if not detected:
        return json.dumps({
            "framework": None,
            "passed": 0, "failed": 0, "errors": 0, "total": 0,
            "duration_seconds": 0.0,
            "output": "",
            "test_files_found": False,
            "error": None,
        })

    if detected == "pytest":
        return _run_pytest(repo_path, timeout)
    elif detected == "jest":
        return _run_jest_stub(repo_path)
    else:
        return json.dumps({
            "framework": detected,
            "passed": 0, "failed": 0, "errors": 0, "total": 0,
            "duration_seconds": 0.0,
            "output": "",
            "test_files_found": False,
            "error": f"Unsupported framework: {detected}",
        })


def _detect_framework(repo_path: str) -> str | None:
    """Auto-detect the test framework used in the repository."""
    root = Path(repo_path)

    # pytest indicators
    if (
        (root / "pytest.ini").exists()
        or (root / "setup.cfg").exists()
        or (root / "pyproject.toml").exists()
        or any(root.rglob("test_*.py"))
        or any(root.rglob("*_test.py"))
    ):
        # Double-check it's not a JS repo trying to look like Python
        py_files = list(root.rglob("*.py"))
        if py_files:
            return "pytest"

    # jest indicators
    if (
        (root / "jest.config.js").exists()
        or (root / "jest.config.ts").exists()
        or (root / "jest.config.json").exists()
    ):
        return "jest"

    # package.json with jest key
    pkg_json = root / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            if "jest" in pkg or "jest" in pkg.get("devDependencies", {}):
                return "jest"
        except (json.JSONDecodeError, OSError):
            pass

    return None


def _run_pytest(repo_path: str, timeout: int) -> str:
    """
    Run pytest with JSON output in a sandboxed subprocess.

    SANDBOXING v1:
    - subprocess.run isolates execution to the child process
    - cwd=repo_path confines pytest's working directory
    - timeout=timeout kills the process if it runs too long
    - No network access control in v1 (added in v2 via Docker)
    """
    import time
    start = time.monotonic()

    try:
        result = subprocess.run(
            [
                "python", "-m", "pytest",
                "--tb=no",          # no tracebacks — we only need counts
                "-q",               # quiet output
                "--no-header",
                "--timeout=30",     # per-test timeout (requires pytest-timeout)
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=repo_path,          # confine to the repo directory
            env={
                **os.environ,
                "PYTHONPATH": repo_path,  # make repo importable
                # Strip any credentials from the environment
                "ANTHROPIC_API_KEY": "",
                "OPENAI_API_KEY": "",
            },
        )

        elapsed = time.monotonic() - start
        output = (result.stdout + result.stderr).strip()

        # Parse pytest's summary line: "3 passed, 1 failed in 0.42s"
        passed, failed, errors = _parse_pytest_output(output)

        # Truncate output — don't send megabytes of test output to the LLM
        truncated = _truncate_output(output, max_lines=50)

        return json.dumps({
            "framework": "pytest",
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "total": passed + failed + errors,
            "duration_seconds": round(elapsed, 2),
            "output": truncated,
            "test_files_found": True,
            "error": None,
        })

    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        return json.dumps({
            "framework": "pytest",
            "passed": 0, "failed": 0, "errors": 0, "total": 0,
            "duration_seconds": round(elapsed, 2),
            "output": "",
            "test_files_found": True,
            "error": f"pytest timed out after {timeout}s — tests may have hung",
        })
    except FileNotFoundError:
        return json.dumps({
            "framework": "pytest",
            "passed": 0, "failed": 0, "errors": 0, "total": 0,
            "duration_seconds": 0.0,
            "output": "",
            "test_files_found": True,
            "error": "pytest not found — not installed in this environment",
        })
    except Exception as exc:
        return json.dumps({
            "framework": "pytest",
            "passed": 0, "failed": 0, "errors": 0, "total": 0,
            "duration_seconds": 0.0,
            "output": "",
            "test_files_found": True,
            "error": str(exc),
        })


def _parse_pytest_output(output: str) -> tuple[int, int, int]:
    """Extract passed/failed/error counts from pytest's summary line."""
    passed = failed = errors = 0
    for line in output.splitlines():
        line = line.lower()
        if "passed" in line or "failed" in line or "error" in line:
            # e.g. "3 passed, 1 failed, 0 errors"
            import re
            if m := re.search(r"(\d+)\s+passed", line):
                passed = int(m.group(1))
            if m := re.search(r"(\d+)\s+failed", line):
                failed = int(m.group(1))
            if m := re.search(r"(\d+)\s+error", line):
                errors = int(m.group(1))
    return passed, failed, errors


def _truncate_output(output: str, max_lines: int = 50) -> str:
    """Keep only the last N lines of output (where the summary lives)."""
    lines = output.splitlines()
    if len(lines) <= max_lines:
        return output
    kept = lines[-max_lines:]
    return f"[... {len(lines) - max_lines} lines omitted ...]\n" + "\n".join(kept)


def _run_jest_stub(repo_path: str) -> str:
    """
    jest stub — deferred to v2.
    Running jest requires node_modules which we don't install for uploaded ZIPs.
    """
    return json.dumps({
        "framework": "jest",
        "passed": 0, "failed": 0, "errors": 0, "total": 0,
        "duration_seconds": 0.0,
        "output": "",
        "test_files_found": True,
        "error": "jest execution deferred to v2 (requires node_modules installation)",
    })


if __name__ == "__main__":
    mcp.run()
