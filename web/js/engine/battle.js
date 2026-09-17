/* 确定性对战引擎 —— namefight/battle.py 的移植（tick 调度器 + 13 原子实现
   + 图执行器 + 战报渲染）。随机数消耗顺序、结算顺序、取整口径与 Python
   完全一致（确定性契约，改变即 breaking）。 */
(function (root) {
  "use strict";
  var NFE = root.NFE = root.NFE || {};
  var statuses = NFE;
  var pyRound = NFE.pyRound, pyInt = NFE.pyInt, pyRoundN = NFE.pyRoundN;
  var pyCmp = NFE.pyCmp, pySorted = NFE.pySorted, pyStr = NFE.pyStr;
  var formatPct = NFE.formatPct, formatNum = NFE.formatNum;
  var renderTemplate = NFE.renderTemplate;
  var isExpr = NFE.isExpr;
  var PLACEHOLDER = /\{(\w+)\}/g;

  var PROC_HOOKS = ["on_attack", "on_defend", "action_start", "action_interrupt"];
  var RICH_NAME_KEYS = ["a", "b", "winner"];
  var RICH_DAMAGE_KEYS = ["damage", "cost"];
  var RICH_HEAL_KEYS = ["heal"];

  function _r(x) {
    return pyRound(Number(x));
  }

  /* ---- 被动修饰聚合 ---- */

  function effAtk(c, game, tick) {
    return (c.atk * (1.0 + statuses.sumMod(c, game, tick || 0, "atk_pct"))
            + statuses.sumMod(c, game, tick || 0, "atk_flat"));
  }

  function effDef(c, game, tick) {
    return Math.max(0.0, c.defense - statuses.sumMod(c, game, tick || 0, "def_break"));
  }

  function effSpd(c, game, tick) {
    return (c.spd * (1.0 + statuses.sumMod(c, game, tick || 0, "spd_pct"))
            + statuses.sumMod(c, game, tick || 0, "spd_flat"));
  }

  function liveValue(c, vid, game, tick) {
    if (vid === "hp") return Math.max(0, c.hp);
    if (vid === "atk") return effAtk(c, game, tick);
    if (vid === "def") return effDef(c, game, tick);
    if (vid === "spd") return effSpd(c, game, tick);
    if (vid === "crit") return c.crit;
    if (vid === "dodge") return c.dodge;
    return 0.0;
  }

  /* ---- 执行上下文 ---- */

  function Ctx(game, rng, ev, tick, owner, opponent, combatants) {
    this.game = game;
    this.rng = rng;
    this.ev = ev;
    this.tick = tick;
    this.owner = owner;
    this.opponent = opponent;
    this.combatants = combatants;
    this.ac = null;
    this.dc = null;
    this.dmg = 0.0;
    this.skill = null;
    this.node = null;
    this.proc_logged = false;
    this.executed = 0;
    this.defer = null;
    this.hook_name = "";
    this.crit_hit = false;
    this.loop_i = 0;
    this.status = null;
  }

  function nodeSpecs(ctx, node) {
    if (node.kind === "op" && node.type === "apply_status") {
      var sid = (node.params || {}).status;
      return NFE.paramSpecs("op", "apply_status", function () {
        return ctx.game.statusSpecs[sid];
      });
    }
    return NFE.paramSpecs(node.kind, node.type);
  }

  function resSpec(ctx, node, param) {
    var ps = nodeSpecs(ctx, node)[param];
    if (ps !== undefined && (ps.fmt || ps.clamp)) {
      var lo = ps.clamp ? ps.clamp[0] : null;
      var hi = ps.clamp ? ps.clamp[1] : null;
      return [ps.fmt, lo, hi];
    }
    return NFE.DEFAULT_RESONANCE_SPEC.slice();
  }

  function exprEnv(ctx) {
    var game = ctx.game, tick = ctx.tick;
    var env = {};

    function put(prefix, c) {
      env[prefix + "hp"] = Math.max(0.0, c.hp);
      env[prefix + "max_hp"] = c.max_hp;
      env[prefix + "hp_pct"] = Math.max(0.0, c.hp) / c.max_hp;
      env[prefix + "atk"] = effAtk(c, game, tick);
      env[prefix + "def"] = effDef(c, game, tick);
      env[prefix + "spd"] = effSpd(c, game, tick);
      env[prefix + "crit"] = c.crit / 100.0;
      env[prefix + "dodge"] = c.dodge / 100.0;
      env[prefix + "gauge"] = c.gauge;
      env[prefix + "gauge_pct"] = c.gauge / game.battle.gaugeThreshold;
      env[prefix + "damage_dealt"] = c.damage_dealt;
      c.marks.forEach(function (m, key) {           // 标记层数
        env[prefix + "mark:" + key] = Number(m.n !== undefined ? m.n : 0);
      });
      c.st.forEach(function (st, sid) {             // 状态派生量
        var sdef = game.statuses[sid] || {};
        env[prefix + "stacks:" + sid] = statuses.liveStacks(c, sid, tick, sdef);
        env[prefix + "total:" + sid] = Number(st.total !== undefined ? st.total : 0.0);
        var recs = st.records || [];
        var rsum = 0.0;
        for (var i = 0; i < recs.length; i++) rsum += recs[i];
        env[prefix + "records:" + sid] = rsum;
      });
    }

    put("self.", ctx.owner);
    put("enemy.", ctx.opponent);
    env["ctx.dmg"] = ctx.dmg;
    env["ctx.absorbed"] = ctx.dc ? ctx.dc.absorbed : 0.0;
    env["ctx.loop"] = ctx.loop_i;
    env["ctx.tick"] = tick;
    if (ctx.status !== null && ctx.status !== undefined) {
      var stParams = ctx.status[1].params;
      for (var key in stParams) {
        if (!Object.prototype.hasOwnProperty.call(stParams, key)) continue;
        var value = stParams[key];
        if (typeof value === "number") env[key] = value;
      }
    }
    return env;
  }

  function clampRes(value, spec) {
    var fmt = spec[0], lo = spec[1], hi = spec[2];
    if (fmt === "turns") {
      var v = Math.max(1, pyInt(pyRound(value)));
      return (hi !== null && hi !== undefined) ? Math.min(pyInt(hi), v) : v;
    }
    if (lo !== null && lo !== undefined && value < lo) value = lo;
    if (hi !== null && hi !== undefined && value > hi) value = hi;
    return value;
  }

  function procParams(node, ctx) {
    var params = node.params || {};
    var proc = {};
    for (var k in params) {
      if (Object.prototype.hasOwnProperty.call(params, k)) proc[k] = params[k];
    }
    var hasExpr = false;
    for (k in proc) {
      if (isExpr(proc[k])) { hasExpr = true; break; }
    }
    if (hasExpr) {
      var linkParams = {};
      var links = node.links || [];
      for (var i = 0; i < links.length; i++) {
        linkParams[String(links[i].param)] = true;
      }
      var env = exprEnv(ctx);
      for (k in proc) {
        if (!Object.prototype.hasOwnProperty.call(proc, k)) continue;
        if (isExpr(proc[k])) {
          var result = NFE.evalExpr(proc[k], env);
          if (linkParams[k]) result = clampRes(result, resSpec(ctx, node, k));
          proc[k] = result;
        }
      }
    }
    return proc;
  }

  function emitLinkEvents(ctx, node, proc) {
    var links = node.links || [];
    for (var i = 0; i < links.length; i++) {
      var link = links[i];
      var param = String(link.param !== undefined ? link.param : "");
      if (!(param in proc)) continue;
      var mode = String(link.mode !== undefined ? link.mode : "own");
      ctx.ev("effect_link", {
        a: ctx.owner.name,
        stat: { ref: "attr", id: link.variable },
        scope: { ref: "stat_word", id: "scope_" + mode },
        field: { ref: "stat_word", id: "field_" + param },
        final: NFE.formatResonanceFinal(Number(proc[param]), resSpec(ctx, node, param)[0], null)
      });
    }
  }

  function cmpSource(ctx, source) {
    var parts = String(source).split(".");
    var who = parts[0], what = parts.slice(1).join(".");
    var c = who === "self" ? ctx.owner : ctx.opponent;
    if (what === "hp_pct") return Math.max(0.0, c.hp) / c.max_hp;
    if (what === "gauge_pct") return c.gauge / ctx.game.battle.gaugeThreshold;
    if (what === "crit") return c.crit / 100.0;
    if (what === "dodge") return c.dodge / 100.0;
    return liveValue(c, what, ctx.game, ctx.tick);
  }

  function cmpOp(op, a, b) {
    if (op === "lt") return a < b;
    if (op === "le") return a <= b;
    if (op === "gt") return a > b;
    return a >= b;
  }

  function condPass(node, ctx, proc) {
    var t = node.type;
    var owner = ctx.owner, opp = ctx.opponent;
    var game = ctx.game, tick = ctx.tick;
    if (t === "chance") {
      return !(ctx.rng.nextFloat() > Number(proc.chance !== undefined ? proc.chance : 1.0));
    }
    if (t === "compare") {
      var left = cmpSource(ctx, proc.left !== undefined ? proc.left : "self.hp_pct");
      var right = String(proc.right) === "const"
        ? Number(proc.value !== undefined ? proc.value : 0.0)
        : cmpSource(ctx, proc.right !== undefined ? proc.right : "enemy.hp_pct");
      return cmpOp(String(proc.op !== undefined ? proc.op : "ge"), left, right);
    }
    if (t === "stacks_cmp") {
      var target = String(proc.target !== undefined ? proc.target : "self") === "self" ? owner : opp;
      var sid = String(proc.status);
      var n = statuses.liveStacks(target, sid, tick, game.statuses[sid] || {});
      return cmpOp(String(proc.op !== undefined ? proc.op : "ge"), n,
                   Number(proc.value !== undefined ? proc.value : 0.0));
    }
    if (t === "has_status") {
      var sid2 = String(proc.status);
      if (ctx.status !== null && ctx.status !== undefined &&
          (ctx.hook_name === "on_status_gain" || ctx.hook_name === "on_status_lose")) {
        return sid2 === ctx.status[0];
      }
      return statuses.live(owner, sid2, tick, game.statuses[sid2] || {});
    }
    if (t === "no_status") {
      var sid3 = String(proc.status);
      if (ctx.status !== null && ctx.status !== undefined &&
          (ctx.hook_name === "on_status_gain" || ctx.hook_name === "on_status_lose")) {
        return sid3 !== ctx.status[0];
      }
      return !statuses.live(owner, sid3, tick, game.statuses[sid3] || {});
    }
    if (t === "has_marker") {
      var key = String(proc.key);
      var mark = owner.marks.get(key);
      if ("op" in proc || "count" in proc) {
        var n2 = Number((mark || {}).n !== undefined ? mark.n : 0);
        return cmpOp(String(proc.op !== undefined ? proc.op : "ge"), n2,
                     Number(proc.count !== undefined ? proc.count : 1));
      }
      return ((mark || {}).n !== undefined ? mark.n : 0) > 0;
    }
    if (t === "no_marker") {
      var m2 = owner.marks.get(String(proc.key));
      return ((m2 || {}).n !== undefined ? m2.n : 0) <= 0;
    }
    if (t === "once_per_battle") {
      var marker = "once:" + String(proc.key);
      if (owner.markers.has(marker)) return false;
      owner.markers.add(marker);
      return true;
    }
    if (t === "last_crit") {
      return !!ctx.crit_hit;
    }
    return true;
  }

  /* ---- 链执行器：条件分支 + 循环 + 原子分发 ---- */

  function runTree(tree, ctx) {
    var node = tree[0], children = tree[1];
    ctx.node = node;
    var i, sig;
    if (node.kind === "trigger") {
      for (i = 0; i < children.length; i++) {
        sig = runTree(children[i][1], ctx);
        if (sig) return sig;
      }
      return null;
    }
    if (node.kind === "condition") {
      var proc = procParams(node, ctx);
      var want = condPass(node, ctx, proc) ? "pass" : "fail";
      var onceMarker = null;
      if (node.type === "once_per_battle" && want === "pass") {
        onceMarker = "once:" + String(proc.key);
      }
      var executedBefore = ctx.executed;
      for (i = 0; i < children.length; i++) {
        if (children[i][0] === want) {
          sig = runTree(children[i][1], ctx);
          if (sig) return sig;
        }
      }
      if (onceMarker !== null && ctx.executed === executedBefore) {
        ctx.owner.markers.delete(onceMarker);
      }
      return null;
    }
    if (node.kind === "struct") {                      // 循环结构（loop）
      var sproc = procParams(node, ctx);
      var maxRounds = Math.max(1, pyInt(sproc.max !== undefined ? sproc.max : 1));
      var mode = String(sproc.mode !== undefined ? sproc.mode : "chain");
      var decay = Number(sproc.decay !== undefined ? sproc.decay : 0.9);
      for (var round = 1; round <= maxRounds; round++) {
        if (mode === "chain" && round > 1 &&
            ctx.rng.nextFloat() >= Math.pow(decay, round - 1)) {
          break;
        }
        ctx.loop_i = round;
        for (i = 0; i < children.length; i++) {
          sig = runTree(children[i][1], ctx);
          if (sig) return sig;
        }
        if (ctx.owner.hp <= 0 || ctx.opponent.hp <= 0) break;
      }
      ctx.loop_i = 0;
      return null;
    }
    // 原子节点
    var oproc = procParams(node, ctx);
    ctx.executed += 1;
    announce(ctx, node, oproc);
    var osig = OP_IMPL[node.type](ctx, oproc);
    if (osig) return osig;
    for (i = 0; i < children.length; i++) {
      sig = runTree(children[i][1], ctx);
      if (sig) return sig;
    }
    return null;
  }

  function announce(ctx, node, proc) {
    var hook = ctx.hook_name;
    var logged = NFE.OPS[node.type].logged;
    if (node.type === "apply_status") {
      var sdef = ctx.game.statuses[String(proc.status)] || {};
      logged = sdef.logged !== undefined ? !!sdef.logged : logged;
    }
    if (logged && PROC_HOOKS.indexOf(hook) >= 0 && !ctx.proc_logged && ctx.skill !== null) {
      ctx.proc_logged = true;
      ctx.ev("skill_proc", {
        a: ctx.owner.name,
        skill: { ref: "skill", id: ctx.skill.id }
      });
    }
    emitLinkEvents(ctx, node, proc);
  }

  function runHook(skills, hook, ctx) {
    ctx.hook_name = hook;
    for (var i = 0; i < skills.length; i++) {
      var trees = skills[i][2][hook];
      if (!trees) continue;
      ctx.skill = skills[i][0];
      ctx.proc_logged = false;
      for (var t = 0; t < trees.length; t++) {
        var sig = runTree(trees[t], ctx);
        if (sig) return sig;
      }
    }
    return null;
  }

  function fireStatusEvent(c, opponent, combatants, sid, gained, game, rng, ev, tick) {
    var hook = gained ? "on_status_gain" : "on_status_lose";
    var ctx = new Ctx(game, rng, ev, tick, c, opponent, combatants);
    ctx.hook_name = hook;
    var st = c.st.get(sid);
    ctx.status = [sid, st ? st : statuses.newRuntime()];
    runHook(c.skills, hook, ctx);
    if (gained) c.live_prev.add(sid);
    else c.live_prev.delete(sid);
  }

  function runStatusHooks(c, opponent, combatants, hook, game, rng, ev, tick, only) {
    var ctx = new Ctx(game, rng, ev, tick, c, opponent, combatants);
    ctx.hook_name = hook;
    var liveList = statuses.eachLive(c, game, tick);
    for (var i = 0; i < liveList.length; i++) {
      var sid = liveList[i][0], st = liveList[i][1];
      if (only !== undefined && only !== null && sid !== only[0]) continue;
      var plan = (game.statusPlans[sid] || {})[hook];
      if (!plan) continue;
      ctx.status = [sid, st];
      for (var t = 0; t < plan.length; t++) {
        var sig = runTree(plan[t], ctx);
        if (sig) return sig;
      }
    }
    return null;
  }

  /* ---- 原子实现（与 effects.OPS 一一对应） ---- */

  function strikeTarget(ctx, proc) {
    return String(proc.target !== undefined ? proc.target : "enemy") === "enemy"
      ? ctx.opponent : ctx.owner;
  }

  function opStrike(ctx, proc) {
    var game = ctx.game, rng = ctx.rng, ev = ctx.ev;
    var bc = game.battle;
    var owner = ctx.owner;
    var target = strikeTarget(ctx, proc);
    var basis = String(proc.basis !== undefined ? proc.basis : "none");
    var mode = String(proc.mode !== undefined ? proc.mode : "extra");
    var value = Number(proc.value !== undefined ? proc.value : 1.0);
    var event = proc.event;

    if (basis !== "none") {
      // 附加伤害：recorded_sum 记录总和（记仇）/ taken_absorbed 本次被减免量（反甲）
      var amount;
      if (basis === "recorded_sum") {
        var total = 0.0;
        var liveList = statuses.eachLive(owner, game, ctx.tick);
        for (var i = 0; i < liveList.length; i++) {
          var st = liveList[i][1];
          if (st.records.length) {
            for (var r = 0; r < st.records.length; r++) total += st.records[r];
            st.records = [];
          }
        }
        amount = _r(total * value);
        event = event || "retribution_release";
      } else {
        var absorbed = ctx.dc ? ctx.dc.absorbed : 0.0;
        amount = _r(absorbed * value);
        event = event || "effect_reflect";
      }
      if (amount <= 0 || target.hp <= 0) return null;
      hurt(target, amount, ev, rng, game, ctx.tick);
      owner.damage_dealt += amount;
      ev(String(event), {
        a: owner.name, b: target.name,
        damage: formatNum(amount), value: formatNum(amount),
        ratio: formatPct(value), hit: ctx.loop_i
      });
      return null;
    }

    var mult = Number(proc.mult !== undefined ? proc.mult : 1.0);
    var real = !!proc.real;
    var mustHit = !!proc.must_hit;
    var pen = Number(proc.pen !== undefined ? proc.pen : 0.0);
    var critBonus = Number(proc.crit_bonus !== undefined ? proc.crit_bonus : 0.0);
    if (target.hp <= 0 || owner.hp <= 0) return null;
    if (!real && !mustHit) {
      if (rng.nextFloat() < target.dodge / 100.0) {
        ev("attack_miss", { a: owner.name, b: target.name });
        return null;
      }
    }
    var crit = false;
    if (!real) {
      crit = rng.nextFloat() < Math.min(bc.critCap / 100.0, owner.crit / 100.0 + critBonus);
      if (crit) ev("attack_crit", {});
    }
    var dmg;
    if (real) {
      var variance = rng.nextTriangular(bc.varianceLo, bc.varianceHi);
      dmg = Math.max(bc.minDamage, effAtk(owner, game, ctx.tick) * bc.atkFactor * mult * variance);
    } else {
      dmg = computeDamage(owner, target, mult, crit, game, rng, pen, ctx.tick);
    }
    dmg = defend(target, owner, dmg, game, rng, ev, ctx.tick);
    dmg = _r(dmg);
    if (dmg > 0) {
      hurt(target, dmg, ev, rng, game, ctx.tick);
      owner.damage_dealt += dmg;
      ev(String(event || "attack_hit"), {
        a: owner.name, b: target.name, damage: formatNum(dmg),
        mult: formatPct(mult), crit: formatPct(critBonus), hit: ctx.loop_i
      });
      applyLifesteal(owner, dmg, ev, game, ctx.tick);
      var lifesteal = Number(proc.lifesteal !== undefined ? proc.lifesteal : 0.0);
      if (lifesteal > 0 && owner.hp > 0) {
        var gained = _r(Math.min(dmg * lifesteal, owner.max_hp - owner.hp));
        if (gained > 0) {
          owner.hp += gained;
          owner.steal_rec += gained;
          ev("effect_lifesteal", { a: owner.name, heal: formatNum(gained) });
        }
      }
      hitReactions(owner, target, dmg, crit, game, rng, ev, ctx.tick);
    }
    if (mode === "replace" && ctx.ac !== null && ctx.ac !== undefined) {
      ctx.ac.replaced = true;
    }
    return null;
  }

  function opHitMod(ctx, proc) {
    var ac = ctx.ac;
    var mult = Number(proc.mult !== undefined ? proc.mult : 1.0);
    if (mult !== 1.0) ac.mult *= mult;
    ac.pen = Math.max(ac.pen, Number(proc.pen !== undefined ? proc.pen : 0.0));
    ac.crit_flat += Number(proc.crit_bonus !== undefined ? proc.crit_bonus : 0.0);
    if (proc.must_hit) ac.must_hit = true;
    var event = proc.event;
    if (event) {
      ctx.ev(String(event), { mult: formatPct(mult) });
    } else if (proc.announce) {
      ctx.ev("effect_execution", { mult: formatPct(ac.mult) });
    }
    return null;
  }

  function opTakenMod(ctx, proc) {
    var bc = ctx.game.battle;
    var ratio = Math.min(Number(proc.cut !== undefined ? proc.cut : 0.0), bc.reflectSplitCap);
    if (ctx.dc.dmg > 0 && ratio > 0) {
      var avoided = ctx.dc.dmg * ratio;
      ctx.dc.absorbed += avoided;
      ctx.dc.dmg = Math.max(bc.minDamage, ctx.dc.dmg - avoided);
      var event = proc.event;
      if (event) {
        ctx.ev(String(event), { b: ctx.owner.name, ratio: formatPct(ratio) });
      }
    }
    return null;
  }

  function opGrantImmune(ctx, proc) {
    if (ctx.dc.dmg > 0) {
      ctx.dc.dmg = 0.0;
      ctx.dc.absorbed = 0.0;
      ctx.ev(String(proc.event || "immune"), { b: ctx.owner.name });
      return "immune";
    }
    return null;
  }

  function opStatMod(ctx, proc) {
    var owner = String(proc.target !== undefined ? proc.target : "self") === "self"
      ? ctx.owner : ctx.opponent;
    var gain = Number(proc.gain !== undefined ? proc.gain : 0.0);
    if (String(proc.basis !== undefined ? proc.basis : "flat") === "recorded_lifesteal") {
      gain = Number(proc.value !== undefined ? proc.value : 0.0) * owner.steal_rec;
    }
    gain = _r(gain);
    if (gain === 0) return null;
    var stat = String(proc.stat !== undefined ? proc.stat : "atk");
    if (stat === "hp") {
      owner.max_hp += gain;
      owner.hp = Math.max(1.0, owner.hp + gain);
    } else if (stat === "def") {
      owner.defense = Math.max(0.0, owner.defense + gain);
    } else {
      owner[stat] = Math.max(0.0, owner[stat] + gain);
    }
    var sid = proc.status;
    if (sid) {
      var st = statuses.ensure(owner, String(sid));
      st.total += gain;
      if (st.params.value === undefined) st.params.value = 0.0;
    }
    var event = proc.event;
    if (event) {
      ctx.ev(String(event), {
        a: owner.name, value: formatNum(gain),
        atk: formatNum(effAtk(owner, ctx.game, ctx.tick))
      });
    }
    return null;
  }

  function opHpMod(ctx, proc) {
    var game = ctx.game;
    var target = String(proc.target !== undefined ? proc.target : "self") === "self"
      ? ctx.owner : ctx.opponent;
    var basis = String(proc.basis !== undefined ? proc.basis : "flat");
    var value = Number(proc.value !== undefined ? proc.value : 0.0);
    var ratio = Number(proc.ratio !== undefined ? proc.ratio : 0.0);
    var amount;
    if (basis === "maxhp") {
      amount = target.max_hp * ratio;
    } else if (basis === "curhp") {
      amount = target.hp * ratio;
    } else if (basis === "applier_atk") {
      var applier = ctx.status ? ctx.status[1].applier : ctx.owner;
      amount = effAtk(applier, game, ctx.tick) * game.battle.atkFactor * ratio;
    } else if (basis === "dealt") {
      amount = ctx.dmg * ratio;
    } else {
      amount = value;
    }
    if (String(proc.type !== undefined ? proc.type : "heal") === "heal") {
      var gained = _r(Math.min(amount, target.max_hp - target.hp));
      if (gained > 0) {
        target.hp += gained;
        ctx.ev(String(proc.event || "effect_heal"), {
          a: target.name, heal: formatNum(gained), value: formatNum(gained)
        });
      }
      return null;
    }
    if (proc.floor1) {
      var cost = _r(amount);
      target.hp = Math.max(1.0, target.hp - cost);
      ctx.ev(String(proc.event || "overload_cost"), {
        a: target.name, cost: formatNum(cost), value: formatNum(cost)
      });
    } else {
      var loss = _r(amount);
      target.hp -= loss;
      var event = proc.event;
      if (event) {
        ctx.ev(String(event), {
          a: target.name, damage: formatNum(loss), value: formatNum(loss)
        });
      }
    }
    return null;
  }

  function opGaugeMod(ctx, proc) {
    var target = String(proc.target !== undefined ? proc.target : "self") === "self"
      ? ctx.owner : ctx.opponent;
    target.gauge = Math.max(0.0, target.gauge + _r(Number(proc.gain !== undefined ? proc.gain : 0.0)));
    return null;
  }

  function opHpSwap(ctx, proc) {
    var a = ctx.owner, b = ctx.opponent;
    var ha = Math.max(0.0, a.hp), hb = Math.max(0.0, b.hp);
    a.hp = Math.min(a.max_hp, hb);
    b.hp = Math.min(b.max_hp, ha);
    ctx.ev(String(proc.event || "fate_swap"), {
      a: a.name, b: b.name,
      a_hp: formatNum(a.hp), b_hp: formatNum(b.hp), value: formatNum(a.hp)
    });
    return null;
  }

  function stackCap(st, sdef) {
    var v = st.params.max_stacks !== undefined ? st.params.max_stacks
      : (sdef.max_stacks !== undefined ? sdef.max_stacks : undefined);
    if (v === undefined || v === null) return -1;
    var n = pyInt(Number(v));
    if (!isFinite(n)) return -1;
    return Math.max(-1, n);
  }

  function applyStatusNow(ctx, sid, proc, target) {
    var game = ctx.game;
    var sdef = game.statuses[sid];
    if (!sdef) return;
    var st = statuses.ensure(target, sid);
    var merged = statuses.statusDefaults(sdef);
    for (var k in proc) {
      if (!Object.prototype.hasOwnProperty.call(proc, k)) continue;
      if (k === "status" || k === "target") continue;
      merged[k] = proc[k];
    }
    for (k in merged) {
      if (Object.prototype.hasOwnProperty.call(merged, k)) st.params[k] = merged[k];
    }
    st.applier = ctx.owner;
    st.links = ((ctx.node || {}).links || []).slice();
    var turns = Math.max(1, pyInt(st.params.turns !== undefined ? st.params.turns : 1));
    var stack = sdef.stack !== undefined ? sdef.stack : "refresh";
    if (stack === "layers") {
      var snapshot = {};
      for (k in merged) {
        if (Object.prototype.hasOwnProperty.call(merged, k)) snapshot[k] = merged[k];
      }
      st.layers.push([ctx.tick + turns, snapshot]);
    } else if (stack === "count") {
      var cap = stackCap(st, sdef);
      if (cap === 0) {
        target.st.delete(sid);
        return;
      }
      if (cap > 0 && st.stacks >= cap) return;
      st.stacks += 1;
      st.expires = ctx.tick + turns;
    } else {
      if ((sdef.expire !== undefined ? sdef.expire : "ticks") === "actions") {
        st.actions = turns;
      } else if ((sdef.expire !== undefined ? sdef.expire : "ticks") !== "none") {
        st.expires = ctx.tick + turns;
      }
      var interval = statuses.resolve(sdef.interval !== undefined ? sdef.interval : 0, st.params);
      if (interval) {
        st.next = ctx.tick + Math.max(1, pyInt(interval));
      }
    }
    var event = sdef.event;
    if (event) {
      var n = statuses.liveStacks(target, sid, ctx.tick, sdef);
      var params = {
        a: ctx.owner.name, b: target.name,
        turns: turns, stacks: n, hit: ctx.loop_i
      };
      var sdefParams = sdef.params || {};
      for (k in st.params) {
        if (!Object.prototype.hasOwnProperty.call(st.params, k)) continue;
        var val = st.params[k];
        if (typeof val === "number") {
          var fmt = ((sdefParams[k] || {}).fmt) || "num";
          params[k] = fmt === "pct" ? formatPct(val) : formatNum(val);
        }
      }
      var mods = sdef.mods || [];
      for (var m = 0; m < mods.length; m++) {
        if (mods[m].kind !== "dmg_out_pct") continue;
        var v = Number(statuses.resolve(mods[m].value !== undefined ? mods[m].value : 0.0, st.params));
        var per = mods[m].per_stack ? n : 1;
        params.mult = formatPct(1.0 + v * per);
      }
      ctx.ev(String(event), params);
    }
    var plan = (game.statusPlans[sid] || {}).on_status_apply;
    if (plan) {
      var sctx = new Ctx(ctx.game, ctx.rng, ctx.ev, ctx.tick,
                         target, target === ctx.owner ? ctx.opponent : ctx.owner,
                         ctx.combatants);
      sctx.hook_name = "on_status_apply";
      sctx.status = [sid, st];
      for (var t = 0; t < plan.length; t++) runTree(plan[t], sctx);
    }
    fireStatusEvent(target, target === ctx.owner ? ctx.opponent : ctx.owner,
                    ctx.combatants, sid, true, ctx.game, ctx.rng, ctx.ev, ctx.tick);
  }

  function opApplyStatus(ctx, proc) {
    var sid = String(proc.status);
    var target = String(proc.target) === "enemy" ? ctx.opponent : ctx.owner;
    if (ctx.hook_name === "on_attack" && target === ctx.opponent) {
      var snapshotProc = {};
      for (var k in proc) {
        if (Object.prototype.hasOwnProperty.call(proc, k)) snapshotProc[k] = proc[k];
      }
      ctx.defer.push(function () { applyStatusNow(ctx, sid, snapshotProc, target); });
      return null;
    }
    applyStatusNow(ctx, sid, proc, target);
    return null;
  }

  function opCleanse(ctx, proc) {
    var scope = String(proc.scope !== undefined ? proc.scope : "both");
    var targets = ctx.combatants.slice();
    if (scope === "self") {
      targets = [ctx.owner];
    } else if (scope === "enemy") {
      targets = [ctx.opponent];
    }
    var count = statuses.dispelAll(targets, ctx.tick, ctx.game, function (c, sid) {
      fireStatusEvent(c, c === ctx.owner ? ctx.opponent : ctx.owner,
                      ctx.combatants, sid, false, ctx.game, ctx.rng, ctx.ev, ctx.tick);
    });
    var healed = _r(Math.min(
      Number(proc.value !== undefined ? proc.value : 0.0) +
      Number(proc.per !== undefined ? proc.per : 0.0) * count,
      ctx.owner.max_hp - ctx.owner.hp));
    if (healed > 0) ctx.owner.hp += healed;
    ctx.ev("purify_cleanse", {
      a: ctx.owner.name, count: count, heal: formatNum(healed)
    });
    return null;
  }

  function opSkipAction(ctx, proc) {
    ctx.ev(String(proc.event || "turn_stun"), { a: ctx.owner.name });
    return "consume";
  }

  function opRecord(ctx, proc) {
    if (String(proc.what !== undefined ? proc.what : "damage_taken") === "lifesteal") {
      ctx.owner.steal_rec += ctx.dmg;
      return null;
    }
    var sid = String(proc.status);
    var st = statuses.ensure(ctx.owner, sid);
    var cap = pyInt(Number(proc.cap !== undefined && proc.cap !== null ? proc.cap : 0) || 0);
    if (cap && st.records.length >= cap) return null;
    st.records.push(Number(ctx.dmg));
    ctx.ev("retribution_record", {
      a: ctx.owner.name,
      damage: formatNum(ctx.dmg), value: formatNum(ctx.dmg),
      stacks: st.records.length
    });
    return null;
  }

  function opMarker(ctx, proc) {
    var owner = ctx.owner;
    var key = String(proc.key);
    var action = String(proc.action !== undefined ? proc.action : "set");
    var mark = owner.marks.get(key);
    var turns = proc.turns;
    if (action === "clear") {
      owner.marks.delete(key);
      return null;
    }
    if (action === "toggle") {
      if (mark && mark.n > 0) {
        owner.marks.delete(key);
        return null;
      }
      owner.marks.set(key, { n: 1, expires: turns ? ctx.tick + pyInt(turns) : 0 });
      return null;
    }
    if (action === "add" || action === "sub") {
      var delta = pyInt(Number(proc.value !== undefined ? proc.value : 1)) * (action === "add" ? 1 : -1);
      var n = ((mark || {}).n !== undefined ? mark.n : 0) + delta;
      if (n <= 0) {
        owner.marks.delete(key);
        return null;
      }
      if (!owner.marks.has(key)) owner.marks.set(key, { n: n, expires: 0 });
      mark = owner.marks.get(key);
      mark.n = n;
      if (turns) mark.expires = ctx.tick + pyInt(turns);
      return null;
    }
    owner.marks.set(key, { n: 1, expires: turns ? ctx.tick + pyInt(turns) : 0 });
    return null;
  }

  function opStatusCtl(ctx, proc) {
    var game = ctx.game;
    var target = String(proc.target !== undefined ? proc.target : "self") === "self"
      ? ctx.owner : ctx.opponent;
    var sid = String(proc.status);
    var sdef = game.statuses[sid] || {};
    var st = target.st.get(sid);
    if (!st || !statuses.live(target, sid, ctx.tick, sdef)) return null;
    var op = String(proc.op !== undefined ? proc.op : "extend");
    var value = _r(Number(proc.value !== undefined ? proc.value : 0.0));
    if (op === "extend") {
      if ((sdef.stack !== undefined ? sdef.stack : "refresh") === "layers") {
        st.layers = st.layers.map(function (e) {
          return [e[0] + Math.max(0, pyInt(value)), e[1]];
        });
      } else {
        st.expires += Math.max(0, pyInt(value));
      }
    } else if (op === "shorten") {
      if ((sdef.stack !== undefined ? sdef.stack : "refresh") === "layers") {
        st.layers = st.layers.map(function (e) {
          return [e[0] - Math.max(0, pyInt(value)), e[1]];
        });
      } else {
        st.expires -= Math.max(0, pyInt(value));
      }
    } else if (op === "stacks") {
      var cap = stackCap(st, sdef);
      st.stacks = Math.max(0, st.stacks + pyInt(value));
      if (cap >= 0) st.stacks = Math.min(cap, st.stacks);
    } else if (op === "clear") {
      st.expires = 0;
      st.layers = [];
      st.stacks = 0;
      st.actions = 0;
      st.records = [];
      fireStatusEvent(target, target === ctx.owner ? ctx.opponent : ctx.owner,
                      ctx.combatants, sid, false, game, ctx.rng, ctx.ev, ctx.tick);
    }
    var event = proc.event;
    if (event) {
      ctx.ev(String(event), {
        a: ctx.owner.name, b: target.name, value: formatNum(value)
      });
    }
    return null;
  }

  var OP_IMPL = {
    strike: opStrike,
    hit_mod: opHitMod,
    taken_mod: opTakenMod,
    grant_immune: opGrantImmune,
    stat_mod: opStatMod,
    hp_mod: opHpMod,
    gauge_mod: opGaugeMod,
    hp_swap: opHpSwap,
    apply_status: opApplyStatus,
    cleanse: opCleanse,
    skip_action: opSkipAction,
    record: opRecord,
    marker: opMarker,
    status_ctl: opStatusCtl
  };

  /* ---- 结算流程 ---- */

  function hurt(c, amount, ev, rng, game, tick) {
    if (amount > 0) {
      var absorbed = 0.0;
      var liveList = statuses.eachLive(c, game, tick);
      for (var i = 0; i < liveList.length; i++) {
        var sdef = liveList[i][2];
        var mods = sdef.mods || [];
        for (var m = 0; m < mods.length; m++) {
          if (mods[m].kind !== "shield") continue;
          var poolKey = mods[m].pool !== undefined ? mods[m].pool : "value";
          var poolRaw = c.st.get(liveList[i][0]).params[poolKey];
          var pool = Number(poolRaw !== undefined && poolRaw !== null ? poolRaw : 0.0) || 0.0;
          if (pool <= 0 || amount <= 0) continue;
          var take = Math.min(amount, pool);
          c.st.get(liveList[i][0]).params[poolKey] = pool - take;
          amount -= take;
          absorbed += take;
        }
      }
      if (absorbed > 0) {
        ev("shield_absorb", {
          a: c.name,
          damage: formatNum(_r(absorbed)), value: formatNum(_r(absorbed))
        });
      }
      if (amount <= 0) return;
    }
    c.hp -= amount;
    if (c.hp <= 0) {
      // 致命伤害拦截钩子（不屈等的原生实现位）
      var opponent = c.opponent_ref;
      if (!opponent) return;
      var ctx = new Ctx(game, rng, ev, tick, c, opponent, [c, opponent]);
      ctx.hook_name = "on_lethal";
      runHook(c.skills, "on_lethal", ctx);
    }
  }

  function makeCombatant(f, pos, game) {
    var bc = game.battle;
    var skills = [];
    var pers = NFE.personalizedEffects(f, game);
    for (var i = 0; i < pers.length; i++) {
      var plan = NFE.compileGraph(pers[i][1], null, false);
      skills.push([pers[i][0], pers[i][1], plan]);
    }
    var c = {
      fighter: f, pos: pos,
      name: "【" + NFE.composeTitleName(f, game) + "】" + f.name,
      plain_name: f.name,
      max_hp: Number(f.attrs.hp), hp: Number(f.attrs.hp),
      atk: Number(f.attrs.atk), defense: Number(f.attrs.def),
      spd: Number(f.attrs.spd), dodge: Number(f.attrs.dodge),
      crit: Number(f.attrs.crit),
      skills: skills,
      gauge: 0.0, seq: 0,
      st: new Map(),           // 状态容器（Map：施加顺序 = 结算顺序）
      markers: new Set(),      // 一次性标记（once:键）
      marks: new Map(),        // 图内标记：key -> {n 层数, expires 到期刻}
      live_prev: new Set(),    // 上一刻在场状态 id
      damage_dealt: 0.0,
      steal_rec: 0.0,
      opponent_ref: null
    };
    c.dodge = Math.min(c.dodge, bc.dodgeCap);
    c.crit = Math.min(c.crit, bc.critCap);
    return c;
  }

  function computeDamage(actor, enemy, mult, crit, game, rng, pen, tick) {
    var bc = game.battle;
    var variance = rng.nextTriangular(bc.varianceLo, bc.varianceHi);
    var critMult = crit ? bc.critMultiplier : 1.0;
    var raw = effAtk(actor, game, tick) * bc.atkFactor * variance * critMult * mult;
    var armor = effDef(enemy, game, tick);
    var reduction = armor / (armor + bc.defenseConstant) * (1.0 - pen);
    return Math.max(bc.minDamage, raw * (1.0 - reduction));
  }

  function snapshot(combatants, threshold, tick, game) {
    function one(c) {
      var buffs = statuses.statusDisplay(c, tick, game.battle.guardReductionCap, game);
      var spd = effSpd(c, game, tick);
      var gaugePct = Math.max(0.0, Math.min(100.0, c.gauge * 100.0 / threshold));
      return {
        hp: pyRoundN(Math.max(0.0, c.hp), 2),
        max_hp: pyRoundN(c.max_hp, 2),
        atk: pyRoundN(effAtk(c, game, tick), 2),
        def: pyRoundN(effDef(c, game, tick), 2),
        spd: pyRoundN(spd, 2),
        crit: pyRoundN(c.crit, 2),
        dodge: pyRoundN(c.dodge, 2),
        gauge: pyRoundN(c.gauge, 2),
        gauge_pct: pyRoundN(gaugePct, 2),
        gauge_gain: pyRoundN(spd, 2),
        gauge_pct_gain: pyRoundN(spd * 100.0 / threshold, 2),
        gauge_threshold: pyRoundN(threshold, 2),
        buffs: buffs
      };
    }
    return { a: one(combatants[0]), b: one(combatants[1]) };
  }

  function defend(defender, attacker, dmg, game, rng, ev, tick) {
    var bc = game.battle;
    var liveList = statuses.eachLive(defender, game, tick);
    for (var i = 0; i < liveList.length; i++) {
      var sid = liveList[i][0], st = liveList[i][1], sdef = liveList[i][2];
      var mods = sdef.mods || [];
      for (var m = 0; m < mods.length; m++) {
        if (mods[m].kind !== "dmg_in_cut_pct" || dmg <= 0) continue;
        var n = statuses.liveStacks(defender, sid, tick, sdef);
        var v = Number(statuses.resolve(mods[m].value !== undefined ? mods[m].value : 0.0, st.params));
        var ratio = Math.min(bc.guardReductionCap, v * (mods[m].per_stack ? n : 1));
        dmg = Math.max(bc.minDamage, dmg * (1.0 - ratio));
        ev("effect_reduction", { b: defender.name, ratio: formatPct(ratio) });
      }
    }
    var dc = { dmg: dmg, absorbed: 0.0 };
    var ctx = new Ctx(game, rng, ev, tick, defender, attacker, [defender, attacker]);
    ctx.dc = dc;
    if (runHook(defender.skills, "on_defend", ctx) === "immune") {
      return 0.0;
    }
    return dc.dmg;
  }

  function applyLifesteal(actor, dmg, ev, game, tick) {
    if (dmg <= 0 || actor.hp <= 0) return;
    var steal = 0.0;
    var recordSids = [];
    var liveList = statuses.eachLive(actor, game, tick);
    for (var i = 0; i < liveList.length; i++) {
      var sid = liveList[i][0], st = liveList[i][1], sdef = liveList[i][2];
      var mods = sdef.mods || [];
      for (var m = 0; m < mods.length; m++) {
        if (mods[m].kind !== "lifesteal_pct") continue;
        var n = statuses.liveStacks(actor, sid, tick, sdef);
        var v = Number(statuses.resolve(mods[m].value !== undefined ? mods[m].value : 0.0, st.params));
        steal += v * (mods[m].per_stack ? n : 1);
        if (mods[m].record === "lifesteal") recordSids.push(sid);
      }
    }
    if (steal <= 0) return;
    var gained = _r(Math.min(dmg * steal, actor.max_hp - actor.hp));
    if (gained <= 0) return;
    actor.hp += gained;
    actor.steal_rec += gained;
    ev("effect_lifesteal", { a: actor.name, heal: formatNum(gained) });
    for (i = 0; i < recordSids.length; i++) {
      var st2 = actor.st.get(recordSids[i]);
      if (st2) st2.total += gained;
    }
  }

  function hitReactions(actor, enemy, dmg, crit, game, rng, ev, tick) {
    if (dmg <= 0 || enemy.hp <= 0) return;
    var ctx = new Ctx(game, rng, ev, tick, actor, enemy, [actor, enemy]);
    ctx.dmg = dmg;
    ctx.crit_hit = crit;
    runHook(actor.skills, "on_hit_landed", ctx);
    var ctx2 = new Ctx(game, rng, ev, tick, enemy, actor, [actor, enemy]);
    ctx2.dmg = dmg;
    ctx2.crit_hit = crit;
    runHook(enemy.skills, "on_hit_taken", ctx2);
  }

  function attack(actor, enemy, game, rng, ev, tick) {
    var combatants = [actor, enemy];

    ev("attack_start", { a: actor.name });

    // 蓄力释放：首个带 on_owner_action_consume 效果图的状态替换本次行动
    var chargeList = statuses.eachLive(actor, game, tick);
    for (var ci = 0; ci < chargeList.length; ci++) {
      var csid = chargeList[ci][0], cst = chargeList[ci][1];
      var plan = (game.statusPlans[csid] || {}).on_owner_action_consume;
      if (!plan) continue;
      actor.st.delete(csid);
      var cctx = new Ctx(game, rng, ev, tick, actor, enemy, combatants);
      cctx.hook_name = "on_owner_action_consume";
      if (cst.links && cst.links.length) {
        var fake = { kind: "op", type: "apply_status",
                     params: {}, links: cst.links };
        for (var fk in cst.params) {
          if (Object.prototype.hasOwnProperty.call(cst.params, fk)) fake.params[fk] = cst.params[fk];
        }
        var cproc = procParams(fake, cctx);
        var filtered = {};
        for (fk in cproc) {
          if (Object.prototype.hasOwnProperty.call(cproc, fk) && typeof cproc[fk] !== "string") {
            filtered[fk] = cproc[fk];
          }
        }
        cst.params = filtered;
      }
      cctx.status = [csid, cst];
      for (var t = 0; t < plan.length; t++) {
        if (runTree(plan[t], cctx)) return;
      }
      return;
    }

    // 攻击钩子链（技能按派生顺序）
    var ac = { mult: 1.0, pen: 0.0, crit_flat: 0.0, must_hit: false,
               crit: false, replaced: false };
    var ctx = new Ctx(game, rng, ev, tick, actor, enemy, combatants);
    ctx.ac = ac;
    ctx.defer = [];
    if (runHook(actor.skills, "on_attack", ctx) === "consume") return;
    if (ac.replaced) return;

    var outPct = statuses.sumMod(actor, game, tick, "dmg_out_pct");
    if (outPct > 0) ac.mult *= 1.0 + outPct;

    // 闪避判定（落空触发 on_attack_miss 钩子）
    var bc = game.battle;
    if (!ac.must_hit && rng.nextFloat() < enemy.dodge / 100.0) {
      ev("attack_miss", { a: actor.name, b: enemy.name });
      var mctx = new Ctx(game, rng, ev, tick, actor, enemy, [actor, enemy]);
      runHook(actor.skills, "on_attack_miss", mctx);
      return;
    }

    var crit = rng.nextFloat() < Math.min(bc.critCap / 100.0, actor.crit / 100.0 + ac.crit_flat);
    ac.crit = crit;
    if (crit) ev("attack_crit", {});
    var dmg = computeDamage(actor, enemy, ac.mult, crit, game, rng, ac.pen, tick);
    dmg = defend(enemy, actor, dmg, game, rng, ev, tick);

    dmg = _r(dmg);
    if (dmg > 0) {
      hurt(enemy, dmg, ev, rng, game, tick);
      actor.damage_dealt += dmg;
    }
    ev("attack_hit", { a: actor.name, b: enemy.name, damage: formatNum(dmg) });
    applyLifesteal(actor, dmg, ev, game, tick);
    hitReactions(actor, enemy, dmg, crit, game, rng, ev, tick);

    // 延迟施加（命中结算后）
    for (var d = 0; d < ctx.defer.length; d++) ctx.defer[d]();
  }

  function runBattle(fighterA, fighterB, game, snapshots, record) {
    if (snapshots === undefined) snapshots = true;
    if (record === undefined) record = true;
    var bc = game.battle;
    var combatants = [makeCombatant(fighterA, 0, game), makeCombatant(fighterB, 1, game)];
    var internal = combatants.slice().sort(function (a, b) {
      var d = b.fighter.attrs.spd - a.fighter.attrs.spd;
      if (d) return d;
      return pyCmp(a.fighter.normalized, b.fighter.normalized);
    });
    for (var i = 0; i < internal.length; i++) internal[i].seq = i;
    var joined = pySorted([fighterA.normalized, fighterB.normalized]).join(bc.seedSeparator);
    var seedHex = NFE.md5Hex(joined);
    var rng = new NFE.DetRng(BigInt("0x" + seedHex));

    var events = [];
    var tick = 0;
    var lastLoggedTick = 0;
    var winner = null;
    var draw = false;

    function ev(template, params) {
      if (!record) return;
      var state = snapshots ? snapshot(combatants, bc.gaugeThreshold, tick, game) : null;
      if (tick !== lastLoggedTick) {
        var marker = { tick: tick, template: "tick_marker", params: { tick: tick } };
        if (snapshots) marker.state = state;
        events.push(marker);
        lastLoggedTick = tick;
      }
      var entry = { tick: tick, template: template, params: params || {} };
      if (snapshots) entry.state = state;
      events.push(entry);
    }

    var first = internal[0], second = internal[1];
    first.opponent_ref = second;
    second.opponent_ref = first;
    ev("battle_start", { a: first.name, b: second.name });

    var boot = new Ctx(game, rng, ev, 0, first, second, combatants);
    runHook(first.skills, "battle_start", boot);
    var boot2 = new Ctx(game, rng, ev, 0, second, first, combatants);
    runHook(second.skills, "battle_start", boot2);

    function settleDeaths(actor, enemy) {
      if (actor.hp <= 0 && enemy.hp <= 0) {
        draw = true;
      } else if (enemy.hp <= 0) {
        ev("death", { b: enemy.name });
        winner = actor;
      } else if (actor.hp <= 0) {
        ev("death", { b: actor.name });
        winner = enemy;
      }
      return winner !== null || draw;
    }

    function statusDeath(c, sid) {
      if (c.hp > 0) return false;
      var deathEvent = null;
      var sdef = sid ? game.statuses[sid] : null;
      if (sdef) deathEvent = sdef.death_event;
      if (!deathEvent) {
        var liveList = statuses.eachLive(c, game, tick);
        for (var i = 0; i < liveList.length; i++) {
          if (liveList[i][2].death_event) {
            deathEvent = liveList[i][2].death_event;
            break;
          }
        }
      }
      if (deathEvent) ev(String(deathEvent), { a: c.name });
      winner = (c === internal[0]) ? internal[1] : internal[0];
      return true;
    }

    while (tick < bc.maxTicks && winner === null && !draw) {
      tick += 1;
      // ---- 每刻开始：审判类逐层到期（on_status_expire，每层一次） ----
      var done = false;
      for (var ci = 0; ci < internal.length && !done; ci++) {
        var c = internal[ci];
        if (c.hp <= 0) continue;
        var enemy = internal[1] === c ? internal[0] : internal[1];
        var stSnapshot = [];
        c.st.forEach(function (st, sid) { stSnapshot.push([sid, st]); });
        for (var si = 0; si < stSnapshot.length; si++) {
          var sid = stSnapshot[si][0], st = stSnapshot[si][1];
          var sdef = game.statuses[sid];
          if (!sdef || sdef.stack !== "layers") continue;
          if (!c.live_prev.has(sid)) continue;
          var dropped = st.layers.filter(function (e) { return e[0] <= tick; });
          if (!dropped.length) continue;
          st.layers = st.layers.filter(function (e) { return e[0] > tick; });
          var plan = (game.statusPlans[sid] || {}).on_status_expire;
          if (!plan) continue;
          for (var di = 0; di < dropped.length; di++) {
            var expTick = dropped[di][0], layerParams = dropped[di][1];
            var ectx = new Ctx(game, rng, ev, tick, c, enemy, combatants);
            ectx.hook_name = "on_status_expire";
            var layerView = statuses.newRuntime();
            layerView.params = {};
            for (var lp in layerParams) {
              if (Object.prototype.hasOwnProperty.call(layerParams, lp)) {
                layerView.params[lp] = layerParams[lp];
              }
            }
            layerView.applier = st.applier;
            ectx.status = [sid, layerView];
            for (var t = 0; t < plan.length; t++) {
              if (runTree(plan[t], ectx)) break;
            }
          }
          if (statusDeath(c, sid)) break;
        }
        if (winner !== null) done = true;
      }
      if (winner !== null) break;

      // ---- 每刻开始：标记到期清理 / 状态失去检测（on_status_lose） ----
      for (ci = 0; ci < internal.length; ci++) {
        c = internal[ci];
        if (c.hp <= 0) continue;
        var markKeys = [];
        c.marks.forEach(function (_m, key) { markKeys.push(key); });
        for (var mk = 0; mk < markKeys.length; mk++) {
          var m = c.marks.get(markKeys[mk]);
          if (m.expires && m.expires <= tick) c.marks.delete(markKeys[mk]);
        }
        var enemy2 = internal[1] === c ? internal[0] : internal[1];
        var nowLive = new Set();
        var liveNow = statuses.eachLive(c, game, tick);
        for (i = 0; i < liveNow.length; i++) nowLive.add(liveNow[i][0]);
        if (c.live_prev.size) {
          var gone = [];
          c.live_prev.forEach(function (sid2) { if (!nowLive.has(sid2)) gone.push(sid2); });
          gone.sort(pyCmp);
          for (i = 0; i < gone.length; i++) {
            fireStatusEvent(c, enemy2, combatants, gone[i], false, game, rng, ev, tick);
          }
        }
        c.live_prev = nowLive;
      }

      // ---- 每刻开始：状态 tick 图（毒发 / 回春回复，按施加顺序） ----
      done = false;
      for (ci = 0; ci < internal.length && !done; ci++) {
        c = internal[ci];
        if (c.hp <= 0) continue;
        var enemy3 = internal[1] === c ? internal[0] : internal[1];
        var tickList = statuses.eachLive(c, game, tick);
        for (var ti = 0; ti < tickList.length; ti++) {
          var tsid = tickList[ti][0], tst = tickList[ti][1], tsdef = tickList[ti][2];
          var interval = statuses.resolve(tsdef.interval !== undefined ? tsdef.interval : 0, tst.params);
          if (!interval || tick < tst.next) continue;
          tst.next += Math.max(1, pyInt(interval));
          runStatusHooks(c, enemy3, combatants, "on_status_tick", game, rng, ev, tick, [tsid, tst]);
          if (statusDeath(c)) break;
        }
        if (winner !== null) done = true;
      }
      if (winner !== null) break;

      // ---- 行动槽推进 ----
      for (i = 0; i < combatants.length; i++) {
        if (combatants[i].hp > 0) {
          combatants[i].gauge += effSpd(combatants[i], game, tick);
        }
      }
      var ready = internal.filter(function (c2) {
        return c2.hp > 0 && c2.gauge >= bc.gaugeThreshold;
      });
      ready.sort(function (a2, b2) { return b2.gauge - a2.gauge || a2.seq - b2.seq; });
      for (var ri = 0; ri < ready.length; ri++) {
        var actor = ready[ri];
        var enemy4 = internal[1] === actor ? internal[0] : internal[1];
        actor.gauge -= bc.gaugeThreshold;
        if (actor.hp <= 0 || enemy4.hp <= 0) break;
        // ---- 打断钩子：敌方即将行动时（斩断退条 + 抢攻） ----
        var ictx = new Ctx(game, rng, ev, tick, enemy4, actor, combatants);
        ictx.defer = [];
        runHook(enemy4.skills, "action_interrupt", ictx);
        for (var df = 0; df < (ictx.defer || []).length; df++) ictx.defer[df]();
        if (ictx.executed) {
          if (settleDeaths(enemy4, actor)) break;
          continue;
        }
        // ---- 拥有者行动开始状态图（流血损失 / 眩晕吞行动） ----
        var sig = runStatusHooks(actor, enemy4, combatants, "on_owner_action",
                                 game, rng, ev, tick);
        if (statusDeath(actor)) break;
        if (sig === "consume") continue;
        // ---- 行动开始钩子（血契 / 回春 / 净化） ----
        var sctx = new Ctx(game, rng, ev, tick, actor, enemy4, combatants);
        sctx.defer = [];
        runHook(actor.skills, "action_start", sctx);
        for (df = 0; df < (sctx.defer || []).length; df++) sctx.defer[df]();
        if (settleDeaths(actor, enemy4)) break;
        // ---- 攻击前钩子（背水一战等一次性判定） ----
        var bctx = new Ctx(game, rng, ev, tick, actor, enemy4, combatants);
        runHook(actor.skills, "before_attack", bctx);
        attack(actor, enemy4, game, rng, ev, tick);
        if (settleDeaths(actor, enemy4)) break;
        // ---- 行动后：成长钩子 / 按行动衰减到期 / 吸血累计清零 ----
        if (actor.hp > 0) {
          var actx = new Ctx(game, rng, ev, tick, actor, enemy4, combatants);
          runHook(actor.skills, "after_action", actx);
          var afterList = statuses.eachLive(actor, game, tick);
          for (var al = 0; al < afterList.length; al++) {
            if (afterList[al][2].expire === "actions") {
              afterList[al][1].actions -= 1;
            }
          }
          actor.steal_rec = 0.0;
        }
      }
    }

    if (winner === null && !draw) {
      ev("timeout", {});
      var ratioA = combatants[0].hp / combatants[0].max_hp;
      var ratioB = combatants[1].hp / combatants[1].max_hp;
      if (ratioA > ratioB) winner = combatants[0];
      else if (ratioB > ratioA) winner = combatants[1];
      else draw = true;
    }

    if (draw) {
      ev("draw", {});
    } else {
      ev("victory", { winner: winner.name });
    }

    return {
      winner_pos: draw ? -1 : winner.pos,
      winner_name: draw ? null : winner.plain_name,
      draw: draw,
      ticks: tick,
      damage: [combatants[0].damage_dealt, combatants[1].damage_dealt],
      seed: seedHex,
      events: events
    };
  }

  /* ---- 战报渲染 ---- */

  function renderEvents(events, game) {
    return events.map(function (e) {
      var tmpl = game.battleLog[e.template] !== undefined ? game.battleLog[e.template] : e.template;
      return renderTemplate(tmpl, e.params, game);
    });
  }

  function richSegments(template, params, game, sideOfName) {
    var out = [];
    var text = String(template === null || template === undefined ? "" : template);
    var pos = 0;
    var re = new RegExp(PLACEHOLDER.source, "g");
    var m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > pos) out.push({ t: text.slice(pos, m.index), k: "plain" });
      var key = m[1];
      var value = (params || {})[key];
      if (value && typeof value === "object" && "ref" in value && "id" in value) {
        var name = game.refName(value.ref, value.id);
        if (name === null || name === undefined) name = String(value.id);
        if (value.ref === "skill") {
          out.push({ t: name, k: "skill", id: String(value.id) });
        } else {
          out.push({ t: name, k: "plain" });
        }
      } else if (RICH_NAME_KEYS.indexOf(key) >= 0 && typeof value === "string" && value) {
        var side = sideOfName[value];
        out.push({ t: value, k: side ? "name-" + side : "plain" });
      } else if (RICH_DAMAGE_KEYS.indexOf(key) >= 0) {
        out.push({ t: pyStr(value), k: "dmg" });
      } else if (RICH_HEAL_KEYS.indexOf(key) >= 0) {
        out.push({ t: pyStr(value), k: "heal" });
      } else {
        out.push({ t: pyStr(value), k: "plain" });
      }
      pos = m.index + m[0].length;
    }
    if (pos < text.length) out.push({ t: text.slice(pos), k: "plain" });
    return out;
  }

  function renderState(state, game) {
    var out = {};
    var sides = state ? Object.keys(state) : [];
    for (var i = 0; i < sides.length; i++) {
      var side = sides[i], snap = state[side];
      var buffs = [];
      var snapBuffs = snap.buffs || [];
      for (var b = 0; b < snapBuffs.length; b++) {
        var entry = game.statuses[snapBuffs[b].id] || {};
        buffs.push({
          id: snapBuffs[b].id,
          name: entry.name !== undefined ? entry.name : snapBuffs[b].id,
          detail: renderTemplate(entry.detail !== undefined ? entry.detail : "",
                                 snapBuffs[b].params, game),
          desc: entry.desc !== undefined ? entry.desc : ""
        });
      }
      var copy = {};
      for (var k in snap) {
        if (Object.prototype.hasOwnProperty.call(snap, k)) copy[k] = snap[k];
      }
      copy.buffs = buffs;
      out[side] = copy;
    }
    return out;
  }

  function battleToApi(outcome, fightersApi, game) {
    var sideOfName = {};
    for (var i = 0; i < (fightersApi || []).length; i++) {
      var f = fightersApi[i];
      var title = String(((f.title || {}).name) || "");
      var key = title ? "【" + title + "】" + (f.name !== undefined ? f.name : "")
                      : String(f.name !== undefined ? f.name : "");
      sideOfName[key] = i === 0 ? "a" : "b";
    }
    var texts = renderEvents(outcome.events, game);
    var log = [];
    for (var e = 0; e < outcome.events.length; e++) {
      var ev = outcome.events[e];
      var entry = {};
      for (var k in ev) {
        if (Object.prototype.hasOwnProperty.call(ev, k)) entry[k] = ev[k];
      }
      entry.text = texts[e];
      var tmpl = game.battleLog[ev.template] !== undefined ? game.battleLog[ev.template] : ev.template;
      entry.rich = richSegments(tmpl, ev.params, game, sideOfName);
      if ("state" in entry) entry.state = renderState(entry.state, game);
      log.push(entry);
    }
    return {
      fighters: fightersApi,
      result: {
        winner: outcome.winner_name,
        winner_pos: outcome.winner_pos,
        draw: outcome.draw,
        ticks: outcome.ticks,
        damage: {
          a: pyRoundN(outcome.damage[0], 2),
          b: pyRoundN(outcome.damage[1], 2)
        }
      },
      seed: outcome.seed,
      log: log
    };
  }

  NFE.runBattle = runBattle;
  NFE.battleToApi = battleToApi;
  NFE.renderEvents = renderEvents;
  NFE.makeCombatant = makeCombatant;
})(typeof window !== "undefined" ? window : globalThis);
