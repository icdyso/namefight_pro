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
from namefight.fighter import (derive_fighter, fighter_to_api,
                               personalized_effects, title_bonus_items)
from namefight.rng import DetRng

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
    return (f.normalized, f.digest, f.element_id,
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
        # 属性为固定基础值，差异主要来自称号加成；技能与称号组合应不同
        differing = (
            fa.skill_ids != fb.skill_ids
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
        self.assertEqual(zh["title"]["structure"], en["title"]["structure"])
        self.assertTrue(zh["title"]["name"])
        self.assertTrue(all(s["text"] for s in zh["skills"]))
        self.assertTrue(all(s["text"] for s in en["skills"]))

    def test_attributes_use_fixed_base(self):
        # 属性无随机：两名斗士的属性只可能因称号加成而不同
        for i in range(20):
            f = derive_fighter("base%02d" % i, GAME)
            for a in GAME.attributes:
                expected = a.base
                structure = next(s for s in GAME.title_structures
                                 if s.id == f.title_structure_id)
                delta = sum(d for attr, d in title_bonus_items(f.title_fields, structure, GAME)
                            if attr == a.id)
                self.assertEqual(f.attrs[a.id], max(1, expected + delta))

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
        link_cfg = GAME.skill_variable_link
        modes = {m for m, _ in link_cfg.mode_weights}
        linked = []
        modded = []
        dual = 0
        for i in range(60):
            f = derive_fighter("linker%02d" % i, GAME)
            for sdef, eff in personalized_effects(f, GAME):
                for link in eff.get("links", ()):
                    self.assertIn(link["mode"], modes)
                    self.assertIn(link["variable"], f.attrs)
                    self.assertIn(link["field"], eff)
                    vdef = next(v for v in link_cfg.variables
                                if v.id == link["variable"])
                    rate = link["rate"]
                    self.assertGreaterEqual(rate, vdef.rate_lo - 1e-9)
                    self.assertLessEqual(rate, vdef.rate_hi + 1e-9)
                if eff.get("links"):
                    linked.append((f, sdef, eff))
                    if len(eff["links"]) == 2:
                        dual += 1
                if "prefix" in eff or "suffix" in eff:
                    modded.append((f, eff))
        self.assertTrue(linked, "应有技能获得变量共鸣")
        self.assertGreater(dual, 0, "应采样到双变数技能")
        self.assertTrue(modded, "应有技能获得词缀")
        # 描述格式：公式括号紧跟对应数值（基数 + 变量式*合并系数）+ 尾句依赖
        f, sdef, eff = linked[0]
        api = fighter_to_api(f, GAME, load_locale(CONFIG_ROOT, "zh"))
        entry = next(s for s in api["skills"] if s["id"] == sdef.id)
        if eff.get("links"):
            self.assertIn("越", entry["text"])
            self.assertIn("%", entry["text"])
            self.assertIn("（", entry["text"])
            self.assertIn(" + ", entry["text"])
            self.assertIn("*", entry["text"])
            self.assertTrue(entry["text"].endswith("。"))
            self.assertLess(entry["text"].index("（"), entry["text"].index("。"))
            # 变数出现概率契约：整体约 25%/槽位（含双变数时两个括号）
            self.assertLessEqual(entry["text"].count("（"), 2)

    def test_variable_appearance_rate_near_quota(self):
        """变数出现概率契约：每个槽位 25%，技能级出现率应接近 1−0.75²。"""
        total = with_link = 0
        for i in range(150):
            f = derive_fighter("quota%03d" % i, GAME)
            for sdef, eff in personalized_effects(f, GAME):
                total += 1
                if eff.get("links"):
                    with_link += 1
        rate = with_link / total
        self.assertGreater(rate, 0.30, "变数出现率过低: %.3f" % rate)
        self.assertLess(rate, 0.58, "变数出现率过高: %.3f" % rate)

    def test_mastery_present_and_scales_chance(self):
        """每个技能实例都有熟练度：0~100，触发概率按各自区间缩放并截断。"""
        seen = 0
        for i in range(60):
            f = derive_fighter("mastery%02d" % i, GAME)
            api = fighter_to_api(f, GAME, load_locale(CONFIG_ROOT, "zh"))
            for sdef, eff in personalized_effects(f, GAME):
                entry = next(s for s in api["skills"] if s["id"] == sdef.id)
                self.assertIn("mastery", eff)
                self.assertTrue(0 <= eff["mastery"] <= 100)
                self.assertTrue(entry["mastery_text"])
                if sdef.mastery_on == "chance" and "chance" in eff:
                    self.assertGreaterEqual(eff["chance"], 0.02)
                    self.assertLessEqual(eff["chance"], 0.95)
                seen += 1
        self.assertGreater(seen, 100)

    def test_effect_link_appears_in_battles(self):
        found = 0
        for i in range(20):
            fa = derive_fighter("linkA%02d" % i, GAME)
            fb = derive_fighter("linkB%02d" % i, GAME)
            outcome = run_battle(fa, fb, GAME)
            for e in outcome.events:
                if e["template"] == "effect_link":
                    self.assertIn("field", e["params"])
                    self.assertIn("final", e["params"])
                    found += 1
                    break
        self.assertGreater(found, 0, "共鸣事件应在若干场对战中出现")

    def _collect_linked(self, count=25):
        """采样获得共鸣技能的斗士（link_calc/live 文本测试用）。"""
        linked = []
        for i in range(300):
            f = derive_fighter("live%03d" % i, GAME)
            for sdef, eff in personalized_effects(f, GAME):
                if eff.get("links"):
                    linked.append((f, sdef, eff))
            if len(linked) >= count:
                break
        return linked

    def test_live_text_marker_and_simple_mode(self):
        """live 文本有 1~2 个 LIVE_MARKER（与 link_calc 一一对应）；
        简易模式隐藏公式（尾句保留）。"""
        from namefight.fighter import LIVE_MARKER
        zh = load_locale(CONFIG_ROOT, "zh")
        linked = self._collect_linked(20)
        self.assertTrue(linked)
        for f, sdef, eff in linked:
            api = fighter_to_api(f, GAME, zh)
            entry = next(s for s in api["skills"] if s["id"] == sdef.id)
            links = eff.get("links")
            if not links:
                continue
            self.assertIn("live_text", entry)
            self.assertIn("live_text_simple", entry)
            self.assertIn("link_calc", entry)
            self.assertEqual(len(entry["link_calc"]), len(links))
            self.assertEqual(entry["live_text"].count(LIVE_MARKER), len(links))
            self.assertEqual(entry["live_text_simple"].count(LIVE_MARKER), len(links))
            # 简易模式：主句保留、公式（* 号）隐藏、尾句保留
            self.assertNotIn("*", entry["text_simple"])
            self.assertIn("*", entry["text"])
            self.assertTrue(entry["text_simple"].endswith("。"))
            # 公式中以属性 emoji 表示变量，且不出现「当前」措辞
            self.assertNotIn("当前", entry["text"])

    def test_link_calc_matches_engine_resonance(self):
        """前端实时公式（base + 变量式 × coeff + 上下限）与引擎
        resonance_coeff + apply_resonance 逐点一致（含双变数技能）。"""
        from namefight.battle import _live_value, _make_combatant
        from namefight.fighter import RESONANCE_SPECS, apply_resonance, resonance_coeff
        zh = load_locale(CONFIG_ROOT, "zh")
        linked = self._collect_linked(25)
        enemy = _make_combatant(derive_fighter("对照者", GAME), 1, GAME)
        checked = 0
        for f, sdef, eff in linked:
            links = eff.get("links")
            if not links:
                continue
            api = fighter_to_api(f, GAME, zh)
            entry = next(s for s in api["skills"] if s["id"] == sdef.id)
            actor = _make_combatant(f, 0, GAME)
            proc = dict(eff)
            for lc in entry["link_calc"]:
                field = lc["field"]
                # 引擎路径：按当前值计算系数并修正参数
                coeff = resonance_coeff(lambda vid: _live_value(actor, vid),
                                        lambda vid: _live_value(enemy, vid),
                                        next(l for l in links if l["field"] == field),
                                        GAME)
                proc = apply_resonance(proc, coeff, field)
                # 前端路径：base + 变量式 × coeff（+ 截断）
                if lc["mode"] in ("difference", "sum"):
                    own = _live_value(actor, lc["variable"])
                    other = _live_value(enemy, lc["against"])
                    expr = own - other if lc["mode"] == "difference" else own + other
                elif lc["mode"] == "enemy":
                    expr = _live_value(enemy, lc["variable"])
                else:
                    expr = _live_value(actor, lc["variable"])
                value = lc["base"] + expr * lc["coeff"]
                lo, hi = lc["clamp"]
                if lo is not None:
                    value = max(lo, value)
                if hi is not None:
                    value = min(hi, value)
                if lc["fmt"] == "turns":
                    value = max(1, int(round(value)))
                self.assertAlmostEqual(proc[field], value, places=9,
                                       msg="技能 %s 字段 %s 实时公式与引擎不一致"
                                           % (sdef.id, field))
                checked += 1
        self.assertGreater(checked, 15)

    def test_snapshot_carriers_live_attributes(self):
        """快照含 crit/dodge/gauge_gain：前端实时技能公式与逐刻行动槽
        动画所需的全部数据可直接取用。"""
        fa = derive_fighter("Alice", GAME)
        fb = derive_fighter("Bob", GAME)
        outcome = run_battle(fa, fb, GAME)
        for e in outcome.events:
            for side in ("a", "b"):
                snap = e["state"][side]
                for key in ("hp", "max_hp", "atk", "def", "spd",
                            "crit", "dodge", "gauge", "gauge_gain"):
                    self.assertIn(key, snap)

    def test_whiteboard_baseline_and_scale(self):
        """数值缩放契约：白板基准 100（hp/atk/def/spd 基础值均为 100），
        战斗常数按同比例折算（atk_factor / defense_factor / gauge_threshold）。"""
        for a in GAME.attributes:
            if a.id in ("hp", "atk", "def", "spd"):
                self.assertEqual(a.base, 100, "白板属性 %s 基准应为 100" % a.id)
        f = derive_fighter("白板测试", GAME)
        for a in GAME.attributes:
            expect = a.base + sum(
                d for aid, d in title_bonus_items(
                    f.title_fields,
                    next(s for s in GAME.title_structures
                         if s.id == f.title_structure_id), GAME)
                if aid == a.id)
            self.assertEqual(f.attrs[a.id], max(1, expect))

    def test_snapshots_off_matches_full_run(self):
        """极速模式（无快照）与完整模式的胜负、tick 与事件序列完全一致。"""
        fa = derive_fighter("Alice", GAME)
        fb = derive_fighter("Bob", GAME)
        full = run_battle(fa, fb, GAME, snapshots=True)
        fast = run_battle(fa, fb, GAME, snapshots=False)
        self.assertEqual(full.winner_name, fast.winner_name)
        self.assertEqual(full.ticks, fast.ticks)
        self.assertEqual(full.damage, fast.damage)
        stripped = [{k: v for k, v in e.items() if k != "state"} for e in full.events]
        self.assertEqual(stripped, fast.events)
        for e in fast.events:
            self.assertNotIn("state", e)


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

    def test_faster_fighter_acts_first(self):
        # 属性固定后，速度差异来自称号加成（苍穹/雷霆 +1、退役 -1 等）
        pair = None
        for i in range(80):
            fa = derive_fighter("paceA%02d" % i, GAME)
            fb = derive_fighter("paceB%02d" % i, GAME)
            if fa.attrs["spd"] != fb.attrs["spd"]:
                pair = (fa, fb)
                break
        if pair is None:
            self.skipTest("未采样到速度不同的名字对")
        fa, fb = pair
        fast = fa if fa.attrs["spd"] > fb.attrs["spd"] else fb
        outcome = run_battle(fa, fb, GAME)
        for e in outcome.events:
            if e["template"] in ("attack_hit", "attack_miss"):
                self.assertEqual(e["params"]["a"], fast.name,
                                 "速度更高者应先行动")
                break

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
