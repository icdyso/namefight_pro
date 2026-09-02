"""斗士派生：名字 -> MD5 -> 属性 / 技能 / 称号。

确定性契约（AGENTS.md 2.1.1）：派生结果是 (归一化名字, 配置快照) 的纯函数。

- 主派生 PRNG 消耗顺序固定（v0.9.1）：属性（配置顺序，三角形分布投掷）-> 技能数量
  -> 技能抽取 -> 称号结构 -> 称号字段（按结构字段顺序）。
- 属性在 [min, max] 内三角形分布投掷（两个均匀数取均值，中点密度最高，
  天然不越界、端点无截断堆积，v1.1.0 起替代正态投掷）；
  命/攻为 ×100 整数量纲（命 20000 / 攻 1500），防御 750（v1.0.0 减半），
  速度同为 ×100 量纲（v1.2.1 起，~1000，与行动槽阈值 10000 配套）；
  投掷结果**取整**；crit/dodge 为百分数，保持浮点；
  全部数值**直接以引擎真实值显示**（不再换算白板 100 单位）。
- 技能个性化（熟练度/数值/词缀/变数随 MD5 扰动）使用独立种子
  md5(规范化名字 + ":" + 技能id)，与主派生流互不影响；熟练度为
  [0,100] 的三角形分布投掷（v1.1.0 起）。
- v2.0.0 起技能为「节点 + 连边」的技能图（见 effects.py）：个性化直接作用
  于图的节点参数，消耗顺序固定：熟练度 -> value -> damage -> 前缀
  (是否 -> 抽取 -> 缩放) -> 后缀(同前) -> 共鸣槽位（按节点数组顺序 ×
  参数声明顺序，至多 variable_link.max_slots 个）。
- 共鸣的展示格式 / 上下限 / 量纲全部来自注册表元数据（effects.OPS /
  CONDITIONS 与 battle.json statuses 的参数规格），不再维护独立的
  规格表；技能描述改为「触发词 + 条件从句 + 效果原语句」沿链组合。
- 文案（技能名/属性名/称号字段名等）与数值自 v0.10.0 起合并在
  config/game 同一条目内保存，本模块直接从 GameCfg 读取。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import effects
from .config import GameCfg, TITLE_FIELD_POOLS
from .rng import DetRng
from .text import format_num, format_pct, render_template

# 可共鸣参数名（注册表声明 link=True 的参数；状态定义声明的可共鸣参数
# 与之同名，量纲/格式在 battle.json statuses 中声明）
_LINK_FIELD_KEYS = set()
for _reg in (effects.CONDITIONS, effects.OPS):
    for _meta in _reg.values():
        for _ps in _meta["params"]:
            if _ps.link:
                _LINK_FIELD_KEYS.add(_ps.key)

# 技能参数标签模板键（测试据此校验配置都有对应文案；st_<状态id> 由
# apply_status 引用的状态动态校验，不在此列）
STATS_KEYS_USED = frozenset(
    {"link_sep", "link_formula", "link_expr_difference", "link_expr_sum",
     "link_ratio", "link_difference", "link_sum",
     "scope_own", "scope_enemy", "scope_difference", "scope_sum",
     "mod_chance", "mod_value", "mod_turns",
     "final_damage", "final_turns",
     "mastery_text", "mastery_text_value", "mastery_text_immune",
     "op_hp_mod_loss"}
    # 状态定义声明的可共鸣参数（turns 等）不在注册表内，手工补齐；
    # apply_status 的描述模板为 st_<状态id>，无通用 op 模板
    | {"field_turns"}
    | {"field_" + k for k in _LINK_FIELD_KEYS}
    | {"hook_" + h for h in effects.HOOKS}
    | {"cond_" + c for c in effects.CONDITIONS}
    | {"op_" + o for o in effects.OPS if o != "apply_status"}
    # compare 条件的值源 / 运算词（数据驱动于 stats）
    | {"cmp_" + op for op in effects.CMP_OPS}
    | {"cmp_" + src for src in effects.CMP_SOURCES}
    | {"cmp_const"}
    # marker 原子的操作词（数据驱动于 stats）
    | {"lbl_marker_" + a for a in ("set", "clear", "toggle", "add", "sub")}
)

# 对战实时技能数据的占位符：live 文本中每个共鸣数值位 = 该标记 + 槽位序号，
# 序号 = 该变数在技能全部 links（按节点数组顺序展平）中的下标
# （与 link_calc 数组下标一致）。前端按序号（而非占位符在文本中的位置）
# 取值后替换，避免模板参数顺序与共鸣槽位顺序不一致时数值交叉错位
# （v1.2.0 修复）。每技能至多 variable_link.max_slots 个占位符。
LIVE_MARKER = "\x01"


class InvalidName(Exception):
    """名字不合法。code 用于映射错误文案。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Fighter:
    name: str            # 原始输入（仅用于展示）
    normalized: str      # 归一化名字（MD5 与对战种子的依据）
    digest: str          # md5 hex
    attrs: dict          # 属性 id -> 三角形分布投掷值（浮点）；crit/dodge 以百分数存储
    skill_ids: tuple     # 按抽取顺序
    title_structure_id: str
    title_fields: dict   # 字段名 -> 字段 id
    power: int


def normalize_name(raw, system) -> str:
    """按 system 配置归一化名字（trim / 大小写折叠）。"""
    name = raw if isinstance(raw, str) else ""
    if system.name_trim:
        name = name.strip()
    if not system.name_case_sensitive:
        name = name.lower()
    if len(name) < system.name_min_length:
        raise InvalidName("empty_name")
    if len(name) > system.name_max_length:
        raise InvalidName("name_too_long")
    return name


