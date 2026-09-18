# Syncroll — Product Spec

> **Audience:** Claude (and any developer) implementing Syncroll.
> **Scope of this file:** behavior, rules, data model, protocol, architecture.
> **Not in this file:** visual design, layout, colors, typography, animation, copy. Those live in **`Design.md`** (not yet written). If the two documents ever conflict, **Product.md wins on behavior, Design.md wins on appearance.**

---

## 1. Overview

**Syncroll** is a tiny real-time web app for rolling one shared die across several phones at the same moment.

Players open a URL on their phones, join a room with a 4-digit code, and each taps a big button to say "I'm ready." When **every connected player** is ready, the server rolls the die and all phones show the same result at the same time.

Primary use case: **Catan Rush** (everyone must agree before the next roll). It is generic enough for any game that needs a synchronized, cheat-proof random number.

### One-sentence pitch
Everyone taps ready, the die rolls for all phones at once.

---

## 2. Goals and Non-Goals

### Goals
- Zero friction: open a URL, type 4 digits, play. No accounts, no install, no sign-up.
- Phone-first. One big, obvious button. Usable one-handed.
- Fair and consistent: **the server rolls**, never the client. Everyone sees the identical result.
- Runs as **a single Docker container**: one Python process serves both the static page and the WebSocket. No separate frontend service, no build step.
- Resilient to phones locking their screens and dropping connections.

### Non-Goals (v1)
- No accounts, logins, or persistence. Rooms live in memory and disappear on restart.
- No database, no Redis, no horizontal scaling.
- No chat, no player names/avatars, no roll history, no multi-dice (e.g. 2d6), no sound (see §16 for future ideas).
- No native app. It is a web page.

---

## 3. Users and Scenario

| Role | Description |
|------|-------------|
| **Admin** | The player who created the room. Chooses the die. Otherwise plays like everyone else. |
| **Player** | Anyone who joined with the room code. |

Typical session: 3–6 people around a table, each with a phone. One person creates a room and reads out the 4-digit code. Others join. Admin picks D6 (or anything from D2 up). Each round, every player taps the big button; when all are green, the die rolls and everybody sees the number. The button resets to gray and the next round begins.

---

## 4. Glossary

| Term | Meaning |
|------|---------|
| **Room** | A session identified by a 4-digit code. Holds players, the current die, and the last roll. |
| **Code** | 4-digit string, `"0000"`–`"9999"`. Leading zeros are valid. **Always a string, never an int.** |
| **Seat** | Small integer assigned to a player on join (1, 2, 3, …), unique within the room, never reused. Public identifier. |
| **Token** | Secret random string given only to its owner, used to rejoin after a disconnect. Never broadcast. |
| **Ready** | A player's vote: gray = not ready, green = ready. |
| **Connected** | The player's WebSocket is currently open. |
| **Roll** | The server generating a random result for the current die and broadcasting it. |
| **Sides** | Number of faces on the die. "D20" means `sides = 20`. |

---

## 5. Constants

Define once in code (top of `main.py` or a `config` section); do not scatter magic numbers.

| Constant | Value | Notes |
|----------|-------|-------|
| `CODE_LENGTH` | `4` | Digits in room code |
| `MIN_SIDES` | `2` | D2 (a coin) |
| `MAX_SIDES` | `1000` | The "X" in D2–DX |
| `DEFAULT_SIDES` | `6` | Die a new room starts with |
| `MIN_PLAYERS_TO_ROLL` | `2` | Connected players required for a roll |
| `MAX_PLAYERS` | `50` | Hard cap per room |
| `RECONNECT_GRACE_SECONDS` | `60` | How long a disconnected player's seat is held |
| `MAX_MESSAGE_BYTES` | `1024` | Reject larger incoming messages |

---

## 6. Core Behavior

### 6.1 Room lifecycle
1. A room is created when a client sends `create`. The server picks an unused random 4-digit code.
2. The creator becomes the **admin** and holds seat 1. The room starts with `sides = DEFAULT_SIDES`, no last roll, and the admin not ready.
3. Other clients join with `join` + code.
4. A room is **deleted** when it has no players left (all left, or all disconnected past the grace period).
5. If all 10,000 codes are in use, `create` fails with `server_full`.

### 6.2 Roles
- Exactly one admin per room at any time.
- Only the admin may change the die.
- If the admin **leaves** (explicit `leave`) or their **grace period expires**, admin passes to the connected player with the lowest seat number. If nobody is connected, admin passes when someone rejoins (lowest seat among connected).
- If the admin merely disconnects and rejoins within the grace period, they keep the admin role.

