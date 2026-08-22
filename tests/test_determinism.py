"""确定性核心不变量测试（最高优先级，见 AGENTS.md 2.1）。

这些测试失败意味着「同名同命」契约被破坏，属于最高优先级事故。
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from namefight.battle import run_battle
from namefight.config import load_game_config, load_locale
from namefight.fighter import derive_fighter, fighter_to_api

CONFIG_ROOT = REPO_ROOT / "config"
GAME = load_game_config(CONFIG_ROOT)


def _outcome_payload(outcome):
    return {
        "winner": outcome.winner_name,
        "draw": outcome.draw,
        "rounds": outcome.rounds,
        "events": outcome.events,
        "damage": outcome.damage,
    }


def _game_data(f):
    """参与确定性契约的派生数据（name 仅为展示输入，不计入）。"""
    return (f.normalized, f.digest, f.rarity_id, f.element_id,
            tuple(sorted(f.attrs.items())), f.skill_ids, f.title_id, f.power)


class FighterDeterminism(unittest.TestCase):
    def test_same_name_same_fighter(self):
        base = derive_fighter("张三", GAME)
        for _ in range(3):
            self.assertEqual(base, derive_fighter("张三", GAME))

    def test_case_folding_follows_config(self):
        # system.json 默认 case_sensitive=false：大小写不同视为同一名字
        self.assertEqual(_game_data(derive_fighter("Alice", GAME)),
                         _game_data(derive_fighter("alice", GAME)))
        self.assertNotEqual(_game_data(derive_fighter("Alice", GAME)),
                            _game_data(derive_fighter("Bob", GAME)))

    def test_different_names_differ(self):
        fa = derive_fighter("张三", GAME)
        fb = derive_fighter("李四", GAME)
        self.assertNotEqual(fa.digest, fb.digest)
        differing = (
            fa.attrs != fb.attrs or fa.skill_ids != fb.skill_ids
            or fa.title_id != fb.title_id or fa.element_id != fb.element_id
        )
        self.assertTrue(differing, "两个不同名字的派生结果不应完全相同")

    def test_derivation_independent_of_locale(self):
        f = derive_fighter("张三", GAME)
        zh = fighter_to_api(f, GAME, load_locale(CONFIG_ROOT, "zh"))
        en = fighter_to_api(f, GAME, load_locale(CONFIG_ROOT, "en"))
        self.assertEqual([(a["id"], a["value"]) for a in zh["attributes"]],
                         [(a["id"], a["value"]) for a in en["attributes"]])
        self.assertEqual([s["id"] for s in zh["skills"]], [s["id"] for s in en["skills"]])
        self.assertEqual(zh["rarity"]["id"], en["rarity"]["id"])
        self.assertEqual(zh["title"]["id"], en["title"]["id"])


class BattleDeterminism(unittest.TestCase):
    def test_battle_reproducible(self):
        fa = derive_fighter("Alice", GAME)
        fb = derive_fighter("Bob", GAME)
        base = _outcome_payload(run_battle(fa, fb, GAME))
        for _ in range(3):
            self.assertEqual(base, _outcome_payload(run_battle(fa, fb, GAME)))

    def test_battle_independent_of_input_order(self):
        fa = derive_fighter("张三", GAME)
        fb = derive_fighter("李四", GAME)
        o1 = run_battle(fa, fb, GAME)
        o2 = run_battle(fb, fa, GAME)
        self.assertEqual(o1.events, o2.events)
        self.assertEqual(o1.winner_name, o2.winner_name)
        self.assertEqual(o1.rounds, o2.rounds)
        self.assertEqual(o1.seed, o2.seed)
        self.assertEqual(o1.damage[0], o2.damage[1])
        self.assertEqual(o1.damage[1], o2.damage[0])

    def test_mirror_battle(self):
        f = derive_fighter("Echo", GAME)
        o1 = _outcome_payload(run_battle(f, f, GAME))
        o2 = _outcome_payload(run_battle(f, f, GAME))
        self.assertEqual(o1, o2)

    def test_battle_outcome_is_valid(self):
        fa = derive_fighter("赵子龙", GAME)
        fb = derive_fighter("吕布", GAME)
        outcome = run_battle(fa, fb, GAME)
        if outcome.draw:
            self.assertIsNone(outcome.winner_name)
            self.assertEqual(outcome.winner_pos, -1)
        else:
            self.assertIn(outcome.winner_name, (fa.name, fb.name))
            self.assertIn(outcome.winner_pos, (0, 1))
        self.assertGreaterEqual(outcome.rounds, 1)
        self.assertLessEqual(outcome.rounds, GAME.battle.max_rounds)
        self.assertTrue(outcome.events)

    def test_battle_stable_across_processes(self):
        script = (
            "import json,sys;sys.path.insert(0,{root!r});"
            "from namefight.config import load_game_config;"
            "from namefight.fighter import derive_fighter;"
            "from namefight.battle import run_battle;"
            "g=load_game_config({cfg!r});"
            "o=run_battle(derive_fighter('Alice',g),derive_fighter('Bob',g),g);"
            "print(json.dumps({{'w':o.winner_name,'r':o.rounds,'e':o.events}},"
            "ensure_ascii=False,sort_keys=True))"
        ).format(root=str(REPO_ROOT), cfg=str(CONFIG_ROOT))
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        runs = [
            subprocess.run([sys.executable, "-c", script],
                           capture_output=True, env=env, check=True).stdout
            for _ in range(2)
        ]
        self.assertEqual(runs[0], runs[1])


if __name__ == "__main__":
    unittest.main()
