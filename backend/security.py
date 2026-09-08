from pathlib import Path, PureWindowsPath


ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".txt", ".docx"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def validate_upload_filename(filename: str) -> str:
    """Return a safe basename or raise ValueError."""
    candidate = (filename or "").strip()
    if not candidate or len(candidate) > 240:
        raise ValueError("Invalid filename")
    if "/" in candidate or "\\" in candidate:
        raise ValueError("Folder paths are not allowed in filenames")
    if candidate in {".", ".."} or PureWindowsPath(candidate).name != candidate:
        raise ValueError("Invalid filename")
    if Path(candidate).suffix.lower() not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError("Only PDF, TXT, and DOCX files are supported")
    return candidate


def safe_upload_path(upload_dir: Path, filename: str) -> Path:
    safe_name = validate_upload_filename(filename)
    root = upload_dir.resolve()
    target = (root / safe_name).resolve()
    if target.parent != root:
        raise ValueError("Upload path escapes the knowledge-base directory")
    return target
