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
from namefight.fighter import (derive_fighter, fighter_to_api, link_bonus,
                               personalized_effects, title_bonus_items)

CONFIG_ROOT = REPO_ROOT / "config"
GAME = load_game_config(CONFIG_ROOT)


def _outcome_payload(outcome):
    return {
        "winner": outcome.winner_name,
        "draw": outcome.draw,
        "ticks": outcome.ticks,
        "events": outcome.events,
        "damage": outcome.damage,
    }


def _game_data(f):
    """参与确定性契约的派生数据（name 仅为展示输入，不计入）。"""
    return (f.normalized, f.digest, f.rarity_id, f.element_id,
            tuple(sorted(f.attrs.items())), f.skill_ids,
            f.title_structure_id, tuple(sorted(f.title_fields.items())), f.power)


def _swap_state(event):
    """把事件快照的 a/b 键互换（用于输入顺序无关性比较）。"""
    e = dict(event)
    if "state" in e:
        st = e["state"]
        e["state"] = {"a": st.get("b"), "b": st.get("a")}
    return e


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
            or fa.title_structure_id != fb.title_structure_id
            or fa.title_fields != fb.title_fields or fa.element_id != fb.element_id
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
        self.assertEqual(zh["title"]["structure"], en["title"]["structure"])
        self.assertTrue(zh["title"]["name"])

    def test_skill_personalization_deterministic(self):
        fa = derive_fighter("Alice", GAME)
        self.assertEqual(personalized_effects(fa, GAME), personalized_effects(fa, GAME))

    def test_skill_personalization_varies_by_name(self):
        from collections import defaultdict
        values = defaultdict(set)
        for i in range(30):
            f = derive_fighter("fighter%02d" % i, GAME)
            for sdef, eff in personalized_effects(f, GAME):
                values[sdef.id].add((eff.get("chance"), eff.get("value"), eff.get("damage")))
        varied = [sid for sid, vs in values.items() if len(vs) > 1]
        self.assertTrue(varied, "技能个性化参数应随名字（MD5）变化")

    def test_title_composition(self):
        f = derive_fighter("TitleTest", GAME)
        loc = load_locale(CONFIG_ROOT, "zh")
        api = fighter_to_api(f, GAME, loc)
        name = api["title"]["name"]
        self.assertTrue(name)
        self.assertTrue(api["title"]["description"].endswith("。"))
        pool_key = {"prefix": "prefixes", "core": "cores", "core2": "cores", "suffix": "suffixes"}
        for fname, fid in f.title_fields.items():
            expected = loc.titles[pool_key[fname]][fid]["name"]
            self.assertIn(expected, name)

    def test_title_bonuses_applied(self):
        for i in range(10):
            f = derive_fighter("bonus%02d" % i, GAME)
            structure = next(s for s in GAME.title_structures
                             if s.id == f.title_structure_id)
            deltas = {}
            for attr_id, delta in title_bonus_items(f.title_fields, structure, GAME):
                deltas[attr_id] = deltas.get(attr_id, 0) + delta
            api = fighter_to_api(f, GAME, load_locale(CONFIG_ROOT, "zh"))
            # API 展示的加成与配置一致
            self.assertEqual({b["attr"]: b["value"] for b in api["title"]["bonuses"]},
                             {k: v for k, v in deltas.items() if v != 0})
            # 派生属性不小于 1
            for attr_id, value in f.attrs.items():
                self.assertGreaterEqual(value, 1)

    def test_variable_link_varies_and_matches_display(self):
        linked_names = []
        for i in range(30):
            f = derive_fighter("linker%02d" % i, GAME)
            for sdef, eff in personalized_effects(f, GAME):
                if "link" in eff:
                    self.assertIn(eff["link"]["variable"], f.attrs)
                    rate = eff["link"]["rate"]
                    vdef = next(v for v in GAME.skill_variable_link.variables
                                if v.id == eff["link"]["variable"])
                    self.assertGreaterEqual(rate, vdef.rate_lo - 1e-9)
                    self.assertLessEqual(rate, vdef.rate_hi + 1e-9)
                    linked_names.append((f, sdef, eff))
        self.assertTrue(linked_names, "应有技能获得变量共鸣")
        # 展示的附伤 = 引擎使用的附伤（同一确定性函数）
        f, sdef, eff = linked_names[0]
        self.assertEqual(linked_names[0][2].get("link") and link_bonus(f, eff),
                         link_bonus(f, eff))
        api = fighter_to_api(f, GAME, load_locale(CONFIG_ROOT, "zh"))
        entry = next(s for s in api["skills"] if s["id"] == sdef.id)
        self.assertEqual(entry["link"]["bonus"], link_bonus(f, eff))

    def test_effect_link_appears_in_battles(self):
        found = 0
        for i in range(20):
            fa = derive_fighter("linkA%02d" % i, GAME)
            fb = derive_fighter("linkB%02d" % i, GAME)
            outcome = run_battle(fa, fb, GAME)
            if any(e["template"] == "effect_link" for e in outcome.events):
                found += 1
        self.assertGreater(found, 0, "共鸣事件应在若干场对战中出现")


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
        # 快照按输入位置 a/b 记录，交换输入顺序需把 a/b 互换后再比较
        self.assertEqual(o1.events, [_swap_state(e) for e in o2.events])
        self.assertEqual(o1.winner_name, o2.winner_name)
        self.assertEqual(o1.ticks, o2.ticks)
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
        self.assertGreaterEqual(outcome.ticks, 1)
        self.assertLessEqual(outcome.ticks, GAME.battle.max_ticks)
        self.assertTrue(outcome.events)

    def test_battle_events_carry_state_snapshots(self):
        fa = derive_fighter("Alice", GAME)
        fb = derive_fighter("Bob", GAME)
        outcome = run_battle(fa, fb, GAME)
        for e in outcome.events:
            self.assertIn("state", e)
            for side in ("a", "b"):
                snap = e["state"][side]
                self.assertIn("hp", snap)
                self.assertIn("max_hp", snap)
                self.assertIn("atk", snap)
                self.assertIn("gauge", snap)
                self.assertIn("buffs", snap)
                self.assertGreaterEqual(snap["hp"], 0)
        # 首个事件应为满血初始状态
        first = outcome.events[0]["state"]
        self.assertEqual(first["a"]["hp"], first["a"]["max_hp"])
        self.assertEqual(first["b"]["hp"], first["b"]["max_hp"])

    def test_faster_fighter_acts_no_later(self):
        # 速度决定行动频率：更快的一方首次行动不应晚于更慢的一方
        lo = derive_fighter("Slowpoke", GAME)
        while lo.attrs["spd"] > 8:
            lo = derive_fighter(lo.name + "x", GAME)
        hi = derive_fighter("Quickstep", GAME)
        while hi.attrs["spd"] < 12:
            hi = derive_fighter(hi.name + "x", GAME)
        outcome = run_battle(lo, hi, GAME)
        first_actions = {}
        for e in outcome.events:
            if e["template"] in ("attack_hit", "attack_miss"):
                actor = e["params"]["a"]
                if actor not in first_actions:
                    first_actions[actor] = e["tick"]
                if len(first_actions) == 2:
                    break
        if hi.name in first_actions and lo.name in first_actions:
            self.assertLessEqual(first_actions[hi.name], first_actions[lo.name])

    def test_battle_stable_across_processes(self):
        script = (
            "import json,sys;sys.path.insert(0,{root!r});"
            "from namefight.config import load_game_config;"
            "from namefight.fighter import derive_fighter;"
            "from namefight.battle import run_battle;"
            "g=load_game_config({cfg!r});"
            "o=run_battle(derive_fighter('Alice',g),derive_fighter('Bob',g),g);"
            "print(json.dumps({{'w':o.winner_name,'t':o.ticks,'e':o.events}},"
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
