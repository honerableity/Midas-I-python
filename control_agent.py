"""
MIDAS I - Control Agent

Runs a small authenticated HTTP API for the dashboard and manages bot.py
as a child process.

Endpoints:
    POST /start
    POST /stop
    POST /restart
    GET  /status
    GET  /logs

Authentication:
    x-api-key: <CONTROL_API_KEY>

Environment:
    CONTROL_API_KEY or BOT_CONTROL_API_KEY
    PORT (default: 16107)
    BOT_SCRIPT (default: bot.py)
    BOT_WORKDIR (default: directory containing this file)
    AUTO_START (default: true)
    LOG_LIMIT (default: 1000)
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections import deque
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
import uvicorn


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

BOT_WORKDIR = Path(
    os.getenv("BOT_WORKDIR", str(BASE_DIR))
).resolve()

BOT_SCRIPT = os.getenv("BOT_SCRIPT", "bot.py")

# Prefer CONTROL_API_KEY, then BOT_CONTROL_API_KEY.
API_KEY = (
    os.getenv("CONTROL_API_KEY")
    or os.getenv("BOT_CONTROL_API_KEY")
)

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "16107"))

AUTO_START = os.getenv("AUTO_START", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

LOG_LIMIT = int(os.getenv("LOG_LIMIT", "1000"))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

app = FastAPI(
    title="MIDAS I Control API",
    version="1.0.0",
)

process: Optional[asyncio.subprocess.Process] = None
process_started_at: Optional[float] = None

log_buffer: deque[dict] = deque(maxlen=LOG_LIMIT)

process_lock = asyncio.Lock()
reader_tasks: set[asyncio.Task] = set()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def add_log(
    message: str,
    level: str = "info",
) -> None:
    entry = {
        "ts": int(time.time() * 1000),
        "level": level,
        "message": message,
    }

    log_buffer.append(entry)

    # Also show it in WispByte console.
    print(
        f"[control-agent][{level.upper()}] {message}",
        flush=True,
    )


async def read_process_stream(
    stream: asyncio.StreamReader,
    level: str = "info",
) -> None:
    while True:
        line = await stream.readline()

        if not line:
            break

        text = line.decode(
            "utf-8",
            errors="replace",
        ).rstrip()

        if text:
            add_log(text, level)


async def monitor_process() -> None:
    global process
    global process_started_at

    current = process

    if current is None:
        return

    return_code = await current.wait()

    # Give the readers a chance to consume their remaining output.
    await asyncio.sleep(0.1)

    if return_code == 0:
        add_log(
            f"bot.py exited normally with code {return_code}.",
            "warning",
        )
    else:
        add_log(
            f"bot.py exited unexpectedly with code {return_code}.",
            "error",
        )

    if process is current:
        process = None
        process_started_at = None


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def require_api_key(
    x_api_key: Optional[str],
) -> None:
    if not API_KEY:
        raise HTTPException(
            status_code=500,
            detail=(
                "Control API key is not configured. "
                "Set CONTROL_API_KEY in the environment."
            ),
        )

    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key.",
        )


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

def bot_path() -> Path:
    path = (BOT_WORKDIR / BOT_SCRIPT).resolve()

    if not path.exists():
        raise FileNotFoundError(
            f"Bot script not found: {path}"
        )

    return path


def is_running() -> bool:
    global process

    if process is None:
        return False

    return process.returncode is None


def current_status() -> dict:
    if is_running():
        uptime = (
            max(0, int(time.time() - process_started_at))
            if process_started_at
            else 0
        )

        return {
            "state": "online",
            "uptimeSeconds": uptime,
        }

    # Check whether the last log indicates a failure.
    last_error = None

    for entry in reversed(log_buffer):
        if entry["level"] == "error":
            last_error = entry["message"]
            break

    return {
        "state": "offline",
        "uptimeSeconds": 0,
        **(
            {"lastError": last_error}
            if last_error
            else {}
        ),
    }


async def start_bot() -> dict:
    global process
    global process_started_at

    async with process_lock:
        if is_running():
            return {
                "ok": True,
                "status": "already_running",
            }

        path = bot_path()

        add_log(
            f"Starting bot: {path}",
            "info",
        )

        # Use the same Python executable that started the control agent.
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(path),
            cwd=str(BOT_WORKDIR),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        process_started_at = time.time()

        add_log(
            f"bot.py started with PID {process.pid}.",
            "info",
        )

        stdout_task = asyncio.create_task(
            read_process_stream(
                process.stdout,
                "info",
            )
        )

        stderr_task = asyncio.create_task(
            read_process_stream(
                process.stderr,
                "error",
            )
        )

        monitor_task = asyncio.create_task(
            monitor_process()
        )

        reader_tasks.update({
            stdout_task,
            stderr_task,
            monitor_task,
        })

        def cleanup(task: asyncio.Task) -> None:
            reader_tasks.discard(task)

        stdout_task.add_done_callback(cleanup)
        stderr_task.add_done_callback(cleanup)
        monitor_task.add_done_callback(cleanup)

        return {
            "ok": True,
            "status": "started",
        }


async def stop_bot() -> dict:
    global process
    global process_started_at

    async with process_lock:
        if not is_running():
            process = None
            process_started_at = None

            return {
                "ok": True,
                "status": "already_stopped",
            }

        current = process

        add_log(
            f"Stopping bot PID {current.pid}...",
            "warning",
        )

        try:
            if os.name == "nt":
                current.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(
                    current.pid,
                    signal.SIGTERM,
                )

            try:
                await asyncio.wait_for(
                    current.wait(),
                    timeout=10,
                )
            except asyncio.TimeoutError:
                add_log(
                    "Graceful shutdown timed out; killing bot.",
                    "warning",
                )

                if os.name == "nt":
                    current.kill()
                else:
                    os.killpg(
                        current.pid,
                        signal.SIGKILL,
                    )

                await current.wait()

        except ProcessLookupError:
            pass
        except Exception as exc:
            add_log(
                f"Failed to stop bot: {exc}",
                "error",
            )
            raise

        finally:
            if process is current:
                process = None
                process_started_at = None

        add_log(
            "Bot stopped.",
            "info",
        )

        return {
            "ok": True,
            "status": "stopped",
        }


async def restart_bot() -> dict:
    add_log(
        "Restart requested.",
        "warning",
    )

    await stop_bot()

    # Small delay so the old process/socket resources settle.
    await asyncio.sleep(0.5)

    return await start_bot()


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {
        "ok": True,
        "service": "MIDAS I Control API",
        "status": current_status(),
    }


@app.get("/status")
async def status(
    x_api_key: Optional[str] = Header(
        default=None,
        alias="x-api-key",
    ),
):
    require_api_key(x_api_key)

    return current_status()


@app.get("/logs")
async def logs(
    x_api_key: Optional[str] = Header(
        default=None,
        alias="x-api-key",
    ),
    since: Optional[str] = Query(
        default=None,
    ),
    level: Optional[str] = Query(
        default=None,
    ),
    limit: int = Query(
        default=500,
        ge=1,
        le=5000,
    ),
):
    require_api_key(x_api_key)

    entries = list(log_buffer)

    # Optional timestamp filtering.
    if since:
        try:
            since_value = int(since)

            entries = [
                entry
                for entry in entries
                if entry["ts"] > since_value
            ]
        except ValueError:
            pass

    if level:
        level_lower = level.lower()

        entries = [
            entry
            for entry in entries
            if entry["level"] == level_lower
        ]

    entries = entries[-limit:]

    return {
        "lines": entries,
        "nextCursor": (
            str(entries[-1]["ts"])
            if entries
            else ""
        ),
    }


@app.post("/start")
async def start(
    x_api_key: Optional[str] = Header(
        default=None,
        alias="x-api-key",
    ),
):
    require_api_key(x_api_key)

    try:
        return await start_bot()
    except FileNotFoundError as exc:
        add_log(str(exc), "error")

        raise HTTPException(
            status_code=500,
            detail=str(exc),
        )
    except Exception as exc:
        add_log(
            f"Start failed: {exc}",
            "error",
        )

        raise HTTPException(
            status_code=500,
            detail="Failed to start bot.",
        )


@app.post("/stop")
async def stop(
    x_api_key: Optional[str] = Header(
        default=None,
        alias="x-api-key",
    ),
):
    require_api_key(x_api_key)

    try:
        return await stop_bot()
    except Exception as exc:
        add_log(
            f"Stop failed: {exc}",
            "error",
        )

        raise HTTPException(
            status_code=500,
            detail="Failed to stop bot.",
        )


@app.post("/restart")
async def restart(
    x_api_key: Optional[str] = Header(
        default=None,
        alias="x-api-key",
    ),
):
    require_api_key(x_api_key)

    try:
        return await restart_bot()
    except Exception as exc:
        add_log(
            f"Restart failed: {exc}",
            "error",
        )

        raise HTTPException(
            status_code=500,
            detail="Failed to restart bot.",
        )


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup():
    add_log(
        f"Control API starting on {HOST}:{PORT}",
        "info",
    )

    add_log(
        f"Bot workdir: {BOT_WORKDIR}",
        "info",
    )

    add_log(
        f"Bot script: {BOT_SCRIPT}",
        "info",
    )

    if not API_KEY:
        add_log(
            "WARNING: CONTROL_API_KEY is not configured.",
            "error",
        )

    if AUTO_START:
        try:
            await start_bot()
        except Exception as exc:
            add_log(
                f"Auto-start failed: {exc}",
                "error",
            )


@app.on_event("shutdown")
async def on_shutdown():
    if is_running():
        add_log(
            "Control API shutting down; stopping bot...",
            "warning",
        )

        try:
            await stop_bot()
        except Exception as exc:
            add_log(
                f"Shutdown stop failed: {exc}",
                "error",
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
    )
