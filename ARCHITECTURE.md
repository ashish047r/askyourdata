# ARCHITECTURE: AskYourData

**Related:** [PRD.md](PRD.md) · [SECURITY.md](SECURITY.md) · [FRONTEND.md](FRONTEND.md) · [TICKETS.md](TICKETS.md)

> **Implementation status (2026-10-03):** all phases are built. File and function names changed during the build (e.g. `guards.py`, `chains.py`, `graph.py`); **prj.md is the source of truth** where this doc differs.

---

## 1. System overview

```
                    ┌────────────────────── Render (free web service) ───────────────────────┐
 Browser (HTML/JS)  │  Django + DRF                                                          │
 Postman  ────────► │   auth (session/token) → throttle → services.ask()                     │
                    │        │                                                               │
                    │        ├─ cache_get ──────────────► Postgres: cache_entry              │
                    │        ├─ agent.core.pipeline.answer_question()                        │
                    │        │     select_examples ─► in-memory vector store (examples.jsonl)│
                    │        │     generate_sql ────► OpenAI gpt-5-mini (LangChain)          │
                    │        │     validate_sql ────► sqlglot (allowlists, LIMIT)            │
                    │        │     run_sql ─────────► Postgres as role askdata_ro + RLS      │
                    │        │     summarize ───────► OpenAI gpt-5-mini                      │
                    │        ├─ log ─────────────────► Postgres: query_log                   │
                    │        └─ cache_set                                                    │
                    └────────────────────────────────────────────────────────────────────────┘
                                                   │
                                     Neon Postgres (free, 1 GB)
                                     clients · ga4_daily · ads_campaign_daily · app tables
                                                   ▲
   sync_data (run locally Day 1; GitHub Actions cron later) ── GA4 Data API + Google Ads API
```

## 2. Request flow: `POST /api/ask/`

1. **Auth.** Session cookie (browser) or `Authorization: Token …` (Postman).
2. **Throttle.** DRF `UserRateThrottle` at 60/hour. A daily cost cap is checked from `query_log`.
3. **Access check.** The user must be assigned to `client_id` (superusers pass).
4. **Cache lookup.** The key is `sha256(normalize(question) | client_id | data_version | PROMPT_VERSION | model)`.
   - `data_version` is `client.last_synced_at`, so a new sync invalidates old answers automatically.
5. **Pipeline (`answer_question`).**
   1. `select_examples(question, k=4)`: the most similar Q→SQL examples (dynamic few-shot).
   2. `generate_sql(question, examples)`: structured output `{sql}`.
   3. `validate_sql(sql)`: returns safe SQL with LIMIT, or raises `SqlRejected`.
   4. `run_sql(safe_sql, client_id)`: read-only transaction with `statement_timeout`; RLS scopes rows to the client.
   5. **On `SqlRejected` or a DB error:** call `generate_sql(..., error=msg, previous_sql=sql)` once, then validate and run again.
   6. `summarize(question, sql, result)`: a 1–3 sentence answer from at most 50 rows.
6. **Log** to `query_log`: status, step timings, tokens, cost, retries.
7. **Cache write** (only on success).
8. **Return** `{query_id, answer, sql, columns, rows, meta}`.

## 3. Data model

**Analytics data** (written by `sync_data` as the owner role; read by `askdata_ro`)

```
clients
  id, name, slug (unique), ga4_property_id, ads_customer_id, ads_manager_id (nullable),
  currency_code, is_active, last_synced_at, users (M2M → auth_user)

ga4_daily                         UNIQUE(client_id, date, session_source, session_medium, device_category)
  client_id, date, session_source, session_medium, device_category,
  sessions, total_users, new_users, engaged_sessions, key_events, total_revenue

ads_campaign_daily                UNIQUE(client_id, date, campaign_id)
  client_id, date, campaign_id, campaign_name, campaign_status, channel_type,
  impressions, clicks, cost (currency units = cost_micros / 1e6), conversions, conversions_value
```

- GA4 renamed "conversions" to **key events**, so the GA4 column is `key_events`. The Ads column stays `conversions`. The schema doc tells the LLM the difference.

**App tables**

