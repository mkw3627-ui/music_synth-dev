/* =============================================================================
 * NekoCalib —— 「量一遍 → 算出校正 → 写进转换配置 → 第二次运行就达标」
 *
 * 标准（尺子的刻度）= 水水（Blue cat）做我们标准动作时，尺子量出来的画面量。
 * 做法：
 *   1) 跑一遍：对每个通道，把转换器给的写入值按 0.25 / 0.5 / 0.75 / 1 倍逐级写进去，用尺子量画面量；
 *   2) 算校正：对比水水在同样标准动作下的画面量，得到「要达到标准，这个通道该写多少」的曲线
 *      （分段线性，张嘴这种非线性的也能对上）；
 *   3) 写进配置：每个模型一份 { 通道: [[标准值, 本模型写入值], ...] }，存在模型旁边的 neko.calib.json
 *      （或宿主数据目录），转换器下次读到就直接按曲线写。
 * 只改「写入值」，不改模型文件和模型自己的参数默认值；功能停用时模型完全是原样。
 * 参数区间够不到标准的，校正曲线停在区间端点，并标「模型上限」，不会把模型拉坏。
 * ========================================================================== */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.NekoCalib = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  // 要校正的通道：标准参数、用尺子的哪个读数、标准动作的取值点（水水上的参数值）
  const CHANNELS = {
    ParamMouthOpenY: { read: 'mouth', points: [0, 0.25, 0.5, 0.75, 1] },
    ParamAngleX: { read: 'turn', points: [-30, -15, 0, 15, 30] },
    ParamAngleY: { read: 'nod', points: [-30, -15, 0, 15, 30] },
    ParamAngleZ: { read: 'tilt', points: [-30, -15, 0, 15, 30] },
  };
  // 水水（Blue cat）的刻度：标准动作 → 尺子读数。由 build_ref.js 在水水上量出来后写死在这里。
  let REF = null;
  function setRef(r) { REF = r; }

  function interp(xs, ys, x) {
    if (x <= xs[0]) return ys[0];
    for (let k = 0; k < xs.length - 1; k++) {
      if (x <= xs[k + 1]) {
        const t = (x - xs[k]) / ((xs[k + 1] - xs[k]) || 1);
        return ys[k] + (ys[k + 1] - ys[k]) * t;
      }
    }
    return ys[ys.length - 1];
  }

  /**
   * 在一个模型上跑一遍、算校正曲线。
   * native：Cubism 原生模型；ruler：NekoRuler.setup 的结果（已 rest()）；
   * convert(std, v) → [真实参数ID, 写入值]：转换器（没有就原样）。
   * 返回 { 通道: { id, curve: [[标准值, 写入值]...], capped, measured: [...] } }
   */
  // settle(): 可选，把物理跑到稳定（白毛宝宝这类「转头只喂物理、物理再带动真正的头」的模型，
  // 不跑物理量出来就是 0，校正会一路推到顶格）。不传就只量直接形变。
  function calibrate(native, ruler, convert, settle) {
    if (!REF) throw new Error('没有水水的刻度（setRef）');
    const P = native.parameters;
    const ids = Array.from(P.ids);
    const saved = Float32Array.from(P.values);
    const reset = () => { for (let i = 0; i < P.count; i++) P.values[i] = P.defaultValues[i]; };
    const out = {};
    try {
      for (const std in CHANNELS) {
        const ch = CHANNELS[std];
        const ref = REF[std];
        if (!ref) continue;
        const [id] = convert(std, ch.points[ch.points.length - 1]);
        const pi = ids.indexOf(id);
        if (pi < 0) { out[std] = { id: null }; continue; }
        const lo = P.minimumValues[pi], hi = P.maximumValues[pi], def = P.defaultValues[pi];
        // 本模型「写入值 → 读数」曲线：在整个区间上取 21 个点量（含物理以外的全部形变）
        const xs = [], ys = [];
        for (let k = 0; k <= 20; k++) {
          const v = lo + (hi - lo) * k / 20;
          reset(); P.values[pi] = v;
          if (settle) settle(pi, v); else native.update();
          const r = ruler.read();
          xs.push(v); ys.push(r && typeof r[ch.read] === 'number' ? r[ch.read] : 0);
        }
        // 对每个标准取值点：水水读数是多少 → 在本模型曲线上找写入值（从默认值往同方向找第一个达到的点）
        const curve = [];
        let capped = false, guarded = 0;
        // 张嘴：按「比例」对齐，不按绝对大小。嘴天生小的模型（全张开也只有水水的一半）如果按绝对值追，
        // 平常说话的小口型会被一路放大到顶格（真宿主实测梦梦平均张嘴变成标准的 2.2 倍）。
        // 做法：把水水的刻度按「本模型全张开 / 水水全张开」缩小，曲线形状仍跟水水一样。
        let refK = 1;
        if (std === 'ParamMouthOpenY') {
          const modelMax = Math.max(...ys.map(Math.abs));
          const refMax = Math.max(...ref.map(Math.abs));
          if (modelMax > 0 && refMax > 0) refK = Math.min(1, modelMax / refMax);
        }
        for (let k = 0; k < ch.points.length; k++) {
          const sv = ch.points[k], want = ref[k] * refK;
          if (Math.abs(want) < 1e-6) { curve.push([sv, def]); continue; }
          // 先用转换器的写入值定方向
          const [, cv] = convert(std, sv);
          const dir = Math.sign(cv - def) || Math.sign(sv) || 1;
          // 沿方向扫描（细分到 200 段），找读数第一次达到 want 的位置
          let found = null, lastV = def, lastY = 0;
          const end = dir > 0 ? hi : lo;
          for (let s = 1; s <= 200; s++) {
            const v = def + (end - def) * s / 200;
            const y = interp(xs, ys, v);
            if ((want > 0 && y >= want) || (want < 0 && y <= want)) {
              const t = (want - lastY) / ((y - lastY) || 1);
              found = lastV + (v - lastV) * Math.min(1, Math.max(0, t));
              break;
            }
            lastV = v; lastY = y;
          }
          if (found === null) { found = end; capped = true; }
          // 安全护栏（第六十七轮）：校正结果必须和转换器同方向、且偏离不超过 0.4~2.5 倍。
          // 超出说明这一点量歪了（物理把头发/挂件的晃动算进了头部、或非线性绑定在某一侧突变），
          // 真宿主实测白毛宝宝点头出现过方向反过来、梦梦转头从 94% 掉到 51%，都是这种情况。
          // 量歪的点退回转换器的值，宁可不校正，也不把动作弄反或弄到顶格。
          if (std !== 'ParamMouthOpenY') {
            const base = cv - def, got = found - def;
            const ratio = Math.abs(base) > 1e-6 ? got / base : 1;
            if (!(ratio >= 0.4 && ratio <= 2.5)) { found = cv; guarded = (guarded || 0) + 1; }
          }
          curve.push([sv, +found.toFixed(4)]);
        }
        out[std] = { id, curve, guarded: guarded || undefined, capped: capped && refK === 1, scaled: refK < 1 ? +refK.toFixed(3) : undefined, lo, hi, def,
          measured: ch.points.map((sv, k) => ({ std: sv, ref: +ref[k].toFixed(2) })) };
      }
    } finally {
      for (let i = 0; i < P.count; i++) P.values[i] = saved[i];
      try { native.update(); } catch (e) { /* 下一帧会自己算 */ }
    }
    return out;
  }

  // 用校正曲线写：标准值 → 本模型写入值（曲线外按两端斜率外推，再夹在参数区间里）
  function apply(entry, v) {
    if (!entry || !entry.curve || !entry.curve.length) return null;
    const c = entry.curve, xs = c.map(p => p[0]), ys = c.map(p => p[1]);
    let y;
    if (v < xs[0]) y = ys[0] + (v - xs[0]) * (ys[1] - ys[0]) / ((xs[1] - xs[0]) || 1);
    else if (v > xs[xs.length - 1]) { const n = xs.length - 1; y = ys[n] + (v - xs[n]) * (ys[n] - ys[n - 1]) / ((xs[n] - xs[n - 1]) || 1); }
    else y = interp(xs, ys, v);
    return Math.min(entry.hi, Math.max(entry.lo, y));
  }

  return { CHANNELS, setRef, calibrate, apply, interp };
});
