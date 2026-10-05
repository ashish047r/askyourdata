# prj.md: AskYourData, end to end

AskYourData is an internal EverClif web tool. A team member picks a client, asks a question about that client's GA4 or Google Ads data in plain English, and gets back:
- a short answer,
- the result table,
- a chart,
- the exact SQL that produced the numbers.

An LLM writes the SQL. Everything around the LLM is deterministic code: it validates the SQL, isolates each client's data, caches answers, logs every request, and measures quality.

This file is the single source of truth for how the project works. For plain-language explanations and interview answers, see **INTERVIEW_GUIDE.md**. If PRD.md, ARCHITECTURE.md, SECURITY.md, FRONTEND.md or TICKETS.md disagree with it, this file wins.

---

## 0. Status: built vs. still to do

| Area | Status |
|---|---|
| Django app, API, frontend, auth, admin | ✅ built, tested locally |
| LangGraph agent (retrieve → write SQL → check → execute → answer, self-correction, clarify) | ✅ built; graph logic tested with mocked LLM |
| SQL guard, read-only role, row-level security (RLS), DB-level attack tests | ✅ built, tested on real Postgres 16 |
| Exact + semantic cache (pgvector) with constraint guard | ✅ built, tested on real Postgres |
| Cost tracking, daily cost cap, rate limit, query log, feedback | ✅ built, tested |
| Langfuse tracing with data masking | ✅ built; masking unit-tested. ⚠️ Not yet sent to a real Langfuse project |
| Eval runner: 100-case golden set, judge, calibration, cache-pair tuning, CI gate | ✅ **Run for real: 90% (v1) → 97% (v2) execution accuracy, 100% faithfulness**, baseline committed |
| GA4 + Google Ads sync | ✅ written. ⚠️ Not run against the live Google APIs (needs your OAuth and developer token) |
| Real OpenAI calls (gpt-5-mini) | ✅ live: about 8.6 s typical, about $0.0015 per question; cached repeat 0.4 s / $0 |
| Deploy (Render + Neon), GitHub Actions CI + daily sync | ✅ config written (`render.yaml`, `.github/workflows/`). ⚠️ You deploy it (§15) |
| Streaming answers | ❌ deliberately skipped. The UI shows a live timer instead. Add only if people complain about waiting. |

> **Resume rule:** put numbers on your resume only after §15 is done and they come from your own `evals/reports/*.json`. Never quote the mocked numbers from development.

---

## 1. Why this exists (when MCP + Claude already works)

The MCP servers let one person, in Claude Desktop, ask Claude to call the Ads and GA4 APIs. Claude then does the arithmetic itself. AskYourData changes that in six ways:

- **Exact numbers:** Postgres does the maths (`SUM`, ratios, joins). The LLM only writes the query.
- **Measurable accuracy:** a 100-question golden set gives a real execution-accuracy number.
- **Access control:** each user only sees their assigned clients, enforced by Postgres.
- **A team tool:** one web link, with no per-person OAuth or Claude Desktop setup.
- **Cross-source questions:** GA4 and Ads live in one database, so they can be joined.
- **Observability:** cost, latency, failures and feedback are logged for every query.

---

## 2. Architecture

```
Browser (HTML/CSS/JS + Chart.js)          Postman (token auth)
          │ session cookie                        │
          ▼                                       ▼
┌──────────────────────── Render free web service (gunicorn, 1 worker × 4 threads) ───────────────────────┐
│ Django + DRF                                                                                            │
│  views.AskView ── throttle (60/h) ──► services.ask()                                                    │
│                                         ├─ access check (Client.users)                                  │
│                                         ├─ daily cost cap (sum of QueryLog.cost_usd today)              │
│                                         ├─ cache_get_exact  ──────────────► Postgres cache_entry        │
│                                         ├─ embed(question) ───────────────► OpenAI text-embedding-3-small│
│                                         ├─ cache_get_semantic (pgvector + constraint guard)             │
│                                         ├─ answer_question()  = LangGraph ───────────────┐              │
│                                         ├─ QueryLog row (status, SQL, tokens, cost, ms)   │              │
│                                         └─ cache_set (only clean successes)               │              │
│                                                                                           ▼              │
│   retrieve_examples ─► write_sql ─► check_sql ─► execute ─► answer                                      │
│   (in-memory vectors)  (gpt-5-mini) (sqlglot)    (askdata_ro + RLS) (gpt-5-mini)                         │
│          Langfuse callback traces every node and LLM call (result rows masked)                          │
└─────────────────────────────────────────────────────────────────────────────────────────────────────────┘
                      │ owner role (ORM)                    │ read-only role (LLM SQL only)
                      ▼                                     ▼
              Neon Postgres 16 (free): clients · ga4_daily · ads_campaign_daily · query_log · cache_entry
                      ▲
GitHub Actions cron (daily) ─ sync_data ─► GA4 Data API + Google Ads API (read-only)
GitHub Actions CI ─ core tests + integration tests + eval gate (Postgres service container)
```

