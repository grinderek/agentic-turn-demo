const $ = (id) => document.getElementById(id);
const state = {
  token: localStorage.getItem("turn-session"),
  cid: localStorage.getItem("turn-conversation"),
  tid: localStorage.getItem("turn-latest"),
  seq: 0,
  calls: [],
  tools: [],
  sources: new Set(),
  actions: [],
  running: false,
  stream: null,
  traceSeq: new Set(),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Demo-Session": state.token || "",
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${response.status})`);
  }
  return response;
}
const json = async (path, options) => (await api(path, options)).json();
function error(message) {
  $("error").textContent = message;
  $("error").hidden = false;
}
function status(value) {
  state.running = value === "running";
  $("status").textContent =
    {
      running: "Running",
      done: "Complete",
      cancelled: "Cancelled",
      error: "Failed",
      interrupted: "Interrupted",
      ready: "Ready",
    }[value] || value;
  $("status").dataset.status = value;
  $("run").disabled = state.running;
  $("cancel").hidden = !state.running;
}
function counters() {
  $("sequence").textContent = `seq ${state.seq}`;
  $("call-count").textContent = state.calls.length;
  $("tool-count").textContent = state.tools.length;
  $("source-count").textContent = state.sources.size;
  $("audit").textContent = JSON.stringify(state.calls, null, 2);
}
function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}
function timeline(event) {
  if (event.type === "delta" || state.traceSeq.has(event.seq)) return;
  if (!state.traceSeq.size) $("trace").replaceChildren();
  state.traceSeq.add(event.seq);
  const labels = {
    start: "Turn started",
    tool: event.name,
    model_call: "Model · " + event.phase,
    staged: "Reply staged",
    answer_complete: "Answer streamed",
    done: "Grounded answer complete",
    cancelled: "Turn cancelled",
    error: "Turn failed",
    citation_rejected: "Citation rejected",
  };
  const row = node(
    "div",
    "trace-row" +
      (event.error || ["error", "citation_rejected"].includes(event.type)
        ? " warning"
        : ""),
  );
  const title = node("div", "trace-label");
  title.append(
    node("span", "", labels[event.type] || event.type),
    node("span", "mono", "#" + event.seq),
  );
  let detail =
    event.type === "tool"
      ? JSON.stringify(event.arguments)
      : event.type === "model_call"
        ? `${event.duration_ms}ms / ${event.usage ? "usage recorded" : "usage unavailable"}`
        : event.reason || (event.source_ids ? event.source_ids.join(", ") : "");
  if (event.type === "staged")
    detail = event.retired_action_id
      ? "Previous draft superseded"
      : "Human approval required";
  row.append(title, node("div", "trace-detail", detail));
  $("trace").append(row);
  $("trace").scrollTop = $("trace").scrollHeight;
}
function citations(items) {
  $("citations").replaceChildren();
  for (const item of items) {
    const button = node("button", "citation", "↗ " + item.source_id);
    button.title = item.reason;
    button.addEventListener("click", async () => {
      try {
        $("source-content").textContent = JSON.stringify(
          await json("/api/sources/" + encodeURIComponent(item.source_id)),
          null,
          2,
        );
        $("source-dialog").showModal();
      } catch (e) {
        error(e.message);
      }
    });
    $("citations").append(button);
  }
}
function drafts() {
  $("actions").replaceChildren();
  for (const action of state.actions
    .filter((a) => !["superseded", "discarded"].includes(a.status))
    .slice(-3)) {
    const card = node("div", "action");
    const title = node("div", "action-title");
    title.append(
      node("strong", "", "DRAFT REPLY"),
      node(
        "span",
        "",
        action.status === "executed_demo" ? "Executed in demo" : action.status,
      ),
    );
    card.append(
      title,
      node("div", "address", "To: " + action.recipient),
      node("strong", "body", action.subject),
      node("p", "body", action.body),
    );
    if (action.status === "pending") {
      const buttons = node("div", "buttons");
      for (const [decision, label] of [
        ["approve", "Approve demo action"],
        ["reject", "Reject"],
      ]) {
        const button = node(
          "button",
          decision === "reject" ? "secondary" : "",
          label,
        );
        button.disabled = state.running;
        button.addEventListener("click", async () => {
          buttons
            .querySelectorAll("button")
            .forEach((b) => (b.disabled = true));
          try {
            const result = await json(`/api/actions/${action.id}/${decision}`, {
              method: "POST",
            });
            action.status = result.status;
            drafts();
          } catch (e) {
            error(e.message);
            drafts();
          }
        });
        buttons.append(button);
      }
      card.append(buttons);
    }
    $("actions").append(card);
  }
}
function frame(event) {
  timeline(event);
  if (event.seq <= state.seq) return; // Replay is idempotent, old deltas cannot duplicate text.
  if (event.seq !== state.seq + 1)
    throw new Error("Event gap; restoring snapshot");
  state.seq = event.seq;
  if (event.type === "delta") $("answer").textContent += event.text;
  if (event.type === "tool") {
    state.tools.push(event);
    event.source_ids.forEach((id) => state.sources.add(id));
  }
  if (event.type === "model_call") state.calls.push(event);
  if (event.type === "staged") {
    const old = state.actions.find((a) => a.id === event.retired_action_id);
    if (old) old.status = "superseded";
    state.actions.push(event.action);
    drafts();
  }
  if (event.type === "done") {
    $("answer").textContent = event.text;
    status("done");
    citations(event.citations);
    drafts();
  }
  if (["cancelled", "error"].includes(event.type)) {
    status(event.type === "cancelled" ? "cancelled" : "error");
    error(event.reason.replaceAll("_", " "));
    state.actions.forEach((a) => {
      if (a.turn_id === state.tid && a.status === "pending")
        a.status = "discarded";
    });
    drafts();
  }
  counters();
}
async function restore() {
  const saved = await json("/api/turns/" + state.tid);
  state.seq = saved.seq;
  state.calls = saved.calls;
  state.tools = saved.tools;
  state.sources = new Set(saved.ledger);
  state.actions = saved.actions;
  $("answer").textContent = saved.text;
  $("user-prompt").textContent = saved.prompt;
  $("user-prompt").hidden = false;
  status(saved.status);
  citations(saved.citations);
  drafts();
  counters();
  if (saved.reason) error(saved.reason.replaceAll("_", " "));
}
async function subscribe(after = 0) {
  const tid = state.tid;
  state.stream?.abort();
  const controller = new AbortController();
  state.stream = controller;
  let cursor = after;
  while (!controller.signal.aborted && tid === state.tid) {
    try {
      const response = await api(`/api/turns/${tid}/events?after=${cursor}`, {
        signal: controller.signal,
      });
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      try {
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let boundary;
          while ((boundary = buffer.indexOf("\n\n")) !== -1) {
            const chunk = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            const data = chunk
              .split("\n")
              .find((line) => line.startsWith("data: "));
            if (data) {
              const event = JSON.parse(data.slice(6));
              frame(event);
              cursor = event.seq;
            }
          }
        }
      } finally {
        await reader.cancel().catch(() => {});
      }
      if (!state.running) return;
      await restore();
      cursor = state.seq;
    } catch (e) {
      if (controller.signal.aborted || tid !== state.tid) return;
      error("Connection interrupted. Restoring saved turn...");
      await new Promise((resolve) => setTimeout(resolve, 1000));
      try {
        await restore();
        cursor = state.seq;
        if (!state.running) return;
      } catch {
        error(e.message);
      }
    }
  }
}
$("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("error").hidden = true;
  $("run").disabled = true;
  try {
    const result = await json(`/api/conversations/${state.cid}/turns`, {
      method: "POST",
      body: JSON.stringify({
        prompt: $("prompt").value,
        scenario: $("scenario").value,
      }),
    });
    state.stream?.abort();
    state.tid = result.id;
    localStorage.setItem("turn-latest", state.tid);
    state.seq = 0;
    state.calls = [];
    state.tools = [];
    state.sources.clear();
    state.traceSeq.clear();
    $("answer").textContent = "";
    $("trace").replaceChildren();
    $("citations").replaceChildren();
    $("user-prompt").textContent = $("prompt").value;
    $("user-prompt").hidden = false;
    status("running");
    drafts();
    counters();
    void subscribe();
  } catch (e) {
    error(e.message);
    $("run").disabled = state.running;
  }
});
$("cancel").addEventListener("click", async () => {
  $("cancel").disabled = true;
  try {
    await api(`/api/turns/${state.tid}/cancel`, { method: "POST" });
  } catch (e) {
    error(e.message);
  } finally {
    $("cancel").disabled = false;
  }
});
$("close-source").addEventListener("click", () => $("source-dialog").close());
async function init() {
  try {
    const health = await json("/api/health");
    $("provider").textContent =
      health.provider === "scripted-demo"
        ? "Scripted demo / no API key"
        : "Live model / " + health.provider;
    if (health.provider !== "scripted-demo") {
      $("scenario").disabled = true;
      document.querySelector(".footnote").textContent =
        "Live model mode. Approval records a demo action; no email is sent.";
    }
    $("run").disabled = true;
    if (!state.token) {
      state.token = (await json("/api/sessions", { method: "POST" })).token;
      localStorage.setItem("turn-session", state.token);
    }
    if (!state.cid) {
      state.cid = (await json("/api/conversations", { method: "POST" })).id;
      localStorage.setItem("turn-conversation", state.cid);
    }
    // Recover even if navigation interrupted the POST response before its ID was saved.
    state.tid = (await json(`/api/conversations/${state.cid}/latest`)).id;
    if (state.tid) {
      localStorage.setItem("turn-latest", state.tid);
      await restore();
      void subscribe(0);
    } else status("ready");
  } catch (e) {
    error(
      e.message +
        ". If the demo database was reset, clear this site's local storage and reload.",
    );
  }
}
void init();
