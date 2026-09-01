/* 名字竞技场前端应用逻辑。
 * 约定（见 AGENTS.md 2.2.3）：前端不硬编码任何面向用户的文案--
 * 所有用户可见文本均来自 GET /api/text（即 config/game/ui.json）。 */
(function () {
  "use strict";

  var SPEEDS = [0.5, 1, 2, 4];  // 回放倍速
  // 后端 live 文本中的实时数值占位符：\u0001 + 槽位序号（对应 link_calc 下标）
  var LIVE_MARK_RE = /\u0001(\d+)/g;

  var state = {
    version: "",
    text: {},
    msgDelay: 320,      // 每条战报的停顿时长（ms，来自 battle.json 的 playback 配置）
    actionEvery: 5,     // 普通播放：每 N 次角色行动插入一次较长停顿
    actionPause: 1600,  // 该较长停顿的时长（ms）
    fighters: null,     // [fighterApi, fighterApi]（按输入顺序）
    battle: null,       // /api/battle 响应
    shown: 0,           // 已展示的战报条数
    playing: false,
    timer: null,
    speed: 1,
    busy: false,
    simple: false,      // 简易显示模式（隐藏技能描述中的共鸣公式）
    stepMode: false,    // 单回合递进模式（手动点击，每次推进一次角色行动）
    actionsShown: 0,    // 普通播放中已展示的角色行动数（行动间大间隔用）
    skillRefs: null,    // {a: [{s, textNode, tipTextNode}], b: [...]} 实时刷新用
    lastBattleState: null,  // 最近一次应用的双方快照（重渲染后恢复实时数值）
    tickPos: 0
  };

  var els = {};

  /* ---------------- 工具 ---------------- */

  function t(key) {
    return state.text[key] != null ? state.text[key] : key;
  }

  /* 展示精度契约（AGENTS.md 2.1.5）：百分数 2 位小数，其余数值取整 */
  function fmtInt(v) {
    return String(Math.round(+v || 0));
  }

  function fmtPct(v) {
    return (+v * 100).toFixed(2) + "%";
  }

  function skillText(s) {
    return state.simple && s.text_simple != null ? s.text_simple : s.text;
  }

  /* 「【称号】名字」形式的全名（与后端战报口径一致，v1.2.1） */
  function displayName(f) {
    if (!f) return "";
    return f.title && f.title.name ? "【" + f.title.name + "】" + f.name : f.name;
  }

  function fmt(key, map) {
    var s = t(key);
    Object.keys(map || {}).forEach(function (k) {
      s = s.split("{" + k + "}").join(map[k]);
    });
    return s;
  }

  /* 每个技能一种专属颜色（由技能 id 确定性散列出 hue，卡牌与战报共用） */
  var skillColorCache = {};
  function skillColor(id) {
    if (skillColorCache[id]) return skillColorCache[id];
    var h = 0;
    for (var i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
    var color = "hsl(" + (h % 360) + ", 74%, 68%)";
    skillColorCache[id] = color;
    return color;
  }

  /* 属性条重新量化：投掷区间线性映射到 8%~92%——永不空条、永不全满 */
  function barPct(v, min, max) {
    var span = Math.max(1e-6, max - min);
    var f = Math.max(0, Math.min(1, (+v - min) / span));
    return 8 + f * 84;
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

  function apiText() {
    return NF.fetchJSON("/api/text");
  }

  function apiFighter(name) {
    return NF.fetchJSON("/api/fighter?name=" + encodeURIComponent(name));
  }

  function apiBattle(a, b) {
    return NF.fetchJSON("/api/battle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ a: a, b: b })
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
        state.lastBattleState = null;
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
        state.lastBattleState = null;
        renderArena();
        renderBattlePanel();
        startPlayback();
      })
      .catch(function (e) { showError(errorText(e)); })
      .then(function () { setBusy(false); });
  }

  function loadText() {
    return apiText().then(function (data) {
      state.version = data.version || "";
      state.text = data.ui || {};
      var pb = data.playback || {};
      state.msgDelay = Math.max(16, +pb.message_delay_ms || 320);
      state.actionEvery = Math.max(1, +pb.action_pause_every || 5);
      state.actionPause = Math.max(0, +pb.action_pause_ms || 0);
      document.title = t("app_title");
    });
  }

  /* ---------------- 战报逐条回放（每条消息停顿 msgDelay/speed 毫秒） ----------------
   * v0.10.0 起回放按「条」推进：每条战报展示后停顿一段可配置的时间
   * （battle.json 的 playback.message_delay_ms ÷ 倍速）；跨刻时行动槽
   * 按每刻推进量顺次补进，仍与服务器快照逐条对齐校正。
   * v1.0.0：普通模式下每 actionEvery 次角色行动（以「发起攻击」宣告计）
   * 额外停顿 actionPause 毫秒；单回合递进模式下不自动播放，
   * 由「递进一步」按钮每次推进一次角色行动。 */

  function stopPlayback() {
    state.playing = false;
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }
  }

  function msgDuration() {
    return Math.max(16, state.msgDelay / state.speed);
  }

  function startPlayback() {
    stopPlayback();
    state.tickPos = 0;
    state.actionsShown = 0;
    state.playing = true;
    renderControls();
    if (!state.stepMode) scheduleMsg();
  }

  function scheduleMsg() {
    // 上一条是「发起攻击」宣告且命中行动间隔节拍时，插入较长停顿
    var log = state.battle ? state.battle.log : [];
    var prev = state.shown > 0 ? log[state.shown - 1] : null;
    var delay = msgDuration();
    if (prev && prev.template === "attack_start"
        && state.actionsShown % state.actionEvery === 0
        && state.actionPause > 0) {
      delay = Math.max(delay, state.actionPause / state.speed);
    }
    state.timer = setTimeout(stepMsg, delay);
  }

  function stepMsg() {
    if (!state.playing || !state.battle) return;
    var log = state.battle.log;
    if (state.shown >= log.length) {
      finishPlayback();
      return;
    }
    var entry = log[state.shown];
    if (entry.tick > state.tickPos) {
      advanceGauges(entry.tick - state.tickPos);
      state.tickPos = entry.tick;
    }
    appendLogEntry(entry);
    state.shown++;
    if (entry.template === "attack_start") state.actionsShown++;
    if (state.shown >= log.length) {
      finishPlayback();
      return;
    }
    if (state.stepMode) {
      renderControls();
      return;
    }
    scheduleMsg();
  }

  /* 单回合递进：展示到下一次角色行动为止（普通攻击宣告标志一次行动）。
   * 若下一条就是行动宣告，则完整展示该行动、停在再下一次行动之前；
   * 若处于回合标记等过渡段，则推进到首个完整行动结束。 */
  function stepForward() {
    if (!state.battle) return;
    stopPlayback();
    state.playing = true;
    var log = state.battle.log;
    if (state.shown >= log.length) {
      finishPlayback();
      return;
    }
    var seenAction = false;
    while (state.shown < log.length) {
      var entry = log[state.shown];
      if (seenAction && entry.template === "attack_start") break;
      if (entry.tick > state.tickPos) {
        advanceGauges(entry.tick - state.tickPos);
        state.tickPos = entry.tick;
      }
      appendLogEntry(entry);
      state.shown++;
      if (entry.template === "attack_start") seenAction = true;
    }
    if (state.shown >= log.length) {
      finishPlayback();
      return;
    }
    renderControls();
  }

  /* 客户端行动槽逐刻推进（每刻推进 gauge_pct_gain%），与服务器快照对齐校正 */
  function advanceGauges(deltaTicks) {
    ["a", "b"].forEach(function (side) {
      var refs = els.hud && els.hud[side];
      if (!refs || refs._dead || !refs._gain) return;
      var g = Math.min(100, (refs._gaugePct || 0) + refs._gain * (deltaTicks || 1));
      refs._gaugePct = g;
      refs.gaugeFill.style.transition = "width " + Math.max(120, msgDuration()) + "ms linear";
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
      if (state.shown >= log.length) return;
      state.playing = true;
      renderControls();
      if (!state.stepMode) scheduleMsg();
    }
  }

  /* 单回合递进模式开关：开启后停止自动播放，改由手动递进 */
  function toggleStepMode(on) {
    if (state.stepMode === !!on) return;
    state.stepMode = !!on;
    try { localStorage.setItem("nf_step", on ? "1" : "0"); } catch (e) { /* 忽略 */ }
    if (on) stopPlayback();
    renderControls();
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
    var simpleBox = NF.h("input", {
      class: "simple-box", type: "checkbox",
      checked: state.simple ? "" : null,
      onchange: function (e) { toggleSimple(e.target.checked); }
    });
    return NF.h("header", { class: "app-header" },
      NF.h("div", { class: "lang-row" },
        NF.h("label", { class: "simple-toggle", title: t("simple_mode_title") },
          simpleBox, NF.h("span", null, t("simple_mode_label"))),
        NF.h("a", { class: "lang-btn", href: "/power.html" }, t("power_link")),
        NF.h("a", { class: "lang-btn", href: "/editor.html" }, t("editor_link"))),
      NF.h("h1", { class: "app-title" }, t("app_title")),
      NF.h("p", { class: "app-subtitle" }, t("app_subtitle"))
    );
  }

  /* 简易显示模式：只重渲染卡牌（技能文本所在处），不影响正在进行的对战回放 */
  function toggleSimple(on) {
    if (state.simple === !!on) return;
    state.simple = !!on;
    try { localStorage.setItem("nf_simple", on ? "1" : "0"); } catch (e) { /* 忽略 */ }
    renderArena();
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
    state.skillRefs = { a: [], b: [] };
    if (!state.fighters) {
      els.arena.appendChild(emptyCard());
      els.arena.appendChild(emptyCard());
      return;
    }
    els.arena.appendChild(fighterCard(state.fighters[0], "a"));
    els.arena.appendChild(fighterCard(state.fighters[1], "b"));
    // 重渲染（如切换简易模式）后，按最近快照恢复实时技能数值
    if (state.lastBattleState) {
      updateLiveSkills(state.lastBattleState.a, state.lastBattleState.b);
    }
  }

  function emptyCard() {
    return NF.h("div", { class: "empty-card" },
      NF.h("div", { class: "empty-title" }, t("empty_card_title")),
      NF.h("div", { class: "empty-hint" }, t("empty_card_hint")));
  }

  function fighterCard(f, side) {
    var statRows = f.attributes.map(function (a) {
      // v0.10.0：数值均为引擎真实值；属性条映射到 8%~92%（永不空/满）
      var pct = barPct(a.value, a.min, a.max);
      var valueText = a.format === "percent"
        ? fmtPct(a.value / 100) : fmtInt(a.value);
      var icon = a.emoji ? a.emoji + " " : "";
      return NF.h("div", { class: "stat-row" },
        NF.h("span", { class: "stat-name" }, icon + a.name),
        NF.h("span", { class: "stat-value" }, valueText),
        NF.h("div", { class: "stat-bar" },
          NF.h("div", { class: "stat-fill " + a.id, style: { width: pct + "%" } })));
    });

    var skillNodes = f.skills.map(function (s) {
      var textNode = s.text ? NF.h("div", { class: "skill-text" }, skillText(s)) : null;
      var tipTextNode = s.text ? NF.h("div", { class: "tip-line" }, skillText(s)) : null;
      // 登记共鸣技能的文本节点：对战中按快照逐刻刷新实时数值（每技能至多两个占位）
      if (s.live_text && state.skillRefs) {
        state.skillRefs[side].push({ s: s, textNode: textNode, tipTextNode: tipTextNode });
      }
      return NF.h("div", { class: "skill-chip" },
        NF.h("div", { class: "skill-name" },
          NF.h("span", { class: "skill-name-text", style: { color: skillColor(s.id) } }, s.name),
          s.mastery_text ? NF.h("span", { class: "skill-mastery" }, s.mastery_text) : null),
        textNode,
        s.flavor ? NF.h("div", { class: "skill-desc" }, s.flavor) : null,
        NF.h("div", { class: "tip" },
          NF.h("div", { class: "tip-title" }, s.name),
          s.mastery_text ? NF.h("div", { class: "tip-line muted" }, s.mastery_text) : null,
          tipTextNode,
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

  /* ---------- 战斗 HUD：HP / 六维属性 / 行动槽 / buff 实时渲染 ----------
   * 属性为引擎真实值（v0.10.0），随每条战报的快照刷新，变化时高亮提示。 */

  function buildHud(f, side) {
    var attrOf = function (id) {
      return f.attributes.filter(function (a) { return a.id === id; })[0];
    };
    var attrIcon = function (id) {
      var found = attrOf(id);
      if (found && found.emoji) return found.emoji + " ";
      var name = found ? found.name : id.toUpperCase();
      return name + " ";
    };
    var refs = {};
    refs.hpFill = NF.h("div", { class: "hud-hpfill" });
    refs.hpText = NF.h("span", { class: "hud-hptext" }, "");
    refs.atkVal = NF.h("b", null, "");
    refs.defVal = NF.h("b", null, "");
    refs.spdVal = NF.h("b", null, "");
    refs.critVal = NF.h("b", null, "");
    refs.dodgeVal = NF.h("b", null, "");
    refs.atkStat = NF.h("span", { class: "hud-stat", title: attrNameOf(f, "atk") }, attrIcon("atk"), refs.atkVal);
    refs.gaugeFill = NF.h("div", { class: "hud-gaugefill" });
    refs.gaugeText = NF.h("span", { class: "hud-gaugeval" }, "");
    refs.buffs = NF.h("div", { class: "hud-buffs" });
    refs.root = NF.h("div", { class: "hud side-" + side },
      NF.h("div", { class: "hud-head" },
        NF.h("span", { class: "hud-name" }, f.name),
        NF.h("span", { class: "hud-title" }, f.title.name)),
      NF.h("div", { class: "hud-hpwrap" }, refs.hpFill, refs.hpText),
      NF.h("div", { class: "hud-stats" },
        refs.atkStat,
        NF.h("span", { class: "hud-stat", title: attrNameOf(f, "def") }, attrIcon("def"), refs.defVal),
        NF.h("span", { class: "hud-stat", title: attrNameOf(f, "spd") }, attrIcon("spd"), refs.spdVal),
        NF.h("span", { class: "hud-stat", title: attrNameOf(f, "crit") }, attrIcon("crit"), refs.critVal),
        NF.h("span", { class: "hud-stat", title: attrNameOf(f, "dodge") }, attrIcon("dodge"), refs.dodgeVal)),
      NF.h("div", { class: "hud-gaugewrap" },
        NF.h("span", { class: "hud-gaugelabel" }, t("gauge_label")),
        NF.h("div", { class: "hud-gauge" }, refs.gaugeFill),
        refs.gaugeText),
      refs.buffs);
    return refs;
  }

  function attrNameOf(f, id) {
    var found = f.attributes.filter(function (a) { return a.id === id; })[0];
    return found ? found.name : id.toUpperCase();
  }

  /* 数值刷新：文本变化时短暂高亮（属性实时变化的可视反馈） */
  function setStat(el, text) {
    if (el.textContent === text) return;
    el.textContent = text;
    el.classList.remove("bump");
    void el.offsetWidth;
    el.classList.add("bump");
  }

  function applyHudState(refs, snap, instant) {
    if (!refs || !snap) return;
    var hpPct = snap.max_hp > 0 ? (snap.hp / snap.max_hp * 100) : 0;
    refs.hpFill.style.width = Math.max(0, Math.min(100, hpPct)) + "%";
    setStat(refs.hpText, fmtInt(snap.hp) + " / " + fmtInt(snap.max_hp));
    setStat(refs.atkVal, fmtInt(snap.atk));
    setStat(refs.defVal, fmtInt(snap.def));
    setStat(refs.spdVal, fmtInt(snap.spd));
    setStat(refs.critVal, fmtPct(snap.crit / 100));
    setStat(refs.dodgeVal, fmtPct(snap.dodge / 100));
    refs._dead = snap.hp <= 0;
    var boosted = (snap.buffs || []).some(function (b) { return b.id === "last_stand"; });
    refs.atkStat.classList.toggle("atk-boost", boosted);
    // 行动槽：真实引擎值 + 百分比条（服务器快照对齐；瞬时模式直接应用）
    var th = snap.gauge_threshold || 100;
    var gauge = Math.max(0, Math.min(+snap.gauge || 0, th));
    var gp = snap.gauge_pct != null ? +snap.gauge_pct
      : Math.min(100, gauge / th * 100);
    refs._gain = snap.gauge_pct_gain != null ? +snap.gauge_pct_gain : 0;
    setStat(refs.gaugeText, fmtInt(gauge) + "/" + fmtInt(th));
    var prev = refs._gaugePct == null ? gp : refs._gaugePct;
    refs._gaugePct = gp;
    var duration;
    if (instant || gp === prev) {
      duration = 0;
    } else if (gp < prev) {
      duration = 150;
    } else {
      duration = Math.max(120, msgDuration());
    }
    refs.gaugeFill.style.transition = duration > 0
      ? "width " + duration + "ms linear" : "none";
    refs.gaugeFill.style.width = gp + "%";
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
    state.lastBattleState = battleState;
    applyHudState(els.hud.a, battleState.a, instant);
    applyHudState(els.hud.b, battleState.b, instant);
    updateLiveSkills(battleState.a, battleState.b);
  }

  /* ---------- 对战实时技能数据：按双方快照重算共鸣最终值 ---------- */

  function liveSnapValue(snap, vid) {
    return snap && snap[vid] != null ? +snap[vid] : 0;
  }

  /* 与引擎 resonance_coeff + apply_resonance 完全一致：
     最终值 = base + 变量式 × coeff，再按字段上下限截断。
     变量式：own 己方单值 / enemy 敌方单值 / difference 差值 / sum 并值。 */
  function liveFinalValue(lc, selfSnap, enemySnap) {
    var expr;
    if (lc.mode === "difference" || lc.mode === "sum") {
      var own = liveSnapValue(selfSnap, lc.variable);
      var other = liveSnapValue(enemySnap, lc.against);
      expr = lc.mode === "difference" ? own - other : own + other;
    } else if (lc.mode === "enemy") {
      expr = liveSnapValue(enemySnap, lc.variable);
    } else {
      expr = liveSnapValue(selfSnap, lc.variable);
    }
    var v = lc.base + expr * lc.coeff;
    var lo = lc.clamp ? lc.clamp[0] : null;
    var hi = lc.clamp ? lc.clamp[1] : null;
    if (lo != null && v < lo) v = lo;
    if (hi != null && v > hi) v = hi;
    if (lc.fmt === "turns") return String(Math.max(1, Math.round(v)));
    if (lc.fmt === "num") return fmtInt(v);
    return fmtPct(v);
  }

  /* live 文本的实时数值占位符为「\u0001 + 槽位序号」，序号对应 link_calc 下标。
     必须按序号取值：模板参数顺序与共鸣槽位顺序可能不一致（如壁垒的
     门槛/效果值），按位置填充会交叉错位（v1.2.0 修复）。 */
  function fillLiveText(tmpl, calcs, selfSnap, enemySnap) {
    var out = "", last = 0, m;
    var re = LIVE_MARK_RE;
    re.lastIndex = 0;
    while ((m = re.exec(tmpl)) !== null) {
      var idx = parseInt(m[1], 10);
      out += tmpl.slice(last, m.index);
      out += (calcs && calcs[idx])
        ? liveFinalValue(calcs[idx], selfSnap, enemySnap) : "";
      last = m.index + m[0].length;
    }
    return out + tmpl.slice(last);
  }

  function updateLiveSkills(snapA, snapB) {
    if (!state.skillRefs || !state.battle) return;
    [["a", snapA, snapB], ["b", snapB, snapA]].forEach(function (row) {
      (state.skillRefs[row[0]] || []).forEach(function (ref) {
        var s = ref.s;
        if (!s.live_text || !ref.textNode) return;
        var tmpl = state.simple && s.live_text_simple ? s.live_text_simple : s.live_text;
        var text = fillLiveText(tmpl, s.link_calc, row[1], row[2]);
        if (ref.textNode.textContent !== text) {
          ref.textNode.textContent = text;
          if (ref.tipTextNode) ref.tipTextNode.textContent = text;
          var chip = ref.textNode.parentNode;
          if (chip && chip.classList) {
            chip.classList.remove("live-flash");
            void chip.offsetWidth;  // 重新触发闪烁动画
            chip.classList.add("live-flash");
          }
        }
      });
    });
  }

  function renderControls() {
    if (!els.controls || !state.battle) return;
    NF.clear(els.controls);
    els.controls.appendChild(NF.h("button", { class: "btn", onclick: togglePlayback },
      state.playing && !state.stepMode ? t("playback_pause") : t("playback_play")));
    els.controls.appendChild(NF.h("button", { class: "btn", onclick: stepForward },
      t("playback_step")));
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
    var stepBox = NF.h("input", {
      class: "simple-box", type: "checkbox",
      checked: state.stepMode ? "" : null,
      onchange: function (e) { toggleStepMode(e.target.checked); }
    });
    els.controls.appendChild(NF.h("label", {
      class: "simple-toggle", title: t("step_mode_title")
    }, stepBox, NF.h("span", null, t("step_mode_label"))));
  }

  /* ---------- 战报富文本渲染 ----------
   * 后端为每条战报提供 rich 段列表：阵营名（红/蓝加粗）、技能名（各自配色
   * 加粗）、伤害（红）、治疗（绿）、普通文本；技能使用行后附个性化描述。
   * v1.2.1 起角色名自带【称号】，且名字头顶挂一条无数字的简易血条：
   * 白=当前生命，红=本次掉血，绿=本次回血，灰=空。
   * 血条数据 = 本条战报快照与上一条快照的生命差值（逐条/跳过/递进模式一致）。 */

  function nameWidget(text, colorClass, hp) {
    var bar = NF.h("div", { class: "nm-hp" });
    if (hp && hp.max > 0) {
      var cur = Math.max(0, Math.min(hp.cur, hp.max));
      var prev = Math.max(0, Math.min(hp.prev, hp.max));
      var pct = function (v) { return (v / hp.max * 100) + "%"; };
      if (cur < prev) {          // 掉血：白=剩余，红=本次损失
        bar.appendChild(NF.h("i", { class: "hp-cur", style: { width: pct(cur) } }));
        bar.appendChild(NF.h("i", { class: "hp-loss", style: { width: pct(prev - cur) } }));
      } else if (cur > prev) {   // 回血：白=原有，绿=本次回复
        bar.appendChild(NF.h("i", { class: "hp-cur", style: { width: pct(prev) } }));
        bar.appendChild(NF.h("i", { class: "hp-gain", style: { width: pct(cur - prev) } }));
      } else {
        bar.appendChild(NF.h("i", { class: "hp-cur", style: { width: pct(cur) } }));
      }
    }
    return NF.h("span", { class: "nmw" }, bar,
      NF.h("b", { class: "nm " + colorClass }, text));
  }

  function richSeg(seg, hpInfo) {
    if (seg.k === "name-a" || seg.k === "name-b") {
      return nameWidget(seg.t, seg.k === "name-a" ? "nm-a" : "nm-b",
        hpInfo ? hpInfo[seg.k.slice(5)] : null);
    }
    if (seg.k === "skill") {
      return NF.h("b", { class: "sk", style: { color: skillColor(seg.id) } }, seg.t);
    }
    if (seg.k === "dmg") return NF.h("span", { class: "dmg" }, seg.t);
    if (seg.k === "heal") return NF.h("span", { class: "heal" }, seg.t);
    return document.createTextNode(seg.t);
  }

  /* 技能使用行（skill_proc）之后附该技能实例的个性化描述一行 */
  function appendSkillDesc(box, entry) {
    var side = null;
    (entry.rich || []).some(function (seg) {
      if (seg.k === "name-a") { side = 0; return true; }
      if (seg.k === "name-b") { side = 1; return true; }
      return false;
    });
    var sid = entry.params && entry.params.skill && entry.params.skill.id;
    if (side == null || !sid || !state.fighters) return;
    var find = function (idx) {
      var list = state.fighters[idx] && state.fighters[idx].skills;
      return list ? list.filter(function (x) { return x.id === sid; })[0] : null;
    };
    var s = find(side) || find(1 - side);
    if (!s) return;
    box.appendChild(NF.h("div", { class: "log-desc" }, skillText(s)));
  }

  function appendLogEntry(entry) {
    if (!els.logBox) return;
    var cls = "log-entry";
    if (entry.template === "tick_marker") cls += " log-round";
    else if (entry.template === "skill_proc") cls += " log-skill";
    else if (entry.template === "death" || entry.template === "poison_death") cls += " log-death";
    else if (entry.template === "victory" || entry.template === "draw") cls += " log-result";
    var box = NF.h("div", { class: cls });
    if (entry.rich && entry.rich.length) {
      // 本条快照相对上一条快照的生命差值 -> 角色名头顶血条的红/绿段；
      // state.lastBattleState 在本函数末尾才被本条快照覆盖，此处即「上一条」
      var hpInfo = null;
      if (entry.state) {
        hpInfo = {};
        ["a", "b"].forEach(function (side) {
          var cur = entry.state[side];
          var prev = (state.lastBattleState && state.lastBattleState[side]) || cur;
          hpInfo[side] = { cur: +cur.hp, prev: +prev.hp, max: +cur.max_hp };
        });
      }
      entry.rich.forEach(function (seg) { box.appendChild(richSeg(seg, hpInfo)); });
    } else {
      box.appendChild(document.createTextNode(entry.text || ""));
    }
    if (entry.template === "skill_proc") appendSkillDesc(box, entry);
    els.logBox.appendChild(box);
    els.logBox.scrollTop = els.logBox.scrollHeight;
    if (entry.state) applyBattleState(entry.state, !!state.skipping);
  }

  function showResult() {
    if (!els.resultBox || !state.battle) return;
    NF.clear(els.resultBox);
    var r = state.battle.result;
    var bannerClass = r.draw ? "result-draw" : (r.winner_pos === 0 ? "result-a" : "result-b");
    var headline = r.draw ? t("draw_text")
      : fmt("winner_text", { name: displayName(state.fighters[r.winner_pos]) || r.winner });
    els.resultBox.appendChild(NF.h("div", { class: "result-banner " + bannerClass },
      NF.h("div", { class: "result-winner" }, headline),
      NF.h("div", { class: "result-summary" }, fmt("summary_text", {
        ticks: r.ticks,
        dmg_a: fmtInt(r.damage.a),
        dmg_b: fmtInt(r.damage.b)
      }))));
  }

  function buildFooter() {
    return NF.h("footer", { class: "app-footer" },
      NF.h("div", null, t("determinism_note")),
      NF.h("div", null, t("tagline") + " · " + fmt("footer_version", { version: state.version })));
  }

  /* ---------------- 启动 ---------------- */

  function init() {
    try {
      state.simple = localStorage.getItem("nf_simple") === "1";
      state.stepMode = localStorage.getItem("nf_step") === "1";
    } catch (e) { /* 忽略 */ }
    loadText()
      .then(function () { renderAll(); })
      .catch(function (e) {
        document.body.appendChild(NF.h("div", { class: "toast show" },
          String((e && e.message) || e)));
      });
  }

  init();
})();
