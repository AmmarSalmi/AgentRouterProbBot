"""Run from a checkout with Python 3.11+; no package installation is needed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentrouter_prober.export_session import main


if __name__ == "__main__":
    raise SystemExit(main())
