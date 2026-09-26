// Installable app: register the service worker and offer "Add to home screen".
(function () {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
  }
  const bar = document.querySelector(".install");
  if (!bar) return;
  const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  let dismissed = false;
  try { dismissed = localStorage.getItem("install-dismissed") === "1"; } catch (e) {}
  if (standalone || dismissed) return;
  const btn = bar.querySelector("[data-install]");
  const text = bar.querySelector("[data-install-text]");
  let deferred = null;
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault(); deferred = e; bar.classList.add("show");
  });
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
  if (isIOS) {
    text.textContent = "Get the app: tap Share, then “Add to Home Screen”.";
    btn.hidden = true;
    bar.classList.add("show");
  }
  btn.addEventListener("click", async () => {
    if (!deferred) return;
    deferred.prompt();
    await deferred.userChoice;
    deferred = null; bar.classList.remove("show");
  });
  bar.querySelector("[data-install-close]").addEventListener("click", () => {
    bar.classList.remove("show");
    try { localStorage.setItem("install-dismissed", "1"); } catch (e) {}
  });
})();

// Upvotes without reloading, confirmations, and the simple editor for members' articles.
(function () {
  document.querySelectorAll("form[data-vote]").forEach((f) => f.addEventListener("submit", async (e) => {
    const btn = f.querySelector("button");
    if (btn.getAttribute("formmethod") === "get") return; // not logged in: go to log in
    e.preventDefault();
    try {
      const r = await fetch(f.action, { method: "POST", body: new FormData(f), headers: { "X-Requested-With": "fetch" } });
      const d = await r.json();
      if (!d.ok) { alert(d.error || "Couldn't vote."); return; }
      btn.classList.toggle("on", d.voted); btn.setAttribute("aria-pressed", d.voted ? "true" : "false");
      f.querySelector("[data-count]").textContent = d.count;
    } catch (err) { f.submit(); }
  }));
  document.querySelectorAll("[data-ab-form]").forEach((form) => {  // live preview of a partner's author box
    const box = document.querySelector("[data-ab-preview]");
    if (!box) return;
    const q = (s) => box.querySelector(s), inp = (n) => form.querySelector(`[data-ab-in=${n}]`);
    const img = q("[data-ab-logo]"), mark = q("[data-ab-mark]"), count = form.querySelector("[data-ab-count]");
    const showLogo = (src) => { if (img) { if (src) img.src = src; img.hidden = !src; } if (mark) mark.hidden = !!src; };
    const text = (key, sel) => { const el = inp(key), out = q(sel); if (!el || !out) return;
      const sync = () => { out.textContent = el.value.trim(); out.hidden = !el.value.trim(); if (count && key === "about") count.textContent = el.value.length; };
      el.addEventListener("input", sync); };
    text("about", "[data-ab-about]"); text("address", "[data-ab-address]");
    const web = inp("website"), link = q("[data-ab-website]");
    if (web && link) web.addEventListener("input", () => { const ok = /^https?:\/\/\S+\.\S+/.test(web.value.trim()); link.hidden = !ok; if (ok) link.href = web.value.trim(); });
    const file = inp("logo"), remove = inp("remove_logo"), original = img && !img.hidden ? img.src : "";
    if (file) file.addEventListener("change", () => {
      const f = file.files && file.files[0];
      if (!f) { showLogo(remove && remove.checked ? "" : original); return; }
      const r = new FileReader(); r.onload = () => showLogo(r.result); r.readAsDataURL(f);
      if (remove) remove.checked = false;
    });
    if (remove) remove.addEventListener("change", () => showLogo(remove.checked ? "" : original));
  });
  document.querySelectorAll("[data-live-feed]").forEach((page) => {  // game night: new updates appear on their own
    const list = page.querySelector("[data-feed]"), url = page.dataset.liveFeed;
    let after = Number(page.dataset.after || 0);
    const set = (sel, v) => { const el = page.querySelector(sel); if (el && v !== null && v !== undefined) el.textContent = v; };
    const tick = async () => {
      if (document.hidden) return;
      try {
        const r = await fetch(`${url}?after=${after}`, { headers: { "X-Requested-With": "fetch" } });
        if (!r.ok) return;
        const d = await r.json();
        set("[data-our]", d.ours); set("[data-their]", d.theirs);
        set("[data-status]", d.status + (d.detail ? ` · ${d.detail}` : ""));
        d.updates.slice().reverse().forEach((u) => {
          const li = document.createElement("li"); li.className = "new";
          const who = document.createElement("span"); who.className = "meta";
          const b = document.createElement("b"); b.textContent = "@" + u.who; who.append(b, ` · ${u.ago}`);
          li.append(who);
          if (u.score) { const sc = document.createElement("b"); sc.className = "fscore"; sc.textContent = u.score; li.append(sc); }
          const body = document.createElement("span"); body.textContent = u.body; li.append(body);
          list.prepend(li); after = Math.max(after, u.id);
        });
        if (d.live !== "live") { clearInterval(timer); location.reload(); }
      } catch (e) { /* try again next time */ }
    };
    const timer = setInterval(tick, 20000);
  });
  const cele = document.querySelector("[data-celebrate]");  // a new badge: confetti!
  if (cele) {
    const box = cele.querySelector(".cele-box"), colors = ["#B3261E", "#C8961E", "#1F5FAD", "#1E6B3E", "#7B3FB5"];
    if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      for (let i = 0; i < 50; i++) {
        const c = document.createElement("i"); c.className = "confetti"; c.style.left = Math.random() * 100 + "%";
        c.style.background = colors[i % colors.length]; c.style.animationDelay = Math.random() * 0.5 + "s"; box.append(c);
      }
    }
    const close = () => cele.remove();
    cele.querySelector("[data-cele-close]").addEventListener("click", close);
    cele.addEventListener("click", (e) => { if (e.target === cele) close(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  }
  const shout = document.querySelector("[data-shout]");  // site-wide moments, shown once per browser
  if (shout && !cele) {
    let seen = "";
    try { seen = localStorage.getItem("shout-seen") || ""; } catch (e) { /* private mode */ }
    if (seen !== shout.dataset.shout) {
      shout.hidden = false;
      const done = () => { shout.remove(); try { localStorage.setItem("shout-seen", shout.dataset.shout); } catch (e) { /* ignore */ } };
      shout.querySelector("[data-shout-close]").addEventListener("click", done);
      setTimeout(done, 9000);
    }
  }
  document.querySelectorAll("[data-ci-range]").forEach((r) => {  // Call It slider shows the number as you slide
    const out = r.closest(".ci-range").querySelector("[data-ci-out]"), unit = r.dataset.unit ? " " + r.dataset.unit : "";
    const show = () => { out.textContent = Number(r.value).toString() + unit; };
    r.addEventListener("input", show); show();
  });
  document.querySelectorAll("[data-autosubmit]").forEach((el) => el.addEventListener("change", () => el.form.submit()));
  document.querySelectorAll("[data-check-rows]").forEach((box) => box.addEventListener("change", () => {
    box.closest("form").querySelectorAll("input[name=keep]").forEach((c) => { c.checked = box.checked; });
  }));
  document.querySelectorAll("[data-confirm]").forEach((el) => el.addEventListener("click", (e) => {
    if (!confirm(el.dataset.confirm)) e.preventDefault();
  }));
  document.querySelectorAll("[data-editor]").forEach((wrap) => {
    const ed = wrap.querySelector(".editor"), field = wrap.querySelector("textarea");
    if (!ed || !field) return;
    field.hidden = true; ed.hidden = false;
    wrap.querySelectorAll(".toolbar button").forEach((b) => b.addEventListener("click", () => {
      ed.focus();
      const c = b.dataset.cmd;
      if (c === "createLink") { const u = prompt("Link address (https://…)"); if (u && /^https?:\/\//.test(u)) document.execCommand("createLink", false, u); }
      else if (c === "h2" || c === "p") document.execCommand("formatBlock", false, c);
      else document.execCommand(c, false, null);
    }));
    wrap.closest("form").addEventListener("submit", () => { field.value = ed.innerHTML; });
  });
})();

// New post form: headline and summary grow as you type; subcategory follows the section
(() => {
  const grow = (el) => { el.style.height = "auto"; el.style.height = el.scrollHeight + 2 + "px"; };
  document.querySelectorAll("textarea[data-grow]").forEach((el) => {
    grow(el); el.addEventListener("input", () => grow(el)); window.addEventListener("resize", () => grow(el));
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" && el.name === "headline") e.preventDefault(); });
  });
  const parent = document.querySelector("[data-subcat-parent]"), sub = document.querySelector("[data-subcat]");
  if (!parent || !sub) return;
  const wrap = document.querySelector("[data-subcat-wrap]");
  parent.addEventListener("change", () => {
    let any = false;
    sub.querySelectorAll("option[data-cat]").forEach((o) => {
      const on = o.dataset.cat === parent.value;
      o.hidden = !on; o.disabled = !on; any = any || on;
      if (!on && o.selected) sub.value = "";
    });
    if (wrap) wrap.hidden = !any;
  });
})();