### Tech stack and why

| Layer | Choice | Why (and what was rejected) |
|---|---|---|
| Web/API | Django 6 + Django REST Framework | You already know Django. It gives auth, admin (the monitoring UI), ORM, migrations and throttling for free. FastAPI would mean learning it alongside evals. |
| Agent orchestration | **LangGraph** | The retry loop and the clarification exit are real branches, and a graph models them explicitly. Each node is a plain function, so it's testable. |
| LLM wrappers | LangChain (`ChatOpenAI`, `ChatPromptTemplate`, `with_structured_output`, `InMemoryVectorStore`) | Structured output removes fragile parsing. The vector store gives dynamic few-shot without a vector DB. |
| LLM | `gpt-5-mini`, `reasoning_effort=low` | Cheap ($0.25 / $2 per 1M tokens) and good at SQL. Low effort keeps latency down. Configurable via `OPENAI_MODEL`. |
| Embeddings | `text-embedding-3-small` | Cheap. One embedding per question serves both the cache and example retrieval. |
| SQL safety | `sqlglot` (AST parsing) | A regex `startswith("SELECT")` is trivially bypassed. A real parser lets us allowlist tables and functions. |
| Database | Postgres 16 on **Neon** (free) | One SQL dialect for both sources, row-level security, pgvector. Neon's free tier doesn't expire, unlike Render's 30-day free Postgres. |
| Cache | Postgres table + pgvector | No extra service. Redis would be another free tier to manage. |
| Tracing | **Langfuse** (optional) | Open-source LLM tracing with a LangChain callback and server-side masking. |
| Hosting | **Render** free web service | Git-push deploys, HTTPS, `render.yaml` as code. The trade-off: it sleeps after 15 min idle. |
| CI/CD + cron | **GitHub Actions** | Free CI with a Postgres service container. Render free has no cron jobs. |
| Frontend | Django templates + vanilla JS + Chart.js | No build step. The interview value is the backend. |

---

## 3. One request, step by step

Example: a user types "Top 5 campaigns by conversions last month" for client Demo Co.

1. **`static/app.js` → `ask()`** POSTs `{client_id, question}` to `/api/ask/` with the CSRF header.
2. **`agent/views.py` → `AskView.post`**
   - DRF authenticates the user (session cookie or token).
   - `ScopedRateThrottle` enforces 60/hour.
   - `AskIn` checks that `client_id` is an int and the question is 3–500 chars.
3. **`agent/services.py` → `ask()`**
   1. Normalises whitespace.
   2. Checks the user can access the client with `user_clients(user)` (a superuser sees all clients). If not, it returns **403**.
   3. Creates a `QueryLog` and sets `flagged = looks_like_injection(question)`.
   4. If `spent_today(user)` ≥ `DAILY_COST_CAP_USD`, it returns **429** with status `blocked`.
   5. `cache_key(question, client_id, data_version)` is a sha256 over the lowercased question, client, `last_synced_at`, `PROMPT_VERSION` and model.
   6. `cache_get_exact(key)`: on a hit, it returns immediately at zero LLM cost.
   7. `embed(question)` produces one vector.
   8. `cache_get_semantic(client, question, vector, data_version)`: the nearest cached question for the same client, data version, prompt and model, within a cosine distance of 0.08, **and** `same_constraints()` must be true. On a hit, it returns.
   9. `answer_question(question, client_id, vector, callbacks=Langfuse)` runs the graph (§4).
   10. If the status is `ok` and the question isn't flagged, `cache_set(...)`.
   11. Fills in the `QueryLog` (SQL, answer, error, retries, per-node timings, tokens, cost, latency, cache kind) and saves it.
   12. Returns the JSON payload and an HTTP status (ok/clarify 200, rejected 422, error 500, blocked 429).
4. **`app.js` → `render()`** shows:
   - the answer (always `textContent`),
   - a chart (line for dates, bar for categories),
   - the SQL in a native `<details>`,
   - the table,
   - a meta line (latency, cost, cache, self-corrected),
   - 👍/👎 buttons that POST to `/api/feedback/`.

---

## 4. The agent (LangGraph): `agent/core/graph.py`

```
START → retrieve_examples → write_sql ──(needs_clarification)──► END  (status=clarify)
                               │  ▲
                               ▼  │ error AND attempts < 2  (self-correction: previous SQL + error fed back)
                            check_sql ──ok──► execute ──ok──► answer → END (status=ok)
                               │                 │
                               └──error, attempts = 2──► fail → END (status=rejected | error)
```

