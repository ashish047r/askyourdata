"""All guardrails in one place. Pure Python, no LLM, no Django.

validate_sql        - SQL guard: the LLM's SQL is untrusted input.
looks_like_injection- input flag: suspicious questions are answered (the SQL guard still applies) but never cached.
same_constraints    - semantic-cache guard: "last 7 days" must never reuse the answer for "last 30 days".
"""

import re

import sqlglot
from sqlglot import exp

ALLOWED_TABLES = {"ga4_daily", "ads_campaign_daily"}
MAX_ROWS = 1000

# Names as sqlglot reports them (sql_name() for known functions, raw name for unknown ones).
# Allowlist, not denylist: anything unknown (set_config, current_setting, pg_sleep, pg_read_file,
# dblink, ...) is rejected by default.
ALLOWED_FUNCS = {
    "SUM", "AVG", "COUNT", "MIN", "MAX", "STDDEV", "PERCENTILE_CONT",
    "ROUND", "ABS", "CEIL", "FLOOR", "SQRT", "POWER", "GREATEST", "LEAST",
    "COALESCE", "NULLIF", "CASE", "IF", "CAST",
    "CURRENT_DATE", "CURRENT_TIMESTAMP", "DATE", "DATE_TRUNC", "TIMESTAMP_TRUNC", "EXTRACT",
    "TIME_TO_STR", "STR_TO_DATE",
    "LOWER", "UPPER", "CONCAT", "TRIM", "LENGTH", "SPLIT_PART", "LEFT", "RIGHT", "REPLACE", "SUBSTRING",
    "LAG", "LEAD", "RANK", "DENSE_RANK", "ROW_NUMBER",
}

FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
    exp.Command, exp.Into, exp.Lock, exp.Set, exp.TruncateTable, exp.Grant,
)


class SqlRejected(Exception):
    pass


def validate_sql(sql: str) -> str:
    """Return a safe, LIMIT-ed SQL string or raise SqlRejected(reason)."""
    if not sql or not sql.strip():
        raise SqlRejected("empty query")
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise SqlRejected(f"could not parse SQL: {str(e).splitlines()[0]}")
    if len(statements) != 1:
        raise SqlRejected("exactly one statement is allowed")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise SqlRejected("only SELECT queries are allowed")

    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SqlRejected(f"forbidden operation: {type(node).__name__.upper()}")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if table.args.get("db") or table.args.get("catalog"):
            raise SqlRejected(f"schema-qualified table not allowed: {table.sql()}")
        if name not in ALLOWED_TABLES and name not in cte_names:
            raise SqlRejected(f"table not allowed: {name}")

    for func in tree.find_all(exp.Func):
        if isinstance(func, exp.Connector):  # AND / OR are modelled as functions by sqlglot
            continue
        name = func.name.upper() if isinstance(func, exp.Anonymous) else func.sql_name()
        if name not in ALLOWED_FUNCS:
            raise SqlRejected(f"function not allowed: {name.lower()}")

    limit = tree.args.get("limit")
    current = limit.expression if limit is not None else None
    if not (isinstance(current, exp.Literal) and current.is_int and int(current.this) <= MAX_ROWS):
        tree = tree.limit(MAX_ROWS)
    return tree.sql(dialect="postgres")


_INJECTION = re.compile(
    r"ignore (all|any|the|previous|above|prior)\b.*\binstructions|system prompt|you are now|"
    r"\b(drop|truncate|alter)\s+table\b|\bdelete\s+from\b|\binsert\s+into\b|\bupdate\s+\w+\s+set\b|"
    r"\bgrant\b|set_config|current_setting|pg_\w+|;\s*--",
    re.I,
)


def looks_like_injection(question: str) -> bool:
    return bool(_INJECTION.search(question))


# Words that change the answer. Two questions may share a cached answer only if these match exactly.
_CONSTRAINT = re.compile(
    r"\d+(?:\.\d+)?|today|yesterday|week|weekly|month|monthly|quarter|year|ytd|mtd|daily|"
    r"desktop|mobile|tablet|device|organic|paid|cpc|direct|referral|email|social|source|medium|"
    r"search|display|video|pmax|performance max|shopping|campaign|channel|"
    r"sessions?|users?|new|engaged|engagement|key events?|revenue|conversions?|conversion rate|"
    r"clicks?|impressions?|cost|spend|ctr|cpa|roas|value|"
    r"top|bottom|best|worst|highest|lowest|most|least|increase|decrease|drop|growth|change|compare|vs|versus",
    re.I,
)


def constraints(question: str) -> list[str]:
    return sorted({m.lower().rstrip("s") for m in _CONSTRAINT.findall(question)})


def same_constraints(a: str, b: str) -> bool:
    # ponytail: keyword heuristic; tuned on evals/cache_pairs.jsonl. Swap for an LLM equivalence
    # check if paraphrase false-hits ever show up in QueryLog feedback.
    return constraints(a) == constraints(b)
