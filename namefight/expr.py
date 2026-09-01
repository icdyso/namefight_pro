"""表达式与变量表（v3.2.0）。

所有数值参数（chance / value / gain / mult / turns …）除直接数值外，还允许
写**表达式**：数字、变量引用、+ - * / 括号与 min / max / abs / floor。
变量以 $ 开头引用，求值环境由 battle._expr_env 按当前双方与上下文现算：

变量表（平表，$ 后接完整路径）：
  $self.hp / $self.max_hp / $self.hp_pct      自身：当前 / 上限生命、生命比例
  $self.atk / $self.def / $self.spd           自身：有效攻击 / 防御 / 速度
  $self.crit / $self.dodge                    自身：暴击 / 闪避（百分数）
  $self.gauge / $self.gauge_pct               自身：行动槽值 / 比例
  $self.damage_dealt                          自身：本场累计造成伤害
  $self.mark:<键>                             自身：某标记层数（无 = 0）
  $self.stacks:<状态id>                       自身：某状态在场层数
  $self.total:<状态id>                        自身：某状态累计值（护盾余量 / 转化量）
  $self.records:<状态id>                      自身：某状态记录总和（记仇）
  $enemy.<同上>                               对方的同套变量
  $ctx.dmg / $ctx.absorbed                    本次造成伤害 / 本次被减免量
  $ctx.loop / $ctx.tick                       当前循环轮次 / 当前刻
  $<状态参数名>                               状态效果图上下文：施加参数
                                               （如毒图里的 $value）

示例：marker(add, value=$self.mark:连击 * 2)  把自身「连击」标记层数翻倍；
     stat_mod(gain=$enemy.atk * 0.2)          按对方攻击的 20% 永久加攻。

实现：手写递归下降解析器（绝不用 eval，纯标准库、完全确定）；
除零按 0 处理；解析结果按表达式文本缓存（同图反复执行零重复解析）。
配置层在加载时做语法校验（expr_check），写错早暴露。
"""
from __future__ import annotations

# 变量的静态清单（编辑器变量表面板与 schema 使用；(键, 中文说明)）
VARIABLE_GROUPS = (
    ("自身", (
        ("$self.hp", "当前生命"), ("$self.max_hp", "生命上限"),
        ("$self.hp_pct", "生命比例（0~1）"),
        ("$self.atk", "有效攻击"), ("$self.def", "有效防御"), ("$self.spd", "有效速度"),
        ("$self.crit", "暴击（分数）"), ("$self.dodge", "闪避（分数）"),
        ("$self.gauge", "行动槽值"), ("$self.gauge_pct", "行动槽比例"),
        ("$self.damage_dealt", "本场累计造成伤害"),
        ("$self.mark:<键>", "标记 <键> 的层数（无 = 0）"),
        ("$self.stacks:<状态id>", "状态在场层数"),
        ("$self.total:<状态id>", "状态累计值（护盾余量 / 转化量）"),
        ("$self.records:<状态id>", "状态记录总和（记仇）"),
    )),
    ("敌方", (
        ("$enemy.hp", "当前生命"), ("$enemy.max_hp", "生命上限"),
        ("$enemy.hp_pct", "生命比例（0~1）"),
        ("$enemy.atk", "有效攻击"), ("$enemy.def", "有效防御"), ("$enemy.spd", "有效速度"),
        ("$enemy.crit", "暴击（分数）"), ("$enemy.dodge", "闪避（分数）"),
        ("$enemy.gauge", "行动槽值"), ("$enemy.gauge_pct", "行动槽比例"),
        ("$enemy.damage_dealt", "本场累计造成伤害"),
        ("$enemy.mark:<键>", "标记 <键> 的层数（无 = 0）"),
        ("$enemy.stacks:<状态id>", "状态在场层数"),
        ("$enemy.total:<状态id>", "状态累计值"),
        ("$enemy.records:<状态id>", "状态记录总和"),
    )),
    ("上下文", (
        ("$ctx.dmg", "本次造成的伤害（命中 / 受击链）"),
        ("$ctx.absorbed", "本次被减免的伤害量（防御链）"),
        ("$ctx.loop", "当前循环轮次"),
        ("$ctx.tick", "当前刻"),
    )),
)

# 支持的函数（中文说明供编辑器提示）
FUNCTIONS = {
    "min": "min(a, b) 较小值",
    "max": "max(a, b) 较大值",
    "abs": "abs(x) 绝对值",
    "floor": "floor(x) 向下取整",
    "pow": "pow(x, y) 幂（不屈概率衰减 0.5^次数 等）",
}


class ExprError(ValueError):
    """表达式语法错误（配置加载时抛出，写错早暴露）。"""


# ---- 词法 ----

_PATH_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:")
for _code in range(0x4E00, 0x9FFF + 1):     # 中文（标记键 / 状态名允许中文）
    _PATH_CHARS.add(chr(_code))


