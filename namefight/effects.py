"""最小原子注册表与图编译（v3.0.0 解耦核心）。

技能与状态的逻辑统一为「节点 + 连边」的有向无环图（nodes / edges），节点四类：

- trigger    触发节点：声明执行时机（技能钩子 HOOKS 或状态钩子 STATUS_HOOKS）；
- condition  条件节点：判断（概率 / 生命阈值 / 状态在场 / 一次性 / 本次暴击），
             出边可带 gate: "pass"（默认）/ "fail" —— **分支**：判定通过走 pass
             子树、失败走 fail 子树；
- op         原子节点：最小效果原子（OPS 注册表，共 12 个）；
- struct     结构节点：控制流（loop 循环），子树按参数反复执行 —— **循环**。

v3.0.0 的原子化原则：引擎只认识「攻击方式 / 属性变动 / 体力变动 / 行动槽 /
施加状态 / 驱散 / 记录 / 控制流」这些最小规则，一切技能行为（雷罚连击、燃血
自耗、永久成长、蓄力、血契……）都是原子在图上的组合，全部数据化于配置：

- 雷罚  = on_attack → chance(0.78) → loop{max 9, decay 0.93} → strike(真伤 0.27)
- 燃血  = on_attack → chance → hp_mod(自耗 5% 最大生命) → hit_mod(×2.15)
- 成长  = after_action → chance → apply_status(tempo: 每层 速度+100 攻击+10)
- 蓄力  = apply_status(charge)，charge 状态自带 on_owner_action_consume 效果图
  （strike 必中 ×2.2 +25% 暴击）——状态定义同样带效果图，可分别编辑。

compile_graph 把图校验并编译为 {hook: (执行树, ...)}。执行顺序与随机数消耗
顺序完全由配置数组顺序确定（确定性契约，改变即 breaking）：技能按派生顺序、
同一图的多个触发节点按 nodes 数组顺序、同一节点的多条出边按 edges 数组顺序
（pass 边组先于 fail 边组）、loop 每轮迭代按子树顺序消耗。

本模块只保存注册表元数据与编译器；原子的战斗行为实现位于 battle.py
（_OP_IMPL，键与 OPS 一一对应，导入时断言）。参数规格（ParamSpec）同时驱动：
配置校验（config.py）、共鸣格式与上下限、熟练度/词缀可作用参数、以及
/api/schema（可视化编辑器的表单渲染依据）。
"""
from __future__ import annotations

from dataclasses import dataclass

# ---- 技能钩子（技能图的触发时机，固定顺序；文案键 hook_<name>） ----
HOOKS = (
    "battle_start",       # 战斗开始（不屈意志注册）
    "action_interrupt",   # 敌方即将行动时（斩断）
    "action_start",       # 自己行动开始（血契 / 回春 / 净化）
    "before_attack",      # 攻击前判定（背水一战）
    "on_attack",          # 攻击链（倍率修饰 / 蓄力 / 雷罚 / 施加状态）
    "on_defend",          # 受击防御链（壁垒减伤 / 反甲反弹 / 锻痕叠层）
    "on_hit_landed",      # 自己命中对方后（乘胜叠层）
    "on_hit_taken",       # 自己被命中后（怨念积攒 / 记仇记录）
    "after_action",       # 行动结束后（大器晚成成长）
    "on_status_gain",     # 自己获得某在场状态时（等待状态/标记——事件驱动）
    "on_status_lose",     # 自己失去某在场状态时（到期 / 驱散 / 清除）
    "on_attack_miss",     # 自己攻击落空时（乘胜清零等的原生实现位）
    "on_lethal",          # 自己生命将降至 0 以下时（钩子内可救援；结束仍 <=0 才死亡）
)

# ---- 状态钩子（状态定义 effects 图的触发时机；文案键 hook_<name>） ----
STATUS_HOOKS = (
    "on_status_apply",         # 状态被施加时
    "on_status_tick",          # 每 interval 刻（毒发 / 回春回复）
    "on_status_expire",        # layers 模式逐层到期：每层触发一次（审判；
                               #   先于 on_status_lose，在拥有者身上执行）
    "on_owner_action",         # 拥有者行动开始（流血损失 / 眩晕吞行动）
    "on_owner_action_consume", # 替换拥有者的本次行动（蓄力释放）
    "on_owner_attack_hit",     # 拥有者命中对方后（吸血）
)

