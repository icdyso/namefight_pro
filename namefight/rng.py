"""确定性伪随机数发生器（splitmix64）。

项目核心不变量之一：所有"随机"都必须来自本模块（见 AGENTS.md 2.1.3）。
- 纯整数运算，不依赖 os / urandom / time / random 等任何外部状态；
- 同一种子永远得到同一序列，与平台、进程、调用次数无关；
- 数值类抽样一律使用 next_triangular（三角形分布：两个均匀数取均值，
  每次消耗两个均匀数），天然落在区间内、端点无截断堆积；
  离散选择仍用加权均匀抽取。
"""

_MASK64 = (1 << 64) - 1
_GOLDEN = 0x9E3779B97F4A7C15
_GAMMA1 = 0xBF58476D1CE4E5B9
_GAMMA2 = 0x94D049BB133111EB


class DetRng:
    """splitmix64 确定性 PRNG。"""

    def __init__(self, seed: int) -> None:
        self._state = seed & _MASK64

    def next_u64(self) -> int:
        self._state = (self._state + _GOLDEN) & _MASK64
        z = self._state
        z = ((z ^ (z >> 30)) * _GAMMA1) & _MASK64
        z = ((z ^ (z >> 27)) * _GAMMA2) & _MASK64
        return z ^ (z >> 31)

    def next_float(self) -> float:
        """返回 [0, 1) 内的浮点数。"""
        return self.next_u64() / float(1 << 64)

    def next_int(self, n: int) -> int:
        """返回 [0, n) 内的整数。"""
        if n <= 0:
            raise ValueError("n 必须为正整数")
        return self.next_u64() % n

    def next_range(self, lo: int, hi: int) -> int:
        """返回 [lo, hi] 内的整数（含两端，均匀分布）。"""
        if hi < lo:
            raise ValueError("区间非法: [%s, %s]" % (lo, hi))
        return lo + self.next_int(hi - lo + 1)

    def next_triangular(self, lo: float, hi: float) -> float:
        """返回 [lo, hi] 内三角形分布的浮点数：两个均匀随机数取均值
        （每次消耗两个均匀数），密度在区间中点处最高、向两端线性递减，
        天然落在区间内--端点不会发生截断堆积（v1.1.0 起替代正态投掷）。"""
        if hi < lo:
            raise ValueError("区间非法: [%s, %s]" % (lo, hi))
        if hi == lo:
            return lo
        mid = (self.next_float() + self.next_float()) / 2.0
        return lo + (hi - lo) * mid

    def next_triangular_range(self, lo: int, hi: int) -> int:
        """返回 [lo, hi] 内近似三角形分布的整数（离散数值，如技能数量；
        以 ±0.5 扩展区间后取整，保证端点有合理概率）。"""
        if hi < lo:
            raise ValueError("区间非法: [%s, %s]" % (lo, hi))
        if hi == lo:
            return lo
        value = round(self.next_triangular(lo - 0.5, hi + 0.5))
        return max(lo, min(hi, int(value)))

    def pick_weighted(self, weighted_items):
        """按权重挑选一个元素。weighted_items: iterable[(item, weight)]"""
        items = list(weighted_items)
        if not items:
            raise ValueError("候选列表为空")
        total = sum(w for _, w in items)
        if total <= 0:
            raise ValueError("权重总和必须为正")
        roll = self.next_float() * total
        acc = 0.0
        for item, w in items:
            acc += w
            if roll < acc:
                return item
        return items[-1][0]  # 浮点边界兜底

    def sample_weighted(self, weighted_items, k: int):
        """不放回按权重抽取 k 个元素，保持抽取顺序（保证确定性）。"""
        pool = list(weighted_items)
        picked = []
        while pool and len(picked) < k:
            item = self.pick_weighted(pool)
            picked.append(item)
            for i, (it, _) in enumerate(pool):
                if it == item:
                    del pool[i]
                    break
        return picked
