from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

_WHITESPACE = re.compile(r"\s+")
_UNSAFE = re.compile(r"[\\/:*?\"<>|]+")
_ZERO_WIDTH_NON_JOINER = "\N{ZERO WIDTH NON-JOINER}"
_ZERO_WIDTH_JOINER = "\N{ZERO WIDTH JOINER}"
_DISALLOWED_UNICODE_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
# These are the only format characters with legitimate shaping semantics that we
# retain. All other Cf characters, including bidi controls and hidden separators,
# are removed below.
_ALLOWED_JOIN_CONTROLS = frozenset({_ZERO_WIDTH_NON_JOINER, _ZERO_WIDTH_JOINER})
_FILENAME_EDGE_CHARACTERS = f" .–-{_ZERO_WIDTH_NON_JOINER}{_ZERO_WIDTH_JOINER}"
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def safe_title_component(title: str, max_length: int = 160) -> str:
    """Return a readable, cross-platform-safe filename component."""
    normalized = unicodedata.normalize("NFKC", title)
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) not in _DISALLOWED_UNICODE_CATEGORIES
        or character in _ALLOWED_JOIN_CONTROLS
    )
    normalized = _UNSAFE.sub(" – ", normalized)
    normalized = _WHITESPACE.sub(" ", normalized).strip(_FILENAME_EDGE_CHARACTERS)
    if not normalized:
        normalized = "Untitled"
    if normalized.upper() in _RESERVED_NAMES:
        normalized = f"{normalized}_"
    normalized = normalized.encode("utf-8")[:max_length].decode("utf-8", "ignore")
    normalized = normalized.rstrip(_FILENAME_EDGE_CHARACTERS)
    return normalized or "Untitled"


def safe_identifier(value: str | None, fallback: str) -> str:
    identifier = re.sub(r"[^A-Za-z0-9_-]", "", value or "")[:32]
    return identifier or fallback[:8]


def artifact_filename(
    title: str,
    extension: str,
    *,
    source_id: str | None,
    job_id: str,
) -> str:
    extension = extension.lower().lstrip(".")
    if not extension or not re.fullmatch(r"[a-z0-9]{1,10}", extension):
        extension = "mp4"
    identifier = safe_identifier(source_id, job_id)
    return f"{safe_title_component(title)} [{identifier}].{extension}"


def unique_destination(root: Path, filename: str, current: Path | None = None) -> Path:
    """Choose a non-overwriting destination within root."""
    root = root.resolve()
    candidate = root / filename
    current_resolved = current.resolve() if current else None
    if not candidate.exists() or (
        current_resolved is not None and candidate.resolve() == current_resolved
    ):
        return candidate

    suffix = "".join(candidate.suffixes)
    stem = candidate.name[: -len(suffix)] if suffix else candidate.name
    for number in range(2, 10_000):
        candidate = root / f"{stem} ({number}){suffix}"
        if not candidate.exists() or (
            current_resolved is not None and candidate.resolve() == current_resolved
        ):
            return candidate
    raise FileExistsError("Could not choose a unique output filename")


def resolve_stored_path(root: Path, relative_path: str | None) -> Path | None:
    """Resolve a persisted path and reject escapes (including symlinks)."""
    if not relative_path:
        return None
    root = root.resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("Stored media path escapes the download directory") from error
    return candidate


def move_artifact(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)