### 6.3 The die
- Any integer from `MIN_SIDES` to `MAX_SIDES` (D2 … D1000).
- Catan die (2d6) also availible.
- The admin can change it at any time.
- **Changing the die resets every player's ready state to false.** Rationale: nobody should get rolled onto a die they didn't vote for.
- The die is **never** rolled on the client. The server uses `secrets.randbelow(sides) + 1` (uniform, 1..sides).

### 6.4 Ready and roll (the core loop)
- Each player has a boolean `ready`, initially `false`.
- A player may toggle their own ready at any time (tap to go green, tap again to go gray).
- **After every state change** (ready toggled, player joined/left/disconnected/reconnected, die changed) the server evaluates the roll condition:

```
roll if:
    number of CONNECTED players >= MIN_PLAYERS_TO_ROLL
    AND every CONNECTED player has ready == true
```

- When the condition holds, in one atomic step (no `await` between them):
  1. Generate `value`.
  2. Increment `roll_no`.
  3. Store `last_roll = { roll_no, value, sides }`.
  4. Reset `ready = false` for **all** players.
  5. Broadcast the new state.
- Because step 1–4 happen synchronously before any `await`, two "last ready" events arriving together can never cause a double roll.
- After a roll, the next round starts immediately; the previous result stays visible until the next roll replaces it.

### 6.5 Joining
- Joining is allowed at any time, including mid-round.
- A new player starts with `ready = false`. This intentionally delays a roll that was about to happen. The newcomer must also vote.
- Joining a full room (`MAX_PLAYERS`) fails with `room_full`.
- A new joiner immediately receives the current state, including the last roll (shown statically, see §11).

### 6.6 Leaving and disconnecting
Two different events:

| Event | Trigger | Server behavior |
|-------|---------|-----------------|
| **Leave** | Client sends `leave` | Player is removed immediately. Admin reassigned if needed. Roll condition re-evaluated. |
| **Disconnect** | WebSocket closes without `leave` (phone locked, network drop, tab killed) | Player is marked `connected = false` and **excluded from the roll condition immediately**. Their seat, token, ready state, and admin role are held for `RECONNECT_GRACE_SECONDS`. After that, they are removed as in *Leave*. |

