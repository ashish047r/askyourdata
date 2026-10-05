# PRD: AskYourData

**Owner:** Ashish Raj (EverClif) · **Status:** Draft v1 · **Last updated:** 2026-10-03
**Related:** [ARCHITECTURE.md](ARCHITECTURE.md) · [SECURITY.md](SECURITY.md) · [FRONTEND.md](FRONTEND.md) · [TICKETS.md](TICKETS.md)

> **Implementation status (2026-10-03):** all phases are built. File and function names changed during the build (e.g. `guards.py`, `chains.py`, `graph.py`); **prj.md is the source of truth** where this doc differs.

---

## 1. Problem

- Today, answering a client question like "Which campaigns drove the most conversions last month?" means:
  - opening GA4 or Google Ads, or writing GAQL by hand
  - exporting to a sheet
  - doing the joins and maths manually
- That takes 10–30 minutes per question and needs someone who knows the APIs.
- The MCP servers (Google Ads, GA4) help inside Claude Desktop, but they are per-person, have no shared history, no evaluation and no access control.

## 2. Goal

An internal web tool where an EverClif team member:
1. Picks a client.
2. Asks a question in plain English.
3. Gets back a correct answer, the result table, and the SQL that produced it, in under 15 seconds.

## 3. Users

| User | Access | Needs |
|---|---|---|
| EverClif team member | Only the clients assigned to them | Fast, trustworthy answers; can see the SQL to verify |
| Admin (Ashish) | All clients + Django admin | Manage users and client access, run data sync, view logs and eval reports |

- **Not users in v1:** clients themselves. This is internal-only.

## 4. Example questions (v1 must handle)

- "Total sessions and key events last 30 days by device."
- "Top 5 campaigns by conversions last month, with cost per conversion."
- "Which source/medium had the biggest drop in sessions week over week?"
- "How much did we spend on Search vs Performance Max in September?"
- "Compare paid clicks (Ads) with google/cpc sessions (GA4) per day last 2 weeks."

## 5. Scope

**In scope (Day-1 MVP)**
- Login (session for the browser, token for Postman)
- Client picker limited to the user's assigned clients
- Text-to-SQL over two tables: `ga4_daily`, `ads_campaign_daily`
- SQL safety guard plus a read-only DB role plus row-level security per client
- One automatic self-correction retry when SQL is rejected or fails
- Plain-English summary of the result
- Exact-match cache
- Query log with latency, tokens and cost
- Per-user rate limit and daily cost cap
- Golden-set evaluation (30 questions) with an execution-accuracy report
- A Postman-testable endpoint for every pipeline step
- Deployed on Render (free) with Neon Postgres (free)

**Hardening (weeks 2–3)**
- Langfuse tracing
- Semantic cache (pgvector)
- 100+ golden questions, LLM-as-judge for answer faithfulness, CI eval gate
- Scheduled data sync (GitHub Actions cron)
- Charts
- Feedback (thumbs up/down)
- Streaming answers

**Out of scope (v1)**
- Any write action (pausing campaigns, editing budgets)
- GSC and Meta Ads data
- Client-facing logins
- Multi-turn chat memory (each question is independent)
- LangGraph. This comes after learning it; the pipeline is designed so each step maps to a graph node.

## 6. Functional requirements

| ID | Requirement |
|---|---|
| FR-1 | A user can log in and only sees clients assigned to them. Superusers see all. |
| FR-2 | `POST /api/ask/` with `{client_id, question}` returns `{answer, sql, columns, rows, meta}`. |
| FR-3 | Generated SQL is validated before execution: one SELECT only, allowed tables and functions only, LIMIT enforced. |
| FR-4 | SQL runs on a read-only connection that can only see the selected client's rows (enforced by Postgres, not the prompt). |
| FR-5 | If SQL is rejected or errors, the system retries once with the error message. If that fails too, it returns a clear error with no made-up answer. |
| FR-6 | The answer is generated only from the returned rows. If there are 0 rows, it says so. |
| FR-7 | An identical question for the same client and the same data version returns the cached answer. |
| FR-8 | Every request is logged: user, client, question, SQL, status, error, latency per step, tokens, cost, cache hit, retries, model, prompt version. |
| FR-9 | Users are limited to 60 questions/hour and US$0.50/day of LLM spend. |
| FR-10 | `python manage.py sync_data` pulls the last N days of GA4 and Ads data per client and upserts it. |
| FR-11 | `python manage.py run_evals` runs the golden set and prints and saves an accuracy report. |
| FR-12 | Every pipeline step can be called alone via a staff-only `/api/debug/...` endpoint (for Postman). |

## 7. Non-functional requirements

| Area | Target |
|---|---|
| Latency | p95 ≤ 15 s uncached, ≤ 500 ms cached (warm server) |
| Cost | ≤ US$0.01 per uncached question (expected ~US$0.003 on gpt-5-mini) |
| Accuracy | Execution accuracy ≥ 80% on the golden set (Day-1 target; raise later) |
| Safety | 100% of red-team SQL attacks blocked (unit tests) |
| Hosting cost | ₹0 (Render free + Neon free). Only OpenAI usage is paid. |
| Cold start | Render free sleeps after 15 min idle, so the first request takes ~1 min. Acceptable; open the link before demos. |

## 8. Success metrics

- **Quality:** execution accuracy on the golden set, tracked per run and broken down by tag (ga4 / ads / cross-source).
- **Usage:** questions per week and active users (from QueryLog).
- **Efficiency:** median latency, cache-hit rate, cost per question.
- **Time saved:** baseline the manual time for 5 typical questions vs. the tool.

## 9. Constraints & assumptions

- **Hosting:** Render free web service + Neon free Postgres. Neon gives 1 GB/project and 100 CU-hours/month, and scales to zero after 5 min idle.

- **Budget:** Ashish pays OpenAI usage; hosting must be free.
- **Approval:** the manager approved the tool. **Open item:** confirm in writing that sending aggregated client analytics to the OpenAI API is approved.
- **Data:** OAuth via the existing refresh-token approach from the Google Ads / GA4 MCP servers. Ads needs the developer token.
- **Model:** `gpt-5-mini`, configurable via env. ⚠️ Unverified: GPT-5-family models may reject a custom `temperature`. We use `reasoning_effort="low"` plus structured output instead. Check this on the first call.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Wrong-but-plausible numbers | SQL is always shown; eval set; answers only from rows; retry limited to 1 |
| Cross-client data leak | Postgres RLS + read-only role + function allowlist (see SECURITY.md) |
| Cost runaway (own money) | Rate limit, daily cost cap, cache, mini model, `max_tokens` |
| GA4/Ads API quota or auth expiry | Sync is a separate command and failures don't affect the Q&A path; refresh-token rotation documented |
| Free-tier sleep / limits | Documented; Neon has 1 GB storage and 100 CU-hours/month, far above needs |
| Resume claims ahead of reality | Only list shipped items. Add hardening items as each one ships. |
