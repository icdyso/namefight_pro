/* 可视化编辑器（v3.0.0）：节点画布（技能图 + 状态效果图）+ 六大配置结构化表单。
 * 零依赖原生 JS，条件/原子/结构/状态的可用参数全部来自 GET /api/schema
 * （引擎自描述），本文件不硬编码任何效果类型——界面管理文案按 AGENTS.md
 * 惯例（编辑器例外）内联于本文件。
 * v3 画布语言：触发（时机）→ 条件（判断，出边分 pass/fail 分支）→
 * 原子（最小效果）/ 结构（loop 循环）。
 * 数据流：GET /api/config -> state.files（工作副本）-> 表单/画布双向绑定
 * -> POST /api/config/preview（草稿试运行）/ /api/config/save（保存热重载）。 */
(function () {
  "use strict";

  var NODE_W = 200;   // 画布节点固定宽度（端口坐标计算依据，与 CSS 保持一致）
  var PORT_Y = 18;    // 通过（pass）端口相对节点顶部的纵向偏移（与 CSS 匹配）
  var FAIL_Y = 42;    // 失败（fail）端口相对节点顶部的纵向偏移（与 CSS 匹配）
  var structBox = {}; // loop 容器的包围盒（rebuildCanvas / 拖动时重算，端口坐标共用）
  var nodeEls = {};   // 节点 id -> 已渲染元素（自适应包围盒的真实尺寸来源）

  /* 全局状态：schema = 引擎自描述；files = 六个配置文件的工作副本；
   * baseline = 已生效版本（脏检测与还原的基准）；view = 画布视图变换
   * （x/y 平移 + s 缩放）；graphOwner = 当前画布编辑的图归属
   * （技能 或 battle 页的状态定义）。 */
  var state = {
    text: {},                    // /api/text 的界面文案（本页基本不用，保留）
    version: "",                 // 当前配置版本号
    schema: null,                // /api/schema 引擎自描述注册表
    files: null,                 // 工作副本：{system,attributes,skills,titles,battle,ui}
    baseline: null,              // 已生效副本（深拷贝，用于脏检测与还原）
    tab: "skills",               // 当前页签：skills/attributes/titles/battle/texts/system
    jsonMode: false,             // JSON 源码模式开关
    graphOwner: { kind: "skill" },  // 画布归属：{kind:"skill"} 或 {kind:"status", id}
    selSkill: null,              // 当前编辑的技能 id
    selStatus: null,             // 当前编辑的状态 id（battle 页）
    selNode: null,               // 画布中选中的节点 id
    selEdge: null,               // 画布中选中的边下标（edges 数组序号）
    view: { x: 40, y: 30, s: 0.85 },  // 画布视图：平移像素 + 缩放比例
    busy: false,                 // 请求进行中（按钮防抖）
    status: null,                // 底部状态栏文案 {text, err}
    preview: null,               // 最近一次试运行的战报（渲染在底部预览区）
  };

  /* ---------- 基础工具 ---------- */

  function h(tag, attrs) { return NF.h.apply(null, arguments); }  // DOM 快捷构造（见 framework.js）
  function clear(el) { return NF.clear(el); }                     // 清空容器
  function deep(v) { return JSON.parse(JSON.stringify(v)); }      // 深拷贝（配置对象）
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }  // 深比较

  function setStatus(text, isErr) {
    /* 更新底部状态栏文案（err=true 时红色示警）。 */
    state.status = text ? { text: text, err: !!isErr} : null;
    var el = NF.qs("#ed-status");
    if (el) {
      el.textContent = text || "";
      el.className = "ed-status" + (isErr ? " err" : "");
    }
  }

  var KIND_CN = { trigger: "触发", condition: "条件", op: "效果", struct: "结构" };

  function nodeLabel(node) {
    /* 节点短标签：取配置 stats 的 lbl_<类型>（数据驱动，无硬编码）。 */
    var key = "lbl_" + node.type;
    return state.files.skills.stats[key] || node.type;
  }

  function statusName(sid) {
    /* 状态显示名（下拉框 / 节点参数摘要用）。 */
    return (state.files.battle.statuses[sid] || {}).name || sid;
  }

  function fmtParams(node) {
    /* 节点参数摘要（画布节点下方的一行文本）。 */
    var out = [];
    var params = node.params || {};
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      var text = typeof v === "number" ? (Math.round(v * 10000) / 10000) : String(v);
      out.push(k + " = " + text);
    });
    return out.length ? out.join(" · ") : "（无参数）";
  }

  /* ---------- 数据访问 ---------- */

  function skillList() { return state.files.skills.skills; }   // 技能池数组
  function curSkill() {
    /* 当前编辑的技能对象（按 selSkill 查找）。 */
    var list = skillList();
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === state.selSkill) return list[i];
    }
    return null;
  }
  function curStatusDef() {
    /* 当前编辑的状态定义对象（battle 页，按 selStatus 查找）。 */
    return state.files.battle.statuses[state.selStatus] || null;
  }
  function graph() {
    /* 当前画布编辑的图（技能 effect 或状态 effects；缺结构时补齐）。 */
    var g = null;
    if (state.graphOwner.kind === "status") {
      var def = curStatusDef();
      if (!def) return null;
      if (!def.effects || !Array.isArray(def.effects.nodes)) def.effects = { nodes: [], edges: [] };
      if (!Array.isArray(def.effects.edges)) def.effects.edges = [];
      return def.effects;
    }
    var sk = curSkill();
    if (!sk) return null;
    if (!sk.effect || !Array.isArray(sk.effect.nodes)) sk.effect = { nodes: [], edges: [] };
    if (!Array.isArray(sk.effect.edges)) sk.effect.edges = [];
    return sk.effect;
  }
  function graphHooks() {
    /* 当前画布可用的触发钩子列表（技能钩子 / 状态钩子）。 */
    if (!state.schema) return [];
    return state.graphOwner.kind === "status"
      ? state.schema.status_hooks : state.schema.hooks;
  }
  function graphOwnerLabel() {
    /* 画布归属的显示名（技能名 / 状态名）。 */
    if (state.graphOwner.kind === "status") return "状态：" + statusName(state.graphOwner.id);
    var sk = curSkill();
    return sk ? "技能：" + sk.name : "技能";
  }
  function nodeById(g, id) {
    /* 按节点 id 查找节点对象。 */
    for (var i = 0; i < g.nodes.length; i++) if (g.nodes[i].id === id) return g.nodes[i];
    return null;
  }
  function fileOfTabKey(tab) {
    /* 页签 -> 配置文件键（texts 页签编辑 ui.json）。 */
    return { skills: "skills", attributes: "attributes", titles: "titles",
             battle: "battle", texts: "ui", system: "system" }[tab];
  }
  function fileOfTab() { return fileOfTabKey(state.tab); }
  function fileDirty(key) { return !same(state.files[key], state.baseline[key]); }  // 单文件脏检测

  /* ---------- 参数规格（schema 驱动） ---------- */

  function specList(node) {
    /* 节点的参数规格列表：条件/原子/结构取注册表声明；apply_status 额外拼接
     * 所选状态定义的数值参数（编辑器因此无需理解任何具体状态）。 */
    if (!state.schema) return [];
    var reg;
    if (node.kind === "condition") reg = state.schema.conditions[node.type];
    else if (node.kind === "struct") reg = state.schema.structs[node.type];
    else reg = state.schema.ops[node.type];
    var base = reg ? reg.params.slice() : [];
    if (node.kind === "op" && node.type === "apply_status") {
      var sid = node.params && node.params.status;
      var sdef = state.schema.statuses[sid];
      if (sdef) base = base.concat(sdef.params);
    }
    return base;
  }

  function defaultParamValue(ps) {
    /* 新建节点时参数的默认值（按参数类型给一个安全初值）。 */
    switch (ps.kind) {
      case "pct": return 0.5;                                   // 百分数（0~1 分数）
      case "float": return ps.unit ? 100 : 1;                   // 绝对数值按量纲取整百
      case "int": case "turns": return ps.kind === "turns" ? 3 : 1;
      case "bool": return true;
      case "text": return "";
      case "enum": return ps.options ? ps.options[0] : "";
      default: return 0;
    }
  }

  /* ---------- 渲染骨架 ---------- */

  function renderAll() {
    /* 全量重绘：结构变化（增删节点/切换页签/保存后）时调用；
     * 输入框的双向绑定只改数据不重绘，避免焦点丢失。 */
    var root = NF.qs("#app");
    clear(root);
    root.appendChild(h("div", { class: "ed-wrap" }, [
      renderHeader(),                       // 顶栏（导航 / 版本 / 模式切换）
      h("div", { class: "ed-tabs" }, renderTabs()),   // 页签条（脏标记 ●）
      h("div", { class: "ed-main" }, [renderBody()]), // 主体（画布页 = 列表+画布+属性面板）
      renderPreview(),                      // 试运行战报预览区
      renderFooter(),                       // 底部操作栏（试运行 / 保存 / 还原）
    ]));
    if ((state.tab === "skills" || state.tab === "battle") && !state.jsonMode) {
      rebuildCanvas();                      // 画布挂载后构建
    }
    if (state.status) setStatus(state.status.text, state.status.err);
  }

  function renderHeader() {
    /* 顶栏：返回主页 / 真战力入口 + 标题与版本 + JSON 模式切换。 */
    return h("header", { class: "ed-header" }, [
      h("a", { class: "ed-btn", href: "/" }, "返回对战"),
      h("a", { class: "ed-btn", href: "/power.html" }, "真战力"),
      h("span", { class: "ed-title" }, "可视化编辑器"),
      h("span", { class: "ed-version" }, "v" + state.version),
      h("span", { class: "ed-spacer" }),
      h("button", { class: "ed-btn", onclick: function () {
        state.jsonMode = !state.jsonMode;
        renderAll();
      } }, state.jsonMode ? "返回表单模式" : "JSON 源码模式"),
    ]);
  }

  var TAB_DEFS = [
    ["skills", "技能"], ["attributes", "属性"], ["titles", "称号"],
    ["battle", "战斗"], ["texts", "文案"], ["system", "系统"],
  ];

  function renderTabs() {
    /* 页签条：当前页签高亮；脏文件名后缀 ●。 */
    return TAB_DEFS.map(function (def) {
      var dirty = fileOfTabKey(def[0]);
      return h("button", {
        class: "ed-tab" + (state.tab === def[0] ? " on" : ""),
        onclick: function () {
          state.tab = def[0];
          state.selNode = null;
          state.selEdge = null;
          state.graphOwner = def[0] === "battle" ? { kind: "status", id: state.selStatus }
                                                 : { kind: "skill" };
          renderAll();
        },
      }, def[1] + (fileDirty(dirty) ? " ●" : ""));
    });
  }

  function renderBody() {
    /* 主体内容：JSON 模式 = 六文件源码；表单模式按页签分发。 */
    if (state.jsonMode) return [renderJsonMode()];
    switch (state.tab) {
      case "skills": return [renderSkillsTab()];
      case "attributes": return [renderAttributesTab()];
      case "titles": return [renderTitlesTab()];
      case "battle": return [renderBattleTab()];
      case "texts": return [renderTextsTab()];
      case "system": return [renderSystemTab()];
    }
    return [];
  }

  function renderJsonMode() {
    /* JSON 源码模式：六个文件的可编辑文本域（合法 JSON 才写回工作副本）。 */
    var wrap = h("div", { class: "ed-panel" });
    CONFIG_KEYS.forEach(function (key) {
      var box = h("textarea", { class: "ed-json", spellcheck: "false" });
      box.value = JSON.stringify(state.files[key], null, 2);
      box.addEventListener("input", function () {
        try {
          state.files[key] = JSON.parse(box.value);
          box.style.borderColor = "";
        } catch (e) {
          box.style.borderColor = "#ff9a8a";   // 语法错误：红框提示，不写回
        }
      });
      wrap.appendChild(h("h4", null, key + ".json"));
      wrap.appendChild(box);
    });
    return wrap;
  }

  var CONFIG_KEYS = ["system", "attributes", "skills", "titles", "battle", "ui"];

  /* ---------- 技能页（三栏：列表 + 画布 + 属性面板） ---------- */

  function renderSkillsTab() {
    /* 技能页：左栏技能列表，中间节点画布（调色板 + 画布），右栏属性面板。 */
    var list = skillList();
    if (!state.selSkill && list.length) state.selSkill = list[0].id;
    if (!curSkill() && list.length) state.selSkill = list[0].id;
    state.graphOwner = { kind: "skill" };

    var side = h("div", { class: "ed-side" }, [
      h("div", { class: "ed-side-head" }, [
        h("span", null, "技能池（" + list.length + "）"),
        h("span", { class: "ed-spacer" }),
        h("button", { class: "ed-btn", onclick: addSkill }, "＋ 新建"),
      ]),
      h("div", { class: "ed-list" }, list.map(function (sk) {
        return h("div", {
          class: "ed-item" + (sk.id === state.selSkill ? " on" : ""),
          onclick: function () { state.selSkill = sk.id; state.selNode = null; renderAll(); },
        }, [
          h("span", { class: "grow" }, sk.name),
          h("span", { class: "sub" }, sk.id),
          h("button", { class: "ed-btn warn", onclick: function (ev) {
            ev.stopPropagation();
            if (!window.confirm("删除技能 " + sk.name + "？")) return;
            var i = list.indexOf(sk);
            if (i >= 0) list.splice(i, 1);
            if (state.selSkill === sk.id) state.selSkill = list[0] && list[0].id;
            renderAll();
          } }, "✕"),
        ]);
      })),
    ]);

    var palette = h("div", { class: "ed-palette" }, [
      h("h4", null, "点击添加节点"),
      h("h4", null, "触发（时机）"),
      paletteItems("trigger", graphHooks()),
      h("h4", null, "条件（判断）"),
      paletteItems("condition", state.schema ? Object.keys(state.schema.conditions) : []),
      h("h4", null, "结构（循环）"),
      paletteItems("struct", state.schema ? Object.keys(state.schema.structs) : []),
      h("h4", null, "效果（原子）"),
      paletteItems("op", state.schema ? Object.keys(state.schema.ops) : []),
    ]);

    var zone = h("div", { class: "ed-canvas-zone" }, [
      palette,
      h("div", { class: "ed-canvas", id: "ed-canvas" }, [
        h("div", { class: "ed-world", id: "ed-world" }, [
          h("svg", { class: "ed-edges", id: "ed-edges",
                     width: "12000", height: "8000", viewBox: "0 0 12000 8000" }),
        ]),
      ]),
    ]);

    var inspector = h("div", { class: "ed-inspector", id: "ed-inspector" },
                      renderInspector());

    return [side, zone, inspector];
  }

  function paletteItems(kind, types) {
    /* 调色板条目列表（点击在画布可视区中心添加节点）。 */
    return types.map(function (type) {
      return h("div", { class: "ed-pal-item", onclick: function () { addNode(kind, type); } },
        state.files.skills.stats["lbl_" + type] || type);
    });
  }

  function addSkill() {
    /* 新建技能：空图 + 默认个性化配置，命名取未占用 id。 */
    var list = skillList();
    var n = 1;
    while (list.some(function (s) { return s.id === "skill_" + n; })) n++;
    var id = "skill_" + n;
    list.push({
      id: id, name: "新技能 " + n, description: "风味短句",
      weight: 5, mastery: [0.7, 1.4], mastery_on: "chance",
      effect: { nodes: [], edges: [] },
    });
    state.selSkill = id;
    renderAll();
  }

  function nextNodeId(g) {
    /* 图内未占用的节点 id（n1、n2……）。 */
    var n = 1;
    while (g.nodes.some(function (x) { return x.id === "n" + n; })) n++;
    return "n" + n;
  }

  function addNode(kind, type) {
    /* 在画布可视区中心添加节点：必填参数按 schema 填安全默认值。
     * 添加 loop 时若已选中条件/效果节点，则直接把它（连同子树）包裹进循环。 */
    var g = graph();
    if (!g) { setStatus("先选择或新建一个" + (state.graphOwner.kind === "status" ? "状态" : "技能"), true); return; }
    if (kind === "struct" && type === "loop" && state.selNode) {
      var target = nodeById(g, state.selNode);
      if (target && (target.kind === "op" || target.kind === "condition")) {
        wrapInLoop(state.selNode);
        return;
      }
    }
    var node = { id: nextNodeId(g), kind: kind, type: type, params: {}, pos: [] };
    var cx = Math.round((-state.view.x + 360) / state.view.s);
    var cy = Math.round((-state.view.y + 160) / state.view.s);
    node.pos = [Math.max(20, cx), Math.max(20, cy)];
    specList(node).forEach(function (ps) {
      if (ps.required || kind === "condition") node.params[ps.key] = defaultParamValue(ps);
    });
    if (kind === "condition" && type === "last_crit") node.params = {};
    g.nodes.push(node);
    state.selNode = node.id;
    state.selEdge = null;
    renderAll();
  }

  /* ---------- 画布（技能图与状态效果图共用） ---------- */

  function worldTransform() {
    /* 应用视图变换（平移 + 缩放）到世界层。 */
    var world = NF.qs("#ed-world");
    if (world) world.style.transform =
      "translate(" + state.view.x + "px," + state.view.y + "px) scale(" + state.view.s + ")";
  }

  function portPos(node, side, gate) {
    /* 节点端口的世界坐标：in = 左侧输入 / out = 右侧通过（pass）输出；
     * out-fail = 右侧偏下的失败（fail）输出（仅条件节点）；
     * loop 容器的 out 端口在容器头右缘（宽随子树展开）。 */
    var pos = Array.isArray(node.pos) ? node.pos : [0, 0];
    if (side === "in") return { x: pos[0], y: pos[1] + PORT_Y };
    if (node.kind === "struct") {
      var box = structBox[node.id] || { w: NODE_W };
      return { x: pos[0] + box.w, y: pos[1] + PORT_Y };
    }
    return { x: pos[0] + NODE_W, y: pos[1] + (gate === "fail" ? FAIL_Y : PORT_Y) };
  }

  function descendants(g, id) {
    /* 节点的全部后代节点 id（含自身）。 */
    var out = [];
    var walk = function (nid) {
      out.push(nid);
      g.edges.forEach(function (e) {
        if (e.from === nid) walk(e.to);
      });
    };
    walk(id);
    return out;
  }

  function computeStructBoxes(g) {
    /* 计算每个 loop 容器的自适应包围盒：按后代节点的**真实渲染尺寸**
     * （nodeEls 测量，缺省 200×65 估计）包住整个循环体 + 内边距。
     * 结果存 structBox（portPos 与连线绘制共用；拖动时实时重算）。 */
    structBox = {};
    var sizeOf = function (nid, n) {
      var el = nodeEls[nid];
      if (n.kind === "struct" && structBox[nid]) {
        return { w: structBox[nid].w, h: structBox[nid].h };
      }
      if (el) return { w: el.offsetWidth, h: el.offsetHeight };
      return { w: NODE_W, h: 65 };
    };
    g.nodes.forEach(function (node) {
      if (node.kind !== "struct") return;
      var ids = descendants(g, node.id);
      var x0 = node.pos[0], y0 = node.pos[1];
      var x1 = x0 + NODE_W, y1 = y0 + 40;   // 头部至少占的区域
      ids.forEach(function (nid) {
        if (nid === node.id) return;
        var n = nodeById(g, nid);
        if (!n) return;
        var s = sizeOf(nid, n);
        x0 = Math.min(x0, n.pos[0]);
        y0 = Math.min(y0, n.pos[1]);
        x1 = Math.max(x1, n.pos[0] + s.w);
        y1 = Math.max(y1, n.pos[1] + s.h);
      });
      structBox[node.id] = {
        x: x0, y: y0,
        w: x1 - x0 + 24,                     // 四周 12px 内边距
        h: y1 - y0 + 18,
      };
    });
  }

  function edgePath(a, b) {
    /* 两端口之间的三次贝塞尔曲线路径。 */
    var dx = Math.max(40, Math.abs(b.x - a.x) * 0.45);
    return "M " + a.x + " " + a.y +
           " C " + (a.x + dx) + " " + a.y + ", " + (b.x - dx) + " " + b.y +
           ", " + b.x + " " + b.y;
  }

  function rebuildCanvas() {
    /* 重建画布内容：普通节点 →（按真实尺寸计算 loop 容器包围盒）→ 容器 →
     * 连线。fail 边以红色渲染（分支语义）；loop 为自适应包裹子树的虚线
     * 容器（z-index 位于节点之下，拖动子树时包围盒实时收缩/扩张）。 */
    var world = NF.qs("#ed-world");
    var svg = NF.qs("#ed-edges");
    if (!world || !svg) return;
    worldTransform();
    clear(world);
    world.appendChild(svg);
    var g = graph();
    if (!g) return;
    nodeEls = {};
    g.nodes.forEach(function (node) {
      if (!Array.isArray(node.pos)) node.pos = [40, 60];   // 缺省位置兜底（手写 JSON 容错）
    });

    // 1) 普通节点（先渲染，供容器按真实尺寸自适应）
    g.nodes.forEach(function (node) {
      if (node.kind === "struct") return;   // loop 作为容器渲染
      var el = h("div", { class: "ed-node k-" + node.kind +
                                 (node.id === state.selNode ? " sel" : ""),
                          style: { left: node.pos[0] + "px", top: node.pos[1] + "px",
                                   width: NODE_W + "px" } }, [
        h("div", { class: "ed-node-head" }, [
          h("span", { class: "kind" }, KIND_CN[node.kind] || node.kind),
          nodeLabel(node),
        ]),
        h("div", { class: "ed-node-params" }, fmtParams(node)),
      ]);
      if (node.kind !== "trigger") {
        el.appendChild(h("div", { class: "ed-port in", dataset: { node: node.id } })); // 输入端口（左）
      }
      // 条件节点带两个输出端口：上 = 通过（pass），下 = 失败（fail，分支）
      el.appendChild(h("div", { class: "ed-port out", dataset: { node: node.id, gate: "pass" } }));
      if (node.kind === "condition") {
        el.appendChild(h("div", { class: "ed-port out fail", dataset: { node: node.id, gate: "fail" } }));
      }
      world.appendChild(el);
      nodeEls[node.id] = el;
    });

    // 2) loop 容器（按已渲染后代节点的真实包围盒自适应）
    computeStructBoxes(g);
    g.nodes.forEach(function (node) {
      if (node.kind !== "struct") return;
      var box = structBox[node.id];
      var el = h("div", { class: "ed-node k-struct ed-container" +
                                 (node.id === state.selNode ? " sel" : ""),
                          style: { left: box.x + "px", top: box.y + "px",
                                   width: box.w + "px", height: box.h + "px" } }, [
        h("div", { class: "ed-node-head", dataset: { node: node.id } }, [
          h("span", { class: "kind" }, KIND_CN.struct),
          nodeLabel(node) + "（循环体）",
        ]),
        h("div", { class: "ed-node-params" },
          "循环 " + (node.params && node.params.max ? "×" + node.params.max : "") +
          " 每轮按 " + (node.params && node.params.decay !== undefined ? node.params.decay : "") + " 衰减"),
      ]);
      el.appendChild(h("div", { class: "ed-port in", dataset: { node: node.id } }));
      el.appendChild(h("div", { class: "ed-port out", dataset: { node: node.id, gate: "pass" } }));
      world.appendChild(el);
      nodeEls[node.id] = el;
    });

    // 3) 连线（loop 出端口在容器右缘，随包围盒自适应）
    //    每条边两层：透明宽命中层（好点选）+ 可见细线；点击选中，Delete
    //    删除，右键直接删除
    g.edges.forEach(function (edge, idx) {
      var from = nodeById(g, edge.from), to = nodeById(g, edge.to);
      if (!from || !to) return;
      var gate = edge.gate === "fail" ? "fail" : "pass";
      var d = edgePath(portPos(from, "out", gate), portPos(to, "in"));
      var sel = idx === state.selEdge;
      var hit = document.createElementNS("http://www.w3.org/2000/svg", "path");
      hit.setAttribute("d", d);
      hit.setAttribute("stroke", "rgba(0,0,0,0)");           // 透明命中层
      hit.setAttribute("stroke-width", String(14 / state.view.s));
      hit.setAttribute("fill", "none");
      hit.style.cursor = "pointer";
      var pick = function (ev) {
        ev.stopPropagation();
        state.selEdge = idx;
        state.selNode = null;
        renderAll();
      };
      hit.addEventListener("mousedown", pick);
      hit.addEventListener("contextmenu", function (ev) {   // 右键直接删除
        ev.preventDefault();
        ev.stopPropagation();
        g.edges.splice(idx, 1);
        state.selEdge = null;
        state.selNode = null;
        setStatus("已删除连线");
        renderAll();
      });
      svg.appendChild(hit);
      var p = document.createElementNS("http://www.w3.org/2000/svg", "path");
      p.setAttribute("d", d);
      p.setAttribute("stroke", sel ? "#e6b84c"
                      : gate === "fail" ? "rgba(255,140,120,.75)" : "rgba(140,160,220,.7)");
      p.setAttribute("stroke-width", String(2 / state.view.s + (sel ? 1 : 0)));
      p.setAttribute("fill", "none");
      p.style.pointerEvents = "none";
      svg.appendChild(p);
    });
    bindCanvas();
  }

  function bindCanvas() {
    /* 画布交互：滚轮缩放 / 空白拖动平移 / 节点点击选择 / 头部拖动 / 端口连线。
     * 同一画布元素只绑定一次（拖动中的局部刷新会反复调用本函数）。 */
    var canvas = NF.qs("#ed-canvas");
    var svg = NF.qs("#ed-edges");
    if (!canvas || !svg || canvas.__bound) return;
    canvas.__bound = true;
    canvas.addEventListener("mousedown", function () {
      // 兜底清理：上一次端口拖拽若丢失 mouseup，其虚线轨迹残留至此
      Array.prototype.forEach.call(
        svg.querySelectorAll(".ed-link-temp"), function (p) { p.remove(); });
    });
    canvas.addEventListener("wheel", function (ev) {
      ev.preventDefault();
      var old = state.view.s;                       // 原缩放
      var factor = ev.deltaY < 0 ? 1.12 : 1 / 1.12; // 缩放步进
      var ns = Math.min(2, Math.max(0.35, old * factor));
      var rect = canvas.getBoundingClientRect();
      var mx = ev.clientX - rect.left, my = ev.clientY - rect.top;  // 鼠标位置
      state.view.x = mx - (mx - state.view.x) * (ns / old);         // 以鼠标为锚缩放
      state.view.y = my - (my - state.view.y) * (ns / old);
      state.view.s = ns;
      rebuildCanvas();
    }, { passive: false });
    canvas.onmousedown = function (ev) {
      if (ev.target.classList && ev.target.classList.contains("ed-port")) {
        startLink(ev, ev.target);   // 端口：开始连线
        return;
      }
      if (ev.target.closest && ev.target.closest(".ed-node")) {
        var nodeEl = ev.target.closest(".ed-node");
        var nodeId = nodeIdOfElement(nodeEl);
        state.selNode = nodeId;
        state.selEdge = null;
        renderInspectorOnly();
        markSelection(nodeId);
        if (ev.target.classList && ev.target.classList.contains("ed-node-head")) {
          startNodeDrag(ev, nodeId);  // 节点头部：拖动节点
        }
        return;
      }
      // 空白处：拖动平移，并取消选择
      state.selNode = null;
      state.selEdge = null;
      renderInspectorOnly();
      markSelection(null);
      var startX = ev.clientX - state.view.x, startY = ev.clientY - state.view.y;
      canvas.classList.add("grabbing");
      var move = function (e) {
        state.view.x = e.clientX - startX;
        state.view.y = e.clientY - startY;
        worldTransform();
      };
      var up = function () {
        canvas.classList.remove("grabbing");
        window.removeEventListener("mousemove", move);
        window.removeEventListener("mouseup", up);
      };
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", up);
    };
  }

  function nodeIdOfElement(nodeEl) {
    /* 由节点元素反查节点 id（布局位置与 pos 数组精确匹配）。 */
    var g = graph();
    if (!g) return null;
    for (var i = 0; i < g.nodes.length; i++) {
      var pos = g.nodes[i].pos || [0, 0];
      if (nodeEl.offsetLeft === pos[0] && nodeEl.offsetTop === pos[1]) return g.nodes[i].id;
    }
    return null;
  }

  function markSelection(nodeId) {
    /* 高亮当前选中节点（不重绘，仅切换类名）。 */
    var world = NF.qs("#ed-world");
    if (!world) return;
    Array.prototype.forEach.call(world.children, function (el) {
      if (!el.classList.contains("ed-node")) return;
      var id = nodeIdOfElement(el);
      el.classList.toggle("sel", id === nodeId);
    });
  }

  function startNodeDrag(ev, nodeId) {
    /* 节点拖动：轻量直更（元素位置 + 连线形状 + 容器包围盒），不重建画布；
     * loop 容器拖动时连同全部后代一起移动（保持包裹关系）。 */
    ev.preventDefault();
    var g = graph();
    var node = nodeById(g, nodeId);
    if (!node) return;
    var world = NF.qs("#ed-world");
    var svg = NF.qs("#ed-edges");
    if (!world || !svg) return;
    var ids = node.kind === "struct" ? descendants(g, nodeId) : [nodeId];  // 联动节点集
    var movers = [];
    var containers = [];
    ids.forEach(function (nid) {
      var n = nodeById(g, nid);
      var el = nodeEls[nid];
      if (!n || !el) return;
      movers.push({ n: n, el: el, x: n.pos[0], y: n.pos[1] });
      if (n.kind === "struct") containers.push(el);
    });
    var start = { mx: ev.clientX, my: ev.clientY };
    var scale = state.view.s;
    var move = function (e) {
      var dx = (e.clientX - start.mx) / scale, dy = (e.clientY - start.my) / scale;
      movers.forEach(function (m) {
        m.n.pos[0] = Math.round(m.x + dx);
        m.n.pos[1] = Math.round(m.y + dy);
        m.el.style.left = m.n.pos[0] + "px";
        m.el.style.top = m.n.pos[1] + "px";
      });
      computeStructBoxes(g);                     // 容器包围盒随子树联动
      containers.forEach(function (el) {
        var id = el.querySelector(".ed-node-head").dataset.node;
        var box = structBox[id];
        if (box) { el.style.left = box.x + "px"; el.style.top = box.y + "px";
                   el.style.width = box.w + "px"; el.style.height = box.h + "px"; }
      });
      refreshEdges();
    };
    var up = function () {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      renderAll();
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  }

  function refreshEdges() {
    /* 按节点当前位置刷新全部连线形状（不重建元素；每条边占两层 path：
     * 命中层 + 可见线，按 2×idx 定位；loop 出端口随容器宽度）。 */
    var svg = NF.qs("#ed-edges");
    var g = graph();
    if (!svg || !g) return;
    g.edges.forEach(function (edge, idx) {
      var from = nodeById(g, edge.from), to = nodeById(g, edge.to);
      if (!from || !to) return;
      var d = edgePath(portPos(from, "out", edge.gate), portPos(to, "in"));
      var hit = svg.children[idx * 2];
      var line = svg.children[idx * 2 + 1];
      if (hit) hit.setAttribute("d", d);
      if (line) line.setAttribute("d", d);
    });
  }

  function startLink(ev, portEl) {
    /* 端口连线：从输出端口（pass/fail）拖到输入端口建立边；本地校验树结构
     * 约束（触发无入边 / 每节点单入边 / 无自环无重复），完整校验在保存时
     * 由服务端执行。fail 端口只存在于条件节点（分支语义）。 */
    ev.stopPropagation();
    ev.preventDefault();
    var g = graph();
    var fromId = portEl.dataset.node;                  // 起点节点 id
    var gate = portEl.dataset.gate === "fail" ? "fail" : "pass";  // 起点端口闸门
    var isOut = portEl.classList.contains("out");      // 是否从输出端口开始拖
    var svg = NF.qs("#ed-edges");
    var temp = document.createElementNS("http://www.w3.org/2000/svg", "path");
    temp.setAttribute("class", "ed-link-temp");   // 兜底清理标记（mouseup 丢失时）
    temp.setAttribute("stroke", gate === "fail" ? "#ff9a8a" : "#e6b84c");
    temp.setAttribute("stroke-width", "2.5");
    temp.setAttribute("fill", "none");
    temp.setAttribute("stroke-dasharray", "6 4");
    svg.appendChild(temp);

    var world = NF.qs("#ed-world");
    var move = function (e) {
      var rect = world.getBoundingClientRect();
      var wx = (e.clientX - rect.left) / state.view.s;   // 鼠标世界坐标
      var wy = (e.clientY - rect.top) / state.view.s;
      var from = nodeById(g, fromId);
      var a = portPos(from, isOut ? "out" : "in", gate);
      temp.setAttribute("d", edgePath(isOut ? a : { x: wx, y: wy },
                                      isOut ? { x: wx, y: wy } : a));
    };
    var up = function (e) {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      temp.remove();
      // 落点解析：优先端口圆点；其次节点主体（含 loop 容器）——拖到节点上
      // 即连接到该节点，与主流节点编辑器一致
      var toId = null;
      var portEl = e.target.closest && e.target.closest(".ed-port");
      if (portEl) {
        toId = portEl.dataset.node;
      } else {
        var nodeEl = e.target.closest && e.target.closest(".ed-node");
        if (nodeEl) toId = nodeIdOfElement(nodeEl);
      }
      if (!toId || toId === fromId) return;
      var edge = isOut ? { from: fromId, to: toId } : { from: toId, to: fromId };
      if (gate === "fail") edge.gate = "fail";
      // 树结构约束：触发无入边；其余恰好一条入边；不重复
      var dst = nodeById(g, edge.to);
      if (!dst || dst.kind === "trigger") { setStatus("触发节点不能有入边", true); return; }
      if (g.edges.some(function (x) { return x.to === edge.to; })) {
        setStatus("每个节点只能有一条入边（树结构）", true); return;
      }
      if (g.edges.some(function (x) { return x.from === edge.from && x.to === edge.to; })) return;
      g.edges.push(edge);
      setStatus("已连接 " + edge.from + " → " + edge.to +
                (gate === "fail" ? "（失败分支）" : ""));
      renderAll();
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  }

  /* ---------- 属性面板（技能 / 状态 / 节点） ---------- */

  function renderInspectorOnly() {
    /* 仅重绘属性面板（选择变化时，避免整页重绘打断画布交互）。 */
    var host = NF.qs("#ed-inspector");
    if (!host) return;
    clear(host);
    renderInspector().forEach(function (el) { host.appendChild(el); });
  }

  function renderInspector() {
    /* 属性面板内容：技能页 = 技能信息或节点参数；战斗页 = 状态定义或节点参数。 */
    if (state.graphOwner.kind === "status") {
      var def = curStatusDef();
      if (!def) return [h("p", { class: "ed-hint" }, "左侧选择或新建一个状态。")];
      var node = state.selNode ? nodeById(graph(), state.selNode) : null;
      if (node) return inspectNode(null, node);
      return inspectStatus(def);
    }
    var sk = curSkill();
    if (!sk) return [h("p", { class: "ed-hint" }, "左侧选择或新建一个技能。")];
    var node2 = state.selNode ? nodeById(graph(), state.selNode) : null;
    if (node2) return inspectNode(sk, node2);
    return inspectSkill(sk);
  }

  function field(label, input) {
    /* 表单字段：标签 + 输入控件。 */
    return h("div", { class: "ed-field" }, [h("label", null, label), input]);
  }

  function numInput(get, set, step) {
    /* 数字输入（双向绑定：只写回合法数字）。 */
    var input = h("input", { type: "number", step: step || "any" });
    input.value = get();
    input.addEventListener("input", function () {
      var v = parseFloat(input.value);
      if (!isNaN(v)) set(v);
    });
    return input;
  }

  function textInput(get, set) {
    /* 文本输入（双向绑定）。 */
    var input = h("input", { type: "text" });
    input.value = get();
    input.addEventListener("input", function () { set(input.value); });
    return input;
  }

  function checkInput(get, set) {
    /* 复选框（双向绑定）。 */
    var input = h("input", { type: "checkbox" });
    input.checked = !!get();
    input.addEventListener("change", function () { set(input.checked); });
    return input;
  }

  function selectInput(options, get, set) {
    /* 下拉框：options = [[值, 显示名], ...]。 */
    var sel = h("select", null, options.map(function (o) {
      return h("option", { value: o[0], selected: get() === o[0] ? "" : null }, o[1]);
    }));
    sel.value = get();
    sel.addEventListener("change", function () { set(sel.value); });
    return sel;
  }

  function inspectSkill(sk) {
    /* 属性面板：技能基本信息（名称 / id / 权重 / 熟练度区间与作用参数）。 */
    return [
      h("h3", null, "技能：" + sk.name),
      h("p", { class: "ed-hint" },
        "效果 = 触发（时机）→ 条件（判断）→ 原子/结构 的有向链。条件出边分两条：" +
        "上方圆点 = 通过（pass），下方红点 = 失败（fail 分支）；loop 结构节点反复执行子树（循环）。" +
        "画布空白处拖动平移、滚轮缩放；从节点右侧圆点拖到另一节点左侧圆点连线。"),
      h("div", { class: "ed-form" }, [
        field("名称", textInput(function () { return sk.name; }, function (v) { sk.name = v; })),
        field("id（改动影响同名结果）", textInput(function () { return sk.id; }, function (v) {
          if (!v || skillList().some(function (s) { return s.id === v && s !== sk; })) return;
          sk.id = v; state.selSkill = v; renderAll();
        })),
        field("抽取权重", numInput(function () { return sk.weight; }, function (v) { sk.weight = v; })),
        field("风味短句", textInput(function () { return sk.description; }, function (v) { sk.description = v; })),
        field("熟练度下限", numInput(function () { return sk.mastery[0]; }, function (v) { sk.mastery[0] = v; })),
        field("熟练度上限", numInput(function () { return sk.mastery[1]; }, function (v) { sk.mastery[1] = v; })),
        field("熟练度作用于（逗号分隔参数名，如 chance / value,spd / value）",
              textInput(function () {
                return Array.isArray(sk.mastery_on) ? sk.mastery_on.join(",") : (sk.mastery_on || "chance");
              },
                        function (v) {
                          var parts = v.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
                          sk.mastery_on = parts.length === 1 ? parts[0] : parts;
                        })),
      ]),
      h("p", { class: "ed-hint" }, "选中画布中的节点可编辑其参数；Delete 键删除选中节点/连线。"),
    ];
  }

  function unitHint(ps) {
    /* 参数的单位/口径提示文案。 */
    var units = { hp: "（点生命）", def: "（点防御）", gauge: "（行动槽）", spd: "（速度）", atk: "（攻击）" };
    if (ps.unit && units[ps.unit]) return units[ps.unit];
    if (ps.kind === "pct") return "（分数，0.06 = 6%）";
    if (ps.kind === "turns") return "（刻/次，≥1 整数）";
    return "";
  }

  function inspectNode(sk, node) {
    /* 属性面板：节点参数表单（按 schema 规格渲染类型化输入）。
     * 状态引用参数渲染为全部状态下拉框（v3 状态图自由组合）。 */
    var forms = [];
    specList(node).forEach(function (ps) {
      var label = ps.key + unitHint(ps) + (ps.link ? " ⟡可共鸣" : "");
      var has = Object.prototype.hasOwnProperty.call(node.params, ps.key);
      if (ps.kind === "bool") {
        forms.push(h("label", { class: "ed-check" }, [
          checkInput(function () { return has ? node.params[ps.key] : false; },
                     function (v) { if (v) node.params[ps.key] = true; else delete node.params[ps.key]; }),
          ps.key + unitHint(ps),
        ]));
        return;
      }
      if (ps.key === "status") {
        // 状态引用参数：全部状态下拉框（v3：状态可自由组合引用）
        var ids = Object.keys(state.schema.statuses);
        forms.push(field(label, selectInput(ids.map(function (sid) {
          return [sid, statusName(sid)];
        }), function () { return node.params[ps.key]; },
           function (v) { node.params[ps.key] = v; renderInspectorOnly(); })));
        return;
      }
      if (ps.key === "event") {
        // 战报模板引用：battle_log 键下拉框（含「无」）
        var evKeys = [["", "（无战报）"]].concat(
          Object.keys(state.files.battle.battle_log).sort().map(function (k) { return [k, k]; }));
        forms.push(field(label, selectInput(evKeys,
          function () { return node.params[ps.key] || ""; },
          function (v) {
            if (v) node.params[ps.key] = v; else delete node.params[ps.key];
            renderInspectorOnly();
          })));
        return;
      }
      if (ps.kind === "enum") {
        var options = ps.options.map(function (o) { return [o, o]; });
        forms.push(field(label, selectInput(options,
          function () { return node.params[ps.key]; },
          function (v) { node.params[ps.key] = v; renderInspectorOnly(); })));
        return;
      }
      var input = h("input", { type: "number", step: "any" });
      var cur = has ? node.params[ps.key] : undefined;
      if (typeof cur === "string" && cur.charAt(0) === "$") {
        // "$参数名" 引用（状态效果图引用施加参数）：文本输入，数字或 $引用
        var ref = h("input", { type: "text" });
        ref.value = cur;
        ref.addEventListener("input", function () {
          var v = ref.value.trim();
          if (v.charAt(0) === "$") node.params[ps.key] = v;
          else {
            var n = parseFloat(v);
            if (!isNaN(n)) node.params[ps.key] = n;
          }
        });
        forms.push(field(label + "（$引用施加参数）", ref));
        return;
      }
      input.value = has ? cur : "";
      input.addEventListener("input", function () {
        if (input.value === "") { delete node.params[ps.key]; return; }   // 清空即移除可选参数
        var v = ps.kind === "int" || ps.kind === "turns" ? parseInt(input.value, 10)
                                                         : parseFloat(input.value);
        if (!isNaN(v)) node.params[ps.key] = v;
      });
      var required = ps.required || node.kind !== "op" || ps.kind === "enum";
      if (!has && required && ps.key !== "announce") {
        input.value = defaultParamValue(ps);
        node.params[ps.key] = defaultParamValue(ps);
      }
      forms.push(field(label + (required ? "" : "（可选）"), input));
    });

    var g = graph();
    var inbound = g.edges.filter(function (e) { return e.to === node.id; });    // 入边
    var outbound = g.edges.filter(function (e) { return e.from === node.id; }); // 出边

    return [
      h("h3", null, nodeLabel(node) + "（" + (KIND_CN[node.kind] || node.kind) + " / " + node.type + "）"),
      h("p", { class: "ed-hint" }, "节点 id: " + node.id + " · 入边 " + inbound.length +
        " · 出边 " + outbound.length + (psHint(node))),
      h("div", { class: "ed-form" }, forms),
      h("div", { style: { marginTop: "10px", display: "flex", gap: "8px", flexWrap: "wrap" } }, [
        h("button", { class: "ed-btn warn", onclick: function () { deleteNode(node.id); } }, "删除节点"),
        node.kind === "op" || node.kind === "condition"
          ? h("button", { class: "ed-btn", onclick: function () { wrapInLoop(node.id); } }, "包裹为循环")
          : null,
        node.kind === "struct"
          ? h("button", { class: "ed-btn", onclick: function () { unwrapLoop(node.id); } }, "解除循环（保留循环体）")
          : null,
        h("button", { class: "ed-btn", onclick: function () { state.selNode = null; renderAll(); } },
          sk ? "返回技能信息" : "返回状态信息"),
      ]),
    ];
  }

  function psHint(node) {
    /* 节点提示：允许挂点（来自 schema）。 */
    var reg = node.kind === "condition" ? (state.schema.conditions[node.type] || {})
                                        : (state.schema.ops[node.type] ||
                                           state.schema.structs[node.type] || {});
    var parts = [];
    if (reg.hooks) parts.push("允许挂点: " + reg.hooks.join("/"));
    return parts.length ? " · " + parts.join(" · ") : "";
  }

  function deleteNode(node_id) {
    /* 删除节点及其关联边；loop 容器删除时连带整个循环体子树。 */
    var g = graph();
    var node = nodeById(g, node_id);
    var ids = node && node.kind === "struct" ? descendants(g, node_id) : [node_id];
    g.nodes = g.nodes.filter(function (n) { return ids.indexOf(n.id) < 0; });
    g.edges = g.edges.filter(function (e) {
      return ids.indexOf(e.from) < 0 && ids.indexOf(e.to) < 0;
    });
    state.selNode = null;
    state.selEdge = null;
    renderAll();
  }

  function wrapInLoop(node_id) {
    /* 一键包裹为循环：新建 loop 容器接在选中节点的上游，选中节点连同其
     * 子树移入容器（拖到容器内偏右下方），原名入边改接容器。 */
    var g = graph();
    var node = nodeById(g, node_id);
    if (!node || node.kind === "trigger" || node.kind === "struct") {
      setStatus("请先选中一个条件或效果节点再包裹为循环", true);
      return;
    }
    var loop = { id: nextNodeId(g), kind: "struct", type: "loop",
                 params: { max: 5, decay: 0.9 },
                 pos: [node.pos[0], Math.max(20, node.pos[1] - 24)] };
    g.nodes.push(loop);
    g.edges.forEach(function (e) {              // 原入边改接容器
      if (e.to === node_id) e.to = loop.id;
    });
    g.edges.push({ from: loop.id, to: node_id });
    node.pos[0] += 60;                          // 子树整体移入容器内
    node.pos[1] += 90;                          // 下移避开容器头部与参数说明区
    descendants(g, node_id).forEach(function (nid) {
      if (nid === node_id) return;
      var n = nodeById(g, nid);
      if (n) { n.pos[0] += 60; n.pos[1] += 90; }
    });
    state.selNode = loop.id;
    state.selEdge = null;
    setStatus("已包裹为循环（×" + loop.params.max + "，衰减 " + loop.params.decay + "）");
    renderAll();
  }

  function unwrapLoop(node_id) {
    /* 一键解除循环：删除 loop 容器但保留循环体子树，原入边改接容器的
     * 首个子节点。 */
    var g = graph();
    var loop = nodeById(g, node_id);
    if (!loop || loop.kind !== "struct") return;
    var kids = g.edges.filter(function (e) { return e.from === loop.id; })
                      .map(function (e) { return e.to; });
    var parents = g.edges.filter(function (e) { return e.to === loop.id; });
    g.edges = g.edges.filter(function (e) {
      return e.from !== loop.id && e.to !== loop.id;
    });
    parents.forEach(function (p, i) {           // 多个父边依次接到各子节点
      var kid = kids[i] || kids[0];
      if (kid) g.edges.push({ from: p.from, to: kid, gate: p.gate });
    });
    g.nodes = g.nodes.filter(function (n) { return n.id !== loop.id; });
    state.selNode = kids[0] || null;
    state.selEdge = null;
    setStatus("已解除循环（循环体保留）");
    renderAll();
  }

  /* ---------- 属性页签 ---------- */

  function renderAttributesTab() {
    /* 属性表：id/名称/emoji/基准/区间/格式/战力权重（id 顺序 = 投掷顺序）。 */
    var attrs = state.files.attributes.attributes;
    var rows = attrs.map(function (a, i) {
      return h("tr", null, [
        h("td", null, textInput(function () { return a.id; }, function (v) {
          if (!v || attrs.some(function (x) { return x.id === v && x !== a; })) return;
          a.id = v;
        })),
        h("td", null, textInput(function () { return a.name; }, function (v) { a.name = v; })),
        h("td", null, textInput(function () { return a.emoji; }, function (v) { a.emoji = v; })),
        h("td", null, numInput(function () { return a.base; }, function (v) { a.base = v; })),
        h("td", null, numInput(function () { return a.min; }, function (v) { a.min = v; })),
        h("td", null, numInput(function () { return a.max; }, function (v) { a.max = v; })),
        h("td", null, selectInput([["int", "整数"], ["percent", "百分数"]],
                                   function () { return a.format; }, function (v) { a.format = v; })),
        h("td", null, numInput(function () { return a.power_weight; }, function (v) { a.power_weight = v; })),
        rowButtons(attrs, i, function () {
          attrs.splice(i + 1, 0, { id: "attr_new", name: "新属性", emoji: "✨",
                                   base: 100, min: 50, max: 150, format: "int", power_weight: 0 });
          renderAll();
        }),
      ]);
    });
    return h("div", { class: "ed-panel" }, [
      h("h3", null, "属性（attributes.json）"),
      h("p", { class: "ed-hint" }, "id 顺序即投掷顺序（改变顺序 = breaking）；hp/atk/def/spd/crit/dodge 为引擎必需。"),
      h("table", { class: "ed-table" }, [
        h("thead", null, h("tr", null, ["id", "名称", "emoji", "基准", "下限", "上限", "格式", "战力权重", "操作"].map(
          function (x) { return h("th", null, x); }))),
        h("tbody", null, rows),
      ]),
    ]);
  }

  function rowButtons(arr, i, onAdd) {
    /* 表格行操作按钮：上移 / 下移 / 删除 / 在其后插入。 */
    return h("td", { class: "ed-rowbtns" }, [
      h("button", { class: "ed-btn", onclick: function () {
        if (i === 0) return; var x = arr.splice(i, 1)[0]; arr.splice(i - 1, 0, x); renderAll();
      } }, "↑"),
      h("button", { class: "ed-btn", onclick: function () {
        if (i === arr.length - 1) return; var x = arr.splice(i, 1)[0]; arr.splice(i + 1, 0, x); renderAll();
      } }, "↓"),
      h("button", { class: "ed-btn warn", onclick: function () {
        arr.splice(i, 1); renderAll();
      } }, "✕"),
      h("button", { class: "ed-btn", onclick: onAdd }, "＋"),
    ]);
  }

  /* ---------- 称号页签 ---------- */

  function renderTitlesTab() {
    /* 称号页签：结构池 + 前缀/主体/后缀三个字段池。 */
    var t = state.files.titles;
    return h("div", { class: "ed-panel" }, [
      h("h3", null, "称号结构（structures）"),
      h("table", { class: "ed-table" }, [
        h("thead", null, h("tr", null, ["id", "权重", "字段(逗号分隔)", "连接符(逗号分隔)", "操作"].map(
          function (x) { return h("th", null, x); }))),
        h("tbody", null, t.structures.map(function (s, i) {
          return h("tr", null, [
            h("td", null, textInput(function () { return s.id; }, function (v) { s.id = v; })),
            h("td", null, numInput(function () { return s.weight; }, function (v) { s.weight = v; })),
            h("td", null, textInput(function () { return (s.fields || []).join(","); }, function (v) {
              s.fields = v.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
            })),
            h("td", null, textInput(function () { return (s.connectors || []).join(","); }, function (v) {
              s.connectors = v.split(",").map(function (x) { return x.trim(); });
            })),
            rowButtons(t.structures, i, function () {
              t.structures.splice(i + 1, 0, { id: "struct_new", weight: 1,
                                              fields: ["prefix", "core"], connectors: [""] });
              renderAll();
            }),
          ]);
        })),
      ]),
      h("h3", null, "前缀池 / 主体池 / 后缀池"),
      h("p", { class: "ed-hint" }, "加成 bonus 以「属性id:数值,属性id:数值」填写（最多三种、可负、与属性同量纲）。"),
      titlePoolTable(t, "prefixes", "前缀池"),
      titlePoolTable(t, "cores", "主体池"),
      titlePoolTable(t, "suffixes", "后缀池"),
    ]);
  }

  function bonusText(entry) {
    /* bonus 字典 <-> 文本：「hp:200,atk:-50」。 */
    return Object.keys(entry.bonus || {}).map(function (k) { return k + ":" + entry.bonus[k]; }).join(",");
  }

  function titlePoolTable(t, key, label) {
    /* 单个称号字段池的可编辑表格。 */
    var pool = t[key] || (t[key] = []);
    return [
      h("h4", { style: { margin: "14px 0 6px" } }, label + "（" + pool.length + "）"),
      h("table", { class: "ed-table" }, [
        h("thead", null, h("tr", null, ["id", "名称", "描述", "权重", "加成 bonus", "操作"].map(
          function (x) { return h("th", null, x); }))),
        h("tbody", null, pool.map(function (entry, i) {
          return h("tr", null, [
            h("td", null, textInput(function () { return entry.id; }, function (v) { entry.id = v; })),
            h("td", null, textInput(function () { return entry.name; }, function (v) { entry.name = v; })),
            h("td", null, textInput(function () { return entry.desc; }, function (v) { entry.desc = v; })),
            h("td", null, numInput(function () { return entry.weight; }, function (v) { entry.weight = v; })),
            h("td", null, textInput(function () { return bonusText(entry); }, function (v) {
              var bonus = {};
              v.split(",").map(function (x) { return x.trim(); }).filter(Boolean).forEach(function (kv) {
                var pair = kv.split(":");
                if (pair.length === 2 && !isNaN(parseInt(pair[1], 10))) bonus[pair[0].trim()] = parseInt(pair[1], 10);
              });
              entry.bonus = bonus;
            })),
            rowButtons(pool, i, function () {
              pool.splice(i + 1, 0, { id: key + "_new", name: "新词条", desc: "描述", weight: 5, bonus: {} });
              renderAll();
            }),
          ]);
        })),
      ]),
    ];
  }

  /* ---------- 战斗页签（常数 + 状态定义编辑 + 状态效果图画布） ---------- */

  /* 战斗常数表单字段：[JSON 路径, 中文名]（数字型；seed_separator 单独处理） */
  var BATTLE_NUM_KEYS = [
    ["crit_multiplier", "暴击倍率"], ["variance.0", "浮动下限"], ["variance.1", "浮动上限"],
    ["atk_factor", "攻击换算系数"], ["defense_constant", "免伤常数 K"], ["min_damage", "伤害下限"],
    ["max_ticks", "最大刻数"], ["gauge_threshold", "行动槽阈值"], ["crit_cap", "暴击上限%"],
    ["dodge_cap", "闪避上限%"], ["guard_reduction_cap", "锻痕减伤上限"], ["reflect_split_cap", "反甲免伤上限"],
    ["power_check.enemies", "真战力敌人数"], ["playback.message_delay_ms", "战报停顿(ms)"],
    ["playback.action_pause_every", "行动停顿间隔"], ["playback.action_pause_ms", "行动停顿(ms)"],
  ];

  function deepGet(obj, path) {
    /* 按点分路径取值（如 "playback.message_delay_ms"）。 */
    return path.split(".").reduce(function (o, k) { return (o || {})[k]; }, obj);
  }
  function deepSet(obj, path, v) {
    /* 按点分路径写值。 */
    var keys = path.split(".");
    var o = obj;
    for (var i = 0; i < keys.length - 1; i++) o = o[keys[i]];
    o[keys[keys.length - 1]] = v;
  }

  function renderBattleTab() {
    /* 战斗页签（三栏）：左 = 常数入口 + 状态列表；中 = 状态效果图调色板与画布；
     * 右 = 属性面板（状态定义表单 / 选中节点参数）。战斗常数以折叠区放在左侧。 */
    var b = state.files.battle;
    var ids = Object.keys(b.statuses);
    if (!state.selStatus && ids.length) state.selStatus = ids[0];
    if (!curStatusDef() && ids.length) state.selStatus = ids[0];
    state.graphOwner = { kind: "status", id: state.selStatus };

    var constFields = BATTLE_NUM_KEYS.map(function (def) {
      return field(def[1], numInput(function () { return deepGet(b, def[0]); },
                                    function (v) { deepSet(b, def[0], v); }));
    });
    constFields.splice(12, 0, field("种子分隔符", textInput(
      function () { return b.seed_separator; }, function (v) { b.seed_separator = v; })));

    var side = h("div", { class: "ed-side" }, [
      h("div", { class: "ed-side-head" }, [
        h("span", null, "状态定义（" + ids.length + "）"),
        h("span", { class: "ed-spacer" }),
        h("button", { class: "ed-btn", onclick: addStatus }, "＋ 新建"),
      ]),
      h("details", { class: "ed-consts" }, [
        h("summary", null, "战斗常数"),
        h("div", { class: "ed-form" }, constFields),
      ]),
      h("div", { class: "ed-list" }, ids.map(function (sid) {
        return h("div", {
          class: "ed-item" + (sid === state.selStatus ? " on" : ""),
          onclick: function () {
            state.selStatus = sid; state.selNode = null;
            state.graphOwner = { kind: "status", id: sid };
            renderAll();
          },
        }, [
          h("span", { class: "grow" }, b.statuses[sid].name || sid),
          h("span", { class: "sub" }, sid),
          h("button", { class: "ed-btn warn", onclick: function (ev) {
            ev.stopPropagation();
            if (!window.confirm("删除状态 " + sid + "？（引用它的技能图会校验失败）")) return;
            delete b.statuses[sid];
            if (state.selStatus === sid) state.selStatus = Object.keys(b.statuses)[0] || null;
            renderAll();
          } }, "✕"),
        ]);
      })),
    ]);

    var palette = h("div", { class: "ed-palette" }, [
      h("h4", null, "状态效果图"),
      h("h4", null, "触发（时机）"),
      paletteItems("trigger", graphHooks()),
      h("h4", null, "条件（判断）"),
      paletteItems("condition", state.schema ? Object.keys(state.schema.conditions) : []),
      h("h4", null, "结构（循环）"),
      paletteItems("struct", state.schema ? Object.keys(state.schema.structs) : []),
      h("h4", null, "效果（原子）"),
      paletteItems("op", state.schema ? Object.keys(state.schema.ops) : []),
    ]);

    var zone = h("div", { class: "ed-canvas-zone" }, [
      palette,
      h("div", { class: "ed-canvas", id: "ed-canvas" }, [
        h("div", { class: "ed-world", id: "ed-world" }, [
          h("svg", { class: "ed-edges", id: "ed-edges",
                     width: "12000", height: "8000", viewBox: "0 0 12000 8000" }),
        ]),
      ]),
    ]);

    var inspector = h("div", { class: "ed-inspector", id: "ed-inspector" },
                      renderInspector());
    return [side, zone, inspector];
  }

  function addStatus() {
    /* 新建状态定义：v3 骨架（策略字段 + 空参数 / mods / 效果图）。 */
    var statuses = state.files.battle.statuses;
    var n = 1;
    while (statuses["status_" + n]) n++;
    var id = "status_" + n;
    statuses[id] = {
      name: "新状态 " + n, detail: "展示文案 {value}", desc: "说明",
      dispellable: true, stack: "refresh", expire: "ticks",
      params: { value: { fmt: "num", default: 100, clamp: [0, null], link: true } },
      mods: [], effects: { nodes: [], edges: [] },
    };
    state.selStatus = id;
    state.graphOwner = { kind: "status", id: id };
    renderAll();
  }

  function inspectStatus(def) {
    /* 属性面板：状态定义表单（策略字段 / 文案 / 参数规格 / mods / lethal）。
     * 数值字段允许「$参数名」引用（施加参数引用，如每刻伤害 $value）。 */
    var statuses = state.files.battle.statuses;
    var modKinds = state.schema ? Object.keys(state.schema.mod_kinds) : [];
    var ref = function (k) {   /* $引用提示 */
      var names = Object.keys(def.params || {});
      return names.length ? "（可填 $引用：" + names.map(function (x) { return "$" + x; }).join(" / ") + "）" : "";
    };
    return [
      h("h3", null, "状态：" + (def.name || state.selStatus)),
      h("p", { class: "ed-hint" },
        "状态 = 策略字段（引擎调度）+ params 数值参数 + mods 被动修饰 + effects 效果图（画布编辑）。" +
        "持续参数统一叫 turns（按 expire 解释为刻数或行动数）。"),
      h("div", { class: "ed-form" }, [
        field("id（改动影响引用处）", textInput(function () { return state.selStatus; }, function (v) {
          if (!v || statuses[v]) return;
          statuses[v] = def;
          delete statuses[state.selStatus];
          state.selStatus = v;
          state.graphOwner = { kind: "status", id: v };
          renderAll();
        })),
        field("名称", textInput(function () { return def.name; }, function (v) { def.name = v; })),
        field("detail 展示文案", textInput(function () { return def.detail; }, function (v) { def.detail = v; })),
        field("desc 说明", textInput(function () { return def.desc; }, function (v) { def.desc = v; })),
        field("叠层策略 stack", selectInput(
          [["refresh", "refresh（重复施加刷新）"], ["layers", "layers（逐层独立到期）"],
           ["count", "count（计数叠层至上限）"]],
          function () { return def.stack || "refresh"; }, function (v) { def.stack = v; })),
        field("到期策略 expire", selectInput(
          [["ticks", "ticks（按刻数）"], ["actions", "actions（按行动数）"], ["none", "none（无期限）"]],
          function () { return def.expire || "ticks"; }, function (v) { def.expire = v; })),
        field("tick 间隔 interval" + ref(), textInput(
          function () { return def.interval === undefined ? "" : String(def.interval); },
          function (v) {
            v = v.trim();
            if (!v) delete def.interval;
            else if (v.charAt(0) === "$") def.interval = v;
            else if (!isNaN(parseInt(v, 10))) def.interval = parseInt(v, 10);
          })),
        field("层数上限 max_stacks", numInput(function () { return def.max_stacks || 0; },
                                               function (v) { def.max_stacks = v; }, "1")),
        h("label", { class: "ed-check" }, [
          checkInput(function () { return !!def.dispellable; }, function (v) { def.dispellable = v; }),
          "可被净化驱散",
        ]),
        h("label", { class: "ed-check" }, [
          checkInput(function () { return !!def.reset_on_miss; }, function (v) {
            if (v) def.reset_on_miss = true; else delete def.reset_on_miss;
          }),
          "攻击落空时清零（乘胜类）",
        ]),
        h("label", { class: "ed-check" }, [
          checkInput(function () { return !!def.lethal; }, function (v) {
            if (v) def.lethal = { chance: "$chance", value: "$value", decay: "$decay" };
            else delete def.lethal;
            renderInspectorOnly();
          }),
          "不屈类（致命伤害拦截重生）",
        ]),
      ]),
      def.lethal ? h("div", { class: "ed-form" }, [
        field("触发概率 chance" + ref(), textInput(
          function () { return String(def.lethal.chance); }, function (v) { def.lethal.chance = v.trim(); })),
        field("回复比例 value" + ref(), textInput(
          function () { return String(def.lethal.value); }, function (v) { def.lethal.value = v.trim(); })),
        field("逐次衰减 decay" + ref(), textInput(
          function () { return String(def.lethal.decay); }, function (v) { def.lethal.decay = v.trim(); })),
      ]) : null,
      h("h4", { style: { margin: "12px 0 4px" } }, "数值参数规格（params）"),
      h("p", { class: "ed-hint" },
        "JSON：fmt = num 数值 / pct 百分数 / turns 刻数；default 施加未覆盖时的默认值；" +
        "clamp 共鸣上下限；link 可共鸣；unit 量纲。施加参数可个性化与共鸣。"),
      jsonArea(def.params || {}, function (v) { def.params = v; }),
      h("h4", { style: { margin: "12px 0 4px" } }, "被动修饰（mods）"),
      h("p", { class: "ed-hint" },
        "引擎聚合点直接生效的修饰：kind 种类 / value 数值或 $引用 / per_stack 按层数倍乘 / " +
        "record=lifesteal 记录吸血量（血契转化基准）。"),
      h("table", { class: "ed-table" }, [
        h("thead", null, h("tr", null, ["种类 kind", "数值 value", "按层 per_stack", "record", "操作"].map(
          function (x) { return h("th", null, x); }))),
        h("tbody", null, (def.mods || (def.mods = [])).map(function (m, i) {
          return h("tr", null, [
            h("td", null, selectInput(modKinds.map(function (k) { return [k, k]; }),
              function () { return m.kind; }, function (v) { m.kind = v; })),
            h("td", null, textInput(function () { return String(m.value); }, function (v) {
              v = v.trim();
              m.value = v.charAt(0) === "$" ? v : parseFloat(v) || 0;
            })),
            h("td", null, checkInput(function () { return !!m.per_stack; },
                                     function (v) { if (v) m.per_stack = true; else delete m.per_stack; })),
            h("td", null, textInput(function () { return m.record || ""; }, function (v) {
              if (v) m.record = v; else delete m.record;
            })),
            rowButtons(def.mods, i, function () {
              def.mods.splice(i + 1, 0, { kind: modKinds[0] || "dmg_out_pct", value: 0 });
              renderAll();
            }),
          ]);
        })),
      ]),
    ];
  }

  function jsonArea(obj, set) {
    /* 小型 JSON 编辑框（参数规格用）：解析成功即写回。 */
    var box = h("textarea", { rows: "4", spellcheck: "false" });
    box.value = JSON.stringify(obj, null, 1);
    box.addEventListener("input", function () {
      try { set(JSON.parse(box.value)); setStatus("JSON 已解析"); }
      catch (e) { setStatus("JSON 语法错误", true); }
    });
    return box;
  }

  /* ---------- 文案页签 ---------- */

  function renderTextsTab() {
    /* 文案页签：战报模板 / 技能描述模板 / 界面文案 三张键值表。 */
    return h("div", { class: "ed-panel" }, [
      h("h3", null, "战报文案（battle.json battle_log）"),
      kvTable(state.files.battle.battle_log),
      h("h3", null, "技能描述模板（skills.json stats）"),
      h("p", { class: "ed-hint" }, "hook_/cond_/op_ 为技能描述组合模板；st_ 为施加状态句；lbl_ 为编辑器调色板短标签；link_/field_/mod_/mastery_/final_ 为共鸣与词缀文案。"),
      kvTable(state.files.skills.stats),
      h("h3", null, "界面文案（ui.json）"),
      kvTable(state.files.ui),
    ]);
  }

  function kvTable(dict) {
    /* 键值对表：键只读展示，值为可编辑文本域。 */
    var keys = Object.keys(dict).sort();
    return h("table", { class: "ed-table" }, keys.map(function (k) {
      var box = h("textarea", { rows: "1", spellcheck: "false" });
      box.value = dict[k];
      box.addEventListener("input", function () { dict[k] = box.value; });
      return h("tr", null, [
        h("td", { style: { whiteSpace: "nowrap", opacity: .7 } }, k),
        h("td", null, box),
      ]);
    }));
  }

  /* ---------- 系统页签 ---------- */

  function renderSystemTab() {
    /* 系统页签：版本 / 语言 / 名字规则。 */
    var sys = state.files.system;
    return h("div", { class: "ed-panel" }, [
      h("h3", null, "系统（system.json）"),
      h("div", { class: "ed-form" }, [
        field("版本号", textInput(function () { return sys.version; }, function (v) { sys.version = v; })),
        field("语言", textInput(function () { return sys.language; }, function (v) { sys.language = v; })),
        h("label", { class: "ed-check" }, [
          checkInput(function () { return !!sys.name.trim; }, function (v) { sys.name.trim = v; }),
          "名字去除首尾空白",
        ]),
        h("label", { class: "ed-check" }, [
          checkInput(function () { return !!sys.name.case_sensitive; }, function (v) { sys.name.case_sensitive = v; }),
          "名字大小写敏感（勾选 = breaking）",
        ]),
        field("名字最小长度", numInput(function () { return sys.name.min_length; }, function (v) { sys.name.min_length = v; }, "1")),
        field("名字最大长度", numInput(function () { return sys.name.max_length; }, function (v) { sys.name.max_length = v; }, "1")),
      ]),
    ]);
  }

  /* ---------- 试运行 / 保存 ---------- */

  function renderFooter() {
    /* 底部操作栏：名字输入 + 试运行 / 保存并生效 / 还原 + 状态栏。 */
    return h("footer", { class: "ed-footer" }, [
      h("input", { type: "text", id: "ed-name-a", placeholder: "名字甲" }),
      h("input", { type: "text", id: "ed-name-b", placeholder: "名字乙" }),
      h("button", { class: "ed-btn", id: "ed-preview-btn", onclick: runPreview }, "用草稿配置试运行"),
      h("button", { class: "ed-btn primary", onclick: saveConfig }, "保存并生效"),
      h("button", { class: "ed-btn warn", onclick: resetDraft }, "还原到已生效配置"),
      h("span", { class: "ed-status", id: "ed-status" }),
    ]);
  }

  function renderPreview() {
    /* 试运行预览区：双方概要 + 胜负 + 战报前 160 条。 */
    if (!state.preview) return h("div", { class: "ed-preview", id: "ed-preview", style: { display: "none" } });
    var p = state.preview;   // battle_to_api 的返回（fighters/result/log）
    var lines = [
      h("div", null, [
        h("b", null, p.fighters[0].name), "（战力 " + p.fighters[0].power + "） vs ",
        h("b", null, p.fighters[1].name), "（战力 " + p.fighters[1].power + "）",
      ]),
      h("div", { class: "ed-kv" }, p.fighters.map(function (f) {
        return f.name + "：" + f.skills.map(function (s) { return s.name; }).join("、");
      }).join(" ｜ ")),
      h("div", null, p.result.draw ? "⚖️ 平局" : "🏆 " + p.result.winner + " 获胜",
        "（" + p.result.ticks + " 刻）"),
    ];
    p.log.slice(0, 160).forEach(function (entry) {
      lines.push(h("p", { class: "log-line" }, entry.tick + " " + entry.text));
    });
    if (p.log.length > 160) {
      lines.push(h("p", { class: "ed-kv" }, "……共 " + p.log.length + " 条战报"));
    }
    return h("div", { class: "ed-preview", id: "ed-preview" }, lines);
  }

  function runPreview() {
    /* 用草稿配置在服务端试运行一场（不落盘），展示战报或配置错误。 */
    if (state.busy) return;
    var a = (NF.qs("#ed-name-a") || {}).value || "测试甲";
    var b = (NF.qs("#ed-name-b") || {}).value || "测试乙";
    state.busy = true;
    setStatus("试运行中……");
    NF.fetchJSON("/api/config/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ files: state.files, a: a, b: b }),
    }).then(function (res) {
      state.busy = false;
      if (!res.ok) { setStatus("草稿配置非法: " + res.error, true); state.preview = null; }
      else { state.preview = res.preview; setStatus("试运行完成（草稿未保存）"); }
      renderAll();
      // 重绘后恢复名字输入框内容
      if (NF.qs("#ed-name-a")) NF.qs("#ed-name-a").value = a;
      if (NF.qs("#ed-name-b")) NF.qs("#ed-name-b").value = b;
    }).catch(function (err) {
      state.busy = false;
      setStatus("请求失败: " + (err.code || err.message), true);
    });
  }

  function saveConfig() {
    /* 保存草稿：服务端完整校验后原子落盘并热重载；成功则更新基线。 */
    if (state.busy) return;
    if (!window.confirm("保存并热重载配置？同名对战结果会随配置改变。")) return;
    state.busy = true;
    setStatus("保存中……");
    NF.fetchJSON("/api/config/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ files: state.files }),
    }).then(function (res) {
      state.busy = false;
      if (!res.ok) setStatus("保存失败: " + res.error, true);
      else {
        state.baseline = deep(state.files);
        state.version = res.version;
        setStatus("已保存并生效（v" + res.version + "）");
      }
      renderAll();
    }).catch(function (err) {
      state.busy = false;
      setStatus("请求失败: " + (err.code || err.message), true);
    });
  }

  function resetDraft() {
    /* 放弃全部未保存修改，还原到已生效配置。 */
    if (!window.confirm("放弃全部未保存修改，还原到已生效配置？")) return;
    state.files = deep(state.baseline);
    setStatus("已还原");
    renderAll();
  }

  /* ---------- 键盘：删除选中节点 / 连线 ---------- */

  window.addEventListener("keydown", function (ev) {
    /* Delete/Backspace 删除画布选中项（输入控件聚焦时不拦截）。 */
    if (state.tab !== "skills" && state.tab !== "battle") return;
    if (state.jsonMode) return;
    if (ev.key !== "Delete" && ev.key !== "Backspace") return;
    var tag = (document.activeElement && document.activeElement.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    var g = graph();
    if (!g) return;
    if (typeof state.selEdge === "number" && g.edges[state.selEdge]) {
      g.edges.splice(state.selEdge, 1);
      state.selEdge = null;
      renderAll();
    } else if (state.selNode) {
      deleteNode(state.selNode);
    }
  });

  /* ---------- 启动 ---------- */

  /* 先取配置原文（工作副本 + 版本），再取引擎 schema，然后渲染。 */
  NF.fetchJSON("/api/config").then(function (cfg) {
    state.files = cfg.files;
    state.baseline = deep(cfg.files);
    state.version = cfg.version;
    return NF.fetchJSON("/api/schema");
  }).then(function (schema) {
    state.schema = schema;
    renderAll();
  }).catch(function (err) {
    var root = NF.qs("#app");
    clear(root);
    root.appendChild(h("div", { class: "ed-panel" }, [
      h("h3", null, "编辑器加载失败"),
      h("p", { class: "ed-hint" }, String((err && err.stack) || err.code || err.message)),
    ]));
  });
})();
