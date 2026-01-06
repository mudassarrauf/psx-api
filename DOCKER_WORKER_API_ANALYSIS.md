# Docker Worker API Analysis - PSX (NexoDynamix)

## Executive Summary

The PSX API is a containerized FastAPI application that provides real-time stock market data streaming and historical price queries. It operates as a Docker-based microservice architecture with PostgreSQL database integration and WebSocket support for live market updates.

---

## 1. Architecture Overview

### Container Structure
```
┌─────────────────────────────────────────────────────────┐
│                    Docker Compose Stack                 │
├─────────────────────────────────────────────────────────┤
│  psx-api (FastAPI)          auth-db (PostgreSQL)       │
│  Port: 8001 → 8000          Port: Internal Only        │
│  Network: shared_proxy_network                          │
└─────────────────────────────────────────────────────────┘
```

### Worker Components

The application implements several "worker" patterns:

1. **Uvicorn ASGI Worker** (main.py:15)
   - Runs FastAPI application
   - Handles HTTP and WebSocket connections
   - Deployed via: `uvicorn main:app --host 0.0.0.0 --port 8000`

2. **PostgreSQL Listener Worker** (main.py:179-188)
   - Background asyncio task
   - Listens to `stock_updates` PostgreSQL NOTIFY channel
   - Broadcasts market data to WebSocket clients
   - Auto-reconnects on failure with 5-second delay

3. **WebSocket Connection Manager** (main.py:155-174)
   - Manages active WebSocket connections
   - Broadcasts real-time updates to all connected clients
   - Handles connection lifecycle and error recovery

---

## 2. Docker Configuration Analysis

### Dockerfile (`Dockerfile`)

**Base Image:** `python:3.9-slim`

**Key Characteristics:**
- Minimal attack surface (slim variant)
- PostgreSQL client libraries installed (`libpq-dev`, `gcc`)
- Single application file deployment (`main.py`)
- No volume mounts (stateless worker)

**Build Process:**
```dockerfile
1. Install system dependencies for PostgreSQL
2. Install Python dependencies from requirements.txt
3. Copy application code
4. Expose port 8000
5. Start Uvicorn server
```

**Production Concerns:**
- ✅ Uses slim base image
- ✅ Single-layer application copy
- ⚠️  No health checks defined
- ⚠️  Runs as root user (security concern)
- ⚠️  No log rotation configured

### Docker Compose (`docker-compose.yml`)

**Services:**

#### 1. psx-api Service
```yaml
Container: psx-api
Port Mapping: 8001:8000
Restart Policy: always
Dependencies: auth-db
```

**Environment Variables:**
- `MARKET_DB_DSN`: External market database connection (read-only)
- `AUTH_DB_DSN`: Internal auth database connection
- `ADMIN_USER`: Admin panel username
- `ADMIN_PASS`: Admin panel password

**Network:** Connected to external `shared_proxy_network` (assumes reverse proxy)

#### 2. auth-db Service
```yaml
Image: postgres:15-alpine
Container: auth-db
Volume: auth_db_data (persistent)
Network: Internal only
```

**Security Analysis:**
- ✅ Separate database for authentication
- ✅ Persistent volume for data
- ✅ Internal network isolation
- ⚠️  Credentials in environment variables (should use secrets)

---

## 3. Worker API Functionality

### Real-Time Worker (PostgreSQL Listener)

**File:** `main.py:179-188`

```python
async def listen_to_postgres():
    try:
        conn = await asyncpg.connect(MARKET_DB_DSN)
        await conn.add_listener("stock_updates",
            lambda c, p, ch, pay: asyncio.create_task(manager.broadcast(pay)))
        print("✅ Market Listener Active")
        while True:
            await asyncio.sleep(60)
    except Exception as e:
        print(f"❌ Market Listener Error: {e}")
        await asyncio.sleep(5)
```

**Behavior:**
1. Connects to market database via `MARKET_DB_DSN`
2. Subscribes to PostgreSQL `NOTIFY` channel: `stock_updates`
3. Broadcasts received updates to all WebSocket clients
4. Implements infinite loop with 60-second heartbeat
5. Auto-recovers from failures with 5-second backoff

**Lifecycle:** Started during application lifespan (main.py:116)

### WebSocket Broadcasting Worker

**File:** `main.py:155-174`

**Connection Manager Responsibilities:**
1. Accept incoming WebSocket connections
2. Maintain active connection pool
3. Broadcast messages to all connected clients
4. Handle disconnections gracefully
5. Remove dead connections automatically

**Error Handling:**
- Silently removes failed connections during broadcast
- No explicit retry mechanism for individual clients

---

## 4. API Endpoints

