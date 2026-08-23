/* 创意工坊（v1.0.0）：可视化编辑全部配置 JSON 并试运行。
 * 独立页面（/workshop.html），数据经 /api/config 读取、
 * /api/config/preview 草稿试运行、/api/config/save 校验后保存并热重载。
 * 约定：本页为管理向工具，界面文案直接书写于此（不占用游戏文案配置）。 */
(function () {
  "use strict";

  var FILES = ["attributes", "skills", "titles", "battle", "system", "ui"];
  var FILE_NAMES = {
    attributes: "属性", skills: "技能", titles: "称号",
    battle: "战斗", system: "系统", ui: "界面文案"
  };
  var TRIGGERS = ["on_attack", "on_defense", "on_turn_start", "passive"];
  var EFFECT_TYPES = [
    "charge", "damage_multiplier", "lifesteal", "poison", "concussive",
    "thunder", "sever", "gauge_surge", "damage_reduction", "reflect",
    "bulwark", "retribution", "iron_will", "heal", "cleanse",
    "low_hp_atk_bonus", "streak_bonus", "overload", "armor_shred",
    "bleed", "gamble", "tempo", "armor_pen", "blood_pact", "grudge"
  ];

  var state = {
    files: null,       // 当前编辑中的全部配置（对象树）
    baseline: null,    // 服务器端最近一次确认的配置（还原用）
    tab: "attributes",
    rawMode: false,
    jsonError: "",
    busy: false
  };

  var els = {};

  /* 选项悬浮提示：鼠标移到 ? 上显示说明。键 = 字段标签 / 表头 / 分区标题文本。 */
  var TIPS = {
    /* ---- 系统 ---- */
    "版本号 version": "配置版本号，仅作展示与记录（/api/health 等处返回）。改数值不影响对战结果；改变同名结果的修改建议递增并在 docs/updates 记录。",
    "语言 language": "语言标识，当前单语言固定为 zh，改动无实际效果。",
    "去除首尾空格": "名字归一化规则：计算 MD5 前是否去掉名字首尾空格。改变归一化规则会改变同名斗士的全部派生数据。",
    "区分大小写": "名字归一化规则：是否区分英文大小写。不区分时先统一转小写再计算 MD5。",
    "最短长度": "名字最短长度（字符数），低于该长度的名字会被拒绝。",
    "最长长度": "名字最长长度（字符数），超过该长度的名字会被拒绝。",
    /* ---- 属性 ---- */
    "名称": "显示名，出现在卡牌、战斗界面、共鸣公式的依赖描述等处。",
    "基准值": "名义基准值：变量共鸣归一化用（共鸣幅度 = 属性当前值 ÷ 基准值），不参与属性投掷；建议取投掷区间中点附近的典型值。",
    "最小": "投掷区间下限：属性值在 [最小, 最大] 内按三角形分布投掷（两个随机数取均值，中点概率最高，不会堆积在边界）。",
    "最大": "投掷区间上限：属性值在 [最小, 最大] 内按三角形分布投掷，中点概率最高。",
    "格式": "int：取整后按数值显示；percent：按百分数显示、保留小数（暴击/闪避，判定时除以 100）。",
    "战力权重": "战力 = Σ(属性值 × 权重)，只影响面板战力展示，不影响战斗。",
    /* ---- 技能：抽取与个性化 ---- */
    "技能数（最少）": "每个斗士技能数量的投掷下限（1~3 间三角形分布取整，中间值概率更高）。",
    "技能数（最多）": "每个斗士技能数量的投掷上限。",
    "数值扰动倍率下限": "技能个性化：数值类参数（value / damage）乘以此区间内的随机倍率，使同名同技能数值随名字不同。下限与上限一般关于 1 对称。",
    "数值扰动倍率上限": "技能个性化：数值类参数（value / damage）扰动倍率的投掷上限。",
    "共鸣变数概率": "每个技能有两个变数槽位，每个槽位以此概率真正绑定一个属性成为共鸣变数（默认 0.25）。",
    "前缀获得概率": "技能名获得前缀词缀（如「缠绵」）的概率；词缀会小幅修正技能的某些参数。",
    "后缀获得概率": "技能名获得后缀词缀（如「入骨」）的概率；词缀会小幅修正技能的某些参数。",
    "词缀缩放下限": "词缀修正量实际生效前乘以此区间内的随机倍率（个性化缩放），下限与上限一般关于 1 对称。",
    "词缀缩放上限": "词缀修正量缩放倍率的投掷上限。",
    "权重": "抽取权重：所有条目按权重比例随机抽取，数值越大越容易被选中。",
    "倍率下限": "该属性作为共鸣变量时的倍率投掷区间下限。共鸣修正幅度 = 倍率 × 变量表达式 ÷ 基准值。",
    "倍率上限": "该属性作为共鸣变量时的倍率投掷区间上限。",
    "差值参照": "差值 / 并值模式下，与对方哪个属性运算（默认与自己相同的属性）。例：速度差值参照防御 = 己方速度 − 对方防御。",
    /* ---- 称号 ---- */
    "字段（逗号分隔）": "该结构由哪些字段拼成，逗号分隔，可选：prefix / core / core2 / suffix。例：prefix,core,suffix。core2 为双核心结构的第二核心，不会与第一核心重复。",
    "连接符（逗号分隔）": "字段之间的连接符，数量 = 字段数 − 1。例：·、· 两段。留空则直接拼接。",
    "描述": "称号字段的描述片段，多个字段描述以「，」拼接成完整称号描述。",
    "属性加成（k:v）": "该称号字段附带的属性加成，格式 属性:值，多个用逗号分隔，如 atk:100, crit:1。可为负数；直接加到属性投掷值上，不消耗随机数。",
    /* ---- 战斗 ---- */
    "暴击倍率": "暴击时伤害乘以的倍率（默认 1.8）。",
    "伤害浮动下限": "每次攻击的伤害浮动倍率投掷区间下限（三角形分布），围绕 1 上下浮动。",
    "伤害浮动上限": "伤害浮动倍率投掷区间上限。",
    "攻击系数": "伤害公式中的攻击换算系数：raw = 攻击 × 系数 × 浮动 × 暴击倍率 × 技能倍率，再按免伤率减免。",
    "免伤常数 K": "免伤率 = 防御 ÷ (防御 + K) × (1 − 穿透)。K 越大，同等防御带来的免伤越低。",
    "最小伤害": "单次伤害的下限，保证防御再高也至少造成该伤害。",
    "最大刻数": "战斗最长刻数；耗尽未分胜负时按剩余生命比例判胜，完全相同为平局。",
    "行动槽阈值": "每刻行动槽累加速度值，累计达到阈值即可行动一次并扣回阈值。速度 10、阈值 100 ≈ 每 10 刻行动一次。",
    "暴击上限 %": "暴击率的封顶百分数（属性 + 加成合计超过时按上限计）。",
    "闪避上限 %": "闪避率的封顶百分数。",
    "对战种子分隔符": "对战种子 = md5(两个名字按字典序用此分隔符拼接)。改变分隔符会改变全部对战过程与结果。",
    "每条停顿 ms": "战报回放：每条战报之间的停顿毫秒数（纯展示，不影响结果）。",
    "行动间隔（每 N 次）": "战报回放：每 N 次角色行动后插入一次较长停顿，便于看清攻防节奏。",
    "行动间停顿 ms": "上述较长停顿的时长（毫秒）。",
    /* ---- 模板与文案 ---- */
    "参数修正（k:v）": "词缀对技能参数的修正量，格式 参数名:增量，如 chance:0.05, ticks:1；只作用于该技能已有的参数。",
    "效果类型": "效果类型决定技能行为与参数集合：引擎只支持列表中已有的类型，改类型后记得同步调整下方参数。",
    "风味": "技能的风味短句，仅展示用，不描述数值（数值由参数自动生成描述）。",
    /* ---- 共鸣模式 ---- */
    "己方 own": "共鸣模式：变量式 = 己方该属性当前值。",
    "对方 enemy": "共鸣模式：变量式 = 对方该属性当前值。",
    "差值 difference": "共鸣模式：变量式 = 己方属性 − 对方参照属性（可为负，效果可能减弱）。",
    "并值 sum": "共鸣模式：变量式 = 己方属性 + 对方参照属性。",
    /* ---- 其余表头 ---- */
    "变量": "可成为共鸣变数的属性（与属性页的 id 对应）。",
    "字段一": "变数槽位一可共鸣的参数字段名（必须存在于该效果类型的技能参数中）。",
    "字段二": "变数槽位二可共鸣的参数字段名；留空表示只有一个槽位。",
    "键": "模板键名（程序引用，勿随意改动）。",
    "文案": "界面显示的文字。",
    "详情": "状态标记悬浮显示的数值详情（可含 {value} 等占位）。",
    "说明": "状态标记悬浮显示的效果说明。",
    "参数名": "技能参数名（如 chance / value / threshold），改名会改变技能行为。",
    "参数值": "该参数的数值；具体含义由效果类型决定（概率为 0~1 小数，倍率为 1 上下）。",
    "熟练度区间": "熟练度 0~100 映射到 [下限, 上限] 的倍率：倍率 = 下限 + (上限−下限) × 熟练度/100。",
    "作用字段": "熟练度倍率作用于哪个参数：chance=触发概率 / value=效果数值 / immune=免疫概率。",
    "触发时机": "on_attack=攻击时 / on_defense=受击时 / on_turn_start=行动开始时 / passive=被动。",
    "效果": "技能效果 = 效果类型 + 参数表；参数含义随类型变化，见各参数行。",
    "id": "唯一标识，引擎与模板按它引用；修改相当于换了一个新条目。",
    "操作": "↑↓ 移动、✕ 删除。条目在列表中的位置参与抽取次序，移动可能改变同名结果。"
  };

  /* 分区标题悬浮提示（按标题前缀匹配） */
  var SECTION_TIPS = {
    "抽取与个性化": "控制每个斗士的技能数量、数值扰动与词缀获得概率——同名斗士的差异都来自这里的随机化。",
    "共鸣模式权重": "变数绑定后抽取哪种运算模式的权重比例（own : enemy : difference : sum）。",
    "共鸣变量池": "哪些属性可以作为共鸣变数、被选中的权重与倍率区间。共鸣按「倍率 × 变量式 ÷ 基准值」修正技能自身参数。",
    "共鸣目标字段": "每个效果类型的哪个参数允许被共鸣修正；每个效果类型最多两个字段，对应两个变数槽位。",
    "名称前缀池": "技能名前缀词缀池：抽中后技能名加前缀，并按修正量调整技能参数。",
    "名称后缀池": "技能名后缀词缀池：抽中后技能名加后缀，并按修正量调整技能参数。",
    "技能池": "全部技能定义：抽取权重、效果参数、熟练度区间。新技能的效果类型必须是引擎已支持的。",
    "技能描述模板": "技能自然语言描述的句式模板，{参数名} 为占位符；引擎按固定键名取用，勿删必要键。",
    "称号结构": "称号由哪些字段按什么连接符拼成，按权重抽取结构。",
    "前缀池": "称号前缀字段池（名称 / 描述 / 权重 / 属性加成）。",
    "核心池": "称号核心字段池（必有字段）。",
    "后缀池": "称号后缀字段池。",
    "战斗常数": "伤害公式、行动节奏与判定规则的核心常数。",
    "战报播放": "前端战报回放的节奏（纯展示，不影响结果）。",
    "战报模板": "战斗日志句式模板，{a}/{b}/{skill}/{damage} 等为占位符。",
    "状态标记文案": "buff / debuff 在状态栏的显示名称与悬浮说明。"
  };

  function clone(x) { return JSON.parse(JSON.stringify(x)); }

  /* 悬浮提示小徽章：鼠标悬停显示 TIPS 说明 */
  function tipBadge(tipText) {
    if (!tipText) return null;
    return NF.h("span", { class: "ws-tip", dataset: { tip: tipText } }, "?");
  }

  function isDirty() {
    return state.files && state.baseline
      && JSON.stringify(state.files) !== JSON.stringify(state.baseline);
  }

  /* ---------------- API ---------------- */

  function apiConfig() { return NF.fetchJSON("/api/config"); }

  function apiPreview(files, a, b) {
    return NF.fetchJSON("/api/config/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ files: files, a: a, b: b })
    });
  }

  function apiSave(files) {
    return NF.fetchJSON("/api/config/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ files: files })
    });
  }

  /* ---------------- 通用输入控件 ---------------- */

  function toNumber(v, fallback) {
    var n = parseFloat(v);
    return isNaN(n) ? fallback : n;
  }

  /* 草稿被编辑后刷新页签的脏标记（●） */
  function markEdited() { renderTabs(); }

  /* 数字输入：写回 obj[key]；空/非法输入不覆盖原值 */
  function numInput(obj, key, cls) {
    return NF.h("input", {
      class: "ws-input num" + (cls ? " " + cls : ""), type: "number", step: "any",
      value: String(obj[key] != null ? obj[key] : 0),
      oninput: function (e) {
        if (e.target.value === "") return;
        var n = parseFloat(e.target.value);
        if (!isNaN(n)) obj[key] = n;
        markEdited();
      }
    });
  }

  function intInput(obj, key) {
    return NF.h("input", {
      class: "ws-input num", type: "number", step: "1",
      value: String(obj[key] != null ? obj[key] : 0),
      oninput: function (e) {
        if (e.target.value === "") return;
        var n = parseInt(e.target.value, 10);
        if (!isNaN(n)) obj[key] = n;
        markEdited();
      }
    });
  }

  function textInput(obj, key, cls) {
    return NF.h("input", {
      class: "ws-input" + (cls ? " " + cls : ""), type: "text",
      value: String(obj[key] != null ? obj[key] : ""),
      oninput: function (e) { obj[key] = e.target.value; markEdited(); }
    });
  }

  function checkInput(obj, key) {
    return NF.h("input", {
      class: "simple-box", type: "checkbox", checked: obj[key] ? "" : null,
      onchange: function (e) { obj[key] = e.target.checked; markEdited(); }
    });
  }

  function selectInput(obj, key, options) {
    return NF.h("select", {
      class: "ws-select",
      onchange: function (e) { obj[key] = e.target.value; markEdited(); }
    }, options.map(function (o) {
      return NF.h("option", { value: o, selected: String(obj[key]) === o ? "" : null }, o);
    }));
  }

  function field(labelText, inputEl) {
    return NF.h("div", { class: "ws-field" },
      NF.h("label", null, labelText, tipBadge(TIPS[labelText])), inputEl);
  }

  function sectionTitle(text, extra) {
    /* 分区标题的提示按前缀匹配（标题可能带数量等后缀，如「技能池（25 个）」） */
    var tip = null;
    Object.keys(SECTION_TIPS).forEach(function (k) {
      if (!tip && text.indexOf(k) === 0) tip = SECTION_TIPS[k];
    });
    return NF.h("div", { class: "ws-section-title" }, text, tipBadge(tip), extra || null);
  }

  /* 表头单元格：带可选的悬浮提示 */
  function thCell(labelText) {
    return NF.h("th", null, labelText, tipBadge(TIPS[labelText]));
  }

  function removeButton(onclick) {
    return NF.h("button", { class: "ws-rowbtn danger", onclick: onclick }, "✕");
  }

  function moveButtons(arr, index, rerender) {
    function move(delta) {
      var j = index + delta;
      if (j < 0 || j >= arr.length) return;
      var t = arr[index];
      arr[index] = arr[j];
      arr[j] = t;
      rerender();
    }
    return [
      NF.h("button", { class: "ws-rowbtn", onclick: function () { move(-1); } }, "↑"),
      NF.h("button", { class: "ws-rowbtn", onclick: function () { move(1); } }, "↓")
    ];
  }

  /* 「k:v, k:v」文本 <-> 对象（称号加成 / 词缀修正） */
  function bonusToText(bonus) {
    return Object.keys(bonus || {}).map(function (k) {
      return k + ":" + bonus[k];
    }).join(", ");
  }

  function bindBonusText(obj, key) {
    return NF.h("input", {
      class: "ws-input wide", type: "text", placeholder: "如 atk:100, crit:1",
      value: bonusToText(obj[key]),
      oninput: function (e) {
        var out = {};
        e.target.value.split(",").forEach(function (part) {
          var kv = part.split(":");
          if (kv.length !== 2) return;
          var k = kv[0].trim();
          var v = parseFloat(kv[1]);
          if (k && !isNaN(v)) out[k] = v;
        });
        obj[key] = out;
        markEdited();
      }
    });
  }

  /* ---------------- 各文件的可视化编辑器 ---------------- */

  function renderSystemForm(d) {
    var box = NF.h("div", null);
    box.appendChild(NF.h("div", { class: "ws-grid" },
      field("版本号 version", textInput(d, "version", "small")),
      field("语言 language", textInput(d, "language", "small"))));
    var name = d.name = d.name || {};
    box.appendChild(sectionTitle("名字归一化规则"));
    box.appendChild(NF.h("div", { class: "ws-grid" },
      field("去除首尾空格", checkInput(name, "trim")),
      field("区分大小写", checkInput(name, "case_sensitive")),
      field("最短长度", intInput(name, "min_length")),
      field("最长长度", intInput(name, "max_length"))));
    return box;
  }

  function renderAttributesForm(d) {
    var list = d.attributes = d.attributes || [];
    var table = NF.h("table", { class: "ws-table" },
      NF.h("tr", null,
        ["id", "名称", "图标", "基准值", "最小", "最大", "格式", "战力权重", "操作"]
          .map(thCell)));
    list.forEach(function (a, i) {
      var cells = [
        NF.h("td", null, NF.h("span", { class: "ws-static" }, a.id)),
        NF.h("td", null, textInput(a, "name", "small")),
        NF.h("td", null, textInput(a, "emoji", "small")),
        NF.h("td", null, numInput(a, "base")),
        NF.h("td", null, numInput(a, "min")),
        NF.h("td", null, numInput(a, "max")),
        NF.h("td", null, selectInput(a, "format", ["int", "percent"])),
        NF.h("td", null, numInput(a, "power_weight")),
        NF.h("td", null, moveButtons(list, i, rerenderMain).concat(
          removeButton(function () { list.splice(i, 1); rerenderMain(); })))
      ];
      table.appendChild(NF.h("tr", null, cells));
    });
    return NF.h("div", null, table,
      NF.h("button", {
        class: "ws-add",
        onclick: function () {
          list.push({ id: "new_attr_" + (list.length + 1), name: "新属性",
                      emoji: "", base: 10, min: 5, max: 15,
                      format: "int", power_weight: 1 });
          rerenderMain();
        }
      }, "＋ 添加属性（新增属性需同步技能共鸣等引用，建议仅调整现有属性）"));
  }

  function renderSkillsForm(d) {
    var box = NF.h("div", null);

    var sc = d.skill_count = d.skill_count || { min: 2, max: 3 };
    var mv = d.md5_variance = d.md5_variance || { value: [0.8, 1.3] };
    mv.value = Array.isArray(mv.value) && mv.value.length === 2 ? mv.value : [1, 1];
    var vl = d.variable_link = d.variable_link || {};
    var nm = d.name_modifiers = d.name_modifiers || {};
    nm.mod_variance = Array.isArray(nm.mod_variance) && nm.mod_variance.length === 2
      ? nm.mod_variance : [1, 1];

    box.appendChild(sectionTitle("抽取与个性化"));
    box.appendChild(NF.h("div", { class: "ws-grid" },
      field("技能数（最少）", intInput(sc, "min")),
      field("技能数（最多）", intInput(sc, "max")),
      field("数值扰动倍率下限", numInput(mv.value, "0")),
      field("数值扰动倍率上限", numInput(mv.value, "1")),
      field("共鸣变数概率", numInput(vl, "chance")),
      field("前缀获得概率", numInput(nm, "prefix_chance")),
      field("后缀获得概率", numInput(nm, "suffix_chance")),
      field("词缀缩放下限", numInput(nm.mod_variance, "0")),
      field("词缀缩放上限", numInput(nm.mod_variance, "1"))));

    /* 共鸣模式权重 */
    vl.mode_weights = vl.mode_weights || { own: 1 };
    box.appendChild(sectionTitle("共鸣模式权重"));
    box.appendChild(NF.h("div", { class: "ws-grid" },
      Object.keys(vl.mode_weights).map(function (mode) {
        return field({ own: "己方 own", enemy: "对方 enemy",
                       difference: "差值 difference", sum: "并值 sum" }[mode] || mode,
                     numInput(vl.mode_weights, mode));
      })));

    /* 共鸣变量池 */
    vl.variables = vl.variables || {};
    box.appendChild(sectionTitle("共鸣变量池"));
    var vtable = NF.h("table", { class: "ws-table" },
      NF.h("tr", null, ["变量", "权重", "倍率下限", "倍率上限", "差值参照", "操作"]
        .map(thCell)));
    Object.keys(vl.variables).forEach(function (vid) {
      var v = vl.variables[vid];
      v.rate = Array.isArray(v.rate) && v.rate.length === 2 ? v.rate : [0, 0];
      vtable.appendChild(NF.h("tr", null,
        NF.h("td", null, NF.h("span", { class: "ws-static" }, vid)),
        NF.h("td", null, numInput(v, "weight")),
        NF.h("td", null, numInput(v.rate, "0")),
        NF.h("td", null, numInput(v.rate, "1")),
        NF.h("td", null, textInput(v, "diff_against", "small")),
        NF.h("td", null, removeButton(function () {
          delete vl.variables[vid];
          rerenderMain();
        }))));
    });
    box.appendChild(vtable);

    /* 共鸣目标字段 */
    vl.targets = vl.targets || {};
    box.appendChild(sectionTitle("共鸣目标字段（每个效果类型 1~2 个可共鸣参数）"));
    var ttable = NF.h("table", { class: "ws-table" },
      NF.h("tr", null, ["效果类型", "字段一", "字段二"]
        .map(thCell)));
    Object.keys(vl.targets).forEach(function (etype) {
      var fields = Array.isArray(vl.targets[etype]) ? vl.targets[etype] : [vl.targets[etype]];
      var holder = { f0: fields[0] || "", f1: fields[1] || "" };
      function writeBack() {
        var out = [holder.f0, holder.f1].filter(function (x) { return x; });
        if (out.length) vl.targets[etype] = out;
        else delete vl.targets[etype];
      }
      function targetInput(key) {
        return NF.h("input", {
          class: "ws-input small", type: "text", value: holder[key],
          oninput: function (e) { holder[key] = e.target.value.trim(); writeBack(); }
        });
      }
      ttable.appendChild(NF.h("tr", null,
        NF.h("td", null, NF.h("span", { class: "ws-static" }, etype)),
        NF.h("td", null, targetInput("f0")),
        NF.h("td", null, targetInput("f1"))));
    });
    box.appendChild(ttable);

    /* 词缀池 */
    ["prefixes", "suffixes"].forEach(function (poolKey) {
      nm[poolKey] = nm[poolKey] || [];
      box.appendChild(sectionTitle(poolKey === "prefixes" ? "名称前缀池" : "名称后缀池"));
      var ptable = NF.h("table", { class: "ws-table" },
        NF.h("tr", null, ["id", "名称", "权重", "参数修正（k:v）", "操作"]
          .map(thCell)));
      nm[poolKey].forEach(function (m, i) {
        ptable.appendChild(NF.h("tr", null,
          NF.h("td", null, textInput(m, "id", "small")),
          NF.h("td", null, textInput(m, "name", "small")),
          NF.h("td", null, numInput(m, "weight")),
          NF.h("td", null, bindBonusText(m, "mod")),
          NF.h("td", null, moveButtons(nm[poolKey], i, rerenderMain).concat(
            removeButton(function () { nm[poolKey].splice(i, 1); rerenderMain(); })))));
      });
      box.appendChild(ptable);
      box.appendChild(NF.h("button", {
        class: "ws-add",
        onclick: function () {
          nm[poolKey].push({ id: "new_mod_" + (nm[poolKey].length + 1),
                             name: "新词缀", weight: 1, mod: {} });
          rerenderMain();
        }
      }, "＋ 添加词缀"));
    });

    /* 技能池 */
    d.skills = d.skills || [];
    box.appendChild(sectionTitle("技能池（" + d.skills.length + " 个）"));
    d.skills.forEach(function (s, i) {
      box.appendChild(skillCard(s, d.skills, i));
    });
    box.appendChild(NF.h("button", {
      class: "ws-add",
      onclick: function () {
        d.skills.push({
          id: "new_skill_" + (d.skills.length + 1), name: "新技能",
          description: "", weight: 5, trigger: "on_attack",
          mastery: [0.7, 1.4],
          effect: { type: "damage_multiplier", chance: 0.3, value: 1.5, threshold: 0.35 }
        });
        rerenderMain();
      }
    }, "＋ 添加技能"));

    /* stats 文案模板 */
    d.stats = d.stats || {};
    box.appendChild(sectionTitle("技能描述模板 stats（自然语言句式，{参数} 占位）"));
    var stable = NF.h("table", { class: "ws-table" });
    Object.keys(d.stats).forEach(function (key) {
      stable.appendChild(NF.h("tr", null,
        NF.h("td", { style: { width: "150px" } }, keyInput(d.stats, key)),
        NF.h("td", null, textInput(d.stats, key, "wide")),
        NF.h("td", null, removeButton(function () {
          delete d.stats[key];
          rerenderMain();
        }))));
    });
    box.appendChild(stable);
    box.appendChild(NF.h("button", {
      class: "ws-add",
      onclick: function () {
        var k = "new_key_" + Object.keys(d.stats).length;
        d.stats[k] = "";
        rerenderMain();
      }
    }, "＋ 添加模板"));

    return box;
  }

  /* stats 键名编辑：键名变更需迁移值 */
  function keyInput(obj, oldKey) {
    return NF.h("input", {
      class: "ws-input small", type: "text", value: oldKey,
      onchange: function (e) {
        var newKey = e.target.value.trim();
        if (!newKey || newKey === oldKey || !(oldKey in obj)) return;
        var entries = Object.keys(obj).map(function (k) {
          return [k === oldKey ? newKey : k, obj[k]];
        });
        Object.keys(obj).forEach(function (k) { delete obj[k]; });
        entries.forEach(function (kv) { obj[kv[0]] = kv[1]; });
        rerenderMain();
      }
    });
  }

  function skillCard(s, list, index) {
    s.effect = s.effect || {};
    s.mastery = Array.isArray(s.mastery) && s.mastery.length === 2 ? s.mastery : [1, 1];
    var card = NF.h("div", { class: "ws-card" });
    card.appendChild(NF.h("div", { class: "ws-card-head" },
      textInput(s, "id", "small"),
      textInput(s, "name", "small"),
      NF.h("span", { class: "ws-static" }, "权重", tipBadge(TIPS["权重"])),
      numInput(s, "weight"),
      selectInput(s, "trigger", TRIGGERS), tipBadge(TIPS["触发时机"]),
      moveButtons(list, index, rerenderMain),
      removeButton(function () { list.splice(index, 1); rerenderMain(); })));
    card.appendChild(NF.h("div", { class: "ws-effect-row" },
      NF.h("span", { class: "ws-static" }, "风味", tipBadge(TIPS["风味"])),
      textInput(s, "description", "wide")));
    card.appendChild(NF.h("div", { class: "ws-effect-row" },
      NF.h("span", { class: "ws-static" }, "熟练度区间",
             tipBadge(TIPS["熟练度区间"])),
      numInput(s.mastery, "0"), numInput(s.mastery, "1"),
      NF.h("span", { class: "ws-static" }, "作用字段",
             tipBadge(TIPS["作用字段"])),
      selectInput(s, "mastery_on", ["chance", "value", "immune"])));

    var eff = s.effect;
    var effRow = NF.h("div", { class: "ws-effect-row" },
      NF.h("span", { class: "ws-static" }, "效果", tipBadge(TIPS["效果"])),
      NF.h("select", {
        class: "ws-select",
        onchange: function (e) { eff.type = e.target.value; rerenderMain(); }
      }, EFFECT_TYPES.map(function (t) {
        return NF.h("option", { value: t, selected: eff.type === t ? "" : null }, t);
      })));
    card.appendChild(effRow);

    var ptable = NF.h("table", { class: "ws-table" },
      NF.h("tr", null, thCell("参数名"), thCell("参数值"), thCell("操作")));
    Object.keys(eff).forEach(function (key) {
      if (key === "type") return;
      var value = eff[key];
      var valueInput;
      if (typeof value === "number") {
        valueInput = numInput(eff, key);
      } else {
        valueInput = NF.h("input", {
          class: "ws-input num", type: "text", value: String(value),
          oninput: function (e) { eff[key] = e.target.value; }
        });
      }
      ptable.appendChild(NF.h("tr", null,
        NF.h("td", null, NF.h("input", {
          class: "ws-input small", type: "text", value: key,
          onchange: function (e) {
            var nk = e.target.value.trim();
            if (!nk || nk === key) return;
            var v = eff[key];
            delete eff[key];
            eff[nk] = v;
            rerenderMain();
          }
        })),
        NF.h("td", null, valueInput),
        NF.h("td", null, removeButton(function () {
          delete eff[key];
          rerenderMain();
        }))));
    });
    card.appendChild(ptable);
    card.appendChild(NF.h("button", {
      class: "ws-add",
      onclick: function () {
        var k = "param_" + Object.keys(eff).length;
        eff[k] = 0;
        rerenderMain();
      }
    }, "＋ 添加参数"));
    return card;
  }

  function renderTitlesForm(d) {
    var box = NF.h("div", null);
    d.structures = d.structures || {};
    var structs = Array.isArray(d.structures) ? d.structures : [];
    d.structures = structs;
    box.appendChild(sectionTitle("称号结构（字段：prefix / core / core2 / suffix）"));
    var stable = NF.h("table", { class: "ws-table" },
      NF.h("tr", null, ["id", "权重", "字段（逗号分隔）", "连接符（逗号分隔）", "操作"]
        .map(thCell)));
    structs.forEach(function (s, i) {
      var holder = {
        fields: (s.fields || []).join(","),
        connectors: (s.connectors || []).join(",")
      };
      function writeBack() {
        s.fields = holder.fields.split(",").map(function (x) { return x.trim(); })
          .filter(function (x) { return x; });
        s.connectors = holder.connectors.split(",").map(function (x) { return x.trim(); });
        if (s.connectors.length === 1 && s.connectors[0] === "") s.connectors = [];
      }
      stable.appendChild(NF.h("tr", null,
        NF.h("td", null, textInput(s, "id", "small")),
        NF.h("td", null, numInput(s, "weight")),
        NF.h("td", null, NF.h("input", {
          class: "ws-input wide", type: "text", value: holder.fields,
          oninput: function (e) { holder.fields = e.target.value; writeBack(); }
        })),
        NF.h("td", null, NF.h("input", {
          class: "ws-input small", type: "text", value: holder.connectors,
          oninput: function (e) { holder.connectors = e.target.value; writeBack(); }
        })),
        NF.h("td", null, moveButtons(structs, i, rerenderMain).concat(
          removeButton(function () { structs.splice(i, 1); rerenderMain(); })))));
    });
    box.appendChild(stable);
    box.appendChild(NF.h("button", {
      class: "ws-add",
      onclick: function () {
        structs.push({ id: "new_struct_" + (structs.length + 1),
                       weight: 10, fields: ["core"], connectors: [] });
        rerenderMain();
      }
    }, "＋ 添加结构"));

    [["prefixes", "前缀池"], ["cores", "核心池"], ["suffixes", "后缀池"]]
      .forEach(function (pair) {
        var poolKey = pair[0];
        d[poolKey] = d[poolKey] || [];
        box.appendChild(sectionTitle(pair[1] + "（" + d[poolKey].length + " 个）"));
        var ptable = NF.h("table", { class: "ws-table" },
          NF.h("tr", null, ["id", "名称", "描述", "权重", "属性加成（k:v）", "操作"]
            .map(thCell)));
        d[poolKey].forEach(function (t, i) {
          ptable.appendChild(NF.h("tr", null,
            NF.h("td", null, textInput(t, "id", "small")),
            NF.h("td", null, textInput(t, "name", "small")),
            NF.h("td", null, textInput(t, "desc")),
            NF.h("td", null, numInput(t, "weight")),
            NF.h("td", null, bindBonusText(t, "bonus")),
            NF.h("td", null, moveButtons(d[poolKey], i, rerenderMain).concat(
              removeButton(function () { d[poolKey].splice(i, 1); rerenderMain(); })))));
        });
        box.appendChild(ptable);
        box.appendChild(NF.h("button", {
          class: "ws-add",
          onclick: function () {
            d[poolKey].push({ id: "new_title_" + (d[poolKey].length + 1),
                              name: "新称号", desc: "", weight: 5, bonus: {} });
            rerenderMain();
          }
        }, "＋ 添加条目"));
      });
    return box;
  }

  function renderBattleForm(d) {
    var box = NF.h("div", null);
    d.variance = Array.isArray(d.variance) && d.variance.length === 2
      ? d.variance : [1, 1];
    d.playback = d.playback || {};
    box.appendChild(sectionTitle("战斗常数"));
    box.appendChild(NF.h("div", { class: "ws-grid" },
      field("暴击倍率", numInput(d, "crit_multiplier")),
      field("伤害浮动下限", numInput(d.variance, "0")),
      field("伤害浮动上限", numInput(d.variance, "1")),
      field("攻击系数", numInput(d, "atk_factor")),
      field("免伤常数 K", numInput(d, "defense_constant")),
      field("最小伤害", intInput(d, "min_damage")),
      field("最大刻数", intInput(d, "max_ticks")),
      field("行动槽阈值", numInput(d, "gauge_threshold")),
      field("暴击上限 %", numInput(d, "crit_cap")),
      field("闪避上限 %", numInput(d, "dodge_cap")),
      field("对战种子分隔符", textInput(d, "seed_separator", "small"))));
    box.appendChild(sectionTitle("战报播放"));
    box.appendChild(NF.h("div", { class: "ws-grid" },
      field("每条停顿 ms", intInput(d.playback, "message_delay_ms")),
      field("行动间隔（每 N 次）", intInput(d.playback, "action_pause_every")),
      field("行动间停顿 ms", intInput(d.playback, "action_pause_ms"))));

    d.battle_log = d.battle_log || {};
    box.appendChild(sectionTitle("战报模板（{a}/{b}/{skill}/{damage}/{heal} 等占位）"));
    var ltable = NF.h("table", { class: "ws-table" });
    Object.keys(d.battle_log).forEach(function (key) {
      ltable.appendChild(NF.h("tr", null,
        NF.h("td", { style: { width: "160px" } }, keyInput(d.battle_log, key)),
        NF.h("td", null, textInput(d.battle_log, key, "wide")),
        NF.h("td", null, removeButton(function () {
          delete d.battle_log[key];
          rerenderMain();
        }))));
    });
    box.appendChild(ltable);
    box.appendChild(NF.h("button", {
      class: "ws-add",
      onclick: function () {
        d.battle_log["new_template_" + Object.keys(d.battle_log).length] = "";
        rerenderMain();
      }
    }, "＋ 添加模板"));

    d.buffs = d.buffs || {};
    box.appendChild(sectionTitle("状态标记文案（buff）"));
    var btable = NF.h("table", { class: "ws-table" },
      NF.h("tr", null, ["id", "名称", "详情", "说明", "操作"]
        .map(thCell)));
    Object.keys(d.buffs).forEach(function (bid) {
      var b = d.buffs[bid];
      btable.appendChild(NF.h("tr", null,
        NF.h("td", null, keyInput(d.buffs, bid)),
        NF.h("td", null, textInput(b, "name", "small")),
        NF.h("td", null, textInput(b, "detail", "wide")),
        NF.h("td", null, textInput(b, "desc", "wide")),
        NF.h("td", null, removeButton(function () {
          delete d.buffs[bid];
          rerenderMain();
        }))));
    });
    box.appendChild(btable);
    box.appendChild(NF.h("button", {
      class: "ws-add",
      onclick: function () {
        d.buffs["new_buff_" + Object.keys(d.buffs).length] =
          { name: "新状态", detail: "", desc: "" };
        rerenderMain();
      }
    }, "＋ 添加状态"));
    return box;
  }

  function renderUiForm(d) {
    var box = NF.h("div", null);
    var table = NF.h("table", { class: "ws-table" },
      NF.h("tr", null, ["键", "文案"].map(thCell)));
    Object.keys(d).forEach(function (key) {
      table.appendChild(NF.h("tr", null,
        NF.h("td", { style: { width: "180px" } }, keyInput(d, key)),
        NF.h("td", null, textInput(d, key, "wide")),
        NF.h("td", null, removeButton(function () {
          delete d[key];
          rerenderMain();
        }))));
    });
    box.appendChild(table);
    box.appendChild(NF.h("button", {
      class: "ws-add",
      onclick: function () {
        d["new_key_" + Object.keys(d).length] = "";
        rerenderMain();
      }
    }, "＋ 添加文案"));
    return box;
  }

  var FORMS = {
    system: renderSystemForm,
    attributes: renderAttributesForm,
    skills: renderSkillsForm,
    titles: renderTitlesForm,
    battle: renderBattleForm,
    ui: renderUiForm
  };

  /* ---------------- 页面渲染 ---------------- */

  function rerenderMain() {
    if (!els.main) return;
    NF.clear(els.main);
    els.main.appendChild(buildToolbar());
    if (state.rawMode) {
      els.main.appendChild(buildRawEditor());
    } else {
      els.main.appendChild(FORMS[state.tab](state.files[state.tab]));
    }
    els.main.appendChild(buildTestPanel());
    renderTabs();
  }

  function buildToolbar() {
    var rawBox = NF.h("input", {
      class: "simple-box", type: "checkbox",
      checked: state.rawMode ? "" : null,
      onchange: function (e) { state.rawMode = e.target.checked; state.jsonError = ""; rerenderMain(); }
    });
    return NF.h("div", { class: "ws-toolbar" },
      NF.h("label", { class: "simple-toggle" }, rawBox,
        NF.h("span", null, "JSON 源码模式")),
      NF.h("button", { class: "btn", onclick: restoreBaseline }, "还原到已生效配置"),
      NF.h("span", { class: "hint" },
        "编辑只影响草稿；「试运行」用草稿打一场，「保存并生效」才会写入配置"));
  }

  function buildRawEditor() {
    var area = NF.h("textarea", {
      class: "ws-json-area", spellcheck: "false",
      oninput: function (e) {
        try {
          state.files[state.tab] = JSON.parse(e.target.value);
          state.jsonError = "";
          markEdited();
        } catch (err) {
          state.jsonError = "JSON 解析失败：" + err.message;
        }
        renderStatus();
      }
    }, JSON.stringify(state.files[state.tab], null, 2));
    return NF.h("div", null, area,
      NF.h("div", { class: "ws-json-error", id: "ws-json-error" }, state.jsonError));
  }

  /* ---------- 试运行 ---------- */

  function buildTestPanel() {
    var aInput = NF.h("input", { class: "ws-input", type: "text", value: "测试甲",
                                 placeholder: "红方名字" });
    var bInput = NF.h("input", { class: "ws-input", type: "text", value: "测试乙",
                                 placeholder: "蓝方名字" });
    els.testA = aInput;
    els.testB = bInput;
    els.testResult = NF.h("div", null);
    return NF.h("div", { class: "ws-test" },
      NF.h("div", { class: "ws-test-row" },
        NF.h("strong", null, "试运行："),
        aInput, NF.h("span", { class: "ws-static" }, "VS"), bInput,
        NF.h("button", { class: "btn primary", onclick: runPreview }, "用草稿配置打一场"),
        NF.h("button", { class: "btn save", onclick: saveConfig }, "保存并生效")),
      NF.h("div", { class: "ws-status", id: "ws-status" }),
      els.testResult);
  }

  function setStatus(text, kind) {
    var el = NF.qs("#ws-status");
    if (!el) return;
    el.textContent = text || "";
    el.className = "ws-status" + (kind ? " " + kind : "");
  }

  function renderStatus() {
    if (state.jsonError) setStatus(state.jsonError, "err");
  }

  function runPreview() {
    if (state.busy) return;
    state.busy = true;
    setStatus("正在用草稿配置试运行…");
    apiPreview(state.files, els.testA.value.trim(), els.testB.value.trim())
      .then(function (res) {
        if (!res.ok) {
          setStatus("草稿配置有误：" + res.error, "err");
          NF.clear(els.testResult);
          return;
        }
        setStatus("试运行成功（草稿版本 " + res.version + "），未写入配置文件。", "ok");
        renderPreviewResult(res.preview);
      })
      .catch(function (e) { setStatus("请求失败：" + String(e.message || e), "err"); })
      .then(function () { state.busy = false; });
  }

  function renderPreviewResult(preview) {
    NF.clear(els.testResult);
    var r = preview.result;
    var headline = r.draw ? "平局" : (r.winner + " 获胜");
    els.testResult.appendChild(NF.h("div", { class: "ws-result-summary" },
      headline + " · 历时 " + r.ticks + " 刻 · 红方伤害 " + Math.round(r.damage.a)
        + " · 蓝方伤害 " + Math.round(r.damage.b)));
    preview.fighters.forEach(function (f, i) {
      var sideCls = i === 0 ? "side-a" : "side-b";
      els.testResult.appendChild(NF.h("div", { class: "ws-fighter-line" },
        NF.h("span", { class: sideCls }, f.name),
        " · " + f.title.name + " · 战力 " + f.power + " · 技能：" +
        f.skills.map(function (s) { return s.name; }).join("、")));
    });
    var logBox = NF.h("div", { class: "ws-logbox" });
    preview.log.forEach(function (e) {
      var cls = e.template === "tick_marker" ? "l-round" : "";
      logBox.appendChild(NF.h("div", { class: cls }, e.text));
    });
    els.testResult.appendChild(NF.h("details", { class: "ws-log-toggle" },
      NF.h("summary", null, "展开完整战报（" + preview.log.length + " 条）"),
      logBox));
  }

  function saveConfig() {
    if (state.busy) return;
    if (!window.confirm("保存后所有对战结果都会按新配置重新推导（同名结果将改变）。确认保存并生效？")) {
      return;
    }
    state.busy = true;
    setStatus("正在校验并保存…");
    apiSave(state.files)
      .then(function (res) {
        if (!res.ok) {
          setStatus("保存失败：" + res.error, "err");
          return;
        }
        state.baseline = clone(state.files);
        setStatus("已保存并生效，当前版本 " + res.version + "。", "ok");
        renderTabs();
      })
      .catch(function (e) { setStatus("请求失败：" + String(e.message || e), "err"); })
      .then(function () { state.busy = false; });
  }

  function restoreBaseline() {
    if (!state.baseline) return;
    if (isDirty() && !window.confirm("丢弃当前草稿，还原到已生效的配置？")) return;
    state.files = clone(state.baseline);
    state.jsonError = "";
    rerenderMain();
    setStatus("已还原到已生效配置（仍是草稿，需保存才会生效）。");
  }

  /* ---------- 页签 ---------- */

  function renderTabs() {
    if (!els.tabs) return;
    NF.clear(els.tabs);
    FILES.forEach(function (key) {
      var btn = NF.h("button", {
        class: "ws-tab" + (state.tab === key ? " active" : ""),
        onclick: function () {
          if (state.rawMode) {
            /* 源码模式下切换页签前保留已解析结果 */
            state.jsonError = "";
          }
          state.tab = key;
          rerenderMain();
        }
      }, FILE_NAMES[key] || key);
      if (state.files && state.baseline
          && JSON.stringify(state.files[key]) !== JSON.stringify(state.baseline[key])) {
        btn.appendChild(NF.h("span", { class: "dirty" }, "●"));
      }
      els.tabs.appendChild(btn);
    });
  }

  function renderAll() {
    var root = NF.qs("#app");
    NF.clear(root);
    root.appendChild(NF.h("header", { class: "ws-header" },
      NF.h("a", { class: "btn", href: "/" }, "← 返回竞技场"),
      NF.h("h1", { class: "ws-title" }, "🛠️ 创意工坊"),
      NF.h("span", { class: "ws-note" },
        "可视化编辑全部配置 JSON · 试运行不写盘 · 保存后立即生效"),
      NF.h("span", { class: "ws-version" },
        "当前生效版本 " + (state.baselineVersion || "?"))));
    var body = NF.h("div", { class: "ws-body" });
    els.tabs = NF.h("div", { class: "ws-tabs" });
    els.main = NF.h("div", { class: "ws-main" });
    body.appendChild(els.tabs);
    body.appendChild(els.main);
    root.appendChild(body);
    rerenderMain();
  }

  /* ---------------- 启动 ---------------- */

  function init() {
    apiConfig()
      .then(function (data) {
        state.files = data.files;
        state.baseline = clone(data.files);
        state.baselineVersion = data.version;
        renderAll();
      })
      .catch(function (e) {
        document.body.appendChild(NF.h("div", { class: "toast show" },
          "读取配置失败：" + String((e && e.message) || e)));
      });
  }

  init();
})();
