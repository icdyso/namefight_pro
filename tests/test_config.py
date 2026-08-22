"""配置完整性与「功能-文案解耦」校验（见 AGENTS.md 2.2）。"""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from namefight.battle import BUFF_IDS, SUPPORTED_EFFECTS, TEMPLATES_USED
from namefight.config import (REQUIRED_ATTRIBUTE_IDS, TITLE_FIELD_POOLS,
                              load_game_config, load_locale)
from namefight.fighter import STATS_KEYS_USED
from namefight.rng import DetRng
from namefight.text import render_template

CONFIG_ROOT = REPO_ROOT / "config"
GAME = load_game_config(CONFIG_ROOT)
LOCALES = {lang: load_locale(CONFIG_ROOT, lang) for lang in GAME.system.available_locales}

_TITLE_POOL_LOCALE = {"prefix": "prefixes", "core": "cores", "suffix": "suffixes"}


class GameConfigTests(unittest.TestCase):
    def test_required_attributes(self):
        ids = {a.id for a in GAME.attributes}
        for attr_id in REQUIRED_ATTRIBUTE_IDS:
            self.assertIn(attr_id, ids)

    def test_attribute_base_and_display_range(self):
        for a in GAME.attributes:
            self.assertLessEqual(a.min, a.max)
            self.assertGreater(a.base, 0)

    def test_skill_count_within_pool(self):
        self.assertLessEqual(GAME.skill_count_min, GAME.skill_count_max)
        self.assertLessEqual(GAME.skill_count_max, len(GAME.skills))

    def test_all_effect_types_supported(self):
        for sk in GAME.skills:
            self.assertIn(sk.effect.get("type"), SUPPORTED_EFFECTS,
                          "技能 %s 使用了引擎未支持的效果类型" % sk.id)

    def test_md5_variance_ranges_sane(self):
        var = GAME.skill_md5_variance
        self.assertLessEqual(var.chance_lo, var.chance_hi)
        self.assertLessEqual(var.value_lo, var.value_hi)

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
        for source, weight in link.source_weights:
            self.assertGreater(weight, 0)
            self.assertIn(source, ("own", "enemy"))
        for mode, weight in link.mode_weights:
            self.assertGreater(weight, 0)
            self.assertIn(mode, ("ratio", "difference"))
        if link.chance > 0:
            self.assertTrue(link.variables)
            for t in link.linkable_types:
                self.assertIn(t, SUPPORTED_EFFECTS,
                              "可共鸣类型 %s 不是引擎支持的效果" % t)
            for effect_type, param in link.targets.items():
                self.assertIn(param, ("chance", "value", "damage", "turns"),
                              "共鸣目标字段非法: %s" % effect_type)

    def test_name_modifiers_sane(self):
        mods = GAME.skill_name_modifiers
        for chance in (mods.prefix_chance, mods.suffix_chance):
            self.assertTrue(0.0 <= chance <= 1.0)
        self.assertLessEqual(mods.scale_lo, mods.scale_hi)
        self.assertGreater(mods.scale_lo, 0)
        known_params = {"chance", "value", "damage", "turns"}
        for pool in (mods.prefixes, mods.suffixes):
            self.assertTrue(pool)
            for m in pool:
                self.assertGreater(m.weight, 0)
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


