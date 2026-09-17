/* 斗士派生：名字 -> MD5 -> 属性 / 技能 / 称号 —— namefight/fighter.py 的移植。
   确定性契约：派生 = f(归一化名字, 配置快照) 的纯函数；主派生与技能个性化
   的随机数消耗顺序与 Python 完全一致（改变即 breaking）。 */
(function (root) {
  "use strict";
  var NFE = root.NFE = root.NFE || {};
  var pyRound = NFE.pyRound, pyInt = NFE.pyInt, pyRoundN = NFE.pyRoundN;
  var pyFix = NFE.pyFix, pyStr = NFE.pyStr, pyLen = NFE.pyLen;
  var formatPct = NFE.formatPct, formatNum = NFE.formatNum;
  var renderTemplate = NFE.renderTemplate;
  var isExpr = NFE.isExpr;
  var TITLE_FIELD_POOLS = NFE.TITLE_FIELD_POOLS;

  // 对战实时技能数据的占位符：live 文本中共鸣数值位 = "\x01" + 槽位序号
  var LIVE_MARKER = "\u0001";

  function InvalidName(code) {
    var e = new Error(code);
    e.name = "InvalidName";
    e.code = code;
    return e;
  }

  function normalizeName(raw, system) {
    var name = typeof raw === "string" ? raw : "";
    if (system.nameTrim) name = name.trim();
    if (!system.nameCaseSensitive) name = name.toLowerCase();
    if (pyLen(name) < system.nameMinLength) throw InvalidName("empty_name");
    if (pyLen(name) > system.nameMaxLength) throw InvalidName("name_too_long");
    return name;
  }

  function deriveFighter(rawName, game) {
    var system = game.system;
    var normalized = normalizeName(rawName, system);
    var digest = NFE.md5Hex(normalized);
    var rng = new NFE.DetRng(BigInt("0x" + digest));

    // 属性三角形投掷（顺序 = 配置顺序）；非百分比投掷即取整
    var attrs = {};
    for (var ai = 0; ai < game.attributes.length; ai++) {
      var a = game.attributes[ai];
      var roll = rng.nextTriangular(a.min, a.max);
      attrs[a.id] = a.format !== "percent" ? pyRound(roll) : roll;
    }

    var count = rng.nextTriangularRange(game.skillCountMin, game.skillCountMax);
    var skills = rng.sampleWeighted(game.skills.map(function (s) { return [s, s.weight]; }), count);

    var structure = rng.pickWeighted(game.titleStructures.map(function (s) { return [s, s.weight]; }));
    var titleFields = {};
    var coreId = null;
    for (var fi = 0; fi < structure.fields.length; fi++) {
      var fname = structure.fields[fi];
      var pool = game.titlePools[TITLE_FIELD_POOLS[fname]];
      var candidates = pool;
      if (fname === "core2" && coreId !== null) {
        var filtered = pool.filter(function (t) { return t.id !== coreId; });
        candidates = filtered.length ? filtered : pool;
      }
      var item = rng.pickWeighted(candidates.map(function (t) { return [t, t.weight]; }));
      titleFields[fname] = item.id;
      if (fname === "core") coreId = item.id;
    }

    // 称号字段小额加成（不消耗随机数）
    var bonuses = titleBonusItems(titleFields, structure, game);
    for (var bi = 0; bi < bonuses.length; bi++) {
      attrs[bonuses[bi][0]] = Math.max(1.0, attrs[bonuses[bi][0]] + bonuses[bi][1]);
    }

    var powerSum = 0.0;
    for (ai = 0; ai < game.attributes.length; ai++) {
      a = game.attributes[ai];
      powerSum += attrs[a.id] * a.powerWeight;
    }
    return {
      name: (typeof rawName === "string" && rawName) ? rawName : normalized,
      normalized: normalized,
      digest: digest,
      attrs: attrs,
      skillIds: skills.map(function (s) { return s.id; }),
      titleStructureId: structure.id,
      titleFields: titleFields,
      power: pyRound(powerSum)
    };
  }

  /* ---- 节点参数规格 / 量纲查询（注册表驱动） ---- */

  function nodeSpecs(node, game) {
    if (node.kind === "op" && node.type === "apply_status") {
      var sid = (node.params || {}).status;
      return NFE.paramSpecs("op", "apply_status", function () {
        return game.statusSpecs[sid];
      });
    }
    return NFE.paramSpecs(String(node.kind || ""), String(node.type || ""));
  }

  function paramSpec(node, param, game) {
    var ps = nodeSpecs(node, game)[param];
    if (ps !== undefined && (ps.fmt || ps.clamp)) {
      var lo = ps.clamp ? ps.clamp[0] : null;
      var hi = ps.clamp ? ps.clamp[1] : null;
      return [ps.fmt, lo, hi];
    }
    return NFE.DEFAULT_RESONANCE_SPEC.slice();
  }

  function paramUnit(node, param, game) {
    var ps = nodeSpecs(node, game)[param];
    return ps ? ps.unit : null;
  }

  function graphParamUnit(pgraph, param, game) {
    var nodes = pgraph.nodes || [];
    for (var i = 0; i < nodes.length; i++) {
      if (param in (nodes[i].params || {})) return paramUnit(nodes[i], param, game);
    }
    return null;
  }

  function graphParamValue(pgraph, param, dflt) {
    if (dflt === undefined) dflt = 0.0;
    var nodes = pgraph.nodes || [];
    for (var i = 0; i < nodes.length; i++) {
      var params = nodes[i].params || {};
      if (param in params) return params[param];
    }
    return dflt;
  }

  function walkLinks(pgraph) {
    var out = [];
    var nodes = pgraph.nodes || [];
    for (var i = 0; i < nodes.length; i++) {
      var links = nodes[i].links || [];
      for (var j = 0; j < links.length; j++) out.push([nodes[i], links[j]]);
    }
    return out;
  }

  function applyModifier(nodes, mod) {
    for (var i = 0; i < nodes.length; i++) {
      var params = nodes[i].params || {};
      for (var key in mod) {
        if (!Object.prototype.hasOwnProperty.call(mod, key)) continue;
        if (!(key in params) || typeof params[key] === "string") continue;
        var delta = Number(mod[key]);
        if (key === "chance") {
          params.chance = Math.min(0.95, Math.max(0.02, Number(params.chance) + delta));
        } else if (key === "turns" || key === "ticks" || key === "cap") {
          params[key] = Math.max(1, pyInt(pyRound(Number(params[key]))) + pyInt(pyRound(delta)));
        } else {
          params[key] = Number(params[key]) + delta;
        }
      }
    }
  }

  /* 技能个性化：md5(规范化名字:技能id) 独立种子扰动（消耗顺序固定） */
  function personalizedEffects(fighter, game) {
    var vcfg = game.skillMd5Variance;
    var linkCfg = game.skillVariableLink;
    var nameMod = game.skillNameModifiers;
    var out = [];
    for (var si = 0; si < fighter.skillIds.length; si++) {
      var sid = fighter.skillIds[si];
      var sdef = null;
      for (var k = 0; k < game.skills.length; k++) {
        if (game.skills[k].id === sid) { sdef = game.skills[k]; break; }
      }
      var nodes = [];
      var srcNodes = (sdef.effect && sdef.effect.nodes) || [];
      for (var n = 0; n < srcNodes.length; n++) {
        var src = srcNodes[n];
        var node = { id: src.id, kind: src.kind, type: src.type, params: {} };
        var sp = src.params || {};
        for (var pk in sp) {
          if (Object.prototype.hasOwnProperty.call(sp, pk)) node.params[pk] = sp[pk];
        }
        if ("pos" in src) node.pos = src.pos;
        nodes.push(node);
      }
      var edges = ((sdef.effect && sdef.effect.edges) || []).map(function (e) {
        var c = {};
        for (var ek in e) {
          if (Object.prototype.hasOwnProperty.call(e, ek)) c[ek] = e[ek];
        }
        return c;
      });
      var graph = { nodes: nodes, edges: edges };
      var seedHex = NFE.md5Hex(fighter.normalized + ":" + sid);
      var rng = new NFE.DetRng(BigInt("0x" + seedHex));

      // 熟练度（0~100 三角形投掷）-> 倍率缩放 mastery_on 参数
      var mastery = rng.nextTriangularRange(0, 100);
      var lo = sdef.mastery[0], hi = sdef.mastery[1];
      var mult = lo + (hi - lo) * mastery / 100.0;
      graph.mastery = mastery;
      graph.mastery_mult = mult;
      for (var mi = 0; mi < sdef.masteryOn.length; mi++) {
        var param = sdef.masteryOn[mi];
        for (n = 0; n < nodes.length; n++) {
          var params = nodes[n].params;
          if (!(param in params) || typeof params[param] === "string") continue;
          var scaled = Number(params[param]) * mult;
          if (param === "chance") {
            params.chance = Math.min(0.95, Math.max(0.02, scaled));
          } else if (param === "immune") {
            params.immune = Math.min(0.5, Math.max(0.01, scaled));
          } else {
            params[param] = scaled;
          }
        }
      }
      // value / damage 按节点数组顺序逐个抽倍率
      var vkeys = ["value", "damage"];
      for (var vk = 0; vk < vkeys.length; vk++) {
        var key = vkeys[vk];
        for (n = 0; n < nodes.length; n++) {
          var p2 = nodes[n].params;
          if (key in p2 && typeof p2[key] !== "string") {
            var factor = rng.nextTriangular(vcfg.valueLo, vcfg.valueHi);
            p2[key] = Number(p2[key]) * factor;
          }
        }
      }
      // 前缀 / 后缀（是否 -> 抽取 -> 缩放）
      if (nameMod.prefixChance > 0 && rng.nextFloat() < nameMod.prefixChance) {
        graph.prefix = rng.pickWeighted(nameMod.prefixes.map(function (m) { return [m, m.weight]; })).id;
        graph.prefix_scale = rng.nextTriangular(nameMod.scaleLo, nameMod.scaleHi);
      }
      if (nameMod.suffixChance > 0 && rng.nextFloat() < nameMod.suffixChance) {
        graph.suffix = rng.pickWeighted(nameMod.suffixes.map(function (m) { return [m, m.weight]; })).id;
        graph.suffix_scale = rng.nextTriangular(nameMod.scaleLo, nameMod.scaleHi);
      }
      var pools = [
        [nameMod.prefixes, graph.prefix, "prefix_scale"],
        [nameMod.suffixes, graph.suffix, "suffix_scale"]
      ];
      for (var pp = 0; pp < pools.length; pp++) {
        var pool = pools[pp][0], modId = pools[pp][1], scaleKey = pools[pp][2];
        if (!modId) continue;
        var mdef = null;
        for (k = 0; k < pool.length; k++) {
          if (pool[k].id === modId) { mdef = pool[k]; break; }
        }
        if (mdef) {
          var scaled2 = {};
          var scale = Number(graph[scaleKey] !== undefined ? graph[scaleKey] : 1.0);
          for (var mk in mdef.mod) {
            if (Object.prototype.hasOwnProperty.call(mdef.mod, mk)) {
              scaled2[mk] = Number(mdef.mod[mk]) * scale;
            }
          }
          applyModifier(nodes, scaled2);
        }
      }
      // 共鸣槽位：候选 = 各节点 link=True 的参数（节点数组顺序 × 声明顺序）
      var overrideNodes = {};
      for (var ri = 0; ri < sdef.resonance.length; ri++) {
        if (sdef.resonance[ri].node) overrideNodes[sdef.resonance[ri].node + "\u0000" + sdef.resonance[ri].param] = ri;
      }
      var byParam = {};
      for (ri = 0; ri < sdef.resonance.length; ri++) {
        if (!sdef.resonance[ri].node) byParam[sdef.resonance[ri].param] = ri;
      }
      var consumed = {};
      var slots = 0;
      for (n = 0; n < nodes.length; n++) {
        if (slots >= linkCfg.maxSlots) break;
        var specs = nodeSpecs(nodes[n], game);
        var specList = Object.keys(specs).map(function (k2) { return specs[k2]; });
        var candidates = Object.keys(specs).filter(function (k2) {
          return specs[k2].link && NFE.specApplicable(specList, nodes[n].params, specs[k2]);
        });
        if (!candidates.length) continue;
        var nodeId = String(nodes[n].id !== undefined ? nodes[n].id : "");
        var links = [];
        for (var ci = 0; ci < candidates.length; ci++) {
          if (slots >= linkCfg.maxSlots) break;
          var cparam = candidates[ci];
          if (!(cparam in nodes[n].params)) continue;
          if (typeof nodes[n].params[cparam] === "string") continue;
          var ovI = overrideNodes[nodeId + "\u0000" + cparam];
          if (ovI === undefined) ovI = byParam[cparam];
          var ov = ovI !== undefined ? sdef.resonance[ovI] : null;
          var mode, vdef, rate;
          if (ov !== null && ovI !== undefined && !(ovI in consumed)) {
            consumed[ovI] = true;
            if (!ov.variable) continue;
            mode = ov.mode;
            vdef = null;
            for (k = 0; k < linkCfg.variables.length; k++) {
              if (linkCfg.variables[k].id === ov.variable) { vdef = linkCfg.variables[k]; break; }
            }
            if (!vdef) continue;
            rate = Number(ov.rate);
          } else {
            if (rng.nextFloat() >= linkCfg.chance) continue;
            mode = rng.pickWeighted(linkCfg.modeWeights);
            vdef = rng.pickWeighted(linkCfg.variables.map(function (v) { return [v, v.weight]; }));
            rate = rng.nextTriangular(vdef.rateLo, vdef.rateHi);
          }
          var baseValue = Math.max(1.0, Number(game.attr(vdef.id).base));
          var varExpr = mode === "own" ? "$self." + vdef.id
            : mode === "enemy" ? "$enemy." + vdef.id : null;
          if (varExpr === null) {
            var against = vdef.diffAgainst;
            varExpr = "($self." + vdef.id + " " + (mode === "difference" ? "-" : "+") +
                      " $enemy." + against + ")";
          }
          var base = Number(nodes[n].params[cparam]);
          // 与 Python "%.17g" 等价：String() 为最短往返表示，回读得到同一 double
          nodes[n].params[cparam] =
            "(" + String(base) + ") * (1 + (" + String(rate) + ") * ((" + varExpr +
            ") / " + String(baseValue) + "))";
          links.push({ param: cparam, variable: vdef.id, rate: rate, mode: mode, base: base });
          slots += 1;
        }
        if (links.length) nodes[n].links = links;
      }
      out.push([sdef, graph]);
    }
    return out;
  }

  /* 共鸣系数（前端实时重算与引擎共用口径） */
  function resonanceCoeff(ownGet, enemyGet, link, game) {
    var vid = link.variable;
    var base;
    try {
      base = Math.max(1, Number(game.attr(vid).base));
    } catch (e) {
      base = 1;
    }
    var rate = Number(link.rate !== undefined ? link.rate : 0.0);
    var mode = link.mode !== undefined ? link.mode : "own";
    var raw;
    if (mode === "difference" || mode === "sum") {
      var vdef = null;
      for (var i = 0; i < game.skillVariableLink.variables.length; i++) {
        if (game.skillVariableLink.variables[i].id === vid) { vdef = game.skillVariableLink.variables[i]; break; }
      }
      var against = vdef ? vdef.diffAgainst : vid;
      raw = Number(ownGet(vid));
      var other = Number(enemyGet(against));
      raw = mode === "difference" ? raw - other : raw + other;
    } else if (mode === "enemy") {
      raw = Number(enemyGet(vid));
    } else {
      raw = Number(ownGet(vid));
    }
    return rate * (raw / base);
  }

  function applyResonance(params, coeff, param, spec) {
    var scaled = {};
    for (var k in params) {
      if (Object.prototype.hasOwnProperty.call(params, k)) scaled[k] = params[k];
    }
    if (!(param in scaled)) return scaled;
    var fmt = spec[0], lo = spec[1], hi = spec[2];
    var value = Number(scaled[param]) * (1.0 + coeff);
    if (fmt === "turns") {
      scaled[param] = Math.max(1, pyInt(pyRound(value)));
      if (hi !== null && hi !== undefined) {
        scaled[param] = Math.min(pyInt(hi), scaled[param]);
      }
      return scaled;
    }
    if (lo !== null && lo !== undefined && value < lo) value = lo;
    if (hi !== null && hi !== undefined && value > hi) value = hi;
    scaled[param] = value;
    return scaled;
  }

  function formatField(value, fmt) {
    if (fmt === "turns") return String(pyInt(pyRound(Number(value))));
    if (fmt === "num") return formatNum(Number(value));
    return formatPct(Number(value));
  }

  function sigDecimals(v, cap) {
    if (cap === undefined) cap = 6;
    v = Math.abs(Number(v));
    var decimals = 2;
    while (decimals < cap && v * Math.pow(10, decimals) < 10) decimals += 1;
    return decimals;
  }

  function formatFormulaNumber(displayValue, bracket) {
    if (Math.abs(displayValue) >= 0.1) {
      return bracket ? pyFix(displayValue, 2) : pyFix(displayValue, 2) + "%";
    }
    if (bracket) {
      var p = displayValue * 100.0;
      return pyFix(p, sigDecimals(p)) + "%";
    }
    return pyFix(displayValue, sigDecimals(displayValue)) + "%";
  }

  function formatResonanceFinal(scaledValue, fmt, game) {
    var text = formatField(scaledValue, fmt);
    if (!game) return text;
    if (fmt === "num") {
      return renderTemplate(game.stats.final_damage !== undefined ? game.stats.final_damage : "{v}",
                            { v: text }, game);
    }
    if (fmt === "turns") {
      return renderTemplate(game.stats.final_turns !== undefined ? game.stats.final_turns : "{v}",
                            { v: text }, game);
    }
    return text;
  }

  function estimatedResonancedEff(fighter, pgraph, game) {
    var baseGet = function (vid) { return game.attr(vid).base; };
    var ownGet = function (vid) { return fighter.attrs[vid] !== undefined ? fighter.attrs[vid] : 0; };
    var display = [];
    var coeffs = [];
    var nodes = pgraph.nodes || [];
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var params = node.params || {};
      var links = node.links;
      if (!links) {
        display.push([node, params]);
        continue;
      }
      var disp = {};
      for (var k in params) {
        if (Object.prototype.hasOwnProperty.call(params, k)) disp[k] = params[k];
      }
      for (var j = 0; j < links.length; j++) {
        var link = links[j];
        var param = String(link.param !== undefined ? link.param : "");
        if (!(param in disp)) continue;
        disp[param] = Number(link.base !== undefined ? link.base : disp[param]);
        var coeff = resonanceCoeff(ownGet, baseGet, link, game);
        disp = applyResonance(disp, coeff, param, paramSpec(node, param, game));
        coeffs.push([link, coeff]);
      }
      display.push([node, disp]);
    }
    return [display, coeffs];
  }

  function titleBonusItems(titleFields, structure, game) {
    var items = [];
    for (var i = 0; i < structure.fields.length; i++) {
      var fname = structure.fields[i];
      var fid = titleFields[fname];
      var pool = game.titlePools[TITLE_FIELD_POOLS[fname]];
      var fdef = null;
      for (var j = 0; j < pool.length; j++) {
        if (pool[j].id === fid) { fdef = pool[j]; break; }
      }
      if (!fdef) continue;
      var bonus = fdef.bonus || {};
      for (var attrId in bonus) {
        if (Object.prototype.hasOwnProperty.call(bonus, attrId)) {
          items.push([attrId, pyInt(Number(bonus[attrId]))]);
        }
      }
    }
    return items;
  }

  /* ---- 技能描述：沿链组合「触发词 + 条件从句 + 效果原语句」 ---- */

  function nodeClause(node, disp, game) {
    if (node.kind === "condition") return "cond_" + node.type;
    if (node.kind === "op" && node.type === "apply_status") return "st_" + String(disp.status || "");
    if (node.kind === "op" && node.type === "hp_mod") {
      if (String(disp.type) === "loss") return "op_hp_mod_loss";
      if ("ratio" in disp && String(disp.basis) === "maxhp") return "op_hp_mod_pct";
    }
    if (node.kind === "op" && node.type === "strike" &&
        String(disp.basis !== undefined ? disp.basis : "none") !== "none") {
      return "op_strike_basis";
    }
    if (node.kind === "op" && node.type === "hit_mod" && !("mult" in disp)) {
      return ("crit_bonus" in disp) ? "op_hit_mod_pen_crit" : "op_hit_mod_pen";
    }
    if (node.kind === "op" && node.type === "stat_mod" &&
        String(disp.basis !== undefined ? disp.basis : "flat") === "recorded_lifesteal") {
      return "op_stat_mod_recorded";
    }
    return "op_" + node.type;
  }

  function prettyExpr(text) {
    var out = String(text);
    out = out.replace(/\$self\.mark:([^+\-*\/() ]+)/g, "$1层数");
    out = out.replace(/\$enemy\.mark:([^+\-*\/() ]+)/g, "对方$1层数");
    out = out.replace(/\$self\./g, "自身");
    out = out.replace(/\$enemy\./g, "对方");
    out = out.replace(/pow\(([^,]+),\s*([^)]+)\)/g, "($1)^$2");
    out = out.split(" * ").join(" × ");
    return out;
  }

  function clauseParams(node, disp, game) {
    var out = {};
    var specs = nodeSpecs(node, game);
    for (var key in disp) {
      if (!Object.prototype.hasOwnProperty.call(disp, key)) continue;
      var ps = specs[key];
      var fmt = (ps && ps.fmt) ? ps.fmt : "num";
      var v = disp[key];
      if (typeof v === "boolean") {
        out[key] = v;
      } else if (typeof v === "string") {
        if (key === "status") {
          var sdef = game.statuses[v] || {};
          out[key] = sdef.name !== undefined ? String(sdef.name) : v;
        } else if (key === "stat") {
          try {
            out[key] = game.attr(String(v)).name;
          } catch (e) {
            out[key] = v;
          }
        } else if (isExpr(v)) {
          out[key] = prettyExpr(v);
        } else {
          out[key] = v;
        }
      } else {
        out[key] = formatField(v, fmt);
      }
    }
    if (node.kind === "op" && node.type === "hp_mod" &&
        !("value" in out) && "ratio" in out) {
      out.value = out.ratio;
    }
    if (node.kind === "op" && node.type === "strike" &&
        String(disp.basis !== undefined ? disp.basis : "none") !== "none") {
      var bw = game.stats["lbl_basis_" + String(disp.basis !== undefined ? disp.basis : "none")];
      out.basis_word = String(bw !== undefined ? bw : disp.basis);
    }
    if (node.kind === "condition" && node.type === "compare") {
      var statsWords = game.stats;
      var keys = ["left", "right"];
      for (var i = 0; i < keys.length; i++) {
        var key2 = keys[i];
        var src = String(disp[key2] !== undefined ? disp[key2] : "");
        var word = statsWords["cmp_" + src];
        out[key2] = (src === "const" && key2 === "right")
          ? String(out.value !== undefined ? out.value : "")
          : String(word !== undefined ? word : src);
      }
      var opw = statsWords["cmp_" + String(disp.op !== undefined ? disp.op : "ge")];
      out.op = String(opw !== undefined ? opw : (disp.op !== undefined ? disp.op : "ge"));
    }
    if (node.kind === "condition" && node.type === "stacks_cmp") {
      var sid = String(disp.status !== undefined ? disp.status : "");
      var sdef2 = game.statuses[sid] || {};
      out.status = String(sdef2.name !== undefined ? sdef2.name : sid);
      var opw2 = game.stats["cmp_" + String(disp.op !== undefined ? disp.op : "ge")];
      out.op = String(opw2 !== undefined ? opw2 : (disp.op !== undefined ? disp.op : "ge"));
    }
    if (node.kind === "struct" && node.type === "loop") {
      var mode = String(disp.mode !== undefined ? disp.mode : "chain");
      var tmpl = String(game.stats["cmp_mode_" + mode] !== undefined ? game.stats["cmp_mode_" + mode] : "");
      var decay = out.decay !== undefined ? out.decay
        : formatField(disp.decay !== undefined ? disp.decay : 0.9, "pct");
      out.mode_word = tmpl.split("{decay}").join(String(decay));
    }
    if (node.kind === "op" && node.type === "marker") {
      var aw = game.stats["lbl_marker_" + String(disp.action !== undefined ? disp.action : "set")];
      out.action_word = String(aw !== undefined ? aw : (disp.action !== undefined ? disp.action : "set"));
    }
    if (node.kind === "op" && node.type === "hp_mod") {
      var tw = game.stats["cmp_target_" + String(disp.target !== undefined ? disp.target : "self")];
      out.target_word = String(tw !== undefined ? tw : (disp.target !== undefined ? disp.target : "self"));
    }
    if (node.kind === "op" && node.type === "status_ctl") {
      var tw2 = game.stats["cmp_target_" + String(disp.target !== undefined ? disp.target : "self")];
      out.target_word = String(tw2 !== undefined ? tw2 : (disp.target !== undefined ? disp.target : "self"));
      var ow = game.stats["ctl_" + String(disp.op !== undefined ? disp.op : "stacks")];
      out.op_word = String(ow !== undefined ? ow : (disp.op !== undefined ? disp.op : "stacks"));
    }
    return out;
  }

  function linkFormula(node, link, param, game) {
    var tmpl = game.stats;
    var varId = String(link.variable);
    var varDef = game.attr(varId);
    var varName = varDef.name;
    var varEmoji = varDef.emoji || varName;
    var base = Math.max(1.0, Number(varDef.base));
    var mode = String(link.mode !== undefined ? link.mode : "own");
    var scopeOwn = String(tmpl.scope_own !== undefined ? tmpl.scope_own : "");
    var scopeEnemy = String(tmpl.scope_enemy !== undefined ? tmpl.scope_enemy : "");
    var fieldWord = String(tmpl["field_" + param] !== undefined ? tmpl["field_" + param] : param);
    var fmt = paramSpec(node, param, game)[0];
    var effRaw = Number(link.base !== undefined ? link.base
      : ((node.params || {})[param] !== undefined ? node.params[param] : 0.0));
    var mergedRaw = effRaw * Number(link.rate !== undefined ? link.rate : 0.0) / base;
    var baseDisplay, merged;
    if (fmt === "pct") {
      baseDisplay = formatFormulaNumber(effRaw * 100.0, true);
      merged = formatFormulaNumber(mergedRaw * 100.0, true);
    } else {
      baseDisplay = formatField(effRaw, fmt);
      merged = formatFormulaNumber(mergedRaw * 100.0, false);
    }
    var expr, tail;
    if (mode === "difference" || mode === "sum") {
      var vdef = null;
      for (var i = 0; i < game.skillVariableLink.variables.length; i++) {
        if (game.skillVariableLink.variables[i].id === varId) { vdef = game.skillVariableLink.variables[i]; break; }
      }
      var against = vdef ? vdef.diffAgainst : varId;
      var againstName = game.attr(against).name;
      var exprTmpl = mode === "difference" ? "link_expr_difference" : "link_expr_sum";
      expr = renderTemplate(tmpl[exprTmpl] !== undefined ? tmpl[exprTmpl] : "{emoji}",
                            { own: scopeOwn, enemy: scopeEnemy, emoji: varEmoji }, game);
      var tailTmpl = mode === "difference" ? "link_difference" : "link_sum";
      tail = renderTemplate(tmpl[tailTmpl] !== undefined ? tmpl[tailTmpl] : "",
                            { own: scopeOwn + varName, enemy: scopeEnemy + againstName,
                              field: fieldWord }, game);
    } else if (mode === "enemy") {
      expr = scopeEnemy + varEmoji;
      tail = renderTemplate(tmpl.link_ratio !== undefined ? tmpl.link_ratio : "",
                            { scope: scopeEnemy, stat: varName, field: fieldWord }, game);
    } else {
      expr = varEmoji;
      tail = renderTemplate(tmpl.link_ratio !== undefined ? tmpl.link_ratio : "",
                            { scope: scopeOwn, stat: varName, field: fieldWord }, game);
    }
    var formula = renderTemplate(tmpl.link_formula !== undefined ? tmpl.link_formula : "",
                                 { base: baseDisplay, expr: expr, merged: merged }, game);
    return [formula, tail];
  }

  function childrenMap(pgraph) {
    var children = {};
    var nodes = pgraph.nodes || [];
    for (var i = 0; i < nodes.length; i++) children[nodes[i].id] = [];
    var edges = pgraph.edges || [];
    for (var e = 0; e < edges.length; e++) {
      if (!children[edges[e].from]) children[edges[e].from] = [];
      children[edges[e].from].push([edges[e].gate !== undefined ? edges[e].gate : "pass", edges[e].to]);
    }
    return children;
  }

  function naturalText(pgraph, fighter, game, simple, live) {
    var stats = game.stats;
    var est = estimatedResonancedEff(fighter, pgraph, game);
    var display = est[0];
    var dispById = {};
    var nodeById = {};
    var nodes = pgraph.nodes || [];
    for (var i = 0; i < display.length; i++) dispById[display[i][0].id] = display[i][1];
    for (i = 0; i < nodes.length; i++) nodeById[nodes[i].id] = nodes[i];
    var slotOf = {};
    var wl = walkLinks(pgraph);
    for (i = 0; i < wl.length; i++) {
      slotOf[wl[i][0].id + "\u0000" + String(wl[i][1].param)] = i;
    }
    var children = childrenMap(pgraph);
    var tails = [];
    var chainTexts = [];

    function render(node) {
      var disp = dispById[node.id] !== undefined ? dispById[node.id] : (node.params || {});
      var params = clauseParams(node, disp, game);
      var links = node.links || [];
      for (var li = 0; li < links.length; li++) {
        var link = links[li];
        var param = String(link.param !== undefined ? link.param : "");
        if (!(param in params)) continue;
        var ft = linkFormula(node, link, param, game);
        var formula = ft[0], tail = ft[1];
        var final = String(params[param]);
        var slot = slotOf[node.id + "\u0000" + param];
        if (live) {
          params[param] = LIVE_MARKER + slot + (simple ? "" : formula);
        } else if (simple) {
          params[param] = final;
        } else {
          params[param] = final + formula;
        }
        if (node.type === "hp_mod" && param === "ratio") params.value = params[param];
        if (node.kind === "condition" && node.type === "compare" && param === "value") {
          params.right = String(params[param]);
        }
        if (node.kind === "struct" && node.type === "loop" && param === "decay") {
          var tmpl = String(stats["cmp_mode_" + String((node.params || {}).mode !== undefined ? node.params.mode : "chain")] !== undefined
            ? stats["cmp_mode_" + String((node.params || {}).mode !== undefined ? node.params.mode : "chain")] : "");
          params.mode_word = tmpl.split("{decay}").join(String(params[param]));
        }
        if (tail) tails.push(tail);
      }
      var key = nodeClause(node, disp, game);
      var template = stats[key];
      if (template === undefined || template === null) {
        if (node.kind === "op" && node.type === "apply_status") {
          var sid = String(disp.status !== undefined ? disp.status : "");
          var sdef = game.statuses[sid] || {};
          return String(sdef.name !== undefined ? sdef.name : sid);
        }
        return "";
      }
      return renderTemplate(template, params, game);
    }

    function chainText(nid) {
      var node = nodeById[nid];
      var head = render(node);
      var parts = head ? [head] : [];
      var kids = children[nid] || [];
      if (node.kind === "condition") {
        for (var i = 0; i < kids.length; i++) {
          var sub = chainText(kids[i][1]);
          if (!sub) continue;
          parts.push(kids[i][0] === "fail" ? "否则" + sub : sub);
        }
      } else {
        for (i = 0; i < kids.length; i++) {
          var sub2 = chainText(kids[i][1]);
          if (sub2) parts.push(sub2);
        }
      }
      return parts.join("，");
    }

    function subtreeOps(nid) {
      var out = [], stack = [nid];
      while (stack.length) {
        var cur = stack.pop();
        var node = nodeById[cur];
        if (!node) continue;
        out.push(node);
        var kids = children[cur] || [];
        for (var i = 0; i < kids.length; i++) stack.push(kids[i][1]);
      }
      return out;
    }

    for (i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (node.kind !== "trigger") continue;
      var key2 = "hook_" + node.type;
      if (node.type === "on_attack") {
        var replacing = false;
        var kids = children[node.id] || [];
        for (var c = 0; c < kids.length && !replacing; c++) {
          var ops = subtreeOps(kids[c][1]);
          for (var o = 0; o < ops.length; o++) {
            if (ops[o].kind === "op" && ops[o].type === "strike" &&
                String(((ops[o].params || {}).mode)) === "replace") {
              replacing = true;
              break;
            }
          }
        }
        if (replacing) key2 = "hook_on_attack_replace";
      }
      var hookWord = String(stats[key2] !== undefined ? stats[key2] : "");
      var parts = hookWord ? [hookWord] : [];
      kids = children[node.id] || [];
      for (c = 0; c < kids.length; c++) {
        var sub3 = chainText(kids[c][1]);
        if (sub3) parts.push(sub3);
      }
      chainTexts.push(parts.join("，"));
    }

    var text = chainTexts.filter(function (t) { return t; }).join("；");
    for (i = 0; i < tails.length; i++) text += tails[i];
    if (text && !text.endsWith("。")) text += "。";
    return text;
  }

  function linkCalc(pgraph, game) {
    var out = [];
    var wl = walkLinks(pgraph);
    for (var i = 0; i < wl.length; i++) {
      var node = wl[i][0], link = wl[i][1];
      var param = String(link.param !== undefined ? link.param : "");
      if (!(param in (node.params || {}))) continue;
      var varId = String(link.variable);
      var base = Math.max(1.0, Number(game.attr(varId).base));
      var mode = String(link.mode !== undefined ? link.mode : "own");
      var against = varId;
      if (mode === "difference" || mode === "sum") {
        var vdef = null;
        for (var j = 0; j < game.skillVariableLink.variables.length; j++) {
          if (game.skillVariableLink.variables[j].id === varId) { vdef = game.skillVariableLink.variables[j]; break; }
        }
        against = vdef ? vdef.diffAgainst : varId;
      }
      var spec = paramSpec(node, param, game);
      var value = Number(link.base !== undefined ? link.base
        : ((node.params || {})[param] !== undefined ? node.params[param] : 0.0));
      out.push({
        field: param,
        fmt: spec[0],
        base: value,
        coeff: value * Number(link.rate !== undefined ? link.rate : 0.0) / base,
        mode: mode,
        variable: varId,
        against: against,
        clamp: [spec[1], spec[2]]
      });
    }
    return out;
  }

  var MOD_TEMPLATES = { chance: "mod_chance", value: "mod_value",
                        damage: "mod_damage", turns: "mod_turns", ticks: "mod_ticks" };

  function modTexts(pgraph, game) {
    var texts = [];
    var kinds = [["prefix", "prefix", "prefix_scale"], ["suffix", "suffix", "suffix_scale"]];
    for (var i = 0; i < kinds.length; i++) {
      var kind = kinds[i][0], key = kinds[i][1], scaleKey = kinds[i][2];
      var modId = pgraph[key];
      if (!modId) continue;
      var mdef = game.nameModifier(kind, modId);
      if (!mdef) continue;
      var scale = Number(pgraph[scaleKey] !== undefined ? pgraph[scaleKey] : 1.0);
      var parts = [];
      for (var param in mdef.mod) {
        if (!Object.prototype.hasOwnProperty.call(mdef.mod, param)) continue;
        var templateKey = MOD_TEMPLATES[param];
        if (!templateKey) continue;
        var scaled = Number(mdef.mod[param]) * scale;
        var magnitude;
        if (param === "chance") {
          magnitude = formatPct(Math.abs(scaled));
        } else if (param === "value" && !graphParamUnit(pgraph, "value", game)) {
          magnitude = formatPct(Math.abs(scaled));
        } else {
          magnitude = formatNum(Math.abs(scaled));
        }
        var sign = scaled > 0 ? "+" : "-";
        parts.push(renderTemplate(
          game.stats[templateKey] !== undefined ? game.stats[templateKey] : templateKey,
          { v: sign + magnitude }, game));
      }
      if (parts.length) texts.push(mdef.name + "：" + parts.join("，"));
    }
    return texts;
  }

  function masteryText(pgraph, sdef, game) {
    var mastery = pgraph.mastery;
    if (mastery === undefined) return "";
    if (sdef.masteryOn.indexOf("immune") >= 0) {
      var rate = Math.min(0.5, Math.max(0.01, Number(graphParamValue(pgraph, "immune", 0.0))));
      return renderTemplate(game.stats.mastery_text_immune !== undefined ? game.stats.mastery_text_immune : "",
                            { v: pyInt(mastery), rate: formatPct(rate) }, game);
    }
    if (sdef.masteryOn.indexOf("chance") >= 0) {
      var raw = graphParamValue(pgraph, "chance", 0.0);
      if (typeof raw === "string") {
        return renderTemplate(game.stats.mastery_text_value !== undefined ? game.stats.mastery_text_value : "",
                              { v: pyInt(mastery),
                                mult: pyFix(Number(pgraph.mastery_mult !== undefined ? pgraph.mastery_mult : 1.0), 2) },
                              game);
      }
      var rate2 = Math.min(0.95, Math.max(0.02, Number(raw)));
      return renderTemplate(game.stats.mastery_text !== undefined ? game.stats.mastery_text : "",
                            { v: pyInt(mastery), rate: formatPct(rate2) }, game);
    }
    return renderTemplate(game.stats.mastery_text_value !== undefined ? game.stats.mastery_text_value : "",
                          { v: pyInt(mastery),
                            mult: pyFix(Number(pgraph.mastery_mult !== undefined ? pgraph.mastery_mult : 1.0), 2) },
                          game);
  }

  function findStructure(fighter, game) {
    for (var i = 0; i < game.titleStructures.length; i++) {
      if (game.titleStructures[i].id === fighter.titleStructureId) return game.titleStructures[i];
    }
    return null;
  }

  function formatBonus(value, attrFormat) {
    var sign = value > 0 ? "+" : "";
    if (attrFormat === "percent") return sign + formatNum(value) + "%";
    return sign + formatNum(value);
  }

  function titleBonusApi(fighter, game) {
    var structure = findStructure(fighter, game);
    if (!structure) return { bonuses: [], bonuses_text: "" };
    var sums = {};
    var items = titleBonusItems(fighter.titleFields, structure, game);
    for (var i = 0; i < items.length; i++) {
      sums[items[i][0]] = (sums[items[i][0]] !== undefined ? sums[items[i][0]] : 0) + items[i][1];
    }
    var bonuses = [];
    var parts = [];
    for (i = 0; i < game.attributes.length; i++) {
      var a = game.attributes[i];
      if (!(a.id in sums) || sums[a.id] === 0) continue;
      var text = formatBonus(sums[a.id], a.format);
      bonuses.push({ attr: a.id, name: a.name, value: sums[a.id],
                     display: text, format: a.format });
      parts.push(a.name + " " + text);
    }
    return { bonuses: bonuses, bonuses_text: parts.join(" · ") };
  }

  function composeTitleName(fighter, game) {
    var structure = findStructure(fighter, game);
    if (!structure) return fighter.titleStructureId;
    var parts = [];
    for (var i = 0; i < structure.fields.length; i++) {
      var fname = structure.fields[i];
      var fid = fighter.titleFields[fname];
      var fdef = game.titleField(TITLE_FIELD_POOLS[fname], fid);
      parts.push(fdef ? fdef.name : String(fid || ""));
    }
    if (!parts.length) return "";
    var out = parts[0];
    for (i = 1; i < parts.length; i++) {
      var connector = i - 1 < structure.connectors.length ? structure.connectors[i - 1] : "";
      out += connector + parts[i];
    }
    return out;
  }

  function composeTitleDesc(fighter, game) {
    var structure = findStructure(fighter, game);
    if (!structure) return "";
    var frags = [];
    for (var i = 0; i < structure.fields.length; i++) {
      var fname = structure.fields[i];
      var fid = fighter.titleFields[fname];
      var fdef = game.titleField(TITLE_FIELD_POOLS[fname], fid);
      if (fdef && fdef.desc) frags.push(fdef.desc);
    }
    return frags.length ? frags.join("，") + "。" : "";
  }

  function fighterToApi(fighter, game) {
    var attrsApi = [];
    for (var i = 0; i < game.attributes.length; i++) {
      var a = game.attributes[i];
      var raw = fighter.attrs[a.id];
      attrsApi.push({
        id: a.id,
        name: a.name,
        emoji: a.emoji,
        value: pyRoundN(Number(raw), 4),
        min: pyRoundN(a.min, 4),
        max: pyRoundN(a.max, 4),
        format: a.format
      });
    }
    var skillsApi = [];
    var pers = personalizedEffects(fighter, game);
    for (var s = 0; s < pers.length; s++) {
      var sdef = pers[s][0], pgraph = pers[s][1];
      var sep = String(game.stats.link_sep !== undefined ? game.stats.link_sep : "·");
      var name = sdef.name;
      if (pgraph.prefix) {
        var pdef = game.nameModifier("prefix", pgraph.prefix);
        if (pdef) name = pdef.name + sep + name;
      }
      if (pgraph.suffix) {
        var smod = game.nameModifier("suffix", pgraph.suffix);
        if (smod) name = name + sep + smod.name;
      }
      var links = walkLinks(pgraph).map(function (pair) { return pair[1]; });
      for (var li = 0; li < links.length; li++) {
        var marker = game.stats["link_" + String(links[li].variable)];
        if (marker) name = name + sep + String(marker);
      }
      var linkApi = [];
      for (li = 0; li < links.length; li++) {
        var vdef = null;
        for (var ai = 0; ai < game.attributes.length; ai++) {
          if (game.attributes[ai].id === links[li].variable) { vdef = game.attributes[ai]; break; }
        }
        linkApi.push({
          field: String(links[li].param !== undefined ? links[li].param : ""),
          variable: links[li].variable,
          name: vdef ? vdef.name : String(links[li].variable),
          mode: links[li].mode !== undefined ? links[li].mode : "own",
          rate: links[li].rate !== undefined ? links[li].rate : 0
        });
      }
      var entry = {
        id: sdef.id,
        name: name,
        flavor: sdef.description,
        text: naturalText(pgraph, fighter, game),
        text_simple: naturalText(pgraph, fighter, game, true),
        modifiers: modTexts(pgraph, game),
        mastery: pyInt(pgraph.mastery !== undefined ? pgraph.mastery : 0),
        mastery_text: masteryText(pgraph, sdef, game),
        link: linkApi.length ? linkApi : null
      };
      if (links.length) {
        entry.live_text = naturalText(pgraph, fighter, game, false, true);
        entry.live_text_simple = naturalText(pgraph, fighter, game, true, true);
        entry.link_calc = linkCalc(pgraph, game);
      }
      skillsApi.push(entry);
    }
    var titleBonus = titleBonusApi(fighter, game);
    return {
      name: fighter.name,
      normalized: fighter.normalized,
      digest: fighter.digest,
      digest_short: fighter.digest.slice(0, 8),
      title: {
        structure: fighter.titleStructureId,
        name: composeTitleName(fighter, game),
        description: composeTitleDesc(fighter, game),
        bonuses: titleBonus.bonuses,
        bonuses_text: titleBonus.bonuses_text
      },
      attributes: attrsApi,
      skills: skillsApi,
      power: fighter.power
    };
  }

  NFE.InvalidName = InvalidName;
  NFE.LIVE_MARKER = LIVE_MARKER;
  NFE.normalizeName = normalizeName;
  NFE.deriveFighter = deriveFighter;
  NFE.personalizedEffects = personalizedEffects;
  NFE.resonanceCoeff = resonanceCoeff;
  NFE.applyResonance = applyResonance;
  NFE.formatField = formatField;
  NFE.formatResonanceFinal = formatResonanceFinal;
  NFE.estimatedResonancedEff = estimatedResonancedEff;
  NFE.titleBonusItems = titleBonusItems;
  NFE.naturalText = naturalText;
  NFE.linkCalc = linkCalc;
  NFE.composeTitleName = composeTitleName;
  NFE.composeTitleDesc = composeTitleDesc;
  NFE.fighterToApi = fighterToApi;
})(typeof window !== "undefined" ? window : globalThis);
