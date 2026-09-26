// Newsroom dashboard behaviour. Everything still works as plain forms if this fails to load.
(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  // mobile menu
  const side = $(".side");
  $$("[data-menu]").forEach((b) => b.addEventListener("click", () => {
    side.classList.toggle("open");
    b.setAttribute("aria-expanded", side.classList.contains("open"));
  }));

  // confirmations
  document.querySelectorAll("[data-autosubmit]").forEach((el) => el.addEventListener("change", () => el.form.submit()));
  document.querySelectorAll("[data-check-rows]").forEach((box) => box.addEventListener("change", () => {
    box.closest("form").querySelectorAll("input[name=keep]").forEach((c) => { c.checked = box.checked; });
  }));
  $$("[data-confirm]").forEach((el) => el.addEventListener("click", (e) => {
    if (!confirm(el.dataset.confirm)) e.preventDefault();
  }));

  // chips: list editors backed by hidden inputs
  $$(".chips[data-name]").forEach((box) => {
    const name = box.dataset.name;
    const input = $("input[type=text]", box);
    const add = (v) => {
      v = v.trim();
      if (!v) return;
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = v;
      const hidden = document.createElement("input");
      hidden.type = "hidden"; hidden.name = name; hidden.value = v;
      const x = document.createElement("button");
      x.type = "button"; x.setAttribute("aria-label", "Remove " + v); x.textContent = "×";
      x.addEventListener("click", () => chip.remove());
      chip.append(hidden, x);
      box.insertBefore(chip, input);
    };
    $$(".chip button", box).forEach((x) => x.addEventListener("click", () => x.parentElement.remove()));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(input.value); input.value = ""; }
      if (e.key === "Backspace" && !input.value) { const last = $$(".chip", box).pop(); if (last) last.remove(); }
    });
    input.addEventListener("blur", () => { add(input.value); input.value = ""; });
    // keep an empty marker so clearing every chip still saves
    const marker = document.createElement("input");
    marker.type = "hidden"; marker.name = name + "__text"; marker.value = "";
    box.append(marker);
  });

  // rich text editor
  $$("[data-editor]").forEach((wrap) => {
    const ed = $(".editor", wrap), field = $("textarea", wrap);
    field.hidden = true; ed.hidden = false;
    $$(".toolbar button", wrap).forEach((b) => b.addEventListener("click", () => {
      ed.focus();
      if (b.dataset.cmd === "createLink") {
        const url = prompt("Link address (https://…)");
        if (url && /^https?:\/\//.test(url)) document.execCommand("createLink", false, url);
      } else if (b.dataset.cmd === "h2") {
        document.execCommand("formatBlock", false, "h2");
      } else if (b.dataset.cmd === "p") {
        document.execCommand("formatBlock", false, "p");
      } else {
        document.execCommand(b.dataset.cmd, false, null);
      }
    }));
    wrap.closest("form").addEventListener("submit", () => { field.value = ed.innerHTML; });
  });

  // verification checklist unlocks approval
  const checks = $$(".vcheck");
  const gated = $$("[data-needs-checks]");
  const hint = $("#approve-hint");
  const sync = () => {
    const ok = checks.every((c) => c.checked);
    gated.forEach((b) => (b.disabled = !ok));
    if (hint) hint.textContent = ok ? (checks.length ? "Everything verified." : "") : "Tick every verification item to approve.";
  };
  if (gated.length) { checks.forEach((c) => c.addEventListener("change", sync)); sync(); }

  // publish now / schedule
  $$("input[name=when]").forEach((r) => r.addEventListener("change", () => {
    const later = $("input[name=when][value=later]").checked;
    $("#schedule-box").hidden = !later;
    $("#btn-approve").hidden = later;
    $("#btn-schedule").hidden = !later;
  }));

  // source test button
  $$("[data-test-source]").forEach((btn) => btn.addEventListener("click", async () => {
    const form = btn.closest("form"), out = $("#test-out");
    out.hidden = false; out.className = "test-out"; out.textContent = "Checking…";
    btn.disabled = true;
    try {
      const r = await fetch(btn.dataset.testSource, { method: "POST", body: new FormData(form),
        headers: { "X-CSRF-Token": form.querySelector("[name=_csrf]").value } });
      const d = await r.json();
      out.className = "test-out " + (d.ok ? "ok" : "bad");
      if (d.ok) {
        out.textContent = d.count ? `Working. Found ${d.count} item${d.count === 1 ? "" : "s"}. Newest: “${d.newest[0]}”.`
                                  : "Working, but there's nothing in it right now.";
      } else out.textContent = d.error;
    } catch (e) { out.className = "test-out bad"; out.textContent = "Couldn't run the test. Check your connection."; }
    btn.disabled = false;
  }));
})();

