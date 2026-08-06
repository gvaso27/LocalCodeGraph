"""Enables ``python -m local_code_graph`` as an alternative to the ``lcg`` script."""

import sys

from local_code_graph.cli import main

if __name__ == "__main__":
    sys.exit(main())
