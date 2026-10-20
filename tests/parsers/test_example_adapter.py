"""The demonstration adapter of the snapshot: the contract, the registry, no network."""

from __future__ import annotations

import socket

import pytest

from src.parsers.capabilities import SourceCapabilities, capabilities_by_key
from src.parsers.example.adapter import ExampleAdapter
from src.parsers.registry import load_all_adapters


async def _collect(since=None):
    return [raw async for raw in ExampleAdapter().fetch_batch(since)]


@pytest.mark.asyncio
async def test_fetch_and_normalize_local_file(monkeypatch):
    def no_net(*a, **k):
        raise AssertionError("the example adapter must not go to the network")

    monkeypatch.setattr(socket, "socket", no_net)
    raws = await _collect()
    assert [ExampleAdapter().build_source_uid(r) for r in raws] == [
        "EX-0001",
        "EX-0002",
    ]
    first = ExampleAdapter().normalize(raws[0])
    assert first.source_key == "example" and first.title == "Supply of office furniture"
    assert first.amount_eur == 12000.0 and first.deadline_at is not None
    assert first.classifications == [{"scheme": "CPV", "code": "39130000"}]
    second = ExampleAdapter().normalize(raws[1])
    assert second.deadline_at is None and second.amount_eur is None


@pytest.mark.asyncio
async def test_since_filters_published():
    from datetime import datetime, timezone

    raws = await _collect(datetime(2026, 1, 16, tzinfo=timezone.utc))
    assert [r.payload["id"] for r in raws] == ["EX-0002"]


def test_registry_has_only_example():
    assert set(load_all_adapters()) == {"example"}
    assert isinstance(capabilities_by_key()["example"], SourceCapabilities)
