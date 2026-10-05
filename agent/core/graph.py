"""The agent loop as a LangGraph state machine.

    START -> retrieve_examples -> write_sql --(clarify)--> END
                                     |  ^
                                     v  | (rejected/DB error, attempts < MAX_ATTEMPTS)
                                  check_sql -> execute -> answer -> END
                                     |            |
                                     +---> fail <-+ (attempts exhausted) -> END
"""

import time
from typing import Annotated, TypedDict

import psycopg
from langgraph.graph import END, START, StateGraph

from .chains import generate_sql, select_examples, summarize
from .executor import run_sql
from .guards import SqlRejected, validate_sql
from .llm import add_usage

MAX_ATTEMPTS = 2  # first try + one self-correction


class State(TypedDict, total=False):
    question: str
    client_id: int
    question_vector: list[float]
    examples: list[dict]
    sql: str
    safe_sql: str
    attempts: int
    error: str
    columns: list
    rows: list
    truncated: bool
    status: str  # ok | clarify | rejected | error
    answer: str
    clarifying_question: str
    usage: Annotated[dict, add_usage]  # reducer: summed across nodes
    timings: Annotated[dict, add_usage]  # ms per node, summed if a node runs twice


def timed(fn):
    def node(state: State) -> dict:
        t0 = time.perf_counter()
        update = fn(state)
        update["timings"] = {fn.__name__: round((time.perf_counter() - t0) * 1000)}
        return update

    node.__name__ = fn.__name__
    return node


@timed
def retrieve_examples(s: State) -> dict:
    return {"examples": select_examples(s["question_vector"])}


@timed
def write_sql(s: State) -> dict:
    draft, usage = generate_sql(s["question"], s["examples"], s.get("sql", ""), s.get("error", ""))
    if draft.needs_clarification:
        return {"status": "clarify", "clarifying_question": draft.clarifying_question, "usage": usage}
    return {"sql": draft.sql, "safe_sql": "", "attempts": s.get("attempts", 0) + 1, "error": "", "usage": usage}


@timed
def check_sql(s: State) -> dict:
    try:
        return {"safe_sql": validate_sql(s["sql"])}
    except SqlRejected as e:
        return {"error": f"rejected by SQL guard: {e}"}


@timed
def execute(s: State) -> dict:
    try:
        return run_sql(s["safe_sql"], s["client_id"])
    except psycopg.Error as e:
        return {"error": f"database error: {str(e).strip().splitlines()[0]}"}


@timed
def answer(s: State) -> dict:
    text, usage = summarize(s["question"], s["safe_sql"], s["columns"], s["rows"], s.get("truncated", False))
    return {"answer": text, "status": "ok", "usage": usage}


def fail(s: State) -> dict:
    return {"status": "rejected" if s["error"].startswith("rejected") else "error"}


def after_write(s: State) -> str:
    return END if s.get("status") == "clarify" else "check_sql"


def after_check(next_node: str):
    def route(s: State) -> str:
        if not s.get("error"):
            return next_node
        return "write_sql" if s.get("attempts", 0) < MAX_ATTEMPTS else "fail"

    return route


def build_graph():
    g = StateGraph(State)
    for node in (retrieve_examples, write_sql, check_sql, execute, answer, fail):
        g.add_node(node.__name__, node)
    g.add_edge(START, "retrieve_examples")
    g.add_edge("retrieve_examples", "write_sql")
    g.add_conditional_edges("write_sql", after_write, ["check_sql", END])
    g.add_conditional_edges("check_sql", after_check("execute"), ["execute", "write_sql", "fail"])
    g.add_conditional_edges("execute", after_check("answer"), ["answer", "write_sql", "fail"])
    g.add_edge("answer", END)
    g.add_edge("fail", END)
    return g.compile()


GRAPH = build_graph()


def answer_question(question: str, client_id: int, question_vector: list[float],
                    callbacks: list | None = None, metadata: dict | None = None) -> dict:
    """Single entry point used by the API, the debug endpoints' callers and the eval runner."""
    s = GRAPH.invoke(
        {"question": question, "client_id": client_id, "question_vector": question_vector, "usage": {}, "timings": {}},
        config={"callbacks": callbacks or [], "metadata": metadata or {}, "run_name": "ask"},
    )
    return {
        "status": s.get("status", "error"),
        "answer": s.get("answer", ""),
        "clarifying_question": s.get("clarifying_question", ""),
        "sql": s.get("safe_sql") or s.get("sql", ""),
        "columns": s.get("columns", []),
        "rows": s.get("rows", []),
        "truncated": s.get("truncated", False),
        "error": s.get("error", ""),
        "retries": max(0, s.get("attempts", 1) - 1),
        "usage": s.get("usage", {}),
        "timings": s.get("timings", {}),
    }
