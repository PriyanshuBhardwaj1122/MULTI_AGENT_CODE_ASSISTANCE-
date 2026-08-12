"""
sandbox/extractor.py — ZIP validation and extraction.

WHY A DEDICATED MODULE FOR THIS?
---------------------------------
Accepting user-uploaded ZIPs is a well-known attack surface. This module
is the single place where all that risk is handled — so security fixes
happen here and nowhere else.

ATTACKS WE DEFEND AGAINST:
---------------------------
1. Zip-slip / path traversal:
   A malicious ZIP can contain members with paths like "../../etc/passwd".
   When naively extracted, these overwrite files OUTSIDE the intended directory.
   We resolve() every member path and verify it still starts with the extract dir.

2. Zip bomb:
   A tiny ZIP that expands to gigabytes. We enforce both upload size AND
   the extracted file count. A proper v2 hardening would also check
   total uncompressed size before extracting.

3. Untrusted code execution:
   We never exec/eval/import from the uploaded code. Linting and test
   running happen in a sandboxed subprocess (M2/M3). This module just
   puts files on disk.

ABOUT tempfile.mkdtemp():
--------------------------
mkdtemp() creates a unique temp directory with permissions 0700 (only
the current user can read/write it). It does NOT get deleted automatically
on process exit — we call extraction.cleanup() explicitly after the job
finishes. This gives us control over when the files disappear.
"""
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException


# Source file extensions we care about analyzing
SUPPORTED_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".json", ".yaml", ".yml", ".toml",
    ".cfg", ".ini", ".md", ".txt",
    ".html", ".css", ".sh",
}

# Directories that are always noise — skip them entirely
NOISE_DIRS = {
    "node_modules", ".venv", "venv", "env",
    "__pycache__", ".git", ".hg",
    "dist", "build", ".next", ".nuxt",
    "coverage", ".coverage", "htmlcov",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
}

# Map from file extension → language name
EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py":  "python",
    ".js":  "javascript",
    ".jsx": "javascript",
    ".ts":  "typescript",
    ".tsx": "typescript",
}


@dataclass
class ExtractionResult:
    """
    Everything the caller needs to know about the extracted repo.

    WHY A DATACLASS AND NOT A PYDANTIC MODEL?
    ------------------------------------------
    This is an internal data-carrier, not a request/response model.
    Dataclass gives us a clean named struct with zero extra overhead.
    Pydantic models validate on construction — useful for untrusted data,
    unnecessary for something we construct ourselves with known types.
    """
    temp_dir: str           # root temp directory (contains the repo/ subdirectory)
    repo_dir: str           # temp_dir/repo — the actual extracted contents
    repo_name: str          # derived from the uploaded filename
    file_count: int         # number of non-noise source files found
    detected_languages: list[str] = field(default_factory=list)

    def cleanup(self) -> None:
        """Delete the temp directory and everything inside it."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)


def validate_and_extract(
    zip_bytes: bytes,
    filename: str,
    max_size_mb: int,
    max_files: int,
) -> ExtractionResult:
    """
    Validates a ZIP upload and extracts it into a sandboxed temp directory.

    Raises HTTPException(400) for any validation failure.
    Raises HTTPException(500) for unexpected errors.
    Returns ExtractionResult on success.
    """

    # ── 1. Size check (fast — no disk I/O needed) ─────────────────────────────
    size_mb = len(zip_bytes) / (1024 * 1024)
    if size_mb > max_size_mb:
        raise HTTPException(
            status_code=400,
            detail=f"Upload too large: {size_mb:.1f} MB (limit: {max_size_mb} MB)",
        )

    # ── 2. Create isolated temp directory ─────────────────────────────────────
    # prefix helps identify our dirs if you ever ls /tmp
    temp_dir = tempfile.mkdtemp(prefix="code_review_")

    try:
        # Write ZIP bytes to a file so zipfile can open it
        zip_path = os.path.join(temp_dir, "upload.zip")
        with open(zip_path, "wb") as f:
            f.write(zip_bytes)

        # ── 3. Validate it's actually a ZIP ────────────────────────────────────
        if not zipfile.is_zipfile(zip_path):
            raise HTTPException(
                status_code=400,
                detail="Uploaded file is not a valid ZIP archive",
            )

        # ── 4. Prepare extraction destination ─────────────────────────────────
        repo_dir = os.path.join(temp_dir, "repo")
        os.makedirs(repo_dir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            members = zf.infolist()

            # ── 5. ZIP-SLIP PROTECTION ─────────────────────────────────────────
            # Path.resolve() turns "repo/../../../etc/passwd" into "/etc/passwd"
            # so we can detect if any member escapes the repo_dir.
            safe_root = Path(repo_dir).resolve()
            for member in members:
                member_path = (safe_root / member.filename).resolve()
                if not str(member_path).startswith(str(safe_root)):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Zip contains unsafe path: {member.filename}",
                    )

            # ── 6. File count check ────────────────────────────────────────────
            # Only count real files (not dirs) that aren't in noise directories
            real_files = [
                m for m in members
                if not m.is_dir() and not _in_noise_dir(m.filename)
            ]
            if len(real_files) > max_files:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Repository contains too many files: {len(real_files)} "
                        f"(limit: {max_files}). Consider reducing the repo size."
                    ),
                )

            # ── 7. Extract ─────────────────────────────────────────────────────
            zf.extractall(repo_dir)

        # Clean up the zip — we only need the extracted contents from here on
        os.remove(zip_path)

        # ── 8. Detect languages ────────────────────────────────────────────────
        detected_languages = _detect_languages(repo_dir)

        # ── 9. Derive repo name from filename ──────────────────────────────────
        repo_name = Path(filename).stem  # "my-project.zip" → "my-project"

        return ExtractionResult(
            temp_dir=temp_dir,
            repo_dir=repo_dir,
            repo_name=repo_name,
            file_count=len(real_files),
            detected_languages=detected_languages,
        )

    except HTTPException:
        # Clean up before re-raising — don't leave orphaned temp dirs
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process uploaded archive: {exc}",
        ) from exc


# ─── Private helpers ──────────────────────────────────────────────────────────

def _in_noise_dir(path: str) -> bool:
    """Return True if this path is inside a noise directory we want to skip."""
    # Path("node_modules/foo/bar.js").parts == ("node_modules", "foo", "bar.js")
    return any(part in NOISE_DIRS for part in Path(path).parts)


def _detect_languages(repo_dir: str) -> list[str]:
    """
    Walk the extracted repo and return a sorted list of detected language names.

    WHY os.walk INSTEAD OF Path.rglob?
    ------------------------------------
    os.walk lets us modify `dirs` in-place to skip entire subtrees (noise dirs)
    without traversing into them at all. Path.rglob("*") would traverse
    node_modules and then filter — wasteful on large repos.
    """
    found: set[str] = set()

    for root, dirs, files in os.walk(repo_dir):
        # Prune noise directories from traversal — this modifies os.walk's
        # iteration so it never descends into them
        dirs[:] = [d for d in dirs if d not in NOISE_DIRS]

        for filename in files:
            ext = Path(filename).suffix.lower()
            lang = EXTENSION_TO_LANGUAGE.get(ext)
            if lang:
                found.add(lang)

    return sorted(found)