### Public Endpoints

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/` | GET | None | Health check |
| `/ws` | WebSocket | Query param | Real-time market data stream |
| `/api/eod` | GET | Header | Historical end-of-day prices |
| `/api/latest` | GET | Header | Latest live price snapshot |
| `/sys-control` | Admin | Session | API key management panel |

### Authentication Methods

1. **WebSocket:** `?api_key={key}` (query parameter)
2. **REST API:** `X-API-KEY: {key}` (header)
3. **Admin Panel:** Session-based (username/password)

**API Key Format:** `sk_live_{32-char-urlsafe-token}`

### Worker-Related Endpoints

**WebSocket Endpoint** (`/ws`) - main.py:215-225
- Validates API key before connection
- Adds client to broadcast pool
- Listens for incoming messages (keepalive)
- Removes client on disconnect

**Latest Price Endpoint** (`/api/latest`) - main.py:251-278
- Designed for clients that cannot maintain WebSocket
- Direct database query to `stocks` table
- Returns current `live_price` and `last_updated`

---

## 5. Database Architecture

### Market Database (External, Read-Only)
**Tables Referenced:**
- `stocks`: Live price data with `last_updated` timestamp
- `historical_prices`: End-of-day closing prices

**Trigger/Notification:**
- PostgreSQL `NOTIFY` on `stock_updates` channel
- Payload broadcasted to WebSocket clients

### Auth Database (Internal, SQLAlchemy)
**Table:** `api_clients`

**Schema:**
```python
id: Integer (PK)
client_name: String
api_key: String (unique, indexed)
email: String (optional)
is_active: Boolean (default: True)
created_at: DateTime (auto)
```

**ORM:** SQLAlchemy with async PostgreSQL driver (`asyncpg`)

---

## 6. Security Analysis

### Strengths
✅ API key authentication on all protected endpoints
✅ Separate auth database isolation
✅ Admin panel hidden at non-standard path (`/sys-control`)
✅ CORS middleware configured
✅ Trusted host middleware (wildcard for reverse proxy)
✅ WebSocket auth before connection acceptance

### Vulnerabilities & Concerns

#### Critical
🔴 **Secrets in Environment Variables** (docker-compose.yml:16-17)
- Database passwords in plain text
- Admin credentials in environment
- Recommendation: Use Docker secrets or external secret manager

🔴 **Root User in Container** (Dockerfile)
- Application runs as root
- Recommendation: Add `USER` directive in Dockerfile

#### High
🟠 **Wildcard CORS** (main.py:129)
- `allow_origins=["*"]` allows any domain
- Recommendation: Restrict to known client domains

🟠 **No Rate Limiting**
- No throttling on API endpoints or WebSocket connections
- Risk: Resource exhaustion attacks

🟠 **Broad Exception Handling** (main.py:186, 223-224)
- Silent failures may hide critical errors
- Recommendation: Implement structured logging

#### Medium
🟡 **No Health Checks** (Dockerfile)
- Container orchestration cannot detect unhealthy workers
- Recommendation: Add `HEALTHCHECK` directive

🟡 **No Request Validation**
- Missing input sanitization on ticker symbols
- Potential SQL injection vector (mitigated by parameterized queries)

🟡 **Session Secret from Environment** (main.py:33)
- Falls back to runtime-generated secret
- Sessions invalidated on restart if not set

---

## 7. Performance Considerations

### Strengths
✅ Asynchronous I/O throughout (asyncpg, FastAPI)
✅ Connection pooling via SQLAlchemy async session
✅ Efficient WebSocket broadcasting (single loop)
✅ PostgreSQL LISTEN/NOTIFY for event-driven updates

### Bottlenecks
⚠️  **Database Connection per Request**
- `/api/eod` and `/api/latest` create new connections
- Recommendation: Use connection pool

⚠️  **No Caching**
- `/api/latest` queries database on every request
- Recommendation: Add Redis cache with TTL

⚠️  **Synchronous Broadcast Loop**
- Broadcasting iterates all connections sequentially
- Large connection pools may introduce latency

⚠️  **No Message Queue**
- Direct coupling between database events and WebSocket clients
- Recommendation: Add Redis Pub/Sub or RabbitMQ for scalability

---

## 8. Scaling Recommendations

### Current Limitations
1. **Stateful WebSocket Connections**
   - Cannot horizontally scale without sticky sessions
   - Load balancer must maintain client affinity

2. **Single PostgreSQL Listener**
   - Only one instance receives NOTIFY events
   - Multiple replicas won't receive broadcasts

### Scaling Solutions

#### Option 1: Redis Pub/Sub
```
Market DB → Trigger → Redis Pub/Sub → Multiple API Workers → WebSocket Clients
```

#### Option 2: Message Queue
```
Market DB → Trigger → RabbitMQ → Worker Pool → WebSocket Clients
```

#### Option 3: Serverless WebSockets
```
Market DB → Trigger → AWS EventBridge → API Gateway WebSocket → Clients
```

---

## 9. Monitoring & Observability

### Current State
- ❌ No structured logging
- ❌ No metrics collection
- ❌ No tracing
- ❌ No health check endpoint
- ✅ Basic console logging (`print` statements)

### Recommendations
1. **Add Prometheus Metrics**
   - Active WebSocket connections
   - Request latency percentiles
   - Database query duration
   - API key validation cache hit rate

2. **Structured Logging**
   - Replace `print()` with proper logger
   - Add request IDs for tracing
   - Log levels: DEBUG, INFO, WARNING, ERROR

3. **Health Check Endpoint**
```python
@app.get("/health")
async def health_check():
    # Check database connectivity
    # Check listener task status
    # Return 200 OK or 503 Service Unavailable