**Disconnected players must never block the room.** Roll condition is evaluated over connected players only. It is intentional that a disconnect can complete a vote (for example 3 players, 2 ready, the third's phone locks: the other two are now all-ready and the die rolls). The returning player will see the latest result on reconnect.

### 6.7 Rejoining
- On successful `create`/`join`, the server returns a `token`. The client stores `{code, token}` in `localStorage`.
- After a dropped connection (and on page reload), the client sends `rejoin` with `{code, token}`.
- If the token matches a held seat, the player resumes as the same seat (same admin role if any). `ready` is preserved.
- If the token is invalid or expired, the server replies `bad_token` (or `room_not_found`) and the client clears its stored session and returns to the Home screen.

---

## 7. User Flows

### 7.1 Create a room
1. Home → "Create room".
2. Client opens the WebSocket and sends `create`.
3. Server replies `joined` (with `code`, `token`, `seat`) then `state`.
4. Admin lands in the room. The code is displayed prominently so others can be told it.

### 7.2 Join a room
1. Home → "Join room" → enter 4 digits.
2. Client sends `join` with the code.
3. On success: `joined` then `state`. On failure: `error` (`room_not_found`, `room_full`, `invalid_code`).

### 7.3 A round
1. Everyone sees a big gray button.
2. Each player taps it, and it turns green for that player. Everyone sees how many players are ready (e.g. "3 / 5").
3. When the last connected player turns green, the server rolls.
4. All phones show the result. All buttons reset to gray.

### 7.4 Admin changes the die
1. Admin opens the die picker (presets plus a custom number input).
2. Client sends `set_sides`. Server validates, resets all ready states, broadcasts.
3. Everyone sees the new die label.

### 7.5 Reconnect after screen lock
1. Phone wakes, socket is dead. Client detects close, retries with backoff.
2. Client sends `rejoin` with stored `{code, token}`.
3. Client receives `state`. It shows the current result statically, with no re-animation of an old roll.

---

## 8. Screens and States (behavior only)

> Layout, styling, and animation are defined in **Design.md**. This section defines *what states exist and what each must allow*.

| Screen / State | When | Must show | Must allow |
|----------------|------|-----------|-----------|
| **Home** | No active session | Create and Join entry points | Create room; enter code and join |
| **Join entry** | User chose Join | 4-digit input | Submit code; go back; show inline error |
| **Waiting** | In a room with fewer than `MIN_PLAYERS_TO_ROLL` connected | Room code, player count, hint to invite others | Leave; admin can change die; button **disabled or clearly inert** |
| **Voting** | ≥ 2 connected players | Big central button (gray = not ready, green = ready), ready count "n / total", current die label, room code | Toggle ready; leave; admin can change die |
| **Result** | Directly after a roll | The rolled value and the die it came from (e.g. "D20 → 13"), buttons reset to gray | Toggle ready to start next round (result stays visible) |
| **Reconnecting** | Socket down, retrying | Non-blocking "reconnecting…" indicator | Nothing else; UI is inert until back |
| **Session lost** | Token expired / room gone / server restarted | Clear message | Return to Home |

**Derived state (client logic):**
- `Waiting` if `connected_count < MIN_PLAYERS_TO_ROLL`, else `Voting`.
- The **admin-only** controls are visible only when `admin_seat === my_seat`.
- "Result" is simply `Voting` with a fresh `last_roll`. There is no separate server phase.

**The big button** is the centerpiece: full-width, thumb-reachable, unmistakable gray/green. Exact size, colors, and motion are in Design.md.

---

## 9. Architecture

### 9.1 Constraints
- **Language / framework:** Python 3.12, **FastAPI**, served by **uvicorn**.
- **One container, one process, one port (8000).** FastAPI serves:
  - the static client from `./static` (mounted at `/`, must be mounted **last** so `/ws` and `/healthz` take precedence), and
  - a WebSocket endpoint at `/ws`.
- **Client:** plain HTML, CSS, and vanilla JavaScript. **No build step, no npm, no framework, no CDN dependencies.** A single `index.html` plus optional `app.js` / `style.css` under `static/`.
- **State:** in-memory `dict[str, Room]`. No database.
- **Single worker only.** Never run more than one uvicorn worker or replica; rooms are not shared across processes.
- **HTTPS is terminated in front of the container** (Caddy, Traefik, cloud proxy). The client must use `wss://` when the page is `https://`, i.e. derive the WebSocket URL from `location`.

### 9.2 Concurrency notes
- The server is a single asyncio event loop. **Mutate room state synchronously**, then `await` the broadcast. Do not `await` between reading the roll condition and applying a roll.
- Broadcast a **snapshot** built before the first `await`, so slow sockets can't cause inconsistent views.
- Sending to a dead socket must never crash the room. Catch, ignore, and let the disconnect handler clean up.

### 9.3 Grace-period timers
- On disconnect, start an `asyncio` task (or store a deadline and sweep periodically) that removes the player after `RECONNECT_GRACE_SECONDS`.
- Cancel the timer if the player rejoins.
- A periodic sweep every ~10 s is acceptable and simpler than per-player tasks.

---

## 10. Data Model (server)

```python
@dataclass
class Player:
    seat: int                 # public, unique per room, never reused
    token: str                # secret, for rejoin (secrets.token_urlsafe(16))
    ws: WebSocket | None      # None when disconnected
    ready: bool = False
    connected: bool = True
    disconnected_at: float | None = None   # monotonic time, for grace expiry

@dataclass
class LastRoll:
    roll_no: int              # 1, 2, 3, ... per room
    value: int
    sides: int                # the die used (admin may change it afterwards)

@dataclass
class Room:
    code: str                 # "0000".."9999"
    sides: int = DEFAULT_SIDES
    admin_seat: int | None = None
    players: dict[int, Player]        # seat -> Player
    next_seat: int = 1
    roll_no: int = 0
    last_roll: LastRoll | None = None
```

**Keep the pure room logic separate from WebSocket plumbing** (e.g. `rooms.py` with methods like `join()`, `set_ready()`, `set_sides()`, `disconnect()`, `evaluate_roll()`), so it can be unit-tested without sockets.

---

## 11. WebSocket Protocol (v1)

Endpoint: `GET /ws` (upgrade). All messages are JSON text frames with a `type` field. Reject frames larger than `MAX_MESSAGE_BYTES` and non-JSON frames with `invalid_message`.

### 11.1 Client → Server

| `type` | Fields                          | Allowed when | Effect |
|--------|---------------------------------|--------------|--------|
| `create` | none                            | Not in a room | Creates a room; sender is admin (seat 1) |
| `join` | `code: string`                  | Not in a room | Joins an existing room as a new player |
| `rejoin` | `code: string`, `token: string` | Not in a room | Resumes a held seat |
| `set_sides` | `sides: string`                 | In room, admin only | Sets die; resets everyone's `ready` |
| `ready` | `ready: bool`                   | In room | Sets own ready state |
| `leave` | none                            | In room | Removes player immediately |

### 11.2 Server → Client

**`joined`** — sent once after `create`, `join`, or `rejoin` succeeds:
```json
{ "type": "joined", "code": "4821", "token": "kX9...", "seat": 2 }
```

**`state`** — full snapshot, sent to every connected player after **every** change:
```json
{
  "type": "state",
  "code": "4821",
  "sides": 20,
  "admin_seat": 1,
  "players": [
    { "seat": 1, "ready": true,  "connected": true  },
    { "seat": 2, "ready": false, "connected": true  },
    { "seat": 3, "ready": false, "connected": false }
  ],
  "last_roll": { "roll_no": 3, "value": 13, "sides": 20 }
}
```
- `last_roll` is `null` until the first roll.
- Tokens are **never** included.
- Players are ordered by seat.
- The client computes `connected_count`, ready count, and "am I admin" from this snapshot.

**`error`**:
```json
{ "type": "error", "code": "room_not_found", "message": "No room with that code." }
```

### 11.3 Error codes

| `code` | Meaning |
|--------|---------|
| `invalid_message` | Malformed JSON, unknown `type`, wrong field types, or oversize frame |
| `invalid_code` | Code is not exactly 4 digits |
| `room_not_found` | No such room |
| `room_full` | `MAX_PLAYERS` reached |
| `server_full` | No free room codes |
| `bad_token` | `rejoin` token doesn't match a held seat |
| `not_admin` | Non-admin sent `set_sides` |
| `invalid_sides` | `sides` not an integer within `[MIN_SIDES, MAX_SIDES]` |
| `not_in_room` | Message requires a room but the socket hasn't joined one |
| `already_in_room` | `create`/`join`/`rejoin` sent while already in a room |

Errors are **non-fatal**: the socket stays open unless the message was unparseable garbage repeated abusively.

### 11.4 Client handling rules
- **Detecting a new roll:** compare `last_roll.roll_no` to the last one this client has seen. Only when it *increases* during a live session does the client play the "new result" reveal. The first `state` after `joined` (join, rejoin, page reload) shows the existing result **statically**, with no animation, to avoid confusing people who reconnect.
- The same die value twice in a row is still a new roll. Use `roll_no`, never the value, to detect it.
- The server is the single source of truth. The client never predicts a roll and may show optimistic ready state only if it reconciles with the next `state`.

---

## 12. Client Requirements

- **Reconnect with backoff:** on socket close, retry (e.g. 0.5 s, 1 s, 2 s, capped ~5 s), then `rejoin` using the stored `{code, token}`. Also reconnect immediately on `visibilitychange` → visible.
- **Persist session:** store `{code, token}` in `localStorage` on `joined`; clear it on `leave`, `bad_token`, or `room_not_found`.
- **Screen Wake Lock:** request `navigator.wakeLock` while in a room; re-acquire when the page becomes visible again. If unsupported or not a secure context, skip silently. Never show an error for it.
- **WebSocket URL:** build from `location` (`wss:` if `https:`, else `ws:`), path `/ws`.
- **Input handling:** the join field is numeric (`inputmode="numeric"`, `maxlength=4`, `pattern="[0-9]*"`); trim whitespace; validate `^\d{4}$` before sending.
- **Prevent accidental double-taps and zoom** on the big button (`touch-action: manipulation`, no double-tap zoom).
- **Accessibility:** the ready state and the roll result must be conveyed by more than color alone (text or icon), since gray/green fails for color-blind users. Details in Design.md.
- Optional but nice: `navigator.vibrate` on roll where supported.

---

## 13. Non-Functional Requirements

| Area | Requirement |
|------|-------------|
| **Latency** | Tap → all phones updated in under ~200 ms on a normal connection |
| **Scale** | Comfortable for dozens of concurrent rooms and up to `MAX_PLAYERS` per room on one small instance |
| **Fairness** | Uniform result from `secrets.randbelow`; roll computed only on server |
| **Resilience** | Dropped/locked phones recover automatically; no room deadlocks because of an absent player |
| **Security/abuse** | Validate every message; cap frame size; tokens unguessable and never broadcast; no HTML injection (only numbers and booleans are shown, but still never `innerHTML` unvalidated strings) |
| **Privacy** | No accounts, no cookies required, no personal data collected or stored |
| **Restart behavior** | All rooms are lost on restart. Clients handle this by returning to Home with a "session lost" message |
| **Observability** | Log room create/delete and errors at INFO. Expose `GET /healthz` returning `{"ok": true}` |

---

## 14. Deployment

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi "uvicorn[standard]"
COPY . .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
```

- **One container, one worker.** Do not add `--workers`. Do not run multiple replicas.
- Put a TLS-terminating reverse proxy in front. The proxy **must** allow WebSocket upgrades on `/ws` and use generous idle timeouts (≥ 60 s); uvicorn's built-in WebSocket ping/pong keeps connections alive.
- Wake Lock and `wss://` require HTTPS. On plain-HTTP LAN use the app still works but wake lock is skipped.

---

## 15. Project Layout

```
syncroll/
├── Product.md          # this file
├── Design.md           # UI design (to be written; reference only)
├── main.py             # FastAPI app: /ws, /healthz, static mount (mount last)
├── rooms.py            # pure Room/Player logic, no sockets (optional split)
├── static/
│   ├── index.html
│   ├── app.js          # optional; inline in index.html is also fine
│   └── style.css       # optional; styling per Design.md
├── tests/
│   └── test_rooms.py   # unit tests for roll condition, admin handoff, grace
└── Dockerfile
```

Starting with everything in `main.py` is acceptable. Split out `rooms.py` as soon as logic grows, so it stays testable.

---

## 16. Acceptance Criteria

**Rooms & joining**
- [ ] Creating a room returns a 4-digit code (leading zeros preserved) and makes the creator admin.
- [ ] Joining with a valid code succeeds; invalid or unknown code shows a clear error.
- [ ] Joining mid-round adds the player as not-ready.
- [ ] Two rooms can run simultaneously without affecting each other.

**Rolling**
- [ ] With one connected player, ready never triggers a roll.
- [ ] With ≥ 2 connected players, a roll happens exactly when the last connected player goes ready.
- [ ] After a roll, everyone's ready resets to false, `roll_no` increments, and all phones show the same value and die.
- [ ] Values are always within `1..sides`; a D2 produces only 1 or 2.
- [ ] Two near-simultaneous final-ready events produce exactly one roll.
- [ ] Un-readying before the roll prevents it.

**Admin & die**
- [ ] Only the admin can change the die; others get `not_admin`.
- [ ] `sides` outside `2..1000` or non-integer is rejected with `invalid_sides`.
- [ ] Changing the die resets all ready states.
- [ ] The result still shows the die it was actually rolled on after the admin changes the die.
- [ ] Admin leaving hands admin to the lowest-seat connected player.

**Disconnects**
- [ ] A disconnected player does not block a roll.
- [ ] Rejoining within 60 s restores seat, ready state, and admin role.
- [ ] After 60 s the seat is removed; an old token yields `bad_token`.
- [ ] Reloading the page mid-game rejoins automatically.
- [ ] When the last player is gone, the room is deleted and its code becomes reusable.

**Client**
- [ ] Reconnecting does not replay an old roll's animation.
- [ ] Works in mobile Safari and Chrome, portrait orientation.
- [ ] Wake Lock keeps the screen on when supported; no error if unsupported.
- [ ] Ready state is understandable without relying on color alone.

**Ops**
- [ ] `docker build` + `docker run -p 8000:8000` serves the full app from one container.
- [ ] `GET /healthz` returns 200.

---

## 17. Open Questions and Future Ideas

**Open (decide before or during v1):**
2. Should the admin be able to **kick** a player? (Not in v1.)
3. Should players be able to set a **nickname**? (Not in v1; players are identified by seat number.)

**Future (explicitly out of scope for v1):**
- Roll history / statistics for the room (useful for Catan number distribution).
- Multiple dice (`NdX`) and sum display.
- Sound and haptic feedback options.
- Optional per-room password.
- Admin-controlled "reset round".
- Persistence across server restarts.

---

## 18. Working Agreement for Claude

When implementing from this document:

1. **Follow this spec, do not invent features.** If something is unspecified and matters, pick the simplest option consistent with the goals and note it, rather than adding scope.
2. **Never move the roll to the client.** Never trust client-supplied results.
3. **Keep it one container, one process, no build step.** Do not introduce a database, Redis, npm, a bundler, or a frontend framework.
4. **Do not write or assume UI visuals.** Read `Design.md` for anything visual once it exists. Until then, use minimal, functional, unstyled-but-usable markup that respects the behavior in §8 and §12.
5. **Test the room logic** (`rooms.py`) with unit tests covering: roll condition, disconnect exclusion, grace expiry, admin handoff, die change reset, and double-final-ready.
6. **Keep messages exactly as defined in §11.** If the protocol needs to change, update this file first.