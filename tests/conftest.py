from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Keep crawler tests offline while still exercising URL validation."""

    from backend import security

    async def resolve(hostname: str, port: int):
        return ("93.184.216.34",)

    security._dns_cache.clear()
    monkeypatch.setattr("backend.security._resolve_host", resolve)
    return resolve


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")
