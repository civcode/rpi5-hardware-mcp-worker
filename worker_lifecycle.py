"""Graceful MCP server lifecycle without overriding Uvicorn's signal handlers.

Uvicorn/ASGI owns SIGINT and SIGTERM while serving. Once its run() returns
(or raises KeyboardInterrupt), clean up worker-managed resources explicitly.
The atexit fallback in server.py covers other normal interpreter exits.
"""
from __future__ import annotations

import sys
from typing import Callable


def run_with_cleanup(run_server: Callable[[], None],
                     close_worker: Callable[[], None]) -> None:
    """Stop on Ctrl+C and always close the hardware worker before returning.

    No MCU reset, flash write, or explicit target halt/resume is requested.
    Closing the worker terminates only its owned persistent GDB OpenOCD server.
    """
    try:
        run_server()
    except KeyboardInterrupt:
        # Some run() backends propagate SIGINT; Uvicorn typically returns on
        # its own graceful SIGINT shutdown instead.
        pass
    finally:
        print("Shutting down Raspberry Pi hardware MCP worker...", file=sys.stderr, flush=True)
        close_worker()
        print("Hardware MCP worker stopped.", file=sys.stderr, flush=True)