### State (`State` TypedDict)

| Field | Meaning |
|---|---|
| `question`, `client_id`, `question_vector` | Inputs |
| `examples` | Few-shot examples chosen for this question |
| `sql` | Raw SQL from the LLM |
| `safe_sql` | SQL after the guard (LIMIT enforced) |
| `attempts`, `error` | Retry control |
| `columns`, `rows`, `truncated` | The DB result |
| `status`, `answer`, `clarifying_question` | The output |
| `usage` | `Annotated[dict, add_usage]`, a **reducer**: token counts and cost from every node are summed automatically |
| `timings` | Same reducer: milliseconds per node, summed if a node runs twice |

### Nodes

Every node is wrapped by `timed` so it reports its own latency.

| Node | Does | On failure |
|---|---|---|
| `retrieve_examples` | `select_examples(vector, k=4)` | — |
| `write_sql` | `generate_sql(question, examples, previous_sql, error)` returns a `SqlDraft{needs_clarification, clarifying_question, sql}`. It increments `attempts`, clears `error` and `safe_sql`. | Clarification → END |
| `check_sql` | `validate_sql(sql)` | Sets `error = "rejected by SQL guard: …"` |
| `execute` | `run_sql(safe_sql, client_id)` | `psycopg.Error` → `error = "database error: …"` |
| `answer` | `summarize(question, safe_sql, columns, rows)` | — |
| `fail` | Sets status `rejected` (guard) or `error` (DB) | — |

### Routing
- `after_write`: if the status is `clarify`, go to END; otherwise go to `check_sql`.
- `after_check(next)`: if there's no error, go to `next`. If there's an error and attempts < `MAX_ATTEMPTS` (2), go back to `write_sql`. Otherwise go to `fail`.
- Both the SQL guard and the DB errors therefore get **exactly one self-correction**, which bounds cost and latency.

### Entry point
`answer_question(question, client_id, question_vector, callbacks, metadata)` is the **only** entry point. It's used by the API, the eval runner and tests, so no logic is duplicated.

---

## 5. Function reference (every file)

### `data/` (analytics data and sync)

| Function / class | What it does | In → Out | Called by | Calls |
|---|---|---|---|---|
| `models.Client` | A client, its API ids, assigned `users`, `last_synced_at` | — | everywhere | — |
| `models.Ga4Daily` | `ga4_daily` table, one row per day × source × medium × device | — | sync, seed, LLM SQL | — |
| `models.AdsCampaignDaily` | `ads_campaign_daily` table, one row per day × campaign | — | sync, seed, LLM SQL | — |
| `google_api.get_credentials()` | Refreshes OAuth credentials from the env refresh token | env → `Credentials` | `sync_data` | Google OAuth |
| `google_api.fetch_ga4_daily()` | GA4 `runReport` with 4 dimensions and 6 metrics, paginated | property, dates, creds → `list[dict]` | `sync_data` | GA4 Data API |
| `google_api._ads_search()` | Google Ads REST `searchStream` | customer, manager, GAQL → results | `fetch_ads_campaign_daily` | Ads API |
| `google_api.fetch_ads_campaign_daily()` | Campaign-day metrics (cost_micros → cost) plus account currency | ids, dates, creds → `(rows, currency)` | `sync_data` | `_ads_search` |
| `sync.upsert_rows()` | `bulk_create(update_conflicts=True)`, so re-syncing is idempotent | model, client, rows → count | `upsert_ga4`, `upsert_ads` | ORM |
| `sync.upsert_ga4()` / `upsert_ads()` | Upsert with each table's key and value fields | client, rows → count | `sync_data` | `upsert_rows` |
| `commands/sync_data` | For each active client, pulls the last N days ending yesterday, upserts, bumps `last_synced_at`. One failing client doesn't stop the others. | `--client --days` | you, GitHub cron | fetchers, upserts |
| `commands/google_login` | One-time browser OAuth; prints the refresh token | client_secret.json → env lines | you | google-auth-oauthlib |
| `commands/seed_demo` → `seed_client()` | Deterministic synthetic data for **Demo Co** and **Acme Labs** over 120 days, relative to today. It plants a LinkedIn sessions drop and a Generic-Search CPC spike. | `--user` | you, tests, CI | ORM |
| `migrations/0002_readonly_role_rls` | Creates `askdata_ro`, grants SELECT on the 2 tables only, creates `app_tenant_id()` and enables the RLS policies | — | `migrate` | SQL |

### `agent/core/` (pure Python, no Django; portable to FastAPI)

