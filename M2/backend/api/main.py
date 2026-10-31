from __future__ import annotations

from pathlib import Path
import os

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api.service import DashboardService


ROOT = Path(__file__).resolve().parents[2]


def create_app(service: DashboardService | None = None) -> FastAPI:
    app = FastAPI(
        title="ETF Stress Research API",
        version="1.0.0",
        description=(
            "Frozen Holdout matrix and validation replay for the synthetic "
            "ETF leverage stress study."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.service = service or DashboardService(
        ROOT,
        ROOT / "config.m2_real.yaml",
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/metadata")
    def metadata(request: Request):
        return request.app.state.service.matrix()["metadata"]

    @app.get("/api/matrix")
    def matrix(request: Request):
        return request.app.state.service.matrix()

    @app.get("/api/backtest")
    def backtest(
        request: Request,
        group: str = Query("G4", pattern="^G[1-6]$"),
        leverage: float = Query(1, ge=1, le=100),
        initial_cash: float = Query(1000000, gt=0),
        cost_scenario: str = Query("base", pattern="^(low|base|high)$"),
        seed: int | None = None,
    ):
        try:
            return request.app.state.service.backtest(
                group=group,
                leverage=leverage,
                initial_cash=initial_cash,
                cost_scenario=cost_scenario,
                seed=seed,
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=422,
                content={"detail": str(exc)},
            )

    @app.get("/api/etf/{code}/daily")
    def daily(code: str, request: Request):
        payload = request.app.state.service.daily_bars(code)
        if not payload["bars"]:
            return JSONResponse(
                status_code=404,
                content={"detail": "ETF code has no validation bars"},
            )
        return payload

    reports = ROOT / "reports"
    if reports.exists():
        app.mount(
            "/reports",
            StaticFiles(directory=reports),
            name="reports",
        )
    frontend_dist = ROOT / "frontend/dist"
    if frontend_dist.exists():
        app.mount(
            "/",
            StaticFiles(directory=frontend_dist, html=True),
            name="frontend",
        )

    return app


app = create_app()