def derive_fighter(raw_name, game: GameCfg) -> Fighter:
    normalized = normalize_name(raw_name, game.system)
    digest = hashlib.md5(normalized.encode("utf-8")).hexdigest()
    rng = DetRng(int(digest, 16))

    # 属性三角形分布投掷：[min, max] 内（两均匀数取均值，中点密度最高），
    # 消耗顺序 = 配置顺序。v0.10.0：非百分比属性为 ×100 整数量纲，投掷即取整；
    # crit/dodge 为百分数，保持浮点
    attrs = {}
    for a in game.attributes:
        roll = rng.next_triangular(a.min, a.max)
        attrs[a.id] = round(roll) if a.format != "percent" else roll

    count = rng.next_triangular_range(game.skill_count_min, game.skill_count_max)
    skills = rng.sample_weighted(((s, s.weight) for s in game.skills), count)

    structure = rng.pick_weighted((s, s.weight) for s in game.title_structures)
    title_fields = {}
    core_id = None
    for fname in structure.fields:
        pool = game.title_pools[TITLE_FIELD_POOLS[fname]]
        candidates = pool
        if fname == "core2" and core_id is not None:
            filtered = [t for t in pool if t.id != core_id]
            candidates = filtered or pool
        item = rng.pick_weighted((t, t.weight) for t in candidates)
        title_fields[fname] = item.id
        if fname == "core":
            core_id = item.id

    # 称号字段小额加成（不消耗随机数，纯查表）
    for attr_id, delta in title_bonus_items(title_fields, structure, game):
        attrs[attr_id] = max(1.0, attrs[attr_id] + delta)

    power = round(sum(attrs[a.id] * a.power_weight for a in game.attributes))
    return Fighter(
        name=raw_name if isinstance(raw_name, str) and raw_name else normalized,
        normalized=normalized, digest=digest,
        attrs=attrs,
        skill_ids=tuple(s.id for s in skills),
        title_structure_id=structure.id, title_fields=title_fields, power=power,
    )


# ---- 节点参数规格 / 量纲查询（注册表驱动） ----

def _node_specs(node: dict, game: GameCfg):
    """节点参数规格表（apply_status 按状态定义展开）。"""
    if node.get("kind") == "op" and node.get("type") == "apply_status":
        sid = node.get("params", {}).get("status")
        return effects.param_specs("op", "apply_status",
                                   lambda: game.status_specs.get(sid))
    return effects.param_specs(str(node.get("kind")), str(node.get("type")))


def _param_spec(node: dict, param: str, game: GameCfg):
    """单参数的 (fmt, lo, hi)；未声明回落默认规格。"""
    ps = _node_specs(node, game).get(param)
    if ps is not None and (ps.fmt or ps.clamp):
        lo, hi = (ps.clamp or (None, None))
        return (ps.fmt, lo, hi)
    return effects.DEFAULT_RESONANCE_SPEC


def _param_unit(node: dict, param: str, game: GameCfg):
    ps = _node_specs(node, game).get(param)
    return ps.unit if ps is not None else None


def graph_param_unit(pgraph: dict, param: str, game: GameCfg):
    """技能图内首个该名参数的量纲（词缀文案展示用；None = 纯倍率/百分比）。"""
    for node in pgraph.get("nodes", ()):
        if param in node.get("params", {}):
            return _param_unit(node, param, game)
    return None


def graph_param_value(pgraph: dict, param: str, default=0.0):
    """技能图内首个该名参数的值（熟练度文案等展示用）。"""
    for node in pgraph.get("nodes", ()):
        params = node.get("params", {})
        if param in params:
            return params[param]
    return default


def _walk_links(pgraph: dict):
    """按节点数组顺序展平全部共鸣链接（槽位顺序的唯一次序依据）。"""
    out = []
    for node in pgraph.get("nodes", ()):
        for link in node.get("links") or ():
            out.append((node, link))
    return out


def _apply_modifier(nodes: list, mod: dict) -> None:
    """词缀修正：仅作用于节点已有的参数（chance 截断到 [0.02, 0.95]，
    turns/ticks/cap 计数至少 1）。"""
    for node in nodes:
        params = node.get("params", {})
        for key, delta in mod.items():
            if key not in params or isinstance(params[key], str):
                continue          # 表达式参数不被词缀改写
            if key == "chance":
                params["chance"] = min(0.95, max(0.02, float(params["chance"]) + float(delta)))
            elif key in ("turns", "ticks", "cap"):
                params[key] = max(1, int(round(float(params[key]))) + int(round(float(delta))))
            else:
                params[key] = float(params[key]) + float(delta)


