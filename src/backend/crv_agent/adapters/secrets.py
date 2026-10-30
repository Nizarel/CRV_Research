from pathlib import Path


def read_secret_file(path: str, what: str) -> str:
    """Secret value of a mounted file without its trailing newline; errors never echo it."""
    try:
        value = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as error:
        raise ConnectionError(f"Cannot read the {what} file {path}: {error.strerror}") from None
    if not value.strip():
        raise ConnectionError(f"The {what} file {path} is empty")
    return value
