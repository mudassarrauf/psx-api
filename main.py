import asyncio
import os
import secrets
import asyncpg
from datetime import date as date_type
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Security, Query
from fastapi.security.api_key import APIKeyHeader

# --- NEW IMPORTS FOR ADMIN & ORM ---
from sqladmin import Admin, ModelView
from sqlalchemy import Column, Integer, String, Boolean, DateTime, create_engine, select, func
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

# ==========================================
# 1. CONFIGURATION
# ==========================================
MARKET_DB_DSN = os.getenv("MARKET_DB_DSN")  # Raw connection string for market-db
AUTH_DB_DSN = os.getenv("AUTH_DB_DSN")  # Connection string for auth-db

# Convert asyncpg DSN to SQLAlchemy Async DSN (add +asyncpg)
AUTH_DB_URL = AUTH_DB_DSN.replace("postgresql://", "postgresql+asyncpg://")

API_KEY_NAME = "X-API-KEY"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

# ==========================================
# 2. AUTH DATABASE SETUP (SQLAlchemy)
# ==========================================
Base = declarative_base()
engine = create_async_engine(AUTH_DB_URL, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# -- The User Model --
class APIClient(Base):
    __tablename__ = "api_clients"

    id = Column(Integer, primary_key=True)
    client_name = Column(String, nullable=False)
    # unique=True ensures the DB rejects duplicates,
    # though secrets.token_urlsafe makes collisions mathematically impossible.
    api_key = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())


# ==========================================
# 3. ADMIN PANEL CONFIGURATION
# ==========================================
class APIClientAdmin(ModelView, model=APIClient):
    name = "User Subscription"
    name_plural = "User Subscriptions"
    icon = "fa-solid fa-user-shield"

    # What columns to show in the list
    column_list = [APIClient.id, APIClient.client_name, APIClient.api_key, APIClient.is_active, APIClient.created_at]

    # Allow searching/filtering
    column_searchable_list = [APIClient.client_name, APIClient.api_key]
    column_filters = [APIClient.is_active]

    # HIDE the API key field in Create form (we auto-generate it)
    form_excluded_columns = [APIClient.created_at]

    # -- AUTO-GENERATE KEY ON SAVE --
    async def on_model_change(self, data, model, is_created, request):
        if is_created and not model.api_key:
            # Generate a secure, URL-safe key (43 chars)
            # This is cryptographically secure
            model.api_key = f"sk_live_{secrets.token_urlsafe(32)}"


# ==========================================
# 4. FASTAPI APP & LIFECYCLE
# ==========================================
# We need to create tables on startup
async def init_auth_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Create Auth Tables if they don't exist
    await init_auth_db()

    # 2. Start Market Listener (Background)
    task = asyncio.create_task(listen_to_postgres())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)

# -- MOUNT ADMIN PANEL --
admin = Admin(app, engine)
admin.add_view(APIClientAdmin)


# ==========================================
# 5. CORE LOGIC (Market & Auth)
# ==========================================

# ... [WebSocket Manager Code - SAME AS BEFORE] ...
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
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message)
            except:
                if connection in self.active_connections:
                    self.active_connections.remove(connection)


manager = ConnectionManager()


# ... [DB Listener Code - SAME AS BEFORE] ...
async def listen_to_postgres():
    try:
        # NOTE: We still use raw asyncpg for the Market DB listener
        conn = await asyncpg.connect(MARKET_DB_DSN)
        await conn.add_listener("stock_updates", lambda c, p, ch, pay: asyncio.create_task(manager.broadcast(pay)))
        while True:
            await asyncio.sleep(60)
    except Exception as e:
        print(f"❌ Listener Error: {e}")
        await asyncio.sleep(5)


# -- NEW AUTH CHECKER --
async def validate_api_key(api_key: str) -> bool:
    if not api_key: return False
    async with async_session() as session:
        # Query Auth DB
        stmt = select(APIClient).where(APIClient.api_key == api_key)
        result = await session.execute(stmt)
        user = result.scalars().first()
        return user.is_active if user else False


async def get_api_key(api_key_header: str = Security(api_key_header)):
    if not await validate_api_key(api_key_header):
        raise HTTPException(status_code=403, detail="Invalid or Inactive API Key")
    return api_key_header


# ==========================================
# 6. ENDPOINTS
# ==========================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, api_key: str = Query(...)):
    if not await validate_api_key(api_key):
        await websocket.close(code=1008)
        return
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except:
        manager.disconnect(websocket)


@app.get("/api/eod", dependencies=[Depends(get_api_key)])
async def get_eod_price(ticker: str, date: str):
    # Connect to MARKET DB for data
    try:
        date_obj = date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "Invalid Date")

    conn = await asyncpg.connect(MARKET_DB_DSN)
    try:
        row = await conn.fetchrow(
            "SELECT close_price, recorded_at FROM historical_prices WHERE ticker = $1 AND recorded_at = $2::date",
            ticker, date_obj
        )
        if row:
            return {"ticker": ticker, "date": str(row['recorded_at']), "price": float(row['close_price'])}
        raise HTTPException(404, "Not found")
    finally:
        await conn.close()