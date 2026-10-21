// API reference page: language tabs (kept in sync across every example) and copy buttons.
(() => {
  const KEY = "docs-lang";
  const blocks = () => document.querySelectorAll(".code-tabs");

  function show(block, tab) {
    const tabs = [...block.querySelectorAll('[role="tab"]')];
    const panels = [...block.querySelectorAll("pre.code")];
    const i = tabs.indexOf(tab);
    tabs.forEach((t, j) => t.setAttribute("aria-selected", String(j === i)));
    panels.forEach((p, j) => (p.hidden = j !== i));
  }

  function selectLang(lang) {
    blocks().forEach((block) => {
      const tab = block.querySelector(`[role="tab"][data-lang="${lang}"]`);
      if (tab) show(block, tab);
    });
  }

  document.addEventListener("click", async (e) => {
    const tab = e.target.closest('.code-tabs [role="tab"]');
    if (tab) {
      // Keep the clicked block's exact tab (e.g. "Python (requests)"), sync the rest by language.
      const lang = tab.dataset.lang;
      selectLang(lang);
      show(tab.closest(".code-tabs"), tab);
      try {
        localStorage.setItem(KEY, lang);
      } catch {}
      return;
    }
    const copy = e.target.closest(".code-copy");
    if (copy) {
      const pre = copy.closest(".code-tabs").querySelector("pre.code:not([hidden])");
      try {
        await navigator.clipboard.writeText(pre.textContent);
        copy.textContent = "Copied";
      } catch {
        window.getSelection().selectAllChildren(pre);
        copy.textContent = "Press Ctrl+C";
      }
      setTimeout(() => (copy.textContent = "Copy"), 1500);
    }
  });

  try {
    const saved = localStorage.getItem(KEY);
    if (saved) selectLang(saved);
  } catch {}

  // Highlight, in the table of contents, the last section whose top has scrolled past the header.
  const links = new Map([...document.querySelectorAll(".docs-toc a")].map((a) => [a.getAttribute("href").slice(1), a]));
  const targets = [...document.querySelectorAll("section[id], article[id]")].filter((el) => links.has(el.id));
  let ticking = false;
  function markCurrent() {
    ticking = false;
    let current = targets[0];
    for (const el of targets) {
      if (el.getBoundingClientRect().top <= 90) current = el;
      else break;
    }
    links.forEach((a) => a.classList.remove("current"));
    const link = current && links.get(current.id);
    if (link) {
      link.classList.add("current");
      link.scrollIntoView({ block: "nearest" });
    }
  }
  addEventListener("scroll", () => {
    if (!ticking) {
      ticking = true;
      requestAnimationFrame(markCurrent);
    }
  }, { passive: true });
  markCurrent();
})();
