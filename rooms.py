"""Pure room logic for Syncroll. No WebSocket or FastAPI imports live here.

Everything in this module mutates state *synchronously*; the transport layer is
responsible for taking a snapshot and awaiting the broadcast afterwards.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Iterable

# ---------------------------------------------------------------------------
# Constants (Product.md section 5)
# ---------------------------------------------------------------------------

CODE_LENGTH = 4
MIN_SIDES = 2
MAX_SIDES = 1000
DEFAULT_SIDES = 6
MIN_PLAYERS_TO_ROLL = 2
MAX_PLAYERS = 50
RECONNECT_GRACE_SECONDS = 60
MAX_MESSAGE_BYTES = 1024

# The Catan die (2d6) is a die spec that is not a plain side count.
CATAN = "catan"

MAX_CODES = 10 ** CODE_LENGTH


class RoomError(Exception):
    """Carries one of the protocol error codes from Product.md section 11.3."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


ERROR_MESSAGES = {
    "invalid_message": "Malformed message.",
    "invalid_code": "Room code must be exactly 4 digits.",
    "room_not_found": "No room with that code.",
    "room_full": "That room is full.",
    "server_full": "No free room codes, try again later.",
    "bad_token": "Your session has expired.",
    "not_admin": "Only the admin can change the die.",
    "invalid_sides": f"Die must be D{MIN_SIDES}-D{MAX_SIDES} or the Catan die.",
    "not_in_room": "You are not in a room.",
    "already_in_room": "You are already in a room.",
}


def error(code: str) -> RoomError:
    return RoomError(code, ERROR_MESSAGES.get(code, "Something went wrong."))


# ---------------------------------------------------------------------------
# Die specs
# ---------------------------------------------------------------------------


def parse_die(raw: object) -> str:
    """Normalise a client-supplied die spec to "catan" or a digit string.

    Accepts strings (as in the protocol) and ints (tolerated for convenience).
    Raises RoomError("invalid_sides") for anything else.
    """
    if isinstance(raw, bool):
        raise error("invalid_sides")
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value == CATAN:
            return CATAN
        if not value.isdigit():
            raise error("invalid_sides")
        number = int(value)
    elif isinstance(raw, int):
        number = raw
    else:
        raise error("invalid_sides")
    if not MIN_SIDES <= number <= MAX_SIDES:
        raise error("invalid_sides")
    return str(number)


def die_public(die: str) -> str | int:
    """The value put on the wire: an int for numeric dice, "catan" otherwise."""
    return CATAN if die == CATAN else int(die)


def roll_die(die: str) -> tuple[int, list[int]]:
    """Roll `die` on the server. Returns (value, individual dice values)."""
    if die == CATAN:
        a = secrets.randbelow(6) + 1
        b = secrets.randbelow(6) + 1
        return a + b, [a, b]
    value = secrets.randbelow(int(die)) + 1
    return value, [value]


def valid_code(code: object) -> bool:
    return isinstance(code, str) and len(code) == CODE_LENGTH and code.isdigit()


# ---------------------------------------------------------------------------
# Data model (Product.md section 10)
# ---------------------------------------------------------------------------


@dataclass
class Player:
    seat: int
    token: str
    ws: object | None = None
    ready: bool = False
    connected: bool = True
    disconnected_at: float | None = None


@dataclass
class LastRoll:
    roll_no: int
    value: int
    sides: str
    values: list[int]

    def to_dict(self) -> dict:
        return {
            "roll_no": self.roll_no,
            "value": self.value,
            "sides": die_public(self.sides),
            "values": list(self.values),
        }


