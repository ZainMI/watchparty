// src/index.ts
export interface Env {
  ROOM: DurableObjectNamespace;
}

type JoinMsg = {
  type: "join";
  userId: string;
  name: string;
  mediaKey?: string;
};
type ControlMsg =
  | { type: "control"; action: "PLAY" | "PAUSE"; baseVersion?: number }
  | {
      type: "control";
      action: "SEEK";
      positionMs: number;
      baseVersion?: number;
    };
type ChatMsg = { type: "chat"; text: string };
type PingMsg = { type: "ping"; t: number };

type ClientMsg = JoinMsg | ControlMsg | ChatMsg | PingMsg;

type State = {
  mediaKey: string; // optional "file identity" (hash prefix)
  isPlaying: boolean;
  positionMs: number; // position at updatedAt
  updatedAt: number; // server time ms
  updatedBy: string; // userId
  version: number;
};

type User = { userId: string; name: string };

function json(ws: WebSocket, obj: any) {
  ws.send(JSON.stringify(obj));
}

function nowMs() {
  return Date.now();
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    // WebSocket endpoint: /room/<roomId>
    const m = url.pathname.match(/^\/room\/([a-zA-Z0-9_-]{1,64})$/);
    if (!m) return new Response("Not found", { status: 404 });

    if (request.headers.get("Upgrade") !== "websocket") {
      return new Response("Expected websocket", { status: 426 });
    }

    const roomId = m[1];
    const id = env.ROOM.idFromName(roomId);
    const stub = env.ROOM.get(id);
    return stub.fetch(request);
  },
};

export class Room implements DurableObject {
  private state: DurableObjectState;

  private sockets = new Map<WebSocket, User>();
  private roomState: State;

  // rate limiting
  private lastControlByUser = new Map<string, number>();
  private controlTimestamps: number[] = []; // room-wide window

  constructor(state: DurableObjectState) {
    this.state = state;
    this.roomState = {
      mediaKey: "",
      isPlaying: false,
      positionMs: 0,
      updatedAt: nowMs(),
      updatedBy: "system",
      version: 1,
    };

    // restore persisted state if present
    this.state.blockConcurrencyWhile(async () => {
      const saved = await this.state.storage.get<State>("roomState");
      if (saved) this.roomState = saved;
    });
  }

  async fetch(request: Request): Promise<Response> {
    const pair = new WebSocketPair();
    const client = pair[0];
    const server = pair[1];

    server.accept();

    // Send initial hello quickly; client will JOIN next.
    json(server, { type: "welcome", serverTimeMs: nowMs() });
    json(server, { type: "state", state: this.computeStateAt(nowMs()) });
    json(server, { type: "presence", users: this.listUsers() });

    server.addEventListener("message", (evt) => {
      try {
        const msg = JSON.parse(String(evt.data)) as ClientMsg;
        this.onMessage(server, msg);
      } catch {
        json(server, {
          type: "error",
          code: "BAD_JSON",
          message: "Invalid JSON",
        });
      }
    });

    server.addEventListener("close", () => {
      this.sockets.delete(server);
      this.broadcastPresence();
    });
    server.addEventListener("error", () => {
      this.sockets.delete(server);
      this.broadcastPresence();
    });

    return new Response(null, { status: 101, webSocket: client });
  }

  private listUsers(): User[] {
    const seen = new Map<string, User>();
    for (const [, u] of this.sockets) seen.set(u.userId, u);
    return [...seen.values()];
  }

  private broadcast(obj: any) {
    for (const ws of this.sockets.keys()) {
      try {
        json(ws, obj);
      } catch {}
    }
  }

  private broadcastPresence() {
    this.broadcast({ type: "presence", users: this.listUsers() });
  }

  // Convert (position at updatedAt) into a "position right now" for reporting.
  private computeStateAt(t: number): State {
    const s = this.roomState;
    if (!s.isPlaying) return s;
    const delta = Math.max(0, t - s.updatedAt);
    return { ...s, positionMs: s.positionMs + delta };
  }

  private roomRateLimitOk(): boolean {
    // Allow max 10 control events in last 5 seconds (room-wide)
    const t = nowMs();
    this.controlTimestamps = this.controlTimestamps.filter((x) => t - x < 5000);
    if (this.controlTimestamps.length >= 10) return false;
    this.controlTimestamps.push(t);
    return true;
  }

  private userRateLimitOk(userId: string): boolean {
    // Allow 1 control per 900ms per user
    const t = nowMs();
    const last = this.lastControlByUser.get(userId) ?? 0;
    if (t - last < 900) return false;
    this.lastControlByUser.set(userId, t);
    return true;
  }

  private async persistState() {
    // Persist occasionally (cheap), so reconnects keep last known state.
    await this.state.storage.put("roomState", this.roomState);
  }

  private onMessage(ws: WebSocket, msg: ClientMsg) {
    if (msg.type === "join") {
      const user: User = { userId: msg.userId, name: msg.name };
      this.sockets.set(ws, user);

      // Set/verify mediaKey (optional “same file” indicator)
      if (msg.mediaKey && !this.roomState.mediaKey) {
        this.roomState.mediaKey = msg.mediaKey;
        this.roomState.updatedAt = nowMs();
        this.roomState.updatedBy = msg.userId;
        this.roomState.version += 1;
        void this.persistState();
        this.broadcast({ type: "state", state: this.computeStateAt(nowMs()) });
      }

      json(ws, { type: "state", state: this.computeStateAt(nowMs()) });
      this.broadcastPresence();
      return;
    }

    const user = this.sockets.get(ws);
    if (!user) {
      json(ws, {
        type: "error",
        code: "NOT_JOINED",
        message: "Send join first",
      });
      return;
    }

    if (msg.type === "ping") {
      json(ws, { type: "pong", t: msg.t, serverTimeMs: nowMs() });
      return;
    }

    if (msg.type === "chat") {
      const text = (msg.text ?? "").slice(0, 500);
      this.broadcast({ type: "chat", from: user, text, at: nowMs() });
      return;
    }

    if (msg.type === "control") {
      if (!this.roomRateLimitOk() || !this.userRateLimitOk(user.userId)) {
        json(ws, {
          type: "error",
          code: "RATE_LIMIT",
          message: "Too many controls",
        });
        return;
      }

      const t = nowMs();
      // canonicalize current position “now” before applying changes
      const current = this.computeStateAt(t);

      if (msg.action === "PLAY") {
        this.roomState = {
          ...current,
          isPlaying: true,
          updatedAt: t,
          updatedBy: user.userId,
          version: current.version + 1,
        };
      } else if (msg.action === "PAUSE") {
        this.roomState = {
          ...current,
          isPlaying: false,
          updatedAt: t,
          updatedBy: user.userId,
          version: current.version + 1,
        };
      } else if (msg.action === "SEEK") {
        const pos = Math.max(0, Math.floor(msg.positionMs ?? 0));
        this.roomState = {
          ...current,
          positionMs: pos,
          updatedAt: t,
          updatedBy: user.userId,
          version: current.version + 1,
        };
      }

      void this.persistState();
      this.broadcast({ type: "state", state: this.computeStateAt(nowMs()) });
      return;
    }

    json(ws, {
      type: "error",
      code: "UNKNOWN",
      message: "Unknown message type",
    });
  }
}
