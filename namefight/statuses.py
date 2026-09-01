"""状态系统（v3.0.0）：状态定义 = 数据 + 自带效果图。

状态定义数据化于 battle.json 的 statuses 节，v3.0.0 起每个状态自带：

- 策略字段（引擎如何调度这个状态）：
    stack     "refresh"（默认，重复施加刷新持续与数值）/ "layers"（每施加
              追加一层，各层独立到期：锻痕 / 怨念）/ "count"（计数叠层至上限）；
    expire    "ticks"（默认，按 turns 参数持续若干刻）/ "actions"（按拥有者
              行动数衰减：嗜血）/ "none"（无期限：成长 / 姿态 / 不屈 / 蓄力）；
    interval  on_status_tick 的触发间隔（刻；数字或 "$参数名"；缺省无 tick）；
    max_stacks  count 模式的层数上限（可被施加参数覆盖；0 = 不限）；
    reset_on_miss  拥有者攻击落空时清零（乘胜）；
    lethal    {chance, value, decay}（值可 "$参数名"）：致命伤害按衰减概率
              重生——不屈（死亡拦截是引擎级原语，不可图化为原子）；
    event / death_event  施加时 / 状态结算致死时的战报模板 id；
- params   数值参数规格（fmt / clamp / link / unit / default）——施加时可被
           覆盖，个性化（熟练度 / 扰动 / 共鸣）即作用于施加节点的参数；
- mods     被动修饰表 [{kind, value, per_stack?, record?}]（value 可 "$参数名"），
           引擎在攻防聚合点按 MOD_KINDS 结算（乘胜 / 怨念增伤、锻痕减伤、
           破甲降防、背水攻速乘区、吸血比例……）；
- effects  状态效果图（与技能图同格式，钩子为 STATUS_HOOKS）：
           on_status_apply / on_status_tick / on_owner_action /
           on_owner_action_consume / on_owner_attack_hit——毒发、流血损失、
           回春回复、眩晕吞行动、蓄力释放、吸血全部是图上的原子组合，
           可在编辑器里分状态单独编辑。

运行时容器 _Combatant.st：状态 id -> {params, stacks, expires, layers,
next, actions, applier, records, total, links}（各字段按定义取用）。
"""
from __future__ import annotations

from .effects import ParamSpec, compile_graph   # noqa: F401  （config 复用）

# 被动修饰种类注册表（mods[].kind 的合法取值；引擎聚合点见 battle.py）：
# value 为 "$参数名" 时按施加参数解析；per_stack=True 时按在场层数倍乘。
MOD_KINDS = {
    "dmg_out_pct":   "拥有者造成伤害的乘区加成（乘胜 / 怨念）",
    "dmg_in_cut_pct": "拥有者所受伤害的减免（锻痕；总量钳 guard_reduction_cap）",
    "atk_pct":       "攻击乘区加成（背水一战）",
    "spd_pct":       "速度乘区加成（背水一战）",
    "atk_flat":      "攻击加值（渐入佳境，每层累计）",
    "spd_flat":      "速度加值（渐入佳境，每层累计）",
    "def_break":     "防御减值（破甲，每层累计）",
    "lifesteal_pct": "命中吸血比例（嗜血 / 血契；record=lifesteal 时记录吸血量）",
}

# 引擎会写入状态快照的状态 id（测试据此校验配置都有对应文案）
STATUS_IDS = frozenset({
    "poison", "bleed", "stun", "shred", "charge", "momentum", "grudge",
    "guard", "retribution", "last_stand", "regen", "lifesteal", "will",
    "will_used", "tempo", "blood_pact",
})

# 状态钩子（与 effects.STATUS_HOOKS 一致；config 校验状态效果图用）
STATUS_HOOKS = ("on_status_apply", "on_status_tick", "on_owner_action",
                "on_owner_action_consume", "on_owner_attack_hit")


def resolve(value, params):
    """解析修饰 / 间隔等字段的取值：数字原样返回，"$参数名" 查施加参数。"""
    if isinstance(value, str) and value.startswith("$"):
        return params.get(value[1:], 0.0)
    return value


