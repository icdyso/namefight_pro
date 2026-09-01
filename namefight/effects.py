"""效果组件注册表与技能图编译（v2.0.0 解耦核心）。

技能逻辑自 v2.0.0 起完全数据化：每个技能的 effect 是一张「节点 + 连边」的
有向无环图（nodes / edges），节点分三类：

- trigger    触发节点：声明执行时机（HOOKS 之一），一张图可含多个触发节点；
- condition  条件节点：概率 / 生命阈值 / 标记判定，失败则其下游子树不执行；
- op         效果节点：效果原语（OPS 注册表），沿链按序执行。

compile_graph 把图校验并编译为 {hook: (执行树, ...)}。执行顺序与随机数消耗
顺序完全由配置数组顺序确定：技能按派生顺序迭代，同一技能的多个触发节点按
其在 nodes 数组中的出现顺序、同一节点的多条出边按 edges 数组顺序展开
（确定性契约，改变即 breaking）。

本模块只保存注册表元数据与编译器；效果原语的战斗行为实现位于 battle.py
（_OP_IMPL，键与 OPS 一一对应，导入时断言）。参数规格（ParamSpec）同时驱动：
配置校验（config.py）、共鸣格式与上下限、熟练度/词缀可作用参数、以及
/api/schema（可视化编辑器的表单渲染依据）。
"""
from __future__ import annotations

from dataclasses import dataclass

# ---- 事件钩子（引擎结算时机，固定顺序；文案键 hook_<name>） ----
HOOKS = (
    "battle_start",       # 战斗开始（不屈意志注册）
    "action_interrupt",   # 敌方即将行动时（斩断）
    "action_start",       # 自己行动开始（血契 / 回春 / 净化）
    "before_attack",      # 攻击前判定（背水一战）
    "on_attack",          # 攻击链（倍率 / 蓄力 / 雷罚 / 施加状态）
    "on_defend",          # 受击防御链（壁垒 / 反甲 / 锻痕叠层）
    "on_hit_landed",      # 自己命中对方后（乘胜叠层）
    "on_hit_taken",       # 自己被命中后（怨念积攒 / 记仇记录）
    "after_action",       # 行动结束后（大器晚成成长）
)


@dataclass(frozen=True)
class ParamSpec:
    """效果参数规格。kind: float / int / pct(0~1 分数) / turns(正整数) /
    bool / text / enum；fmt 为共鸣展示格式（pct / num / turns），clamp 为
    共鸣上下限 (lo, hi)（None = 不限）；unit 为展示量纲（词缀文案用）；
    link=True 的参数可成为共鸣变数（槽位按遍历顺序分配，每技能至多
    variable_link.max_slots 个）。"""
    key: str                 # 参数名（技能图 params 字典里的键）
    kind: str = "float"      # 数值类型：float/int/pct/turns/bool/text/enum
    fmt: str = "pct"         # 共鸣展示格式：pct 百分数 / num 整数 / turns 刻数
    clamp: tuple = None      # 共鸣上下限 (lo, hi)；None 表示该端不限制
    unit: str = None         # 展示量纲：hp/def/gauge/spd/atk（词缀文案用）
    link: bool = False       # 是否可成为共鸣变数（槽位候选）
    required: bool = True    # 基配置中是否必填（如 apply_status 的 status/target）
    options: tuple = None    # kind=enum 的合法取值（如 target: self/enemy）


def _pct(key, clamp=None, link=False, unit=None):
    """百分数参数（0~1 分数存储，展示 ×100）的规格快捷构造。"""
    return ParamSpec(key, "pct", "pct", clamp, unit, link)


def _num(key, clamp=None, link=False, unit=None):
    """绝对数值参数（引擎真实值，展示取整）的规格快捷构造。"""
    return ParamSpec(key, "float", "num", clamp, unit, link)


def _turns(key, clamp=(1, 20), link=False):
    """刻数 / 次数参数（正整数，共鸣后仍至少为 1）的规格快捷构造。"""
    return ParamSpec(key, "turns", "turns", clamp, None, link)


def _st(required_kind):
    """状态引用参数（必填文本：目标状态的 id；编辑器渲染为状态下拉框）。
    required_kind 仅供文档说明，实际种类校验由注册表的 status_kind 驱动。"""
    return ParamSpec("status", "text")


P = ParamSpec  # 通用规格别名（枚举 / 布尔 / 文本参数直接用）


def _cond(type_, params):
    """条件注册项快捷构造：type_ = 条件类型名，params = 参数规格表。"""
    return type_, {"params": tuple(params), "text_key": "cond_" + type_}


