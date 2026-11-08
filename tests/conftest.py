import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from vinylcast.metrics import read_records

requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def events(path: Path, name: str | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        r
        for r in read_records(path)
        if r["kind"] == "event" and (name is None or r["event"] == name)
    ]


async def wait_until(cond: Callable[[], object], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.02)
