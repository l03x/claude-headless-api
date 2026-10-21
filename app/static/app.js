// Confirm destructive actions: <form data-confirm="Really?">
document.addEventListener("submit", (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !window.confirm(msg)) e.preventDefault();
});

// Copy buttons: <button data-copy="#target">
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const el = document.querySelector(btn.dataset.copy);
  try {
    await navigator.clipboard.writeText(el.textContent.trim());
    const old = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => (btn.textContent = old), 1500);
  } catch {
    window.getSelection().selectAllChildren(el);
  }
});

// Theme switch: <div class="theme-switch"><button data-theme-set="light|dark|system">.
// The choice is kept in localStorage; "system" follows the OS. head.html applies it before paint.
(() => {
  const root = document.documentElement;
  const read = () => {
    try {
      return localStorage.getItem("theme") || "system";
    } catch {
      return "system";
    }
  };
  const apply = (choice) => {
    if (choice === "light" || choice === "dark") root.dataset.theme = choice;
    else delete root.dataset.theme;
    document.querySelectorAll("[data-theme-set]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeSet === choice)));
  };
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-theme-set]");
    if (!btn) return;
    const choice = btn.dataset.themeSet;
    try {
      if (choice === "system") localStorage.removeItem("theme");
      else localStorage.setItem("theme", choice);
    } catch {}
    apply(choice);
  });
  apply(read());
})();
