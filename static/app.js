/* Syncroll client. Vanilla JS, no build step.
   The server is the single source of truth: this file never rolls a die. */

(() => {
  "use strict";

  const MIN_PLAYERS_TO_ROLL = 2;
  const MAX_SIDES = 1000;
  const MIN_SIDES = 2;
  const HISTORY_LIMIT = 25;
  const STORE_KEY = "syncroll.session";

  const $ = (id) => document.getElementById(id);

  const el = {
    home: $("home"), room: $("room"),
    btnCreate: $("btn-create"), joinForm: $("join-form"), joinCode: $("join-code"),
    homeError: $("home-error"),
    ready: $("ready"), readyMark: $("ready-mark"), readyLabel: $("ready-label"),
    readyCount: $("ready-count"), hint: $("hint"),
    code: $("code"), btnLeave: $("btn-leave"),
    dieLabel: $("die-label"), dieControls: $("die-controls"),
    customForm: $("custom-form"), customSides: $("custom-sides"),
    result: $("result"), resultValue: $("result-value"), resultSub: $("result-sub"),
    history: $("history"), players: $("players"),
    status: $("status"), lost: $("lost"), lostText: $("lost-text"), btnHome: $("btn-home"),
  };

  // --- session state -------------------------------------------------------

  let ws = null;
  let session = null;        // { code, token }
  let mySeat = null;
  let state = null;          // last `state` snapshot
  let seenRollNo = null;     // roll_no already rendered; null until first state
  let history = [];          // newest first, client-side only
  let retries = 0;
  let retryTimer = null;
  let intent = null;         // pending action for the next open socket
  let wakeLock = null;
  let leaving = false;

  // --- storage -------------------------------------------------------------

  function loadSession() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed.code === "string" && typeof parsed.token === "string") {
        return parsed;
      }
    } catch (_) { /* private mode or corrupt value */ }
    return null;
  }

  function saveSession(value) {
    session = value;
    try {
      if (value) localStorage.setItem(STORE_KEY, JSON.stringify(value));
      else localStorage.removeItem(STORE_KEY);
    } catch (_) { /* ignore */ }
  }

  // --- die helpers ---------------------------------------------------------

  const dieLabel = (sides) => (sides === "catan" ? "Catan 2d6" : "D" + sides);

  // --- socket --------------------------------------------------------------

  function wsUrl() {
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    return scheme + "//" + location.host + "/ws";
  }

  function connect(action) {
    if (action) intent = action;
    clearTimeout(retryTimer);
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      if (ws.readyState === WebSocket.OPEN) flushIntent();
      return;
    }
    ws = new WebSocket(wsUrl());
    ws.onopen = () => {
      retries = 0;
      setStatus(false);
      flushIntent();
    };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      onMessage(msg);
    };
    ws.onclose = () => {
      ws = null;
      if (leaving) return;
      if (session) {
        setStatus(true);
        scheduleRetry();
      }
    };
    ws.onerror = () => { /* onclose does the work */ };
  }

  function flushIntent() {
    const action = intent || (session ? { type: "rejoin", code: session.code, token: session.token } : null);
    intent = null;
    if (action) send(action);
  }

  function scheduleRetry() {
    const delay = Math.min(5000, 500 * Math.pow(2, retries));
    retries += 1;
    clearTimeout(retryTimer);
    retryTimer = setTimeout(() => connect(), delay);
  }

  function send(payload) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload));
  }

  // --- incoming ------------------------------------------------------------

  function onMessage(msg) {
    setStatus(false);
    if (msg.type === "joined") {
      mySeat = msg.seat;
      saveSession({ code: msg.code, token: msg.token });
      showRoom();
    } else if (msg.type === "state") {
      applyState(msg);
    } else if (msg.type === "error") {
      onError(msg);
    }
  }

  function onError(msg) {
    if (msg.code === "bad_token" || msg.code === "room_not_found" || msg.code === "invalid_code") {
      if (session && !state) {           // failed create/join/rejoin
        endSession(msg.message || "Session lost.");
        return;
      }
      if (!state) { showHomeError(msg.message); return; }
    }
    if (msg.code === "already_in_room") return;   // benign race after a reconnect
    if (!state) showHomeError(msg.message);
    else flashHint(msg.message);
  }

  function applyState(next) {
    const previous = seenRollNo;
    state = next;
    const roll = next.last_roll;
    // Only a roll_no that *increases* during a live session is a new roll; a
    // first snapshot after join/rejoin/reload renders statically.
    const isNew = roll && previous !== null && roll.roll_no > previous;
    if (roll) {
      if (previous === null) {
        rebuildHistory(roll);
      } else if (roll.roll_no > previous) {
        pushHistory(roll);
      }
      seenRollNo = roll.roll_no;
    } else {
      seenRollNo = 0;
      history = [];
    }
    render(isNew);
    if (isNew && navigator.vibrate) { try { navigator.vibrate(40); } catch (_) {} }
  }

  function rebuildHistory(roll) {
    history = [{ roll_no: roll.roll_no, value: roll.value, sides: roll.sides }];
  }

  function pushHistory(roll) {
    history.unshift({ roll_no: roll.roll_no, value: roll.value, sides: roll.sides });
    if (history.length > HISTORY_LIMIT) history.length = HISTORY_LIMIT;
  }

  // --- rendering -----------------------------------------------------------

  function render(isNew) {
    if (!state) return;
    const players = state.players || [];
    const connected = players.filter((p) => p.connected);
    const readyCount = connected.filter((p) => p.ready).length;
    const me = players.find((p) => p.seat === mySeat);
    const amAdmin = state.admin_seat === mySeat;
    const waiting = connected.length < MIN_PLAYERS_TO_ROLL;

    el.code.textContent = state.code;
    el.dieLabel.textContent = dieLabel(state.sides);

    // Big button. State is carried by text and mark, not colour alone.
    const iAmReady = !!(me && me.ready);
    el.ready.classList.toggle("is-ready", iAmReady);
    el.ready.setAttribute("aria-pressed", String(iAmReady));
    el.readyMark.textContent = iAmReady ? "✓" : "○";
    el.readyLabel.textContent = iAmReady ? "READY" : "NOT READY";
    el.readyCount.textContent = readyCount + " / " + connected.length + " ready";
    el.ready.disabled = waiting;
    el.hint.textContent = waiting
      ? "Waiting for players - share code " + state.code
      : "";

    // Die controls, admin only.
    el.dieControls.hidden = !amAdmin;
    for (const chip of el.dieControls.querySelectorAll("[data-sides]")) {
      chip.classList.toggle("active", String(state.sides) === chip.dataset.sides);
    }

    // Result.
    const roll = state.last_roll;
    if (roll) {
      el.resultValue.textContent = String(roll.value);
      const dice = roll.values && roll.values.length > 1 ? " (" + roll.values.join(" + ") + ")" : "";
      el.resultSub.textContent = dieLabel(roll.sides) + " → " + roll.value + dice + " · roll #" + roll.roll_no;
    } else {
      el.resultValue.textContent = "--";
      el.resultSub.textContent = "No roll yet";
    }
    el.result.classList.remove("is-new");
    if (isNew) {
      void el.result.offsetWidth;   // restart the animation
      el.result.classList.add("is-new");
    }

    renderHistory();
    renderPlayers(players, amAdmin);
  }

  function renderHistory() {
    el.history.textContent = "";
    if (!history.length) {
      const empty = document.createElement("li");
      empty.className = "history-empty";
      empty.textContent = "No rolls yet";
      el.history.appendChild(empty);
      return;
    }
    for (const item of history) {
      const li = document.createElement("li");
      const die = document.createElement("span");
      die.className = "h-die";
      die.textContent = "#" + item.roll_no + "  " + dieLabel(item.sides);
      const value = document.createElement("span");
      value.className = "h-value";
      value.textContent = String(item.value);
      li.append(die, value);
      el.history.appendChild(li);
    }
  }

  function renderPlayers(players, amAdmin) {
    el.players.textContent = "";
    for (const p of players) {
      const li = document.createElement("li");
      li.classList.toggle("ready", p.ready && p.connected);
      li.classList.toggle("offline", !p.connected);
      li.classList.toggle("me", p.seat === mySeat);
      const mark = document.createElement("span");
      mark.className = "p-mark";
      mark.textContent = !p.connected ? "⦸" : p.ready ? "✓" : "○";
      const name = document.createElement("span");
      let label = "P" + p.seat;
      if (p.seat === mySeat) label += " (you)";
      if (p.seat === (state && state.admin_seat)) label += " ★";
      if (!p.connected) label += " offline";
      name.textContent = label;
      li.append(mark, name);
      li.title = amAdmin ? label : label;
      el.players.appendChild(li);
    }
  }

  function setStatus(on) { el.status.hidden = !on; }

  function showHomeError(message) {
    el.homeError.textContent = message || "";
    if (message) setTimeout(() => { if (el.homeError.textContent === message) el.homeError.textContent = ""; }, 4000);
  }

  let hintTimer = null;
  function flashHint(message) {
    el.hint.textContent = message || "";
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => render(false), 2500);
  }

  // --- screens -------------------------------------------------------------

  function showRoom() {
    el.home.hidden = true;
    el.room.hidden = false;
    el.lost.hidden = true;
    requestWakeLock();
  }

  function showHome() {
    el.room.hidden = true;
    el.home.hidden = false;
    el.lost.hidden = true;
    el.homeError.textContent = "";
    releaseWakeLock();
  }

  function endSession(message) {
    saveSession(null);
    state = null;
    mySeat = null;
    seenRollNo = null;
    history = [];
    intent = null;
    setStatus(false);
    el.lostText.textContent = message || "Session lost.";
    el.lost.hidden = false;
    releaseWakeLock();
  }

  function resetToHome() {
    leaving = true;
    saveSession(null);
    state = null;
    mySeat = null;
    seenRollNo = null;
    history = [];
    intent = null;
    setStatus(false);
    if (ws) { try { ws.close(); } catch (_) {} ws = null; }
    setTimeout(() => { leaving = false; }, 0);
    showHome();
  }

  // --- wake lock -----------------------------------------------------------

  async function requestWakeLock() {
    if (!("wakeLock" in navigator) || wakeLock) return;
    try {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => { wakeLock = null; });
    } catch (_) { wakeLock = null; }   // unsupported or insecure context: silent
  }

  function releaseWakeLock() {
    if (wakeLock) { try { wakeLock.release(); } catch (_) {} wakeLock = null; }
  }

  // --- input ---------------------------------------------------------------

  el.btnCreate.addEventListener("click", () => {
    saveSession(null);
    connect({ type: "create" });
  });

  el.joinForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const code = el.joinCode.value.trim();
    if (!/^\d{4}$/.test(code)) { showHomeError("Enter exactly 4 digits."); return; }
    saveSession(null);
    connect({ type: "join", code: code });
  });

  el.joinCode.addEventListener("input", () => {
    el.joinCode.value = el.joinCode.value.replace(/\D/g, "").slice(0, 4);
  });

  el.ready.addEventListener("click", () => {
    if (!state || el.ready.disabled) return;
    const me = state.players.find((p) => p.seat === mySeat);
    send({ type: "ready", ready: !(me && me.ready) });
  });

  el.btnLeave.addEventListener("click", () => {
    send({ type: "leave" });
    resetToHome();
  });

  el.dieControls.addEventListener("click", (ev) => {
    const chip = ev.target.closest("[data-sides]");
    if (!chip) return;
    send({ type: "set_sides", sides: chip.dataset.sides });
  });

  el.customForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const raw = el.customSides.value.trim();
    const n = Number(raw);
    if (!/^\d+$/.test(raw) || n < MIN_SIDES || n > MAX_SIDES) {
      flashHint("Sides must be " + MIN_SIDES + "-" + MAX_SIDES + ".");
      return;
    }
    send({ type: "set_sides", sides: raw });
    el.customSides.value = "";
  });

  el.btnHome.addEventListener("click", resetToHome);

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    requestWakeLock();
    if (session && (!ws || ws.readyState === WebSocket.CLOSED)) {
      retries = 0;
      connect();
    }
  });

  window.addEventListener("online", () => { if (session) { retries = 0; connect(); } });

  // --- boot ----------------------------------------------------------------

  session = loadSession();
  if (session) {
    showRoom();
    setStatus(true);
    connect({ type: "rejoin", code: session.code, token: session.token });
  } else {
    showHome();
  }
})();
