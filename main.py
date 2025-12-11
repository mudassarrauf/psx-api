import asyncio
import os
import secrets
import asyncpg
from contextlib import asynccontextmanager
from datetime import date as date_type
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Security, Query
from fastapi.security.api_key import APIKeyHeader
from starlette.middleware.sessions import SessionMiddleware  # <--- NEW: For Login Session
from starlette.requests import Request
from starlette.responses import RedirectResponse

# --- IMPORTS FOR ADMIN & ORM ---
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend  # <--- NEW: For Auth
from sqlalchemy import Column, Integer, String, Boolean, DateTime, select, func
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

# ==========================================
# 1. CONFIGURATION
# ==========================================
MARKET_DB_DSN = os.getenv("MARKET_DB_DSN")
AUTH_DB_DSN = os.getenv("AUTH_DB_DSN")

# Admin Credentials (CHANGE THESE IN PRODUCTION ENV VARS IF YOU WANT)
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "wykwYd-gehqyg-7xebva")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_urlsafe(32))

# Convert asyncpg DSN to SQLAlchemy Async DSN
AUTH_DB_URL = AUTH_DB_DSN.replace("postgresql://", "postgresql+asyncpg://")

API_KEY_NAME = "X-API-KEY"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

# ==========================================
# 2. AUTH DATABASE SETUP (SQLAlchemy)
# ==========================================
Base = declarative_base()
engine = create_async_engine(AUTH_DB_URL, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class APIClient(Base):
    __tablename__ = "api_clients"
    id = Column(Integer, primary_key=True)
    client_name = Column(String, nullable=False)
    api_key = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())


# ==========================================
# 3. ADMIN PANEL SECURITY & CONFIG
# ==========================================

# -- A. Authentication Backend --
class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        username = form.get("username")
        password = form.get("password")

        # Validate username/password
        if username == ADMIN_USER and password == ADMIN_PASS:
            request.session.update({"token": "logged_in"})
            return True
        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        return "token" in request.session


authentication_backend = AdminAuth(secret_key=SECRET_KEY)


# -- B. The View --
class APIClientAdmin(ModelView, model=APIClient):
    name = "User Subscription"
    name_plural = "User Subscriptions"
    icon = "fa-solid fa-user-shield"

    column_list = [APIClient.id, APIClient.client_name, APIClient.api_key, APIClient.is_active, APIClient.created_at]
    column_searchable_list = [APIClient.client_name, APIClient.api_key]

    # --- CRITICAL FIX: COMMENTED OUT FILTERS TO PREVENT CRASH ---
    # column_filters = [APIClient.is_active]

    form_excluded_columns = [APIClient.created_at]

    async def on_model_change(self, data, model, is_created, request):
        if is_created and not model.api_key:
            model.api_key = f"sk_live_{secrets.token_urlsafe(32)}"


# ==========================================
# 4. FASTAPI APP & LIFECYCLE
# ==========================================
async def init_auth_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_auth_db()
    task = asyncio.create_task(listen_to_postgres())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)

# -- Add Session Middleware (Required for Login) --
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

# -- Mount Admin with Security --
admin = Admin(app, engine, authentication_backend=authentication_backend)
admin.add_view(APIClientAdmin)


# ==========================================
# 5. CORE LOGIC (Market & Auth)
# ==========================================
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

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


async def listen_to_postgres():
    try:
        conn = await asyncpg.connect(MARKET_DB_DSN)
        await conn.add_listener("stock_updates", lambda c, p, ch, pay: asyncio.create_task(manager.broadcast(pay)))
        while True:
            await asyncio.sleep(60)
    except Exception as e:
        print(f"❌ Listener Error: {e}")
        await asyncio.sleep(5)


async def validate_api_key(api_key: str) -> bool:
    if not api_key: return False
    async with async_session() as session:
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