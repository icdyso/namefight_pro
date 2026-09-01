"""真战力测量：与 N 个固定编号敌人（名字 "1".."N"）各打一场，胜场数即真战力。

- 敌人名字固定为编号字符串（"1"、"2"、…），派生与对战完全确定：
  同一名字在配置不变时永远得到同一真战力；
- 采用极速模拟（不生成快照、不记录战报条目），随机数消耗与常规对战
  完全一致，胜负判定与普通对战相同；平局不计胜场；
- 敌人斗士按配置快照缓存（同配置重复测量不重复派生；配置热重载后自动失效）；
- 只在显式请求（/api/power，真战力页）时运行，正常对战不受影响。
"""
from __future__ import annotations

import time

from .battle import run_battle
from .config import GameCfg
from .fighter import Fighter, derive_fighter

# 敌人缓存：单槽（game 对象同一且数量一致时命中）
_enemy_cache: dict = {"game": None, "count": 0, "fighters": ()}


def _enemy_fighters(game: GameCfg, count: int):
    if _enemy_cache["game"] is game and _enemy_cache["count"] == count:
        return _enemy_cache["fighters"]
    fighters = tuple(derive_fighter(str(i), game) for i in range(1, count + 1))
    _enemy_cache.update(game=game, count=count, fighters=fighters)
    return fighters


def measure_true_power(raw_name, game: GameCfg, count=None):
    """测量真战力：返回 (主斗士 Fighter, 胜场数, 耗时秒)。

    count 缺省取 battle.json 的 power_check.enemies（默认 10000；
    实测每场极速对战约 0.5ms，10000 场约数秒，100000 场过慢故不默认）。"""
    total = int(count or game.battle.power_enemies)
    fighter = derive_fighter(raw_name, game)
    enemies = _enemy_fighters(game, total)
    started = time.perf_counter()
    wins = 0
    for enemy in enemies:
        outcome = run_battle(fighter, enemy, game,
                             snapshots=False, record=False)
        if outcome.winner_pos == 0:
            wins += 1
    return fighter, wins, time.perf_counter() - started
