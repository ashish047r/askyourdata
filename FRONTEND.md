# FRONTEND: AskYourData

**Related:** [PRD.md](PRD.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [SECURITY.md](SECURITY.md)

- **Stack:** Django templates + one CSS file + one vanilla JS file. No framework, no build step.
- **Why:** the value of this project is the backend, safety and evals. A small, clean UI that's easy to explain beats a React app you can't defend in an interview.

> **Implementation status (2026-10-03):** all phases are built. File and function names changed during the build (e.g. `guards.py`, `chains.py`, `graph.py`); **prj.md is the source of truth** where this doc differs.

---

## 1. Pages

| URL | Template | Purpose |
|---|---|---|
| `/login/` | `login.html` | Django `LoginView`: username + password |
| `/` | `app.html` | The tool (login required) |
| `/admin/` | Django admin | Users, client access, logs (admin only) |

## 2. Layout: `app.html`

```
┌──────────────────────────────────────────────────────────────────────┐
│ AskYourData          [ Client: Augmintech ▾ ]            ashish · ⎋ │
├───────────────┬──────────────────────────────────────────────────────┤
│ Recent        │  ┌────────────────────────────────────────────────┐  │
│ ───────────── │  │ Ask about this client's GA4 + Google Ads data… │  │
│ Top campaigns │  │                                                │  │
│ Sessions by…  │  └────────────────────────────────────────────────┘  │
│ Spend Search… │  [Top 5 campaigns last month] [Sessions by device]   │
│               │                                     [ Ask  ⌘↵ ]     │
│               │                                                      │
│               │  ANSWER                                              │
│               │  Brand-Search drove 142 conversions at ₹310 CPA…     │
│               │                                                      │
│               │  ▸ Show SQL                    (native <details>)    │
│               │  ┌──────────────┬────────┬──────────┐                │
│               │  │ campaign     │ conv.  │ cost/conv│  (table)       │
│               │  └──────────────┴────────┴──────────┘                │
│               │  4.2 s · $0.003 · cached ✓ · data synced 2 h ago     │
└───────────────┴──────────────────────────────────────────────────────┘
```

- **Mobile (< 720 px):** the sidebar collapses under the main panel and the table scrolls horizontally inside its own container.

## 3. Components & behaviour

| Component | Element | Behaviour |
|---|---|---|
| Client picker | `<select>` | Filled from `GET /api/clients/`. Last choice remembered in `localStorage` (wrapped in try/catch). Changing it reloads history. |
| Question box | `<textarea maxlength="500">` | Ctrl/⌘+Enter submits. Character counter shown near the limit. |
| Example chips | `<button>`s | Click fills the textarea (doesn't auto-submit). |
| Ask button | `<button>` | Disabled while a request is in flight (prevents double spend). |
| Answer | `<p aria-live="polite">` | Set with `textContent`. |
| SQL | `<details><summary>Show SQL</summary><pre>` | Native disclosure, no JS. Copy button uses `navigator.clipboard`. |
| Result table | `<table>` | Built with `createElement` + `textContent`. Numbers right-aligned and formatted with `Intl.NumberFormat('en-IN')`. |
| Meta line | `<small>` | Latency, cost, cache hit, retry used, data freshness. |
| History | `<ul>` in sidebar | `GET /api/history/?client_id=`. Clicking an item re-shows that question in the box. |

## 4. States

| State | UI |
|---|---|
| Idle | Placeholder + example chips |
| Loading | Button shows "Thinking…", a skeleton block replaces the answer, and a timer counts seconds (cold start can take ~60 s; a hint appears after 10 s: "Server waking up…") |
| Success | Answer + SQL + table + meta |
| Zero rows | Answer says no data found; table hidden; SQL still shown |
| Rejected (guard) | Amber notice: "I couldn't build a safe query for that. Try rephrasing." SQL shown for transparency. |
| Error | Red notice with a short message and the `query_id` for debugging |
| 429 | "Hourly limit or daily budget reached. Try later." |
| 401/403 | Redirect to `/login/` / show "No access to this client" |

## 5. API contract used by the frontend

**`POST /api/ask/`**

Request:
```json
{ "client_id": 3, "question": "Top 5 campaigns by conversions last month" }
```

Response 200:
```json
{
  "query_id": 812,
  "status": "ok",
  "answer": "Brand-Search led with 142 conversions…",
  "sql": "SELECT campaign_name, SUM(conversions) …",
  "columns": ["campaign_name", "conversions", "cost_per_conversion"],
  "rows": [["Brand-Search", 142, 310.5]],
  "meta": { "latency_ms": 4210, "cost_usd": 0.0031, "cache_hit": false,
            "retries": 0, "data_synced_at": "2026-10-03T06:00:00Z" }
}
```

Errors: `400` (validation), `403` (no client access), `422` (`status: "rejected"`), `429`, `500`. Each returns `{status, error, query_id?}`.

## 6. JS (`static/app.js`, target ≲ 150 lines)

- `api(path, opts)`: a `fetch` wrapper. Adds `X-CSRFToken` from the cookie and `Content-Type: application/json`, and handles 401 by redirecting.
- `loadClients()`, `loadHistory(clientId)`, `ask()`.
- `renderAnswer(resp)`, `renderTable(columns, rows)`, `renderError(resp)`.
- **No `innerHTML` with server data.** Only `textContent` (XSS rule from SECURITY.md).

## 7. Styling (`static/app.css`)

**Brand:** this is an internal EverClif tool, so it uses EverClif tokens.

```css
:root {
  --navy: #000080; --accent: #1E90FF; --amber: #F59E0B;
  --bg: #F5F6F7; --surface: #fff; --text: #333; --muted: #6b7280;
  --danger: #dc2626; --radius: 10px; --gap: 16px;
  --font: 'Satoshi', system-ui, -apple-system, 'Segoe UI', sans-serif;
}
```

- **Font:** Satoshi via the Fontshare CSS link, falling back to the system font.
- ⚠️ **Variable-font gotcha** (already seen in Blog Automation): every bold selector must set `font-variation-settings: 'wght' 700` together with `font-weight: 700`.
- **Layout:** CSS Grid (`grid-template-columns: 240px 1fr`), changed to a single column below 720 px.
- **Dark mode:** skipped in v1. Add a `prefers-color-scheme` block later if wanted.

## 8. Accessibility checklist

- Every input has a `<label>`. The answer region is `aria-live="polite"`.
- Visible focus ring (`:focus-visible { outline: 2px solid var(--accent) }`).
- Colour is never the only signal: notices include an icon/word ("Rejected", "Error").
- Keyboard: Tab order is client → question → Ask; Ctrl/⌘+Enter submits.
- Table has `<th scope="col">` headers.

## 9. Hardening additions (status: charts ✅, feedback ✅, streaming ❌ skipped)

- **Charts:** Chart.js 4.4.1 via jsDelivr (`chart.umd.js`). Line chart when the first column is a date, bar chart otherwise.
- **Feedback:** 👍/👎 per answer, sent to `POST /api/feedback/`.
- **Streaming:** stream the summary token by token via `StreamingHttpResponse` + `fetch` reader.
