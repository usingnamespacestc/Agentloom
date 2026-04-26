"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agentloom import __version__, tenancy_runtime
from agentloom.api import (
    chatflows,
    folders,
    health,
    mcp_servers,
    providers,
    tools,
    workflows,
    workspace_settings as workspace_settings_api,
)
from agentloom.config import get_settings
from agentloom.db.base import get_session_maker
from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
from agentloom.db.repositories.chatflow import (
    backfill_missing_node_index,
    sweep_orphaned_running_nodes,
)
from agentloom.db.repositories.mcp_server import MCPServerRepository
from agentloom.db.repositories.workspace_settings import WorkspaceSettingsRepository
from agentloom.mcp import runtime as mcp_runtime


#: How often the orphan-sweep watchdog re-runs during the lifespan of
#: a live process. Start-up sweep catches orphans from the PREVIOUS
#: run's crash; the watchdog catches coroutine leaks that happen while
#: the current process is still alive (e.g. a scheduler task raising
#: an unhandled exception and silently exiting without transitioning
#: the RUNNING node to FAILED).
#:
#: Default 15 minutes — long enough to keep DB chatter low, short
#: enough that a user returning from coffee doesn't stare at a ghost
#: "running" indicator.
ORPHAN_WATCHDOG_INTERVAL_SECONDS = 15 * 60


