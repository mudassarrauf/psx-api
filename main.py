import asyncio
import os
import json
import asyncpg
from datetime import date as date_type  # <--- CRITICAL IMPORT
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from contextlib import asynccontextmanager
from typing import List

# --- CONFIGURATION ---
# Ensure these match your docker-compose environment variables
DB_DSN = f"postgresql://{os.getenv('POSTGRES_USER')}:{os.getenv('POSTGRES_PASSWORD')}@{os.getenv('POSTGRES_HOST')}/{os.getenv('POSTGRES_DB')}"


# --- WEBSOCKET MANAGER ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        # Broadcast message to all connected iOS clients
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message)
            except:
                # Remove dead connections
                if connection in self.active_connections:
                    self.active_connections.remove(connection)


manager = ConnectionManager()


# --- DATABASE LISTENER (BACKGROUND TASK) ---
async def listen_to_postgres():
    """Listens to the 'stock_updates' channel from Postgres"""
    print("🔌 Connector: Connecting to DB Listener...")
    try:
        conn = await asyncpg.connect(DB_DSN)
        await conn.add_listener("stock_updates",
                                lambda conn, pid, channel, payload: asyncio.create_task(manager.broadcast(payload)))
        print("✅ Connector: Listening for DB updates...")
        while True:
            await asyncio.sleep(60)  # Keep connection alive
    except Exception as e:
        print(f"❌ Listener Error: {e}")
        # Retry logic could be added here
        await asyncio.sleep(5)


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
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # Keep socket open
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


# 2. EOD API (REST)
@app.get("/api/eod")
async def get_eod_price(ticker: str, date: str):
    # --- STEP 1: CONVERT STRING TO DATE OBJECT ---
    try:
        # Converts "2025-12-10" (str) -> 2025-12-10 (date object)
        date_obj = date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    # --- STEP 2: CONNECT AND QUERY ---
    try:
        conn = await asyncpg.connect(DB_DSN)
        try:
            # We pass 'date_obj', NOT 'date' (the string)
            row = await conn.fetchrow(
                """
                SELECT close_price, recorded_at 
                FROM historical_prices 
                WHERE ticker = $1 AND recorded_at = $2::date
                """,
                ticker, date_obj
            )

            if row:
                return {
                    "ticker": ticker,
                    "date": str(row['recorded_at']),
                    "price": float(row['close_price'])
                }
            else:
                raise HTTPException(status_code=404, detail=f"No data found for {ticker} on {date}")
        finally:
            await conn.close()
    except Exception as e:
        print(f"❌ API Error: {e}")
        raise HTTPException(status_code=500, detail="Database connection failed")