| Function / class | What it does | In → Out | Called by | Calls |
|---|---|---|---|---|
| `prompts.*` | `SCHEMA_DOC`, `DATE_RULES`, SQL, summary and judge prompts, `PROMPT_VERSION` | constants | chains, cache, services | — |
| `llm.chat_model(max_tokens)` | Cached `ChatOpenAI(model, reasoning_effort, max_tokens, timeout=45, max_retries=2)` | int → model | chains | — |
| `llm.embeddings()` / `llm.embed(text)` | Embedding client / one vector plus estimated usage | str → `(vector, usage)` | services, debug views, run_evals | OpenAI |
| `llm.usage()` / `usage_from(msg)` | Tokens → USD using the `PRICES` table | → `{input_tokens, output_tokens, cost_usd}` | chains | — |
| `llm.add_usage(a, b)` | Sums two dicts (the LangGraph reducer) | dicts → dict | graph, services, run_evals | — |
| `llm.mask(data=)` | Langfuse mask: hides `rows`, `question_vector`, and `<data>`/`<result>` blocks | any → any | Langfuse SDK | — |
| `llm.callbacks()` / `flush_traces()` | Langfuse handler if keys are set, else `[]` / flush on exit | — | services, run_evals | Langfuse |
| `guards.validate_sql(sql)` | The SQL guard (§8). Returns safe SQL with LIMIT ≤ 1000 or raises `SqlRejected` | str → str | graph, debug views, run_evals | sqlglot |
| `guards.looks_like_injection(q)` | Regex flag for prompt-injection phrases. Flagged questions are logged and never cached. | str → bool | services | — |
| `guards.constraints(q)` / `same_constraints(a, b)` | Extracts the answer-changing words (numbers, dates, metrics, dimensions, ranking words) and compares them | str → list / bool | cache, run_evals | — |
| `executor.run_sql(sql, client_id, dsn)` | Read-only role, temp `tenant_scope`, `transaction_read_only`, 5s timeout, one statement, rollback | → `{columns, rows, truncated}` | graph, debug, run_evals | psycopg |
| `executor._jsonable(v)` | Decimal → float, date → ISO, midnight timestamps → date | — | run_sql | — |
| `chains.load_jsonl(path)` | Reads a JSONL dataset | path → list | chains, run_evals | — |
| `chains.select_examples(vector, k)` | Dynamic few-shot: the k nearest of 24 examples (embedded once per process) | vector → `list[{question, sql}]` | graph, debug | InMemoryVectorStore |
| `chains.generate_sql(q, examples, previous_sql, error)` | Prompt → `with_structured_output(SqlDraft, include_raw=True)`. The **same function** does the first try and the retry. | → `(SqlDraft, usage)` | graph, debug | gpt-5-mini |
| `chains.summarize(q, sql, columns, rows, truncated)` | Answer from at most 50 rows inside `<data>` | → `(text, usage)` | graph, debug | gpt-5-mini |
| `chains.judge_answer(q, columns, rows, answer)` | LLM-as-judge → `Verdict{faithful, reason}` | → `(Verdict, usage)` | run_evals | gpt-5-mini |
| `graph.*` | See §4 | | | |
| `evals.compare_results(gold, pred, ordered)` | Execution-accuracy comparison (§10) | rows → bool | run_evals | — |
| `evals.percentile(values, p)` | p50 / p95 latency | — | run_evals | — |

### `agent/` (Django layer)

| Function / class | What it does | Called by | Calls |
|---|---|---|---|
| `models.QueryLog` | One row per ask: the monitoring and feedback table | services, admin, views | — |
| `models.CacheEntry` | Cached responses plus `embedding vector(1536)` | cache | — |
| `cache.cache_key()` | sha256 key (question, client, data_version, prompt, model) | services | — |
| `cache.cache_get_exact()` / `cache_get_semantic()` / `cache_set()` / `_hit()` | The two-level cache (§9) | services | pgvector `CosineDistance`, `same_constraints` |
| `services.user_clients(user)` | Clients this user can see | services, views | — |
| `services.spent_today(user)` | Today's LLM spend | `ask` | — |
| `services.ask(user, client_id, question)` | Orchestrates everything in §3 | `AskView` | cache, `embed`, `answer_question` |
| `views.AskView` / `ClientsView` / `HistoryView` / `FeedbackView` | The public API | urls | services, ORM |
| `views.DebugExamples` / `DebugGenerateSql` / `DebugValidateSql` / `DebugRunSql` / `DebugSummarize` | Staff-only endpoints, **one per pipeline step**, for Postman | urls | the core functions directly (no duplicated logic) |
| `views.AppView` / `healthz` | The app page / liveness for Render | urls | — |
| `admin.QueryLogAdmin` | Monitoring UI: filters, search, and a header with totals (count, cost, average latency) | /admin | — |
| `commands/run_evals` | Eval runner, gate, cache tuning and judge calibration (§10) | you, CI | `answer_question`, `run_sql`, `judge_answer` |

