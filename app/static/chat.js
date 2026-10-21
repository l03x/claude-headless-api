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
    try {
      if (isNew) {
        const system = document.getElementById("system-prompt")?.value || "";
        sessionId = (await post("/admin/chat/new", { model, effort, system_prompt: system })).id;
        history.replaceState(null, "", `/admin/chat/${sessionId}`);
      }
      const data = await post(`/admin/chat/${sessionId}/send`, { content, model, effort });
      pending.remove();
      addMessage("assistant", data.reply, fmtMeta(data.meta));
      status.textContent = "";
      if (isNew) location.reload(); // refresh the sidebar and header for the new conversation
    } catch (err) {
      pending.remove();
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
