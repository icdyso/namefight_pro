/* 确定性伪随机数发生器（splitmix64）——namefight/rng.py 的逐行移植。
   BigInt 实现 64 位混位乘法；所有"随机"必须来自本模块（确定性契约）。 */
(function (root) {
  "use strict";
  var NFE = root.NFE = root.NFE || {};

  var M64 = (1n << 64n) - 1n;
  var GOLDEN = 0x9E3779B97F4A7C15n;
  var GAMMA1 = 0xBF58476D1CE4E5B9n;
  var GAMMA2 = 0x94D049BB133111EBn;
  var TWO_POW_64 = 18446744073709551616.0;   // 2^64（精确）

  function DetRng(seed) {
    // seed: BigInt（md5 hex 转来）或 Number
    this._state = BigInt(seed) & M64;
  }

  DetRng.prototype.nextU64 = function () {
    this._state = (this._state + GOLDEN) & M64;
    var z = this._state;
    z = ((z ^ (z >> 30n)) * GAMMA1) & M64;
    z = ((z ^ (z >> 27n)) * GAMMA2) & M64;
    return z ^ (z >> 31n);
  };

  DetRng.prototype.nextFloat = function () {
    // [0, 1)；Number(BigInt) 舍入到最近双精度，除以 2^64 为精确幂缩放，
    // 与 Python int/int 真除法（正确舍入）同结果
    return Number(this.nextU64()) / TWO_POW_64;
  };

  DetRng.prototype.nextInt = function (n) {
    if (n <= 0) throw new Error("n 必须为正整数");
    return Number(this.nextU64() % BigInt(n));
  };

  DetRng.prototype.nextRange = function (lo, hi) {
    if (hi < lo) throw new Error("区间非法: [" + lo + ", " + hi + "]");
    return lo + this.nextInt(hi - lo + 1);
  };

  DetRng.prototype.nextTriangular = function (lo, hi) {
    // 两个均匀数取均值（消耗两个），密度中点最高、向两端线性递减
    if (hi < lo) throw new Error("区间非法: [" + lo + ", " + hi + "]");
    if (hi === lo) return lo;
    var mid = (this.nextFloat() + this.nextFloat()) / 2.0;
    return lo + (hi - lo) * mid;
  };

  DetRng.prototype.nextTriangularRange = function (lo, hi) {
    if (hi < lo) throw new Error("区间非法: [" + lo + ", " + hi + "]");
    if (hi === lo) return lo;
    var value = NFE.pyRound(this.nextTriangular(lo - 0.5, hi + 0.5));
    return Math.max(lo, Math.min(hi, NFE.pyInt(value)));
  };

  DetRng.prototype.pickWeighted = function (weightedItems) {
    var items = Array.from(weightedItems);
    if (!items.length) throw new Error("候选列表为空");
    var total = 0.0;
    for (var i = 0; i < items.length; i++) total += items[i][1];
    if (total <= 0) throw new Error("权重总和必须为正");
    var roll = this.nextFloat() * total;
    var acc = 0.0;
    for (i = 0; i < items.length; i++) {
      acc += items[i][1];
      if (roll < acc) return items[i][0];
    }
    return items[items.length - 1][0];      // 浮点边界兜底
  };

  DetRng.prototype.sampleWeighted = function (weightedItems, k) {
    var pool = Array.from(weightedItems);
    var picked = [];
    while (pool.length && picked.length < k) {
      var item = this.pickWeighted(pool);
      picked.push(item);
      for (var i = 0; i < pool.length; i++) {
        if (pool[i][0] === item) { pool.splice(i, 1); break; }
      }
    }
    return picked;
  };

  NFE.DetRng = DetRng;
})(typeof window !== "undefined" ? window : globalThis);
