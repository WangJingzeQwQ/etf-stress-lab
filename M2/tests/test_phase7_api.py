from __future__ import annotations

from fastapi.testclient import TestClient

from backend.api.main import create_app


class _FakeService:
    def matrix(self):
        return {
            "metadata": {
                "p0_1_frozen": True,
                "financing_mode": "synthetic_stress",
                "cost_model": "parameterized_etf_v1",
            },
            "cells": [],
        }

    def backtest(self, **kwargs):
        return {
            "metadata": {
                **kwargs,
                "split_id": "validation",
                "p0_1_frozen": True,
            },
            "metrics": {"total_return": 0.1},
            "equity_curve": [],
            "trades": [],
            "daily_snapshot": [],
            "target_portfolio": [],
        }

    def daily_bars(self, code):
        return {
            "code": code,
            "bars": [{"code": code, "close": 1.0}],
        }


def test_api_exposes_frozen_metadata_and_validation_replay():
    client = TestClient(create_app(_FakeService()))
    assert client.get("/api/health").json() == {"status": "ok"}
    metadata = client.get("/api/metadata").json()
    assert metadata["p0_1_frozen"] is True
    assert metadata["financing_mode"] == "synthetic_stress"
    result = client.get(
        "/api/backtest",
        params={
            "group": "G4",
            "leverage": 10,
            "initial_cash": 1000000,
            "cost_scenario": "base",
        },
    ).json()
    assert result["metadata"]["split_id"] == "validation"
    assert result["metadata"]["p0_1_frozen"] is True
    assert client.get("/api/etf/510300/daily").json()["code"] == "510300"


def test_api_rejects_unknown_group():
    client = TestClient(create_app(_FakeService()))
    response = client.get("/api/backtest", params={"group": "G9"})
    assert response.status_code == 422

