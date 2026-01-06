import argparse
import asyncio
import json
import os
import platform
import random
import socket
import string
import subprocess
import sys
import time
from pathlib import Path

import websockets

# ----------------------------
# mpv JSON IPC helpers
# ----------------------------


class MPV:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.proc = None
        self.ipc_path = self._make_ipc_path()

    def _make_ipc_path(self) -> str:
        if platform.system().lower().startswith("win"):
            # Named pipe
            suffix = "".join(
                random.choice(string.ascii_lowercase) for _ in range(8)
            )
            return rf"\\.\pipe\watchparty-mpv-{suffix}"
        else:
            # Unix socket
            suffix = "".join(
                random.choice(string.ascii_lowercase) for _ in range(8)
            )
            return f"/tmp/watchparty-mpv-{suffix}.sock"

    def start(self):
        # Start mpv with an IPC endpoint
        # --force-window=yes keeps a window even if paused
        args = [
            "mpv",
            self.file_path,
            "--force-window=yes",
            f"--input-ipc-server={self.ipc_path}",
            "--idle=yes",
        ]
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    async def _connect_ipc(self, timeout_s=5.0):
        # Connect to mpv IPC socket/pipe
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                if platform.system().lower().startswith("win"):
                    return await asyncio.open_connection(self.ipc_path)
                else:
                    return await asyncio.open_unix_connection(self.ipc_path)
            except Exception:
                await asyncio.sleep(0.1)
        raise RuntimeError("Could not connect to mpv IPC")

    async def command(self, cmd):
        reader, writer = await self._connect_ipc()
        payload = json.dumps({"command": cmd}).encode("utf-8") + b"\n"
        writer.write(payload)
        await writer.drain()
        # mpv replies with JSON line; read one line
        line = await reader.readline()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        if not line:
            return None
        try:
            return json.loads(line.decode("utf-8", errors="ignore"))
        except Exception:
            return None

    async def get_property(self, prop: str):
        resp = await self.command(["get_property", prop])
        if resp and resp.get("error") == "success":
            return resp.get("data")
        return None

    async def set_property(self, prop: str, value):
        await self.command(["set_property", prop, value])

    async def seek_to(self, seconds: float):
        # absolute seek
        await self.command(["set_property", "time-pos", float(seconds)])

    async def play(self):
        await self.set_property("pause", False)

    async def pause(self):
        await self.set_property("pause", True)


# ----------------------------
# Watch party protocol helpers
# ----------------------------