ALL_HOOKS = HOOKS + STATUS_HOOKS   # 全部钩子（任意挂载的原子用）


@dataclass(frozen=True)
class ParamSpec:
    """效果参数规格。kind: float / int / pct(0~1 分数) / turns(正整数) /
    bool / text / enum；fmt 为共鸣展示格式（pct 百分数 / num 整数 / turns 刻数），
    clamp 为共鸣上下限 (lo, hi)（None = 不限）；unit 为展示量纲（词缀文案用）；
    link=True 的参数可成为共鸣变数（槽位按遍历顺序分配，每技能至多
    variable_link.max_slots 个）。
    show_if = (依赖键, 允许值元组)：仅当依赖参数取这些值时该参数才适用
    （编辑器据此联动显隐；依赖键缺省取其枚举首项，如 basis 缺省 flat）。
    派生层的共鸣候选与编辑器表单共用同一判定，避免给不适用的参数共鸣。"""
    key: str                 # 参数名（图 params 字典里的键）
    kind: str = "float"      # 数值类型：float/int/pct/turns/bool/text/enum
    fmt: str = "pct"         # 共鸣展示格式：pct 百分数 / num 整数 / turns 刻数
    clamp: tuple = None      # 共鸣上下限 (lo, hi)；None 表示该端不限制
    unit: str = None         # 展示量纲：hp/def/gauge/spd/atk（词缀文案用）
    link: bool = False       # 是否可成为共鸣变数（槽位候选）
    required: bool = True    # 基配置中是否必填（可选参数声明 False）
    options: tuple = None    # kind=enum 的合法取值（如 target: self/enemy）
    show_if: tuple = None    # 联动适用条件 (依赖键, 允许值元组)


def spec_applicable(specs, params: dict, spec) -> bool:
    """参数在当前取值下是否适用（show_if 判定；依赖键缺省取其枚举首项）。
    specs 为同节点全部规格（查依赖键的默认值用），params 为节点当前参数。"""
    if spec.show_if is None:
        return True
    dep_key, allowed = spec.show_if
    if dep_key in params:
        current = params[dep_key]
    else:
        dep = next((s for s in specs if s.key == dep_key), None)
        current = dep.options[0] if dep is not None and dep.options else None
    return current in allowed


def _pct(key, clamp=None, link=False, unit=None, required=True, show_if=None):
    """百分数参数（0~1 分数存储，展示 ×100）的规格快捷构造。"""
    return ParamSpec(key, "pct", "pct", clamp, unit, link, required,
                     None, show_if)


def _num(key, clamp=None, link=False, unit=None, required=True, show_if=None):
    """绝对数值参数（引擎真实值，展示取整）的规格快捷构造。"""
    return ParamSpec(key, "float", "num", clamp, unit, link, required,
                     None, show_if)


def _turns(key, clamp=(1, 20), link=False, required=True):
    """刻数 / 次数参数（正整数，共鸣后仍至少为 1）的规格快捷构造。"""
    return ParamSpec(key, "turns", "turns", clamp, None, link, required)


def _st():
    """状态引用参数（必填文本：目标状态的 id；编辑器渲染为状态下拉框）。"""
    return ParamSpec("status", "text")


P = ParamSpec  # 通用规格别名（枚举 / 布尔 / 文本参数直接用）


def _cond(type_, params):
    """条件注册项快捷构造：type_ = 条件类型名，params = 参数规格表。"""
    return type_, {"params": tuple(params), "text_key": "cond_" + type_}


# 值源注册表（compare 条件的 left/right 取值；id 形如 "self.hp_pct"）：
# 比例类（hp_pct / gauge_pct / crit / dodge）均为 0~1 分数，绝对值类为引擎真实值
CMP_SOURCES = (
    "self.hp_pct", "enemy.hp_pct",
    "self.atk", "enemy.atk",
    "self.def", "enemy.def",
    "self.spd", "enemy.spd",
    "self.crit", "enemy.crit",
    "self.dodge", "enemy.dodge",
    "self.gauge_pct", "enemy.gauge_pct",
)
# 右值额外可取 "const"（固定值，由 value 参数给出）
CMP_RIGHT = CMP_SOURCES + ("const",)
# 比较运算（文案键 cmp_<op>）
CMP_OPS = ("lt", "le", "gt", "ge")


