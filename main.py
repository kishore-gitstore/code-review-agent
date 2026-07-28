"""
Code Review Agent — Main Entry Point
GitHub Copilot (Claude Sonnet) + GitLab MR reviews
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn
from app.server import create_app

app = create_app()

if __name__ == "__main__":
    cfg_host = os.getenv("APP_HOST", "0.0.0.0")
    cfg_port = int(os.getenv("APP_PORT", "8000"))
    try:
        uvicorn.run(
            "main:app",
            host=cfg_host,
            port=cfg_port,
            reload=True,
            log_level="info",
        )
    except KeyboardInterrupt:
        pass