def rand_id(n=10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def ms() -> int:
    return int(time.time() * 1000)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ----------------------------
# Client logic
# ----------------------------


async def watchparty_client(ws_url: str, room: str, name: str, file_path: str):
    user_id = rand_id()
    mpv = MPV(file_path)
    mpv.start()

    # Local time offset to server, estimated from PONG
    server_offset_ms = 0
    last_state_version = 0

    async def send(ws, obj):
        await ws.send(json.dumps(obj))

    async def ping_loop(ws):
        nonlocal server_offset_ms
        while True:
            t0 = ms()
            await send(ws, {"type": "ping", "t": t0})
            await asyncio.sleep(25)

    async def apply_state(state: dict):
        """
        state.positionMs is position at state.updatedAt (server time).
        We compute target position "now" and correct mpv.
        """
        nonlocal server_offset_ms, last_state_version

        version = int(state.get("version", 0))
        if version <= last_state_version:
            return
        last_state_version = version

        is_playing = bool(state.get("isPlaying", False))
        position_ms = int(state.get("positionMs", 0))
        updated_at = int(state.get("updatedAt", 0))

        # estimate current server time
        server_now = ms() + server_offset_ms
        target_ms = (
            position_ms + (server_now - updated_at)
            if is_playing
            else position_ms
        )
        target_s = max(0.0, target_ms / 1000.0)

        # current mpv time
        cur_s = await mpv.get_property("time-pos")
        if cur_s is None:
            return

        drift_s = target_s - float(cur_s)

        # Correct playback state
        paused = await mpv.get_property("pause")
        if paused is None:
            paused = False
        paused = bool(paused)

        if is_playing and paused:
            await mpv.play()
        if (not is_playing) and (not paused):
            await mpv.pause()

        # Correct time
        adrift = abs(drift_s)
        if adrift > 2.0:
            await mpv.seek_to(target_s)
        elif adrift > 0.6:
            await mpv.seek_to(target_s)
        else:
            # small drift: ignore (avoid jitter)
            pass

        # Print who changed it (nice UX)
        updated_by = state.get("updatedBy", "")
        print(
            f"[STATE v{version}] playing={is_playing} target={target_s:.2f}s (drift {drift_s:+.2f}s) by={updated_by}"
        )

    async def stdin_loop(ws):
        """
        Commands:
          play
          pause
          seek <seconds>
          fwd <seconds>
          back <seconds>
          chat <message...>
          time   (prints local mpv time)
          quit
        """
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                await asyncio.sleep(0.1)
                continue
            line = line.strip()
            if not line:
                continue

            parts = line.split()
            cmd = parts[0].lower()

            if cmd == "quit" or cmd == "exit":
                print("Bye.")
                return

            if cmd == "time":
                cur = await mpv.get_property("time-pos")
                print(f"mpv time: {cur}")
                continue

            if cmd == "play":
                await send(ws, {"type": "control", "action": "PLAY"})
                continue

            if cmd == "pause":
                await send(ws, {"type": "control", "action": "PAUSE"})
                continue

            if cmd == "seek" and len(parts) >= 2:
                sec = float(parts[1])
                await send(
                    ws,
                    {
                        "type": "control",
                        "action": "SEEK",
                        "positionMs": int(sec * 1000),
                    },
                )
                continue

            if cmd in ("fwd", "forward") and len(parts) >= 2:
                delta = float(parts[1])
                cur = await mpv.get_property("time-pos") or 0.0
                await send(
                    ws,
                    {
                        "type": "control",
                        "action": "SEEK",
                        "positionMs": int((cur + delta) * 1000),
                    },
                )
                continue

            if cmd in ("back", "rewind") and len(parts) >= 2:
                delta = float(parts[1])
                cur = await mpv.get_property("time-pos") or 0.0
                await send(
                    ws,
                    {
                        "type": "control",
                        "action": "SEEK",
                        "positionMs": int(max(0.0, cur - delta) * 1000),
                    },
                )
                continue

            if cmd == "chat":
                msg = line[len("chat") :].strip()
                if msg:
                    await send(ws, {"type": "chat", "text": msg})
                continue

            print(
                "Unknown command. Try: play | pause | seek 120 | fwd 10 | back 10 | chat hello | time | quit"
            )

    full_ws_url = f"{ws_url.rstrip('/')}/room/{room}"
    print(f"Connecting: {full_ws_url}")
    async with websockets.connect(full_ws_url, ping_interval=None) as ws:
        await send(ws, {"type": "join", "userId": user_id, "name": name})

        # Start background tasks
        ping_task = asyncio.create_task(ping_loop(ws))
        stdin_task = asyncio.create_task(stdin_loop(ws))

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                mtype = msg.get("type")
                if mtype == "welcome":
                    # optional: could use this for logging
                    continue

                if mtype == "pong":
                    # RTT + offset estimation
                    t0 = int(msg.get("t", 0))
                    server_time = int(msg.get("serverTimeMs", 0))
                    t1 = ms()
                    rtt = max(1, t1 - t0)
                    # offset ≈ server_time - (t0 + rtt/2)
                    server_offset_ms = int(server_time - (t0 + rtt / 2))
                    continue

                if mtype == "presence":
                    users = msg.get("users", [])
                    names = [u.get("name", "?") for u in users]
                    print(f"[PRESENCE] {len(users)} users: {', '.join(names)}")
                    continue

                if mtype == "chat":
                    frm = msg.get("from", {})
                    print(f"[CHAT] {frm.get('name','?')}: {msg.get('text','')}")
                    continue

                if mtype == "state":
                    state = msg.get("state", {})
                    await apply_state(state)
                    continue

                if mtype == "error":
                    print(f"[ERROR] {msg.get('code')}: {msg.get('message')}")
                    continue
        finally:
            ping_task.cancel()
            stdin_task.cancel()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--ws-url",
        default="wss://watchparty.zainmagdon.workers.dev",
        help="Worker base URL (wss://...)",
    )
    ap.add_argument("--room", required=True, help="Room id/code (e.g. abc123)")
    ap.add_argument("--name", default="anon", help="Display name")
    ap.add_argument("--file", required=True, help="Path to local video file")
    args = ap.parse_args()

    if not Path(args.file).exists():
        print("File not found:", args.file)
        sys.exit(1)

    asyncio.run(watchparty_client(args.ws_url, args.room, args.name, args.file))


if __name__ == "__main__":
    main()
