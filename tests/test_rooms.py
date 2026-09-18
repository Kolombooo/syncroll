"""Unit tests for the pure room logic (no sockets involved)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rooms import (  # noqa: E402
    CATAN,
    MAX_PLAYERS,
    MIN_PLAYERS_TO_ROLL,
    RECONNECT_GRACE_SECONDS,
    RoomError,
    RoomManager,
    parse_die,
)


@pytest.fixture
def manager():
    return RoomManager()


def room_with(manager, n):
    room = manager.create()
    players = [room.add_player() for _ in range(n)]
    return room, players


# --- rooms & joining -------------------------------------------------------


def test_create_gives_four_digit_code_and_admin(manager):
    room = manager.create()
    assert len(room.code) == 4 and room.code.isdigit()
    player = room.add_player()
    assert player.seat == 1
    assert room.admin_seat == 1


def test_codes_keep_leading_zeros(manager):
    # Codes are always strings, so "0042" must survive as-is.
    manager.rooms["0042"] = manager.create()
    assert manager.get("0042") is manager.rooms["0042"]


def test_get_validates_code(manager):
    with pytest.raises(RoomError) as exc:
        manager.get("42")
    assert exc.value.code == "invalid_code"
    with pytest.raises(RoomError) as exc:
        manager.get("9999")
    assert exc.value.code == "room_not_found"


def test_room_full(manager):
    room, _ = room_with(manager, MAX_PLAYERS)
    with pytest.raises(RoomError) as exc:
        room.add_player()
    assert exc.value.code == "room_full"


def test_rooms_are_independent(manager):
    a, (a1, a2) = room_with(manager, 2)
    b, (b1, b2) = room_with(manager, 2)
    a.set_ready(a1.seat, True)
    a.set_ready(a2.seat, True)
    assert a.evaluate_roll() is not None
    assert b.evaluate_roll() is None


def test_join_mid_round_is_not_ready(manager):
    room, (p1, p2) = room_with(manager, 2)
    room.set_ready(p1.seat, True)
    p3 = room.add_player()
    assert p3.ready is False
    assert room.evaluate_roll() is None


# --- rolling ---------------------------------------------------------------


def test_single_player_never_rolls(manager):
    room, (p1,) = room_with(manager, 1)
    room.set_ready(p1.seat, True)
    assert MIN_PLAYERS_TO_ROLL == 2
    assert room.evaluate_roll() is None


def test_roll_on_last_ready_resets_and_increments(manager):
    room, (p1, p2) = room_with(manager, 2)
    room.set_ready(p1.seat, True)
    assert room.evaluate_roll() is None
    room.set_ready(p2.seat, True)
    roll = room.evaluate_roll()
    assert roll is not None
    assert roll.roll_no == 1
    assert all(not p.ready for p in room.players.values())
    assert room.last_roll is roll


def test_values_stay_in_range(manager):
    room, (p1, p2) = room_with(manager, 2)
    room.set_sides(p1.seat, "2")
    seen = set()
    for _ in range(60):
        room.set_ready(p1.seat, True)
        room.set_ready(p2.seat, True)
        roll = room.evaluate_roll()
        assert 1 <= roll.value <= 2
        seen.add(roll.value)
    assert seen == {1, 2}


def test_double_final_ready_rolls_once(manager):
    room, (p1, p2) = room_with(manager, 2)
    room.set_ready(p1.seat, True)
    room.set_ready(p2.seat, True)
    first = room.evaluate_roll()
    # A second "last ready" arriving right behind the first finds everyone
    # already reset, so it cannot roll again.
    second = room.evaluate_roll()
    assert first is not None and second is None
    assert room.roll_no == 1


def test_unready_prevents_roll(manager):
    room, (p1, p2) = room_with(manager, 2)
    room.set_ready(p1.seat, True)
    room.set_ready(p2.seat, True)
    room.set_ready(p1.seat, False)
    assert room.evaluate_roll() is None


def test_catan_die_is_two_d6(manager):
    room, (p1, p2) = room_with(manager, 2)
    room.set_sides(p1.seat, CATAN)
    for _ in range(40):
        room.set_ready(p1.seat, True)
        room.set_ready(p2.seat, True)
        roll = room.evaluate_roll()
        assert len(roll.values) == 2
        assert all(1 <= v <= 6 for v in roll.values)
        assert roll.value == sum(roll.values)
        assert roll.to_dict()["sides"] == CATAN


# --- admin & die -----------------------------------------------------------


def test_only_admin_sets_sides(manager):
    room, (p1, p2) = room_with(manager, 2)
    with pytest.raises(RoomError) as exc:
        room.set_sides(p2.seat, "20")
    assert exc.value.code == "not_admin"


@pytest.mark.parametrize("bad", ["1", "1001", "abc", "", "6.5", None, True, 1.5, 0])
def test_invalid_sides_rejected(bad):
    with pytest.raises(RoomError) as exc:
        parse_die(bad)
    assert exc.value.code == "invalid_sides"


@pytest.mark.parametrize("good,expected", [("2", "2"), ("1000", "1000"), (20, "20"), ("CATAN", CATAN)])
def test_valid_sides_accepted(good, expected):
    assert parse_die(good) == expected


def test_changing_die_resets_ready(manager):
    room, (p1, p2) = room_with(manager, 2)
    room.set_ready(p1.seat, True)
    room.set_ready(p2.seat, True)
    room.evaluate_roll()
    room.set_ready(p1.seat, True)
    room.set_sides(p1.seat, "20")
    assert all(not p.ready for p in room.players.values())
    assert room.evaluate_roll() is None


def test_result_keeps_the_die_it_was_rolled_on(manager):
    room, (p1, p2) = room_with(manager, 2)
    room.set_sides(p1.seat, "6")
    room.set_ready(p1.seat, True)
    room.set_ready(p2.seat, True)
    room.evaluate_roll()
    room.set_sides(p1.seat, "20")
    assert room.last_roll.sides == "6"
    assert room.snapshot()["sides"] == 20
    assert room.snapshot()["last_roll"]["sides"] == 6


def test_admin_leaving_hands_over_to_lowest_connected_seat(manager):
    room, (p1, p2, p3) = room_with(manager, 3)
    room.disconnect(p2.seat)
    room.remove(p1.seat)
    assert room.admin_seat == p3.seat   # p2 is disconnected, so p3 takes it


def test_admin_keeps_role_across_reconnect(manager):
    room, (p1, p2) = room_with(manager, 2)
    room.disconnect(p1.seat)
    assert room.admin_seat == p1.seat
    room.rejoin(p1.token)
    assert room.admin_seat == p1.seat


def test_admin_passes_on_rejoin_when_nobody_connected(manager):
    room, (p1, p2) = room_with(manager, 2)
    room.disconnect(p1.seat)
    room.disconnect(p2.seat)
    room.remove(p1.seat)            # admin's grace expired
    assert room.admin_seat is None
    room.rejoin(p2.token)
    assert room.admin_seat == p2.seat


# --- disconnects -----------------------------------------------------------


def test_disconnected_player_does_not_block_roll(manager):
    room, (p1, p2, p3) = room_with(manager, 3)
    room.set_ready(p1.seat, True)
    room.set_ready(p2.seat, True)
    assert room.evaluate_roll() is None
    room.disconnect(p3.seat)
    roll = room.evaluate_roll()
    assert roll is not None


def test_rejoin_restores_seat_ready_and_token(manager):
    room, (p1, p2) = room_with(manager, 2)
    room.set_ready(p1.seat, True)
    room.disconnect(p1.seat)
    back, displaced = room.rejoin(p1.token)
    assert back.seat == p1.seat
    assert back.ready is True
    assert back.connected is True
    assert back.disconnected_at is None


def test_bad_token(manager):
    room, (p1,) = room_with(manager, 1)
    with pytest.raises(RoomError) as exc:
        room.rejoin("nope")
    assert exc.value.code == "bad_token"


def test_grace_expiry_removes_seat(manager):
    room, (p1, p2) = room_with(manager, 2)
    room.disconnect(p1.seat, now=0.0)
    assert room.expired_seats(now=RECONNECT_GRACE_SECONDS - 1) == []
    assert room.expired_seats(now=RECONNECT_GRACE_SECONDS) == [p1.seat]
    for seat in room.expired_seats(now=RECONNECT_GRACE_SECONDS):
        room.remove(seat)
    with pytest.raises(RoomError):
        room.rejoin(p1.token)


def test_seats_are_never_reused(manager):
    room, (p1, p2) = room_with(manager, 2)
    room.remove(p1.seat)
    p3 = room.add_player()
    assert p3.seat == 3


def test_room_deleted_when_last_player_goes(manager):
    room, (p1,) = room_with(manager, 1)
    code = room.code
    room.remove(p1.seat)
    assert manager.drop_if_empty(room) is True
    assert code not in manager.rooms


def test_sweep_drops_empty_rooms_and_reports_changed(manager):
    empty_room, (a1,) = room_with(manager, 1)
    empty_room.disconnect(a1.seat, now=0.0)

    live_room, (b1, b2) = room_with(manager, 2)
    live_room.disconnect(b1.seat, now=0.0)

    changed = manager.sweep(now=RECONNECT_GRACE_SECONDS + 1)
    assert empty_room.code not in manager.rooms
    assert changed == [live_room]
    assert live_room.players.keys() == {b2.seat}
    assert live_room.admin_seat == b2.seat


def test_snapshot_has_no_tokens_and_is_seat_ordered(manager):
    room, players = room_with(manager, 3)
    room.disconnect(players[1].seat)
    snap = room.snapshot()
    assert [p["seat"] for p in snap["players"]] == [1, 2, 3]
    assert "token" not in str(snap)
    assert snap["last_roll"] is None
    assert snap["players"][1]["connected"] is False


def test_rejoin_displaces_the_previous_socket(manager):
    room, (p1, p2) = room_with(manager, 2)
    old, new = object(), object()
    room.players[p1.seat].ws = old
    player, displaced = room.rejoin(p1.token, new)
    assert displaced is old
    assert player.ws is new
    # The stale socket closing later must not knock the live player offline.
    assert room.owns_socket(p1.seat, old) is False
    assert room.owns_socket(p1.seat, new) is True
