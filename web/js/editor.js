/* 可视化编辑器（v2.0.0）：节点画布（技能）+ 六大配置结构化表单。
 * 零依赖原生 JS，节点/条件/原语/状态的可用参数全部来自 GET /api/schema
 * （引擎自描述），本文件不硬编码任何效果类型——界面管理文案按 AGENTS.md
 * 惯例（原工坊例外）内联于本文件。
 * 数据流：GET /api/config -> state.files（工作副本）-> 表单/画布双向绑定
 * -> POST /api/config/preview（草稿试运行）/ /api/config/save（保存热重载）。 */
(function () {
  "use strict";

  var NODE_W = 200;   // 画布节点固定宽度（端口坐标计算依据，与 CSS 保持一致）
  var PORT_Y = 18;    // 端口相对节点顶部的纵向偏移（与 CSS 的 head 高度匹配）

  /* 全局状态：schema = 引擎自描述；files = 六个配置文件的工作副本；
   * baseline = 已生效版本（脏检测与还原的基准）；view = 画布视图变换
   * （x/y 平移 + s 缩放）；其余为当前页签 / 选择 / 交互态。 */
  var state = {
    text: {},                    // /api/text 的界面文案（本页基本不用，保留）
    version: "",                 // 当前配置版本号
    schema: null,                // /api/schema 引擎自描述注册表
    files: null,                 // 工作副本：{system,attributes,skills,titles,battle,ui}
    baseline: null,              // 已生效副本（深拷贝，用于脏检测与还原）
    tab: "skills",               // 当前页签：skills/attributes/titles/battle/texts/system
    jsonMode: false,             // JSON 源码模式开关
    selSkill: null,              // 当前编辑的技能 id
    selNode: null,               // 画布中选中的节点 id
    selEdge: null,               // 画布中选中的边下标（edges 数组序号）
    view: { x: 40, y: 30, s: 0.85 },  // 画布视图：平移像素 + 缩放比例
    busy: false,                 // 请求进行中（按钮防抖）
    status: null,                // 底部状态栏文案 {text, err}
    preview: null,               // 最近一次试运行的战报（渲染在底部预览区）
    drag: null,                  // 画布节点拖动状态（保留位）
    link: null,                  // 画布连线拖动状态（保留位）
  };

  /* ---------- 基础工具 ---------- */

  function h(tag, attrs) { return NF.h.apply(null, arguments); }  // DOM 快捷构造（见 framework.js）
  function clear(el) { return NF.clear(el); }                     // 清空容器
  function deep(v) { return JSON.parse(JSON.stringify(v)); }      // 深拷贝（配置对象）
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }  // 深比较
  function esc(n) { return parseFloat(n) || 0; }                  // 宽松取数（容错用）

  function setStatus(text, isErr) {
    /* 更新底部状态栏文案（err=true 时红色示警）。 */
    state.status = text ? { text: text, err: !!isErr} : null;
    var el = NF.qs("#ed-status");
    if (el) {
      el.textContent = text || "";
      el.className = "ed-status" + (isErr ? " err" : "");
    }
  }

  function nodeLabel(node) {
    /* 节点短标签：取配置 stats 的 lbl_<类型>（数据驱动，无硬编码）。 */
    var key = "lbl_" + node.type;
    return state.files.skills.stats[key] || node.type;
  }

  function statusName(sid) {
    /* 状态显示名（下拉框 / 节点参数摘要用）。 */
    var def = (state.schema && state.schema.statuses[sid]) || null;
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
  function graph() {
    /* 当前技能的技能图（缺结构时补齐 nodes/edges 数组）。 */
    var sk = curSkill();
    if (!sk) return null;
    if (!sk.effect || !Array.isArray(sk.effect.nodes)) sk.effect = { nodes: [], edges: [] };
    if (!Array.isArray(sk.effect.edges)) sk.effect.edges = [];
    return sk.effect;
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
  function anyDirty() {
    /* 是否有任一文件被修改（未使用，保留给保存前提示）。 */
    var keys = Object.keys(state.files);
    for (var i = 0; i < keys.length; i++) if (fileDirty(keys[i])) return true;
    return false;
  }

  /* ---------- 参数规格（schema 驱动） ---------- */

  function specList(node) {
    /* 节点的参数规格列表：条件/原语取注册表声明；apply_status 额外拼接
     * 所选状态定义的数值参数（编辑器因此无需理解任何具体状态）。 */
    if (!state.schema) return [];
    var reg = node.kind === "condition" ? state.schema.conditions[node.type]
                                        : state.schema.ops[node.type];
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
      h("div", { class: "ed-main" }, [renderBody()]), // 主体（技能页 = 列表+画布+属性面板）
      renderPreview(),                      // 试运行战报预览区
      renderFooter(),                       // 底部操作栏（试运行 / 保存 / 还原）
    ]));
    if (state.tab === "skills" && !state.jsonMode) rebuildCanvas();  // 画布挂载后构建
    setStatus(state.status && state.status.text, state.status && state.status.err);
  }

  function renderHeader() {
    /* 顶栏：导航链接 + 标题 + 版本 + 模式切换。 */
    return h("header", { class: "ed-header" }, [
      h("a", { href: "/", class: "lang-btn" }, "返回对战"),
      h("a", { href: "/power.html", class: "lang-btn" }, "真战力"),
      h("span", { class: "ed-title" }, "可视化编辑器"),
      h("span", { class: "ed-version" }, "v" + state.version),
      h("span", { class: "ed-spacer" }),
      h("button", { class: "ed-btn", onclick: function () {
        state.jsonMode = !state.jsonMode;
        renderAll();
      } }, state.jsonMode ? "表单模式" : "JSON 源码模式"),
    ]);
  }

  /* 页签定义：[文件键映射见 fileOfTabKey, 显示名] */
  var TAB_DEFS = [
    ["skills", "技能"], ["attributes", "属性"], ["titles", "称号"],
    ["battle", "战斗"], ["texts", "文案"], ["system", "系统"],
  ];

  function renderTabs() {
    /* 页签条：当前页高亮，脏文件页签带 ● 标记。 */
    return TAB_DEFS.map(function (def) {
      return h("button", {
        class: "ed-tab" + (state.tab === def[0] ? " on" : ""),
        onclick: function () { state.tab = def[0]; state.selNode = null; state.selEdge = null; renderAll(); },
      }, [
        def[1],
        fileDirty(fileOfTabKey(def[0])) ? h("span", { class: "dot" }, "●") : null,
      ]);
    });
  }

  function renderBody() {
    /* 主体内容：JSON 模式覆盖全部页签，表单模式按页签分发。 */
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

  /* ---------- JSON 源码模式 ---------- */

  function renderJsonMode() {
    /* 当前页签对应文件的整份 JSON 编辑：解析成功即写入草稿。 */
    var key = fileOfTab();
    var box = h("textarea", { class: "ed-json", spellcheck: "false" });
    box.value = JSON.stringify(state.files[key], null, 2);
    box.addEventListener("input", function () {
      try {
        state.files[key] = JSON.parse(box.value);
        setStatus("JSON 已解析（尚未保存）");
      } catch (e) {
        setStatus("JSON 语法错误: " + e.message, true);
      }
    });
    return h("div", { class: "ed-panel" }, [
      h("h3", null, key + ".json（源码模式）"),
      h("p", { class: "ed-hint" }, "直接编辑整份 JSON；解析成功即写入草稿。切换回表单模式可回到结构化编辑。"),
      box,
    ]);
  }

  /* ---------- 技能页签：列表 + 调色板 + 画布 + 属性面板 ---------- */

  function renderSkillsTab() {
    /* 技能页签布局：左侧技能列表 / 中间节点调色板 + 画布 / 右侧属性面板。 */
    var list = skillList();
    if (!state.selSkill && list.length) state.selSkill = list[0].id;
    if (!curSkill() && list.length) state.selSkill = list[0].id;

    var side = h("div", { class: "ed-side" }, [
      h("div", { class: "ed-side-head" }, [
        "技能池（" + list.length + "）",
        h("span", { class: "ed-spacer" }),
        h("button", { class: "ed-btn", onclick: addSkill }, "＋ 新技能"),
      ]),
      h("div", { class: "ed-list" }, list.map(function (sk) {
        return h("div", {
          class: "ed-item" + (sk.id === state.selSkill ? " on" : ""),
          onclick: function () { state.selSkill = sk.id; state.selNode = null; renderAll(); },
        }, [
          h("span", { class: "grow" }, [
            h("div", null, sk.name),
            h("div", { class: "sub" }, sk.id),
          ]),
          h("button", { class: "ed-btn warn", onclick: function (ev) {
            ev.stopPropagation();
            if (!window.confirm("删除技能 " + sk.id + "？（同名结果会改变）")) return;
            skillList().splice(skillList().indexOf(sk), 1);
            if (state.selSkill === sk.id) state.selSkill = skillList()[0] && skillList()[0].id;
            renderAll();
          } }, "✕"),
        ]);
      })),
    ]);

    var palette = h("div", { class: "ed-palette" }, [
      h("h4", null, "点击添加节点"),
      h("h4", null, "触发（时机）"),
      paletteItems("trigger", state.schema ? state.schema.hooks : []),
      h("h4", null, "条件"),
      paletteItems("condition", state.schema ? Object.keys(state.schema.conditions) : []),
      h("h4", null, "效果"),
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
    /* 调色板条目：按注册表类型列出，标签取 stats 的 lbl_* 短标签。 */
    return types.map(function (type) {
      return h("div", { class: "ed-pal-item", onclick: function () { addNode(kind, type); } },
               nodeLabel({ type: type }));
    });
  }

  function addSkill() {
    /* 新建技能：预置一条「攻击时 -> 概率 -> 倍率」的最小可玩链。 */
    var n = 1;
    while (skillList().some(function (s) { return s.id === "skill_" + n; })) n++;
    var id = "skill_" + n;
    skillList().push({
      id: id, name: "新技能 " + n, description: "在这里写风味短句。",
      weight: 5, mastery: [0.7, 1.4], mastery_on: "chance",
      effect: {
        nodes: [
          { id: "n1", kind: "trigger", type: "on_attack", params: {}, pos: [40, 60] },
          { id: "n2", kind: "condition", type: "chance", params: { chance: 0.3 }, pos: [300, 60] },
          { id: "n3", kind: "op", type: "attack_mult", params: { value: 1.6 }, pos: [580, 60] },
        ],
        edges: [{ from: "n1", to: "n2" }, { from: "n2", to: "n3" }],
      },
    });
    state.selSkill = id;
    state.selNode = null;
    renderAll();
  }

  function nextNodeId(g) {
    /* 生成不冲突的节点 id（n1、n2……）。 */
    var n = 1;
    while (g.nodes.some(function (x) { return x.id === "n" + n; })) n++;
    return "n" + n;
  }

  function addNode(kind, type) {
    /* 从调色板添加节点：落在当前视图中心附近，必填参数预填默认值。 */
    var g = graph();
    if (!g) return;
    var canvas = NF.qs("#ed-canvas");
    var rect = canvas ? canvas.getBoundingClientRect() : { width: 800, height: 500 };
    var cx = (rect.width / 2 - state.view.x) / state.view.s - NODE_W / 2;  // 视图中心（世界坐标）
    var cy = (rect.height / 2 - state.view.y) / state.view.s - 30;
    var node = { id: nextNodeId(g), kind: kind, type: type, params: {},
                 pos: [Math.round(cx + (Math.random() * 40 - 20)),
                       Math.round(cy + (Math.random() * 40 - 20))] };
    specList(node).forEach(function (ps) {
      if (ps.required || (kind === "op" && ps.kind !== "enum" && ps.key !== "announce")) {
        node.params[ps.key] = defaultParamValue(ps);
      }
    });
    g.nodes.push(node);
    state.selNode = node.id;
    renderAll();
  }

  /* ---------- 画布 ---------- */

  function worldTransform() {
    /* 应用视图变换（平移 + 缩放）到世界层。 */
    var world = NF.qs("#ed-world");
    if (world) world.style.transform =
      "translate(" + state.view.x + "px," + state.view.y + "px) scale(" + state.view.s + ")";
  }

  function portPos(node, side) {
    /* 节点端口的世界坐标（out = 右侧输出 / in = 左侧输入）。 */
    var pos = node.pos || [0, 0];
    return { x: pos[0] + (side === "out" ? NODE_W : 0), y: pos[1] + PORT_Y };
  }

  function edgePath(a, b) {
    /* 两端口之间的三次贝塞尔曲线路径。 */
    var dx = Math.max(40, Math.abs(b.x - a.x) * 0.45);
    return "M " + a.x + " " + a.y +
           " C " + (a.x + dx) + " " + a.y + ", " + (b.x - dx) + " " + b.y +
           ", " + b.x + " " + b.y;
  }

  function rebuildCanvas() {
    /* 重建画布内容：连线 SVG + 节点元素（挂载交互），保持选择高亮。 */
    var world = NF.qs("#ed-world");
    var svg = NF.qs("#ed-edges");
    if (!world || !svg) return;
    worldTransform();
    clear(world);
    world.appendChild(svg);
    var g = graph();
    if (!g) return;

    g.edges.forEach(function (edge, idx) {
      var from = nodeById(g, edge.from), to = nodeById(g, edge.to);
      if (!from || !to) return;
      var p = document.createElementNS("http://www.w3.org/2000/svg", "path");
      p.setAttribute("d", edgePath(portPos(from, "out"), portPos(to, "in")));
      p.setAttribute("stroke", idx === state.selEdge ? "#e6b84c" : "rgba(140,160,220,.7)");
      p.setAttribute("stroke-width", String(2 / state.view.s + (idx === state.selEdge ? 1 : 0)));
      p.setAttribute("fill", "none");
      p.addEventListener("mousedown", function (ev) {
        ev.stopPropagation();
        state.selEdge = idx;
        state.selNode = null;
        renderAll();
      });
      svg.appendChild(p);
    });

    g.nodes.forEach(function (node) {
      var el = h("div", { class: "ed-node k-" + node.kind +
                                 (node.id === state.selNode ? " sel" : ""),
                          style: { left: node.pos[0] + "px", top: node.pos[1] + "px",
                                   width: NODE_W + "px" } }, [
        h("div", { class: "ed-node-head" }, [
          h("span", { class: "kind" },
            node.kind === "trigger" ? "触发" : node.kind === "condition" ? "条件" : "效果"),
          nodeLabel(node),
        ]),
        h("div", { class: "ed-node-params" }, fmtParams(node)),
      ]);
      if (node.kind !== "trigger") {
        var pin = h("div", { class: "ed-port in", dataset: { node: node.id } });   // 输入端口（左）
        el.appendChild(pin);
      }
      var pout = h("div", { class: "ed-port out", dataset: { node: node.id } });   // 输出端口（右）
      el.appendChild(pout);
      world.appendChild(el);
    });
    bindCanvas();
  }

  function bindCanvas() {
    /* 画布交互：滚轮缩放 / 空白拖动平移 / 节点点击选择 / 头部拖动 / 端口连线。 */
    var canvas = NF.qs("#ed-canvas");
    var svg = NF.qs("#ed-edges");
    if (!canvas || !svg) return;
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
    /* 节点拖动：更新 pos 并同步元素位置与连线形状。 */
    ev.preventDefault();
    var node = nodeById(graph(), nodeId);
    if (!node) return;
    var start = { mx: ev.clientX, my: ev.clientY, x: node.pos[0], y: node.pos[1] };
    var scale = state.view.s;
    var move = function (e) {
      node.pos[0] = Math.round(start.x + (e.clientX - start.mx) / scale);
      node.pos[1] = Math.round(start.y + (e.clientY - start.my) / scale);
      var world = NF.qs("#ed-world");
      var el = world && world.querySelector(".ed-node.sel");
      if (el) { el.style.left = node.pos[0] + "px"; el.style.top = node.pos[1] + "px"; }
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
    /* 拖动节点时按新位置刷新连线形状（不重建元素）。 */
    var svg = NF.qs("#ed-edges");
    var g = graph();
    if (!svg || !g) return;
    Array.prototype.forEach.call(svg.children, function (path, idx) {
      var edge = g.edges[idx];
      if (!edge) return;
      var from = nodeById(g, edge.from), to = nodeById(g, edge.to);
      if (!from || !to) return;
      path.setAttribute("d", edgePath(portPos(from, "out"), portPos(to, "in")));
    });
  }

  function startLink(ev, portEl) {
    /* 端口连线：从输出端口拖到输入端口建立边；本地校验树结构约束
     * （触发无入边 / 每节点单入边 / 无自环无重复），完整校验在保存时
     * 由服务端执行。 */
    ev.stopPropagation();
    ev.preventDefault();
    var g = graph();
    var fromId = portEl.dataset.node;     // 起点节点 id
    var isOut = portEl.classList.contains("out");  // 是否从输出端口开始拖
    var svg = NF.qs("#ed-edges");
    var temp = document.createElementNS("http://www.w3.org/2000/svg", "path");
    temp.setAttribute("stroke", "#e6b84c");
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
      var a = portPos(from, isOut ? "out" : "in");
      temp.setAttribute("d", edgePath(isOut ? a : { x: wx, y: wy },
                                      isOut ? { x: wx, y: wy } : a));
    };
    var up = function (e) {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      temp.remove();
      var target = e.target.closest && e.target.closest(".ed-port");
      if (!target) return;
      var toId = target.dataset.node;
      if (!toId || toId === fromId) return;
      var edge = isOut ? { from: fromId, to: toId } : { from: toId, to: fromId };
      // 树结构约束：触发无入边；其余恰好一条入边；不重复
      var dst = nodeById(g, edge.to);
      if (!dst || dst.kind === "trigger") { setStatus("触发节点不能有入边", true); return; }
      if (g.edges.some(function (x) { return x.to === edge.to; })) {
        setStatus("每个节点只能有一条入边（树结构）", true); return;
      }
      if (g.edges.some(function (x) { return x.from === edge.from && x.to === edge.to; })) return;
      g.edges.push(edge);
      setStatus("已连接 " + edge.from + " → " + edge.to);
      renderAll();
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  }

  /* ---------- 属性面板（技能 / 节点） ---------- */

  function renderInspectorOnly() {
    /* 仅重绘属性面板（选择变化时，避免整页重绘打断画布交互）。 */
    var host = NF.qs("#ed-inspector");
    if (!host) return;
    clear(host);
    renderInspector().forEach(function (el) { host.appendChild(el); });
  }

  function renderInspector() {
    /* 属性面板内容：默认显示技能基本信息，选中节点时显示节点参数表单。 */
    var sk = curSkill();
    if (!sk) return [h("p", { class: "ed-hint" }, "左侧选择或新建一个技能。")];
    var node = state.selNode ? nodeById(graph(), state.selNode) : null;
    if (node) return inspectNode(sk, node);
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
        "效果 = 触发（时机）→ 条件 → 原语 的有向链。节点按数组顺序执行；条件失败则其下游全部跳过。" +
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
        field("熟练度作用于（逗号分隔参数名，如 chance / value,spd / immune）",
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
     * status 参数渲染为状态下拉框（按原语的 status_kind 过滤）。 */
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
        // 状态引用参数：按原语要求的 status_kind 过滤下拉框（apply = 任意可施加种类）
        var opMeta = state.schema.ops[node.type] || {};
        var want = opMeta.status_kind;
        var ids = Object.keys(state.schema.statuses).filter(function (sid) {
          var kind = state.schema.statuses[sid].kind;
          if (!want || want === "apply") return true;
          return kind === want;
        });
        forms.push(field(label, selectInput(ids.map(function (sid) {
          return [sid, statusName(sid)];
        }), function () { return node.params[ps.key]; },
           function (v) { node.params[ps.key] = v; renderInspectorOnly(); })));
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
      input.value = has ? node.params[ps.key] : "";
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
      h("h3", null, nodeLabel(node) + "（" + node.kind + " / " + node.type + "）"),
      h("p", { class: "ed-hint" }, "节点 id: " + node.id + " · 入边 " + inbound.length +
        " · 出边 " + outbound.length + (psHint(node))),
      h("div", { class: "ed-form" }, forms),
      h("div", { style: { marginTop: "10px", display: "flex", gap: "8px" } }, [
        h("button", { class: "ed-btn warn", onclick: function () { deleteNode(node.id); } }, "删除节点"),
        h("button", { class: "ed-btn", onclick: function () { state.selNode = null; renderAll(); } }, "返回技能信息"),
      ]),
    ];
  }

  function psHint(node) {
    /* 节点提示：允许挂点 / 状态行为种类（来自 schema）。 */
    var reg = node.kind === "condition" ? (state.schema.conditions[node.type] || {})
                                        : (state.schema.ops[node.type] || {});
    var parts = [];
    if (reg.hooks) parts.push("允许挂点: " + reg.hooks.join("/"));
    if (node.type === "apply_status") {
      var sdef = state.schema.statuses[node.params && node.params.status];
      if (sdef) parts.push("行为种类: " + sdef.kind + (sdef.timing ? " · " + sdef.timing : ""));
    }
    return parts.length ? " · " + parts.join(" · ") : "";
  }

  function deleteNode(node_id) {
    /* 删除节点及其关联边。 */
    var g = graph();
    g.nodes = g.nodes.filter(function (n) { return n.id !== node_id; });
    g.edges = g.edges.filter(function (e) { return e.from !== node_id && e.to !== node_id; });
    state.selNode = null;
    state.selEdge = null;
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

  /* ---------- 战斗页签 ---------- */

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
    /* 战斗页签：常数表单 + 状态定义表（种类 / timing / power / 文案 / 参数规格）。 */
    var b = state.files.battle;
    var constFields = BATTLE_NUM_KEYS.map(function (def) {
      return field(def[1], numInput(function () { return deepGet(b, def[0]); },
                                    function (v) { deepSet(b, def[0], v); }));
    });
    constFields.splice(12, 0, field("种子分隔符", textInput(
      function () { return b.seed_separator; }, function (v) { b.seed_separator = v; })));
    return h("div", { class: "ed-panel" }, [
      h("h3", null, "战斗常数（battle.json）"),
      h("div", { class: "ed-form" }, constFields),
      h("h3", { style: { marginTop: "16px" } }, "状态定义（statuses）"),
      h("p", { class: "ed-hint" },
        "行为种类 kind 决定结算方式（编辑器只允许注册表内的种类）；dot 需指定 timing 与 power；" +
        "params 为数值参数规格（JSON：fmt 百分数/数值/刻数、clamp 共鸣上下限、link 可共鸣、unit 量纲）。"),
      h("table", { class: "ed-table" }, [
        h("thead", null, h("tr", null,
          ["id", "种类", "timing", "power", "记战报", "名称", "detail 文案", "desc 说明", "params(JSON)", "操作"].map(
            function (x) { return h("th", null, x); }))),
        h("tbody", null, Object.keys(b.statuses).map(function (sid) {
          var entry = b.statuses[sid];   // 单条状态定义
          var kinds = Object.keys(state.schema.status_kinds).map(function (k) { return [k, k]; });
          return h("tr", null, [
            h("td", null, sid),
            h("td", null, selectInput(kinds, function () { return entry.kind; },
                                      function (v) { entry.kind = v; renderAll(); })),
            h("td", null, entry.kind === "dot" ? textInput(
              function () { return entry.timing || "every_tick"; },
              function (v) { entry.timing = v; }) : "—"),
            h("td", null, entry.kind === "dot" ? textInput(
              function () { return entry.power || ""; }, function (v) {
                if (v) entry.power = v; else delete entry.power;
              }) : "—"),
            h("td", null, checkInput(function () { return !!entry.logged; },
                                     function (v) { entry.logged = v; })),
            h("td", null, textInput(function () { return entry.name; }, function (v) { entry.name = v; })),
            h("td", null, textInput(function () { return entry.detail; }, function (v) { entry.detail = v; })),
            h("td", null, textInput(function () { return entry.desc; }, function (v) { entry.desc = v; })),
            h("td", null, jsonArea(entry.params || {}, function (v) { entry.params = v; })),
          ]);
        })),
      ]),
    ]);
  }

  function jsonArea(obj, set) {
    /* 小型 JSON 编辑框（状态参数规格用）：解析成功即写回。 */
    var box = h("textarea", { rows: "3", spellcheck: "false" });
    box.value = JSON.stringify(obj);
    box.addEventListener("input", function () {
      try { set(JSON.parse(box.value)); setStatus("params 已解析"); }
      catch (e) { setStatus("params JSON 语法错误", true); }
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
      h("p", { class: "ed-hint" }, "hook_/cond_/op_/st_ 为技能描述组合模板；lbl_ 为编辑器调色板短标签；link_/field_/mod_/mastery_/final_ 为共鸣与词缀文案。"),
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
    if (state.tab !== "skills") return;
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
      h("p", { class: "ed-hint" }, String(err.code || err.message)),
    ]));
  });
})();
