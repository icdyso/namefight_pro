/* 同步 MD5（UTF-8 字节 -> 小写 hex）。
   Python 侧 hashlib.md5(name.encode("utf-8")).hexdigest() 的等价实现，
   斗士种子 / 对战种子 / 个性化种子的共同来源（确定性契约 2.1.2）。 */
(function (root) {
  "use strict";
  var NFE = root.NFE = root.NFE || {};

  // 手写 UTF-8 编码（避免依赖 TextEncoder；代理对按 U+FFFD 处理）
  function utf8Bytes(str) {
    var out = [];
    for (var i = 0; i < str.length; i++) {
      var code = str.charCodeAt(i);
      if (code >= 0xD800 && code <= 0xDBFF) {
        var lo = i + 1 < str.length ? str.charCodeAt(i + 1) : 0;
        if (lo >= 0xDC00 && lo <= 0xDFFF) {
          code = 0x10000 + ((code - 0xD800) << 10) + (lo - 0xDC00);
          i++;
        } else {
          code = 0xFFFD;
        }
      } else if (code >= 0xDC00 && code <= 0xDFFF) {
        code = 0xFFFD;
      }
      if (code < 0x80) {
        out.push(code);
      } else if (code < 0x800) {
        out.push(0xC0 | (code >> 6), 0x80 | (code & 0x3F));
      } else if (code < 0x10000) {
        out.push(0xE0 | (code >> 12), 0x80 | ((code >> 6) & 0x3F), 0x80 | (code & 0x3F));
      } else {
        out.push(0xF0 | (code >> 18), 0x80 | ((code >> 12) & 0x3F),
                 0x80 | ((code >> 6) & 0x3F), 0x80 | (code & 0x3F));
      }
    }
    return out;
  }

  var K = [
    0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee, 0xf57c0faf, 0x4787c62a, 0xa8304613, 0xfd469501,
    0x698098d8, 0x8b44f7af, 0xffff5bb1, 0x895cd7be, 0x6b901122, 0xfd987193, 0xa679438e, 0x49b40821,
    0xf61e2562, 0xc040b340, 0x265e5a51, 0xe9b6c7aa, 0xd62f105d, 0x02441453, 0xd8a1e681, 0xe7d3fbc8,
    0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed, 0xa9e3e905, 0xfcefa3f8, 0x676f02d9, 0x8d2a4c8a,
    0xfffa3942, 0x8771f681, 0x6d9d6122, 0xfde5380c, 0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70,
    0x289b7ec6, 0xeaa127fa, 0xd4ef3085, 0x04881d05, 0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665,
    0xf4292244, 0x432aff97, 0xab9423a7, 0xfc93a039, 0x655b59c3, 0x8f0ccc92, 0xffeff47d, 0x85845dd1,
    0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1, 0xf7537e82, 0xbd3af235, 0x2ad7d2bb, 0xeb86d391
  ];
  var S = [
    7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
    5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
    4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
    6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21
  ];

  function rotl(x, c) {
    return (x << c) | (x >>> (32 - c));
  }

  function md5Hex(str) {
    var msg = utf8Bytes(str);
    var bitLen = msg.length * 8;
    msg.push(0x80);
    while (msg.length % 64 !== 56) msg.push(0);
    // 64 位小端长度（游戏名字长度远小于 2^32，高 32 位恒 0）
    msg.push(bitLen & 0xff, (bitLen >>> 8) & 0xff, (bitLen >>> 16) & 0xff, (bitLen >>> 24) & 0xff, 0, 0, 0, 0);

    var a0 = 0x67452301, b0 = 0xefcdab89, c0 = 0x98badcfe, d0 = 0x10325476;
    var M = new Uint32Array(16);
    for (var off = 0; off < msg.length; off += 64) {
      for (var i = 0; i < 16; i++) {
        M[i] = (msg[off + i * 4]) | (msg[off + i * 4 + 1] << 8) |
               (msg[off + i * 4 + 2] << 16) | (msg[off + i * 4 + 3] << 24);
      }
      var A = a0, B = b0, C = c0, D = d0;
      for (i = 0; i < 64; i++) {
        var F, g;
        if (i < 16)      { F = (B & C) | (~B & D);      g = i; }
        else if (i < 32) { F = (D & B) | (~D & C);      g = (5 * i + 1) % 16; }
        else if (i < 48) { F = B ^ C ^ D;               g = (3 * i + 5) % 16; }
        else             { F = C ^ (B | ~D);            g = (7 * i) % 16; }
        F = (F + A + K[i] + M[g]) | 0;
        A = D; D = C; C = B;
        B = (B + rotl(F, S[i])) | 0;
      }
      a0 = (a0 + A) | 0; b0 = (b0 + B) | 0; c0 = (c0 + C) | 0; d0 = (d0 + D) | 0;
    }
    function hex8(n) {
      var s = "";
      for (var k = 0; k < 4; k++) {
        var b = (n >>> (k * 8)) & 0xff;
        s += (b < 16 ? "0" : "") + b.toString(16);
      }
      return s;
    }
    return hex8(a0) + hex8(b0) + hex8(c0) + hex8(d0);
  }

  NFE.md5Hex = md5Hex;
})(typeof window !== "undefined" ? window : globalThis);
