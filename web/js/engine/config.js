/* 配置构建 —— namefight/config.py build_game_config 的移植（静态版）。
   静态包内携带的六份 JSON 已由 Python 侧在发布时校验过，这里只做
   形状构建与图编译（默认值口径与 Python 一致），不做重复的完整性校验。 */
(function (root) {
  "use strict";
  var NFE = root.NFE = root.NFE || {};
  var statuses = NFE, effects = NFE;

  var TITLE_FIELD_POOLS = { prefix: "prefix", core: "core", core2: "core", suffix: "suffix" };

  function num(v, dflt) {
    v = (v === undefined || v === null) ? dflt : v;
    return Number(v);
  }

  function buildGameConfig(data) {
    var sysData = data.system, attrsData = data.attributes, skillsData = data.skills;
    var titlesData = data.titles, battleData = data.battle, uiData = data.ui;

    var nameCfg = sysData.name || {};
    var system = {
      version: String(sysData.version),
      language: String(sysData.language !== undefined ? sysData.language : "zh"),
      nameTrim: nameCfg.trim !== undefined ? !!nameCfg.trim : true,
      nameCaseSensitive: !!nameCfg.case_sensitive,
      nameMinLength: num(nameCfg.min_length, 1),
      nameMaxLength: num(nameCfg.max_length, 32)
    };

    var attributes = (attrsData.attributes || []).map(function (a) {
      var base = Number(a.base);
      return {
        id: String(a.id),
        name: String(a.name !== undefined ? a.name : a.id),
        emoji: String(a.emoji !== undefined ? a.emoji : ""),
        base: base,
        min: Number(a.min !== undefined ? a.min : base),
        max: Number(a.max !== undefined ? a.max : base),
        format: String(a.format !== undefined ? a.format : "int"),
        powerWeight: num(a.power_weight, 0)
      };
    });

    var battleLog = battleData.battle_log || {};

    // 状态定义 -> 参数规格（先于技能图编译：apply_status 参数校验依赖）
    var statusesData = battleData.statuses || {};
    var statusSpecs = {};
    Object.keys(statusesData).forEach(function (sid) {
      statusSpecs[String(sid)] = statuses.statusParamSpecs(statusesData[sid]);
    });
    function statusSpecsCb(sid) {
      if (!sid) return null;
      if (!statusSpecs[sid]) throw new Error("apply_status 引用了未定义的状态: " + sid);
      return statusSpecs[sid];
    }

    var statusPlans = {};
    Object.keys(statusesData).forEach(function (sid) {
      var plan = statuses.compileStatusEffects(statusesData[sid], statusSpecsCb);
      if (Object.keys(plan).length) statusPlans[sid] = plan;
    });

    var skills = (skillsData.skills || []).map(function (s) {
      var mastery = s.mastery !== undefined ? s.mastery : [1.0, 1.0];
      var masteryOn = s.mastery_on !== undefined ? s.mastery_on : "chance";
      if (typeof masteryOn === "string") masteryOn = [masteryOn];
      masteryOn = masteryOn.map(String);
      var plan = effects.compileGraph(s.effect || {}, statusSpecsCb, true);
      var resonance = (s.resonance || []).map(function (r) {
        return {
          param: String(r.param),
          variable: String(r.variable || ""),
          mode: String(r.mode !== undefined ? r.mode : "own"),
          rate: num(r.rate, 0.45),
          node: String(r.node || "")
        };
      });
      return {
        id: String(s.id),
        name: String(s.name !== undefined ? s.name : s.id),
        description: String(s.description !== undefined ? s.description : ""),
        weight: num(s.weight, 1),
        effect: s.effect || {},
        plan: plan,
        mastery: [Number(mastery[0]), Number(mastery[1])],
        masteryOn: masteryOn,
        resonance: resonance
      };
    });

    var skillCount = skillsData.skill_count || {};
    var scMin = num(skillCount.min, 1), scMax = num(skillCount.max, 1);

    var varData = skillsData.md5_variance || {};
    var varValue = varData.value !== undefined ? varData.value : [1.0, 1.0];
    var md5Variance = { valueLo: Number(varValue[0]), valueHi: Number(varValue[1]) };

    // 技能变量共鸣（variables 保持 JSON 声明顺序 = 抽取遍历顺序）
    var linkData = skillsData.variable_link || {};
    var linkVariables = [];
    var linkRaw = linkData.variables || {};
    Object.keys(linkRaw).forEach(function (vid) {
      var spec = linkRaw[vid];
      var rate = spec.rate !== undefined ? spec.rate : [0.0, 0.0];
      linkVariables.push({
        id: String(vid),
        weight: num(spec.weight, 1),
        rateLo: Number(rate[0]),
        rateHi: Number(rate[1]),
        diffAgainst: String(spec.diff_against !== undefined ? spec.diff_against : vid)
      });
    });
    var modeWeights = [];
    var mwRaw = linkData.mode_weights || { own: 1 };
    Object.keys(mwRaw).forEach(function (key) {
      modeWeights.push([String(key), Number(mwRaw[key])]);
    });
    if (!modeWeights.length) modeWeights = [["own", 1.0]];
    var variableLink = {
      chance: num(linkData.chance, 0),
      variables: linkVariables,
      modeWeights: modeWeights,
      maxSlots: num(linkData.max_slots, 2)
    };

    // 技能名称词缀（前缀 / 后缀，附带小幅参数修正）
    var modData = skillsData.name_modifiers || {};
    function loadModPool(pool) {
      return (pool || []).map(function (entry) {
        var mod = {};
        var raw = entry.mod || {};
        Object.keys(raw).forEach(function (k) { mod[String(k)] = Number(raw[k]); });
        return {
          id: String(entry.id),
          name: String(entry.name !== undefined ? entry.name : entry.id),
          weight: num(entry.weight, 1),
          mod: mod
        };
      });
    }
    var modScale = modData.mod_variance !== undefined ? modData.mod_variance : [1.0, 1.0];
    var nameModifiers = {
      prefixChance: num(modData.prefix_chance, 0),
      suffixChance: num(modData.suffix_chance, 0),
      prefixes: loadModPool(modData.prefixes),
      suffixes: loadModPool(modData.suffixes),
      scaleLo: Number(modScale[0]),
      scaleHi: Number(modScale[1])
    };

    // 称号：结构与字段池
    var structures = (titlesData.structures || []).map(function (s) {
      var fields = (s.fields || []).map(String);
      var connectors = (s.connectors || []).map(String);
      while (connectors.length < Math.max(0, fields.length - 1)) connectors.push("");
      return {
        id: String(s.id),
        weight: num(s.weight, 1),
        fields: fields,
        connectors: connectors
      };
    });

    var titlePools = {};
    [["prefix", "prefixes"], ["core", "cores"], ["suffix", "suffixes"]].forEach(function (pair) {
      var poolName = pair[0], poolKey = pair[1];
      titlePools[poolName] = (titlesData[poolKey] || []).map(function (entry) {
        var bonus = {};
        var raw = entry.bonus || {};
        Object.keys(raw).forEach(function (k) { bonus[String(k)] = NFE.pyInt(Number(raw[k])); });
        return {
          id: String(entry.id),
          name: String(entry.name !== undefined ? entry.name : entry.id),
          desc: String(entry.desc !== undefined ? entry.desc : ""),
          weight: num(entry.weight, 1),
          bonus: bonus
        };
      });
    });

    var playback = battleData.playback || {};
    var variance = battleData.variance !== undefined ? battleData.variance : [1.0, 1.0];
    var powerCheck = battleData.power_check || {};
    var battle = {
      critMultiplier: num(battleData.crit_multiplier, 1.8),
      varianceLo: Number(variance[0]),
      varianceHi: Number(variance[1]),
      atkFactor: num(battleData.atk_factor, 1.0),
      defenseConstant: num(battleData.defense_constant, 25.0),
      minDamage: num(battleData.min_damage, 1),
      maxTicks: num(battleData.max_ticks, 600),
      gaugeThreshold: num(battleData.gauge_threshold, 100),
      critCap: num(battleData.crit_cap, 100),
      dodgeCap: num(battleData.dodge_cap, 60),
      guardReductionCap: num(battleData.guard_reduction_cap, 0.75),
      reflectSplitCap: num(battleData.reflect_split_cap, 0.9),
      seedSeparator: String(battleData.seed_separator !== undefined ? battleData.seed_separator : ""),
      powerEnemies: num(powerCheck.enemies, 10000),
      messageDelayMs: num(playback.message_delay_ms, 320),
      actionPauseEvery: num(playback.action_pause_every, 5),
      actionPauseMs: num(playback.action_pause_ms, 1600)
    };

    var game = {
      system: system,
      attributes: attributes,
      skills: skills,
      titleStructures: structures,
      titlePools: titlePools,
      skillCountMin: scMin,
      skillCountMax: scMax,
      skillMd5Variance: md5Variance,
      skillVariableLink: variableLink,
      skillNameModifiers: nameModifiers,
      battle: battle,
      stats: skillsData.stats || {},
      battleLog: battleLog,
      statuses: statusesData,
      statusSpecs: statusSpecs,
      statusPlans: statusPlans,
      ui: uiData
    };

    game.attr = function (attrId) {
      for (var i = 0; i < attributes.length; i++) {
        if (attributes[i].id === attrId) return attributes[i];
      }
      throw new Error("未定义的属性: " + attrId);
    };
    game.skillDef = function (skillId) {
      for (var i = 0; i < skills.length; i++) {
        if (skills[i].id === skillId) return skills[i];
      }
      return null;
    };
    game.titleField = function (poolName, fieldId) {
      var pool = titlePools[poolName] || [];
      for (var i = 0; i < pool.length; i++) {
        if (pool[i].id === fieldId) return pool[i];
      }
      return null;
    };
    game.nameModifier = function (kind, modId) {
      var pool = kind === "prefix" ? nameModifiers.prefixes : nameModifiers.suffixes;
      for (var i = 0; i < pool.length; i++) {
        if (pool[i].id === modId) return pool[i];
      }
      return null;
    };
    game.refName = function (registry, refId) {
      if (registry === "stat_word") {
        var word = game.stats[refId];
        return word !== undefined && word !== null ? String(word) : null;
      }
      if (registry === "attr") {
        for (var i = 0; i < attributes.length; i++) {
          if (attributes[i].id === refId) return attributes[i].name;
        }
        return null;
      }
      if (registry === "skill") {
        var s = game.skillDef(refId);
        return s ? s.name : null;
      }
      return null;
    };

    return game;
  }

  NFE.TITLE_FIELD_POOLS = TITLE_FIELD_POOLS;
  NFE.buildGameConfig = buildGameConfig;
})(typeof window !== "undefined" ? window : globalThis);
