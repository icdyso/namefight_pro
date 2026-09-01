/* 真战力试炼页（v1.3.0）：与固定编号敌人（"1".."N"）各打一场，胜场数即真战力。
 * 约定：文案全部来自 GET /api/text（config/game/ui.json 的 power_* 键），
 * 与主站一致不在前端硬编码；仅在显式请求时测量，正常对战不受影响。 */
(function () {
  "use strict";

  var state = { text: {}, busy: false };

  function t(key) {
    return state.text[key] != null ? state.text[key] : key;
  }

  function fmt(key, map) {
    var s = t(key);
    Object.keys(map || {}).forEach(function (k) {
      s = s.split("{" + k + "}").join(map[k]);
    });
    return s;
  }

  function errorText(err) {
    var code = err && err.code ? err.code : "error_request";
    var text = t(code);
    return text === code ? t("error_request") : text;
  }

  function measure(name, box, btn) {
    if (state.busy) return;
    state.busy = true;
    btn.disabled = true;
    NF.clear(box);
    box.appendChild(NF.h("div", { class: "power-waiting" }, t("power_measuring")));
    NF.fetchJSON("/api/power", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name })
    }).then(function (r) {
      state.busy = false;
      btn.disabled = false;
      renderResult(box, r);
    }).catch(function (e) {
      state.busy = false;
      btn.disabled = false;
      NF.clear(box);
      box.appendChild(NF.h("div", { class: "power-error" }, errorText(e)));
    });
  }

  function renderResult(box, r) {
    var rate = (r.rate * 100).toFixed(2) + "%";
    NF.clear(box);
    box.appendChild(NF.h("div", { class: "power-fighter" },
      NF.h("span", { class: "power-fighter-title" }, r.title && r.title.name
        ? "【" + r.title.name + "】" : ""),
      NF.h("span", { class: "power-fighter-name" }, r.name)));
    box.appendChild(NF.h("div", { class: "power-cards" },
      NF.h("div", { class: "power-card true" },
        NF.h("div", { class: "power-card-label" }, t("power_true_label")),
        NF.h("div", { class: "power-card-value" }, String(r.true_power))),
      NF.h("div", { class: "power-card panel" },
        NF.h("div", { class: "power-card-label" }, t("power_panel_label")),
        NF.h("div", { class: "power-card-value" }, String(r.power)))));
    box.appendChild(NF.h("div", { class: "power-summary" },
      fmt("power_summary", { wins: r.true_power, total: r.total, rate: rate })));
    box.appendChild(NF.h("div", { class: "power-elapsed" },
      fmt("power_elapsed", { ms: r.elapsed_ms })));
    box.appendChild(NF.h("div", { class: "power-note" },
      fmt("power_note", { total: r.total })));
  }

  function renderAll() {
    var root = NF.qs("#app");
    NF.clear(root);
    var nameInput = NF.h("input", {
      class: "name-input power-input", type: "text", maxlength: "32",
      placeholder: t("power_name_placeholder"), autocomplete: "off",
      onkeydown: function (e) { if (e.key === "Enter") btn.onclick(); }
    });
    var resultBox = NF.h("div", { class: "power-result" });
    var btn = NF.h("button", {
      class: "btn primary",
      onclick: function () {
        var name = nameInput.value.trim();
        if (!name) { nameInput.focus(); return; }
        measure(name, resultBox, btn);
      }
    }, t("power_run_button"));
    root.appendChild(NF.h("header", { class: "app-header" },
      NF.h("div", { class: "lang-row" },
        NF.h("a", { class: "lang-btn", href: "/" }, t("power_back")),
        NF.h("a", { class: "lang-btn", href: "/workshop.html" }, t("workshop_link"))),
      NF.h("h1", { class: "app-title" }, t("power_page_title")),
      NF.h("p", { class: "app-subtitle" }, t("power_page_subtitle"))));
    root.appendChild(NF.h("section", { class: "input-panel power-panel" },
      nameInput, btn));
    root.appendChild(resultBox);
  }

  NF.fetchJSON("/api/text").then(function (data) {
    state.text = data.ui || {};
    document.title = t("power_page_title") + " · " + t("app_title");
    renderAll();
  }).catch(function (e) {
    document.body.appendChild(
      NF.h("div", { class: "toast show" }, String((e && e.message) || e)));
  });
})();