def new_runtime():
    """状态运行时条目的通用骨架（各字段按定义取用）。"""
    return {"params": {},      # 施加时合并的参数（含个性化 / 共鸣结果）
            "stacks": 0,       # count 模式层数
            "expires": 0,      # refresh / count 模式的到期刻
            "layers": [],      # layers 模式各层的到期刻列表
            "next": 0,         # on_status_tick 的下次触发刻
            "actions": 0,      # expire=actions 的剩余行动数
            "applier": None,   # 施加者引用（撕裂按施加者攻击折算用）
            "records": [],     # record 原子的记录（记仇：所受伤害）
            "total": 0.0,      # stat_mod 的累计展示（血契转化攻击）
            "links": []}       # 施加节点的共鸣声明（蓄力释放时重算）


def ensure(c, sid: str) -> dict:
    """取（或创建）状态的运行时容器。"""
    st = c.st.get(sid)
    if st is None:
        st = new_runtime()
        c.st[sid] = st
    return st


def live(c, sid: str, tick: int, sdef: dict) -> bool:
    """状态当前是否在场（按定义的到期方式判定；layers 顺带清理过期层）。"""
    st = c.st.get(sid)
    if st is None:
        return False
    if sdef.get("expire") == "actions":
        return st["actions"] > 0
    if sdef.get("stack") == "layers":
        st["layers"] = [t for t in st["layers"] if t > tick]
        return bool(st["layers"])
    if sdef.get("expire") == "none":
        # 无期限状态：有施加参数 / 层数 / 记录 / 累计值之一即视为在场
        # （持有器类状态如记仇、血契展示依赖后两者）
        return (bool(st["params"]) or st["stacks"] > 0
                or bool(st["records"]) or st["total"] > 0)
    return st["expires"] > tick


def live_stacks(c, sid: str, tick: int, sdef: dict) -> int:
    """在场层数：refresh 语义为 1、count 为计数、layers 为存活层数。"""
    st = c.st.get(sid)
    if st is None or not live(c, sid, tick, sdef):
        return 0
    if sdef.get("stack") == "layers":
        return len(st["layers"])
    if sdef.get("stack") == "count":
        return st["stacks"]
    return 1


def each_live(c, game, tick: int):
    """遍历战斗者身上全部在场状态 [(状态id, 运行时dict, 定义dict), ...]，
    施加顺序（= dict 插入顺序）即结算顺序（确定性保证）。"""
    out = []
    for sid, st in c.st.items():
        sdef = game.statuses.get(sid)
        if sdef is not None and live(c, sid, tick, sdef):
            out.append((sid, st, sdef))
    return out


def sum_mod(c, game, tick: int, kind: str) -> float:
    """聚合某种被动修饰的总量（乘区类相加后由调用方 +1，如 _eff_atk 的
    (1 + Σatk_pct)）；per_stack=True 的修饰按在场层数倍乘。
    lifesteal_pct 的 record 标志在 battle.py 吸血结算处另行处理。"""
    total = 0.0
    for sid, st, sdef in each_live(c, game, tick):
        n = live_stacks(c, sid, tick, sdef)
        if n <= 0:
            continue
        for m in sdef.get("mods") or ():
            if m.get("kind") != kind:
                continue
            v = float(resolve(m.get("value", 0.0), st["params"]))
            total += v * (n if m.get("per_stack") else 1)
    return total