def personalized_effects(fighter: Fighter, game: GameCfg):
    """技能个性化：以 md5(规范化名字:技能id) 为种子做确定性扰动。

    消耗顺序固定（v0.9.0 起，v2.0.0 作用于技能图节点参数）：
    熟练度 -> value -> damage -> 前缀(是否 -> 抽取 -> 缩放) -> 后缀(是否 -> 抽取 -> 缩放)
    -> 共鸣槽位（节点数组顺序 × 参数声明顺序，每槽位：是否 -> 模式 -> 变量 -> 倍率）。

    熟练度（0~100）按技能各自区间缩放 mastery_on 声明的参数（默认触发概率；
    同名参数全部缩放，chance/immune 按惯例钳制）；value/damage 按节点数组
    顺序逐个抽取倍率（同名参数各一次）。

    返回 [(SkillDef, 个性化后的技能图dict), ...]，顺序与 fighter.skill_ids 一致。
    """
    var = game.skill_md5_variance
    link_cfg = game.skill_variable_link
    name_mod = game.skill_name_modifiers
    out = []
    for sid in fighter.skill_ids:
        sdef = next(s for s in game.skills if s.id == sid)
        nodes = []
        for n in sdef.effect.get("nodes", []):
            node = {"id": n["id"], "kind": n["kind"], "type": n["type"],
                    "params": dict(n.get("params") or {})}
            if "pos" in n:
                node["pos"] = n["pos"]
            nodes.append(node)
        graph = {"nodes": nodes,
                 "edges": [dict(e) for e in sdef.effect.get("edges", [])]}
        seed_hex = hashlib.md5((fighter.normalized + ":" + sid).encode("utf-8")).hexdigest()
        rng = DetRng(int(seed_hex, 16))
        # 熟练度：[0,100] 三角形分布投掷（集中于 50），按技能区间换算为倍率
        mastery = rng.next_triangular_range(0, 100)
        lo, hi = sdef.mastery
        mult = lo + (hi - lo) * mastery / 100.0
        graph["mastery"] = mastery
        graph["mastery_mult"] = mult
        for param in sdef.mastery_on:
            for node in nodes:
                params = node["params"]
                if param not in params or isinstance(params[param], str):
                    continue          # 表达式参数不参与数值缩放（精确控制）
                scaled = float(params[param]) * mult
                if param == "chance":
                    params["chance"] = min(0.95, max(0.02, scaled))
                elif param == "immune":
                    params["immune"] = min(0.5, max(0.01, scaled))
                else:
                    params[param] = scaled
        for key in ("value", "damage"):
            for node in nodes:
                params = node["params"]
                if key in params and not isinstance(params[key], str):
                    factor = rng.next_triangular(var.value_lo, var.value_hi)
                    params[key] = float(params[key]) * factor
        if name_mod.prefix_chance > 0 and rng.next_float() < name_mod.prefix_chance:
            graph["prefix"] = rng.pick_weighted((m, m.weight) for m in name_mod.prefixes).id
            graph["prefix_scale"] = rng.next_triangular(name_mod.scale_lo, name_mod.scale_hi)
        if name_mod.suffix_chance > 0 and rng.next_float() < name_mod.suffix_chance:
            graph["suffix"] = rng.pick_weighted((m, m.weight) for m in name_mod.suffixes).id
            graph["suffix_scale"] = rng.next_triangular(name_mod.scale_lo, name_mod.scale_hi)
        for pool, mod_id, scale_key in ((name_mod.prefixes, graph.get("prefix"), "prefix_scale"),
                                        (name_mod.suffixes, graph.get("suffix"), "suffix_scale")):
            if not mod_id:
                continue
            mdef = next((m for m in pool if m.id == mod_id), None)
            if mdef is not None:
                scaled = {k: v * float(graph.get(scale_key, 1.0)) for k, v in mdef.mod.items()}
                _apply_modifier(nodes, scaled)
        # 共鸣槽位：候选 = 各节点 link=True 的参数（节点数组顺序 × 声明顺序）。
        # v3.2.0 起共鸣与表达式统一：槽位直接生成表达式字符串写入参数
        # （基数 × (1 + 率 × 变量式 / 基准)），运行期与手写表达式走同一条
        # 求值路径；links 保留为显示元数据（公式括号 / 尾句 / live 占位 /
        # link_calc 前端实时重算），base 记录共鸣前的个性化基数。
        # v3.5.0 起技能可声明 resonance 覆盖表：声明的参数固定绑定
        # （不掷概率、不抽变量，编辑器可编辑），未声明的照旧随机；覆盖与
        # 随机共享 max_slots 上限。带 node 的条目精确锚定节点，无 node 的
        # 命中图内首个该名可共鸣参数。
        override_nodes = {(r["node"], r["param"]): i
                          for i, r in enumerate(sdef.resonance) if r["node"]}
        by_param = {r["param"]: i for i, r in enumerate(sdef.resonance)
                    if not r["node"]}
        consumed = set()                  # 已消耗的覆盖条目下标
        slots = 0
        for node in nodes:
            if slots >= link_cfg.max_slots:
                break
            specs = _node_specs(node, game)
            candidates = [k for k, ps in specs.items() if ps.link
                          and effects.spec_applicable(list(specs.values()),
                                                      node["params"], ps)]
            if not candidates:
                continue
            node_id = str(node.get("id", ""))
            links = []
            for param in candidates:
                if slots >= link_cfg.max_slots:
                    break
                if param not in node["params"]:
                    continue
                if isinstance(node["params"][param], str):
                    continue          # 表达式参数不参与共鸣（作者精确控制）
                ov_i = override_nodes.get((node_id, param),
                                          by_param.get(param))
                ov = sdef.resonance[ov_i] if ov_i is not None else None
                if ov is not None and ov_i not in consumed:
                    consumed.add(ov_i)
                    if not ov["variable"]:
                        continue          # 禁用共鸣：该参数永不绑定
                    mode = ov["mode"]
                    vdef = next((v for v in link_cfg.variables
                                 if v.id == ov["variable"]), None)
                    if vdef is None:
                        continue
                    rate = float(ov["rate"])
                else:
                    if rng.next_float() >= link_cfg.chance:
                        continue
                    mode = rng.pick_weighted(link_cfg.mode_weights)
                    vdef = rng.pick_weighted((v, v.weight)
                                             for v in link_cfg.variables)
                    rate = rng.next_triangular(vdef.rate_lo, vdef.rate_hi)
                base_value = max(1.0, float(game.attr(vdef.id).base))
                var_expr = {"own": "$self." + vdef.id,
                            "enemy": "$enemy." + vdef.id}.get(mode)
                if var_expr is None:          # difference / sum：与参照属性运算
                    against = vdef.diff_against
                    var_expr = "($self.%s %s $enemy.%s)" % (
                        vdef.id, "-" if mode == "difference" else "+", against)
                base = float(node["params"][param])
                node["params"][param] = \
                    "(%.17g) * (1 + (%.17g) * ((%s) / %.17g))" % (
                        base, rate, var_expr, base_value)
                links.append({"param": param, "variable": vdef.id,
                              "rate": rate, "mode": mode, "base": base})
                slots += 1
            if links:
                node["links"] = links
        out.append((sdef, graph))
    return out


