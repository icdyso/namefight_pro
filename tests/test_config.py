"""配置完整性校验（v0.10.0：数值与文案合并在 config/game，单语言）。"""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from namefight.battle import BUFF_IDS, SUPPORTED_EFFECTS, TEMPLATES_USED
from namefight.config import (REQUIRED_ATTRIBUTE_IDS, TITLE_FIELD_POOLS,
                              load_game_config)
from namefight.fighter import STATS_KEYS_USED
from namefight.rng import DetRng
from namefight.text import format_num, format_pct, render_template

CONFIG_ROOT = REPO_ROOT / "config"
GAME = load_game_config(CONFIG_ROOT)


class GameConfigTests(unittest.TestCase):
    def test_required_attributes(self):
        ids = {a.id for a in GAME.attributes}
        for attr_id in REQUIRED_ATTRIBUTE_IDS:
            self.assertIn(attr_id, ids)

    def test_attribute_base_and_range(self):
        for a in GAME.attributes:
            self.assertLessEqual(a.min, a.max)
            self.assertGreater(a.base, 0)
            self.assertTrue(a.name, "属性 %s 缺显示名" % a.id)

    def test_skill_count_within_pool(self):
        self.assertLessEqual(GAME.skill_count_min, GAME.skill_count_max)
        self.assertLessEqual(GAME.skill_count_max, len(GAME.skills))

    def test_all_effect_types_supported(self):
        for sk in GAME.skills:
            self.assertIn(sk.effect.get("type"), SUPPORTED_EFFECTS,
                          "技能 %s 使用了引擎未支持的效果类型" % sk.id)

    def test_md5_variance_ranges_sane(self):
        var = GAME.skill_md5_variance
        self.assertLessEqual(var.value_lo, var.value_hi)
        self.assertGreater(var.value_lo, 0)

    def test_variable_link_sane(self):
        link = GAME.skill_variable_link
        self.assertTrue(0.0 <= link.chance <= 1.0)
        attr_ids = {a.id for a in GAME.attributes}
        for v in link.variables:
            self.assertIn(v.id, attr_ids, "共鸣变量 %s 不是已定义属性" % v.id)
            self.assertGreater(v.weight, 0)
            self.assertLessEqual(v.rate_lo, v.rate_hi)
            self.assertGreaterEqual(v.rate_lo, 0)
            self.assertIn(v.diff_against, attr_ids,
                          "共鸣变量 %s 的差值参照 %s 不是已定义属性" % (v.id, v.diff_against))
        modes = {m for m, _ in link.mode_weights}
        self.assertTrue(modes)
        for mode in modes:
            self.assertIn(mode, ("own", "enemy", "difference", "sum"))
        if link.chance > 0:
            self.assertTrue(link.variables)
            for effect_type, fields in link.targets.items():
                self.assertIn(effect_type, SUPPORTED_EFFECTS,
                              "可共鸣类型 %s 不是引擎支持的效果" % effect_type)
                self.assertTrue(1 <= len(fields) <= 2,
                                "共鸣目标字段应为 1~2 个: %s" % effect_type)

    def test_mastery_ranges_sane(self):
        for s in GAME.skills:
            lo, hi = s.mastery
            self.assertGreater(lo, 0)
            self.assertLessEqual(lo, hi)
            self.assertIn(s.mastery_on, ("chance", "value", "immune"))

    def test_name_modifiers_sane(self):
        mods = GAME.skill_name_modifiers
        for chance in (mods.prefix_chance, mods.suffix_chance):
            self.assertTrue(0.0 <= chance <= 1.0)
        self.assertLessEqual(mods.scale_lo, mods.scale_hi)
        self.assertGreater(mods.scale_lo, 0)
        known_params = {"chance", "value", "damage", "turns", "ticks"}
        for pool in (mods.prefixes, mods.suffixes):
            self.assertTrue(pool)
            for m in pool:
                self.assertGreater(m.weight, 0)
                self.assertTrue(m.name, "词缀 %s 缺显示名" % m.id)
                for param in m.mod:
                    self.assertIn(param, known_params,
                                  "词缀 %s 修正了未知参数 %s" % (m.id, param))

    def test_title_bonuses_reference_known_attributes(self):
        attr_ids = {a.id for a in GAME.attributes}
        for pool in GAME.title_pools.values():
            for fdef in pool:
                for attr_id, delta in fdef.bonus.items():
                    self.assertIn(attr_id, attr_ids)
                    self.assertIsInstance(delta, int)

    def test_title_structures_reference_valid_fields(self):
        for s in GAME.title_structures:
            self.assertTrue(s.fields)
            for fname in s.fields:
                self.assertIn(fname, TITLE_FIELD_POOLS,
                              "称号结构 %s 引用了未知字段 %s" % (s.id, fname))
            self.assertEqual(len(s.connectors), len(s.fields) - 1)
        for pool_name in ("prefix", "core", "suffix"):
            self.assertTrue(GAME.title_pools[pool_name], "称号字段池为空: %s" % pool_name)

    def test_battle_constants_sane(self):
        self.assertGreaterEqual(GAME.battle.max_ticks, 1)
        self.assertGreater(GAME.battle.gauge_threshold, 0)
        self.assertGreater(GAME.battle.defense_constant, 0)
        self.assertGreaterEqual(GAME.battle.message_delay_ms, 16)
        self.assertGreaterEqual(GAME.battle.action_pause_every, 1)
        self.assertGreaterEqual(GAME.battle.action_pause_ms, 0)