### `static/app.js` (frontend)

| Function | Does |
|---|---|
| `api(path, body)` | `fetch` wrapper; adds the CSRF header and redirects to login on 401 |
| `loadClients()` / `loadHistory()` | Fills the client picker (last choice remembered) and the recent-questions sidebar |
| `ask()` | Sends the question and shows a live seconds counter ("server waking up" after 10s). If a clarification is pending, it merges the answer into the original question. |
| `render(resp, status)` | Answer, notices per status, SQL, table, chart, meta line, feedback buttons |
| `renderTable()` / `renderChart()` | Safe DOM building / Chart.js (line for dates with up to 2 series on dual axes, bar for categories) |
| `notice(kind, text)` | Info / warn / error banner |

---

## 6. Call graphs

**Browser or Postman → `/api/ask/`**
```
AskView.post
└── services.ask
    ├── user_clients, spent_today, looks_like_injection
    ├── cache_key → cache_get_exact
    ├── llm.embed → cache_get_semantic → guards.same_constraints
    ├── graph.answer_question → GRAPH.invoke
    │   ├── retrieve_examples → chains.select_examples → _example_store (embeds 24 examples once)
    │   ├── write_sql → chains.generate_sql → chat_model → OpenAI
    │   ├── check_sql → guards.validate_sql
    │   ├── execute → executor.run_sql → Postgres (askdata_ro, RLS)
    │   ├── answer → chains.summarize → OpenAI
    │   └── (retry edge back to write_sql | fail)
    ├── cache_set
    └── QueryLog.save
```

**Evals:** `run_evals.handle` → leakage check → `validate_sql`(every gold query) → for each case, in 4 threads: `embed` → `answer_question` → `run_sql(gold)` → `compare_results` → [`judge_answer`] → summary → report JSON → [`--write-baseline` | `--gate`].

