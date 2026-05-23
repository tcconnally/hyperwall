"""
PROJECT: HYPERWALL
VERSION: 8.1 (Structured Package)
AUTHOR:  Thomas Connally / Clio
DATE:    May 2026

Shim for the HyperWall package. The NVIDIA profile Inspector profile targets 
the basename 'hyperwall_v8' (either .exe or .py). This shim preserves that 
contract while delegating to the structured /hyperwall/ package.
"""

from hyperwall import main

if __name__ == "__main__":
    main()