def resonance_coeff(own_get, enemy_get, link: dict, game: GameCfg) -> float:
    """归一化共鸣系数（不改变技能逻辑，只按比例修正技能自身参数）：

    - own / enemy：coeff = rate × (该方[变量]当前值 ÷ 变量基础值)
    - difference：coeff = rate × ((己方[变量] − 敌方[参照]) ÷ 变量基础值)，可为负
    - sum：       coeff = rate × ((己方[变量] + 敌方[参照]) ÷ 变量基础值)

    own_get/enemy_get 为「变量id -> 数值」的取值函数；基础值归一化保证
    不同量纲的变量产生可比的修正幅度。
    """
    vid = link.get("variable")
    try:
        base = max(1, game.attr(vid).base)
    except Exception:
        base = 1
    rate = float(link.get("rate", 0.0))
    mode = link.get("mode", "own")
    if mode in ("difference", "sum"):
        vdef = next((v for v in game.skill_variable_link.variables if v.id == vid), None)
        against = vdef.diff_against if vdef else vid
        raw = float(own_get(vid))
        other = float(enemy_get(against))
        raw = raw - other if mode == "difference" else raw + other
    elif mode == "enemy":
        raw = float(enemy_get(vid))
    else:
        raw = float(own_get(vid))
    return rate * (raw / base)


def apply_resonance(params: dict, coeff: float, param: str, spec) -> dict:
    """按共鸣系数缩放目标参数，返回新的参数 dict（按规格截断/取整）。
    spec = (fmt, lo, hi)，来自注册表 / 状态定义的参数规格。"""
    scaled = dict(params)
    if param not in scaled:
        return scaled
    fmt, lo, hi = spec
    value = float(scaled[param]) * (1.0 + coeff)
    if fmt == "turns":
        scaled[param] = max(1, int(round(value)))
        if hi is not None:
            scaled[param] = min(int(hi), scaled[param])
        return scaled
    if lo is not None and value < lo:
        value = lo
    if hi is not None and value > hi:
        value = hi
    scaled[param] = value
    return scaled


def format_field(value, fmt: str) -> str:
    """共鸣字段统一展示：pct 两位百分数、num 整数、turns 整数。"""
    if fmt == "turns":
        return str(int(round(float(value))))
    if fmt == "num":
        return format_num(float(value))
    return format_pct(float(value))


def _sig_decimals(v: float, cap: int = 6) -> int:
    """保留两位有效数字所需的小数位数（至少 2 位、至多 cap 位）。"""
    v = abs(float(v))
    decimals = 2
    while decimals < cap and v * (10 ** decimals) < 10:
        decimals += 1
    return decimals


def _format_formula_number(display_value: float, bracket: bool) -> str:
    """共鸣公式内数值展示（v1.2.0）：

    默认保留两位小数（如 6.31 / 0.42%）；当数值低于 0.1 时「保留两位小数」
    会吞掉全部有效位变成 0.00——此时改为「保留两位有效数字」：
    - bracket=True（百分数字段括号内的纯数字，正常时不带 %）：
      数值 ×100 并加 %，如 0.00209 -> 0.21%；
    - bracket=False（已是百分数形式的系数，如数值字段的合并系数）：
      仅加深小数位，如 0.00209 -> 0.0021%。"""
    if abs(display_value) >= 0.1:
        return ("%.2f" if bracket else "%.2f%%") % display_value
    if bracket:
        p = display_value * 100.0
        return "%.*f%%" % (_sig_decimals(p), p)
    return "%.*f%%" % (_sig_decimals(display_value), display_value)


def format_resonance_final(scaled_value, fmt: str, game: GameCfg = None) -> str:
    """共鸣后目标参数的展示值（引擎真实值）；game 为 None 时不带单位词。"""
    text = format_field(scaled_value, fmt)
    if game is None:
        return text
    if fmt == "num":
        return render_template(game.stats.get("final_damage", "{v}"),
                               {"v": text}, game)
    if fmt == "turns":
        return render_template(game.stats.get("final_turns", "{v}"),
                               {"v": text}, game)
    return text


def estimated_resonanced_eff(fighter: Fighter, pgraph: dict, game: GameCfg):
    """卡牌展示用的共鸣估算：敌方取基础值（实际战斗中按当前值动态计算）。
    返回 (各节点展示参数 [(node, params), ...] 按节点数组顺序, [(link, 系数), ...])。"""
    base_get = lambda vid: game.attr(vid).base  # noqa: E731
    own_get = lambda vid: fighter.attrs.get(vid, 0)  # noqa: E731
    display = []
    coeffs = []
    for node in pgraph.get("nodes", ()):
        params = node.get("params", {})
        links = node.get("links")
        if not links:
            display.append((node, params))
            continue
        disp = dict(params)
        for link in links:
            param = str(link.get("param"))
            if param not in disp:
                continue
            # v3.2.0：共鸣参数在派生期已写为表达式，此处按显示口径还原
            # 基数（link.base）再做共鸣估算
            disp[param] = float(link.get("base", disp[param]))
            coeff = resonance_coeff(own_get, base_get, link, game)
            disp = apply_resonance(disp, coeff, param, _param_spec(node, param, game))
            coeffs.append((link, coeff))
        display.append((node, disp))
    return display, coeffs


def title_bonus_items(title_fields, structure, game: GameCfg):
    """按结构字段顺序展开称号加成 [(属性id, 加成值), ...]（不去重，直接叠加）。"""
    items = []
    for fname in structure.fields:
        fid = title_fields.get(fname)
        pool = game.title_pools[TITLE_FIELD_POOLS[fname]]
        fdef = next((t for t in pool if t.id == fid), None)
        if fdef is None:
            continue
        for attr_id, delta in fdef.bonus.items():
            items.append((attr_id, int(delta)))
    return items


# ---- 技能描述：沿链组合「触发词 + 条件从句 + 效果原语句」 ----

def _display_param(node: dict, params: dict, key: str, game: GameCfg) -> str:
    fmt = _param_spec(node, key, game)[0]
    return format_field(params.get(key, 0.0), fmt)