@dataclass
class Room:
    code: str
    sides: str = str(DEFAULT_SIDES)
    admin_seat: int | None = None
    players: dict[int, Player] = field(default_factory=dict)
    next_seat: int = 1
    roll_no: int = 0
    last_roll: LastRoll | None = None

    # -- queries ------------------------------------------------------------

    def connected_players(self) -> list[Player]:
        return [p for p in self.players.values() if p.connected]

    def is_empty(self) -> bool:
        return not self.players

    def snapshot(self) -> dict:
        """The `state` message. Built before any await; contains no tokens."""
        return {
            "type": "state",
            "code": self.code,
            "sides": die_public(self.sides),
            "admin_seat": self.admin_seat,
            "players": [
                {"seat": p.seat, "ready": p.ready, "connected": p.connected}
                for p in sorted(self.players.values(), key=lambda p: p.seat)
            ],
            "last_roll": self.last_roll.to_dict() if self.last_roll else None,
        }

    # -- mutations ----------------------------------------------------------

    def add_player(self, ws: object | None = None) -> Player:
        if len(self.players) >= MAX_PLAYERS:
            raise error("room_full")
        player = Player(seat=self.next_seat, token=secrets.token_urlsafe(16), ws=ws)
        self.next_seat += 1
        self.players[player.seat] = player
        if self.admin_seat is None:
            self.admin_seat = player.seat
        return player

    def rejoin(self, token: object, ws: object | None = None) -> tuple[Player, object | None]:
        """Resume a held seat. Returns the player and any socket it displaced
        (the same person reconnecting before the old socket noticed it died);
        the caller should close that one."""
        if not isinstance(token, str):
            raise error("bad_token")
        for player in self.players.values():
            if secrets.compare_digest(player.token, token):
                displaced = player.ws if player.ws is not ws else None
                player.ws = ws
                player.connected = True
                player.disconnected_at = None
                if self.admin_seat is None:
                    self._reassign_admin()
                return player, displaced
        raise error("bad_token")

    def owns_socket(self, seat: int, ws: object) -> bool:
        """Is `ws` still the socket attached to `seat`? False once a newer
        connection has taken the seat over."""
        player = self.players.get(seat)
        return player is not None and player.ws is ws

    def set_ready(self, seat: int, ready: object) -> None:
        player = self.players.get(seat)
        if player is None:
            raise error("not_in_room")
        player.ready = bool(ready)

    def set_sides(self, seat: int, raw: object) -> None:
        if self.admin_seat != seat:
            raise error("not_admin")
        die = parse_die(raw)
        self.sides = die
        # Nobody should get rolled onto a die they did not vote for.
        self.reset_ready()

    def reset_ready(self) -> None:
        for player in self.players.values():
            player.ready = False

    def disconnect(self, seat: int, now: float | None = None) -> None:
        """Mark a seat as dropped. The seat, token, ready state and admin role
        are held until the grace period expires."""
        player = self.players.get(seat)
        if player is None:
            return
        player.connected = False
        player.ws = None
        player.disconnected_at = time.monotonic() if now is None else now

    def remove(self, seat: int) -> None:
        if self.players.pop(seat, None) is None:
            return
        if self.admin_seat == seat:
            self.admin_seat = None
            self._reassign_admin()

    def _reassign_admin(self) -> None:
        connected = sorted(p.seat for p in self.players.values() if p.connected)
        self.admin_seat = connected[0] if connected else None

    def expired_seats(self, now: float | None = None) -> list[int]:
        now = time.monotonic() if now is None else now
        return [
            p.seat
            for p in self.players.values()
            if not p.connected
            and p.disconnected_at is not None
            and now - p.disconnected_at >= RECONNECT_GRACE_SECONDS
        ]

    # -- the core loop ------------------------------------------------------

    def should_roll(self) -> bool:
        connected = self.connected_players()
        if len(connected) < MIN_PLAYERS_TO_ROLL:
            return False
        return all(p.ready for p in connected)

    def evaluate_roll(self) -> LastRoll | None:
        """Roll if the condition holds. Fully synchronous: no awaits inside."""
        if not self.should_roll():
            return None
        value, values = roll_die(self.sides)
        self.roll_no += 1
        self.last_roll = LastRoll(
            roll_no=self.roll_no, value=value, sides=self.sides, values=values
        )
        self.reset_ready()
        return self.last_roll


class RoomManager:
    """Owns every room. In memory only; rooms vanish on restart."""

    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}

    def create(self) -> Room:
        if len(self.rooms) >= MAX_CODES:
            raise error("server_full")
        while True:
            code = f"{secrets.randbelow(MAX_CODES):0{CODE_LENGTH}d}"
            if code not in self.rooms:
                break
        room = Room(code=code)
        self.rooms[code] = room
        return room

    def get(self, code: object) -> Room:
        if not valid_code(code):
            raise error("invalid_code")
        room = self.rooms.get(code)  # type: ignore[arg-type]
        if room is None:
            raise error("room_not_found")
        return room

    def drop_if_empty(self, room: Room) -> bool:
        if room.is_empty() and self.rooms.get(room.code) is room:
            del self.rooms[room.code]
            return True
        return False

    def sweep(self, now: float | None = None) -> list[Room]:
        """Remove seats past their grace period. Returns rooms that changed."""
        changed: list[Room] = []
        for room in list(self.rooms.values()):
            expired = room.expired_seats(now)
            if not expired:
                continue
            for seat in expired:
                room.remove(seat)
            if not self.drop_if_empty(room):
                changed.append(room)
        return changed

    def all_rooms(self) -> Iterable[Room]:
        return self.rooms.values()
