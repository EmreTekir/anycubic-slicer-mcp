"""Run the server from a source checkout: python server.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from anycubic_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    main()