def _node_clause(node: dict, disp: dict, game: GameCfg):
    """单个节点的描述（条件从句 / 原子句 / 结构句）模板键。
    hp_mod 按 type（治疗 / 流失）与基准（固定量 / 最大生命比例）取模板。"""
    if node["kind"] == "condition":
        return "cond_" + node["type"]
    if node["kind"] == "op" and node["type"] == "apply_status":
        return "st_" + str(disp.get("status", ""))
    if node["kind"] == "condition" and node["type"] == "has_marker" \
            and ("op" in disp or "count" in disp):
        return "cond_has_marker_count"           # 标记层数比较形态
    if node["kind"] == "op" and node["type"] == "hp_mod":
        if str(disp.get("type")) == "loss":
            return "op_hp_mod_loss"
        if "ratio" in disp and str(disp.get("basis")) == "maxhp":
            return "op_hp_mod_pct"             # 比例治疗（不屈重生）
    if node["kind"] == "op" and node["type"] == "strike" \
            and str(disp.get("basis", "none")) != "none":
        return "op_strike_basis"               # 附加伤害型打击（记仇释放 / 反弹）
    if node["kind"] == "op" and node["type"] == "hit_mod" and "mult" not in disp:
        return "op_hit_mod_pen_crit" if "crit_bonus" in disp else "op_hit_mod_pen"
    if node["kind"] == "op" and node["type"] == "stat_mod" \
            and str(disp.get("basis", "flat")) == "recorded_lifesteal":
        return "op_stat_mod_recorded"          # 按吸血量转化（血契）
    return "op_" + node["type"]


def _clause_params(node: dict, disp: dict, game: GameCfg) -> dict:
    """模板参数：数值按规格格式化（共鸣公式由调用方注入）；
    状态 id / 属性 id 等引用参数替换为显示名；hp_mod 的展示值取
    value（固定量）或 ratio（比例）中实际存在的一个。"""
    out = {}
    specs = _node_specs(node, game)
    for key in disp:
        ps = specs.get(key)
        fmt = ps.fmt if ps is not None and ps.fmt else "num"
        if isinstance(disp[key], bool):
            out[key] = disp[key]
        elif isinstance(disp[key], str):
            if key == "status":
                out[key] = str(game.statuses.get(disp[key], {})
                               .get("name", disp[key]))
            elif key == "stat":
                try:
                    out[key] = game.attr(str(disp[key])).name
                except Exception:
                    out[key] = disp[key]
            else:
                out[key] = disp[key]
        else:
            out[key] = format_field(disp[key], fmt)
    if node.get("kind") == "op" and node.get("type") == "hp_mod" \
            and "value" not in out and "ratio" in out:
        out["value"] = out["ratio"]          # 比例基准的流失也用 {value} 位展示
    if node.get("kind") == "op" and node.get("type") == "strike" \
            and str(disp.get("basis", "none")) != "none":
        out["basis_word"] = str(game.stats.get(
            "lbl_basis_" + str(disp.get("basis")), disp.get("basis")))
    if node.get("kind") == "condition" and node.get("type") == "compare":
        # compare 条件：值源与运算替换为显示词（「自身生命比例 ≤ 33%」；
        # 右值取常数时直接展示数值本身，不加「常数」前缀）
        stats_words = game.stats
        for key in ("left", "right"):
            src = str(disp.get(key, ""))
            word = stats_words.get("cmp_" + src, src)
            out[key] = out.get("value", "") \
                if src == "const" and key == "right" else word
        out["op"] = str(stats_words.get("cmp_" + str(disp.get("op", "ge")),
                                        disp.get("op", "ge")))
    if node.get("kind") == "condition" and node.get("type") == "stacks_cmp":
        sid = str(disp.get("status", ""))
        out["status"] = str(game.statuses.get(sid, {}).get("name", sid))
        out["op"] = str(game.stats.get("cmp_" + str(disp.get("op", "ge")),
                                        disp.get("op", "ge")))
    if node.get("kind") == "struct" and node.get("type") == "loop":
        # loop 的模式词：chain 带 {decay} 展示位（共鸣注入由 render 重建）
        mode = str(disp.get("mode", "chain"))
        tmpl = str(game.stats.get("cmp_mode_" + mode, ""))
        decay = out.get("decay", format_field(disp.get("decay", 0.9), "pct"))
        out["mode_word"] = tmpl.replace("{decay}", str(decay))
    if node.get("kind") == "op" and node.get("type") == "marker":
        out["action_word"] = str(game.stats.get(
            "lbl_marker_" + str(disp.get("action", "set")),
            disp.get("action", "set")))
    if node.get("kind") == "op" and node.get("type") == "hp_mod":
        # 体力变动：治疗模板带目标词（「回复自身 / 回复敌方」）
        out["target_word"] = str(game.stats.get(
            "cmp_target_" + str(disp.get("target", "self")),
            disp.get("target", "self")))
    if node.get("kind") == "op" and node.get("type") == "status_ctl":
        # 状态操控：目标词 + 操作词（延长 / 缩短 / 叠层 / 强制清除）
        out["target_word"] = str(game.stats.get(
            "cmp_target_" + str(disp.get("target", "self")),
            disp.get("target", "self")))
        out["op_word"] = str(game.stats.get(
            "ctl_" + str(disp.get("op", "stacks")), disp.get("op", "stacks")))
    return out