```
query_log
  id, user, client, question, sql, status (ok|rejected|error|cached|throttled),
  error, retries, latency_ms_total, timings (JSON per step), prompt_tokens,
  completion_tokens, cost_usd, cache_hit, model, prompt_version, created_at

cache_entry
  key (PK, sha256), client, response (JSON), hit_count, created_at
```

- **Golden set and few-shot examples are files, not tables.** They are versioned in git and diffable.
  - `agent/data/examples.jsonl`: few-shot examples used in prompts.
  - `evals/golden.jsonl`: the test set. It **must not overlap** with the examples (no leakage).

## 4. Project layout (as built)

```
analytics_agent/
├── manage.py · requirements.txt · .env.example · .python-version · render.yaml · postman_collection.json
├── prj.md                        # end-to-end guide, source of truth
├── config/                       # settings.py, urls.py, wsgi.py
├── data/                         # analytics data + sync
│   ├── models.py                 # Client, Ga4Daily, AdsCampaignDaily
│   ├── google_api.py             # get_credentials, fetch_ga4_daily, fetch_ads_campaign_daily
│   ├── sync.py                   # upsert_rows / upsert_ga4 / upsert_ads
│   ├── migrations/0002_readonly_role_rls.py   # askdata_ro, app_tenant_id(), RLS policies
│   └── management/commands/      # sync_data, google_login, seed_demo
├── agent/
│   ├── core/                     # PURE PYTHON, no Django imports
│   │   ├── prompts.py            # SCHEMA_DOC, DATE_RULES, all prompts, PROMPT_VERSION
│   │   ├── llm.py                # chat_model, embed, PRICES/usage, add_usage, Langfuse callbacks + mask
│   │   ├── guards.py             # validate_sql, looks_like_injection, same_constraints
│   │   ├── executor.py           # run_sql (read-only role + tenant_scope)
│   │   ├── chains.py             # select_examples, generate_sql, summarize, judge_answer
│   │   ├── graph.py              # LangGraph state machine + answer_question
│   │   ├── evals.py              # compare_results, percentile
│   │   └── test_core.py          # guard red-team, scoring, masking tests
│   ├── data/examples.jsonl       # 24 few-shot examples
│   ├── models.py                 # QueryLog, CacheEntry (pgvector)
│   ├── cache.py                  # exact + semantic cache
│   ├── services.py               # ask()
│   ├── views.py · admin.py · tests.py
│   └── management/commands/run_evals.py
├── evals/                        # golden.jsonl (100), cache_pairs.jsonl, baseline.json (after first run), reports/
├── templates/ · static/          # login.html, app.html, app.css, app.js (Chart.js)
└── .github/workflows/            # ci.yml (tests + eval gate), sync.yml (daily cron)
```

