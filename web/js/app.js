/* 名字竞技场前端应用逻辑。
 * 约定（见 AGENTS.md 2.2.3）：前端不硬编码任何面向用户的文案——
 * 所有用户可见文本均来自 GET /api/text（即 config/locales/<lang>/ui.json）。 */
(function () {
  "use strict";

  var TICK_MS = 85;           // 每个 tick 的基础演出时长（ms）
  var SPEEDS = [0.5, 1, 2, 4];  // 回放倍速

  var state = {
    lang: "zh",
    langs: ["zh"],
    version: "",
    text: {},
    fighters: null,   // [fighterApi, fighterApi]（按输入顺序）
    battle: null,     // /api/battle 响应
    shown: 0,         // 已展示的战报条数
    playing: false,
    timer: null,
    speed: 1,
    busy: false
  };

  var els = {};

  /* ---------------- 工具 ---------------- */

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

  function showError(message) {
    if (!els.toast) return;
    els.toast.textContent = message;
    els.toast.classList.add("show");
    clearTimeout(showError._timer);
    showError._timer = setTimeout(function () {
      els.toast.classList.remove("show");
    }, 3200);
  }

  function setBusy(busy) {
    state.busy = busy;
    [els.deriveBtn, els.battleBtn].forEach(function (b) {
      if (b) b.disabled = busy;
    });
  }

  /* ---------------- API ---------------- */

  function apiText(lang) {
    return NF.fetchJSON("/api/text?lang=" + encodeURIComponent(lang));
  }

  function apiFighter(name) {
    return NF.fetchJSON("/api/fighter?name=" + encodeURIComponent(name) +
      "&lang=" + encodeURIComponent(state.lang));
  }

  function apiBattle(a, b) {
    return NF.fetchJSON("/api/battle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ a: a, b: b, lang: state.lang })
    });
  }

  /* ---------------- 数据动作 ---------------- */

  function currentNames() {
    return {
      a: els.nameA ? els.nameA.value.trim() : "",
      b: els.nameB ? els.nameB.value.trim() : ""
    };
  }

  function validateNames() {
    var n = currentNames();
    if (!n.a || !n.b) {
      showError(t("error_enter_names"));
      return null;
    }
    return n;
  }

  function derive() {
    var n = validateNames();
    if (!n) return Promise.resolve();
    setBusy(true);
    return Promise.all([apiFighter(n.a), apiFighter(n.b)])
      .then(function (fs) {
        state.fighters = fs;
        state.battle = null;
        stopPlayback();
        renderArena();
        renderBattlePanel();
      })
      .catch(function (e) { showError(errorText(e)); })
      .then(function () { setBusy(false); });
  }

  function fight() {
    var n = validateNames();
    if (!n) return Promise.resolve();
    setBusy(true);
    return apiBattle(n.a, n.b)
      .then(function (res) {
        state.fighters = res.fighters;
        state.battle = res;
        state.shown = 0;
        renderArena();
        renderBattlePanel();
        startPlayback();
      })
      .catch(function (e) { showError(errorText(e)); })
      .then(function () { setBusy(false); });
  }

  function loadLangText(lang) {
    return apiText(lang).then(function (data) {
      state.lang = data.lang;
      state.langs = data.langs || [data.lang];
      state.version = data.version || "";
      state.text = data.ui || {};
      document.title = t("app_title");
    });
  }

  /* 切换语言后，用同样的名字重新拉取当前数据，验证文案切换、结果不变 */
  function refreshAfterLangChange() {
    var n = currentNames();
    if (!n.a || !n.b) return Promise.resolve();
    if (state.battle) return fight();
    if (state.fighters) return derive();
    return Promise.resolve();
  }

  function switchLang(lang) {
    if (lang === state.lang || state.busy) return;
    loadLangText(lang)
      .then(function () {
        renderAll();
        return refreshAfterLangChange();
      })
      .catch(function (e) { showError(errorText(e)); });
  }

  /* ---------------- 战报逐刻回放（tick 时钟驱动） ----------------
   * 回放按游戏刻推进：每刻双方行动槽 +速度值，行动槽满时该方的战报条目
   * 恰好到达并被揭示——由此直观展现双方真实的攻击间隔与出手节奏。 */

  function stopPlayback() {
    state.playing = false;
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }
  }

  function tickDuration() {
    return Math.max(16, TICK_MS / state.speed);
  }

  function startPlayback() {
    stopPlayback();
    state.tickPos = 0;
    state.playing = true;
    renderControls();
    scheduleTick();
  }

  function scheduleTick() {
    state.timer = setTimeout(tickStep, tickDuration());
  }

  function tickStep() {
    if (!state.playing || !state.battle) return;
    var log = state.battle.log;
    var finalTick = log.length ? log[log.length - 1].tick : 0;
    if (state.shown >= log.length && state.tickPos >= finalTick) {
      finishPlayback();
      return;
    }
    state.tickPos++;
    advanceGauges();
    while (state.shown < log.length && log[state.shown].tick <= state.tickPos) {
      appendLogEntry(log[state.shown]);
      state.shown++;
    }
    if (state.shown >= log.length && state.tickPos >= finalTick) {
      finishPlayback();
      return;
    }
    scheduleTick();
  }

  /* 客户端行动槽逐刻推进（每刻 +速度%），与服务器快照对齐校正 */
  function advanceGauges() {
    ["a", "b"].forEach(function (side) {
      var refs = els.hud && els.hud[side];
      if (!refs || refs._dead || !refs._spd) return;
      var g = Math.min(100, (refs._gauge || 0) + refs._spd);
      refs._gauge = g;
      refs.gaugeFill.style.transition = "width " + tickDuration() + "ms linear";
      refs.gaugeFill.style.width = g + "%";
    });
  }

  function finishPlayback() {
    stopPlayback();
    renderControls();
    showResult();
  }

  function skipPlayback() {
    if (!state.battle) return;
    stopPlayback();
    state.skipping = true;
    var log = state.battle.log;
    while (state.shown < log.length) {
      appendLogEntry(log[state.shown]);
      state.shown++;
    }
    state.tickPos = log.length ? log[log.length - 1].tick : 0;
    state.skipping = false;
    showResult();
    renderControls();
  }

  function togglePlayback() {
    if (!state.battle) return;
    if (state.playing) {
      stopPlayback();
      renderControls();
    } else {
      var log = state.battle.log;
      var finalTick = log.length ? log[log.length - 1].tick : 0;
      if (state.shown >= log.length && state.tickPos >= finalTick) return;
      state.playing = true;
      renderControls();
      scheduleTick();
    }
  }

  /* ---------------- 渲染 ---------------- */

  function renderAll() {
    var keep = {
      a: els.nameA ? els.nameA.value : "",
      b: els.nameB ? els.nameB.value : ""
    };
    var root = NF.qs("#app");
    NF.clear(root);
    root.appendChild(buildToast());
    root.appendChild(buildHeader());
    root.appendChild(buildInputPanel());
    root.appendChild(buildArena());
    root.appendChild(buildBattlePanel());
    root.appendChild(buildFooter());
    if (els.nameA) els.nameA.value = keep.a;
    if (els.nameB) els.nameB.value = keep.b;
    renderArena();
    renderBattlePanel();
  }

  function buildToast() {
    els.toast = NF.h("div", { class: "toast", role: "alert" });
    return els.toast;
  }

  function buildHeader() {
    return NF.h("header", { class: "app-header" },
      NF.h("div", { class: "lang-row", role: "group", "aria-label": t("lang_label") },
        state.langs.map(function (lg) {
          return NF.h("button", {
            class: "lang-btn" + (lg === state.lang ? " active" : ""),
            onclick: function () { switchLang(lg); }
          }, lg.toUpperCase());
        })),
      NF.h("h1", { class: "app-title" }, t("app_title")),
      NF.h("p", { class: "app-subtitle" }, t("app_subtitle"))
    );
  }

  function buildInputPanel() {
    function nameInput(side) {
      return NF.h("input", {
        class: "name-input", type: "text", maxlength: "32",
        placeholder: t("name_placeholder"), autocomplete: "off",
        "aria-label": side === "a" ? t("label_a") : t("label_b"),
        onkeydown: function (e) { if (e.key === "Enter") fight(); }
      });
    }
    els.nameA = nameInput("a");
    els.nameB = nameInput("b");
    els.deriveBtn = NF.h("button", { class: "btn", onclick: derive }, t("derive_button"));
    els.battleBtn = NF.h("button", { class: "btn primary", onclick: fight }, t("battle_button"));
    return NF.h("section", { class: "input-panel" },
      NF.h("div", { class: "fighter-input side-a" },
        NF.h("span", { class: "side-label a" }, t("label_a")), els.nameA),
      NF.h("div", { class: "vs-badge" }, t("vs_text")),
      NF.h("div", { class: "fighter-input side-b" },
        NF.h("span", { class: "side-label b" }, t("label_b")), els.nameB),
      NF.h("div", { class: "action-row" }, els.deriveBtn, els.battleBtn)
    );
  }

  function buildArena() {
    els.arena = NF.h("section", { class: "arena" });
    return els.arena;
  }

  function renderArena() {
    if (!els.arena) return;
    NF.clear(els.arena);
    if (!state.fighters) {
      els.arena.appendChild(emptyCard());
      els.arena.appendChild(emptyCard());
      return;
    }
    els.arena.appendChild(fighterCard(state.fighters[0], "a"));
    els.arena.appendChild(fighterCard(state.fighters[1], "b"));
  }

  function emptyCard() {
    return NF.h("div", { class: "empty-card" },
      NF.h("div", { class: "empty-title" }, t("empty_card_title")),
      NF.h("div", { class: "empty-hint" }, t("empty_card_hint")));
  }

  function fighterCard(f, side) {
    var statRows = f.attributes.map(function (a) {
      var span = Math.max(1, a.max - a.min);
      var pct = Math.max(0, Math.min(100, (a.value - a.min) / span * 100));
      var valueText = a.format === "percent" ? a.value + "%" : String(a.value);
      return NF.h("div", { class: "stat-row" },
        NF.h("span", { class: "stat-name" }, a.name),
        NF.h("span", { class: "stat-value" }, valueText),
        NF.h("div", { class: "stat-bar" },
          NF.h("div", { class: "stat-fill " + a.id, style: { width: pct + "%" } })));
    });

    var skillNodes = f.skills.map(function (s) {
      return NF.h("div", { class: "skill-chip" },
        NF.h("div", { class: "skill-name" }, s.name),
        s.text ? NF.h("div", { class: "skill-text" }, s.text) : null,
        s.flavor ? NF.h("div", { class: "skill-desc" }, s.flavor) : null,
        NF.h("div", { class: "tip" },
          NF.h("div", { class: "tip-title" }, s.name),
          s.text ? NF.h("div", { class: "tip-line" }, s.text) : null,
          s.flavor ? NF.h("div", { class: "tip-line muted" }, s.flavor) : null,
          s.modifiers && s.modifiers.length
            ? NF.h("div", { class: "tip-mods" }, s.modifiers.join("；")) : null));
    });

    return NF.h("div", { class: "fighter-card side-" + side },
      NF.h("div", { class: "card-head" },
      NF.h("div", { class: "card-id" },
        NF.h("h2", { class: "fighter-name" }, f.name),
        NF.h("div", { class: "fighter-title" }, f.title.name),
        f.title.description
          ? NF.h("div", { class: "title-desc" }, f.title.description) : null,
        f.title.bonuses_text
          ? NF.h("div", { class: "title-bonus" },
              fmt("title_bonus_label", { bonuses: f.title.bonuses_text })) : null),
        NF.h("div", { class: "fighter-power" },
          NF.h("div", { class: "power-value" }, String(f.power)),
          NF.h("div", { class: "power-label" }, t("power_label")))),
      NF.h("div", { class: "badges" },
        NF.h("span", { class: "badge element" },
          (f.element.emoji ? f.element.emoji + " " : "") + f.element.name),
        NF.h("span", { class: "badge md5" }, fmt("md5_label", { digest: f.digest_short }))),
      NF.h("div", { class: "section-title" }, t("stats_title")),
      statRows,
      NF.h("div", { class: "section-title" }, t("skills_title")),
      NF.h("div", { class: "skill-list" }, skillNodes)
    );
  }

  function buildBattlePanel() {
    els.battlePanel = NF.h("section", { class: "battle-panel", style: { display: "none" } });
    return els.battlePanel;
  }

  function renderBattlePanel() {
    if (!els.battlePanel) return;
    stopPlayback();
    NF.clear(els.battlePanel);
    if (!state.battle) {
      els.battlePanel.style.display = "none";
      return;
    }
    els.battlePanel.style.display = "";
    els.controls = NF.h("div", { class: "playback" });
    els.logBox = NF.h("div", { class: "battle-log" });
    els.resultBox = NF.h("div", { class: "result-slot" });
    els.hud = {
      a: buildHud(state.fighters[0], "a"),
      b: buildHud(state.fighters[1], "b")
    };
    els.battlePanel.appendChild(NF.h("div", { class: "battle-head" },
      NF.h("h3", { class: "battle-title" }, t("battle_title")),
      els.controls));
    els.battlePanel.appendChild(NF.h("div", { class: "battle-hud" }, els.hud.a.root, els.hud.b.root));
    els.battlePanel.appendChild(els.logBox);
    els.battlePanel.appendChild(els.resultBox);
    renderControls();
    // 开战前的初始状态（满血 + 常驻被动），瞬时应用
    if (state.battle.log.length && state.battle.log[0].state) {
      applyBattleState(state.battle.log[0].state, true);
    }
  }

  /* ---------- 战斗 HUD：HP / 属性 / 行动槽 / buff 实时渲染 ---------- */

  function buildHud(f, side) {
    var attrName = function (id) {
      var found = f.attributes.filter(function (a) { return a.id === id; })[0];
      return found ? found.name : id.toUpperCase();
    };
    var refs = {};
    refs.hpFill = NF.h("div", { class: "hud-hpfill" });
    refs.hpText = NF.h("span", { class: "hud-hptext" }, "");
    refs.atkVal = NF.h("b", null, "");
    refs.defVal = NF.h("b", null, "");
    refs.spdVal = NF.h("b", null, "");
    refs.atkStat = NF.h("span", { class: "hud-stat" }, attrName("atk") + " ", refs.atkVal);
    refs.gaugeFill = NF.h("div", { class: "hud-gaugefill" });
    refs.buffs = NF.h("div", { class: "hud-buffs" });
    refs.root = NF.h("div", { class: "hud side-" + side },
      NF.h("div", { class: "hud-head" },
        NF.h("span", { class: "hud-name" }, f.name),
        NF.h("span", { class: "hud-title" }, f.title.name)),
      NF.h("div", { class: "hud-hpwrap" }, refs.hpFill, refs.hpText),
      NF.h("div", { class: "hud-stats" },
        refs.atkStat,
        NF.h("span", { class: "hud-stat" }, attrName("def") + " ", refs.defVal),
        NF.h("span", { class: "hud-stat" }, attrName("spd") + " ", refs.spdVal)),
      NF.h("div", { class: "hud-gaugewrap" },
        NF.h("span", { class: "hud-gaugelabel" }, t("gauge_label")),
        NF.h("div", { class: "hud-gauge" }, refs.gaugeFill)),
      refs.buffs);
    return refs;
  }

  function applyHudState(refs, snap, instant) {
    if (!refs || !snap) return;
    var hpPct = snap.max_hp > 0 ? (snap.hp / snap.max_hp * 100) : 0;
    refs.hpFill.style.width = Math.max(0, Math.min(100, hpPct)) + "%";
    refs.hpText.textContent = snap.hp + " / " + snap.max_hp;
    refs.atkVal.textContent = String(snap.atk);
    refs.defVal.textContent = String(snap.def);
    refs.spdVal.textContent = String(snap.spd);
    refs._spd = snap.spd;
    refs._dead = snap.hp <= 0;
    var boosted = (snap.buffs || []).some(function (b) { return b.id === "last_stand"; });
    refs.atkStat.classList.toggle("atk-boost", boosted);
    // 行动槽：服务器快照对齐（行动消耗时快速回落；校正上升按刻节奏；瞬时模式直接应用）
    var gauge = Math.max(0, Math.min(100, snap.gauge));
    var prev = refs._gauge == null ? gauge : refs._gauge;
    refs._gauge = gauge;
    var duration;
    if (instant || gauge === prev) {
      duration = 0;
    } else if (gauge < prev) {
      duration = 150;
    } else {
      duration = tickDuration();
    }
    refs.gaugeFill.style.transition = duration > 0
      ? "width " + duration + "ms linear" : "none";
    refs.gaugeFill.style.width = gauge + "%";
    // buff 差量刷新：集合未变化时不重建，避免闪烁
    var sig = JSON.stringify(snap.buffs || []);
    if (refs._buffSig !== sig) {
      refs._buffSig = sig;
      NF.clear(refs.buffs);
      (snap.buffs || []).forEach(function (b) {
        var debuff = b.id === "poison" || b.id === "stun";
        refs.buffs.appendChild(NF.h("span", {
          class: "buff-chip" + (debuff ? " debuff" : "")
        }, b.name,
          NF.h("div", { class: "tip" },
            NF.h("div", { class: "tip-title" }, b.name),
            b.detail ? NF.h("div", { class: "tip-line" }, b.detail) : null,
            b.desc ? NF.h("div", { class: "tip-line muted" }, b.desc) : null)));
      });
    }
  }

  function applyBattleState(battleState, instant) {
    if (!battleState || !els.hud) return;
    applyHudState(els.hud.a, battleState.a, instant);
    applyHudState(els.hud.b, battleState.b, instant);
  }

  function renderControls() {
    if (!els.controls || !state.battle) return;
    NF.clear(els.controls);
    els.controls.appendChild(NF.h("button", { class: "btn", onclick: togglePlayback },
      state.playing ? t("playback_pause") : t("playback_play")));
    els.controls.appendChild(NF.h("button", { class: "btn", onclick: skipPlayback },
      t("playback_skip")));
    els.controls.appendChild(NF.h("select", {
      class: "speed-select", title: t("speed_label"),
      onchange: function (e) { state.speed = parseFloat(e.target.value) || 1; }
    }, SPEEDS.map(function (s) {
      return NF.h("option", {
        value: String(s),
        selected: s === state.speed ? "" : null
      }, s + "x");
    })));
  }

  function appendLogEntry(entry) {
    if (!els.logBox) return;
    var cls = "log-entry";
    if (entry.template === "tick_marker") cls += " log-round";
    else if (entry.template === "skill_proc") cls += " log-skill";
    else if (entry.template === "death" || entry.template === "poison_death") cls += " log-death";
    else if (entry.template === "victory" || entry.template === "draw") cls += " log-result";
    els.logBox.appendChild(NF.h("div", { class: cls }, entry.text));
    els.logBox.scrollTop = els.logBox.scrollHeight;
    if (entry.state) applyBattleState(entry.state, !!state.skipping);
  }

  function showResult() {
    if (!els.resultBox || !state.battle) return;
    NF.clear(els.resultBox);
    var r = state.battle.result;
    var bannerClass = r.draw ? "result-draw" : (r.winner_pos === 0 ? "result-a" : "result-b");
    var headline = r.draw ? t("draw_text") : fmt("winner_text", { name: r.winner });
    els.resultBox.appendChild(NF.h("div", { class: "result-banner " + bannerClass },
      NF.h("div", { class: "result-winner" }, headline),
      NF.h("div", { class: "result-summary" }, fmt("summary_text", {
        ticks: r.ticks,
        dmg_a: r.damage.a,
        dmg_b: r.damage.b
      }))));
  }

  function buildFooter() {
    return NF.h("footer", { class: "app-footer" },
      NF.h("div", null, t("determinism_note")),
      NF.h("div", null, t("tagline") + " · " + fmt("footer_version", { version: state.version })));
  }

  /* ---------------- 启动 ---------------- */

  function detectLang() {
    var nav = (navigator.language || "zh").toLowerCase();
    return nav.indexOf("en") === 0 ? "en" : "zh";
  }

  function init() {
    loadLangText(detectLang())
      .catch(function () { return loadLangText("zh"); })
      .then(function () { renderAll(); })
      .catch(function (e) {
        document.body.appendChild(NF.h("div", { class: "toast show" },
          String((e && e.message) || e)));
      });
  }

  init();
})();
