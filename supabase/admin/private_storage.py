"""Resolve a non-repository storage boundary for pilot secrets and direct PII."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


PRIVATE_ROOT_ENV = "BOK_PILOT_PRIVATE_DIR"
APPLICATION_DIRECTORY = "bok-stance-pilot"


def default_private_root(environ: Mapping[str, str] | None = None) -> Path:
    """Return the configured or OS-local private root without creating it."""
    values = os.environ if environ is None else environ
    configured = values.get(PRIVATE_ROOT_ENV, "").strip()
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured)))

    local_app_data = values.get("LOCALAPPDATA", "").strip()
    if os.name == "nt":
        if not local_app_data:
            raise ValueError(
                f"{PRIVATE_ROOT_ENV} or LOCALAPPDATA is required for private storage"
            )
        return Path(local_app_data) / APPLICATION_DIRECTORY

    state_home = values.get("XDG_STATE_HOME", "").strip()
    if state_home:
        return Path(os.path.expandvars(os.path.expanduser(state_home))) / APPLICATION_DIRECTORY
    return Path.home() / ".local" / "state" / APPLICATION_DIRECTORY


def _is_below_or_equal(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve_private_root(repository_root: Path, private_root: Path | None = None) -> Path:
    """Resolve a private root and require it to be disjoint from the repository."""
    repository_absolute = Path(os.path.abspath(repository_root))
    repository = repository_root.resolve()
    candidate = default_private_root() if private_root is None else private_root
    if not candidate.is_absolute():
        raise ValueError(
            f"private root must be absolute; set --private-root or {PRIVATE_ROOT_ENV}"
        )
    candidate_absolute = Path(os.path.abspath(candidate))
    if (
        _is_below_or_equal(candidate_absolute, repository_absolute)
        or _is_below_or_equal(repository_absolute, candidate_absolute)
    ):
        raise ValueError(
            "private root must be outside and not contain the repository; "
            "repository .private paths are forbidden for operational data"
        )
    resolved = candidate.resolve()
    if _is_below_or_equal(resolved, repository) or _is_below_or_equal(repository, resolved):
        raise ValueError(
            "private root must be outside and not contain the repository; "
            "repository .private paths are forbidden for operational data"
        )
    return resolved


def ensure_private_root(repository_root: Path, private_root: Path | None = None) -> Path:
    """Create the resolved private root and apply owner-only POSIX permissions."""
    resolved = resolve_private_root(repository_root, private_root)
    resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not resolved.is_dir():
        raise ValueError(f"private root is not a directory: {resolved}")
    try:
        resolved.chmod(0o700)
    except OSError:
        pass
    return resolved


def private_child(
    path: Path,
    repository_root: Path,
    private_root: Path | None,
    *,
    must_exist: bool,
) -> Path:
    """Require a file/directory path strictly below the external private root."""
    root = ensure_private_root(repository_root, private_root)
    repository_absolute = Path(os.path.abspath(repository_root))
    path_absolute = Path(os.path.abspath(path))
    if _is_below_or_equal(path_absolute, repository_absolute):
        raise ValueError(
            "operational private paths must not be located inside the repository"
        )
    resolved = path.resolve()
    if resolved == root or not _is_below_or_equal(resolved, root):
        raise ValueError(f"path must be a child of external private root {root}")
    if must_exist and not resolved.is_file():
        raise ValueError(f"input file does not exist: {resolved}")
    if not must_exist and resolved.exists():
        raise FileExistsError(f"refusing to overwrite existing path: {resolved}")
    return resolved
