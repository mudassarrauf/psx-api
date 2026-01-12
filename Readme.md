
# NexoDynamix Market Data API

**Version:** 1.0.0
**Base URL:** `https://api.nexodynamix.com`

## 🔐 Authentication

This API is secured via an API Key. You must include your key in every request.

| Method | Location | Key Name | Example |
| --- | --- | --- | --- |
| **HTTP Requests** | Header | `X-API-KEY` | `X-API-KEY: sk_live_...` |
| **WebSockets** | Query Param | `api_key` | `ws://...?api_key=sk_live_...` |

---

## 📡 REST Endpoints

### 1. Get Intraday Chart Data

Fetches all recorded trades for the current day. Use this to draw the "Today" line chart.

* **Endpoint:** `GET /api/intraday`
* **Parameters:**
* `ticker` (string, required): The stock symbol (e.g., `OGDC`, `TRG`, `BTC`).



**Request:**

```bash
curl -H "X-API-KEY: YOUR_KEY" \
     "https://api.nexodynamix.com/api/intraday?ticker=OGDC"

```

**Response (200 OK):**

```json
{
  "ticker": "OGDC",
  "count": 3,
  "data": [
    // [ Timestamp (Unix), Price, Volume ]
    [ 1736652000, 115.50, 500 ],
    [ 1736652300, 115.65, 200 ],
    [ 1736652600, 115.40, 1000 ]
  ]
}

```

---

### 2. Get Latest Price (Snapshot)

Fetches the single most recent price. Useful for watchlists or widgets that don't need a live socket connection.

* **Endpoint:** `GET /api/latest`
* **Parameters:**
* `ticker` (string, required): The stock symbol.



**Request:**

```bash
curl -H "X-API-KEY: YOUR_KEY" \
     "https://api.nexodynamix.com/api/latest?ticker=FFC"

```

**Response (200 OK):**

```json
{
  "ticker": "FFC",
  "price": 598.50,
  "updated_at": "2026-01-12T08:05:00.123Z"
}

```

---

### 3. Get Historical Price (EOD)

Fetches the closing price for a specific date in the past.

* **Endpoint:** `GET /api/eod`
* **Parameters:**
* `ticker` (string, required): The stock symbol.
* `date` (string, required): Format `YYYY-MM-DD`.



**Request:**

```bash
curl -H "X-API-KEY: YOUR_KEY" \
     "https://api.nexodynamix.com/api/eod?ticker=LUCK&date=2025-12-01"

```

**Response (200 OK):**

```json
{
  "ticker": "LUCK",
  "date": "2025-12-01",
  "price": 845.25
}

```

---

## ⚡ Real-Time WebSocket

Connect to this endpoint to receive instant price updates whenever the market moves.

* **URL:** `wss://api.nexodynamix.com/ws`
* **Query Param:** `?api_key=YOUR_KEY`

**Connection Example (JS):**

```javascript
const ws = new WebSocket("wss://api.nexodynamix.com/ws?api_key=sk_live_...");

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log("Price Update:", data);
};

```

**Message Payload:**
The server broadcasts a JSON object whenever any stock is updated in the database:

```json
{
  "ticker": "TRG",
  "price": 72.45,
  "updated_at": "2026-01-12T08:10:00.000Z"
}

```

---

## 🛑 Error Codes

| Code | Meaning | Description |
| --- | --- | --- |
| **400** | Bad Request | Missing ticker or invalid date format. |
| **403** | Forbidden | Invalid or missing API Key. |
| **404** | Not Found | The ticker does not exist in the database. |
| **500** | Server Error | Database connection failure or internal error. |

