// Dashboard chat: renders messages and sends turns to /admin/chat/<id>/send.
(() => {
  const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  // Minimal, escape-first Markdown: fenced code, inline code, bold, headings.
  function renderMarkdown(text) {
    const parts = text.split(/```/);
    return parts
      .map((part, i) => {
        if (i % 2 === 1) {
          const body = part.replace(/^[\w+-]*\n/, "");
          return `<pre><code>${esc(body.replace(/\n$/, ""))}</code></pre>`;
        }
        return esc(part)
          .replace(/`([^`\n]+)`/g, "<code>$1</code>")
          .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
          .replace(/^#{1,6} (.+)$/gm, "<strong>$1</strong>");
      })
      .join("");
  }

  document.querySelectorAll("[data-md]").forEach((el) => (el.innerHTML = renderMarkdown(el.textContent)));
  const root = document.getElementById("chat");
  if (!root) return; // transcript pages only need the Markdown pass above
  const messages = document.getElementById("messages");
  const form = document.getElementById("composer");
  const input = document.getElementById("content");
  const status = document.getElementById("status");
  const sendBtn = document.getElementById("send");
  const csrf = root.dataset.csrf;
  let sessionId = root.dataset.session;

  const scrollDown = () => (messages.scrollTop = messages.scrollHeight);
  scrollDown();

  function addMessage(role, text, meta) {
    messages.querySelector(".empty")?.remove();
    const wrap = document.createElement("div");
    wrap.className = `msg ${role}`;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = renderMarkdown(text);
    wrap.appendChild(bubble);
    if (meta) {
      const m = document.createElement("div");
      m.className = "meta";
      m.textContent = meta;
      wrap.appendChild(m);
    }
    messages.appendChild(wrap);
    scrollDown();
    return wrap;
  }

  async function post(url, fields) {
    const body = new FormData();
    body.append("csrf_token", csrf);
    for (const [k, v] of Object.entries(fields)) body.append(k, v);
    const res = await fetch(url, { method: "POST", body, credentials: "same-origin" });
    let data;
    try {
      data = await res.json();
    } catch {
      throw new Error(res.status === 403 ? "Security token expired. Reload the page." : "Session expired. Reload the page and sign in.");
    }
    if (!res.ok) throw Object.assign(new Error(data.error || `Request failed (${res.status})`), data);
    return data;
  }

  // POST a turn with stream=1 and read the server-sent events as they arrive. Calls onDelta(text) for
  // each piece; resolves with the final {done, title, meta} event; rejects on an error event or response.
  async function sendStreaming(url, fields, onDelta) {
    const body = new FormData();
    body.append("csrf_token", csrf);
    body.append("stream", "1");
    for (const [k, v] of Object.entries(fields)) body.append(k, v);
    const res = await fetch(url, { method: "POST", body, credentials: "same-origin" });
    if (!(res.headers.get("content-type") || "").startsWith("text/event-stream")) {
      let data;
      try {
        data = await res.json();
      } catch {
        throw new Error(res.status === 403 ? "Security token expired. Reload the page." : "Session expired. Reload the page and sign in.");
      }
      throw Object.assign(new Error(data.error || `Request failed (${res.status})`), data);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let end;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        const line = buffer.slice(0, end).split("\n").find((l) => l.startsWith("data: "));
        buffer = buffer.slice(end + 2);
        if (!line) continue;
        const event = JSON.parse(line.slice(6));
        if (event.delta) onDelta(event.delta);
        else if (event.error) throw Object.assign(new Error(event.error), event);
        else if (event.done) return event;
      }
    }
    throw new Error("The reply was cut off. Try again.");
  }

  const fmtMeta = (m) => {
    const cost = m.cost_usd != null ? `$${m.cost_usd.toFixed(4)}` : "—";
    return `${m.model} · ${m.effort} · ${m.input_tokens ?? "—"} in / ${m.output_tokens ?? "—"} out · ${cost} · ${(m.latency_ms / 1000).toFixed(1)}s`;
  };

  async function send() {
    const content = input.value.trim();
    if (!content || sendBtn.disabled) return;
    const model = document.getElementById("model").value;
    const effort = document.getElementById("effort").value;
    sendBtn.disabled = true;
    input.value = "";
    addMessage("user", content);
    const pending = addMessage("assistant", "…");
    pending.classList.add("pending");
    status.textContent = "Claude is thinking…";
    const isNew = !sessionId;
    let reply = "";
    try {
      if (isNew) {
        const system = document.getElementById("system-prompt")?.value || "";
        sessionId = (await post("/admin/chat/new", { model, effort, system_prompt: system })).id;
        history.replaceState(null, "", `/admin/chat/${sessionId}`);
      }
      const bubble = pending.querySelector(".bubble");
      const data = await sendStreaming(`/admin/chat/${sessionId}/send`, { content, model, effort }, (delta) => {
        if (!reply) {
          pending.classList.remove("pending");
          status.textContent = "Claude is writing…";
        }
        reply += delta;
        bubble.innerHTML = renderMarkdown(reply);
        scrollDown();
      });
      pending.remove();
      addMessage("assistant", reply, fmtMeta(data.meta));
      status.textContent = "";
      if (isNew) location.reload(); // refresh the sidebar and header for the new conversation
    } catch (err) {
      if (reply) {
        pending.classList.remove("pending"); // keep what already arrived, marked as incomplete
        pending.querySelector(".bubble").insertAdjacentHTML("beforeend", "<p class=\"muted\">(incomplete)</p>");
      } else {
        pending.remove();
      }
      if (err.discarded) {
        sessionId = "";
        history.replaceState(null, "", "/admin/chat");
      }
      addMessage("error", err.message);
      input.value = content;
      status.textContent = "";
    } finally {
      sendBtn.disabled = false;
      input.focus();
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    send();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  });
})();
