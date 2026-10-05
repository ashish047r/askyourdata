# AskYourData

Ask GA4 and Google Ads questions in plain English. The answer comes from SQL that Postgres computes, not from an LLM doing arithmetic. Each answer comes with the SQL, a table and a chart.

Built with Django, LangGraph, LangChain and OpenAI. Data lives in Postgres with pgvector and row-level security. Deployed on Render + Neon, with GitHub Actions for CI and daily sync.

- **Agent:** a LangGraph loop (retrieve examples → write SQL → guard → execute → answer). It self-corrects once and asks for clarification when a question is ambiguous.
- **Safety:** an AST SQL guard with allowlists, a read-only database role, and per-client row-level security. Attacks are tested at the database level.
- **Cache:** exact match plus a pgvector semantic cache, with a constraint guard so "last 7 days" never reuses the answer for "last 30 days".
- **Evals:** a 100-question golden set scored on execution accuracy, an LLM-as-judge faithfulness check with human calibration, and a CI eval gate.
- **Ops:** Langfuse tracing with data masking, a query log, a per-user rate limit and cost cap, and user feedback.

Full guide: **[prj.md](prj.md)**. Quick start:

```bash
pip install -r requirements.txt && cp .env.example .env   # fill DB + OpenAI vars
python manage.py migrate && python manage.py createcachetable
python manage.py createsuperuser && python manage.py seed_demo --user <you>
python manage.py runserver
python agent/core/test_core.py && python manage.py test     # tests
python manage.py run_evals --client demo --judge            # real eval (uses OpenAI)
```
