"""
PROJECT: HYPERWALL
AUTHOR:  Thomas Connally / Clio
DATE:    June 2026

Entry point shim for the macOS-native structured `hyperwall/` package.
`launch.sh` establishes the Homebrew libmpv path before this module imports Qt.
"""

from hyperwall.app import main

if __name__ == "__main__":
    main()