**Sync:** `sync_data.handle` → `get_credentials` → per client: `fetch_ga4_daily` → `upsert_ga4`, then `fetch_ads_campaign_daily` → `_ads_search` → `upsert_ads` → `last_synced_at = now` (which automatically invalidates that client's cache).

---

## 7. Data model and pipeline

- **`ga4_daily`:**
  - Columns: `client_id, date, session_source, session_medium, device_category, sessions, total_users, new_users, engaged_sessions, key_events (numeric), total_revenue (numeric)`.
  - Unique on (client, date, source, medium, device).
- **`ads_campaign_daily`:**
  - Columns: `client_id, date, campaign_id, campaign_name, campaign_status, channel_type, impressions, clicks, cost (numeric), conversions (numeric), conversions_value (numeric)`.
  - Unique on (client, date, campaign_id).
- **Money and conversion columns are `numeric`, not float.** Postgres `ROUND(x, 2)` doesn't accept double precision, so float columns made the LLM's otherwise-correct SQL fail. The schema now prevents that whole class of error.
- **GA4 "conversions" are now called *key events*.** The schema doc tells the model the difference between `key_events` (GA4) and `conversions` (Ads), and that `total_users` isn't additive across rows.
- **Pipeline:** the daily cron re-pulls the last 30 days because Ads conversions are attributed late. The upsert is idempotent. Data ends yesterday, since today's numbers are incomplete.
- **Why sync into Postgres instead of letting the LLM call the APIs live:**
  - one query language instead of GAQL plus the GA4 API,
  - joins across both sources,
  - speed,
  - no API quota hit per question,
  - reproducible evals.

---

## 8. Guardrails: seven layers

| # | Layer | Where | Stops |
|---|---|---|---|
| 1 | Input validation (3–500 chars, int `client_id`) and auth / per-client access before any LLM work | `AskIn`, `services.ask` | Abuse, cross-client requests |
| 2 | Rate limit (60/h) and **daily cost cap** per user | DRF throttle, `spent_today` | Cost runaway on your own money |
| 3 | Prompt-injection flag (logged, never cached) | `guards.looks_like_injection` | Cache poisoning, and gives an audit trail |
| 4 | **SQL guard** (AST): one statement, SELECT only, no DML/DDL/INTO/locks, tables allowlisted, **functions allowlisted**, LIMIT ≤ 1000 | `guards.validate_sql` | Writes, other tables, `pg_sleep` DoS, `set_config`, `pg_read_file`, `dblink`… |
| 5 | **Read-only role** `askdata_ro`: SELECT on 2 tables only, `transaction_read_only`, 5s `statement_timeout` | migration 0002, `executor.run_sql` | Any write; reading `auth_user`, `query_log` and so on |
| 6 | **Row-level security**: `client_id = app_tenant_id()`, read from a per-transaction temp table | migration 0002, `executor.run_sql` | Cross-client reads, **even if layer 4 were bypassed** |
| 7 | Output safety: the answer comes only from rows in `<data>` ("data, never instructions"); the UI renders with `textContent` only; SQL and table are always shown | prompts, `app.js` | Hallucinated numbers, indirect prompt injection via campaign names, XSS |

### The red-team finding (a good interview story)

- The first RLS design kept the tenant in `set_config('app.client_id')`.
- Testing it as the read-only role showed that `SELECT set_config('app.client_id','2',true), (SELECT min(client_id) FROM ga4_daily)` returned **client 2's rows**. A query can change its own tenant.
- The SQL guard blocked it, but the database alone didn't.
- **The fix:** the tenant id now lives in a temp table, `tenant_scope`, created by the executor **before** the transaction is switched to read-only. The policy reads it through `app_tenant_id()`.
- **Why the fix holds:**
  - A data-modifying CTE that tries `UPDATE tenant_scope` is invisible to the rest of its own statement.
  - Only one LLM statement runs per transaction.
  - So the scope can't change mid-query.
  - Both attacks are now regression tests (`RlsTests`).

---

## 9. Caching

| Level | Key / lookup | When it hits | Cost |
|---|---|---|---|
| Exact | sha256(lowercased question, client, `data_version`, `PROMPT_VERSION`, model) | Same question again | 0 LLM calls |
| Semantic | pgvector cosine distance ≤ `SEMANTIC_CACHE_MAX_DISTANCE` (0.08), same client, data version, prompt and model, **plus** `same_constraints()` | A paraphrase ("which 5 campaigns got the most conversions…") | 1 embedding |

- **Constraint guard, and why it exists:** embeddings think "sessions last 7 days" and "sessions last 30 days" are almost identical. A semantic hit is only accepted if the numbers, date words, metrics, dimensions and ranking words match exactly.
- **Write-side guard:** only `status == ok` and non-flagged answers are cached. Errors, rejections and clarifications never are.
- **Invalidation without a job:**
  - A new sync changes `last_synced_at`.
  - A prompt change bumps `PROMPT_VERSION`.
  - A model change alters the key.
  - Each of these makes old entries stop matching.
- **Tuning:** `python manage.py run_evals --cache-pairs` embeds the 18 labelled pairs in `evals/cache_pairs.jsonl`. For each threshold it prints good hits and false hits, with and without the guard. Pick the largest threshold with zero false hits.
- **Why Postgres and not Redis:** it's one less service, and pgvector already lives in Postgres. `ponytail` notes in the code say when to add an HNSW index (around 10k rows).

---

## 10. Evaluation

### Datasets (versioned in git)
- **`agent/data/examples.jsonl`:** 24 Q→SQL pairs used as few-shot examples in the prompt.
- **`evals/golden.jsonl`:**
  - 100 test cases: 40 GA4, 40 Ads, 20 cross-source.
  - Each case: `{id, client_slug, question, gold_sql, tags, ordered}`.
  - Every gold query was checked to pass the guard and return rows on the demo data.
- **No leakage:** `run_evals` refuses to run if any golden question also appears in the examples.
- **Why synthetic demo data:** it's deterministic, it's always "recent" (relative to today), it holds no client data (safe for CI and public demos), and it contains planted patterns (a LinkedIn drop, a CPC spike) that "which…" questions must find.
- **`evals/cache_pairs.jsonl`:** 18 question pairs labelled "should share an answer: yes/no".

### Metrics (`python manage.py run_evals --client demo --judge`)

| Metric | Definition |
|---|---|
| **Execution accuracy** (headline) | The predicted SQL's rows equal the gold SQL's rows: compared as a multiset (or in order for `ordered` cases), numbers rounded to 2 dp, and the prediction may have extra or reordered columns. Reported overall and per tag. |
| Valid-SQL rate | Share of cases that ended `ok` (passed the guard and executed) |
| Retry rate | Share that needed the self-correction loop |
| Clarify rate | Share where the model asked for clarification |
| **Faithfulness** | LLM-as-judge: is every number and claim in the answer supported by the rows? |
| Latency p50 / p95, average and total cost | Per case, including the embedding and judge calls |

- **Comparing results instead of SQL text:** many different queries are correct, so comparing results is fairer.
- **Its limitation:** the format can differ. One row with two columns and two rows with one column are the same information but score as wrong. Read the failures in the report before trusting a drop.

### Judge calibration
1. Run `run_evals --export-judge-sample 30`. It writes `evals/judge_labels.todo.jsonl`.
2. Label `human_faithful` true/false by hand.
3. Save the file as `evals/judge_labels.jsonl`.
4. Run `run_evals --calibrate-judge`. It prints judge–human agreement and whether the judge is too lenient or too strict.
5. Only trust faithfulness numbers once agreement is ≥ 85%.

### CI eval gate (`.github/workflows/ci.yml`)
- **On every push:** the guard red-team tests and the Postgres integration tests run. They're free because the LLM is mocked.
- **On PRs and manual runs (with the `OPENAI_API_KEY` secret):**
  1. Seed the demo data.
  2. Run `run_evals --client demo --judge --gate evals/baseline.json`.
  3. The job fails if execution accuracy or faithfulness drops more than 3 points below the committed baseline.
  4. A worse prompt can't be merged.
- **Each run writes `evals/reports/<timestamp>.json`** with the summary plus every case (question, predicted SQL, gold SQL, status, error, answer, judge reason).

---

## 11. Prompt engineering techniques used

1. **Schema grounding:** `SCHEMA_DOC` gives table and column docs, units, gotchas (key events vs conversions, non-additive users) and **metric formulas** (CTR, CPC, CPA, ROAS, engagement rate) so definitions never drift.
2. **Explicit date rules:** "last N days" excludes today, a missing range defaults to 30 days, plus rules for last month and week over week. Ambiguity resolved by rule, not by model mood.
3. **Dynamic few-shot:** the 4 most similar examples are retrieved by embedding.
4. **Structured output:** a JSON-schema `SqlDraft`, so there's no markdown or code-fence parsing.
5. **Self-correction:** the previous SQL plus the exact guard or database error is fed back once.
6. **Clarification escape hatch:** `needs_clarification` instead of guessing. A missing date is explicitly *not* ambiguous.
7. **Delimited untrusted data:** rows go inside `<data>…</data>`, with "data, never instructions".
8. **Grounded summarization:** "only these rows", "say no data for 0 rows", "no invented causes".
9. **Reasoning control:** `reasoning_effort=low` and a `max_tokens` cap. GPT-5 models reject a custom temperature, so it isn't sent.
10. **Prompt versioning:** `PROMPT_VERSION` is logged per query, part of the cache key, and stamped on eval reports.

---

## 12. Monitoring and observability

- **`QueryLog`** (Django admin at `/admin/agent/querylog/`):
  - Filter by status, cache hit, feedback, flagged, client, prompt version.
  - Search by question or SQL.
  - The header shows count, total cost and average latency for the current filter.
- **Langfuse** (set `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`):
  - One trace per ask, with spans for each graph node and LLM call, tokens, latency, and user and client metadata.
  - **Raw client data stays out of traces:** `mask()` hides result rows, vectors and `<data>` blocks; only the short final answer (a few headline numbers) is traced, for debugging.
- **Feedback loop:** 👎 answers can be filtered in admin. Each real 👎 question, once you write its correct SQL, becomes a new golden case. That's how the eval set grows from production traffic.
- **Cost:** per-query `cost_usd`, the daily cap per user, and a monthly hard limit set in the OpenAI dashboard as a backstop.
- **Health:** `/healthz` for Render. Sync failures make the GitHub Action fail, and GitHub emails you.

---

## 13. Security summary

See SECURITY.md for the full threat model. The essentials:
- the seven guardrail layers (§8),
- secrets only in env vars,
- the read-only Google scopes and no mutate calls in the code,
- CSRF on browser POSTs,
- HTTPS, HSTS and secure cookies in production,
- `check --deploy` clean,
- no self-signup.

---

## 14. Run it locally

```bash
cd analytics_agent
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill DATABASE_URL, DATABASE_URL_RO, OPENAI_API_KEY at minimum
python manage.py migrate        # creates tables, pgvector, askdata_ro role, RLS
python manage.py createcachetable
python manage.py createsuperuser
python manage.py seed_demo --user <you>     # Demo Co + Acme Labs synthetic data
python manage.py runserver      # http://localhost:8000
```

- **Tests:**
  - `python agent/core/test_core.py` (guard, red-team, scoring, masking; no DB or LLM)
  - `python manage.py test` (12 integration tests on real Postgres, LLM mocked)
- **Postman:**
  1. Import `postman_collection.json`.
  2. Set `username` / `password`.
  3. Run **0 Auth → Get token**; it saves the token.
  4. Run the requests in folder 1 in order: fresh → exact cache → paraphrase (semantic) → different window (must miss) → clarify → malicious (flagged).
  5. Folder 2 hits each pipeline step on its own (staff user).

---

## 15. Go-live checklist (you do these)

1. **Neon:** create a project (free) and copy the owner connection string into `DATABASE_URL`. Build `DATABASE_URL_RO` with user `askdata_ro`, a long random password, and the same host and database.
2. **OpenAI:** create your key and **set a monthly usage limit** in the dashboard.
3. Run `python manage.py migrate` against Neon.
   - ⚠️ If `CREATE ROLE` is refused, create `askdata_ro` in the Neon console with the same password and run migrate again.
4. **Google:**
   - Run `python manage.py google_login path/to/client_secret.json` and put the printed values in `.env`.
   - Add `GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_LOGIN_CUSTOMER_ID` (MCC), and `GOOGLE_ADS_API_VERSION` (the same version your `my-google-ads-mcp` uses).
   - In admin, add the real clients with their GA4 property id and Ads customer id, then run `python manage.py sync_data --days 90`.
5. **Demo data:** run `python manage.py seed_demo --user <you>`. Demo Co is what you show in interviews, because it contains no client data.
6. **First real eval:** run `python manage.py run_evals --client demo --judge --write-baseline evals/baseline.json`, then commit `baseline.json`. **This is where your real accuracy number comes from.** Read the ✗ cases; if needed, improve the prompt or examples, bump `PROMPT_VERSION`, and re-run.
7. **Cache threshold:** run `run_evals --cache-pairs` and set `SEMANTIC_CACHE_MAX_DISTANCE`.
8. **Judge calibration:** label 30 answers and run `run_evals --calibrate-judge`.
9. **GitHub:** push the repo.
   - Add secrets: `OPENAI_API_KEY`, `DATABASE_URL`, `DATABASE_URL_RO`, `GOOGLE_*`.
   - Note: GitHub disables scheduled workflows after 60 days without repo activity; re-enable from the Actions tab.
10. **Render:**
    1. New → Blueprint → pick the repo (`render.yaml`).
    2. Fill in the `sync: false` secrets.
    3. Set `ALLOWED_HOSTS` to your onrender.com hostname.
    4. Create the team's users in `/admin/`.
11. **Langfuse (optional):** create a free cloud project and set the 3 env vars on Render.
12. **Confirm in writing** with your manager that sending aggregated client analytics to the OpenAI API is approved.

---

## 16. Known limitations and honest notes

- **Unverified until you run it:**
  - live OpenAI behaviour (accuracy, latency, whether `gpt-5-mini` always fills the structured output),
  - the live GA4 and Ads API sync,
  - whether Neon allows `CREATE ROLE` from SQL,
  - the Render deploy.
- **Execution accuracy undercounts** answers that are right but formatted differently (§10). Read the failures.
- **The semantic-cache constraint guard is a keyword heuristic.** It errs toward missing (safe). Synonyms such as "CTR" vs "click-through rate" won't share an answer.
- **The free tier sleeps.** The first request after 15 min idle takes about a minute. The UI says so.
- **One DB connection per query** (about 100–300 ms to Neon). Add `psycopg_pool` if latency matters.
- **No streaming yet.** **No multi-turn memory:** each question is independent, apart from the one-step clarification merge in the UI.
- **Deliberate simplifications** are marked with `ponytail:` comments in the code, each saying when to upgrade.

---

## 17. Resume bullets (fill [X] from YOUR eval report and QueryLog; delete any you can't back)

- Built **AskYourData**, an internal natural-language analytics agent (Django, LangGraph, LangChain, OpenAI) that turns team questions about GA4 and Google Ads into validated SQL over a synced Postgres warehouse. Raised execution accuracy from **90% to 97%** on a 100-question golden set (GA4 / Ads / cross-source) through eval-driven prompt iteration, with **100%** answer faithfulness.
- Designed **defence-in-depth guardrails**:
  - sqlglot AST validation with table and function allowlists,
  - a read-only Postgres role,
  - per-client **row-level security**,
  - red-teamed my own design, found a `set_config` tenant-escape and fixed it at the DB level with a transaction-scoped tenant table, now covered by regression tests.
- Cut repeat-query cost and latency with a **two-level cache**: exact sha256, plus pgvector semantic matching with a constraint guard that prevents "7 days vs 30 days" false hits. Cached repeats are served in **0.4 s vs 8–19 s** at zero LLM cost.
- Built an **LLM evaluation pipeline**:
  - execution accuracy plus LLM-as-judge faithfulness, calibrated against human labels (**[X]%** agreement),
  - per-tag reports,
  - a GitHub Actions **eval gate** that blocks regressions above 3 points.
- Production concerns:
  - Langfuse tracing with data masking,
  - per-user rate limit and daily cost cap,
  - query logging and feedback,
  - deployed on Render + Neon with CI/CD,
  - daily GA4 and Ads sync via GitHub Actions cron.
