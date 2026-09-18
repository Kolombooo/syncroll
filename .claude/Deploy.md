# Syncroll — Deployment (Portainer + Nginx Proxy Manager)

> **Scope:** getting the container running through the Portainer UI and putting
> Nginx Proxy Manager (NPM) in front of it with HTTPS and WebSocket support.
> **Not in this file:** app behavior (`Product.md`) or visuals (`Design.md`).

---

## 0. What the app needs from its environment

Read this before touching any UI — most deployment failures for Syncroll are one
of these four things.

| Requirement | Why | What breaks if you get it wrong |
|---|---|---|
| **Exactly one container, one uvicorn worker** | Rooms live in the memory of one process. There is no database, no Redis. | Players who land on different replicas cannot see each other. Room codes appear to "not exist". |
| **WebSocket upgrade allowed on `/ws`** | The whole app is one WebSocket. | The page loads, the button does nothing, the client sits in "Reconnecting…" forever. |
| **Proxy idle timeout ≥ 60 s** (use 1 hour) | A quiet room sends no traffic between rolls. | Phones drop every 60 s and silently rejoin, or a locked phone loses its seat early. |
| **HTTPS on the public hostname** | The client derives `wss://` from `location`, and `navigator.wakeLock` needs a secure context. | Mixed-content error on the socket; screens sleep mid-game. |

`GET /healthz` returns `{"ok": true}` and is the right target for health checks
and uptime monitors.

---

## 1. Get the image onto the Docker host

Portainer's **web editor** stacks have no build context, so a bare `build: .` in
the editor will fail. Pick one of these.

### Path A — Git repository (recommended)

Portainer clones the repo itself, so the `Dockerfile` and `static/` are present
at build time, and redeploying later is one click.

1. Push this project to a Git remote (GitHub/Gitea/GitLab, private is fine).
2. Portainer → **Stacks** → **Add stack** → name it `syncroll`.
3. Build method: **Repository**.
   - *Repository URL*: your remote.
   - *Reference*: `refs/heads/main`.
   - *Compose path*: `docker-compose.yml`.
   - Add credentials if the repo is private.
4. Paste the compose file from §2 into the repo at that path (commit it first).
5. **Deploy the stack**.

Later updates: **Stacks → syncroll → Pull and redeploy** (tick *Re-pull image
and redeploy*, which rebuilds from the new commit).

### Path B — Build on the host, deploy from the web editor

No Git remote needed.

1. Copy the project folder to the Docker host (e.g. `/opt/syncroll`).
2. On the host:
   ```bash
   docker build -t syncroll:1.0 /opt/syncroll
   ```
3. Portainer → **Stacks** → **Add stack** → **Web editor**, paste the compose
   from §2, and change `build: .` to `image: syncroll:1.0`.

Rebuilding means repeating step 2 with a new tag and editing the stack.

### Path C — Registry

Build and push (`ghcr.io/<you>/syncroll:1.0`), then reference that in `image:`.
Add the registry under Portainer → **Registries** if it is private. Best if the
Docker host is not the machine you develop on.

### Path D — Deploy from the console now, adopt into Portainer later

Perfectly valid: Portainer discovers whatever Docker is already running, so you
can ship it from an SSH session today and wire up the UI whenever. **Use
`docker compose`, not `docker run`** — see §1.1 for why.

```bash
mkdir -p /opt/syncroll          # copy the project here (scp / rsync / git clone)
cd /opt/syncroll
docker network ls | grep -i -E 'npm|proxy'      # find the NPM network
```

Write `/opt/syncroll/docker-compose.yml` with the content from §2 (using
`build: .`, since the source is right there), then:

```bash
docker compose up -d --build
```

Verify without publishing a port:

```bash
docker compose ps
docker compose logs -f syncroll
docker exec syncroll python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read())"
```

That last command should print `b'{"ok":true}'`. Then go straight to §4 and set
up the proxy host in NPM — NPM does not care how the container was started.

### 1.1 What Portainer can and cannot do with it afterwards

| Started with | How it appears in Portainer | What you get |
|---|---|---|
| `docker run` | **Containers** list only | Logs, console, stats, stop/restart/remove. No stack grouping, no editor. |
| `docker compose up -d` | **Stacks**, marked *external* / limited | Same, plus the stack grouping. The **Editor** tab is unavailable on most CE versions, because the compose file lives on the host disk and Portainer has no copy of it. |

So "adopting" is really two different things:

- **Just managing it** (logs, console, restart, redeploy by hand) — works
  immediately, nothing to do. This is enough for most people.
- **Making it a fully Portainer-managed stack** (editable compose in the web
  editor, Pull-and-redeploy button) — Portainer has to own the stack definition,
  which means recreating it once:

  ```bash
  cd /opt/syncroll && docker compose down
  ```

  then create the stack in Portainer per Path A or B. This is cheap for Syncroll
  specifically: there are no volumes and no database, so nothing is lost except
  the rooms that happen to be live at that moment. Do it between games.

Keep the host directory at `/opt/syncroll` and the stack name `syncroll` in both
places, so the container name and network wiring stay identical across the
switch and NPM's proxy host keeps working untouched.

---

## 2. The compose file

