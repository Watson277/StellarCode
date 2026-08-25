"""PyInstaller entry point for the desktop Runtime Sidecar.

Development invokes ``python -m stellarcode.runtime.sidecar``. The release
bundle invokes this executable directly, so end users do not need Python or
the StellarCode source tree.
"""

from stellarcode.runtime.sidecar import main


if __name__ == "__main__":
    main()