# ---- 条件注册表（判断；出边 gate=pass/fail 构成分支；文案键 cond_<type>） ----
CONDITIONS = dict([
    _cond("chance", [_pct("chance", (0.02, 0.95), link=True)]),  # 概率判定
    # （v3.7.0 起触发率恢复可共鸣——v1 的「触发门槛」共鸣；钳制 [0.02, 0.95]）
    # compare：比较值与值（通用判断）——自身/敌方的 生命比例、攻防速、暴击、
    # 闪避、行动槽比例 两两比较，或与固定值比较（right=const 时用 value）
    _cond("compare", [P("left", "enum", options=CMP_SOURCES),
                      P("op", "enum", options=CMP_OPS),
                      P("right", "enum", options=CMP_RIGHT),
                      _pct("value", (0.0, 1.0), required=False,
                           show_if=("right", ("const",)))]),
    # stacks_cmp：某状态在己方/敌方身上的在场层数与固定值比较
    _cond("stacks_cmp", [_st(),
                         P("target", "enum", options=("self", "enemy")),
                         P("op", "enum", options=CMP_OPS),
                         _num("value", (0.0, None), required=False)]),
    _cond("has_status", [_st()]),                          # 自身在场某状态
    _cond("no_status", [_st()]),                           # 自身不在场某状态
    _cond("has_marker", [P("key", "text"),                 # 自身带有某标记
                         P("op", "enum", options=CMP_OPS, required=False),
                         _num("count", (0.0, None), required=False,
                              show_if=("op", CMP_OPS))]),  # 层数比较（选了 op 才有）
    _cond("no_marker", [P("key", "text")]),                # 自身没有某标记（层数 0）
    _cond("once_per_battle", [P("key", "text")]),          # 每场一次（真执行才占位）
    _cond("last_crit", []),                                # 本次命中为暴击
])


def _op(type_, hooks, params, logged):
    """原子注册项快捷构造：hooks = 允许挂载的钩子（技能钩子与状态钩子的
    并集子集），params = 参数规格表，logged = 执行时是否宣告「使用了技能」。"""
    return type_, {"hooks": tuple(hooks),
                   "params": tuple(params),
                   "logged": logged,
                   "text_key": "op_" + type_}


