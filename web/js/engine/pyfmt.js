/* Python 数值语义兼容层：银行家舍入 / 保留 N 位小数（半偶、精确十进制） /
   浮点 repr / 码点序比较。全部为确定性移植（与 CPython 同口径），
   差分测试（tools/port_check.mjs）保证与 Python 输出逐字节一致。 */
(function (root) {
  "use strict";
  var NFE = root.NFE = root.NFE || {};

  /* round(x)：CPython 银行家舍入（0.5 -> 0，2.5 -> 2，-2.5 -> -2） */
  function pyRound(x) {
    x = Number(x);
    if (!isFinite(x)) return x;
    var f = Math.floor(x), d = x - f;
    if (d > 0.5) return f + 1;
    if (d < 0.5) return f;
    return (((f % 2) + 2) % 2) === 0 ? f : f + 1;
  }

  /* int(x)：向零截断 */
  function pyInt(x) {
    return Math.trunc(x);
  }

  var _dv = new DataView(new ArrayBuffer(8));

  /* 把 double 精确分解为 {sign, m, e}：x == sign * m * 2^e（m 为正 BigInt） */
  function _decomp(x) {
    _dv.setFloat64(0, x);
    var hi = _dv.getUint32(0), lo = _dv.getUint32(4);
    var sign = (hi >>> 31) ? -1 : 1;
    var exp = (hi >>> 20) & 0x7ff;
    var mant = (BigInt(hi & 0xfffff) << 32n) | BigInt(lo);
    var e;
    if (exp === 0) {
      e = -1074n;                       // 次正规数
    } else {
      mant |= (1n << 52n);
      e = BigInt(exp) - 1075n;
    }
    return { sign: sign, m: mant, e: e };
  }

  /* x * 10^digits 的精确整数化（半偶舍入），返回 BigInt（带符号） */
  function _scaledHalfEven(x, digits) {
    var d = _decomp(x);
    if (d.m === 0n) return 0n;
    var dg = BigInt(digits);
    // scaled = m * 5^dg * 2^(e+dg)
    var num = d.m * (5n ** dg);
    var sh = d.e + dg;
    var i;
    if (sh >= 0n) {
      i = num << sh;
    } else {
      var den = 1n << (-sh);
      var q = num / den, r = num % den;
      var twice = r * 2n;
      if (twice > den) q += 1n;
      else if (twice === den && (q % 2n) !== 0n) q += 1n;   // 恰半：取偶
      i = q;
    }
    return d.sign < 0 ? -i : i;
  }

  /* round(x, digits)：CPython 十进制半偶舍入（返回最接近结果的双精度数） */
  function pyRoundN(x, digits) {
    x = Number(x);
    if (!isFinite(x)) return x;
    var i = _scaledHalfEven(x, digits);
    var s = (i < 0n ? (-i) : i).toString();
    if (i < 0n) s = "-" + s;
    if (digits > 0) {
      s = s.padStart(digits + 1, "0");
      s = s.slice(0, s.length - digits) + "." + s.slice(s.length - digits);
    }
    return Number(s);
  }

  /* "%.Df" % x：精确十进制半偶舍入后按 D 位小数输出（含 "-0.00" 语义） */
  function pyFix(x, digits) {
    x = Number(x);
    if (!isFinite(x)) return String(x);
    var neg = Object.is(x, -0) || x < 0;
    var i = _scaledHalfEven(x, digits);
    var mag = i < 0n ? (-i) : i;
    var s = mag.toString();
    if (digits > 0) {
      s = s.padStart(digits + 1, "0");
      s = s.slice(0, s.length - digits) + "." + s.slice(s.length - digits);
    }
    if (mag === 0n) {
      // C 风格 "%f"：负零保留符号（-0.001 -> "-0.00"；0 -> "0.00"）
      return (neg ? "-" : "") + "0" + (digits > 0 ? "." + "0".repeat(digits) : "");
    }
    return (neg ? "-" : "") + s;
  }

  /* str(float)：CPython repr（最短往返；整值补 .0；e 记法阈值与两位指数） */
  function pyFloatStr(v) {
    if (Number.isInteger(v) && Math.abs(v) < 1e16) {
      return (Object.is(v, -0) ? "-0.0" : v.toFixed(1));
    }
    var s = String(v);                   // JS 最短往返
    var m = /^(-?)(\d)(?:\.(\d+))?e([+-]\d+)$/.exec(s);
    if (m) {
      var ex = parseInt(m[4], 10);
      var mant = m[2] + (m[3] ? "." + m[3] : ".0");
      var exs = (ex < 0 ? "-" : "+") + String(Math.abs(ex)).padStart(2, "0");
      return m[1] + mant + "e" + exs;
    }
    return s;
  }

  /* 模板参数的 str()：布尔 True/False、整数原样、浮点走 repr；
     None（缺参）与 CPython 一致渲染为 "None" */
  function pyStr(v) {
    if (v === null || v === undefined) return "None";
    if (typeof v === "boolean") return v ? "True" : "False";
    if (typeof v === "number") {
      return Number.isInteger(v) ? String(v) : pyFloatStr(v);
    }
    return String(v);
  }

  /* 码点序字符串比较（Python str 比较 / sorted 的口径；JS 默认按 UTF-16 码元，
     星面字符会排序不同） */
  function pyCmp(a, b) {
    var ia = a[Symbol.iterator](), ib = b[Symbol.iterator]();
    for (;;) {
      var ra = ia.next(), rb = ib.next();
      if (ra.done && rb.done) return 0;
      if (ra.done) return -1;
      if (rb.done) return 1;
      var d = ra.value.codePointAt(0) - rb.value.codePointAt(0);
      if (d) return d < 0 ? -1 : 1;
    }
  }

  function pySorted(arr, keyFn) {
    var k = arr.slice();
    k.sort(function (a, b) { return pyCmp(keyFn ? keyFn(a) : a, keyFn ? keyFn(b) : b); });
    return k;
  }

  /* len()：码点数（ astral 字符算 1） */
  function pyLen(s) {
    var n = 0;
    for (var _ of s) n++;
    return n;
  }

  NFE.pyRound = pyRound;
  NFE.pyInt = pyInt;
  NFE.pyRoundN = pyRoundN;
  NFE.pyFix = pyFix;
  NFE.pyFloatStr = pyFloatStr;
  NFE.pyStr = pyStr;
  NFE.pyCmp = pyCmp;
  NFE.pySorted = pySorted;
  NFE.pyLen = pyLen;
})(typeof window !== "undefined" ? window : globalThis);
