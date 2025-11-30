import asyncio, json, random, math, time, logging
import websockets

# --- CONFIG ---
HOST = "0.0.0.0"
PORT = 8765
TICK_RATE = 20                 # 20Hz server tick
LATENCY = 0.2                  # 200 ms simulated latency
MAP_W, MAP_H = 800, 500
PLAYER_SPEED = 180
PLAYER_R = 15
COIN_R = 10

# Game round timing
GAME_DURATION = 180            # 3 minutes
INTERMISSION = 10              # seconds between rounds

# --- STATE ---
players = {}                   # pid -> {ws,x,y,vx,vy,score,color}
next_pid = 1                   # strictly increment; do not recycle
coin = None

# Game cycle
game_active = False
game_end_time = 0.0
intermission_end = 0.0
last_winner = None

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

# --- COLOR MAPPING (stable per PID) ---
def get_color_for_pid(pid: int) -> str:
    palette = ["#2b85f0", "#ff4d4f", "#2bbf57", "#f07f2b", "#a02bf0", "#f02b85"]
    return palette[(pid - 1) % len(palette)]

# --- HELPERS ---
def clamp(v, a, b): return max(a, min(b, v))

async def delayed_send(ws, msg):
    await asyncio.sleep(LATENCY)
    try:
        await ws.send(msg)
    except Exception as e:
        logging.warning(f"Failed to send: {e}")

async def broadcast():
    if not players:
        return
    # time left
    now = time.time()
    if game_active:
        time_left = max(0, int(game_end_time - now))
    else:
        # during intermission show intermission countdown if set
        time_left = max(0, int(intermission_end - now)) if intermission_end > now else 0

    state = {
        "type": "state",
        "players": {
                str(pid): {
                    "x": p["x"],
                    "y": p["y"],
                    "score": p["score"],
                    "color": p["color"]
                }
                for pid, p in players.items()
        },
        "coin": coin,
        "time_left": time_left,
        "game_active": game_active,
        "game_duration": GAME_DURATION,
        "last_winner": last_winner
    }
    js = json.dumps(state)
    await asyncio.gather(*(delayed_send(p["ws"], js) for p in players.values()))

async def spawn_coin():
    global coin
    while True:
        if game_active and coin is None:
            coin = {
                "x": random.uniform(20, MAP_W - 20),
                "y": random.uniform(20, MAP_H - 20)
            }
            logging.info(f"Coin spawned at {coin}")
        await asyncio.sleep(1)

async def apply_input(pid, data):
    await asyncio.sleep(LATENCY)  # simulate receive latency
    if pid not in players:
        return

    up = data.get("up", False)
    down = data.get("down", False)
    left = data.get("left", False)
    right = data.get("right", False)

    vx = (-1 if left else 0) + (1 if right else 0)
    vy = (-1 if up else 0) + (1 if down else 0)

    if vx != 0 or vy != 0:
        d = math.hypot(vx, vy)
        vx /= d
        vy /= d

    players[pid]["vx"] = vx
    players[pid]["vy"] = vy

async def handle(ws):
    global next_pid, players, game_active, game_end_time

    # assign a new PID (no recycling)
    pid = next_pid
    next_pid += 1

    color = get_color_for_pid(pid)
    players[pid] = {
        "ws": ws,
        "x": random.uniform(50, MAP_W - 50),
        "y": random.uniform(50, MAP_H - 50),
        "vx": 0, "vy": 0,
        "score": 0,
        "color": color
    }

    logging.info(f"Player {pid} connected with color {color}")

    # welcome message includes game duration
    await delayed_send(ws, json.dumps({
        "type": "welcome",
        "id": pid,
        "map_w": MAP_W,
        "map_h": MAP_H,
        "color": color,
        "game_duration": GAME_DURATION
    }))

    # auto-start game if not active and at least one player (you can change to require 2)
    if not game_active and len(players) >= 1:
        await start_round()

    try:
        async for msg in ws:
            try:
                data = json.loads(msg)
                if data.get("type") == "input":
                    asyncio.create_task(apply_input(pid, data["input"]))
            except json.JSONDecodeError:
                logging.warning("Invalid JSON")
    except websockets.ConnectionClosed:
        logging.info(f"Player {pid} disconnected")
    finally:
        players.pop(pid, None)
        try:
            await ws.close()
        except Exception:
            pass

async def start_round():
    global game_active, game_end_time, intermission_end, last_winner, coin
    # reset scores and positions
    logging.info("Starting new round")
    for p in players.values():
        p["score"] = 0
        p["x"] = random.uniform(50, MAP_W - 50)
        p["y"] = random.uniform(50, MAP_H - 50)
        p["vx"] = 0
        p["vy"] = 0
    coin = None
    last_winner = None
    intermission_end = 0.0
    game_active = True
    game_end_time = time.time() + GAME_DURATION

async def end_round():
    global game_active, intermission_end, last_winner, coin
    logging.info("Ending round")
    game_active = False
    # determine winner
    if players:
        best_pid = None
        best_score = -1
        for pid, p in players.items():
            if p["score"] > best_score:
                best_score = p["score"]
                best_pid = pid
        last_winner = best_pid
        logging.info(f"Round winner: Player {last_winner} ({best_score})")
    else:
        last_winner = None
    coin = None
    intermission_end = time.time() + INTERMISSION

async def game_loop():
    global coin, game_active, game_end_time
    last = time.time()
    while True:
        now = time.time()
        dt = now - last
        last = now

        # integrate movement (authoritative) only during active round
        if game_active:
            for p in players.values():
                p["x"] += p["vx"] * PLAYER_SPEED * dt
                p["y"] += p["vy"] * PLAYER_SPEED * dt
                p["x"] = clamp(p["x"], PLAYER_R, MAP_W - PLAYER_R)
                p["y"] = clamp(p["y"], PLAYER_R, MAP_H - PLAYER_R)

            # coin collision
            if coin:
                for pid, p in list(players.items()):
                    if math.hypot(p["x"] - coin["x"], p["y"] - coin["y"]) <= PLAYER_R + COIN_R:
                        p["score"] += 1
                        logging.info(f"Player {pid} collected coin (score now {p['score']})")
                        coin = None
                        break

            # check round end
            if time.time() >= game_end_time:
                await end_round()

        else:
            # during intermission, if intermission ended and we have players, start next round
            if intermission_end and time.time() >= intermission_end and players:
                await start_round()

        await broadcast()

        # fixed tick
        await asyncio.sleep(max(0, 1 / TICK_RATE - (time.time() - now)))

async def main():
    logging.info("Starting server...")
    async with websockets.serve(handle, HOST, PORT):
        logging.info(f"Listening on ws://{HOST}:{PORT}")
        await asyncio.gather(spawn_coin(), game_loop())

if __name__ == "__main__":
    asyncio.run(main())