# ---- 效果原子注册表（v3.0.0 最小规则集；行为实现见 battle.py._OP_IMPL） ----
OPS = dict([
    # strike：一次独立攻击（攻击方式原子）。
    # basis=none 时伤害 = 攻击 × mult；basis=recorded_sum 时 = 记录总和 × value
    # （记仇释放）；basis=taken_absorbed 时 = 本次被减免量 × value（反甲反弹）。
    # real=true 为真实伤害（无视防御 / 闪避 / 暴击）；mode：extra 追加打击 /
    # replace 替换本次攻击 / append 附加到本次攻击的已结算伤害；
    # lifesteal 本次攻击按造成伤害的比例吸血。
    _op("strike",
        ("on_attack", "action_interrupt", "on_defend", "on_hit_landed",
         "on_owner_action_consume", "on_status_apply",
         "on_status_gain", "on_status_lose"),
        [P("target", "enum", options=("enemy", "self")),
         _pct("mult", (0.05, 8.0), link=True, required=False),
         P("basis", "enum", options=("none", "recorded_sum", "taken_absorbed"),
           required=False),
         _pct("value", (0.05, 8.0), link=True, required=False,
              show_if=("basis", ("recorded_sum", "taken_absorbed"))),
         P("real", "bool", required=False),
         _pct("pen", (0.0, 1.0), link=True, required=False),
         _pct("crit_bonus", (0.0, 1.0), link=True, required=False),
         P("must_hit", "bool", required=False),
         P("mode", "enum", options=("extra", "replace", "append"), required=False),
         _pct("lifesteal", (0.0, 1.5), link=True, required=False),
         P("event", "text", required=False)],
        logged=True),
    # hit_mod：修饰本次攻击（倍率 / 穿透 / 暴击加成 / 必中），不产生独立伤害。
    _op("hit_mod", ("on_attack",),
        [_pct("mult", (0.1, 6.0), link=True, required=False),
         _pct("pen", (0.0, 1.0), link=True, required=False),
         _pct("crit_bonus", (0.0, 1.0), link=True, required=False),
         P("must_hit", "bool", required=False),
         P("announce", "bool", required=False),
         P("event", "text", required=False)],
        logged=True),
    # taken_mod：减免本次所受伤害的一部分（被减免量计入 absorbed 供反弹）。
    _op("taken_mod", ("on_defend",),
        [_pct("cut", (0.01, 0.9), link=True), P("event", "text", required=False)],
        logged=False),
    # grant_immune：完全免疫本次伤害（终止防御链）。
    _op("grant_immune", ("on_defend",), [P("event", "text", required=False)],
        logged=False),
    # stat_mod：属性变动原子（目标某属性永久 ± 数值；basis=recorded_lifesteal
    # 时增量 = value × 本次记录的吸血总量——血契转化）。
    _op("stat_mod",
        ("after_action", "on_status_apply", "on_status_tick",
         "on_status_expire", "on_owner_action", "on_owner_attack_hit",
         "on_status_gain", "on_status_lose"),
        [P("target", "enum", options=("self", "enemy")),
         P("stat", "enum", options=("hp", "atk", "def", "spd", "crit", "dodge")),
         _num("gain", (0.0, None), link=True, required=False),
         P("basis", "enum", options=("flat", "recorded_lifesteal"), required=False),
         _pct("value", (0.01, 4.0), link=True, required=False),
         ParamSpec("status", "text", required=False),   # 累计展示用状态 id（可选）
         P("event", "text", required=False)],
        logged=False),
    # hp_mod：体力变动原子。type=heal 治疗（不溢出）；type=loss 流失（不触发
    # 受击反应与不屈，can_kill=true 时可致死——毒 / 流血；floor1=true 时保底
    # 1 点不自灭——燃血 / 血契献祭）。basis=flat 用 value（固定量）；比例基准
    # （maxhp 最大生命 / curhp 当前生命 / applier_atk 施加者攻击 /
    # dealt 本次造成伤害）用 ratio。
    _op("hp_mod",
        ("on_attack", "action_start", "action_interrupt", "on_hit_taken",
         "on_status_apply", "on_status_tick", "on_status_expire",
         "on_owner_action", "on_owner_attack_hit", "on_status_gain",
         "on_status_lose", "on_lethal"),
        [P("target", "enum", options=("self", "enemy")),
         P("type", "enum", options=("heal", "loss")),
         P("basis", "enum", options=("flat", "maxhp", "curhp", "applier_atk", "dealt")),
         _num("value", (0.0, None), link=True, unit="hp", required=False,
              show_if=("basis", ("flat",))),
         _pct("ratio", (0.0, 2.0), link=True, required=False,
              show_if=("basis", ("maxhp", "curhp", "applier_atk", "dealt"))),
         P("can_kill", "bool", required=False, show_if=("type", ("loss",))),
         P("floor1", "bool", required=False, show_if=("type", ("loss",))),
         P("event", "text", required=False)],
        logged=True),
    # gauge_mod：行动槽推进 / 倒退（×100 量纲，速度 ~1000）。
    _op("gauge_mod",
        ("on_attack", "action_interrupt", "on_hit_landed", "on_owner_attack_hit",
         "on_status_expire", "on_status_gain", "on_status_lose"),
        [P("target", "enum", options=("self", "enemy")),
         _num("gain", (-20000.0, 20000.0), link=True, unit="gauge")],
        logged=True),
    # hp_swap：交换双方当前生命值（各自不超过自身上限——命运天平；
    # 无中间态的精确交换，不可由 hp_mod 组合表达，故为独立最小原子）。
    _op("hp_swap", ALL_HOOKS,
        [P("event", "text", required=False)],
        logged=True),
    # apply_status：施加状态（唯一状态入口；数值参数按状态定义声明，
    # 施加时覆盖定义默认值——个性化 / 共鸣即作用于此）。
    _op("apply_status", ALL_HOOKS,
        [P("status", "text"), P("target", "enum", options=("self", "enemy"))],
        logged=True),
    # cleanse：驱散状态（scope=both 双方 / self / enemy），并按驱散种数回复。
    _op("cleanse", ("action_start",),
        [P("scope", "enum", options=("both", "self", "enemy")),
         _num("value", (0.0, None), link=True, unit="hp"),
         _num("per", (0.0, None), link=True, unit="hp")],
        logged=True),
    # skip_action：吞掉本次行动（眩晕吞行动 / 蓄力占用行动；信号原语）。
    _op("skip_action", ("on_attack", "on_owner_action"),
        [P("event", "text", required=False)],
        logged=False),
    # record：记录（what=damage_taken 所受伤害，至多 cap 条——记仇；
    # what=lifesteal 吸血量累计——血契转化）。
    _op("record",
        ("on_hit_taken", "on_owner_attack_hit", "on_status_apply"),
        [_st(),
         P("what", "enum", options=("damage_taken", "lifesteal")),
         _turns("cap", (1, 20), link=True, required=False)],
        logged=False),
    # marker：标记操作（图内自由可用的私有计数开关，免预定义）。
    # action=set/clear 置位/清除；toggle 翻转；add/sub 层数 ±value（减到 0
    # 自动清除）；turns 可选——标记存在刻数，到期整条消失（timer）。
    _op("marker", ALL_HOOKS,
        [P("key", "text"),
         P("action", "enum", options=("set", "clear", "toggle", "add", "sub")),
         _num("value", (-20.0, 20.0), required=False,
              show_if=("action", ("add", "sub"))),
         _turns("turns", (1, 40), link=True, required=False)],
        logged=False),
    # status_ctl：状态操控原子（对己方/敌方某状态运行时做延长 / 缩短 /
    # 叠层增减 / 强制清除；value 为刻数或层数增量）。
    _op("status_ctl", ALL_HOOKS,
        [_st(), P("target", "enum", options=("self", "enemy")),
         P("op", "enum", options=("extend", "shorten", "stacks", "clear")),
         _num("value", (-20.0, 20.0), link=True, required=False,
              show_if=("op", ("extend", "shorten", "stacks"))),
         P("event", "text", required=False)],
        logged=True),
    # loop：循环结构节点（struct）。mode=chain：第 1 轮必定执行（外层
    # chance 已消耗首道概率），第 i 轮（i>=2）按 decay^(i-1) 概率续链
    # （雷罚连击）；mode=count：固定执行 max 轮，不掷骰。
    ("loop", {"params": (P("max", "int", "num"),
                         _pct("decay", (0.3, 0.99), link=True, required=False,
                              show_if=("mode", ("chain",))),
                         P("mode", "enum", options=("chain", "count"),
                           required=False)),
              "text_key": "op_loop"}),
])