// New items: select all, selection count, and a "working" state while the AI writes
(function () {
  const form = document.querySelector(".items-form");
  if (form) {
    const boxes = Array.from(form.querySelectorAll("input[name=item]"));
    const all = form.querySelector("[data-check-all]");
    const count = form.querySelector("[data-selected-count]");
    const bulk = Array.from(form.querySelectorAll("[data-needs-selection]"));
    const aiOff = new Set(bulk.filter((b) => b.disabled));
    const sync = () => {
      const n = boxes.filter((b) => b.checked).length;
      if (count) count.textContent = n ? `${n} selected` : "Tick items to act on several at once.";
      bulk.forEach((b) => { b.disabled = !n || aiOff.has(b); });
      if (all) all.checked = n && n === boxes.length;
    };
    boxes.forEach((b) => b.addEventListener("change", sync));
    if (all) all.addEventListener("change", () => { boxes.forEach((b) => (b.checked = all.checked)); sync(); });
    sync();
  }
  document.addEventListener("submit", (e) => {
    const btn = e.submitter;
    if (!btn) return;
    let msg = btn.dataset.working;
    if (!msg && /^(Develop|Create event|Check facts)/.test(btn.textContent.trim())) msg = "Working… (about 20 seconds)";
    if (!msg) return;
    setTimeout(() => { btn.disabled = true; btn.textContent = msg; }, 0);
  });
})();

// Social post boxes: live character counts, and show the text box only when the platform is ticked
(function () {
  document.querySelectorAll(".share").forEach((box) => {
    const cb = box.querySelector("input[type=checkbox]");
    const ta = box.querySelector("textarea");
    const out = box.querySelector(".cnt");
    if (!ta) return;
    const limit = parseInt(ta.dataset.limit || "0", 10);
    const sync = () => {
      if (out && limit) { out.textContent = `${ta.value.length} / ${limit}`; out.classList.toggle("over", ta.value.length > limit); }
      if (cb && !cb.disabled) ta.hidden = !cb.checked;
    };
    ta.addEventListener("input", sync);
    if (cb) cb.addEventListener("change", sync);
    sync();
  });
  // Social accounts page: Test buttons
  document.querySelectorAll("[data-test-social]").forEach((btn) => btn.addEventListener("click", async () => {
    const form = btn.closest("form"), out = form.querySelector(".test-out");
    out.hidden = false; out.className = "test-out"; out.textContent = "Checking…";
    btn.disabled = true;
    try {
      const fd = new FormData(); fd.append("platform", btn.dataset.testSocial);
      const r = await fetch(btn.dataset.url, { method: "POST", body: fd,
        headers: { "X-CSRF-Token": form.querySelector("[name=_csrf]").value } });
      const d = await r.json();
      out.className = "test-out " + (d.ok ? "ok" : "bad");
      out.textContent = d.message;
    } catch (e) { out.className = "test-out bad"; out.textContent = "Couldn't run the test."; }
    btn.disabled = false;
  }));
})();

// Sources: tick several to move or pause at once
(function () {
  document.querySelectorAll("form[data-bulk]").forEach((form) => {
    const boxes = Array.from(form.querySelectorAll("[data-bulk-box]"));
    const all = form.querySelector("[data-bulk-all]");
    const count = form.querySelector("[data-bulk-count]");
    const needs = Array.from(form.querySelectorAll("[data-bulk-needs]"));
    const sync = () => {
      const n = boxes.filter((b) => b.checked).length;
      if (count) count.textContent = n ? `${n} selected` : "Tick sources to move or pause several at once.";
      needs.forEach((b) => { b.disabled = !n; });
      if (all) all.checked = n > 0 && n === boxes.length;
    };
    boxes.forEach((b) => b.addEventListener("change", sync));
    if (all) all.addEventListener("change", () => { boxes.forEach((b) => (b.checked = all.checked)); sync(); });
    sync();
  });
})();

// Story editor: the subcategory list follows the category (Sports → College, …)
(function () {
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

// Post editor: headline and summary grow as you type; AI headline picks fill the fields
(function () {
  const grow = (el) => { el.style.height = "auto"; el.style.height = el.scrollHeight + "px"; };
  document.querySelectorAll("textarea[data-grow]").forEach((el) => {
    grow(el); el.addEventListener("input", () => grow(el)); window.addEventListener("resize", () => grow(el));
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" && el.name === "headline") e.preventDefault(); });
  });
  document.querySelectorAll("[data-fill]").forEach((b) => b.addEventListener("click", () => {
    const f = document.querySelector(`textarea[name=${b.dataset.fill}]`);
    if (!f) return;
    f.value = b.dataset.value; grow(f); f.focus();
    b.parentElement.querySelectorAll("[data-fill]").forEach((x) => { x.style.borderColor = ""; });
    b.style.borderColor = "#5B3FB5";
  }));
})();
