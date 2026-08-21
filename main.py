"""
Code Review Agent — Main Entry Point
Multi-agent GitLab MR reviews, powered by whichever LLM_PROVIDER is configured in .env
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if sys.platform == "win32":
    # uvicorn --reload resets Windows back to SelectorEventLoop, which can't
    # spawn subprocesses (asyncio.create_subprocess_exec) — needed here for
    # nvidia-smi hardware detection and the static-analysis linters.
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import uvicorn
from app.server import create_app

app = create_app()

if __name__ == "__main__":
    cfg_host = os.getenv("APP_HOST", "0.0.0.0")
    cfg_port = int(os.getenv("APP_PORT", "8000"))
    # Auto-reload is a dev convenience (run.sh enables it) — off by default so
    # running this under systemd/a process supervisor doesn't re-exec on every
    # file touch in production.
    cfg_reload = os.getenv("UVICORN_RELOAD", "false").lower() == "true"
    try:
        uvicorn.run(
            "main:app",
            host=cfg_host,
            port=cfg_port,
            reload=cfg_reload,
            log_level="info",
        )
    except KeyboardInterrupt:
        pass