async def _orphan_watchdog(
    app: FastAPI,
    interval_seconds: int = ORPHAN_WATCHDOG_INTERVAL_SECONDS,
) -> None:
    """Periodically clean orphan RUNNING nodes that the in-memory engine
    no longer tracks. Skips chatflows that currently HAVE active
    scheduler tasks so a live turn can't be flipped mid-execution.

    Cancelled cleanly on shutdown from the lifespan hook. Individual
    iteration failures are logged and swallowed — a transient DB
    error shouldn't kill the watchdog and leave the process without
    the safety net.
    """
    log = logging.getLogger(__name__)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise
        try:
            engine = getattr(app.state, "chatflow_engine", None)
            # Use ``attached_chatflow_ids`` rather than
            # ``active_chatflow_ids``: a long-running turn (qwen36
            # auto_plan can take minutes) has transient windows where
            # ``active_tasks`` is momentarily empty between scheduler
            # task transitions. Watchdog using the narrower set in
            # those windows would write ``status=failed`` rows and
            # the eventual succeeded save would trip frozen-guard.
            # Attach lifecycle is the right granularity — once a
            # chatflow has a runtime, it's engine-owned end-to-end.
            attached_ids: set[str] = (
                engine.attached_chatflow_ids() if engine is not None else set()
            )
            cleaned = await sweep_orphaned_running_nodes(
                get_session_maker(),
                skip_chatflow_ids=attached_ids,
            )
            if cleaned:
                log.info(
                    "orphan_watchdog: cleaned %d stale node(s) "
                    "(skipped %d attached chatflow(s))",
                    cleaned,
                    len(attached_ids),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — watchdog must survive one-off errors
            log.exception("orphan_watchdog: iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown hooks.

    MCP connect is fired as a background task so a slow or unreachable
    remote server can't block the app from serving. Tools that reference
    not-yet-connected MCP servers will 404 until the background task
    finishes — that's the tradeoff for non-blocking startup.
    """
    mcp_runtime.init_runtime()
    mcp_task: asyncio.Task[None] | None = None
    try:
        session_maker = get_session_maker()
        async with session_maker() as session:
            settings_repo = WorkspaceSettingsRepository(
                session, workspace_id=DEFAULT_WORKSPACE_ID
            )
            ws_settings = await settings_repo.get()
            tenancy_runtime.set_settings(DEFAULT_WORKSPACE_ID, ws_settings)
            repo = MCPServerRepository(session, workspace_id=DEFAULT_WORKSPACE_ID)
            configs = await repo.list_all()
        mcp_task = asyncio.create_task(
            mcp_runtime.load_and_connect_all(configs), name="mcp-connect-all"
        )
    except Exception as exc:  # noqa: BLE001 — never fail-fast on MCP boot
        logging.getLogger(__name__).exception("mcp: startup load failed")
        print(f"mcp: startup load failed: {exc!r}", flush=True)
    try:
        backfilled = await backfill_missing_node_index(get_session_maker())
        if backfilled:
            logging.getLogger(__name__).info(
                "node_index: backfilled %d pre-existing chatflow(s)", backfilled
            )
    except Exception:  # noqa: BLE001 — backfill is best-effort
        logging.getLogger(__name__).exception("node_index: backfill failed")
    try:
        orphaned = await sweep_orphaned_running_nodes(get_session_maker())
        if orphaned:
            logging.getLogger(__name__).info(
                "orphan_sweep: transitioned %d stale running/retrying/waiting node(s) to failed",
                orphaned,
            )
    except Exception:  # noqa: BLE001 — sweep is best-effort, never fail startup
        logging.getLogger(__name__).exception("orphan_sweep: failed")
    # Spawn the periodic orphan watchdog. Unlike the startup sweep this
    # one runs throughout the process lifetime and skips chatflows that
    # currently have live scheduler tasks (so it never clobbers a
    # legitimate in-flight turn).
    watchdog_task: asyncio.Task[None] | None = None
    try:
        watchdog_task = asyncio.create_task(
            _orphan_watchdog(app), name="orphan-watchdog"
        )
    except Exception:  # noqa: BLE001 — watchdog is best-effort, never fail startup
        logging.getLogger(__name__).exception("orphan_watchdog: failed to start")
    try:
        yield
    finally:
        # Aggressive shutdown for `--reload` cycles. Without this,
        # any in-flight ``submit_turn`` whose worker is mid-LLM-call
        # blocks ``uvicorn``'s graceful shutdown until the call finishes
        # naturally (often minutes for qwen36 / auto_plan). The watcher
        # then prints "Waiting for background tasks to complete" and
        # the dev has to ``kill -9`` the process. We instead cancel
        # every attached chatflow runtime first — each ``detach``
        # sends ``CancelledError`` into the runtime's active scheduler
        # tasks and awaits ``runtime.drain()`` — so by the time
        # uvicorn checks "any work left?" the answer is reliably no.
        # Trade-off: an in-flight turn loses its assistant draft if
        # the user happens to reload backend code mid-LLM-call. That's
        # the right priority in dev: don't let one stuck turn force
        # a manual kill.
        engine = getattr(app.state, "chatflow_engine", None)
        if engine is not None:
            for cf_id in list(engine.attached_chatflow_ids()):
                try:
                    await engine.detach(cf_id, cancel=True)
                except Exception:  # noqa: BLE001 — best-effort drain
                    logging.getLogger(__name__).exception(
                        "lifespan: detach failed for chatflow %s", cf_id
                    )
        if mcp_task is not None and not mcp_task.done():
            mcp_task.cancel()
            try:
                await mcp_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if watchdog_task is not None and not watchdog_task.done():
            watchdog_task.cancel()
            try:
                await watchdog_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await mcp_runtime.close_all()


_LOGGING_CONFIGURED = False


def _configure_logging(level_name: str) -> None:
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    level = getattr(logging, level_name.upper(), logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    app_logger = logging.getLogger("agentloom")
    app_logger.setLevel(level)
    app_logger.addHandler(handler)
    app_logger.propagate = False
    _LOGGING_CONFIGURED = True


def create_app() -> FastAPI:
    settings = get_settings()
    _configure_logging(settings.log_level)
    app = FastAPI(
        title="Agentloom",
        version=__version__,
        description="Visual agent workflow platform.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"] if settings.environment == "dev" else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, tags=["health"])
    app.include_router(workflows.router)
    app.include_router(chatflows.router)
    app.include_router(folders.router)
    app.include_router(providers.router)
    app.include_router(mcp_servers.router)
    app.include_router(tools.router)
    app.include_router(workspace_settings_api.router)

    # τ-bench benchmark sessions — only registered when tau_bench is
    # importable (i.e. backend/vendor/tau_bench present). Off by
    # default in production-style deploys; harmless to leave on for
    # dev since the endpoint just exposes session create/teardown.
    try:
        from agentloom.benchmarks.tau_bench.api import router as tau_bench_router

        app.include_router(tau_bench_router)
    except ImportError:  # pragma: no cover — vendor missing in stripped builds
        pass

    @app.exception_handler(RequestValidationError)
    async def _log_422(request: Request, exc: RequestValidationError) -> JSONResponse:
        from fastapi.encoders import jsonable_encoder
        body = b""
        try:
            body = await request.body()
        except Exception:  # noqa: BLE001
            pass
        logging.getLogger(__name__).warning(
            "422 on %s %s\n  content-type=%s\n  headers=%s\n  errors=%s\n  body=%s",
            request.method,
            request.url.path,
            request.headers.get("content-type"),
            dict(request.headers),
            exc.errors(),
            body[:2000],
        )
        # ``exc.errors()`` may embed non-JSON-serializable objects (bytes,
        # ValueError instances) in the ``input``/``ctx`` fields.
        # ``jsonable_encoder`` coerces them to strings so the response can
        # serialize.
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

    return app


app = create_app()
