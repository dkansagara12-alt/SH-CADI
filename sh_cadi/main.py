"""SH-CADI entry point.  Run from the project root:

    python -m sh_cadi.main                 # full project demo (scale section: 100,000 records)
    python -m sh_cadi.main 1000000         # same, scale section with 1,000,000 records (~2.2 GB RAM)
    python -m sh_cadi.main --quick         # full demo without the scale section
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sh_cadi.demo import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(0 if main(*sys.argv[1:]) else 1)
