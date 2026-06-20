"""
PROJECT: HYPERWALL
VERSION: 9.0 (Ground-Up Rewrite)
AUTHOR:  Thomas Connally / Clio
DATE:    June 2026

Entry point shim. The NVIDIA Profile Inspector profile targets the basename
'hyperwall_v8' (either .exe or .py). This shim preserves that contract while
delegating to the structured /hyperwall/ package.
"""

from hyperwall.app import main

if __name__ == "__main__":
    main()
