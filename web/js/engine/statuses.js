/* 状态系统 —— namefight/statuses.py 的移植。
   运行时容器挂在 _Combatant.st（Map，施加顺序 = 结算顺序）；
   定义数据化于 battle.json 的 statuses（策略字段 + params + mods + 效果图）。 */
(function (root) {
  "use strict";
  var NFE = root.NFE = root.NFE || {};
  var formatPct = NFE.formatPct, formatNum = NFE.formatNum;

  var MOD_KINDS = {
    dmg_out_pct: "拥有者造成伤害的乘区加成（乘胜 / 怨念）",
    dmg_in_cut_pct: "拥有者所受伤害的减免（锻痕；总量钳 guard_reduction_cap）",
    atk_pct: "攻击乘区加成（背水一战）",
    spd_pct: "速度乘区加成（背水一战）",
    atk_flat: "攻击加值（渐入佳境，每层累计）",
    spd_flat: "速度加值（渐入佳境，每层累计）",
    def_break: "防御减值（破甲，每层累计）",
    lifesteal_pct: "命中吸血比例（嗜血 / 血契；record=lifesteal 时记录吸血量）",
    shield: "护盾池（受到的伤害先按施加顺序消耗护盾余量，余量存于 value 参数）"
  };

  function resolve(value, params) {
    if (typeof value === "string" && value.charAt(0) === "$") {
      var v = params[value.slice(1)];
      return v === undefined ? 0.0 : v;
    }
    return value;
  }

  function newRuntime() {
    return {
      params: {},      // 施加时合并的参数（含个性化 / 共鸣结果）
      stacks: 0,       // count 模式层数
      expires: 0,      // refresh / count 模式的到期刻
      layers: [],      // layers 模式各层 [到期刻, 施加参数快照]
      next: 0,         // on_status_tick 的下次触发刻
      actions: 0,      // expire=actions 的剩余行动数
      applier: null,   // 施加者引用
      records: [],     // record 原子的记录（记仇：所受伤害）
      total: 0.0,      // stat_mod 的累计展示（血契转化攻击）
      links: []        // 施加节点的共鸣声明（蓄力释放时重算）
    };
  }

  function ensure(c, sid) {
    var st = c.st.get(sid);
    if (!st) {
      st = newRuntime();
      c.st.set(sid, st);
    }
    return st;
  }

  function live(c, sid, tick, sdef) {
    var st = c.st.get(sid);
    if (!st) return false;
    if (sdef.expire === "actions") return st.actions > 0;
    if (sdef.stack === "layers") {
      st.layers = st.layers.filter(function (e) { return e[0] > tick; });
      return st.layers.length > 0;
    }
    if (sdef.expire === "none") {
      return (Object.keys(st.params).length > 0 || st.stacks > 0 ||
              st.records.length > 0 || st.total > 0);
    }
    return st.expires > tick;
  }

  function liveStacks(c, sid, tick, sdef) {
    var st = c.st.get(sid);
    if (!st || !live(c, sid, tick, sdef)) return 0;
    if (sdef.stack === "layers") return st.layers.length;
    if (sdef.stack === "count") return st.stacks;
    return 1;
  }

  function eachLive(c, game, tick) {
    var out = [];
    c.st.forEach(function (st, sid) {
      var sdef = game.statuses[sid];
      if (sdef != null && live(c, sid, tick, sdef)) out.push([sid, st, sdef]);
    });
    return out;
  }

  function sumMod(c, game, tick, kind) {
    var total = 0.0;
    var liveList = eachLive(c, game, tick);
    for (var i = 0; i < liveList.length; i++) {
      var sid = liveList[i][0], st = liveList[i][1], sdef = liveList[i][2];
      var n = liveStacks(c, sid, tick, sdef);
      if (n <= 0) continue;
      var mods = sdef.mods || [];
      for (var m = 0; m < mods.length; m++) {
        if (mods[m].kind !== kind) continue;
        var v = Number(resolve(mods[m].value !== undefined ? mods[m].value : 0.0, st.params));
        total += v * (mods[m].per_stack ? n : 1);
      }
    }
    return total;
  }

  function statusDisplay(c, tick, guardCap, game) {
    var out = [];
    var liveList = eachLive(c, game, tick);
    for (var i = 0; i < liveList.length; i++) {
      var sid = liveList[i][0], st = liveList[i][1], sdef = liveList[i][2];
      var n = liveStacks(c, sid, tick, sdef);
      var params = { stacks: n };
      if (sdef.expire === "actions") {
        params.turns = st.actions;
      } else if (sdef.stack === "layers") {
        params.turns = st.layers.length
          ? Math.max.apply(null, st.layers.map(function (e) { return e[0]; })) - tick
          : 0;
      } else if (sdef.expire !== "none") {
        params.turns = Math.max(0, st.expires - tick);
      }
      var sdefParams = sdef.params || {};
      for (var key in st.params) {
        if (!Object.prototype.hasOwnProperty.call(st.params, key)) continue;
        var val = st.params[key];
        if (typeof val === "number") {
          var fmt = ((sdefParams[key] || {}).fmt) || "num";
          params[key] = fmt === "pct" ? formatPct(val) : formatNum(val);
        }
      }
      if (st.total) params.total = formatNum(st.total);
      if (st.records.length) {
        params.stacks = st.records.length;
        var sum = 0.0;
        for (var r = 0; r < st.records.length; r++) sum += st.records[r];
        params.value = formatNum(sum);
      }
      var mods = sdef.mods || [];
      for (var m = 0; m < mods.length; m++) {
        var mk = mods[m].kind;
        if (mk !== "dmg_out_pct" && mk !== "dmg_in_cut_pct") continue;
        var v2 = Number(resolve(mods[m].value !== undefined ? mods[m].value : 0.0, st.params));
        var per = mods[m].per_stack ? n : 1;
        if (mk === "dmg_in_cut_pct") {
          params.value = formatPct(Math.min(guardCap, v2 * per));
        } else {
          params.mult = formatPct(1.0 + v2 * per);
        }
      }
      out.push({ id: sid, params: params });
    }
    return out;
  }

  function dispelAll(combatants, tick, game, onLose) {
    var count = 0;
    for (var ci = 0; ci < combatants.length; ci++) {
      var c = combatants[ci];
      if (c.hp <= 0) continue;
      var snapshot = [];
      c.st.forEach(function (st, sid) { snapshot.push([sid, st]); });
      for (var si = 0; si < snapshot.length; si++) {
        var sid = snapshot[si][0], st = snapshot[si][1];
        var sdef = game.statuses[sid];
        if (!sdef || !sdef.dispellable) continue;
        if (!live(c, sid, tick, sdef)) continue;
        st.expires = 0;
        st.layers = [];
        st.stacks = 0;
        st.actions = 0;
        st.records = [];
        count += 1;
        if (onLose) onLose(c, sid);
      }
    }
    return count;
  }

  function statusParamSpecs(statusEntry) {
    var out = {};
    var params = statusEntry.params || {};
    for (var key in params) {
      if (!Object.prototype.hasOwnProperty.call(params, key)) continue;
      var spec = params[key];
      var fmt = String(spec.fmt !== undefined ? spec.fmt : "num");
      var clamp = spec.clamp !== undefined ? spec.clamp : null;
      var kind = fmt === "turns" ? "turns" : (fmt === "num" ? "float" : "pct");
      out[key] = NFE.P(key, kind, {
        fmt: fmt,
        clamp: clamp === null ? null : [
          clamp[0] === null ? null : clamp[0],
          (clamp[1] === undefined || clamp[1] === null) ? null : clamp[1]
        ],
        unit: spec.unit !== undefined ? spec.unit : null,
        link: !!spec.link
      });
    }
    return out;
  }

  function statusDefaults(statusEntry) {
    var out = {};
    var params = statusEntry.params || {};
    for (var k in params) {
      if (!Object.prototype.hasOwnProperty.call(params, k)) continue;
      if (params[k].default !== undefined && params[k].default !== null) {
        out[k] = params[k].default;
      }
    }
    return out;
  }

  function compileStatusEffects(statusEntry, statusParams) {
    var graph = statusEntry.effects || {};
    if (!graph.nodes || !graph.nodes.length) return {};
    return NFE.compileGraph(graph, statusParams, true);
  }

  NFE.MOD_KINDS = MOD_KINDS;
  NFE.resolve = resolve;
  NFE.newRuntime = newRuntime;
  NFE.ensure = ensure;
  NFE.live = live;
  NFE.liveStacks = liveStacks;
  NFE.eachLive = eachLive;
  NFE.sumMod = sumMod;
  NFE.statusDisplay = statusDisplay;
  NFE.dispelAll = dispelAll;
  NFE.statusParamSpecs = statusParamSpecs;
  NFE.statusDefaults = statusDefaults;
  NFE.compileStatusEffects = compileStatusEffects;
})(typeof window !== "undefined" ? window : globalThis);
