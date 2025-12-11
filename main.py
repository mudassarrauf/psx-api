import asyncio
import os
import json
import asyncpg
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from contextlib import asynccontextmanager
from typing import List

# --- CONFIGURATION ---
DB_DSN = f"postgresql://{os.getenv('POSTGRES_USER')}:{os.getenv('POSTGRES_PASSWORD')}@{os.getenv('POSTGRES_HOST')}/{os.getenv('POSTGRES_DB')}"

# --- WEBSOCKET MANAGER ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        # Broadcast message to all connected iOS clients
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except:
                pass

manager = ConnectionManager()

# --- DATABASE LISTENER (BACKGROUND TASK) ---
async def listen_to_postgres():
    """Listens to the 'stock_updates' channel from Postgres"""
    print("🔌 Connector: Connecting to DB Listener...")
    conn = await asyncpg.connect(DB_DSN)
    try:
        await conn.add_listener("stock_updates", lambda conn, pid, channel, payload: asyncio.create_task(manager.broadcast(payload)))
        print("✅ Connector: Listening for DB updates...")
        while True:
            await asyncio.sleep(60) # Keep connection alive
    except Exception as e:
        print(f"❌ Listener Error: {e}")
    finally:
        await conn.close()

# --- LIFECYCLE ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the DB listener in the background
    task = asyncio.create_task(listen_to_postgres())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)

# --- ROUTES ---

# 1. REAL-TIME API (WebSocket)
# URL: ws://your-domain/ws
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text() # Keep socket open
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# 2. EOD API (REST)
# URL: GET /api/eod?ticker=OGDC&date=2025-10-27
@app.get("/api/eod")
async def get_eod_price(ticker: str, date: str):
    conn = await asyncpg.connect(DB_DSN)
    try:
        # Query your existing historical_prices table
        row = await conn.fetchrow(
            "SELECT close_price, recorded_at FROM historical_prices WHERE ticker = $1 AND recorded_at = $2::date",
            ticker, date
        )
        if row:
            return {"ticker": ticker, "date": str(row['recorded_at']), "price": float(row['close_price'])}
        else:
            raise HTTPException(status_code=404, detail="Data not found for this date")
    finally:
        await conn.close()