def status_display(c, tick: int, guard_cap: float, game) -> list:
    """把在场状态渲染为快照 buff 条目 [{id, params}, ...]。
    展示 id = 状态定义 id（含 lethal 块的状态已触发过时展示为 <id>_used）。
    params 供 detail 模板渲染：turns 剩余 / stacks 层数 / value 等施加参数
    （按 fmt 格式化）/ mult 乘区类修饰的最终倍率。"""
    from .text import format_num, format_pct

    out = []
    for sid, st, sdef in each_live(c, game, tick):
        n = live_stacks(c, sid, tick, sdef)
        params = {"stacks": n}
        if sdef.get("expire") == "actions":
            params["turns"] = st["actions"]
        elif sdef.get("stack") == "layers":
            params["turns"] = (max(st["layers"]) - tick) if st["layers"] else 0
        elif sdef.get("expire") not in ("none",):
            params["turns"] = max(0, st["expires"] - tick)
        # 施加参数按定义的 fmt 格式化（毒伤 / 破甲量 / 吸血比例……）
        for key, val in st["params"].items():
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                fmt = ((sdef.get("params") or {}).get(key) or {}).get("fmt", "num")
                params[key] = format_pct(float(val)) if fmt == "pct" \
                    else format_num(float(val))
        if st["total"]:
            params["total"] = format_num(st["total"])
        if st["records"]:
            params["stacks"] = len(st["records"])
            params["value"] = format_num(sum(st["records"]))
        # 乘区 / 减免类修饰的最终倍率（「伤害倍率 132%」/「减伤 21%」类展示）
        for m in sdef.get("mods") or ():
            mk = m.get("kind")
            if mk not in ("dmg_out_pct", "dmg_in_cut_pct"):
                continue
            v = float(resolve(m.get("value", 0.0), st["params"]))
            per = n if m.get("per_stack") else 1
            if mk == "dmg_in_cut_pct":
                params["value"] = format_pct(min(guard_cap, v * per))
            else:
                params["mult"] = format_pct(1.0 + v * per)
        # 不屈类已触发过展示为 <id>_used（与 v1.x 口径一致；引擎置 marker）
        display_id = sid
        if sdef.get("lethal") and ("will_used:" + sid) in c.markers:
            display_id = sid + "_used"
        out.append({"id": display_id, "params": params})
    return out


def dispel_all(combatants, tick: int, game) -> int:
    """驱散双方所有可驱散状态（按定义的 dispellable 标记；成长 / 已转化 /
    姿态 / 不屈类不可驱散）。返回驱散的状态种数（供净化回复计算）。"""
    count = 0
    for c in combatants:
        if c.hp <= 0:
            continue
        for sid, st in list(c.st.items()):
            sdef = game.statuses.get(sid)
            if sdef is None or not sdef.get("dispellable", False):
                continue
            if not live(c, sid, tick, sdef):
                continue
            st["expires"] = 0
            st["layers"] = []
            st["stacks"] = 0
            st["actions"] = 0
            st["records"] = []
            count += 1
    return count


def clear_stacks(c, game, tick: int):
    """清零全部带 reset_on_miss 标记的状态（攻击落空：乘胜清零）。"""
    for sid, st, sdef in each_live(c, game, tick):
        if not sdef.get("reset_on_miss"):
            continue
        st["stacks"] = 0
        st["expires"] = 0
        st["layers"] = []


def status_param_specs(status_entry: dict):
    """状态定义（battle.json statuses 条目）-> {参数名: ParamSpec}。
    参数的 fmt / clamp / link / unit 由定义声明，kind 按名称约定。"""
    out = {}
    for key, spec in (status_entry.get("params") or {}).items():
        fmt = str(spec.get("fmt", "num"))
        clamp = spec.get("clamp")
        clamp = (None if clamp is None else
                 (float(clamp[0]), None if clamp[1] is None else float(clamp[1])))
        kind = "turns" if fmt == "turns" else ("float" if fmt == "num" else "pct")
        out[str(key)] = ParamSpec(str(key), kind, fmt, clamp,
                                  spec.get("unit"), bool(spec.get("link", False)))
    return out


def status_defaults(status_entry: dict) -> dict:
    """状态定义声明的参数默认值（施加未覆盖时使用）。"""
    return {k: v.get("default") for k, v in (status_entry.get("params") or {}).items()
            if v.get("default") is not None}


def compile_status_effects(status_entry: dict, status_params=None):
    """编译状态定义的 effects 图（钩子为 STATUS_HOOKS；编译器共用）。
    无节点（纯 mods / 持有器类状态）返回空计划。"""
    graph = status_entry.get("effects") or {}
    if not (graph.get("nodes") or []):
        return {}
    return compile_graph(graph, status_params, validate=True)
