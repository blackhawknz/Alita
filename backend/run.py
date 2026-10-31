"""
Alita Backend Runner — Entry point for the Alita agent server.
"""

import os
import sys
import uvicorn
from dotenv import load_dotenv

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Force UTF-8 stdout/stderr — Windows defaults to cp1252 which cannot encode
# the box-drawing chars or emoji used in the banner / log output below.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

from alita.api.security import assert_safe_bind, configured_token  # noqa: E402


def main():
    """Start the Alita API server."""
    # Loopback by default — /tools/execute runs arbitrary shell, so do not
    # expose it to the network unless the operator explicitly opts in.
    host = os.getenv("ALITA_HOST", "127.0.0.1")
    port = int(os.getenv("ALITA_PORT", "8080"))
    reload = os.getenv("ALITA_DEBUG", "false").lower() == "true"

    # Fail-closed: refuse to bind a non-loopback interface without a token.
    assert_safe_bind(host, configured_token())

    from alita import __version__

    print(f"\n  Alita Agent Server v{__version__}\n"
          f"  API:    http://{host}:{port}\n"
          f"  Docs:   http://{host}:{port}/docs\n"
          f"  Health: http://{host}:{port}/health\n")

    uvicorn.run(
        "alita.api.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
