/* Polar web UI. No inline script: the page loads this file and reads its strings from #ui-strings. */
(function () {
  "use strict";
  var S = {};
  try { S = JSON.parse(document.getElementById("ui-strings").textContent || "{}"); } catch (e) { S = {}; }
  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var CSRF = csrfMeta ? csrfMeta.getAttribute("content") : "";
  var busy = 0;
  var dirty = false;

  function fmt(key, vals) {
    var s = S[key] || key;
    Object.keys(vals || {}).forEach(function (k) { s = s.split("{" + k + "}").join(String(vals[k])); });
    return s;
  }

  function cardOf(el) { return el.closest(".card"); }

  function showMsg(card, text) {
    var box = card.querySelector(".card-msg");
    if (!box) return;
    box.textContent = text || "";
    box.hidden = !text;
  }

  function setBadge(n) {
    var b = document.querySelector("[data-count]");
    if (!b || typeof n !== "number") return;
    b.textContent = String(n);
    if (n === 0) b.setAttribute("data-zero", ""); else b.removeAttribute("data-zero");
  }

  function setBusy(card, on) {
    card.querySelectorAll("button").forEach(function (b) {
      if (on) { b.setAttribute("aria-busy", "true"); b.disabled = true; }
      else { b.removeAttribute("aria-busy"); b.disabled = b.hasAttribute("data-nextbtn") && !picked(card).length; }
    });
  }

  function picked(card) {
    var out = [];
    card.querySelectorAll("[data-pick][aria-pressed='true']").forEach(function (b) {
      out.push(parseInt(b.getAttribute("data-pick"), 10));
    });
    return out;
  }

  function field(card, name) {
    var el = card.querySelector('[name="' + name + '"]');
    return el ? el.value : "";
  }

  function payload(card, btn) {
    var action = btn.getAttribute("data-action");
    var p = { id: card.getAttribute("data-id"), action: action };
    if (action === "choose") p.option = btn.getAttribute("data-option");
    if (action === "send") p.text = field(card, "text");
    if (action === "revise") p.text = field(card, "revise");
    if (action === "next") p.indexes = picked(card);
    if (action === "answer") {
      p.answers = {};
      card.querySelectorAll("fieldset.q").forEach(function (fs) {
        var typed = (fs.querySelector('input[name="qt"]') || {}).value || "";
        var radio = fs.querySelector('input[type="radio"]:checked');
        var value = typed.trim() ? typed.trim() : (radio ? radio.value : "");
        if (value) p.answers[fs.getAttribute("data-qkey")] = value;
      });
      var note = field(card, "note");
      if (note.trim()) p.note = note.trim();
    }
    return p;
  }

  function afterDone(card, data) {
    dirty = false;
    if (data && data.v) document.body.setAttribute("data-v", data.v);
    if (data) setBadge(data.waiting);
    if (card.hasAttribute("data-stay")) { window.location.reload(); return; }
    card.classList.add("leaving");
    window.setTimeout(function () {
      card.remove();
      if (!document.querySelector("main .card")) {
        var empty = document.querySelector("main .empty");
        if (empty) empty.hidden = false;
      }
    }, 220);
  }

  function act(card, btn) {
    showMsg(card, "");
    setBusy(card, true);
    busy += 1;
    fetch("/api/act", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
      body: JSON.stringify(payload(card, btn))
    }).then(function (res) {
      return res.json().catch(function () { return { ok: false }; }).then(function (data) {
        return { ok: res.ok && data.ok, data: data };
      });
    }).then(function (r) {
      busy -= 1;
      if (r.ok) { afterDone(card, r.data); return; }
      if (r.data && r.data.v) document.body.setAttribute("data-v", r.data.v);
      setBusy(card, false);
      showMsg(card, (r.data && r.data.error) || S.js_err_net);
    }).catch(function () {
      busy -= 1;
      setBusy(card, false);
      showMsg(card, S.js_err_net);
    });
  }

  document.addEventListener("click", function (ev) {
    var t = ev.target;
    if (!(t instanceof Element)) return;
    var pick = t.closest("[data-pick]");
    if (pick) {
      var c = cardOf(pick);
      pick.setAttribute("aria-pressed", pick.getAttribute("aria-pressed") === "true" ? "false" : "true");
      var go = c.querySelector("[data-nextbtn]");
      if (go) go.disabled = !picked(c).length;
      return;
    }
    var tog = t.closest("[data-toggle='revise']");
    if (tog) {
      var card = cardOf(tog);
      var box = card.querySelector(".revise-box");
      var open = box.hidden;
      box.hidden = !open;
      tog.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) { var ta = box.querySelector("textarea"); if (ta) ta.focus(); }
      return;
    }
    var btn = t.closest("[data-action]");
    if (btn && !btn.disabled) {
      var cd = cardOf(btn);
      if (cd) act(cd, btn);
      return;
    }
    var th = t.closest("a[data-lbx]");
    if (th && !ev.metaKey && !ev.ctrlKey && !ev.shiftKey) {
      ev.preventDefault();
      openViewer(th);
    }
  });

  document.addEventListener("input", function (ev) {
    if (ev.target instanceof Element && ev.target.closest(".card")) dirty = true;
  });

  /* photo viewer */
  var dlg = null;
  var group = [];
  var at = 0;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }

  function buildViewer() {
    dlg = el("dialog", "lbx");
    dlg.setAttribute("aria-label", S.js_viewer || "");
    var bar = el("div", "lbx-bar");
    var tab = el("a", "btn link", S.js_open_tab);
    tab.target = "_blank";
    tab.rel = "noopener";
    tab.setAttribute("data-lbx-tab", "");
    var close = el("button", "btn", S.js_close);
    close.type = "button";
    close.addEventListener("click", function () { dlg.close(); });
    bar.appendChild(tab);
    bar.appendChild(close);
    var fig = el("figure", "lbx-fig");
    var img = el("img");
    img.alt = "";
    fig.appendChild(img);
    fig.appendChild(el("figcaption", "lbx-cap"));
    var nav = el("div", "lbx-nav");
    var prev = el("button", "btn", S.js_prev);
    var next = el("button", "btn", S.js_next);
    prev.type = next.type = "button";
    prev.addEventListener("click", function () { move(-1); });
    next.addEventListener("click", function () { move(1); });
    nav.appendChild(prev);
    nav.appendChild(next);
    dlg.appendChild(bar);
    dlg.appendChild(fig);
    dlg.appendChild(nav);
    dlg.addEventListener("click", function (ev) { if (ev.target === dlg || ev.target === fig) dlg.close(); });
    dlg.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowLeft") { ev.preventDefault(); move(-1); }
      if (ev.key === "ArrowRight") { ev.preventDefault(); move(1); }
    });
    document.body.appendChild(dlg);
  }

  function render() {
    var a = group[at];
    var name = a.getAttribute("data-name") || "";
    var img = dlg.querySelector("img");
    img.src = a.getAttribute("href");
    img.alt = name;
    dlg.querySelector(".lbx-cap").textContent =
      name + (group.length > 1 ? " · " + fmt("js_count", { i: at + 1, n: group.length }) : "");
    dlg.querySelector("[data-lbx-tab]").href = a.getAttribute("href");
    dlg.querySelector(".lbx-nav").hidden = group.length < 2;
  }

  function move(step) {
    if (group.length < 2) return;
    at = (at + step + group.length) % group.length;
    render();
  }

  function openViewer(a) {
    if (typeof HTMLDialogElement === "undefined") { window.open(a.href, "_blank", "noopener"); return; }
    if (!dlg) buildViewer();
    var card = cardOf(a);
    group = Array.prototype.slice.call((card || document).querySelectorAll("a[data-lbx]"));
    at = Math.max(0, group.indexOf(a));
    render();
    dlg.showModal();
  }

  /* live refresh: reload when the data changed, never while the owner types or waits for an answer */
  function quiet() {
    var f = document.activeElement;
    var typing = f && (f.tagName === "TEXTAREA" || f.tagName === "INPUT");
    return !busy && !dirty && !typing && !(dlg && dlg.open) && !document.hidden;
  }

  function poll() {
    fetch("/api/state", { credentials: "same-origin", cache: "no-store" }).then(function (res) {
      return res.ok ? res.json() : null;
    }).then(function (data) {
      if (!data) return;
      setBadge(data.waiting);
      var v = document.body.getAttribute("data-v");
      if (data.v && v && data.v !== v && quiet()) window.location.reload();
    }).catch(function () { /* offline: try again on the next tick */ });
  }

  setBadge(parseInt((document.querySelector("[data-count]") || {}).textContent || "", 10));
  window.setInterval(poll, 5000);
})();
