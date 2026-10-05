# TICKETS: AskYourData

**Related:** [PRD.md](PRD.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [SECURITY.md](SECURITY.md) · [FRONTEND.md](FRONTEND.md)

- **Workflow:**
  - Build one function at a time, in order.
  - After each function: what it does, why it's built this way, prompt choices, how to verify (Postman / assert), and where it's reused.
  - Don't move on until it's confirmed.
- **Day 1:** T01–T14 (about 10–12 focused hours). **Hardening:** H01–H09.
- **Status:** T01–T14 and H01–H07 and H09 are built. H08 (streaming) was deliberately skipped. What remains is live verification (prj.md §15).
- **Name changes from this plan:**
  - `sql_guard.py` → `guards.py`
  - `schema.py` → `prompts.py`
  - `examples.py`, `sql_gen.py`, `summarize.py` → `chains.py`
  - `cost.py`, `llm.py` → `llm.py`
  - `pipeline.py` → `graph.py` (LangGraph)
  - the RLS tenant moved from `set_config` to a temp table (SECURITY §2.3)

> **Implementation status (2026-10-03):** all phases are built. File and function names changed during the build (e.g. `guards.py`, `chains.py`, `graph.py`); **prj.md is the source of truth** where this doc differs.

---

## Day 1

### T01: Project setup
- **Steps:**
  1. Create `venv`.
  2. `requirements.txt`:
     - web: django, djangorestframework, psycopg[binary], dj-database-url, whitenoise, gunicorn, python-dotenv
     - AI: langchain-openai, langchain-core, sqlglot
     - Google: google-analytics-data, google-auth, requests
  3. Create `config/` and the apps `data` and `agent`.
  4. Add `.env.example` and `.gitignore` (`.env`, `*_token.json`, `client_secrets.json`, `evals/reports/`).
  5. Create a Neon project (free) and set `DATABASE_URL`.
  6. Settings: DRF with Session + Token auth, throttle `user: 60/hour`, WhiteNoise, security settings driven by `DEBUG`.
  7. `/healthz` view.
- **Done when:** `python manage.py migrate` runs against Neon, and `GET /healthz` in Postman returns 200.

### T02: Data models + read-only role + RLS
- **Steps:**
  1. Models: `Client` (with `users` M2M, `last_synced_at`), `Ga4Daily`, `AdsCampaignDaily` with the UNIQUE constraints from ARCHITECTURE §3.
  2. A migration with `RunSQL`: GRANTs + RLS policies (SECURITY §2.3). Create `askdata_ro` in the Neon console if `CREATE ROLE` fails.
  3. Register the models in admin.
- **Done when:**
  - In admin you can create a client and assign a user.
  - The manual RLS check (SECURITY §3) returns only the selected `client_id`.

### T03: Google auth + GA4 fetch
| Function | Does | In → Out | Calls |
|---|---|---|---|
| `get_google_credentials()` | Builds OAuth credentials from the env refresh token (reuses the MCP approach) | env → `google.oauth2.credentials.Credentials` | none |
| `fetch_ga4_daily(property_id, start, end)` | GA4 Data API `runReport`: dims date/sessionSource/sessionMedium/deviceCategory; metrics sessions, totalUsers, newUsers, engagedSessions, keyEvents, totalRevenue; paginates | ids + dates → `list[dict]` | `get_google_credentials` |
- **Done when:** a test script prints the row count and the first row for one real property over 7 days.

### T04: Ads fetch
| Function | Does | In → Out | Calls |
|---|---|---|---|
| `fetch_ads_campaign_daily(customer_id, manager_id, start, end)` | GAQL on `campaign` with `segments.date`, impressions, clicks, cost_micros→cost, conversions, conversions_value, via REST `searchStream` (same API version as `my-google-ads-mcp`) | ids + dates → `list[dict]` | `get_google_credentials` |
- **Done when:** the row count and total cost match the Google Ads UI for the same 7 days (±rounding).

### T05: Upsert + `sync_data` command
| Function | Does | In → Out | Calls |
|---|---|---|---|
| `upsert_rows(model, client, rows)` | `bulk_create(update_conflicts=True, unique_fields=…, update_fields=…)` | rows → count | none |
| `sync_data --client <slug> --days 90` | For each active client (or one): fetch GA4 + Ads, upsert, set `last_synced_at` | CLI → summary print | T03, T04, `upsert_rows` |
- **Done when:**
  - Running it twice gives the same row counts (idempotent).
  - The admin shows the data.

### T06: `llm.py` + `schema.py` + examples file
| Item | Does | In → Out | Calls |
|---|---|---|---|
| `get_chat_model()` | Cached `ChatOpenAI(model=OPENAI_MODEL, reasoning_effort="low", max_tokens=…)` | none → model | none |
| `get_embeddings()` | Cached `OpenAIEmbeddings("text-embedding-3-small")` | none → embeddings | none |
| `SCHEMA_DOC` | Table/column docs + metric formulas (CTR, CPC, CPA, ROAS, engagement rate) + key_events vs conversions note | constant | none |
| `agent/data/examples.jsonl` | ~25 Q→SQL pairs covering GA4, Ads, cross-source, week-over-week, top-N | file | none |
- **Done when:**
  - A one-line call `get_chat_model().invoke("ping")` works.
  - ⚠️ Confirm here that `gpt-5-mini` accepts `reasoning_effort` (unverified).
  - Every example's SQL runs successfully via `run_sql` once T09 exists.

### T07: `select_examples`
| Function | Does | In → Out | Calls |
|---|---|---|---|
| `select_examples(question, k=4)` | `SemanticSimilarityExampleSelector` over an `InMemoryVectorStore` built once from examples.jsonl | str → `list[{question, sql}]` | `get_embeddings` |
- **Postman:** `POST /api/debug/examples/ {"question":"top campaigns by spend"}` returns 4 Ads-related examples.

### T08: `generate_sql`
| Function | Does | In → Out | Calls |
|---|---|---|---|
| `generate_sql(question, examples, error=None, previous_sql=None)` | Prompt (rules + SCHEMA_DOC + examples + question [+ previous SQL + error on retry]) → structured output `SqlDraft{sql}`; returns usage | → `(sql: str, usage: dict)` | `get_chat_model` |
- One function handles both the first attempt and the retry, so there's no duplicated prompt logic.
- **Postman:** `POST /api/debug/generate-sql/ {"question":"sessions by device last 30 days"}` returns SQL with `GROUP BY device_category` and a `CURRENT_DATE - 30` filter, and no `client_id` filter.

### T09: `validate_sql` + `run_sql` + guard tests
| Function | Does | In → Out | Calls |
|---|---|---|---|
| `validate_sql(sql)` | sqlglot parse → single SELECT, table + function allowlists, no DML/DDL, enforce LIMIT ≤ 1000 | str → safe str / raises `SqlRejected(reason)` | none |
| `run_sql(sql, client_id)` | Connect as `askdata_ro`; READ ONLY txn; `statement_timeout 5s`; `set_config('app.client_id')`; execute; rollback | → `{columns, rows, truncated}` | none |
| `test_core.py` | All rows from the SECURITY §3 table as asserts | `python agent/core/test_core.py` | `validate_sql` |
- **Postman:**
  - `POST /api/debug/validate-sql/ {"sql":"DROP TABLE ga4_daily"}` returns 422 with the reason.
  - `POST /api/debug/run-sql/ {"client_id":1,"sql":"SELECT DISTINCT client_id FROM ga4_daily"}` returns only `[[1]]`.

### T10: `summarize` + `estimate_cost`
| Function | Does | In → Out | Calls |
|---|---|---|---|
| `summarize(question, sql, result)` | 1–3 sentence answer from ≤ 50 rows (JSON in a delimited data block, "use only these rows", "say no data for 0 rows") | → `(text, usage)` | `get_chat_model` |
| `estimate_cost(usage, model)` | Tokens × price table in settings | dict → float USD | none |
- **Postman:** `POST /api/debug/summarize/` with `rows: []` returns an answer saying no data was found.

### T11: `answer_question` pipeline
| Function | Does | In → Out | Calls |
|---|---|---|---|
| `answer_question(question, client_id)` | examples → generate → validate → run; on `SqlRejected`/DB error retry once with the error; summarize; collect per-step timings, total usage, cost | → `AnswerResult{status, answer, sql, columns, rows, retries, timings, usage, cost_usd, error}` | T07–T10 |
- **Done when:** 5 PRD §4 questions return sensible answers from a Python shell.

### T12: Cache + service + API + logging
| Function | Does | In → Out | Calls |
|---|---|---|---|
| `cache_key(question, client_id, data_version)` | sha256 of normalized question + ids + PROMPT_VERSION + model | → str | none |
| `cache_get(key)` / `cache_set(key, client, payload)` | `CacheEntry` read (increments hit_count) / write | → dict \| None | none |
| `ask(user, client_id, question)` | Access check → daily cost cap → cache → `answer_question` → `QueryLog` → cache_set | → response dict + HTTP status | `cache_*`, `answer_question` |
| Views | `ClientsView`, `AskView`, `HistoryView`, debug views (thin wrappers over core functions), `obtain_auth_token` route | HTTP | `ask` / core functions |
- **Postman collection** (saved in the repo as `postman_collection.json`):
  1. Get a token.
  2. List clients.
  3. Ask (cold): `cache_hit: false`.
  4. Ask again: `cache_hit: true`, under 500 ms.
  5. Ask for an unassigned client: 403.
  6. View history.

### T13: Golden set + `run_evals`
| Function | Does | In → Out | Calls |
|---|---|---|---|
| `evals/golden.jsonl` | 30 cases `{id, client_slug, question, gold_sql, tags}`, with **no overlap** with examples.jsonl | file | none |
| `compare_results(gold, pred, ordered=False)` | Multiset compare, rounded to 2 dp, pred may have extra columns | → bool | none |
| `evaluate_case(case)` | `answer_question` + `run_sql(gold_sql)` + compare | → result dict | T11, `run_sql`, `compare_results` |
| `run_evals` command | Leakage check, run all cases, print the report (exec accuracy overall + per tag, valid-SQL %, retry %, p50/p95 latency, avg cost), save JSON | CLI | `evaluate_case` |
- **Done when:** the report is printed. Record the baseline accuracy in the README.

### T14: Frontend + deploy
- **Frontend:** `login.html`, `app.html`, `app.css`, `app.js` per FRONTEND.md.
- **Deploy:**
  1. Push to GitHub.
  2. Create a Render web service (build: `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate`; start: `gunicorn config.wsgi --timeout 60`).
  3. Set the env vars.
  4. `python manage.py check --deploy` must be clean.
  5. Set the OpenAI monthly usage limit.
- **Done when:** the live URL works end to end, and a question from the browser shows the answer, SQL and table.

---

## Hardening (after Day 1, one per evening)

| ID | Ticket | Done when |
|---|---|---|
| H01 | Langfuse tracing via the LangChain callback; rows masked | A trace shows the steps, tokens and cost per request |
| H02 | Golden set → 100+ cases from real team questions in `query_log` | The report shows per-tag accuracy with ≥ 15 cases per tag |
| H03 | LLM-as-judge for answer faithfulness, calibrated vs 30 human labels | Judge–human agreement reported (target ≥ 85%) |
| H04 | GitHub Actions: guard tests + evals on a frozen Neon branch snapshot; fail if accuracy drops > 3 pts | A PR with a worse prompt is blocked |
| H05 | Scheduled `sync_data` via GitHub Actions cron (daily) | `last_synced_at` updates daily without manual runs |
| H06 | Semantic cache (pgvector), per-client namespace, threshold tuned on the eval set | Paraphrase hit rate measured; 0 wrong-answer hits on the eval set |
| H07 | Charts (Chart.js) + 👍/👎 feedback endpoint | Feedback rows visible in admin; 👎 cases feed the golden set |
| H08 | Streaming summary | First token in under 2 s after the SQL result |
| H09 | LangGraph rewrite of the pipeline (retry as a graph loop + `clarify` node) | Same eval accuracy or better; graph diagram in the README |
