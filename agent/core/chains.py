"""The three LLM calls (SQL writer, summarizer, judge) and few-shot example selection."""

import json
from functools import lru_cache
from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.vectorstores import InMemoryVectorStore
from pydantic import BaseModel, Field

from . import prompts
from .llm import chat_model, embeddings, usage_from

EXAMPLES_PATH = Path(__file__).resolve().parent.parent / "data" / "examples.jsonl"
SUMMARY_ROWS = 50  # never send more than this to the LLM


def load_jsonl(path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


# ---------- dynamic few-shot ----------

@lru_cache
def _example_store() -> InMemoryVectorStore:
    examples = load_jsonl(EXAMPLES_PATH)
    store = InMemoryVectorStore(embeddings())
    store.add_texts([e["question"] for e in examples], metadatas=examples)  # 1 embeddings call per process
    return store


def select_examples(question_vector: list[float], k: int = 4) -> list[dict]:
    return [d.metadata for d in _example_store().similarity_search_by_vector(question_vector, k=k)]


# ---------- SQL writer ----------

class SqlDraft(BaseModel):
    needs_clarification: bool = Field(description="true only if the question can't be answered or is ambiguous")
    clarifying_question: str = Field(description="one short question for the user, else empty")
    sql: str = Field(description="the PostgreSQL query, else empty")


_SQL_PROMPT = ChatPromptTemplate.from_messages([("system", prompts.SQL_SYSTEM), ("human", prompts.SQL_USER)])


def generate_sql(question: str, examples: list[dict], previous_sql: str = "", error: str = "") -> tuple[SqlDraft, dict]:
    """First attempt and self-correcting retry share this one function (retry adds previous SQL + error)."""
    retry = prompts.SQL_RETRY.format(previous_sql=previous_sql, error=error) if error else ""
    shots = "\n\n".join(f"Q: {e['question']}\nSQL: {e['sql']}" for e in examples)
    chain = _SQL_PROMPT | chat_model(4000).with_structured_output(SqlDraft, include_raw=True)
    out = chain.invoke({"examples": shots, "question": question, "retry": retry})
    if out["parsed"] is None:
        raise ValueError(f"SQL writer returned unparseable output: {out.get('parsing_error')}")
    return out["parsed"], usage_from(out["raw"])


# ---------- summarizer ----------

def _data_block(columns, rows, truncated=False) -> str:
    return json.dumps({"columns": columns, "rows": rows[:SUMMARY_ROWS], "row_count": len(rows),
                       "truncated": truncated or len(rows) > SUMMARY_ROWS}, default=str)


_SUMMARY_PROMPT = ChatPromptTemplate.from_messages([("system", prompts.SUMMARY_SYSTEM), ("human", prompts.SUMMARY_USER)])


def summarize(question: str, sql: str, columns: list, rows: list, truncated: bool = False) -> tuple[str, dict]:
    msg = (_SUMMARY_PROMPT | chat_model(2000)).invoke(
        {"question": question, "sql": sql, "data": _data_block(columns, rows, truncated)})
    return msg.text.strip(), usage_from(msg)


# ---------- LLM-as-judge (evals only) ----------

class Verdict(BaseModel):
    faithful: bool
    reason: str


_JUDGE_PROMPT = ChatPromptTemplate.from_messages([("system", prompts.JUDGE_SYSTEM), ("human", prompts.JUDGE_USER)])


def judge_answer(question: str, columns: list, rows: list, answer: str) -> tuple[Verdict, dict]:
    chain = _JUDGE_PROMPT | chat_model(2000).with_structured_output(Verdict, include_raw=True)
    out = chain.invoke({"question": question, "data": _data_block(columns, rows), "answer": answer})
    return out["parsed"] or Verdict(faithful=False, reason="judge output unparseable"), usage_from(out["raw"])
