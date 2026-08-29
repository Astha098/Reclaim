"""Dump every dashboard read model to a JSON fixture.

    python3 scripts/fixture.py [--out data/fixture.json] [--count 400]

Two uses. It backs `scripts/render_check.mjs`, which renders the React dashboard
against this file so the frontend can be checked without a running backend or a
network. And it is a readable snapshot of the entire API contract in one file —
useful when you want to see what the dashboard actually consumes without reading
ten endpoint handlers.

Requests go through `httpx.ASGITransport` rather than calling the `db` functions
directly. That costs a lifespan harness, and buys exactness: the fixture is what
the API returns, including Pydantic's serialization and the `/api/taxonomy`
projection, rather than my second-hand reconstruction of it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["DEMO_TIME_COMPRESSION"] = "1"
os.environ.setdefault("DB_PATH", str(ROOT / "data" / "fixture.db"))

import logging  # noqa: E402

logging.disable(logging.INFO)

import httpx  # noqa: E402

from app.main import app  # noqa: E402

PATHS = [
    "/api/config",
    "/api/stats",
    "/api/buckets",
    "/api/attempts?limit=60",
    "/api/issuers",
    "/api/classifier",
    "/api/suppressions",
    "/api/timeline",
    "/api/scheduler",
    "/api/taxonomy",
]


class Lifespan:
    """Drive ASGI startup/shutdown, which `ASGITransport` does not do.

    Same shape as the one in `scripts/api_check.py`. Kept local rather than shared
    because a fixture dumper that imports a test script is a dependency nobody
    expects, and this is fifteen lines.
    """

    def __init__(self, app_) -> None:
        self.app = app_

    async def __aenter__(self) -> Lifespan:
        self._recv: asyncio.Queue = asyncio.Queue()
        self._send: asyncio.Queue = asyncio.Queue()
        await self._recv.put({"type": "lifespan.startup"})
        self._task = asyncio.create_task(
            self.app(
                {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}},
                self._recv.get,
                self._send.put,
            )
        )
        msg = await self._send.get()
        if msg["type"] != "lifespan.startup.complete":
            raise RuntimeError(f"app failed to start: {msg}")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._recv.put({"type": "lifespan.shutdown"})
        try:
            await asyncio.wait_for(self._send.get(), timeout=10)
            await asyncio.wait_for(self._task, timeout=10)
        except (TimeoutError, asyncio.TimeoutError):
            pass


async def build(count: int, seed: int, span: int, outage: bool) -> dict[str, object]:
    async with Lifespan(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture", timeout=120.0
    ) as c:
        await c.post("/api/demo/reset", json={})
        await c.post(
            "/api/demo/seed", json={"count": count, "seed": seed, "span_minutes": span}
        )
        await c.post("/api/demo/tick", json={"limit": count * 2})
        if outage:
            # An open circuit in the fixture is the more useful default: it is the
            # state the issuer panel exists to render, and a fixture where
            # everything is healthy would leave that path unexercised.
            await c.post("/api/demo/outage", json={"issuer": "HDFC", "rail": "card"})

        out: dict[str, object] = {}
        for path in PATHS:
            r = await c.get(path)
            r.raise_for_status()
            out[path] = r.json()
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "data" / "fixture.json"))
    ap.add_argument("--count", type=int, default=400)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--span-minutes", type=int, default=180)
    ap.add_argument("--no-outage", action="store_true")
    args = ap.parse_args()

    db_path = Path(os.environ["DB_PATH"])
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)

    fixture = asyncio.run(
        build(args.count, args.seed, args.span_minutes, not args.no_outage)
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, indent=1), encoding="utf-8")

    stats = fixture["/api/stats"]
    print(f"wrote {out.relative_to(ROOT)}  ({out.stat().st_size // 1024} KB)")
    print(
        f"  {stats['failed_payments']} failed · {stats['recovered_count']} recovered · "
        f"{stats['attempts_made']} attempts · {len(fixture['/api/attempts?limit=60'])} in feed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
