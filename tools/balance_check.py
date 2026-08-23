"""技能平衡性蒙特卡洛检验（v0.8.0 平衡调参工具）。

用法：python tools/balance_check.py [名字数] [对局数]

- 以固定种子生成一批名字、随机采样对局（全程确定性，结果可复现）；
- 统计每个技能的持有者胜率与出场数，以及先后手胜场 / 平局 / 平均刻数；
- 经验目标：所有技能胜率落在 45%~55% 区间，显著超出即为调参信号
  （调数值调 weights 均可，改完重跑本脚本验证）。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from namefight.battle import run_battle  # noqa: E402
from namefight.config import load_game_config  # noqa: E402
from namefight.fighter import derive_fighter  # noqa: E402
from namefight.rng import DetRng  # noqa: E402


def main(n_names: int = 400, n_battles: int = 6000) -> int:
    game = load_game_config(REPO_ROOT / "config")
    rng = DetRng(20260822)
    names = ["斗士%03d" % i for i in range(n_names)]
    fighters = [derive_fighter(n, game) for n in names]
    skills_of = [set(f.skill_ids) for f in fighters]
    stat = {s.id: [0, 0] for s in game.skills}  # [持有人次, 持有者获胜人次]
    pos_wins = [0, 0]
    draws = 0
    ticks_total = 0
    for _ in range(n_battles):
        ia = rng.next_u64() % n_names
        ib = rng.next_u64() % n_names
        if ia == ib:
            ib = (ib + 1) % n_names
        outcome = run_battle(fighters[ia], fighters[ib], game, snapshots=False)
        ticks_total += outcome.ticks
        winner_idx = None
        if outcome.draw:
            draws += 1
        else:
            pos_wins[outcome.winner_pos] += 1
            winner_idx = ia if outcome.winner_pos == 0 else ib
        for idx in (ia, ib):
            for sid in skills_of[idx]:
                stat[sid][0] += 1
                if idx == winner_idx:
                    stat[sid][1] += 1

    locale_names = {}
    try:
        import json
        with open(REPO_ROOT / "config" / "game" / "skills.json",
                  encoding="utf-8") as fh:
            locale_names = {s["id"]: s.get("name", s["id"])
                            for s in json.load(fh).get("skills", [])}
    except Exception:
        locale_names = {}

    rows = []
    for s in game.skills:
        present, wins = stat[s.id]
        rate = (wins / present) if present else 0.0
        rows.append((rate, present, s.id, s.weight))
    rows.sort()
    print("技能胜率（升序，目标区间 45%~55%）：")
    print("%-14s %-10s %8s %8s %6s" % ("skill", "name", "win%", "出现", "weight"))
    for rate, present, sid, weight in rows:
        flag = "" if 0.45 <= rate <= 0.55 else ("  <-- 偏弱" if rate < 0.45 else "  <-- 偏强")
        print("%-14s %-10s %7.1f%% %8d %6.1f%s"
              % (sid, locale_names.get(sid, ""), rate * 100, present, weight, flag))
    total = pos_wins[0] + pos_wins[1] + draws
    print()
    print("对局总数 %d · 平局 %.2f%% · 平均刻数 %.1f" % (
        total, draws * 100.0 / total, ticks_total / total))
    print("先手(位置0)胜 %d · 后手(位置1)胜 %d（位置偏差 %.2f%%）" % (
        pos_wins[0], pos_wins[1], abs(pos_wins[0] - pos_wins[1]) * 100.0 / total))
    return 0


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    b = int(sys.argv[2]) if len(sys.argv) > 2 else 6000
    sys.exit(main(n, b))