```

4. **APM Integration**
   - Add OpenTelemetry instrumentation
   - Integrate with Datadog/New Relic/Sentry

---

## 10. Dependencies Analysis

**File:** `requirements.txt`

```
fastapi          → Web framework
uvicorn[standard] → ASGI server
asyncpg          → Async PostgreSQL driver
psycopg2-binary  → Sync PostgreSQL driver (for SQLAdmin)
python-dotenv    → Environment variable loading
sqladmin[full]   → Admin panel
sqlalchemy       → ORM
greenlet         → Async/sync bridge
```

**Security Audit:**
- ✅ No known critical vulnerabilities (as of analysis date)
- ⚠️  Missing version pinning (may cause dependency drift)
- Recommendation: Pin exact versions in production

---

## 11. Deployment Architecture

### Production Environment
```
Internet → Nginx Reverse Proxy (shared_proxy_network)
           ↓
       psx-api:8001 (Docker Container)
           ↓
       ┌─────────────────┬────────────────┐
       ↓                 ↓                ↓
   auth-db         market-db         WebSocket Pool
   (internal)      (external)        (in-memory)
```

### Network Flow
1. External requests → Nginx → Container port 8001
2. Container port 8001 → Internal port 8000 (Uvicorn)
3. API validates key → Queries databases
4. Market updates → PostgreSQL NOTIFY → Listener task → Broadcast

---

## 12. Key Findings Summary

### Worker Pattern Implementation
The application implements a **hybrid worker model**:
1. **Web Worker:** Uvicorn serving HTTP/WebSocket requests
2. **Background Worker:** Async PostgreSQL listener broadcasting updates
3. **Connection Worker:** WebSocket manager maintaining client pool

### Critical Issues
1. Security: Root user, plain-text secrets, wildcard CORS
2. Scalability: Stateful WebSocket, single listener instance
3. Observability: No metrics, basic logging

### Recommended Immediate Actions
1. ✅ Add Dockerfile `USER` directive (non-root)
2. ✅ Implement Docker secrets for credentials
3. ✅ Add health check endpoint and Dockerfile `HEALTHCHECK`
4. ✅ Pin dependency versions
5. ✅ Restrict CORS origins
6. ✅ Add rate limiting middleware

### Long-Term Improvements
1. Migrate to Redis Pub/Sub for horizontal scaling
2. Implement connection pooling
3. Add caching layer (Redis)
4. Integrate APM/observability platform
5. Implement comprehensive error handling and logging

---

## 13. Test Coverage

**Test File:** `test.html`

**Purpose:** Manual WebSocket connection tester

**Features:**
- API key input
- WebSocket connection management
- Real-time message logging
- Connection status monitoring

**Coverage Gaps:**
- No unit tests
- No integration tests
- No load testing
- No security testing

**Recommendation:** Implement pytest suite with:
- API endpoint tests
- WebSocket connection tests
- Authentication tests
- Database integration tests

---

## Conclusion

The PSX Docker Worker API is a well-architected real-time data streaming service with a clean separation of concerns. However, it requires security hardening, observability improvements, and scalability enhancements before production deployment at scale.

**Overall Rating:** 7/10
- **Functionality:** 9/10 (works as designed)
- **Security:** 5/10 (multiple vulnerabilities)
- **Scalability:** 6/10 (limited horizontal scaling)
- **Observability:** 3/10 (minimal monitoring)

---

*Analysis Date: 2026-01-06*
*Analyzed By: Claude (Anthropic)*
*Repository: mudassarrauf/psx-api*
*Branch: claude/analyze-docker-worker-api-WSkTi*
