"""配置完整性与「功能-文案解耦」校验（见 AGENTS.md 2.2）。"""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from namefight.battle import SUPPORTED_EFFECTS, TEMPLATES_USED
from namefight.config import REQUIRED_ATTRIBUTE_IDS, load_game_config, load_locale
from namefight.rng import DetRng
from namefight.text import render_template

CONFIG_ROOT = REPO_ROOT / "config"
GAME = load_game_config(CONFIG_ROOT)
LOCALES = {lang: load_locale(CONFIG_ROOT, lang) for lang in GAME.system.available_locales}


class GameConfigTests(unittest.TestCase):
    def test_required_attributes(self):
        ids = {a.id for a in GAME.attributes}
        for attr_id in REQUIRED_ATTRIBUTE_IDS:
            self.assertIn(attr_id, ids)

    def test_attribute_ranges_sane(self):
        for a in GAME.attributes:
            self.assertLessEqual(a.min, a.max)

    def test_skill_count_within_pool(self):
        self.assertLessEqual(GAME.skill_count_min, GAME.skill_count_max)
        self.assertLessEqual(GAME.skill_count_max, len(GAME.skills))

    def test_all_effect_types_supported(self):
        for sk in GAME.skills:
            self.assertIn(sk.effect.get("type"), SUPPORTED_EFFECTS,
                          "技能 %s 使用了引擎未支持的效果类型" % sk.id)

    def test_element_advantage_references_exist(self):
        element_ids = {e.id for e in GAME.elements}
        for e in GAME.elements:
            for defender in e.advantage:
                self.assertIn(defender, element_ids)

    def test_rarity_multipliers_reference_known_attributes(self):
        attr_ids = {a.id for a in GAME.attributes}
        for r in GAME.rarities:
            for attr_id in r.multipliers:
                self.assertIn(attr_id, attr_ids)


class LocaleCoverageTests(unittest.TestCase):
    def test_every_game_id_has_text(self):
        for lang, loc in LOCALES.items():
            for a in GAME.attributes:
                self.assertIn(a.id, loc.attributes, "[%s] 属性 %s 缺文案" % (lang, a.id))
            for e in GAME.elements:
                self.assertIn(e.id, loc.elements, "[%s] 元素 %s 缺文案" % (lang, e.id))
            for r in GAME.rarities:
                self.assertIn(r.id, loc.rarities, "[%s] 稀有度 %s 缺文案" % (lang, r.id))
            for s in GAME.skills:
                self.assertIn(s.id, loc.skills, "[%s] 技能 %s 缺文案" % (lang, s.id))
                self.assertIn("description", loc.skills[s.id])
            for ttl in GAME.titles:
                self.assertIn(ttl.id, loc.titles, "[%s] 称号 %s 缺文案" % (lang, ttl.id))

    def test_every_template_has_text(self):
        for lang, loc in LOCALES.items():
            for template in TEMPLATES_USED:
                self.assertIn(template, loc.battle_log,
                              "[%s] 战报模板 %s 缺文案" % (lang, template))

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


if __name__ == "__main__":
    unittest.main()
