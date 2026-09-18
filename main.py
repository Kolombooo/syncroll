"""Syncroll - one shared die, rolled on the server, for several phones at once.

Single process: FastAPI serves the static client and the /ws WebSocket.
Run with exactly one worker; rooms live in this process's memory.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from rooms import (
    MAX_MESSAGE_BYTES,
    ERROR_MESSAGES,
    Room,
    RoomError,
    RoomManager,
    error,
    valid_code,
)

SWEEP_INTERVAL_SECONDS = 10
STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("syncroll")

manager = RoomManager()


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


async def send(ws: WebSocket, payload: dict) -> None:
    """Send one message. A dead socket must never take the room down."""
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:  # noqa: BLE001 - the disconnect handler does the cleanup
        pass


async def send_error(ws: WebSocket, code: str) -> None:
    await send(ws, {"type": "error", "code": code, "message": ERROR_MESSAGES.get(code, "Error.")})


async def broadcast(room: Room) -> None:
    """Broadcast one snapshot taken before the first await, so every client
    sees the same view even when some sockets are slow."""
    snapshot = room.snapshot()
    targets = [p.ws for p in room.players.values() if p.connected and p.ws is not None]
    for ws in targets:
        await send(ws, snapshot)


# ---------------------------------------------------------------------------
# Grace-period sweep
# ---------------------------------------------------------------------------


async def sweep_loop() -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            for room in manager.sweep():
                # A seat expiring can complete a vote, so re-evaluate.
                room.evaluate_roll()
                await broadcast(room)
        except Exception:  # noqa: BLE001 - never let the sweeper die
            log.exception("sweep failed")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(sweep_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Syncroll", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


class Session:
    """What this socket is currently attached to."""

    def __init__(self) -> None:
        self.room: Room | None = None
        self.seat: int | None = None

    @property
    def in_room(self) -> bool:
        return self.room is not None and self.seat is not None

    def attach(self, room: Room, seat: int) -> None:
        self.room = room
        self.seat = seat

    def detach(self) -> None:
        self.room = None
        self.seat = None


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    session = Session()
    try:
        while True:
            raw = await ws.receive_text()
            if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
                await send_error(ws, "invalid_message")
                continue
            try:
                message = json.loads(raw)
            except (ValueError, TypeError):
                await send_error(ws, "invalid_message")
                continue
            if not isinstance(message, dict):
                await send_error(ws, "invalid_message")
                continue
            await handle(ws, session, message)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("socket error")
    finally:
        await on_disconnect(session, ws)


async def handle(ws: WebSocket, session: Session, message: dict) -> None:
    kind = message.get("type")
    try:
        if kind == "create":
            await do_create(ws, session)
        elif kind == "join":
            await do_join(ws, session, message)
        elif kind == "rejoin":
            await do_rejoin(ws, session, message)
        elif kind == "ready":
            await do_ready(ws, session, message)
        elif kind == "set_sides":
            await do_set_sides(ws, session, message)
        elif kind == "leave":
            await do_leave(ws, session)
        else:
            await send_error(ws, "invalid_message")
    except RoomError as exc:
        await send_error(ws, exc.code)


async def do_create(ws: WebSocket, session: Session) -> None:
    if session.in_room:
        raise error("already_in_room")
    room = manager.create()
    player = room.add_player(ws)
    session.attach(room, player.seat)
    log.info("room %s created by seat %s", room.code, player.seat)
    await send(ws, {"type": "joined", "code": room.code, "token": player.token, "seat": player.seat})
    await broadcast(room)


async def do_join(ws: WebSocket, session: Session, message: dict) -> None:
    if session.in_room:
        raise error("already_in_room")
    room = manager.get(message.get("code"))
    player = room.add_player(ws)
    session.attach(room, player.seat)
    await send(ws, {"type": "joined", "code": room.code, "token": player.token, "seat": player.seat})
    # A newcomer is not ready, so this cannot trigger a roll, but staying
    # uniform keeps every mutation followed by the same evaluate + broadcast.
    room.evaluate_roll()
    await broadcast(room)


async def do_rejoin(ws: WebSocket, session: Session, message: dict) -> None:
    if session.in_room:
        raise error("already_in_room")
    code = message.get("code")
    if not valid_code(code):
        raise error("invalid_code")
    room = manager.get(code)
    player, displaced = room.rejoin(message.get("token"), ws)
    session.attach(room, player.seat)
    if displaced is not None:
        # The same player reconnected before their old socket noticed it was
        # dead. Close the stale one; its disconnect handler will no-op because
        # the seat now belongs to this socket.
        with contextlib.suppress(Exception):
            await displaced.close()
    await send(ws, {"type": "joined", "code": room.code, "token": player.token, "seat": player.seat})
    room.evaluate_roll()
    await broadcast(room)


async def do_ready(ws: WebSocket, session: Session, message: dict) -> None:
    if not session.in_room:
        raise error("not_in_room")
    ready = message.get("ready")
    if not isinstance(ready, bool):
        raise error("invalid_message")
    room = session.room
    assert room is not None and session.seat is not None
    # Synchronous from here to the roll: no await can interleave, so two
    # final-ready messages arriving together still produce exactly one roll.
    room.set_ready(session.seat, ready)
    rolled = room.evaluate_roll()
    if rolled:
        log.info("room %s rolled %s on %s", room.code, rolled.value, rolled.sides)
    await broadcast(room)


async def do_set_sides(ws: WebSocket, session: Session, message: dict) -> None:
    if not session.in_room:
        raise error("not_in_room")
    room = session.room
    assert room is not None and session.seat is not None
    room.set_sides(session.seat, message.get("sides"))
    room.evaluate_roll()
    await broadcast(room)


async def do_leave(ws: WebSocket, session: Session) -> None:
    if not session.in_room:
        raise error("not_in_room")
    room = session.room
    seat = session.seat
    assert room is not None and seat is not None
    session.detach()
    room.remove(seat)
    if manager.drop_if_empty(room):
        log.info("room %s deleted", room.code)
        return
    room.evaluate_roll()
    await broadcast(room)


async def on_disconnect(session: Session, ws: WebSocket) -> None:
    """Socket closed without `leave`: hold the seat for the grace period but
    exclude the player from the roll condition right away."""
    if not session.in_room:
        return
    room = session.room
    seat = session.seat
    assert room is not None and seat is not None
    session.detach()
    if not room.owns_socket(seat, ws):
        # A newer connection already took this seat over; this is a stale
        # socket closing and must not knock the live player offline.
        return
    room.disconnect(seat)
    # The seat is held, so the room is never empty here; the sweeper deletes it
    # once every held seat has expired.
    room.evaluate_roll()
    await broadcast(room)


# The static mount must come last so /ws and /healthz win.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