```yaml
services:
  syncroll:
    build: .                    # Path B/C: replace with  image: syncroll:1.0
    container_name: syncroll
    restart: unless-stopped
    environment:
      # Only needed if you ever want correct client IPs / scheme server-side.
      # uvicorn otherwise ignores X-Forwarded-* from a non-localhost peer.
      FORWARDED_ALLOW_IPS: "*"
    networks:
      - npm
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s

networks:
  npm:
    external: true
    name: <your-npm-network>    # see 2.1
```

Notes on what is deliberately absent:

- **No `ports:`.** NPM reaches the container over the shared Docker network by
  name, so port 8000 never needs to be exposed on the host. Add
  `ports: ["8000:8000"]` only if you also want direct LAN access — and know that
  this route is plain HTTP, so Wake Lock will be skipped there.
- **No `deploy.replicas`.** See §0. One container, always.
- **No volumes.** Rooms are in memory by design; there is nothing to persist.
  Restarting the stack drops every room, and clients handle that by showing
  "session lost" and returning Home.

### 2.1 Find your NPM network name

The app container and the NPM container must share a network, or NPM cannot
resolve the hostname `syncroll`.

Portainer → **Networks**, find the network your NPM container is attached to
(commonly `npm_default`, `nginx-proxy-manager_default`, or a hand-made `proxy`).
Put that exact name in `networks.npm.name` above.

To confirm afterwards: Portainer → **Containers → syncroll → Inspect**, and
check that the NPM network appears under `NetworkSettings.Networks`.

---

## 3. Deploy and verify before touching NPM

1. Deploy the stack. The container should reach **healthy** within ~15 s.
2. Portainer → **Containers → syncroll → Logs**. Expect:
   ```
   Uvicorn running on http://0.0.0.0:8000
   ```
   Room creates and rolls log here too, which is handy while testing.
3. Portainer → **Containers → syncroll → Console** (`/bin/sh`), then:
   ```bash
   python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read())"
   ```
   Expect `b'{"ok":true}'`.

Fix anything wrong here first — a broken container looks exactly like a broken
proxy config from the browser.

---

## 4. Point Nginx Proxy Manager at it

NPM UI → **Hosts → Proxy Hosts → Add Proxy Host**.

### Details tab

| Field | Value |
|---|---|
| Domain Names | `roll.example.com` (your hostname) |
| Scheme | `http` |
| Forward Hostname / IP | `syncroll` (the container name from §2) |
| Forward Port | `8000` |
| Cache Assets | off — the client is tiny and you want new versions picked up |
| Block Common Exploits | on |
| **Websockets Support** | **on — this is the one that matters** |
| Access List | Publicly Accessible |

### SSL tab

| Field | Value |
|---|---|
| SSL Certificate | Request a new SSL Certificate (Let's Encrypt) |
| Force SSL | on |
| HTTP/2 Support | on |
| HSTS | optional |
| Agree to ToS / email | as required |

Let's Encrypt needs the hostname's DNS pointing at the NPM host and ports 80/443
reachable from the internet before you save.

### Advanced tab

Paste this. Without it the socket is cut at NPM's default 60 s read timeout,
which shows up as phones reconnecting once a minute in a quiet room.

```nginx
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
proxy_buffering off;
```

`Websockets Support` already adds the `Upgrade` / `Connection` headers, so do not
add those by hand here.

Save.

---

## 5. Verify the deployment

1. Open `https://roll.example.com` on a phone. You should see the Syncroll home
   screen over a valid certificate.
2. **Create room** on one phone, **Join** with the 4-digit code on a second.
   Both must appear in the players bar at the bottom.
3. Tap the big button on both. The die must roll and **both phones must show the
   same value at the same moment**. This is the real WebSocket test — if the page
   loads but nothing happens here, Websockets Support is off (§4).
4. Lock one phone for ~10 s and unlock it. It should rejoin its own seat with the
   last result still shown, and with no re-animation.
5. Leave a room open and idle for 3–4 minutes. No "Reconnecting…" badge should
   appear. If it flickers every ~60 s, the Advanced block in §4 is missing.
6. `https://roll.example.com/healthz` → `{"ok": true}`.

---

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Page loads, button does nothing, "Reconnecting…" never clears | WebSocket upgrade not passed through | Turn on **Websockets Support** on the proxy host |
| NPM shows 502 Bad Gateway | NPM cannot resolve `syncroll` | The two containers are not on the same network — fix `networks.npm.name` (§2.1) |
| Works, but every phone reconnects about once a minute | Proxy idle timeout | Add the Advanced block (§4) |
| Console error about an insecure WebSocket | Page served over HTTP | Turn on **Force SSL**; the client picks `wss://` only on an `https://` page |
| Screens sleep during a game | Wake Lock needs a secure context | Use the HTTPS hostname, not the LAN IP |
| A room code "does not exist" for some players but works for others | More than one container or worker | One replica, no `--workers` (§0) |
| All rooms vanished | Container restarted | Expected. State is in memory by design; players tap "Back to home" and make a new room |
| Codes still work after a redeploy | Browser kept the stored `{code, token}` | Harmless — the server answers `room_not_found` and the client clears it |

---

## 7. Updating

- **Path A:** push the commit → Portainer → Stacks → `syncroll` → **Pull and
  redeploy** (tick re-pull/rebuild).
- **Path B:** rebuild on the host with a new tag → edit the stack's `image:` →
  **Update the stack**.

Either way the container restarts, so **every live room is destroyed**. Deploy
between games, not during one.