STRUCTS = frozenset(("loop",))            # 结构节点（控制流）注册表
OP_TYPES = frozenset(k for k, m in OPS.items() if k not in STRUCTS)
CONDITION_TYPES = frozenset(CONDITIONS)

# 共鸣展示的默认规格（未显式声明的可共鸣参数兜底，与 v1.x 一致）
DEFAULT_RESONANCE_SPEC = ("pct", 0.02, 5.0)

# 熟练度 / 词缀可作用的参数名钳制（按参数名全局生效，与 v1.x 口径一致）
CHANCE_CLAMP = (0.02, 0.95)
IMMUNE_CLAMP = (0.01, 0.5)


def param_specs(kind: str, type_: str, status_params=None):
    """取某节点的参数规格表 {参数名: ParamSpec}。
    apply_status 的数值参数由状态定义（battle.json statuses 的 params，
    status_params 回调提供）决定；其余节点由注册表决定。
    返回的 dict 保持声明顺序（个性化 / 共鸣槽位遍历顺序的依据）。"""
    if kind == "condition":
        spec = CONDITIONS.get(type_)          # 条件注册项
        params = spec["params"] if spec else ()
    elif kind == "struct":
        params = OPS[type_]["params"]          # 结构节点（loop）
    else:
        spec = OPS.get(type_)                 # 效果原子注册项
        params = spec["params"] if spec else ()
    out = {}
    for ps in params:
        out[ps.key] = ps
    if kind == "op" and type_ == "apply_status" and status_params is not None:
        # 合并状态定义声明的数值参数（status/target 之外的部分）
        for key, ps in (status_params() or {}).items():
            if key not in out:
                out[key] = ps
    return out


