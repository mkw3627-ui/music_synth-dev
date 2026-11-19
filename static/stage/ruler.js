/* =============================================================================
 * NekoRuler —— 模型上的「尺子」：量这个模型此刻动了多少（画面量），和水水的标准比
 *
 * 量什么（都除以「眼距」= 两只眼珠的横向跨度，所以和模型大小、头发耳朵帽子无关）：
 *   mouth：嘴张开了多高（嘴里那块网格的高度 - 闭嘴时的高度）
 *   turn ：头左右移了多少（头部网格中心横移）
 *   nod  ：头上下移了多少（头部网格中心纵移）
 *   tilt ：头歪了多少度
 * 用法：
 *   const r = NekoRuler.setup(native)   // 模型加载后量一次「哪几块是眼珠 / 嘴 / 头」（会临时改参数，量完还原）
 *   r.rest()                             // 记下静止时的样子
 *   r.read()                             // 之后任意一帧：返回 {mouth, turn, nod, tilt}（读的是实际渲染的网格，含物理）
 * native = coreModel.getModel()（Cubism Core 原生模型）
 * ========================================================================== */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.NekoRuler = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function grab(native) {
    const D = native.drawables, out = [];
    for (let i = 0; i < D.count; i++) out.push(Float32Array.from(D.vertexPositions[i]));
    return out;
  }
  function moved(a, b) {
    let d = 0;
    for (let j = 0; j < a.length; j++) { const x = Math.abs(a[j] - b[j]); if (x > d) d = x; }
    return d;
  }
  function boxOf(v) {
    let x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9;
    for (let j = 0; j < v.length; j += 2) {
      if (v[j] < x0) x0 = v[j]; if (v[j] > x1) x1 = v[j];
      if (v[j + 1] < y0) y0 = v[j + 1]; if (v[j + 1] > y1) y1 = v[j + 1];
    }
    return [x0, x1, y0, y1];
  }

  // opts.mouthId：张嘴用哪个参数（mao_pro 这类模型张嘴在 LipSync 分组的 ParamA 上），默认 ParamMouthOpenY
  function setup(native, opts) {
    const mouthId = (opts && opts.mouthId) || 'ParamMouthOpenY';
    const P = native.parameters, D = native.drawables;
    const ids = Array.from(P.ids);
    const saved = Float32Array.from(P.values);
    const reset = () => { for (let i = 0; i < P.count; i++) P.values[i] = P.defaultValues[i]; };
    const poke = (id, pick) => {
      const i = ids.indexOf(id);
      if (i < 0) return null;
      reset(); native.update(); const base = grab(native);
      reset(); P.values[i] = pick(P.minimumValues[i], P.maximumValues[i], P.defaultValues[i]); native.update();
      const now = grab(native);
      const op = Float32Array.from(D.opacities);
      return { base, now, op };
    };
    let info = { ok: false };
    let eyeFallback = false;
    try {
      // 1) 眼珠：眼球 X 推满时动得最多的那几块（≥ 最大位移的一半）
      const e = poke('ParamEyeBallX', (lo, hi) => hi);
      let eyes = [];
      if (e) {
        const d = e.base.map((b, i) => moved(e.now[i], b));
        const mx = Math.max(...d);
        if (mx > 0) eyes = d.map((x, i) => (x >= mx * 0.5 ? i : -1)).filter(i => i >= 0);
      }
      // 2) 嘴：张嘴推满时「高度增加最多、且张开后是可见的、静止时不大」的那一块
      const m = poke(mouthId, (lo, hi) => hi);
      // 3) 头：歪头推满时会动的网格
      const z = poke('ParamAngleZ', (lo, hi, def) => (hi - def >= def - lo ? hi : lo));
      reset(); native.update();
      const rest = grab(native);
      // 眼距 = 两只眼珠中心的距离。眼球参数还会带动高光、眼角饰品等小块（梦梦的 26 块里有好几块在脸外侧），
      // 所以按 x 中心分左右两组，各取「位移最大的那块」当眼珠，量两颗眼珠中心的间距。
      let eyeW = 0;
      if (e && eyes.length) {
        const d = e.base.map((bb, i) => moved(e.now[i], bb));
        const cx = i => { const b = boxOf(rest[i]); return (b[0] + b[1]) / 2; };
        const xs = eyes.map(cx).sort((p, q) => p - q);
        const mid = (xs[0] + xs[xs.length - 1]) / 2;
        let li = -1, ri = -1;
        for (const i of eyes) {
          if (cx(i) < mid) { if (li < 0 || d[i] > d[li]) li = i; } else if (ri < 0 || d[i] > d[ri]) ri = i;
        }
        if (li >= 0 && ri >= 0) eyeW = Math.abs(cx(ri) - cx(li));
      }
      // 合理性检查：眼距至少要有头宽的 6%（头宽里可能含头纱、帽檐，所以只查下限）。有的模型眼球参数只带动高光（mao_pro 量出来只有头宽的 2%），
      // 这时退回「头宽 × 0.4」（动漫脸两眼中心距大约是头宽的四成）。
      let headW = 0;
      if (z) {
        let x0 = 1e9, x1 = -1e9;
        for (let i = 0; i < D.count; i++) if (moved(z.now[i], z.base[i]) > 1e-4) { const bb = boxOf(rest[i]); x0 = Math.min(x0, bb[0]); x1 = Math.max(x1, bb[1]); }
        if (x1 > x0) headW = x1 - x0;
      }
      eyeFallback = false;
      if (headW > 0 && !(eyeW > headW * 0.06)) { eyeW = headW * 0.4; eyeFallback = true; }
      if (!(eyeW > 0)) eyeW = 0;
      let mouth = -1, best = 0;
      if (m) {
        for (let i = 0; i < D.count; i++) {
          const b0 = boxOf(m.base[i]), b1 = boxOf(m.now[i]);
          const h0 = b0[3] - b0[2], dh = (b1[3] - b1[2]) - h0;
          if (m.op[i] < 0.5 || h0 > eyeW * 0.6) continue;
          if (dh > best) { best = dh; mouth = i; }
        }
      }
      let head = [];
      if (z) head = z.base.map((b, i) => (moved(z.now[i], b) > 1e-4 ? i : -1)).filter(i => i >= 0);
      info = { ok: eyes.length > 0, eyes, mouth, head, eyeW, eyeFallback };
    } finally {
      for (let i = 0; i < P.count; i++) P.values[i] = saved[i];
      try { native.update(); } catch (e2) { /* 下一帧会自己算 */ }
    }

    let restSnap = null;
    const centroid = (snap, idx) => {
      let sx = 0, sy = 0, n = 0;
      for (const i of idx) { const v = snap[i]; for (let j = 0; j < v.length; j += 2) { sx += v[j]; sy += v[j + 1]; n++; } }
      return n ? [sx / n, sy / n] : [0, 0];
    };
    const rotation = (a, b, idx) => {
      const [ax, ay] = centroid(a, idx), [bx, by] = centroid(b, idx);
      let s = 0, c = 0;
      for (const i of idx) {
        const u = a[i], v = b[i];
        for (let j = 0; j < u.length; j += 2) {
          const p = u[j] - ax, q = u[j + 1] - ay, r = v[j] - bx, t = v[j + 1] - by;
          c += p * r + q * t; s += p * t - q * r;
        }
      }
      return Math.atan2(s, c) * 180 / Math.PI;
    };
    function measureFrom(snap) {
      if (!restSnap) return null;
      const out = {};
      const W = info.eyeW || 1;
      if (info.mouth >= 0) {
        const b0 = boxOf(restSnap[info.mouth]), b1 = boxOf(snap[info.mouth]);
        out.mouth = ((b1[3] - b1[2]) - (b0[3] - b0[2])) / W * 100;
      }
      if (info.head.length) {
        const [x0, y0] = centroid(restSnap, info.head), [x1, y1] = centroid(snap, info.head);
        out.turn = (x1 - x0) / W * 100;
        out.nod = (y1 - y0) / W * 100;
        out.tilt = rotation(restSnap, snap, info.head);
      }
      return out;
    }
    return {
      info,
      rest() { restSnap = grab(native); },
      read() { return measureFrom(grab(native)); },
    };
  }

  // 小表情尺子：把某个参数从默认值推到 val，量「两边都看得见的网格」最大位移（眼距%）。
  // 只算两边都可见的网格：有的表情会把一整块零件显示 / 隐藏（kemomimi 嘴角量出 400 多倍就是这个），那不是形变。
  function exprAmp(native, id, val, eyeW) {
    const P = native.parameters, D = native.drawables;
    const ids = Array.from(P.ids);
    const i = ids.indexOf(id);
    if (i < 0) return null;
    const saved = Float32Array.from(P.values);
    try {
      for (let k = 0; k < P.count; k++) P.values[k] = P.defaultValues[k];
      native.update();
      const base = grab(native), op0 = Float32Array.from(D.opacities);
      P.values[i] = Math.min(P.maximumValues[i], Math.max(P.minimumValues[i], P.defaultValues[i] + val));
      native.update();
      const now = grab(native), op1 = Float32Array.from(D.opacities);
      let mx = 0;
      for (let k = 0; k < D.count; k++) {
        if (op0[k] < 0.5 || op1[k] < 0.5) continue;
        const d = moved(now[k], base[k]);
        if (d > mx) mx = d;
      }
      return mx / (eyeW || 1) * 100;
    } finally {
      for (let k = 0; k < P.count; k++) P.values[k] = saved[k];
      try { native.update(); } catch (e) { /* 下一帧会自己算 */ }
    }
  }

  return { setup, exprAmp };
});
