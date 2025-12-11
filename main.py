import asyncio
import os
import secrets
import asyncpg
from contextlib import asynccontextmanager
from datetime import date as date_type
from typing import Optional, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Security, Query
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

# --- IMPORTS FOR ADMIN & ORM ---
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from sqlalchemy import Column, Integer, String, Boolean, DateTime, select, func
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

# ==========================================
# 1. CONFIGURATION
# ==========================================
# Database Connections
MARKET_DB_DSN = os.getenv("MARKET_DB_DSN")
AUTH_DB_DSN = os.getenv("AUTH_DB_DSN")

# Admin Panel Security
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "SuperSecretPass2025!")
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

# -- A. Login Logic --
class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        username = form.get("username")
        password = form.get("password")

        # Validate credentials
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


# -- B. The Admin View --
class APIClientAdmin(ModelView, model=APIClient):
    name = "User Subscription"
    name_plural = "User Subscriptions"
    icon = "fa-solid fa-user-shield"

    column_list = [APIClient.id, APIClient.client_name, APIClient.api_key, APIClient.is_active, APIClient.created_at]
    column_searchable_list = [APIClient.client_name, APIClient.api_key]

    # CRITICAL: Commented out filters to prevent crash on some systems
    # column_filters = [APIClient.is_active]

    form_excluded_columns = [APIClient.created_at]

    # FIX: Allows you to leave API Key empty in the form
    form_args = dict(api_key=dict(required=False))

    async def on_model_change(self, data, model, is_created, request):
        # Auto-generate key if left blank
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
    # 1. Initialize Auth Tables
    await init_auth_db()

    # 2. Start Market Data Listener
    task = asyncio.create_task(listen_to_postgres())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)

# -- MIDDLEWARE CONFIGURATION --

# 1. Session (Required for Admin Login)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

# 2. CORS (Allow Web/App access)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. Trusted Host (Helps Nginx Proxy Manager render UI correctly)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

# -- MOUNT ADMIN --
admin = Admin(
    app,
    engine,
    authentication_backend=authentication_backend,
    title="NexoDynamix Admin",
    # logo_url="OPTIONAL_LOGO_URL_HERE"
)
admin.add_view(APIClientAdmin)


# ==========================================
# 5. CORE LOGIC (Market & Auth)
# ==========================================
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


async def listen_to_postgres():
    """Listens to 'stock_updates' channel on MARKET_DB"""
    try:
        conn = await asyncpg.connect(MARKET_DB_DSN)
        await conn.add_listener("stock_updates", lambda c, p, ch, pay: asyncio.create_task(manager.broadcast(pay)))
        print("✅ Market Listener Active")
        while True:
            await asyncio.sleep(60)
    except Exception as e:
        print(f"❌ Market Listener Error: {e}")
        await asyncio.sleep(5)


async def validate_api_key(api_key: str) -> bool:
    """Checks AUTH_DB to see if key is active"""
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

@app.get("/")
def home():
    return {"status": "online", "service": "NexoDynamix API"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, api_key: str = Query(...)):
    # 1. Validate Key
    if not await validate_api_key(api_key):
        await websocket.close(code=1008)  # Policy Violation
        return

    # 2. Accept Connection
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # Keep open
    except:
        manager.disconnect(websocket)


@app.get("/api/eod", dependencies=[Depends(get_api_key)])
async def get_eod_price(ticker: str, date: str):
    # 1. Parse Date
    try:
        date_obj = date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "Invalid Date Format (YYYY-MM-DD)")

    # 2. Fetch Data from Market DB
    try:
        conn = await asyncpg.connect(MARKET_DB_DSN)
        try:
            row = await conn.fetchrow(
                "SELECT close_price, recorded_at FROM historical_prices WHERE ticker = $1 AND recorded_at = $2::date",
                ticker, date_obj
            )
            if row:
                return {"ticker": ticker, "date": str(row['recorded_at']), "price": float(row['close_price'])}
            raise HTTPException(404, "Data not found")
        finally:
            await conn.close()
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Server Error: {e}")
        raise HTTPException(500, "Internal Server Error")