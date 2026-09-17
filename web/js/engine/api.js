/* 静态版 API shim：以嵌入式配置数据构建引擎，产出与后端各端点
   完全相同形状的 JSON（/api/text、/api/fighter、/api/battle、/api/power）。
   静态包页面检测到 NF.localApi 即走本地引擎，不再请求服务器。 */
(function (root) {
  "use strict";
  var NFE = root.NFE = root.NFE || {};

  function toApiError(e) {
    if (e && e.name === "InvalidName") {
      var err = new Error(e.code);
      err.code = e.code;
      return err;
    }
    var err2 = new Error("internal_error: " + ((e && e.message) || e));
    err2.code = "internal_error";
    return err2;
  }

  NFE.createApi = function (data) {
    var game = NFE.buildGameConfig(data);

    // 敌人缓存（同配置重复测量不重复派生）
    var enemyCache = { count: 0, fighters: [] };
    function enemyFighters(count) {
      if (enemyCache.count === count) return enemyCache.fighters;
      var fighters = [];
      for (var i = 1; i <= count; i++) {
        fighters.push(NFE.deriveFighter(String(i), game));
      }
      enemyCache = { count: count, fighters: fighters };
      return fighters;
    }

    return {
      local: true,
      version: game.system.version,

      /* GET /api/text */
      text: function () {
        return {
          lang: game.system.language,
          langs: [game.system.language],
          version: game.system.version,
          ui: game.ui,
          playback: {
            message_delay_ms: game.battle.messageDelayMs,
            action_pause_every: game.battle.actionPauseEvery,
            action_pause_ms: game.battle.actionPauseMs
          }
        };
      },

      /* GET /api/fighter?name= */
      fighter: function (name) {
        try {
          return NFE.fighterToApi(NFE.deriveFighter(name, game), game);
        } catch (e) {
          throw toApiError(e);
        }
      },

      /* POST /api/battle {a, b} */
      battle: function (a, b) {
        if (typeof a !== "string" || typeof b !== "string") {
          var e1 = new Error("empty_name");
          e1.code = "empty_name";
          throw e1;
        }
        try {
          var fa = NFE.deriveFighter(a, game);
          var fb = NFE.deriveFighter(b, game);
          var outcome = NFE.runBattle(fa, fb, game, true, true);
          return NFE.battleToApi(outcome, [
            NFE.fighterToApi(fa, game),
            NFE.fighterToApi(fb, game)
          ], game);
        } catch (e) {
          throw toApiError(e);
        }
      },

      /* POST /api/power {name, count} —— 分块执行保持页面响应 */
      measurePower: function (name, count, onProgress) {
        var total = pyIntCount(count || game.battle.powerEnemies);
        return new Promise(function (resolve, reject) {
          var fighter, wins, enemies, started, done;
          try {
            fighter = NFE.deriveFighter(name, game);
          } catch (e) {
            reject(toApiError(e));
            return;
          }
          enemies = enemyFighters(total);
          started = Date.now();
          wins = 0;
          done = 0;

          function chunk() {
            var limit = Math.min(done + 250, total);
            for (; done < limit; done++) {
              var outcome = NFE.runBattle(fighter, enemies[done], game, false, false);
              if (outcome.winner_pos === 0) wins += 1;
            }
            if (onProgress) onProgress(done, total);
            if (done < total) {
              setTimeout(chunk, 0);
            } else {
              var fApi = NFE.fighterToApi(fighter, game);
              resolve({
                name: fApi.name,
                title: fApi.title,
                power: fApi.power,
                true_power: wins,
                total: total,
                rate: NFE.pyRoundN(wins / total, 4),
                elapsed_ms: NFE.pyRound((Date.now() - started)), // 仅展示用
                version: game.system.version
              });
            }
          }
          chunk();
        });
      }
    };
  };

  function pyIntCount(v) {
    v = Math.trunc(Number(v));
    if (!isFinite(v) || v < 1) v = 1;
    return v;
  }
})(typeof window !== "undefined" ? window : globalThis);