class LocaleCoverageTests(unittest.TestCase):
    def test_every_game_id_has_text(self):
        for lang, loc in LOCALES.items():
            for a in GAME.attributes:
                self.assertIn(a.id, loc.attributes, "[%s] 属性 %s 缺文案" % (lang, a.id))
            for e in GAME.elements:
                self.assertIn(e.id, loc.elements, "[%s] 元素 %s 缺文案" % (lang, e.id))
            for s in GAME.skills:
                self.assertIn(s.id, loc.skills, "[%s] 技能 %s 缺文案" % (lang, s.id))
                self.assertIn("description", loc.skills[s.id])
            for pool_name, pool_key in _TITLE_POOL_LOCALE.items():
                for fdef in GAME.title_pools[pool_name]:
                    entry = loc.titles.get(pool_key, {}).get(fdef.id)
                    self.assertIsNotNone(entry, "[%s] 称号字段 %s/%s 缺文案" % (lang, pool_key, fdef.id))
                    self.assertIn("name", entry)
                    self.assertIn("desc", entry)

    def test_every_template_has_text(self):
        for lang, loc in LOCALES.items():
            for template in TEMPLATES_USED:
                self.assertIn(template, loc.battle_log,
                              "[%s] 战报模板 %s 缺文案" % (lang, template))

    def test_every_buff_has_text(self):
        for lang, loc in LOCALES.items():
            for buff_id in BUFF_IDS:
                entry = loc.buffs.get(buff_id)
                self.assertIsNotNone(entry, "[%s] buff %s 缺文案" % (lang, buff_id))
                self.assertIn("name", entry)
                self.assertIn("detail", entry)
                self.assertIn("desc", entry, "[%s] buff %s 缺详细说明" % (lang, buff_id))

    def test_every_stat_key_has_text(self):
        for lang, loc in LOCALES.items():
            for key in STATS_KEYS_USED:
                self.assertIn(key, loc.stats, "[%s] 技能参数标签 %s 缺文案" % (lang, key))
            for v in GAME.skill_variable_link.variables:
                self.assertIn("link_" + v.id, loc.stats,
                              "[%s] 共鸣标记 link_%s 缺文案" % (lang, v.id))

    def test_modifiers_have_locale_text(self):
        mods = GAME.skill_name_modifiers
        for lang, loc in LOCALES.items():
            for m in mods.prefixes:
                self.assertIn(m.id, loc.modifiers.get("prefixes", {}),
                              "[%s] 前缀 %s 缺文案" % (lang, m.id))
            for m in mods.suffixes:
                self.assertIn(m.id, loc.modifiers.get("suffixes", {}),
                              "[%s] 后缀 %s 缺文案" % (lang, m.id))

    def test_every_stat_key_has_text(self):
        for lang, loc in LOCALES.items():
            for key in STATS_KEYS_USED:
                self.assertIn(key, loc.stats, "[%s] 技能参数标签 %s 缺文案" % (lang, key))

    def test_ui_key_parity_across_locales(self):
        key_sets = [frozenset(loc.ui.keys()) for loc in LOCALES.values()]
        self.assertTrue(key_sets)
        for ks in key_sets[1:]:
            self.assertEqual(key_sets[0], ks)


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

    def test_gaussian_deterministic_and_bounded(self):
        a, b = DetRng(7), DetRng(7)
        for _ in range(50):
            x = a.next_gaussian(0.85, 1.15)
            y = b.next_gaussian(0.85, 1.15)
            self.assertEqual(x, y)
            self.assertGreaterEqual(x, 0.85)
            self.assertLessEqual(x, 1.15)

    def test_gaussian_concentrates_near_midpoint(self):
        rng = DetRng(99)
        inside = 0
        total = 300
        for _ in range(total):
            x = rng.next_gaussian(0.0, 1.0)
            if 0.25 <= x <= 0.75:
                inside += 1
        self.assertGreater(inside / total, 0.5,
                           "高斯抽样应集中在区间中段（实测 %.2f）" % (inside / total))

    def test_gaussian_range_discrete(self):
        rng = DetRng(42)
        counts = {2: 0, 3: 0}
        for _ in range(200):
            value = rng.next_gaussian_range(2, 3)
            self.assertIn(value, (2, 3))
            counts[value] += 1
        self.assertTrue(counts[2] and counts[3])

    def test_render_template_resolves_refs(self):
        loc = LOCALES["zh"]
        text = render_template(loc.battle_log["skill_proc"],
                               {"a": "张三", "skill": {"ref": "skill", "id": "heavy_strike"}}, loc)
        self.assertIn("张三", text)
        self.assertIn(loc.skills["heavy_strike"]["name"], text)

    def test_render_template_missing_keys_are_safe(self):
        loc = LOCALES["zh"]
        self.assertEqual(render_template("{a} {missing}", {"a": 1}, loc), "1 {missing}")
        self.assertEqual(render_template(None, {}, loc), "")

    def test_format_helpers(self):
        from namefight.text import format_num, format_pct
        self.assertEqual(format_pct(0.213), "21%")
        self.assertEqual(format_pct(1.6), "160%")
        self.assertEqual(format_num(10.0), "10")
        self.assertEqual(format_num(12.5), "12.5")


if __name__ == "__main__":
    unittest.main()
