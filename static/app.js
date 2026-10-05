// AskYourData frontend. Rule: server data is only ever rendered with textContent (no innerHTML) -> no XSS.
const $ = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });
let lastQueryId = null;
let pendingClarification = null; // original question while the agent waits for a clarification
let chart = null;

function csrf() {
  return (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 || res.status === 403 && path === "/api/clients/") location.href = "/login/";
  const data = await res.json().catch(() => ({ status: "error", error: `HTTP ${res.status}` }));
  return { status: res.status, data };
}

function remember(key, value) {
  try { value === undefined ? (value = localStorage.getItem(key)) : localStorage.setItem(key, value); } catch (e) {}
  return value;
}

async function loadClients() {
  const { data } = await api("/api/clients/");
  const select = $("client");
  select.replaceChildren(...data.map((c) => new Option(c.name, c.id)));
  const saved = remember("client");
  if (saved && data.some((c) => String(c.id) === saved)) select.value = saved;
  if (!data.length) notice("warn", "You don't have access to any client yet. Ask an admin to assign one.");
  loadHistory();
}

async function loadHistory() {
  const { data } = await api(`/api/history/?client_id=${$("client").value}`);
  $("history").replaceChildren(...(data || []).map((h) => {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.textContent = h.question;
    b.title = h.question;
    b.onclick = () => { $("question").value = h.question; $("question").focus(); };
    li.append(b);
    return li;
  }));
}

function notice(kind, text) {
  const n = $("notice");
  n.hidden = !text;
  n.className = `notice notice-${kind}`;
  n.textContent = text || "";
  $("result").hidden = false;
}

function renderTable(columns, rows) {
  const head = document.createElement("tr");
  columns.forEach((c, i) => {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = c;
    if (rows.length && typeof rows[0][i] === "number") th.className = "num";
    head.append(th);
  });
  const body = rows.slice(0, 200).map((r) => {
    const tr = document.createElement("tr");
    r.forEach((v) => {
      const td = document.createElement("td");
      if (typeof v === "number") { td.className = "num"; td.textContent = fmt.format(v); }
      else td.textContent = v === null ? "—" : String(v);
      tr.append(td);
    });
    return tr;
  });
  $("table").replaceChildren(head, ...body);
}

function renderChart(columns, rows) {
  if (chart) { chart.destroy(); chart = null; }
  const numeric = columns.map((_, i) => rows.length && rows.every((r) => typeof r[i] === "number" || r[i] === null));
  const series = columns.map((c, i) => i).filter((i) => i > 0 && numeric[i]);
  const isDate = rows.length && /^\d{4}-\d{2}-\d{2}/.test(String(rows[0][0]));
  const show = window.Chart && rows.length >= 2 && rows.length <= 60 && !numeric[0] && series.length;
  $("chart-wrap").hidden = !show;
  if (!show) return;
  const data = isDate ? [...rows].sort((a, b) => String(a[0]).localeCompare(String(b[0]))) : rows;
  const used = isDate ? series.slice(0, 2) : series.slice(0, 1); // two series max, second on its own axis
  chart = new Chart($("chart"), {
    type: isDate ? "line" : "bar",
    data: {
      labels: data.map((r) => r[0]),
      datasets: used.map((i, k) => ({
        label: columns[i], data: data.map((r) => r[i]), yAxisID: k ? "y1" : "y",
        borderColor: k ? "#F59E0B" : "#1E90FF", backgroundColor: k ? "#F59E0B" : "#1E90FF", tension: 0.25,
      })),
    },
    options: {
      maintainAspectRatio: false, indexAxis: !isDate && data.length > 8 ? "y" : "x",
      scales: used.length > 1 ? { y1: { position: "right", grid: { drawOnChartArea: false } } } : {},
      plugins: { legend: { display: used.length > 1 } },
    },
  });
}

function render(resp, status) {
  lastQueryId = resp.query_id || null;
  document.querySelectorAll("#feedback .link").forEach((b) => b.setAttribute("aria-pressed", "false"));
  $("feedback").hidden = !lastQueryId || resp.status !== "ok";
  $("answer").classList.remove("loading");
  $("answer").textContent = resp.status === "ok" ? resp.answer : "";
  $("sql").textContent = resp.sql || "";
  $("sql-box").hidden = !resp.sql;
  renderTable(resp.columns || [], resp.rows || []);
  renderChart(resp.columns || [], resp.rows || []);

  pendingClarification = null;
  if (resp.status === "ok") notice("info", resp.rows && !resp.rows.length ? "No rows matched." : "");
  else if (resp.status === "clarify") {
    pendingClarification = $("question").value;
    notice("info", `${resp.clarifying_question} (type your answer and press Ask)`);
    $("question").value = "";
  } else if (resp.status === "rejected") notice("warn", "Rejected: I couldn't build a safe query for that. Try rephrasing.");
  else if (status === 429) notice("warn", resp.error || "Hourly limit or daily budget reached. Try later.");
  else notice("error", `Error: ${resp.error || "something went wrong"}${resp.query_id ? ` (query #${resp.query_id})` : ""}`);

  const m = resp.meta;
  $("meta").textContent = m ? [
    `${(m.latency_ms / 1000).toFixed(1)} s`, `$${m.cost_usd.toFixed(4)}`,
    m.cache_hit ? `cached (${m.cache_hit}) ✓` : "fresh", m.retries ? "self-corrected ✓" : "",
    m.data_synced_at ? `data synced ${new Date(m.data_synced_at).toLocaleString()}` : "",
  ].filter(Boolean).join(" · ") : "";
}

async function ask() {
  let question = $("question").value.trim();
  if (question.length < 3) return;
  if (pendingClarification) question = `${pendingClarification} (clarification: ${question})`;
  const btn = $("ask");
  btn.disabled = true;
  $("result").hidden = false;
  $("notice").hidden = true;
  $("answer").classList.add("loading");
  const t0 = Date.now();
  const tick = setInterval(() => {
    const s = Math.round((Date.now() - t0) / 1000);
    $("answer").textContent = `Thinking… ${s}s${s > 10 ? " (the free server may be waking up)" : ""}`;
  }, 500);
  try {
    const { status, data } = await api("/api/ask/", { client_id: Number($("client").value), question });
    render(data, status);
    loadHistory();
  } catch (e) {
    render({ status: "error", error: "Network error" }, 0);
  } finally {
    clearInterval(tick);
    btn.disabled = false;
  }
}

$("ask").onclick = ask;
$("question").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) ask(); });
$("question").addEventListener("input", () => {
  const n = $("question").value.length;
  $("count").textContent = n > 400 ? `${n}/500` : "";
});
$("client").onchange = () => { remember("client", $("client").value); loadHistory(); };
$("chips").onclick = (e) => { if (e.target.classList.contains("chip")) $("question").value = e.target.textContent; };
$("copy").onclick = () => navigator.clipboard && navigator.clipboard.writeText($("sql").textContent);
$("feedback").onclick = async (e) => {
  const rating = Number(e.target.dataset.rating);
  if (!rating || !lastQueryId) return;
  await api("/api/feedback/", { query_id: lastQueryId, rating });
  document.querySelectorAll("#feedback .link").forEach((b) => b.setAttribute("aria-pressed", String(b === e.target)));
};
loadClients();
