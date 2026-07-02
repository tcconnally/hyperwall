"""
PROJECT: HYPERWALL
AUTHOR:  Thomas Connally / Clio
DATE:    June 2026

Entry point shim. The NVIDIA Profile Inspector profile targets the versionless
basename 'hyperwall' (.exe when frozen). G-Sync isolation is gated on the
'hyperwall*.exe' prefix (or HYPERWALL_ISOLATED=1), so the exe name stays stable
across version bumps. This shim delegates to the structured /hyperwall/ package.
The single source of version truth is hyperwall/__init__.py:__version__.
"""

from hyperwall.app import main

if __name__ == "__main__":
    main()
