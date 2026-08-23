"""Record the first real Health Auto Export payload, then replay it forever.

The ingest path is the one part of Umai that cannot be developed without a
phone — and the phone is the slowest possible edit-test loop. So: capture one
real delivery, commit it, and never need the phone again.

Capturing the *first successful* export matters more than it sounds. The
documented payload shape and the actual one are rarely identical, and the
difference is small, expensive and only discoverable against a real device. A
committed fixture turns that into a regression test instead of a rediscovery.

Usage:

    uv run python tools/replay_health.py --record
        Listens on port 8010 for exactly one POST, writes the body verbatim to
        tests/fixtures/health_<date>.json, prints what the parser makes of it,
        and exits. Point `tailscale serve` at 8010 for the one export:

            tailscale serve --bg --set-path /ingest/health http://127.0.0.1:8010/ingest/health
            # fire Health Auto Export's manual export, then put it back:
            tailscale serve --bg --set-path /ingest/health http://127.0.0.1:8000/ingest/health

    uv run python tools/replay_health.py --replay tests/fixtures/health_2026-08-22.json
        Feeds the file through the real ingest path against the dev database.
        Run it twice: the second run must write nothing and report duplicates.

    ... --replay <file> --dry-run
        Parses and prints without touching the database. The fast loop.

The body is written as raw bytes rather than re-serialised JSON: key order and
formatting are part of the evidence about what the app actually sends.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import uvicorn
from fastapi import FastAPI, Request

from umai.clock import SystemClock, local_date
from umai.config.settings import get_settings
from umai.ingest.health import extract, ingest

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
RECORD_PORT = 8010


def describe(payload: dict) -> None:
    """What the parser makes of a payload, without writing anything.

    Deliberately reports rejections too. `extract` never raises — a bad sample
    is collected as a reason rather than taking the delivery down — so the
    rejects are the only place a timezone-less timestamp or an implausible
    value becomes visible.
    """
    readings, rejects = extract(payload)
    print(f"  {len(readings)} readings, {len(rejects)} rejected")

    by_metric: dict[str, list] = {}
    for r in readings:
        by_metric.setdefault(r.metric, []).append(r)

    for metric, rows in sorted(by_metric.items()):
        stamps = sorted(r.recorded_at for r in rows)
        total = sum(r.value for r in rows)
        print(
            f"  {metric}: {len(rows)} samples, sum {total:,.0f}, "
            f"{stamps[0].isoformat()} .. {stamps[-1].isoformat()}"
        )
    for reason in rejects[:10]:
        print(f"  rejected: {reason}")


async def record(port: int, out_dir: Path) -> int:
    """Accept exactly one POST, save the body, print what it holds, stop.

    FastAPI and Request are imported at module level rather than here, because
    `from __future__ import annotations` stringifies the handler's annotations
    and FastAPI resolves them against module globals: a function-local import
    leaves `Request` unresolvable, and the parameter is silently demoted to a
    query string field, which answers every POST with a 422.
    """
    app = FastAPI()
    captured: dict[str, bytes] = {}
    done = asyncio.Event()

    @app.post("/ingest/health")
    async def capture(request: Request) -> dict:
        captured["body"] = await request.body()
        done.set()
        # 200 so Health Auto Export records the export as successful and does
        # not queue it for redelivery.
        return {"ok": True}

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    print(f"listening on 127.0.0.1:{port} for one POST to /ingest/health — ctrl-c to give up")
    try:
        await done.wait()
    finally:
        server.should_exit = True
        await serving

    body = captured["body"]
    out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - local disk, once, at exit
    path = out_dir / f"health_{local_date(SystemClock().now(), get_settings().tz)}.json"
    path.write_bytes(body)
    print(f"\nwrote {len(body):,} bytes to {path}")

    try:
        describe(json.loads(body))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"  (could not parse: {exc}) — the raw body is saved regardless")
    return 0


async def replay(path: Path, dry_run: bool) -> int:
    payload = json.loads(path.read_text())  # noqa: ASYNC240 - one small local file
    print(f"{path.name}:")
    describe(payload)

    if dry_run:
        return 0

    from sqlalchemy import select

    from umai.db.models import User
    from umai.db.session import dispose_engine, init_engine, session_scope

    init_engine(get_settings())
    try:
        async with session_scope() as session:
            # Inlined rather than reusing web.api._single_user_id, which raises
            # HTTPException — an exception type with no business in a CLI.
            user_id = (await session.execute(select(User.id).limit(1))).scalar_one_or_none()
            if user_id is None:
                print("no user registered yet; send the bot a message first")
                return 1
            report = await ingest(session, user_id, payload)
        print(f"  -> {report.summary}")
        for reason in report.rejects:
            print(f"     {reason}")
    finally:
        await dispose_engine()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--record", action="store_true", help="capture one real payload")
    mode.add_argument("--replay", metavar="FILE", help="feed a saved payload through ingest")
    ap.add_argument("--port", type=int, default=RECORD_PORT)
    ap.add_argument("--out", type=Path, default=FIXTURES)
    ap.add_argument("--dry-run", action="store_true", help="parse only, no database")
    args = ap.parse_args()

    if args.record:
        return asyncio.run(record(args.port, args.out))
    return asyncio.run(replay(Path(args.replay), args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