def _tokenize(text: str):
    """切词：数字 / $变量路径 / 运算符 / 括号 / 逗号 / 函数名。"""
    tokens = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and (text[j].isdigit() or text[j] == "."):
                j += 1
            tokens.append(("num", float(text[i:j])))
            i = j
            continue
        if ch == "$":
            j = i + 1
            while j < n and text[j] in _PATH_CHARS:
                j += 1
            if j == i + 1:
                raise ExprError("变量引用 $ 后为空: %r" % text)
            tokens.append(("var", text[i + 1:j]))
            i = j
            continue
        if ch.isalpha():
            j = i
            while j < n and (text[j].isalpha() or text[j] == "_"):
                j += 1
            tokens.append(("name", text[i:j]))
            i = j
            continue
        if ch in "+-*/(),":
            tokens.append((ch, ch))
            i += 1
            continue
        raise ExprError("表达式含非法字符 %r: %r" % (ch, text))
    return tokens


# ---- 递归下降解析（产出闭包树） ----

def _parse(tokens):
    """递归下降：expr := term (('+'|'-') term)*；term := factor (('*'|'/') factor)*；
    factor := 数 | $变量 | 函数(参数,…) | (expr) | -factor。"""
    pos = [0]

    def peek():
        return tokens[pos[0]] if pos[0] < len(tokens) else (None, None)

    def take(expected=None):
        kind, value = peek()
        if expected is not None and kind != expected:
            raise ExprError("期望 %r，得到 %r" % (expected, kind))
        pos[0] += 1
        return kind, value

    def parse_expr():
        node = parse_term()
        while peek()[0] in ("+", "-"):
            op = take()[0]
            rhs = parse_term()
            node = (_add if op == "+" else _sub, node, rhs)
        return node

    def parse_term():
        node = parse_factor()
        while peek()[0] in ("*", "/"):
            op = take()[0]
            rhs = parse_factor()
            node = (_mul if op == "*" else _div, node, rhs)
        return node

    def parse_factor():
        kind, value = peek()
        if kind == "-":
            take()
            return (_neg, parse_factor())
        if kind == "+":
            take()
            return parse_factor()
        if kind == "num":
            take()
            return (_const, value)
        if kind == "var":
            take()
            return (_var, value)
        if kind == "name":
            take()
            fname = str(value)
            if fname not in _FUNCS:
                raise ExprError("未知函数 %r" % fname)
            take("(")
            args = [parse_expr()]
            while peek()[0] == ",":
                take(",")
                args.append(parse_expr())
            take(")")
            return (_call, _FUNCS[fname], args)
        if kind == "(":
            take("(")
            node = parse_expr()
            take(")")
            return node
        raise ExprError("表达式在 %r 处无法解析" % (value,))

    tree = parse_expr()
    if pos[0] != len(tokens):
        raise ExprError("表达式末尾有多余内容: %r" % (tokens[pos[0]],))
    return tree


def _add(a, b):
    return lambda env: a(env) + b(env)


def _sub(a, b):
    return lambda env: a(env) - b(env)


def _mul(a, b):
    return lambda env: a(env) * b(env)


def _div(a, b):
    def run(env):
        d = b(env)
        return a(env) / d if d else 0.0      # 除零按 0（确定性兜底）
    return run


def _neg(a):
    return lambda env: -a(env)


def _const(v):
    return lambda env: v


def _var(path):
    def run(env):
        # 缺失按 0：动态键（$self.mark:键 等）不存在 = 层数/量为 0，
        # 与变量表文档语义一致
        return float(env.get(path, 0.0))
    return run


_FUNCS = {
    "min": min, "max": max, "abs": abs,
    "floor": lambda x: float(int(x // 1)),
    "pow": lambda x, y: float(x) ** float(y),
}


def _call(fn, args):
    return lambda env: float(fn(*[a(env) for a in args]))


# ---- 编译缓存与入口 ----

_cache = {}      # 表达式文本 -> 求值闭包（同图反复执行零重复解析）


def compile_expr(text: str):
    """编译表达式为求值闭包（带缓存）；语法错误抛 ExprError。"""
    fn = _cache.get(text)
    if fn is None:
        fn = _run(_parse(_tokenize(text)))
        _cache[text] = fn
    return fn


def _run(tree):
    """把 (算子, 参数…) 树转成闭包。"""
    op = tree[0]
    if op in (_add, _sub, _mul, _div):
        return op(_run(tree[1]), _run(tree[2]))
    if op is _neg:
        return _neg(_run(tree[1]))
    if op is _call:
        return _call(tree[1], [_run(a) for a in tree[2]])
    return op(tree[1])            # _const / _var


def eval_expr(text: str, env: dict) -> float:
    """在变量环境 env 下求值（env 为平表：{'self.hp': 100, ...}）。"""
    return compile_expr(text)(env)


def is_expr(value) -> bool:
    """该参数值是否为表达式（含 $ 的字符串）。"""
    return isinstance(value, str) and "$" in value


def expr_check(text: str) -> None:
    """配置加载时的语法校验（不求值，只确认可解析）。"""
    compile_expr(text)