## 5. API surface

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/auth/token/` | none | DRF `obtain_auth_token` (Postman login) |
| GET | `/api/clients/` | user | Clients assigned to me |
| POST | `/api/ask/` | user | Full pipeline |
| GET | `/api/history/?client_id=` | user | My last 20 questions |
| POST | `/api/debug/examples/` | staff | `select_examples` |
| POST | `/api/debug/generate-sql/` | staff | `generate_sql` |
| POST | `/api/debug/validate-sql/` | staff | `validate_sql` |
| POST | `/api/debug/run-sql/` | staff | `validate_sql` then `run_sql` (always validated) |
| POST | `/api/debug/summarize/` | staff | `summarize` |
| GET | `/healthz` | none | Liveness |
| GET | `/` , `/login/` | session | Frontend pages |

## 6. LLM design

| Call | Model | Settings | Why |
|---|---|---|---|
| `generate_sql` | `gpt-5-mini` | `reasoning_effort="low"`, structured output `SqlDraft{sql: str}`, `max_tokens` cap | Structured output removes markdown/fence parsing. Low effort keeps latency down. |
| `summarize` | `gpt-5-mini` | Short instruction, rows as compact JSON, "use only these rows" | Grounded answer; easy to judge for faithfulness later |
| Example retrieval | `text-embedding-3-small` | `InMemoryVectorStore` + `SemanticSimilarityExampleSelector` | Dynamic few-shot beats static examples for text-to-SQL; no vector DB needed for ~40 examples |

- **Prompt contents** (SQL generation):
  - Role and rules: Postgres dialect, one SELECT, never filter by client (RLS does it), dates relative to `CURRENT_DATE`.
  - `SCHEMA_DOC`: table descriptions, column meanings, metric formulas (CTR, CPC, CPA, ROAS, engagement rate).
  - The selected examples.
  - The question.
  - On retry: the previous SQL plus the error message.
- **Versioning:** `PROMPT_VERSION` is a constant in `sql_gen.py`, logged with every query and included in the cache key. Changing the prompt invalidates the cache and tags the eval reports.

## 7. Evaluation architecture

```
evals/golden.jsonl ─► run_evals ─► for each case:
                                     answer_question(q, client)  (same code as prod)
                                     run_sql(gold_sql, client)
                                     compare_results(gold, pred)
                                 ─► report: exec accuracy (overall + per tag), valid-SQL rate,
                                    retry rate, p50/p95 latency, avg cost  → stdout + evals/reports/*.json
```

- **Execution accuracy:**
  - Both the gold and the predicted SQL run on the same live data.
  - Rows are compared as multisets after rounding numbers to 2 decimals.
  - The predicted result may have extra columns, as long as every gold column is present.
  - Order matters only for cases tagged `ordered`.
- **No leakage:** a check fails the run if any golden question text appears in `examples.jsonl`.
- **Red-team tests** (`test_core.py`): no LLM call; they test the guard directly and must pass 100%.

## 8. Deployment

| Piece | Where | Notes |
|---|---|---|
| Web | Render free web service | Auto-deploy on push to `main`; `gunicorn config.wsgi --timeout 60`; sleeps after 15 min idle |
| DB | Neon free | 1 GB/project, scales to zero after 5 min (cold query +~1 s). Two DSNs: owner (Django) and `askdata_ro` (LLM SQL). |
| Static | WhiteNoise | No CDN or bucket needed |
| Secrets | Render env vars / local `.env` | See below |
| Data sync | Day 1: run `sync_data` locally against Neon. Later: GitHub Actions cron. | Render free has no cron jobs |

- **Env vars:**
  - Django: `DJANGO_SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`
  - Database: `DATABASE_URL` (owner), `DATABASE_URL_RO` (read-only)
  - OpenAI: `OPENAI_API_KEY`, `OPENAI_MODEL`
  - Google: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_LOGIN_CUSTOMER_ID`
  - Limits: `DAILY_COST_CAP_USD`

## 9. Key design decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| Sync to Postgres, not live API calls | One SQL dialect, joins across GA4 and Ads, fast, testable, evals reproducible | LLM writes GAQL / GA4 API calls directly: two query languages, no joins, slow, quota-bound |
| Tenant isolation in Postgres RLS | Even a malicious or hallucinated query cannot read another client's rows | "Add `WHERE client_id = X`" in the prompt: one missed filter = data leak |
| Separate read-only role + DSN | LLM SQL physically cannot write | Same Django connection with a regex check |
| `sqlglot` AST validation | Real parsing; allowlists for tables and functions | Regex `startswith("SELECT")`: trivially bypassed |
| Cache in Postgres table | No extra service (Redis) for Day 1 | Redis: another free tier to manage; add only if needed |
| Examples and golden set as JSONL | Git history = dataset versioning | DB tables: harder to review in PRs |
| Django + DRF | Known stack; built-in auth, admin, throttling | FastAPI: would mean learning it alongside evals |
| One retry max | Bounds cost and latency; most fixes land on the first retry | Unlimited loop: cost runaway |

## 10. Path to LangGraph (later)

| Pipeline function | Future graph node |
|---|---|
| `select_examples` | `retrieve_examples` |
| `generate_sql` | `generate_sql` |
| `validate_sql` + `run_sql` | `execute` with a conditional edge to `generate_sql` on error (the retry becomes a graph loop with max_attempts) |
| `summarize` | `answer` |
| Not built yet | `clarify` (human-in-the-loop when the question is ambiguous) |