# ---- 条件注册表（失败则下游子树不执行；文案键 cond_<type>） ----
CONDITIONS = dict([
    _cond("chance", [_pct("chance")]),
    _cond("self_hp_below", [_pct("threshold", (0.05, 0.9))]),
    _cond("self_hp_above", [_pct("threshold", (0.1, 0.9), link=True)]),
    _cond("target_hp_below", [_pct("threshold", (0.05, 0.9), link=True)]),
    _cond("target_hp_above", [_pct("threshold", (0.05, 0.9), link=True)]),
    _cond("has_marker", [P("marker", "text")]),
    _cond("no_marker", [P("marker", "text")]),
    _cond("once_per_battle", [P("marker", "text")]),
])


def _op(type_, hooks, params, logged, status_kind=None):
    """效果原语注册项快捷构造：hooks = 允许挂载的钩子，params = 参数规格表，
    logged = 执行时是否宣告「使用了技能」（apply_status 以状态定义的
    logged 为准）；status_kind = 携带 status 参数的原语所要求的状态行为
    种类（"apply" 表示任意可施加种类，见 statuses.APPLY_KINDS）。"""
    meta = {"hooks": tuple(hooks),
            "params": tuple(params),
            "logged": logged,
            "text_key": "op_" + type_}
    if status_kind is not None:
        meta["status_kind"] = status_kind
    return type_, meta


# ---- 效果原语注册表（行为实现见 battle.py._OP_IMPL；文案键 op_<type>） ----
OPS = dict([
    # 攻击链：修正本次攻击
    _op("prepare_charge", ("on_attack",),
        [_st("charge"), _pct("value", (0.5, 8.0), link=True),
         _pct("crit", (0.0, 1.0), link=True)],
        logged=True, status_kind="charge"),
    _op("thunder_strike", ("on_attack",),
        [_pct("value", (0.05, 1.0), link=True), _pct("decay", (0.5, 0.99), link=True),
         _pct("chain"), P("max_hits", "int")],
        logged=True),
    _op("attack_mult", ("on_attack",),
        [_pct("value", (0.1, 6.0), link=True),
         P("announce", "bool", required=False)],
        logged=True),
    _op("random_mult", ("on_attack",),
        [_pct("win"), _pct("value", (0.5, 6.0), link=True),
         _pct("penalty", (0.1, 1.0), link=True)],
        logged=True),
    _op("momentum_mult", ("on_attack",),
        [_pct("value", (0.005, 0.3), link=True), _turns("cap", link=True)],
        logged=True),
    _op("armor_pen_flat", ("on_attack",),
        [_pct("value", (0.05, 1.0), link=True)], logged=True),
    _op("armor_pen_full", ("on_attack",),
        [_pct("crit", (0.0, 1.0), link=True)], logged=False),
    _op("self_cost", ("on_attack",),
        [_pct("cost", (0.01, 0.4), link=True)], logged=True),
    # 施加 / 即时效果（status_kind="apply"：状态的行为种类须可施加）
    _op("apply_status",
        ("on_attack", "on_defend", "on_hit_landed", "on_hit_taken", "action_start"),
        [P("status", "text"), P("target", "enum", options=("self", "enemy"))],
        logged=True, status_kind="apply"),   # logged 以状态定义（battle.json statuses）为准
    _op("heal", ("action_start",),
        [_num("value", (0.0, None), link=True, unit="hp")], logged=True),
    _op("cleanse", ("action_start",),
        [_num("value", (0.0, None), link=True, unit="hp"),
         _num("per", (0.0, None), link=True, unit="hp")], logged=True),
    _op("gauge_add", ("on_attack",),
        [_num("value", (0.0, None), link=True, unit="gauge"),
         _num("crit_value", (0.0, None), link=True, unit="gauge")], logged=True),
    # 打断（斩断 = 退条 + 抢攻）
    _op("gauge_delay", ("action_interrupt",),
        [_num("delay", (0.0, None), link=True, unit="gauge")], logged=False),
    _op("quick_strike", ("action_interrupt",),
        [_pct("value", (0.1, 2.0), link=True)], logged=True),
    # 成长 / 姿态（携带 status 参数：写入哪条状态定义可编辑）
    _op("stat_gain", ("after_action",),
        [_st("tempo"),
         _num("value", (0.0, None), link=True, unit="spd"),
         _num("atk", (0.0, None), link=True, unit="atk")], logged=False,
        status_kind="tempo"),
    _op("stat_boost_once", ("before_attack",),
        [_st("boost"), _pct("value", (0.05, 3.0), link=True),
         _pct("spd", (0.05, 3.0), link=True)],
        logged=False, status_kind="boost"),
    _op("will_register", ("battle_start",),
        [_st("will"), _pct("chance"), _pct("value", (0.05, 0.9), link=True),
         _pct("decay", (0.05, 0.9), link=True)], logged=False, status_kind="will"),
    _op("pact_cost", ("action_start",),
        [_st("pact"), _pct("cost", (0.01, 0.4), link=True),
         _pct("value", (0.05, 1.5), link=True),
         _pct("convert", (0.05, 2.0), link=True)],
        logged=True, status_kind="pact"),
    _op("pact_convert", ("after_action",),
        [_st("pact")], logged=False, status_kind="pact"),
    _op("record_damage", ("on_hit_taken",),
        [_st("record"), _pct("ratio", (0.2, 3.0), link=True), _turns("cap", link=True)],
        logged=False, status_kind="record"),
    # 防御链（壁垒拆解为独立的减伤 / 免疫原语，可自由组合）
    _op("reflect_damage", ("on_defend",),
        [_pct("value", (0.05, 0.9), link=True), _pct("ratio", (0.2, 4.0), link=True)],
        logged=True),
    _op("damage_reduce", ("on_defend",),
        [_pct("value", (0.05, 0.9), link=True)], logged=False),
    _op("immune_chance", ("on_defend",),
        [_pct("immune")], logged=False),
])

