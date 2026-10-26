from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path


async def run_io(method, *args, tasks=None):
    """Drain an already-started file transaction before releasing its caller."""
    task = asyncio.create_task(asyncio.to_thread(method, *args))
    if tasks is not None:
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


def read_json(path, default):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, separators=(",", ":")))
