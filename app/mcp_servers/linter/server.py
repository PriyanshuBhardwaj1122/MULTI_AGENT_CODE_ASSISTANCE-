"""
mcp_servers/linter/server.py — Linter MCP server.

HOW TO RUN STANDALONE:
    python app/mcp_servers/linter/server.py

WHAT IT DOES:
    Runs language-appropriate linters against the uploaded repository and
    returns structured findings. The raw output is the same regardless of
    which agent calls this — the agents interpret it differently:

    - Static Analysis Agent: looks at error codes (F-series = logic errors,
      E-series = runtime errors, unused vars, etc.)
    - Style Agent: looks at style codes (E-series formatting, I-series imports,
      N-series naming, D-series docstrings, etc.)

    One tool, two consumers, different lenses — that's intentional.

LINTERS USED:
    Python: ruff (fast, structured JSON output, covers ruff's rule set which
            includes pyflakes, pycodestyle, isort, pydocstyle, and more)
    JS/TS:  eslint (deferred — requires node_modules, complex setup for v1;
            stub returns a clear not-supported message)

WHY RUFF OVER PYLINT OR FLAKE8?
    ruff is ~100x faster than pylint, outputs clean JSON via --output-format=json,
    and covers most of the same rules plus more. For a demo that needs to run
    in under 60 seconds, speed matters.

IMPORTANT: Linting runs the repo's code through a static analysis tool — it does
    NOT execute the code. Safe to run without a sandbox. Only the test runner
    (which actually executes code) needs sandboxing.
"""
import json
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("linter")

# ruff rule codes and their human-readable categories
RUFF_SEVERITY_MAP = {
    # Pyflakes — logic/correctness errors
    "F": "high",
    # pycodestyle errors
    "E": "medium",
    # pycodestyle warnings
    "W": "low",
    # isort — import order
    "I": "low",
    # pep8-naming
    "N": "low",
    # pydocstyle
    "D": "low",
    # flake8-bandit (security)
    "S": "high",
    # flake8-bugbear — opinionated correctness
    "B": "medium",
    # Pyupgrade
    "UP": "low",
    # flake8-simplify
    "SIM": "low",
    # Refurb
    "FURB": "low",
}

def _get_severity(rule_code: str) -> str:
    """Map a ruff rule code prefix to a severity level."""
    for prefix, severity in RUFF_SEVERITY_MAP.items():
        if rule_code.startswith(prefix):
            return severity
    return "low"


@mcp.tool()
def run_linter(
    repo_path: str,
    language: str,
    path: str | None = None,
) -> str:
    """
    Run the appropriate linter against the repository and return structured findings.

    Args:
        repo_path: Absolute path to the extracted repository directory.
        language: "python", "javascript", or "typescript".
        path: Optional relative path to a specific file or subdirectory to lint.
              Lints the whole repo if omitted.

    Returns:
        JSON string with keys:
          language: the language linted
          target: what was linted (file or "whole repo")
          findings: list of finding dicts (file, line, col, rule, message, severity)
          error: null on success, error message string on failure
    """
    target = path or "."
    abs_target = str(Path(repo_path) / target) if path else repo_path

    if language == "python":
        return _run_ruff(repo_path, abs_target, target)
    elif language in ("javascript", "typescript"):
        return _run_eslint_stub(language)
    else:
        return json.dumps({
            "language": language,
            "target": target,
            "findings": [],
            "error": f"Unsupported language: {language}. Supported: python, javascript, typescript",
        })


def _run_ruff(repo_path: str, abs_target: str, target_label: str) -> str:
    """Run ruff and return structured output."""
    try:
        result = subprocess.run(
            [
                "ruff", "check",
                abs_target,
                "--output-format=json",
                "--no-cache",
                # Don't error on fixable issues — we just report them
                "--exit-zero",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=repo_path,
        )

        # ruff outputs valid JSON on stdout even with findings
        raw = result.stdout.strip()
        if not raw:
            ruff_findings = []
        else:
            try:
                ruff_findings = json.loads(raw)
            except json.JSONDecodeError:
                return json.dumps({
                    "language": "python",
                    "target": target_label,
                    "findings": [],
                    "error": f"ruff output parse error: {raw[:200]}",
                })

        # Normalize to our shared Finding-compatible shape
        findings = []
        for f in ruff_findings:
            rule_code = f.get("code", "")
            # Make path relative to repo_path for consistent reporting
            abs_file = f.get("filename", "")
            try:
                rel_file = str(Path(abs_file).relative_to(repo_path))
            except ValueError:
                rel_file = abs_file

            findings.append({
                "file": rel_file,
                "line": f.get("location", {}).get("row"),
                "col": f.get("location", {}).get("column"),
                "end_line": f.get("end_location", {}).get("row"),
                "rule": rule_code,
                "message": f.get("message", ""),
                "severity": _get_severity(rule_code),
                "fix_available": f.get("fix") is not None,
            })

        return json.dumps({
            "language": "python",
            "target": target_label,
            "findings": findings,
            "error": None,
        })

    except subprocess.TimeoutExpired:
        return json.dumps({
            "language": "python",
            "target": target_label,
            "findings": [],
            "error": "ruff timed out after 60s",
        })
    except FileNotFoundError:
        return json.dumps({
            "language": "python",
            "target": target_label,
            "findings": [],
            "error": "ruff not found — install with: pip install ruff",
        })
    except Exception as exc:
        return json.dumps({
            "language": "python",
            "target": target_label,
            "findings": [],
            "error": str(exc),
        })


def _run_eslint_stub(language: str) -> str:
    """
    eslint stub — deferred to v2.

    eslint requires node_modules to be installed in the repo, which we don't
    do for uploaded ZIPs in v1. This returns a clear not-supported message
    so the agent can note the gap rather than failing silently.
    """
    return json.dumps({
        "language": language,
        "target": "whole repo",
        "findings": [],
        "error": (
            f"eslint for {language} is deferred to v2. "
            "JavaScript/TypeScript repos will not have linter findings in v1."
        ),
    })


if __name__ == "__main__":
    mcp.run()
