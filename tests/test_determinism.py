"""确定性核心不变量测试（最高优先级，见 AGENTS.md 2.1）。

这些测试失败意味着「同名同命」契约被破坏，属于最高优先级事故。
"""
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from namefight.battle import (_Combatant, _compute_damage, _make_combatant,
                              _snapshot, battle_to_api, run_battle)
from namefight.config import load_game_config
from namefight.fighter import (_format_formula_number, _param_spec,
                               _walk_links, apply_resonance,
                               compose_title_name, derive_fighter,
                               fighter_to_api, personalized_effects,
                               resonance_coeff, title_bonus_items)
from namefight.rng import DetRng
from namefight.text import format_num, format_pct

CONFIG_ROOT = REPO_ROOT / "config"
GAME = load_game_config(CONFIG_ROOT)


def _skill_links(pgraph):
    """技能图的全部共鸣链接（按节点数组顺序展平，与 link_calc 一致）。"""
    return _walk_links(pgraph)


def _graph_param(pgraph, name, default=None):
    """技能图内首个该名参数的值。"""
    for node in pgraph.get("nodes", ()):
        if name in node.get("params", {}):
            return node["params"][name]
    return default


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
    return (f.normalized, f.digest,
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
        # 大小写折叠跟随 system.json 的 case_sensitive 配置（v1.1.0 用户设为 true）
        if GAME.system.name_case_sensitive:
            self.assertNotEqual(_game_data(derive_fighter("Alice", GAME)),
                                _game_data(derive_fighter("alice", GAME)))
        else:
            self.assertEqual(_game_data(derive_fighter("Alice", GAME)),
                             _game_data(derive_fighter("alice", GAME)))
        self.assertNotEqual(_game_data(derive_fighter("Alice", GAME)),
                            _game_data(derive_fighter("Bob", GAME)))

    def test_different_names_differ(self):
        fa = derive_fighter("张三", GAME)
        fb = derive_fighter("李四", GAME)
        self.assertNotEqual(fa.digest, fb.digest)
        # 属性投掷（v1.1.0 三角形分布）+ 技能与称号组合应不同
        differing = (
            fa.skill_ids != fb.skill_ids
            or fa.title_structure_id != fb.title_structure_id
            or fa.title_fields != fb.title_fields
            or fa.attrs != fb.attrs
        )
        self.assertTrue(differing, "两个不同名字的派生结果不应完全相同")

    def test_attributes_triangular_rolled(self):
        """属性三角形分布投掷契约（v1.1.0，两均匀数取均值）：[min, max] 内、
        同名一致、不同名显著变化且集中于区间中点附近、端点无截断堆积；
        非百分比属性为整数。"""
        base = derive_fighter("attrRoll", GAME)
        self.assertEqual(derive_fighter("attrRoll", GAME).attrs, base.attrs)
        names = ["tri%03d" % i for i in range(60)]
        for a in GAME.attributes:
            values = set()
            inside = 0
            mid = (a.min + a.max) / 2.0
            half = (a.max - a.min) / 4.0  # 中段带宽 = 区间宽 / 2（中点 ±宽/4）
            at_bound = 0
            for n in names:
                f = derive_fighter(n, GAME)
                structure = next(s for s in GAME.title_structures
                                 if s.id == f.title_structure_id)
                delta = sum(d for aid, d in title_bonus_items(
                    f.title_fields, structure, GAME) if aid == a.id)
                # 投掷值 = 面板值 − 称号加成（称号可把面板推过区间，属设计预期）
                rolled = f.attrs[a.id] - delta if f.attrs[a.id] > 1.0 else f.attrs[a.id]
                self.assertGreaterEqual(rolled, a.min - 1e-9)
                self.assertLessEqual(rolled, a.max + 1e-9)
                if rolled == a.min or rolled == a.max:
                    at_bound += 1
                values.add(round(rolled, 4))
                if abs(rolled - mid) <= half:
                    inside += 1
                if a.format != "percent":
                    self.assertIsInstance(f.attrs[a.id], int,
                                          "非百分比属性 %s 应为整数" % a.id)
            self.assertGreater(len(values), 8,
                               "属性 %s 应随名字显著变化" % a.id)
            self.assertGreater(inside / len(names), 0.55,
                               "属性 %s 应集中于区间中点附近（实测 %.2f）"
                               % (a.id, inside / len(names)))
            self.assertLess(at_bound / len(names), 0.05,
                            "属性 %s 端点占比过高，疑似截断堆积（实测 %.2f）"
                            % (a.id, at_bound / len(names)))
        # 称号加成叠加在投掷值上（不消耗随机数），下限保护不低于 1
        for i in range(10):
            f = derive_fighter("bonus%02d" % i, GAME)
            for aid, value in f.attrs.items():
                self.assertGreaterEqual(value, 1.0)

    def test_skill_personalization_deterministic(self):
        fa = derive_fighter("Alice", GAME)
        self.assertEqual(personalized_effects(fa, GAME), personalized_effects(fa, GAME))

    def test_skill_personalization_varies_by_name(self):
        from collections import defaultdict
        values = defaultdict(set)
        for i in range(30):
            f = derive_fighter("fighter%02d" % i, GAME)
            for sdef, pg in personalized_effects(f, GAME):
                snapshot = []
                for node in pg["nodes"]:
                    base_of = {str(l.get("param")): l.get("base")
                               for l in node.get("links") or ()}
                    for key in ("chance", "value", "damage"):
                        if key in node["params"]:
                            v = node["params"][key]
                            if isinstance(v, str):          # 共鸣生成的表达式
                                v = base_of.get(key, 0.0)
                            snapshot.append(round(float(v), 6))
                values[sdef.id].add(tuple(snapshot))
        varied = [sid for sid, vs in values.items() if len(vs) > 1]
        self.assertTrue(varied, "技能个性化参数应随名字（MD5）变化")

    def test_title_composition(self):
        f = derive_fighter("TitleTest", GAME)
        api = fighter_to_api(f, GAME)
        name = api["title"]["name"]
        self.assertTrue(name)
        self.assertTrue(api["title"]["description"].endswith("。"))
        pool_key = {"prefix": "prefix", "core": "core", "core2": "core", "suffix": "suffix"}
        for fname, fid in f.title_fields.items():
            expected = GAME.title_field(pool_key[fname], fid).name
            self.assertIn(expected, name)

    def test_title_bonuses_applied(self):
        for i in range(10):
            f = derive_fighter("bonus%02d" % i, GAME)
            structure = next(s for s in GAME.title_structures
                             if s.id == f.title_structure_id)
            deltas = {}
            for attr_id, delta in title_bonus_items(f.title_fields, structure, GAME):
                deltas[attr_id] = deltas.get(attr_id, 0) + delta
            api = fighter_to_api(f, GAME)
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
            for sdef, pg in personalized_effects(f, GAME):
                for node, link in _skill_links(pg):
                    self.assertIn(link["mode"], modes)
                    self.assertIn(link["variable"], f.attrs)
                    self.assertIn(link["param"], node["params"])
                    vdef = next(v for v in link_cfg.variables
                                if v.id == link["variable"])
                    rate = link["rate"]
                    self.assertGreaterEqual(rate, vdef.rate_lo - 1e-9)
                    self.assertLessEqual(rate, vdef.rate_hi + 1e-9)
                links = _skill_links(pg)
                if links:
                    linked.append((f, sdef, pg))
                    if len(links) == 2:
                        dual += 1
                if "prefix" in pg or "suffix" in pg:
                    modded.append((f, pg))
        self.assertTrue(linked, "应有技能获得变量共鸣")
        self.assertGreater(dual, 0, "应采样到双变数技能")
        self.assertTrue(modded, "应有技能获得词缀")
        # 描述格式：公式括号紧跟对应数值（基数 + 变量式*合并系数）+ 尾句依赖
        f, sdef, eff = linked[0]
        api = fighter_to_api(f, GAME)
        entry = next(s for s in api["skills"] if s["id"] == sdef.id)
        if _skill_links(eff):
            self.assertIn("越", entry["text"])
            self.assertIn("%", entry["text"])
            self.assertIn("（", entry["text"])
            self.assertIn(" + ", entry["text"])
            self.assertIn("*", entry["text"])
            self.assertTrue(entry["text"].endswith("。"))
            self.assertLess(entry["text"].index("（"), entry["text"].index("。"))
            # 变数出现概率契约：整体约 25%/槽位（含双变数时两个公式括号；
            # 只数公式括号——以数字/小数点开头的全角括号，「（每场战斗一次）」
            # 等固定文案括号不计）
            self.assertLessEqual(len(re.findall("（[\\d.]", entry["text"])), 2)

    @staticmethod
    def _fill_live(tmpl, calcs, own, enemy):
        """复刻前端 fillLiveText 的取值逻辑（按占位符携带的槽位序号取
        link_calc 对应条目），用于校验后端占位符协议与前后端一致性。"""
        def repl(m):
            lc = calcs[int(m.group(1))]
            if lc["mode"] in ("difference", "sum"):
                other = enemy[lc["against"]]
                expr = own[lc["variable"]] - other if lc["mode"] == "difference" \
                    else own[lc["variable"]] + other
            elif lc["mode"] == "enemy":
                expr = enemy[lc["variable"]]
            else:
                expr = own[lc["variable"]]
            v = lc["base"] + expr * lc["coeff"]
            lo, hi = lc["clamp"]
            if lo is not None:
                v = max(lo, v)
            if hi is not None:
                v = min(hi, v)
            if lc["fmt"] == "turns":
                return str(max(1, int(round(v))))
            if lc["fmt"] == "num":
                return format_num(v)
            return format_pct(v)
        return re.sub("\x01(\\d+)", repl, tmpl)

    def test_live_markers_carry_slot_index(self):
        """live 占位符协议（v1.2.0）：占位符 = \\x01 + 槽位序号，序号与
        link_calc 下标一致；按序号填充（而非按出现位置）必须还原出与
        卡牌估算文本完全一致的结果——模板参数顺序与槽位顺序不一致
        （如壁垒的门槛/效果值）时按位置填充会交叉错位。"""
        checked = 0
        enemy_base = {a.id: a.base for a in GAME.attributes}
        for i in range(80):
            f = derive_fighter("live%02d" % i, GAME)
            api = fighter_to_api(f, GAME)
            for entry in api["skills"]:
                calcs = entry.get("link_calc")
                if not calcs:
                    continue
                markers = re.findall("\x01(\\d+)", entry["live_text"])
                self.assertEqual(sorted(int(x) for x in markers),
                                 list(range(len(calcs))),
                                 "占位符序号必须与 link_calc 下标一一对应")
                filled = self._fill_live(entry["live_text"], calcs,
                                         f.attrs, enemy_base)
                self.assertEqual(filled, entry["text"],
                                 "按序号填充的实时文本必须等于卡牌估算文本")
                filled_simple = self._fill_live(entry["live_text_simple"],
                                                calcs, f.attrs, enemy_base)
                self.assertEqual(filled_simple, entry["text_simple"])
                checked += 1
        self.assertGreater(checked, 0, "应采样到带共鸣变数的技能")

    def test_formula_small_values_keep_significant_digits(self):
        """共鸣公式数值显示（v1.2.0）：>=0.1 保留两位小数；<0.1 改百分数
        形式并保留两位有效数字，不允许出现被两位小数吞没的 0.00。"""
        self.assertEqual(_format_formula_number(355.61, True), "355.61")
        self.assertEqual(_format_formula_number(6.31, True), "6.31")
        self.assertEqual(_format_formula_number(0.42, True), "0.42")
        self.assertEqual(_format_formula_number(0.05, True), "5.00%")
        self.assertEqual(_format_formula_number(0.00209, True), "0.21%")
        self.assertEqual(_format_formula_number(0.42, False), "0.42%")
        self.assertEqual(_format_formula_number(0.0042, False), "0.0042%")
        self.assertEqual(_format_formula_number(0.099, False), "0.099%")

    def test_variable_appearance_rate_near_quota(self):
        """变数出现概率契约：每个槽位 25%，技能级出现率应接近 1−0.75²。"""
        total = with_link = 0
        for i in range(150):
            f = derive_fighter("quota%03d" % i, GAME)
            for sdef, pg in personalized_effects(f, GAME):
                total += 1
                if _skill_links(pg):
                    with_link += 1
        rate = with_link / total
        self.assertGreater(rate, 0.30, "变数出现率过低: %.3f" % rate)
        self.assertLess(rate, 0.58, "变数出现率过高: %.3f" % rate)

    def test_mastery_present_and_scales_chance(self):
        """每个技能实例都有熟练度：0~100，触发概率按各自区间缩放并截断；
        熟练度文案直接给出最终触发率（v0.9.1，无倍率写法，永不超过 100%）。"""
        seen = 0
        for i in range(60):
            f = derive_fighter("mastery%02d" % i, GAME)
            api = fighter_to_api(f, GAME)
            for sdef, pg in personalized_effects(f, GAME):
                entry = next(s for s in api["skills"] if s["id"] == sdef.id)
                self.assertIn("mastery", pg)
                self.assertTrue(0 <= pg["mastery"] <= 100)
                self.assertTrue(entry["mastery_text"])
                if "chance" in sdef.mastery_on:
                    chance = _graph_param(pg, "chance")
                    if chance is not None and not isinstance(chance, str):
                        self.assertGreaterEqual(chance, 0.02)
                        self.assertLessEqual(chance, 0.95)
                raw_chance = _graph_param(pg, "chance")
                if "chance" in sdef.mastery_on and not isinstance(raw_chance, str):
                    # 触发率类文案为最终概率（含百分号）；表达式 chance 走倍率口径
                    self.assertIn("%", entry["mastery_text"])
                    self.assertNotIn("×", entry["mastery_text"])
                seen += 1
        self.assertGreater(seen, 100)

    def test_mastery_triangular_distribution(self):
        """熟练度分布契约（v1.1.0，三角形分布）：集中于 50--
        均值靠近 50，中段占比显著高于均匀分布，端点无截断堆积。"""
        values = []
        for i in range(120):
            f = derive_fighter("熟练度分布%03d" % i, GAME)
            for sdef, pg in personalized_effects(f, GAME):
                values.append(pg["mastery"])
        self.assertGreater(len(values), 200)
        mean = sum(values) / len(values)
        self.assertGreater(mean, 42.0, "熟练度均值应靠近 50（实测 %.1f）" % mean)
        self.assertLess(mean, 58.0, "熟练度均值应靠近 50（实测 %.1f）" % mean)
        mid = sum(1 for v in values if 25 <= v <= 75) / len(values)
        self.assertGreater(mid, 0.65,
                           "熟练度中段占比应显著高于均匀分布（实测 %.2f）" % mid)
        edge = sum(1 for v in values if v <= 2 or v >= 98) / len(values)
        self.assertLess(edge, 0.02,
                        "熟练度极端值占比过高，疑似截断堆积（实测 %.3f）" % edge)

    def test_trigger_chances_distinct_and_capped(self):
        """触发率契约（v0.9.1）：各技能基础触发率按强度互不相同；
        个性化后的触发率始终不超过 100%（基配置触发率取自技能图各节点的
        chance 参数）。"""
        chances = {}
        for s in GAME.skills:
            for node in s.effect.get("nodes", ()):
                params = node.get("params", {})
                if "chance" in params and not isinstance(params["chance"], str):
                    # 表达式 chance（如不屈衰减概率）不参与该数值契约
                    self.assertGreater(params["chance"], 0)
                    self.assertLessEqual(params["chance"], 0.95)
                    if s.id in chances:
                        # 同一技能的多个 chance（如乘胜两条链）应相同
                        self.assertEqual(chances[s.id],
                                         round(float(params["chance"]), 6))
                    chances[s.id] = round(float(params["chance"]), 6)
        self.assertGreater(len(chances), 10)
        self.assertEqual(len(chances), len(set(chances.values())),
                         "技能基础触发率应互不相同: %s" % sorted(chances.items()))
        for i in range(40):
            f = derive_fighter("cap%02d" % i, GAME)
            for sdef, pg in personalized_effects(f, GAME):
                chance = _graph_param(pg, "chance")
                if chance is not None and not isinstance(chance, str):
                    self.assertLessEqual(chance, 0.95)

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
            for sdef, pg in personalized_effects(f, GAME):
                if _skill_links(pg):
                    linked.append((f, sdef, pg))
            if len(linked) >= count:
                break
        return linked

    def test_live_text_marker_and_simple_mode(self):
        """live 文本有 1~2 个 LIVE_MARKER（与 link_calc 一一对应）；
        简易模式隐藏公式（尾句保留）。"""
        from namefight.fighter import LIVE_MARKER
        linked = self._collect_linked(20)
        self.assertTrue(linked)
        for f, sdef, eff in linked:
            api = fighter_to_api(f, GAME)
            entry = next(s for s in api["skills"] if s["id"] == sdef.id)
            links = _skill_links(eff)
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
        resonance_coeff + apply_resonance 逐点一致（v0.10.0 起均为引擎真实值；
        v2.0.0 共鸣挂点为技能图节点参数，link_calc 与节点链接按序一一对应）。"""
        from namefight.battle import _live_value
        linked = self._collect_linked(25)
        enemy = _make_combatant(derive_fighter("对照者", GAME), 1, GAME)
        checked = 0
        for f, sdef, pg in linked:
            api = fighter_to_api(f, GAME)
            entry = next(s for s in api["skills"] if s["id"] == sdef.id)
            actor = _make_combatant(f, 0, GAME)
            for (node, link), lc in zip(_skill_links(pg), entry["link_calc"]):
                param = lc["field"]
                self.assertEqual(param, link["param"])
                spec = _param_spec(node, param, GAME)
                # 引擎路径（v3.2.0 共鸣=表达式）：求值生成的表达式并按规格钳制
                from namefight import expr as _expr
                from namefight.battle import _clamp_res
                value0 = _expr.eval_expr(node["params"][param], {
                    "self." + vid: _live_value(actor, vid, GAME) for vid in
                    ("hp", "atk", "def", "spd", "crit", "dodge")
                } | {
                    "enemy." + vid: _live_value(enemy, vid, GAME) for vid in
                    ("hp", "atk", "def", "spd", "crit", "dodge")
                })
                proc = {param: _clamp_res(value0, spec)}
                # 前端路径（同一真实值口径）：base + 变量式 × coeff（+ 上下限）
                if lc["mode"] in ("difference", "sum"):
                    own = _live_value(actor, lc["variable"], GAME)
                    other = _live_value(enemy, lc["against"], GAME)
                    expr = own - other if lc["mode"] == "difference" else own + other
                elif lc["mode"] == "enemy":
                    expr = _live_value(enemy, lc["variable"], GAME)
                else:
                    expr = _live_value(actor, lc["variable"], GAME)
                value = lc["base"] + expr * lc["coeff"]
                lo, hi = lc["clamp"]
                if lo is not None:
                    value = max(lo, value)
                if hi is not None:
                    value = min(hi, value)
                if lc["fmt"] == "turns":
                    value = max(1, int(round(value)))
                    self.assertEqual(int(proc[param]), value)
                else:
                    self.assertAlmostEqual(proc[param], value, places=5,
                                           msg="技能 %s 参数 %s 实时公式与引擎不一致"
                                               % (sdef.id, param))
                checked += 1
        self.assertGreater(checked, 15)

    def test_snapshot_carriers_live_attributes(self):
        """快照含 crit/dodge/gauge/gauge_gain 等全部实时数据：
        前端实时技能公式与逐刻行动槽动画所需的全部数据可直接取用。"""
        fa = derive_fighter("Alice", GAME)
        fb = derive_fighter("Bob", GAME)
        outcome = run_battle(fa, fb, GAME)
        for e in outcome.events:
            for side in ("a", "b"):
                snap = e["state"][side]
                for key in ("hp", "max_hp", "atk", "def", "spd",
                            "crit", "dodge", "gauge", "gauge_gain",
                            "gauge_pct", "gauge_pct_gain", "gauge_threshold"):
                    self.assertIn(key, snap)

    def test_real_value_display(self):
        """量纲契约（v1.0.0/v1.2.1）：引擎与显示统一为真实值。攻击 [1000, 2000]
        （×100 整数量纲）、防御 [500, 1000]；生命/暴击区间为 v1.1.0 用户手动
        调参后的现值，速度自 v1.2.1 起同为 ×100 量纲（行动槽阈值 10000 配套）；
        API 的 value 即引擎原始值（不再换算白板单位）。"""
        expect = {"hp": (20000, 10000, 30000),
                  "atk": (1500, 1000, 2000),
                  "def": (750, 500, 1000),
                  "spd": (1000, 500, 1500),
                  "crit": (15, 5, 30),
                  "dodge": (10, 5, 15)}
        for a in GAME.attributes:
            base, lo, hi = expect[a.id]
            self.assertEqual((a.base, a.min, a.max), (base, lo, hi),
                             "属性 %s 量纲不符" % a.id)
        f = derive_fighter("真实值测试", GAME)
        api = fighter_to_api(f, GAME)
        by_id = {a["id"]: a for a in api["attributes"]}
        for a in GAME.attributes:
            entry = by_id[a.id]
            self.assertAlmostEqual(entry["value"], f.attrs[a.id], places=3)
            self.assertAlmostEqual(entry["min"], a.min, places=3)
            self.assertAlmostEqual(entry["max"], a.max, places=3)

    def test_snapshot_uses_real_values(self):
        """快照属性为引擎真实值：开局快照等于卡牌显示值，
        gauge 为原始行动槽值、gauge_gain = 每刻推进速度值。"""
        fa = derive_fighter("显示甲", GAME)
        fb = derive_fighter("显示乙", GAME)
        outcome = run_battle(fa, fb, GAME)
        first = outcome.events[0]["state"]
        for side, f in (("a", fa), ("b", fb)):
            snap = first[side]
            self.assertAlmostEqual(snap["max_hp"], f.attrs["hp"], places=1)
            self.assertAlmostEqual(snap["atk"], f.attrs["atk"], places=1)
            self.assertAlmostEqual(snap["def"], f.attrs["def"], places=1)
            self.assertAlmostEqual(snap["spd"], f.attrs["spd"], places=1)
            self.assertAlmostEqual(snap["gauge_gain"], f.attrs["spd"], places=1)
            self.assertAlmostEqual(snap["crit"], f.attrs["crit"], places=1)
            self.assertAlmostEqual(
                snap["gauge_threshold"], GAME.battle.gauge_threshold, places=1)

    def test_defense_reciprocal_reduction(self):
        """防御契约（v0.10.0/v1.0.0）：倒数百分比免伤 dmg = raw × (1 − DEF/(DEF+K))，
        不再直接扣减；穿透按比例抵消灭伤率（pen=1 时无视全部防御）。"""
        bc = GAME.battle
        atk_v, def_v = 1500.0, 750.0

        def mk(atk, dfn):
            c = _make_combatant(derive_fighter("防御测试", GAME), 0, GAME)
            c.atk = atk
            c.defense = dfn
            return c

        actor, enemy = mk(atk_v, def_v), mk(atk_v, def_v)
        rng = DetRng(7)
        dmg = _compute_damage(actor, enemy, 1.0, False, GAME, rng)
        rng2 = DetRng(7)
        variance = rng2.next_triangular(bc.variance_lo, bc.variance_hi)
        reduction = def_v / (def_v + bc.defense_constant)
        self.assertAlmostEqual(dmg, atk_v * variance * (1.0 - reduction), places=6)
        # 零防御：无免伤
        enemy.defense = 0.0
        dmg0 = _compute_damage(actor, enemy, 1.0, False, GAME, DetRng(7))
        reduction0 = 0.0 / (0.0 + bc.defense_constant)
        self.assertAlmostEqual(dmg0, atk_v * variance * (1.0 - reduction0), places=6)
        # 全穿透：免伤率被完全抵消
        dmg_pen = _compute_damage(actor, mk(atk_v, 1000.0), 1.0, False, GAME,
                                  DetRng(7), pen=1.0)
        self.assertAlmostEqual(dmg_pen, atk_v * variance, places=6)

    def test_integer_results_policy(self):
        """取整契约（v0.10.0）：多步浮点计算只在最终应用时取整一次--
        战报中的伤害/治疗/消耗/剩余生命均为整数字符串；血量全程保持整数。"""
        fa = derive_fighter("整数甲", GAME)
        fb = derive_fighter("整数乙", GAME)
        outcome = run_battle(fa, fb, GAME)
        # 仅检查恒为整数语义的参数键（value/spd 等键在不同事件中可为百分数）
        numeric_keys = {"damage", "heal", "cost", "hp"}
        for e in outcome.events:
            for key, v in e["params"].items():
                if key in numeric_keys:
                    self.assertIsInstance(v, str)
                    self.assertNotIn(".", v,
                                     "战报数值 %s=%r 应为整数字符串" % (key, v))
            for side in ("a", "b"):
                hp = e["state"][side]["hp"]
                self.assertAlmostEqual(hp, round(hp), places=6,
                                       msg="生命应保持整数值（实测 %r）" % hp)

    def test_blood_pact_buff_shows_accumulated_atk(self):
        """血契标记契约（v0.10.0）：只显示累计转化的攻击量（v3.0.0 起
        存于通用状态容器 blood_pact.total）。"""
        from namefight.statuses import ensure
        fa = derive_fighter("血契甲", GAME)
        fb = derive_fighter("血契乙", GAME)
        ca = _make_combatant(fa, 0, GAME)
        cb = _make_combatant(fb, 1, GAME)
        snap = _snapshot([ca, cb], GAME.battle.gauge_threshold, 0, GAME)
        self.assertEqual([b for b in snap["a"]["buffs"] if b["id"] == "blood_pact"], [],
                         "未转化过攻击时不显示血契标记")
        ensure(ca, "blood_pact")["total"] = 237.6
        snap = _snapshot([ca, cb], GAME.battle.gauge_threshold, 0, GAME)
        pact = [b for b in snap["a"]["buffs"] if b["id"] == "blood_pact"]
        self.assertEqual(len(pact), 1)
        self.assertEqual(pact[0]["params"]["total"], "238")

    def test_battle_log_rich_segments(self):
        """富文本契约（v0.10.0）：每条战报带 rich 段，各段拼接后与纯文本
        完全一致；阵营名/技能/伤害/治疗段在若干场对局中均会出现。"""
        kinds = set()
        battles = 0
        for i in range(10):
            fa = derive_fighter("富文本甲%02d" % i, GAME)
            fb = derive_fighter("富文本乙%02d" % i, GAME)
            outcome = run_battle(fa, fb, GAME)
            api = battle_to_api(outcome, [fighter_to_api(fa, GAME),
                                          fighter_to_api(fb, GAME)], GAME)
            battles += 1
            for e in api["log"]:
                self.assertIn("rich", e)
                joined = "".join(seg["t"] for seg in e["rich"])
                self.assertEqual(joined, e["text"])
                for seg in e["rich"]:
                    kinds.add(seg["k"])
                    self.assertIn("t", seg)
                    if seg["k"] == "skill":
                        self.assertIn("id", seg)
            if {"name-a", "name-b", "skill", "dmg", "heal"} <= kinds:
                break
        self.assertGreater(battles, 0)
        self.assertIn("name-a", kinds)
        self.assertIn("name-b", kinds)
        self.assertIn("skill", kinds)
        self.assertIn("dmg", kinds)
        self.assertIn("heal", kinds)

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

    def test_attack_start_logged_before_attacks(self):
        """普通攻击宣告契约（v1.0.0）：每次攻击行动（普攻/蓄力释放/雷罚）
        之前都先输出 attack_start 战报；斩断反击属于快速打击，不经过宣告。"""
        fa = derive_fighter("普攻甲", GAME)
        fb = derive_fighter("普攻乙", GAME)
        outcome = run_battle(fa, fb, GAME)
        marker = {}
        starts = attacks = with_start = 0
        for e in outcome.events:
            tpl = e["template"]
            if tpl == "attack_start":
                marker[e["params"]["a"]] = "start"
                starts += 1
            elif tpl == "sever_proc":
                marker[e["params"]["a"]] = "sever"
            elif tpl in ("attack_hit", "attack_miss", "thunder_cast",
                         "charge_release"):
                attacks += 1
                state = marker.get(e["params"]["a"])
                self.assertIn(state, ("start", "sever"),
                              "攻击事件 %s 前缺少 attack_start/sever_proc" % tpl)
                if state == "start":
                    with_start += 1
        self.assertGreater(starts, 0, "应出现普通攻击宣告")
        self.assertGreaterEqual(starts, with_start)
        self.assertGreater(with_start, attacks // 2,
                           "绝大多数攻击应由 attack_start 宣告")

    def test_faster_fighter_acts_first(self):
        # 属性投掷（v1.1.0 三角形分布），速度差异普遍存在
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
                # v1.2.1 起战报中的角色名为「【称号】名字」
                self.assertEqual(e["params"]["a"],
                                 "【%s】%s" % (compose_title_name(fast, GAME), fast.name),
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

    def test_judgment_layers_expire_independently(self):
        """审判标记（洗礼）：layers 逐层独立到期——on_status_expire 每层
        触发一次、伤害各自结算；施加与落雷时刻满足 施加刻+turns；
        审判可致死（judgment_death）；同配置双跑完全一致。"""
        import json as _json
        from namefight.config import load_game_config_from_data
        data = {name: _json.loads((CONFIG_ROOT / "game" / ("%s.json" % name))
                                  .read_text(encoding="utf-8"))
                for name in ("system", "attributes", "skills", "titles",
                             "battle", "ui")}
        for s in data["skills"]["skills"]:
            if s["id"] == "baptism":
                s["weight"] = 99
                for n in s["effect"]["nodes"]:
                    if n.get("type") == "chance":
                        n["params"]["chance"] = 1.0
                    if n.get("type") == "apply_status":
                        n["params"]["turns"] = 25
                        n["params"]["value"] = 800
            else:
                s["weight"] = 1
                for n in s["effect"]["nodes"]:
                    if n.get("type") == "chance":
                        n["params"]["chance"] = 0.0
        data["skills"]["skills"] = [s for s in data["skills"]["skills"]
                                    if s["id"] in ("baptism", "execution",
                                                   "momentum")]
        game = load_game_config_from_data(data)

        def play():
            fa = derive_fighter("洗礼审判甲", game)
            fb = derive_fighter("洗礼审判乙", game)
            out = run_battle(fa, fb, game)
            gains = [(e["tick"], e["params"]["turns"])
                     for e in out.events if e["template"] == "judgment_gain"]
            strikes = [e["tick"] for e in out.events
                       if e["template"] == "judgment_strike"]
            return out, gains, strikes

        out, gains, strikes = play()
        self.assertGreater(len(gains), 0, "应采样到审判施加")
        # 每层到期时刻 = 施加刻 + turns（个性化后的实际持续）
        applied = {t: int(tv) for t, tv in gains}
        for tick in strikes:
            self.assertTrue(any(t + tv == tick for t, tv in applied.items()),
                            "落雷刻 %d 不对应任何一层的 施加刻+持续" % tick)
        # 战斗结束时双方身上仍可能有悬空层，只要求足量落雷被观测到
        self.assertGreaterEqual(len(strikes), 3)
        self.assertIn("judgment_death",
                      [e["template"] for e in out.events],
                      "800×多层审判应能致死")
        out2, gains2, strikes2 = play()
        self.assertEqual((out.winner_name, out.events),
                         (out2.winner_name, out2.events),
                         "审判结算必须同名同命（双跑一致）")


if __name__ == "__main__":
    unittest.main()
