import sys

from .cli import main

if sys.argv[0].endswith("__main__.py"):
    sys.argv[0] = "vedit"

sys.exit(main())
