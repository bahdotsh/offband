from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fakes import ME, FakeApi
from offband.node import Node


@pytest.fixture
async def node(tmp_path: Path):
    """A node with a workspace, on the fake local API."""
    n = Node(FakeApi(), "Me", tmp_path / "ws")  # type: ignore[arg-type]
    n.workspace.mkdir(parents=True)
    n.address = ME
    yield n
    for task in list(n._tasks):
        task.cancel()
    await asyncio.gather(*n._tasks, return_exceptions=True)
