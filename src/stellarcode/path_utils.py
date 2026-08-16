from __future__ import annotations

import os
from pathlib import Path


def subprocess_safe_path(path: str | Path) -> Path:
    r"""Return an absolute path suitable for Windows shell child processes.

    Windows canonicalization can preserve the verbatim ``\\?\`` prefix supplied by
    Tauri. Python accepts it, but CMD and Conda treat it like a UNC working directory.
    """

    resolved = Path(path).resolve()
    if os.name != "nt":
        return resolved
    value = str(resolved)
    unc_prefix = "\\\\?\\UNC\\"
    verbatim_prefix = "\\\\?\\"
    if value.upper().startswith(unc_prefix.upper()):
        return Path("\\\\" + value[len(unc_prefix) :])
    if value.startswith(verbatim_prefix):
        return Path(value[len(verbatim_prefix) :])
    return resolved
