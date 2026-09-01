"""表达式系统（namefight/expr.py）单元测试：语法 / 求值 / 缓存与报错。"""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from namefight.expr import ExprError, eval_expr, expr_check, is_expr  # noqa: E402


class ExprTests(unittest.TestCase):
    ENV = {
        "self.hp": 20000.0, "self.max_hp": 20000.0, "self.atk": 1500.0,
        "self.mark:连击": 3.0, "enemy.atk": 1000.0,
        "value": 38.0,                     # 状态图施加参数
    }

    def test_basic_arithmetic(self):
        """四则与优先级。"""
        self.assertEqual(eval_expr("1 + 2 * 3", {}), 7.0)
        self.assertEqual(eval_expr("(1 + 2) * 3", {}), 9.0)
        self.assertEqual(eval_expr("10 / 4", {}), 2.5)
        self.assertEqual(eval_expr("-5 + 2", {}), -3.0)

    def test_variables_and_chinese_keys(self):
        """变量引用（含中文标记键）与混合运算。"""
        self.assertEqual(eval_expr("$self.atk * 2", self.ENV), 3000.0)
        self.assertEqual(eval_expr("$self.mark:连击 * 2", self.ENV), 6.0)
        self.assertEqual(eval_expr("$value + 2", self.ENV), 40.0)
        self.assertEqual(eval_expr("$enemy.atk / $self.atk", self.ENV),
                         1000.0 / 1500.0)

    def test_functions(self):
        """min / max / abs / floor。"""
        self.assertEqual(eval_expr("min($self.atk, $enemy.atk)", self.ENV), 1000.0)
        self.assertEqual(eval_expr("max(3, $self.mark:连击)", self.ENV), 3.0)
        self.assertEqual(eval_expr("abs(0 - 7)", {}), 7.0)
        self.assertEqual(eval_expr("floor(2.7)", {}), 2.0)

    def test_resonance_shape(self):
        """共鸣生成的表达式形态求值正确（基数×(1+率×变量式/基准)）。"""
        text = "(38) * (1 + (0.5) * (($self.atk) / 1500))"
        self.assertEqual(eval_expr(text, self.ENV), 38 * (1 + 0.5 * (1500 / 1500)))

    def test_div_zero_and_missing_var(self):
        """除零按 0；缺失变量按 0（标记层数语义）。"""
        self.assertEqual(eval_expr("1 / 0", {}), 0.0)
        self.assertEqual(eval_expr("$self.mark:不存在 * 2", self.ENV), 0.0)

    def test_syntax_errors_rejected(self):
        """语法错误必须被拒绝（配置加载早暴露）。"""
        for bad in ("1 +", "$ ", "foo(1)", "(1", "1 @ 2", "$$x"):
            with self.assertRaises(ExprError, msg=bad):
                expr_check(bad)

    def test_is_expr(self):
        """表达式判定：含 $ 的字符串。"""
        self.assertTrue(is_expr("$self.hp * 2"))
        self.assertFalse(is_expr("38"))
        self.assertFalse(is_expr(38))
        self.assertFalse(is_expr(None))


if __name__ == "__main__":
    unittest.main()
