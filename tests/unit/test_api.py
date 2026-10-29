"""Tests for the FastAPI surface."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_analyze_returns_a_report(client: TestClient) -> None:
    response = client.post(
        "/analyze",
        json={"repo": "acme/billing-service", "pr_number": 42},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"]
    assert body["report"]["context"]["repo"] == "acme/billing-service"
    assert body["report"]["context"]["pr_number"] == 42
    assert body["report"]["findings"] == []


def test_analyze_run_ids_are_unique(client: TestClient) -> None:
    payload = {"repo": "acme/billing-service", "pr_number": 42}

    first = client.post("/analyze", json=payload).json()["run_id"]
    second = client.post("/analyze", json=payload).json()["run_id"]

    assert first != second


def test_analyze_rejects_a_non_positive_pr_number(client: TestClient) -> None:
    response = client.post("/analyze", json={"repo": "acme/x", "pr_number": 0})

    assert response.status_code == 422


def test_analyze_requires_repo_and_pr_number(client: TestClient) -> None:
    response = client.post("/analyze", json={})

    assert response.status_code == 422