def _link_formula(node: dict, link: dict, param: str, game: GameCfg):
    """共鸣描述两部分（v0.10.0 起全部为引擎真实值）：

    1. 内联最简线性公式（紧跟对应数值）：最终值 = 基数 + 变量式 * 合并系数；
       变量式以属性 emoji 表示--own 省略范围词、enemy 前缀「对方」、
       difference「【己方-对方】」、sum「【己方+对方】」；
       百分数字段括号内为纯数字、百分号移到括号外；
       v1.2.0 起数值保留两位有效数字：低于 0.1 的数值改为百分数形式；
    2. 尾句依赖描述（使用属性全名，如「己方攻击越高，效果值越高。」）。"""
    tmpl = game.stats
    var_id = str(link.get("variable"))
    var_def = game.attr(var_id)
    var_name = var_def.name
    var_emoji = var_def.emoji or var_name
    base = max(1.0, float(var_def.base))
    mode = str(link.get("mode", "own"))
    scope_own = str(tmpl.get("scope_own", ""))
    scope_enemy = str(tmpl.get("scope_enemy", ""))
    field_word = str(tmpl.get("field_" + param, param))
    fmt = _param_spec(node, param, game)[0]
    eff_raw = float(link.get("base", node.get("params", {}).get(param, 0.0)))
    merged_raw = eff_raw * float(link.get("rate", 0.0)) / base
    if fmt == "pct":
        base_display = _format_formula_number(eff_raw * 100.0, bracket=True)
        merged = _format_formula_number(merged_raw * 100.0, bracket=True)
    else:
        base_display = format_field(eff_raw, fmt)
        merged = _format_formula_number(merged_raw * 100.0, bracket=False)
    against = var_id
    if mode in ("difference", "sum"):
        vdef = next((v for v in game.skill_variable_link.variables if v.id == var_id), None)
        against = vdef.diff_against if vdef else var_id
        against_name = game.attr(against).name
        expr_tmpl = "link_expr_difference" if mode == "difference" else "link_expr_sum"
        expr = render_template(tmpl.get(expr_tmpl, "{emoji}"),
                               {"own": scope_own, "enemy": scope_enemy,
                                "emoji": var_emoji}, game)
        tail_tmpl = "link_difference" if mode == "difference" else "link_sum"
        tail = render_template(tmpl.get(tail_tmpl, ""),
                               {"own": scope_own + var_name,
                                "enemy": scope_enemy + against_name,
                                "field": field_word}, game)
    elif mode == "enemy":
        expr = scope_enemy + var_emoji
        tail = render_template(tmpl.get("link_ratio", ""),
                               {"scope": scope_enemy, "stat": var_name,
                                "field": field_word}, game)
    else:
        expr = var_emoji
        tail = render_template(tmpl.get("link_ratio", ""),
                               {"scope": scope_own, "stat": var_name,
                                "field": field_word}, game)
    formula = render_template(tmpl.get("link_formula", ""),
                              {"base": base_display, "expr": expr, "merged": merged},
                              game)
    if fmt == "pct":
        formula += "%"
    return formula, tail


def _children_map(pgraph: dict):
    """节点 id -> [(gate, 子节点id), ...]（按边数组顺序；gate = pass/fail）。"""
    children = {n["id"]: [] for n in pgraph.get("nodes", ())}
    for e in pgraph.get("edges", ()):
        children.setdefault(e["from"], []).append((e.get("gate", "pass"), e["to"]))
    return children


def _natural_text(pgraph: dict, fighter: Fighter, game: GameCfg,
                  simple: bool = False, live: bool = False) -> str:
    """标准化自然语言描述（v3.0.0 组合式，支持判断 / 分支 / 循环）：

    - 按触发节点在 nodes 数组中的顺序逐链生成句子，句间以「；」相连；
    - 每链递归组合：条件从句在前、原子句在后，子句以「，」相连；条件节点的
      fail 分支以「否则」衔接（分支）；loop 结构节点输出循环从句（循环）；
    - 参数为共鸣估算后的最终值（敌方按基础值估算）；每个共鸣参数的公式
      括号紧跟该数值（simple 模式隐藏公式）；live=True 时共鸣数值位替换为
      「LIVE_MARKER + 槽位序号」（序号对应 link_calc 下标），供前端实时填充。"""
    stats = game.stats
    display, _coeffs = estimated_resonanced_eff(fighter, pgraph, game)
    disp_by_id = {node["id"]: disp for node, disp in display}
    node_by_id = {n["id"]: n for n in pgraph.get("nodes", ())}
    slot_of = {}
    for idx, (node, link) in enumerate(_walk_links(pgraph)):
        slot_of[(node["id"], str(link.get("param")))] = idx
    children = _children_map(pgraph)
    tails = []
    chain_texts = []

    def render(node):
        """渲染单个节点的从句文本（共鸣公式 / live 占位符注入参数位）。"""
        disp = disp_by_id.get(node["id"], node.get("params", {}))
        params = _clause_params(node, disp, game)
        for link in node.get("links") or ():
            param = str(link.get("param"))
            if param not in params:
                continue
            formula, tail = _link_formula(node, link, param, game)
            final = str(params[param])
            slot = slot_of.get((node["id"], param))
            if live:
                params[param] = "%s%d%s" % (LIVE_MARKER, slot,
                                            "" if simple else formula)
            elif simple:
                params[param] = final
            else:
                params[param] = final + formula
            # hp_mod 的比例基准参数在模板中以 {value} 位展示（见 _clause_params）
            if node.get("type") == "hp_mod" and param == "ratio":
                params["value"] = params[param]
            # compare 条件的固定值并入 {right} 显示位（直显数值，无「常数」前缀）
            if node.get("kind") == "condition" and node.get("type") == "compare" \
                    and param == "value":
                params["right"] = str(params[param])
            # loop 的衰减参数并入 {mode_word} 显示位
            if node.get("kind") == "struct" and node.get("type") == "loop" \
                    and param == "decay":
                tmpl = str(game.stats.get("cmp_mode_" +
                           str(node.get("params", {}).get("mode", "chain")), ""))
                params["mode_word"] = tmpl.replace("{decay}", str(params[param]))
            if tail:
                tails.append(tail)
        key = _node_clause(node, disp, game)
        template = stats.get(key)
        if template is None:
            if node["kind"] == "op" and node["type"] == "apply_status":
                sid = str(disp.get("status", ""))
                sname = game.statuses.get(sid, {}).get("name", sid)
                return sname
            return ""
        return render_template(template, params, game)

    def chain_text(nid: str) -> str:
        """以节点 nid 为根的子链文本：条件从句在前、原子句在后；条件节点的
        fail 子链以「否则」衔接在 pass 子链之后（分支语义）。"""
        node = node_by_id[nid]
        parts = [p for p in [render(node)] if p]
        if node["kind"] == "condition":
            for gate, cid in children.get(nid, ()):
                sub = chain_text(cid)
                if not sub:
                    continue
                if gate == "fail":
                    parts.append("否则" + sub)
                else:
                    parts.append(sub)
        else:
            for _gate, cid in children.get(nid, ()):
                sub = chain_text(cid)
                if sub:
                    parts.append(sub)
        return "，".join(parts)

    def _subtree_ops(nid: str):
        """以 nid 为根的子树内全部节点。"""
        out, stack = [], [nid]
        while stack:
            cur = stack.pop()
            node = node_by_id.get(cur)
            if node is None:
                continue
            out.append(node)
            for _gate, cid in children.get(cur, ()):
                stack.append(cid)
        return out

    for node in pgraph.get("nodes", ()):
        if node["kind"] != "trigger":
            continue
        key = "hook_" + node["type"]
        if node["type"] == "on_attack":
            # 攻击链细分：子树为修饰 / 施加类（攻击照常）用「攻击时」；
            # 含 replace 模式的 strike（雷罚等——本次攻击被替换）用
            # 「替代攻击时」，避免读者误解为附带效果
            replacing = any(
                sub.get("kind") == "op" and sub.get("type") == "strike"
                and str(sub.get("params", {}).get("mode")) == "replace"
                for _gate, cid in children.get(node["id"], ())
                for sub in _subtree_ops(cid))
            if replacing:
                key = "hook_on_attack_replace"
        hook_word = str(stats.get(key, ""))
        parts = [p for p in [hook_word] if p]
        for _gate, cid in children.get(node["id"], ()):
            sub = chain_text(cid)
            if sub:
                parts.append(sub)
        chain_texts.append("，".join(parts))

    text = "；".join(t for t in chain_texts if t)
    for tail in tails:
        text += tail
    if not text.endswith("。") and text:
        text = text + "。"
    return text


