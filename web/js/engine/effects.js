/* 最小原子注册表与图编译 —— namefight/effects.py 的移植。
   技能 / 状态逻辑统一为 {nodes, edges} 图；执行顺序与随机数消耗顺序完全
   由配置数组顺序确定（确定性契约，改变即 breaking）。
   注册表同时驱动配置校验、共鸣规格与编辑器表单。 */
(function (root) {
  "use strict";
  var NFE = root.NFE = root.NFE || {};

  var HOOKS = [
    "battle_start", "action_interrupt", "action_start", "before_attack",
    "on_attack", "on_defend", "on_hit_landed", "on_hit_taken", "after_action",
    "on_status_gain", "on_status_lose", "on_attack_miss", "on_lethal"
  ];

  var STATUS_HOOKS = [
    "on_status_apply", "on_status_tick", "on_status_expire",
    "on_owner_action", "on_owner_action_consume", "on_owner_attack_hit"
  ];

  var ALL_HOOKS = HOOKS.concat(STATUS_HOOKS);

  /* ParamSpec：与 Python dataclass 同字段 */
  function P(key, kind, opts) {
    opts = opts || {};
    return {
      key: key,
      kind: kind || "float",
      fmt: opts.fmt !== undefined ? opts.fmt : (kind === "pct" ? "pct" : (kind === "float" ? "num" : null)),
      clamp: opts.clamp || null,
      unit: opts.unit || null,
      link: !!opts.link,
      required: opts.required !== undefined ? opts.required : true,
      options: opts.options || null,
      show_if: opts.show_if || null
    };
  }

  function _pct(key, clamp, link, unit, required, show_if) {
    return P(key, "pct", { fmt: "pct", clamp: clamp || null, link: !!link,
                           unit: unit || null, required: required !== undefined ? required : true,
                           show_if: show_if || null });
  }

  function _num(key, clamp, link, unit, required, show_if) {
    return P(key, "float", { fmt: "num", clamp: clamp || null, link: !!link,
                             unit: unit || null, required: required !== undefined ? required : true,
                             show_if: show_if || null });
  }

  function _turns(key, clamp, link, required) {
    return P(key, "turns", { fmt: "turns", clamp: clamp || [1, 20], link: !!link,
                             required: required !== undefined ? required : true });
  }

  function _st() {
    return P("status", "text", { fmt: null });
  }

  function specApplicable(specs, params, spec) {
    if (!spec.show_if) return true;
    var depKey = spec.show_if[0], allowed = spec.show_if[1];
    var current;
    if (depKey in params) {
      current = params[depKey];
    } else {
      var dep = null;
      for (var i = 0; i < specs.length; i++) {
        if (specs[i].key === depKey) { dep = specs[i]; break; }
      }
      current = (dep && dep.options) ? dep.options[0] : null;
    }
    return allowed.indexOf(current) >= 0;
  }

  var CMP_SOURCES = [
    "self.hp_pct", "enemy.hp_pct",
    "self.atk", "enemy.atk",
    "self.def", "enemy.def",
    "self.spd", "enemy.spd",
    "self.crit", "enemy.crit",
    "self.dodge", "enemy.dodge",
    "self.gauge_pct", "enemy.gauge_pct"
  ];
  var CMP_RIGHT = CMP_SOURCES.concat(["const"]);
  var CMP_OPS = ["lt", "le", "gt", "ge"];

  var CONDITIONS = {
    chance: { params: [_pct("chance", [0.02, 0.95], true)] },
    compare: { params: [
      P("left", "enum", { options: CMP_SOURCES }),
      P("op", "enum", { options: CMP_OPS }),
      P("right", "enum", { options: CMP_RIGHT }),
      _pct("value", [0.0, 1.0], false, null, false, ["right", ["const"]])
    ] },
    stacks_cmp: { params: [
      _st(),
      P("target", "enum", { options: ["self", "enemy"] }),
      P("op", "enum", { options: CMP_OPS }),
      _num("value", [0.0, null], false, null, false)
    ] },
    has_status: { params: [_st()] },
    no_status: { params: [_st()] },
    has_marker: { params: [
      P("key", "text", { fmt: null }),
      P("op", "enum", { options: CMP_OPS, required: false }),
      _num("count", [0.0, null], false, null, false, ["op", CMP_OPS])
    ] },
    no_marker: { params: [P("key", "text", { fmt: null })] },
    once_per_battle: { params: [P("key", "text", { fmt: null })] },
    last_crit: { params: [] }
  };

  var OPS = {
    strike: {
      hooks: ["on_attack", "action_interrupt", "on_defend", "on_hit_landed",
              "on_owner_action_consume", "on_status_apply",
              "on_status_gain", "on_status_lose"],
      params: [
        P("target", "enum", { options: ["enemy", "self"] }),
        _pct("mult", [0.05, 8.0], true, null, false),
        P("basis", "enum", { options: ["none", "recorded_sum", "taken_absorbed"], required: false }),
        _pct("value", [0.05, 8.0], true, null, false, ["basis", ["recorded_sum", "taken_absorbed"]]),
        P("real", "bool", { required: false }),
        _pct("pen", [0.0, 1.0], true, null, false),
        _pct("crit_bonus", [0.0, 1.0], true, null, false),
        P("must_hit", "bool", { required: false }),
        P("mode", "enum", { options: ["extra", "replace", "append"], required: false }),
        _pct("lifesteal", [0.0, 1.5], true, null, false),
        P("event", "text", { required: false })
      ],
      logged: true
    },
    hit_mod: {
      hooks: ["on_attack"],
      params: [
        _pct("mult", [0.1, 6.0], true, null, false),
        _pct("pen", [0.0, 1.0], true, null, false),
        _pct("crit_bonus", [0.0, 1.0], true, null, false),
        P("must_hit", "bool", { required: false }),
        P("announce", "bool", { required: false }),
        P("event", "text", { required: false })
      ],
      logged: true
    },
    taken_mod: {
      hooks: ["on_defend"],
      params: [_pct("cut", [0.01, 0.9], true), P("event", "text", { required: false })],
      logged: false
    },
    grant_immune: {
      hooks: ["on_defend"],
      params: [P("event", "text", { required: false })],
      logged: false
    },
    stat_mod: {
      hooks: ["after_action", "on_status_apply", "on_status_tick",
              "on_status_expire", "on_owner_action", "on_owner_attack_hit",
              "on_status_gain", "on_status_lose"],
      params: [
        P("target", "enum", { options: ["self", "enemy"] }),
        P("stat", "enum", { options: ["hp", "atk", "def", "spd", "crit", "dodge"] }),
        _num("gain", [0.0, null], true, null, false),
        P("basis", "enum", { options: ["flat", "recorded_lifesteal"], required: false }),
        _pct("value", [0.01, 4.0], true, null, false),
        P("status", "text", { required: false }),
        P("event", "text", { required: false })
      ],
      logged: false
    },
    hp_mod: {
      hooks: ["on_attack", "action_start", "action_interrupt", "on_hit_taken",
              "on_status_apply", "on_status_tick", "on_status_expire",
              "on_owner_action", "on_owner_attack_hit", "on_status_gain",
              "on_status_lose", "on_lethal"],
      params: [
        P("target", "enum", { options: ["self", "enemy"] }),
        P("type", "enum", { options: ["heal", "loss"] }),
        P("basis", "enum", { options: ["flat", "maxhp", "curhp", "applier_atk", "dealt"] }),
        _num("value", [0.0, null], true, "hp", false, ["basis", ["flat"]]),
        _pct("ratio", [0.0, 2.0], true, null, false, ["basis", ["maxhp", "curhp", "applier_atk", "dealt"]]),
        P("can_kill", "bool", { required: false, show_if: ["type", ["loss"]] }),
        P("floor1", "bool", { required: false, show_if: ["type", ["loss"]] }),
        P("event", "text", { required: false })
      ],
      logged: true
    },
    gauge_mod: {
      hooks: ["on_attack", "action_interrupt", "on_hit_landed", "on_owner_attack_hit",
              "on_status_expire", "on_status_gain", "on_status_lose"],
      params: [
        P("target", "enum", { options: ["self", "enemy"] }),
        _num("gain", [-20000.0, 20000.0], true, "gauge")
      ],
      logged: true
    },
    hp_swap: {
      hooks: ALL_HOOKS,
      params: [P("event", "text", { required: false })],
      logged: true
    },
    apply_status: {
      hooks: ALL_HOOKS,
      params: [
        P("status", "text", { fmt: null }),
        P("target", "enum", { options: ["self", "enemy"] })
      ],
      logged: true
    },
    cleanse: {
      hooks: ["action_start"],
      params: [
        P("scope", "enum", { options: ["both", "self", "enemy"] }),
        _num("value", [0.0, null], true, "hp"),
        _num("per", [0.0, null], true, "hp")
      ],
      logged: true
    },
    skip_action: {
      hooks: ["on_attack", "on_owner_action"],
      params: [P("event", "text", { required: false })],
      logged: false
    },
    record: {
      hooks: ["on_hit_taken", "on_owner_attack_hit", "on_status_apply"],
      params: [
        _st(),
        P("what", "enum", { options: ["damage_taken", "lifesteal"] }),
        _turns("cap", [1, 20], true, false)
      ],
      logged: false
    },
    marker: {
      hooks: ALL_HOOKS,
      params: [
        P("key", "text", { fmt: null }),
        P("action", "enum", { options: ["set", "clear", "toggle", "add", "sub"] }),
        _num("value", [-20.0, 20.0], false, null, false, ["action", ["add", "sub"]]),
        _turns("turns", [1, 40], true, false)
      ],
      logged: false
    },
    status_ctl: {
      hooks: ALL_HOOKS,
      params: [
        _st(),
        P("target", "enum", { options: ["self", "enemy"] }),
        P("op", "enum", { options: ["extend", "shorten", "stacks", "clear"] }),
        _num("value", [-20.0, 20.0], true, null, false, ["op", ["extend", "shorten", "stacks"]]),
        P("event", "text", { required: false })
      ],
      logged: true
    },
    loop: {
      params: [
        P("max", "int", { fmt: "num" }),
        _pct("decay", [0.3, 0.99], true, null, false, ["mode", ["chain"]]),
        P("mode", "enum", { options: ["chain", "count"], required: false })
      ]
    }
  };

  var STRUCTS = ["loop"];
  var OP_TYPES = Object.keys(OPS).filter(function (k) { return STRUCTS.indexOf(k) < 0; });

  var DEFAULT_RESONANCE_SPEC = ["pct", 0.02, 5.0];

  function paramSpecs(kind, type_, statusParams) {
    var params;
    if (kind === "condition") {
      var cspec = CONDITIONS[type_];
      params = cspec ? cspec.params : [];
    } else if (kind === "struct") {
      params = OPS[type_].params;
    } else {
      var ospec = OPS[type_];
      params = ospec ? ospec.params : [];
    }
    var out = {};
    for (var i = 0; i < params.length; i++) out[params[i].key] = params[i];
    if (kind === "op" && type_ === "apply_status" && statusParams) {
      var sp = statusParams() || {};
      for (var key in sp) {
        if (!Object.prototype.hasOwnProperty.call(sp, key)) continue;
        if (!(key in out)) out[key] = sp[key];
      }
    }
    return out;
  }

  function nodeParamSpec(kind, type_, key, statusParams) {
    var specs = paramSpecs(kind, type_, statusParams);
    var ps = specs[key];
    if (ps) return ps;
    return P(key, "float", { fmt: "num" });
  }

  function linkableParams(kind, type_, statusParams) {
    var specs = paramSpecs(kind, type_, statusParams);
    return Object.keys(specs).filter(function (k) { return specs[k].link; });
  }

  function validateParamValue(ps, value) {
    var key = ps.key;
    if (ps.kind === "float" || ps.kind === "pct" || ps.kind === "int" || ps.kind === "turns") {
      if (typeof value === "string") {
        if (value.indexOf("$") >= 0) {
          try { NFE.exprCheck(value); } catch (e) {
            throw new Error("参数 " + key + " 的表达式非法: " + e.message);
          }
          return value;
        }
        throw new Error("参数 " + key + " 必须是数字或 $ 表达式");
      }
      if (typeof value !== "number") throw new Error("参数 " + key + " 必须是数字");
      if (ps.kind === "int" || ps.kind === "turns") {
        if (value !== Math.trunc(value)) throw new Error("参数 " + key + " 必须是整数");
      }
      if (ps.kind === "turns" && value < 1) throw new Error("参数 " + key + " 必须 >= 1");
    } else if (ps.kind === "bool") {
      if (typeof value !== "boolean") throw new Error("参数 " + key + " 必须是布尔值");
    } else if (ps.kind === "enum") {
      if ((ps.options || []).indexOf(value) < 0) {
        throw new Error("参数 " + key + " 取值必须是 " + (ps.options || []).join("/") + " 之一");
      }
    } else {
      if (typeof value !== "string" || !value) throw new Error("参数 " + key + " 必须是非空字符串");
    }
    return value;
  }

  /* 编译 {nodes, edges} 为 {hook: [执行树, ...]}；节点表 / 邻接表用 Map
     保持配置数组顺序（确定性契约）。 */
  function compileGraph(graph, statusParams, validate) {
    if (validate === undefined) validate = true;
    if (!graph || typeof graph !== "object") {
      throw new Error("effect 必须是包含 nodes/edges 的对象");
    }
    var nodes = graph.nodes;
    var edges = graph.edges;
    if (!Array.isArray(nodes) || !nodes.length) throw new Error("effect.nodes 必须是非空数组");
    if (!Array.isArray(edges)) throw new Error("effect.edges 必须是数组");

    var byId = new Map();
    for (var ni = 0; ni < nodes.length; ni++) {
      var node = nodes[ni];
      if (!node || typeof node !== "object") throw new Error("节点必须是对象");
      var nid = node.id;
      if (typeof nid !== "string" || !nid) throw new Error("节点缺少 id");
      if (byId.has(nid)) throw new Error("节点 id 重复: " + nid);
      var kind = node.kind, type_ = node.type;
      if (["trigger", "condition", "op", "struct"].indexOf(kind) < 0) {
        throw new Error("节点 " + nid + " 的 kind 非法: " + kind);
      }
      if (kind === "trigger") {
        if (HOOKS.indexOf(type_) < 0 && STATUS_HOOKS.indexOf(type_) < 0) {
          throw new Error("触发节点 " + nid + " 的时机非法: " + type_);
        }
      } else if (kind === "condition") {
        if (!CONDITIONS[type_]) throw new Error("条件节点 " + nid + " 的类型未注册: " + type_);
      } else if (kind === "struct") {
        if (STRUCTS.indexOf(type_) < 0) throw new Error("结构节点 " + nid + " 的类型未注册: " + type_);
      } else if (!OPS[type_] || STRUCTS.indexOf(type_) >= 0) {
        throw new Error("原子节点 " + nid + " 的类型未注册: " + type_);
      }
      var params = node.params || {};
      if (kind === "trigger" && Object.keys(params).length) {
        throw new Error("触发节点 " + nid + " 不能带参数");
      }
      if (validate && kind !== "trigger") {
        var reg = kind === "condition" ? CONDITIONS[type_].params : OPS[type_].params;
        var sp = null;
        if (kind === "op" && type_ === "apply_status" && statusParams) {
          var selfStatus = params.status;
          sp = function () { return statusParams(selfStatus); };
        }
        var specs = paramSpecs(kind, type_, sp);
        for (var key in params) {
          if (!Object.prototype.hasOwnProperty.call(params, key)) continue;
          var ps = specs[key];
          if (!ps) throw new Error("节点 " + nid + "（" + type_ + "）没有参数 " + key);
          validateParamValue(ps, params[key]);
        }
        for (var ri = 0; ri < reg.length; ri++) {
          if (reg[ri].required && !(reg[ri].key in params)) {
            throw new Error("节点 " + nid + "（" + type_ + "）缺少参数 " + reg[ri].key);
          }
        }
      }
      var nodeOut = {};
      for (var nk in node) {
        if (Object.prototype.hasOwnProperty.call(node, nk)) nodeOut[nk] = node[nk];
      }
      nodeOut.params = {};
      for (var pk in params) {
        if (Object.prototype.hasOwnProperty.call(params, pk)) nodeOut.params[pk] = params[pk];
      }
      byId.set(nid, nodeOut);
    }

    var children = new Map();      // nid -> [[gate, 目标id], ...]（按边序）
    var incoming = new Map();      // nid -> 入边计数（树结构 = 至多 1）
    byId.forEach(function (_n, id) { children.set(id, []); incoming.set(id, 0); });
    for (var ei = 0; ei < edges.length; ei++) {
      var edge = edges[ei];
      if (!edge || typeof edge !== "object" || !("from" in edge) || !("to" in edge)) {
        throw new Error("边必须是包含 from/to 的对象");
      }
      var src = edge.from, dst = edge.to;
      var gate = edge.gate !== undefined ? edge.gate : "pass";
      if (gate !== "pass" && gate !== "fail") throw new Error("边的 gate 非法: " + gate);
      if (!byId.has(src) || !byId.has(dst)) {
        throw new Error("边引用了不存在的节点: " + src + " -> " + dst);
      }
      if (byId.get(dst).kind === "trigger") throw new Error("触发节点 " + dst + " 不能有入边");
      if (gate === "fail" && byId.get(src).kind !== "condition") {
        throw new Error("gate=fail 的边只能出自条件节点: " + src + " -> " + dst);
      }
      incoming.set(dst, incoming.get(dst) + 1);
      if (incoming.get(dst) > 1) throw new Error("节点 " + dst + " 有多条入边（图必须为树结构）");
      children.get(src).push([gate, dst]);
    }

    var plan = {};
    var visited = new Set();

    function build(nid, hook) {
      if (visited.has(nid)) throw new Error("图中存在环（经 " + nid + "）");
      visited.add(nid);
      var node = byId.get(nid);
      if (validate && node.kind === "op" && OPS[node.type].hooks.indexOf(hook) < 0) {
        throw new Error("原子 " + node.type + " 不能挂在 " + hook + " 之下（允许: " +
                        OPS[node.type].hooks.join("/") + "）");
      }
      var kids = children.get(nid).map(function (gd) {
        return [gd[0], build(gd[1], hook)];
      });
      return [node, kids];
    }

    var order = [];
    byId.forEach(function (node, id) { order.push([id, node]); });
    for (var oi = 0; oi < order.length; oi++) {
      var id = order[oi][0], node = order[oi][1];
      if (node.kind !== "trigger") continue;
      var tree = build(id, node.type);
      (plan[node.type] = plan[node.type] || []).push(tree);
    }

    if (visited.size !== byId.size) {
      var missing = [];
      byId.forEach(function (_n, mid) { if (!visited.has(mid)) missing.push(mid); });
      throw new Error("存在未挂在触发节点之下的节点: " + missing.join("、"));
    }
    return plan;
  }

  NFE.HOOKS = HOOKS;
  NFE.STATUS_HOOKS = STATUS_HOOKS;
  NFE.ALL_HOOKS = ALL_HOOKS;
  NFE.P = P;
  NFE.CONDITIONS = CONDITIONS;
  NFE.OPS = OPS;
  NFE.STRUCTS = STRUCTS;
  NFE.OP_TYPES = OP_TYPES;
  NFE.CMP_SOURCES = CMP_SOURCES;
  NFE.CMP_OPS = CMP_OPS;
  NFE.DEFAULT_RESONANCE_SPEC = DEFAULT_RESONANCE_SPEC;
  NFE.specApplicable = specApplicable;
  NFE.paramSpecs = paramSpecs;
  NFE.nodeParamSpec = nodeParamSpec;
  NFE.linkableParams = linkableParams;
  NFE.validateParamValue = validateParamValue;
  NFE.compileGraph = compileGraph;
})(typeof window !== "undefined" ? window : globalThis);
