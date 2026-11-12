"""FastAPI application factory.

Demo mode (``APP_ENV=demo`` or no Airwallex keys): the in-memory simulator
backs the agents and a demo organization is seeded, so everything works
with zero keys. Live mode swaps in the real Airwallex REST client — same
graph, same API.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import Settings, load_settings
from ..db import Database
from ..security import LoginThrottle
from ..services.runtime import PlatformRuntime
from ..services.reminders import email_delivery_loop, funding_reminder_loop
from ..services.seed import seed_demo
from .routes import auth, bedata, chat, employers, platform, runs, tap, team
from .routes import contractor_trust
from .routes import funding_plan
from .routes import notifications

logger = logging.getLogger("paysilo")


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.session_secret()  # fail fast: live mode requires JWT_SECRET
    db = database or Database.from_url(settings.database_url)
    runtime = PlatformRuntime(settings, db)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings.auto_create_schema:
            db.create_schema()
        runtime.startup()
        if settings.seed_demo and settings.app_env != "live":
            with db.sessionmaker() as s:
                seed_demo(s, runtime)
        logger.info("PaySilo ready (%s mode, %s database)", "demo" if settings.demo_mode else "live",
                    db.engine.dialect.name)
        tasks: list[asyncio.Task] = []
        if settings.funding_reminders == "on":
            # Single-worker assumption: one process runs the sweep (dedupe keys protect against repeats).
            tasks.append(asyncio.create_task(funding_reminder_loop(db.sessionmaker, runtime, settings)))
            tasks.append(asyncio.create_task(email_delivery_loop(db.sessionmaker, settings)))
        yield
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        db.engine.dispose()

    app = FastAPI(title="PaySilo", version="0.2.0", lifespan=lifespan,
                  description="Multi-Employer Payroll Executor — the model reads, the code decides, "
                              "the API moves the money.")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Content-Type", "Authorization"])
    app.state.settings = settings
    app.state.db = db
    app.state.runtime = runtime
    app.state.login_throttle = LoginThrottle()
    app.state.onboarding_throttle = LoginThrottle(limit=20, window=300.0)
    for module in (platform, auth, employers, runs, chat, tap, bedata, team):
        app.include_router(module.router)
    app.include_router(contractor_trust.router)
    app.include_router(funding_plan.router)
    app.include_router(notifications.router)
    return app