def _link_calc(pgraph: dict, game: GameCfg) -> list:
    """对战实时技能数据（每个共鸣变数一条）：前端按公式
    最终值 = base + 变量式 × coeff（含上下限）用双方快照逐刻重算，
    与引擎 resonance_coeff + apply_resonance 完全一致，按槽位顺序排列。
    base/coeff/clamp 均为引擎真实值，与快照属性口径一致。"""
    out = []
    for node, link in _walk_links(pgraph):
        param = str(link.get("param"))
        if param not in node.get("params", {}):
            continue
        var_id = str(link.get("variable"))
        base = max(1.0, float(game.attr(var_id).base))
        mode = str(link.get("mode", "own"))
        against = var_id
        if mode in ("difference", "sum"):
            vdef = next((v for v in game.skill_variable_link.variables
                         if v.id == var_id), None)
            against = vdef.diff_against if vdef else var_id
        fmt, lo, hi = _param_spec(node, param, game)
        value = float(link.get("base", node.get("params", {}).get(param, 0.0)))
        out.append({
            "field": param,
            "fmt": fmt,
            "base": value,
            "coeff": value * float(link.get("rate", 0.0)) / base,
            "mode": mode,
            "variable": var_id,
            "against": against,
            "clamp": [lo, hi],
        })
    return out


_MOD_TEMPLATES = {"chance": "mod_chance", "value": "mod_value",
                  "damage": "mod_damage", "turns": "mod_turns", "ticks": "mod_ticks"}


def _mod_texts(pgraph: dict, game: GameCfg) -> list:
    """词缀修正的可读文案（显示个性化缩放后的实际值），如「疾风：触发率 +3%」。
    绝对数值字段以整数展示，纯倍率字段以百分数展示。"""
    texts = []
    for kind, key, scale_key in (("prefix", "prefix", "prefix_scale"),
                                 ("suffix", "suffix", "suffix_scale")):
        mod_id = pgraph.get(key)
        if not mod_id:
            continue
        mdef = game.name_modifier(kind, mod_id)
        if mdef is None:
            continue
        scale = float(pgraph.get(scale_key, 1.0))
        parts = []
        for param, delta in mdef.mod.items():
            template_key = _MOD_TEMPLATES.get(param)
            if not template_key:
                continue
            scaled = float(delta) * scale
            if param == "chance":
                magnitude = format_pct(abs(scaled))
            elif param == "value" and not graph_param_unit(pgraph, "value", game):
                magnitude = format_pct(abs(scaled))  # 纯倍率字段：增量以百分数展示
            else:
                magnitude = format_num(abs(scaled))  # 绝对数值字段：真实值整数展示
            sign = "+" if scaled > 0 else "-"
            parts.append(render_template(game.stats.get(template_key, template_key),
                                         {"v": sign + magnitude}, game))
        if parts:
            texts.append(mdef.name + "：" + "，".join(parts))
    return texts


def _mastery_text(pgraph: dict, sdef, game: GameCfg) -> str:
    """熟练度文案（v0.9.1）：直接给出该技能实例的最终触发率，
    如「熟练度 63：触发率 36.52%」--永远不超过 100%；
    条件型技能给出效果倍率（×1.21），壁垒类给出免疫触发率。"""
    mastery = pgraph.get("mastery")
    if mastery is None:
        return ""
    if "immune" in sdef.mastery_on:
        rate = min(0.5, max(0.01, float(graph_param_value(pgraph, "immune", 0.0))))
        return render_template(game.stats.get("mastery_text_immune", ""),
                               {"v": int(mastery), "rate": format_pct(rate)},
                               game)
    if "chance" in sdef.mastery_on:
        raw = graph_param_value(pgraph, "chance", 0.0)
        if isinstance(raw, str):
            # chance 为表达式（如不屈的衰减概率）：不走触发率口径，按倍率展示
            return render_template(game.stats.get("mastery_text_value", ""),
                                   {"v": int(mastery),
                                    "mult": "%.2f" % float(pgraph.get("mastery_mult", 1.0))},
                                   game)
        rate = min(0.95, max(0.02, float(raw)))
        return render_template(game.stats.get("mastery_text", ""),
                               {"v": int(mastery), "rate": format_pct(rate)},
                               game)
    return render_template(game.stats.get("mastery_text_value", ""),
                           {"v": int(mastery),
                            "mult": "%.2f" % float(pgraph.get("mastery_mult", 1.0))},
                           game)


