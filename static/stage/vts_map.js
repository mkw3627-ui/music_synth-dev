/* =============================================================================
 * NekoVtsMap —— 标准动作 → 当前模型参数的「转换器」
 *
 * 思路（用户 2026-11-19 提的方案）：我们有一套标准动作（头转多少度、嘴张多大、笑多少……），
 * 换模型时由转换器把标准翻译成这个模型自己的参数；只在我们的功能写入那一帧生效，
 * 不改模型文件、不改模型的默认值，功能停了模型就是它原本的样子。
 *
 * 转换表从哪来，按可靠程度排：
 *   1) 模型自带的 VTube Studio 配置（xxx.vtube.json 的 ParameterSettings）。这是模型作者自己调好、
 *      随模型发布的「标准输入 → 本模型参数」映射，含输出区间和方向（例如眼球 X 是 1→-1 反向、
 *      张嘴 0→2.1、头 ±40）。VTube Studio 是 Live2D 主播圈最通用的软件，所以绝大多数模型都带这个文件。
 *   2) 没有 vtube.json：按 model3.json 的 LipSync / EyeBlink 分组、cdi3 显示名找同义参数（兜底）。
 *   3) 都找不到：原样写标准 ID（和改之前完全一样）。
 *
 * 我们的数值是在 Blue cat（水水）上调出来的参数值：先按水水自己的 vtube.json 反推成 VTS 输入量
 * （FaceAngleX 度数、MouthOpen 0..1、MouthSmile 0..1……），再按当前模型 vtube.json 里同一输入的行算出输出。
 * 参考：ARVTubeLink（GitHub，Android 面捕 → VTS）同样以「VTS 输入 → 模型 IN/OUT 映射」作为换模型的统一层。
 * ========================================================================== */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.NekoVtsMap = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  // 我们的数值是在 Blue cat（水水）上调出来的「参数值」。换算分两步：
  //   1) 用水水自己 vtube.json 里同一条映射反推出「VTS 输入量」（例：水水头 ±40 ↔ 输入 ±30，写 20 → 输入 15）；
  //   2) 再按当前模型 vtube.json 的同一条映射算出它的参数值。
  // 这样水水本身的效果一点不变（自己换算到自己），别的模型按作者给的区间和方向对齐水水。
  // 下表就是水水 vtube.json 的那几行：[输入名, 输入下限, 输入上限, 输出下限, 输出上限]
  const REF = {
    ParamAngleX: ['FaceAngleX', -30, 30, -40, 40],
    ParamAngleY: ['FaceAngleY', -20, 20, -40, 40],
    ParamAngleZ: ['FaceAngleZ', -30, 30, -30, 30],
    ParamBodyAngleX: ['FaceAngleX', -30, 30, -13.3, 13.3],
    ParamBodyAngleY: ['FaceAngleY', -30, 30, -20, 20],
    ParamBodyAngleZ: ['FaceAngleZ', -30, 30, -10, 10],
    ParamMouthForm: ['MouthSmile', 0, 1, -1.2, 1.2],
    ParamMouthOpenY: ['MouthOpen', 0, 1, 0, 2.1],
    ParamEyeBallX: ['EyeRightX', -1, 1, 1, -1],
    ParamEyeBallY: ['EyeRightY', -1, 1, -1, 1],
    ParamBrowLY: ['Brows', 0, 1, -1, 1],
    ParamBrowRY: ['Brows', 0, 1, -1, 1],
    ParamBrowLForm: ['Brows', 0, 1, -1, 1],
    ParamBrowRForm: ['Brows', 0, 1, -1, 1],
  };
  // 睁眼（ParamEyeL/ROpen）不换算：它是「状态量」，1 = 正常睁开在所有模型上意思一样；
  // 水水的 vtube.json 把睁眼映射到 0..1.9（睁大），按它反推会把别的模型的正常睁眼压成半闭（梦梦实测）。
  // 身体类参数：作者改了名时按名字认
  const BODY = { ParamBodyAngleX: 1, ParamBodyAngleY: 1, ParamBodyAngleZ: 1 };
  const STD = {};
  for (const k in REF) {
    const [input, iL, iH, oL, oH] = REF[k];
    // 标准值（水水参数值）→ VTS 输入量（不夹，超出部分按同斜率延伸）
    STD[k] = { input, body: !!BODY[k], toIn: v => iL + (v - oL) / (oH - oL) * (iH - iL) };
  }

  const CACHE = typeof WeakMap === 'function' ? new WeakMap() : null;
  const PENDING = typeof WeakSet === 'function' ? new WeakSet() : null;

  function coreOf(model) {
    const im = model && model.internalModel;
    return im && im.coreModel;
  }

  function lerpRange(x, inLo, inHi, outLo, outHi, clamp) {
    const span = inHi - inLo;
    let t = span ? (x - inLo) / span : 0;
    if (clamp) t = Math.min(1, Math.max(0, t));
    return outLo + (outHi - outLo) * t;
  }

  /**
   * 由 vtube.json 的 ParameterSettings 生成「标准参数 → [本模型参数, 输出值函数]」表。
   * 只用 OutputLive2D 本身就是这个标准参数的那一行（作者给这个参数配的区间和方向）：
   * 标准 ID 在模型里存在时，转换只是「按作者的区间重新缩放」；
   * 标准 ID 是作者绑到别的参数上的（例如 FaceAngleX → Param42），就改写到那个参数。
   */
  function fromVts(vts, has) {
    const rows = (vts && vts.ParameterSettings) || [];
    const map = {};
    for (const std in STD) {
      const spec = STD[std];
      // 优先：输入是这个通道、输出就是这个标准参数
      let row = rows.find(r => r.Input === spec.input && r.OutputLive2D === std);
      // 其次：输入是这个通道、输出是同类参数（头 / 身 / 嘴……），作者改了参数名
      if (!row) {
        const want = spec.body ? /body|身体/i : null;
        const cands = rows.filter(r => r.Input === spec.input && r.OutputLive2D && !STD[r.OutputLive2D] && has(r.OutputLive2D));
        row = want ? cands.find(r => want.test(r.OutputLive2D)) : (spec.body ? null : cands[0]);
      }
      if (!row || !has(row.OutputLive2D)) continue;
      const inLo = Number(row.InputRangeLower), inHi = Number(row.InputRangeUpper);
      const outLo = Number(row.OutputRangeLower), outHi = Number(row.OutputRangeUpper);
      if (!isFinite(inLo) || !isFinite(inHi) || !isFinite(outLo) || !isFinite(outHi) || inHi === inLo) continue;
      map[std] = { id: row.OutputLive2D, inLo, inHi, outLo, outHi, clamp: !!row.ClampOutput, src: 'vtube.json' };
    }
    return map;
  }

  async function fetchJson(url) {
    try { const r = await fetch(url, { cache: 'force-cache' }); return r.ok ? await r.json() : null; } catch (e) { return null; }
  }

  // 找模型目录下的 *.vtube.json：model3.json 不列它，只能按同名 / 常见名去试
  async function loadVts(model) {
    const st = model.internalModel && model.internalModel.settings;
    if (!st || !st.url) return null;
    const base = new URL(st.url, location.href);
    const file = decodeURIComponent(base.pathname.split('/').pop() || '');
    const stem = file.replace(/\.model3\.json$/i, '');
    const names = [stem + '.vtube.json', 'model.vtube.json'];
    if (st.name) names.push(String(st.name) + '.vtube.json');
    for (const n of names) {
      const j = await fetchJson(new URL(encodeURIComponent(n), base).href);
      if (j && Array.isArray(j.ParameterSettings)) return j;
    }
    return null;
  }

  // 没有 vtube.json 时的兜底：只做「名字认得出」的那几条，区间按参数自身的 min / max
  function fallback(model, core, has) {
    const map = {};
    try {
      const st = model.internalModel.settings;
      const g = (st && (st.groups || (st.json && st.json.Groups))) || [];
      const lip = (g.find(x => (x.Name || x.name) === 'LipSync') || {});
      const lipIds = (lip.Ids || lip.ids || []).filter(has);
      if (!has('ParamMouthOpenY') && lipIds.length) {
        const i = core.getParameterIndex(lipIds[0]);
        map.ParamMouthOpenY = { id: lipIds[0], inLo: 0, inHi: 1, outLo: core.getParameterMinimumValue(i),
          outHi: core.getParameterMaximumValue(i), clamp: true, src: 'LipSync 分组' };
      }
    } catch (e) { /* 没有分组 */ }
    return map;
  }

  /** 读这个模型的转换表（异步，结果缓存；没有就返回空表，写入原样透传） */
  async function load(model) {
    const core = coreOf(model);
    if (!core) return null;
    if (CACHE && CACHE.has(core)) return CACHE.get(core);
    const has = id => { try { return core.getParameterIndex(id) >= 0; } catch (e) { return false; } };
    const prof = { map: {}, source: 'none' };
    if (CACHE) CACHE.set(core, prof);
    const vts = await loadVts(model);
    if (vts) { prof.map = fromVts(vts, has); prof.source = 'vtube.json'; }
    const fb = fallback(model, core, has);
    for (const k in fb) if (!prof.map[k]) prof.map[k] = fb[k];
    if (prof.source === 'none' && Object.keys(fb).length) prof.source = 'fallback';
    return prof;
  }

  // 标准参数 + 标准值 → [本模型参数, 值]；表里没有就原样
  function convert(prof, std, v) {
    const m = prof && prof.map[std];
    if (!m) return [std, v];
    const spec = STD[std];
    return [m.id, lerpRange(spec.toIn(v), m.inLo, m.inHi, m.outLo, m.outHi, m.clamp)];
  }

  // 叠加写的偏移量：按同一条线性映射的斜率换算（偏移不加截距）
  function convertDelta(prof, std, dv) {
    const m = prof && prof.map[std];
    if (!m) return [std, dv];
    const spec = STD[std];
    const slope = (m.outHi - m.outLo) / (m.inHi - m.inLo);
    return [m.id, (spec.toIn(dv) - spec.toIn(0)) * slope];
  }

  // 本模型参数值 → 标准值（读回当前值时用）
  function toStd(prof, std, real) {
    const m = prof && prof.map[std];
    if (!m) return real;
    const x = lerpRange(real, m.outLo, m.outHi, m.inLo, m.inHi, false);
    const spec = STD[std];
    // toIn 都是线性的：解 spec.toIn(v) = x
    const a = spec.toIn(1) - spec.toIn(0), b = spec.toIn(0);
    return a ? (x - b) / a : real;
  }

  /** 包一层 coreModel：标准参数按表换算，其余原样；表没加载好时就是原 coreModel */
  function wrap(core, prof) {
    if (!core || !prof || !Object.keys(prof.map).length) return core;
    const M = prof.map;
    return {
      __vtsMapped: true,
      __raw: core,
      getParameterIndex: id => core.getParameterIndex(M[id] ? M[id].id : id),
      getParameterCount: () => core.getParameterCount(),
      getParameterMinimumValue: i => core.getParameterMinimumValue(i),
      getParameterMaximumValue: i => core.getParameterMaximumValue(i),
      getParameterDefaultValue: i => core.getParameterDefaultValue(i),
      getParameterValueById: id => M[id] ? toStd(prof, id, Number(core.getParameterValueById(M[id].id)) || 0) : core.getParameterValueById(id),
      setParameterValueById: (id, v, w) => { const r = convert(prof, id, v); return w === undefined ? core.setParameterValueById(r[0], r[1]) : core.setParameterValueById(r[0], r[1], w); },
      addParameterValueById: (id, v, w) => { const r = convertDelta(prof, id, v); return w === undefined ? core.addParameterValueById(r[0], r[1]) : core.addParameterValueById(r[0], r[1], w); },
    };
  }

  /** 一步到位：拿包好的 core；第一次调用在后台读表，读好之前返回原 core */
  function mapped(model) {
    const core = coreOf(model);
    if (!core) return core;
    const prof = CACHE ? CACHE.get(core) : null;
    if (prof) return wrap(core, prof);
    if (PENDING && !PENDING.has(core)) { PENDING.add(core); Promise.resolve().then(() => load(model)).catch(() => {}); }
    return core;
  }

  return { STD, load, convert, convertDelta, toStd, wrap, mapped, fromVts };
});