class ConfigTextTests(unittest.TestCase):
    """文案契约（v0.10.0）：每个条目的文字与其数值保存在同一配置文件内。"""

    def test_skills_have_text(self):
        for s in GAME.skills:
            self.assertTrue(s.name, "技能 %s 缺名称" % s.id)
            self.assertTrue(s.description, "技能 %s 缺风味描述" % s.id)

    def test_title_fields_have_text(self):
        for pool_name in ("prefix", "core", "suffix"):
            for fdef in GAME.title_pools[pool_name]:
                self.assertTrue(fdef.name, "称号字段 %s/%s 缺名称" % (pool_name, fdef.id))
                self.assertTrue(fdef.desc, "称号字段 %s/%s 缺描述" % (pool_name, fdef.id))

    def test_every_template_has_text(self):
        for template in TEMPLATES_USED:
            self.assertIn(template, GAME.battle_log,
                          "战报模板 %s 缺文案" % template)

    def test_every_buff_has_text(self):
        for buff_id in BUFF_IDS:
            entry = GAME.buffs.get(buff_id)
            self.assertIsNotNone(entry, "buff %s 缺文案" % buff_id)
            self.assertIn("name", entry)
            self.assertIn("detail", entry)

    def test_every_stat_key_has_text(self):
        for key in STATS_KEYS_USED:
            self.assertIn(key, GAME.stats, "技能参数标签 %s 缺文案" % key)
        for v in GAME.skill_variable_link.variables:
            self.assertIn("link_" + v.id, GAME.stats,
                          "共鸣标记 link_%s 缺文案" % v.id)

    def test_ui_text_present(self):
        for key in ("app_title", "battle_button", "winner_text", "error_empty_name"):
            self.assertIn(key, GAME.ui)


class RngAndTextTests(unittest.TestCase):
    # splitmix64 金向量：算法一旦被改动，此测试立即报警（确定性契约的一部分）
    GOLDEN_SEED_42 = [13679457532755275413, 2949826092126892291, 5139283748462763858]
    GOLDEN_SEED_0 = [16294208416658607535, 7960286522194355700]

    def test_rng_golden_vector(self):
        rng = DetRng(42)
        self.assertEqual([rng.next_u64() for _ in range(3)], self.GOLDEN_SEED_42)
        rng0 = DetRng(0)
        self.assertEqual([rng0.next_u64() for _ in range(2)], self.GOLDEN_SEED_0)

    def test_rng_same_seed_same_stream(self):
        a, b = DetRng(123), DetRng(123)
        self.assertEqual([a.next_u64() for _ in range(10)],
                         [b.next_u64() for _ in range(10)])

    def test_triangular_deterministic_and_bounded(self):
        """三角形分布（v1.1.0，两均匀数取均值）：同种子同序列、
        永远落在区间内且不出现截断堆积（端点值不会被硬压到边界）。"""
        a, b = DetRng(7), DetRng(7)
        at_bound = 0
        for _ in range(50):
            x = a.next_triangular(0.85, 1.15)
            y = b.next_triangular(0.85, 1.15)
            self.assertEqual(x, y)
            self.assertGreaterEqual(x, 0.85)
            self.assertLessEqual(x, 1.15)
            if x == 0.85 or x == 1.15:
                at_bound += 1
        self.assertEqual(at_bound, 0, "三角形分布不应出现边界截断堆积")

    def test_triangular_concentrates_near_midpoint(self):
        rng = DetRng(99)
        inside = 0
        total = 300
        for _ in range(total):
            x = rng.next_triangular(0.0, 1.0)
            if 0.25 <= x <= 0.75:
                inside += 1
        self.assertGreater(inside / total, 0.65,
                           "三角形抽样应集中在区间中段（实测 %.2f）" % (inside / total))

    def test_triangular_range_discrete(self):
        rng = DetRng(42)
        counts = {2: 0, 3: 0}
        for _ in range(200):
            value = rng.next_triangular_range(2, 3)
            self.assertIn(value, (2, 3))
            counts[value] += 1
        self.assertTrue(counts[2] and counts[3])

    def test_render_template_resolves_refs(self):
        text = render_template(GAME.battle_log["skill_proc"],
                               {"a": "张三", "skill": {"ref": "skill", "id": "heavy_strike"}}, GAME)
        self.assertIn("张三", text)
        self.assertIn(GAME.skill_def("heavy_strike").name, text)

    def test_render_template_missing_keys_are_safe(self):
        self.assertEqual(render_template("{a} {missing}", {"a": 1}, GAME), "1 {missing}")
        self.assertEqual(render_template(None, {}, GAME), "")

    def test_format_helpers(self):
        from namefight.text import format_num, format_pct
        # v0.8.0 起展示层：百分数 2 位小数，其余数值取整
        self.assertEqual(format_pct(0.2131), "21.31%")
        self.assertEqual(format_pct(1.6), "160.00%")
        self.assertEqual(format_num(10.0), "10")
        self.assertEqual(format_num(7.82), "8")
        self.assertEqual(format_num(95.18), "95")
        self.assertEqual(format_num(-3.5), "-4")


if __name__ == "__main__":
    unittest.main()