def _find_structure(fighter: Fighter, game: GameCfg):
    for s in game.title_structures:
        if s.id == fighter.title_structure_id:
            return s
    return None


def _format_bonus(value: float, attr_format: str) -> str:
    sign = "+" if value > 0 else ""
    if attr_format == "percent":
        return "%s%s%%" % (sign, format_num(value))
    return "%s%s" % (sign, format_num(value))


def _title_bonus_api(fighter: Fighter, game: GameCfg) -> dict:
    """称号加成的对外表示：按属性配置顺序聚合，供卡牌展示。
    value 为引擎真实值增量（v0.10.0 起直显，不再换算）。"""
    structure = _find_structure(fighter, game)
    if structure is None:
        return {"bonuses": [], "bonuses_text": ""}
    sums = {}
    for attr_id, delta in title_bonus_items(fighter.title_fields, structure, game):
        sums[attr_id] = sums.get(attr_id, 0) + delta
    bonuses = []
    parts = []
    for a in game.attributes:
        if a.id not in sums or sums[a.id] == 0:
            continue
        text = _format_bonus(sums[a.id], a.format)
        bonuses.append({"attr": a.id, "name": a.name, "value": sums[a.id],
                        "display": text, "format": a.format})
        parts.append("%s %s" % (a.name, text))
    return {"bonuses": bonuses, "bonuses_text": " · ".join(parts)}


def compose_title_name(fighter: Fighter, game: GameCfg) -> str:
    """按结构与连接符拼接称号显示名。"""
    structure = _find_structure(fighter, game)
    if structure is None:
        return fighter.title_structure_id
    parts = []
    for fname in structure.fields:
        fid = fighter.title_fields.get(fname)
        fdef = game.title_field(TITLE_FIELD_POOLS[fname], fid)
        parts.append(fdef.name if fdef is not None else str(fid or ""))
    if not parts:
        return ""
    out = parts[0]
    for i in range(1, len(parts)):
        connector = structure.connectors[i - 1] if i - 1 < len(structure.connectors) else ""
        out += connector + parts[i]
    return out


def compose_title_desc(fighter: Fighter, game: GameCfg) -> str:
    """把各字段的描述片段用「，」连接成称号描述。"""
    structure = _find_structure(fighter, game)
    if structure is None:
        return ""
    frags = []
    for fname in structure.fields:
        fid = fighter.title_fields.get(fname)
        fdef = game.title_field(TITLE_FIELD_POOLS[fname], fid)
        if fdef is not None and fdef.desc:
            frags.append(fdef.desc)
    return "，".join(frags) + "。" if frags else ""


def fighter_to_api(fighter: Fighter, game: GameCfg) -> dict:
    """斗士数据的对外表示：数值来自 Fighter/个性化技能图，显示名来自同一配置。
    v0.10.0 起属性 value/min/max 均为引擎真实值（不再换算白板单位）。"""
    attrs_api = []
    for a in game.attributes:
        raw = fighter.attrs[a.id]
        attrs_api.append({
            "id": a.id,
            "name": a.name,
            "emoji": a.emoji,
            "value": round(float(raw), 4),
            "min": round(a.min, 4),
            "max": round(a.max, 4),
            "format": a.format,
        })
    skills_api = []
    for sdef, pgraph in personalized_effects(fighter, game):
        sep = str(game.stats.get("link_sep", "·"))
        name = sdef.name
        if pgraph.get("prefix"):
            pdef = game.name_modifier("prefix", pgraph["prefix"])
            if pdef is not None:
                name = pdef.name + sep + name
        if pgraph.get("suffix"):
            smod = game.name_modifier("suffix", pgraph["suffix"])
            if smod is not None:
                name = name + sep + smod.name
        links = [link for _node, link in _walk_links(pgraph)]
        for link in links:
            marker = game.stats.get("link_" + str(link.get("variable")))
            if marker:
                name = name + sep + str(marker)
        link_api = []
        for link in links:
            vdef = next((a for a in game.attributes if a.id == link.get("variable")), None)
            link_api.append({
                "field": str(link.get("param")),
                "variable": link.get("variable"),
                "name": vdef.name if vdef else str(link.get("variable")),
                "mode": link.get("mode", "own"),
                "rate": link.get("rate", 0),
            })
        skill_entry = {
            "id": sdef.id,
            "name": name,
            "flavor": sdef.description,
            "text": _natural_text(pgraph, fighter, game),
            "text_simple": _natural_text(pgraph, fighter, game, simple=True),
            "modifiers": _mod_texts(pgraph, game),
            "mastery": int(pgraph.get("mastery", 0)),
            "mastery_text": _mastery_text(pgraph, sdef, game),
            "link": link_api if link_api else None,
        }
        if links:
            skill_entry["live_text"] = _natural_text(pgraph, fighter, game, live=True)
            skill_entry["live_text_simple"] = _natural_text(
                pgraph, fighter, game, simple=True, live=True)
            skill_entry["link_calc"] = _link_calc(pgraph, game)
        skills_api.append(skill_entry)
    title_bonus = _title_bonus_api(fighter, game)
    return {
        "name": fighter.name,
        "normalized": fighter.normalized,
        "digest": fighter.digest,
        "digest_short": fighter.digest[:8],
        "title": {
            "structure": fighter.title_structure_id,
            "name": compose_title_name(fighter, game),
            "description": compose_title_desc(fighter, game),
            "bonuses": title_bonus["bonuses"],
            "bonuses_text": title_bonus["bonuses_text"],
        },
        "attributes": attrs_api,
        "skills": skills_api,
        "power": fighter.power,
    }