def node_param_spec(kind: str, type_: str, key: str, status_params=None):
    """单个参数的规格；apply_status 数值参数回落状态定义，再回落默认。"""
    specs = param_specs(kind, type_, status_params)
    ps = specs.get(key)
    if ps is not None:
        return ps
    return ParamSpec(key, "float", "num", None, None, False)


def linkable_params(kind: str, type_: str, status_params=None):
    """该节点可共鸣参数名列表（按声明顺序，槽位分配顺序的依据）。"""
    return [k for k, ps in param_specs(kind, type_, status_params).items() if ps.link]


def validate_param_value(node, ps, value):
    """按参数规格校验基配置数值；非法抛 ValueError（node 仅用于报错上下文）。
    数值参数允许表达式字符串（含 $，如 "$enemy.mark:连击 * 2"）——这里只做
    语法校验（expr.expr_check），运行期由 battle._proc_params 统一求值；
    状态效果图的 "$参数名" 引用同属表达式语法（变量平表含施加参数）。"""
    from . import expr as _expr
    key = ps.key
    if ps.kind in ("float", "pct", "int", "turns"):
        if isinstance(value, str):
            if "$" in value:
                try:
                    _expr.expr_check(value)
                except _expr.ExprError as e:
                    raise ValueError("参数 %s 的表达式非法: %s" % (key, e)) from e
                return value
            raise ValueError("参数 %s 必须是数字或 $ 表达式" % key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("参数 %s 必须是数字" % key)
        if ps.kind in ("int", "turns") and float(value) != int(value):
            raise ValueError("参数 %s 必须是整数" % key)
        if ps.kind == "turns" and int(value) < 1:
            raise ValueError("参数 %s 必须 >= 1" % key)
    elif ps.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("参数 %s 必须是布尔值" % key)
    elif ps.kind == "enum":
        if value not in (ps.options or ()):
            raise ValueError("参数 %s 取值必须是 %s 之一" % (key, "/".join(ps.options or ())))
    else:  # text
        if not isinstance(value, str) or not value:
            raise ValueError("参数 %s 必须是非空字符串" % key)
    return value


def compile_graph(graph, status_params=None, validate=True):
    """校验技能 / 状态效果图并编译为 {hook: ((node, (子树, ...)), ...)}。

    - nodes 数组顺序、edges 数组顺序即执行顺序（确定性契约）；
      条件节点的出边按 gate 分组（pass 组先于 fail 组，组内按边序）；
    - 非触发节点必须恰好有一条入边（树结构，禁止子图汇合）；
    - gate=fail 的边只能出自条件节点（分支语义）；
    - 无环；所有节点必须挂在某个触发节点之下，且原子必须挂在其注册表
      声明的挂点之下（技能图用 HOOKS，状态图用 STATUS_HOOKS）；
    - 参数键与数值按注册表规格校验（apply_status 的数值参数按状态定义）。

    status_params: 可选回调 status_id -> {参数名: ParamSpec}（config.py 注入）。
    validate=False 跳过数值校验（个性化后的图结构已在校验时确认，数值可
    因熟练度/共鸣越出基配置区间，属设计预期）。"""
    if not isinstance(graph, dict):
        raise ValueError("effect 必须是包含 nodes/edges 的对象")
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("effect.nodes 必须是非空数组")
    if not isinstance(edges, list):
        raise ValueError("effect.edges 必须是数组")

    by_id = {}          # 节点 id -> 规范化后的节点 dict
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("节点必须是对象")
        nid = node.get("id")            # 节点 id（图内唯一）
        if not isinstance(nid, str) or not nid:
            raise ValueError("节点缺少 id")
        if nid in by_id:
            raise ValueError("节点 id 重复: %s" % nid)
        kind = node.get("kind")         # 节点类别：trigger / condition / op / struct
        type_ = node.get("type")        # 具体类型：钩子名 / 条件名 / 原子名 / 结构名
        if kind not in ("trigger", "condition", "op", "struct"):
            raise ValueError("节点 %s 的 kind 非法: %r" % (nid, kind))
        if kind == "trigger":
            if type_ not in HOOKS and type_ not in STATUS_HOOKS:
                raise ValueError("触发节点 %s 的时机非法: %r" % (nid, type_))
        elif kind == "condition":
            if type_ not in CONDITIONS:
                raise ValueError("条件节点 %s 的类型未注册: %r" % (nid, type_))
        elif kind == "struct":
            if type_ not in STRUCTS:
                raise ValueError("结构节点 %s 的类型未注册: %r" % (nid, type_))
        elif type_ not in OPS or type_ in STRUCTS:
            raise ValueError("原子节点 %s 的类型未注册: %r" % (nid, type_))
        params = node.get("params", {})  # 节点参数（基配置数值）
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError("节点 %s 的 params 必须是对象" % nid)
        if kind == "trigger" and params:
            raise ValueError("触发节点 %s 不能带参数" % nid)
        if validate and kind != "trigger":
            # 注册表声明的必填参数（apply_status 的状态数值参数按需出现）
            reg = (CONDITIONS[type_]["params"] if kind == "condition"
                   else OPS[type_]["params"])
            sp = None
            if kind == "op" and type_ == "apply_status" and status_params is not None:
                sp = lambda: status_params(params.get("status"))  # noqa: E731
            specs = param_specs(kind, type_, sp)
            for key, value in params.items():
                ps = specs.get(key)
                if ps is None:
                    raise ValueError("节点 %s（%s）没有参数 %s" % (nid, type_, key))
                validate_param_value(node, ps, value)
            for ps in reg:
                if ps.required and ps.key not in params:
                    raise ValueError("节点 %s（%s）缺少参数 %s" % (nid, type_, ps.key))
        # 保留节点全部键（pos 供编辑器、links 供个性化共鸣），仅规范化 params
        node_out = dict(node)
        node_out["params"] = dict(params)
        by_id[nid] = node_out

    children = {nid: [] for nid in by_id}   # 节点 id -> [(gate, 目标id)]（按边序）
    incoming = {nid: 0 for nid in by_id}    # 节点 id -> 入边计数（树结构 = 至多 1）
    for edge in edges:
        if not isinstance(edge, dict) or "from" not in edge or "to" not in edge:
            raise ValueError("边必须是包含 from/to 的对象")
        src, dst = edge.get("from"), edge.get("to")   # 起点 / 终点节点 id
        gate = edge.get("gate", "pass")               # 边闸门：pass / fail（分支）
        if gate not in ("pass", "fail"):
            raise ValueError("边的 gate 非法: %r" % gate)
        if src not in by_id or dst not in by_id:
            raise ValueError("边引用了不存在的节点: %s -> %s" % (src, dst))
        if by_id[dst]["kind"] == "trigger":
            raise ValueError("触发节点 %s 不能有入边" % dst)
        if gate == "fail" and by_id[src]["kind"] != "condition":
            raise ValueError("gate=fail 的边只能出自条件节点: %s -> %s" % (src, dst))
        incoming[dst] += 1
        if incoming[dst] > 1:
            raise ValueError("节点 %s 有多条入边（图必须为树结构）" % dst)
        children[src].append((gate, dst))

    plan = {}          # 钩子名 -> 执行树列表（树的插入顺序 = 触发节点数组顺序）
    visited = set()    # 已访问节点（用于环检测）

    def build(nid, hook):
        """从节点 nid 深度优先构建执行树；hook 为所属触发节点的钩子名。
        子树形如 (gate, 子树)：gate 为该边的闸门（pass / fail），条件节点
        执行时按判定结果选择走哪一组子树（分支语义）。"""
        if nid in visited:
            raise ValueError("图中存在环（经 %s）" % nid)
        visited.add(nid)
        node = by_id[nid]
        if validate and node["kind"] == "op" \
                and hook not in OPS[node["type"]]["hooks"]:
            raise ValueError("原子 %s 不能挂在 %s 之下（允许: %s）"
                             % (node["type"], hook,
                                "/".join(OPS[node["type"]]["hooks"])))
        return (node, tuple((gate, build(cid, hook)) for gate, cid in children[nid]))

    for node in by_id.values():
        if node["kind"] != "trigger":
            continue
        tree = build(node["id"], node["type"])
        plan.setdefault(node["type"], []).append(tree)

    if len(visited) != len(by_id):
        missing = sorted(set(by_id) - visited)   # 未被任何触发节点覆盖的节点
        raise ValueError("存在未挂在触发节点之下的节点: %s" % "、".join(missing))
    return {hook: tuple(trees) for hook, trees in plan.items()}