OP_TYPES = frozenset(OPS)
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
    else:
        spec = OPS.get(type_)                 # 效果原语注册项
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
    """按参数规格校验基配置数值；非法抛 ValueError（node 仅用于报错上下文）。"""
    key = ps.key
    if ps.kind in ("float", "pct", "int", "turns"):
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
    """校验技能图并编译为 {hook: ((node, (子树, ...)), ...)}。

    - nodes 数组顺序、edges 数组顺序即执行顺序（确定性契约）；
    - 非触发节点必须恰好有一条入边（树结构，禁止子图汇合）；
    - 无环；所有条件/效果节点必须挂在某个触发节点之下，且效果原语
      必须挂在其注册表声明的挂点之下；
    - 参数键与数值按注册表规格校验（apply_status 的数值参数按状态定义；
      状态 id 与 target 始终必填，状态数值参数按需出现）。

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
        kind = node.get("kind")         # 节点类别：trigger / condition / op
        type_ = node.get("type")        # 具体类型：钩子名 / 条件名 / 原语名
        if kind not in ("trigger", "condition", "op"):
            raise ValueError("节点 %s 的 kind 非法: %r" % (nid, kind))
        if kind == "trigger":
            if type_ not in HOOKS:
                raise ValueError("触发节点 %s 的时机非法: %r" % (nid, type_))
        elif kind == "condition":
            if type_ not in CONDITIONS:
                raise ValueError("条件节点 %s 的类型未注册: %r" % (nid, type_))
        elif type_ not in OPS:
            raise ValueError("效果节点 %s 的原语未注册: %r" % (nid, type_))
        params = node.get("params", {})  # 节点参数（基配置数值）
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError("节点 %s 的 params 必须是对象" % nid)
        if kind == "trigger" and params:
            raise ValueError("触发节点 %s 不能带参数" % nid)
        if validate and kind != "trigger":
            # 注册表声明的必填参数（apply_status 的状态数值参数按需出现）
            reg = CONDITIONS[type_]["params"] if kind == "condition" else OPS[type_]["params"]
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

    children = {nid: [] for nid in by_id}   # 节点 id -> 出边目标列表（按 edges 顺序）
    incoming = {nid: 0 for nid in by_id}    # 节点 id -> 入边计数（树结构 = 至多 1）
    for edge in edges:
        if not isinstance(edge, dict) or "from" not in edge or "to" not in edge:
            raise ValueError("边必须是包含 from/to 的对象")
        src, dst = edge.get("from"), edge.get("to")   # 起点 / 终点节点 id
        if src not in by_id or dst not in by_id:
            raise ValueError("边引用了不存在的节点: %s -> %s" % (src, dst))
        if by_id[dst]["kind"] == "trigger":
            raise ValueError("触发节点 %s 不能有入边" % dst)
        incoming[dst] += 1
        if incoming[dst] > 1:
            raise ValueError("节点 %s 有多条入边（图必须为树结构）" % dst)
        children[src].append(dst)

    plan = {}          # 钩子名 -> 执行树列表（树的插入顺序 = 触发节点数组顺序）
    visited = set()    # 已访问节点（用于环检测）

    def build(nid, hook):
        """从节点 nid 深度优先构建执行树；hook 为所属触发节点的钩子名。"""
        if nid in visited:
            raise ValueError("图中存在环（经 %s）" % nid)
        visited.add(nid)
        node = by_id[nid]
        if validate and node["kind"] == "op" and hook not in OPS[node["type"]]["hooks"]:
            raise ValueError("效果 %s 不能挂在 %s 之下（允许: %s）"
                             % (node["type"], hook, "/".join(OPS[node["type"]]["hooks"])))
        return (node, tuple(build(cid, hook) for cid in children[nid]))

    for node in by_id.values():
        if node["kind"] != "trigger":
            continue
        tree = build(node["id"], node["type"])
        plan.setdefault(node["type"], []).append(tree)

    if len(visited) != len(by_id):
        missing = sorted(set(by_id) - visited)   # 未被任何触发节点覆盖的节点
        raise ValueError("存在未挂在触发节点之下的节点: %s" % "、".join(missing))
    return {hook: tuple(trees) for hook, trees in plan.items()}
