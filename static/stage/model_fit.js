/* =============================================================================
 * NekoModelFit —— 舞台用的「转换器 + 尺子 + 自适应校正」一体模块
 *
 * 一个模型第一次上台时：
 *   1) 读模型自带的 xxx.vtube.json（作者给的标准动作 → 本模型参数的映射），没有就用 LipSync 分组兜底；
 *   2) 用「尺子」（NekoRuler）在页面里真量一遍：张嘴、转头、点头、歪头各推一遍，读画面实际动了多少
 *      （以两颗眼珠中心距离为单位）；
 *   3) 和水水（Blue cat）的刻度比，算出每个通道「要写多少才和水水一样」的校正曲线（NekoCalib），
 *      存进 localStorage（按模型文件地址区分），下次上台直接用，不再量。
 * 每一帧：舞台按标准单位（水水上调出来的数值）写，本模块换算成本模型该写的参数和值。
 * 只改写入值，不改模型文件、不改模型参数默认值；舞台退出后模型完全是原样。
 * 量不出来 / 没加载到的通道：原样写（和接入之前一样）。
 * 依赖：NekoVtsMap（vts_map.js）、NekoRuler（ruler.js）、NekoCalib（calib.js），需先加载。
 * ========================================================================== */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.NekoModelFit = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  const VERSION = 5;   // 5：小表情（眉毛 / 嘴角）按画面拉齐（4：护栏；3：量时跑物理；2：张嘴按比例）；旧存档作废
  // 尺子刻度：水水在标准动作取值点上的读数（_tools/l2d_adapt/calib_run.js 实测，2026-11-19）
  const REF = {
    ParamMouthOpenY: [0, 7.024, 14.143, 21.107, 28.072],
    ParamAngleX: [-15.393, -7.824, 0, 7.637, 15.025],
    ParamAngleY: [-17.616, -8.806, 0, 1.598, 3.197],
    ParamAngleZ: [16.613, 8.307, 0, -8.325, -16.649],
  };
  // 小表情刻度：水水把参数从默认推 ±0.5 时，「两边都可见的网格」最大位移（眼距%）。expr_ref.js 实测。
  // 眉毛、嘴角这几条在每个模型上幅度差得最多（白毛宝宝眉毛是水水的 3 倍，嘴角只有 0.4 倍），按画面拉齐。
  // 眼睛笑（EyeSmile）水水本身几乎不动（0.006），没法当标准，不校正，原样写。
  const EXPR_REF = {
    ParamBrowLY: [1.788, 1.972], ParamBrowRY: [1.788, 1.972],
    ParamBrowLForm: [2.393, 3.777], ParamBrowRForm: [2.393, 3.777],
    ParamBrowLAngle: [2.529, 5.643], ParamBrowRAngle: [2.529, 5.643],
    ParamMouthForm: [2.832, 13.282],
  };
  const EXPR_PROBE = 0.5;
  const EXPR_GAIN = [0.33, 2.0];   // 只在这个范围里拉：再大会让嘴角 / 眉毛变形出界
  const KEY = 'nekoModelFit.v' + VERSION + ':';
  const CACHE = typeof WeakMap === 'function' ? new WeakMap() : null;
  const LOG = [];

  function coreOf(model) { const im = model && model.internalModel; return im && im.coreModel; }
  function nativeOf(core) { try { return core.getModel ? core.getModel() : core._model; } catch (e) { return null; } }
  function modelKey(model) {
    try { return String(model.internalModel.settings.url || ''); } catch (e) { return ''; }
  }
  function loadSaved(key) {
    try { const s = root.localStorage && root.localStorage.getItem(KEY + key); return s ? JSON.parse(s) : null; } catch (e) { return null; }
  }
  function save(key, v) {
    try { if (root.localStorage) root.localStorage.setItem(KEY + key, JSON.stringify(v)); } catch (e) { /* 存不下就每次量 */ }
  }

  async function build(model) {
    const core = coreOf(model);
    const native = nativeOf(core);
    const prof = { ok: false, source: '', vts: null, calib: {}, t0: Date.now() };
    const V = root.NekoVtsMap, R = root.NekoRuler, C = root.NekoCalib;
    if (!core || !native || !V || !R || !C) { prof.reason = 'missing-module'; return prof; }
    // 1) 转换器
    try { prof.vts = await V.load(model); } catch (e) { prof.vts = { map: {}, source: 'none' }; }
    prof.alias = aliasesOf(core);
    const convert = (std, v) => V.convert(prof.vts, std, v);
    // 2) 有存档就直接用
    const key = modelKey(model);
    const saved = key && loadSaved(key);
    if (saved && saved.calib) {
      prof.calib = saved.calib; prof.source = 'saved'; prof.ok = true; prof.measuredAt = saved.at;
      return prof;
    }
    // 3) 量一遍、算校正、存档
    try {
      const mouthEntry = prof.vts && prof.vts.map && prof.vts.map.ParamMouthOpenY;
      const ruler = R.setup(native, { mouthId: mouthEntry ? mouthEntry.id : 'ParamMouthOpenY' });
      const P = native.parameters, saved0 = Float32Array.from(P.values);
      for (let i = 0; i < P.count; i++) P.values[i] = P.defaultValues[i];
      native.update(); ruler.rest();
      for (let i = 0; i < P.count; i++) P.values[i] = saved0[i];
      C.setRef(REF);
      // 量的时候连物理一起跑（每个姿势固定跑 40 步 × 1/30 秒，跑稳了再读）：
      // 白毛宝宝这类转头靠物理带动的模型才量得出真实的转动。
      // 物理状态是共享的，量完把它推回静止姿势再跑一遍，避免残留的晃动带到舞台上。
      const phys = model.internalModel && model.internalModel.physics;
      const evalPhys = phys && typeof phys.evaluate === 'function' ? (phys.__nekoOrigEvaluate || phys.evaluate) : null;
      const settle = evalPhys ? (pi, v) => {
        for (let k = 0; k < 40; k++) {
          native.parameters.values[pi] = v;
          try { evalPhys.call(phys, core, 1 / 30); } catch (e) { break; }
          native.update();
        }
      } : null;
      prof.calib = C.calibrate(native, ruler, convert, settle);
      // 小表情：每条参数正负两个方向各量一次，增益 = 水水 / 本模型（夹在 0.33~2 之间）。
      // 模型上不动（=0）的不校正：那是作者没做这个表情，放大也没用。
      if (typeof R.exprAmp === 'function') {
        const W = ruler.info && ruler.info.eyeW;
        prof.calib.__expr = {};
        for (const id in EXPR_REF) {
          const up = R.exprAmp(native, id, EXPR_PROBE, W), dn = R.exprAmp(native, id, -EXPR_PROBE, W);
          if (up == null && dn == null) continue;
          const g = (ref, got) => (got > 0.05 ? Math.min(EXPR_GAIN[1], Math.max(EXPR_GAIN[0], ref / got)) : 1);
          prof.calib.__expr[id] = { up: +g(EXPR_REF[id][0], up || 0).toFixed(3), dn: +g(EXPR_REF[id][1], dn || 0).toFixed(3),
            ampUp: up == null ? null : +up.toFixed(3), ampDn: dn == null ? null : +dn.toFixed(3) };
        }
      }
      if (settle) {
        const P2 = native.parameters;
        for (let i = 0; i < P2.count; i++) P2.values[i] = P2.defaultValues[i];
        for (let k = 0; k < 40; k++) { try { evalPhys.call(phys, core, 1 / 30); } catch (e) { break; } }
        for (let i = 0; i < P2.count; i++) P2.values[i] = saved0[i];
        native.update();
      }
      prof.ruler = ruler.info;
      prof.source = 'measured';
      prof.ok = true;
      if (key) save(key, { at: new Date().toISOString(), calib: prof.calib });
    } catch (e) {
      prof.reason = 'calib-failed: ' + (e && e.message);
      prof.ok = !!(prof.vts && Object.keys(prof.vts.map || {}).length);
    }
    LOG.push({ key, source: prof.source, reason: prof.reason || '', vts: prof.vts && prof.vts.source,
      capped: Object.keys(prof.calib).filter(k => prof.calib[k] && prof.calib[k].capped) });
    if (LOG.length > 20) LOG.shift();
    return prof;
  }

  // 标准参数在模型上不存在、但有「拆开做」的替身时，按方向写到替身上（只在模型确实缺标准参数时生效）：
  //   嘴角：mao_pro 这类没有 ParamMouthForm，嘴角拆成 ParamMouthUp（默认 1 = 微笑）/ ParamMouthDown（撇嘴），
  //         标准值往下撇（<0）写 MouthDown = -v；往上（≥0）MouthDown 归 0，靠模型默认的微笑。
  const ALIAS = {
    ParamMouthForm: { id: 'ParamMouthDown', f: v => Math.max(0, -v) },
  };
  function aliasesOf(core) {
    const out = {};
    const has = id => { try { return core.getParameterIndex(id) >= 0; } catch (e) { return false; } };
    for (const std in ALIAS) if (!has(std) && has(ALIAS[std].id)) out[std] = ALIAS[std];
    return out;
  }

  // 标准参数 + 标准值 → [本模型参数, 写入值]：有校正曲线用曲线，否则用转换器，都没有就原样
  function resolve(prof, std, v) {
    const C = root.NekoCalib, V = root.NekoVtsMap;
    const al = prof && prof.alias && prof.alias[std];
    if (al) return [al.id, al.f(v)];
    // 小表情：按增益缩放偏离默认值的量（这几条参数默认值都是 0，舞台写的是「偏移」）
    const xg = prof && prof.calib && prof.calib.__expr && prof.calib.__expr[std];
    if (xg) return [std, v * (v >= 0 ? xg.up : xg.dn)];
    const e = prof && prof.calib && prof.calib[std];
    if (e && e.id && C) { const y = C.apply(e, v); if (y !== null) return [e.id, y]; }
    if (prof && prof.vts && V) return V.convert(prof.vts, std, v);
    return [std, v];
  }

  function wrap(core, prof) {
    if (!core || !prof || !prof.ok) return core;
    return {
      __fitted: true,
      __raw: core,
      __prof: prof,
      getParameterIndex: id => core.getParameterIndex(resolve(prof, id, 0)[0]),
      getParameterCount: () => core.getParameterCount(),
      getParameterMinimumValue: i => core.getParameterMinimumValue(i),
      getParameterMaximumValue: i => core.getParameterMaximumValue(i),
      getParameterDefaultValue: i => core.getParameterDefaultValue(i),
      getParameterValueById: id => core.getParameterValueById(resolve(prof, id, 0)[0]),
      setParameterValueById: (id, v, w) => {
        const r = resolve(prof, id, v);
        return w === undefined ? core.setParameterValueById(r[0], r[1]) : core.setParameterValueById(r[0], r[1], w);
      },
      addParameterValueById: (id, v, w) => {
        const r0 = resolve(prof, id, 0), r1 = resolve(prof, id, v);
        return w === undefined ? core.addParameterValueById(r1[0], r1[1] - r0[1]) : core.addParameterValueById(r1[0], r1[1] - r0[1], w);
      },
    };
  }

  const PENDING = typeof WeakSet === 'function' ? new WeakSet() : null;
  /** 舞台每帧调用：返回换算后的 core；第一次调用在后台开始读表 / 量模型，好之前返回原 core */
  // 总开关：localStorage['nekoModelFit.off'] = '1' 时不做任何换算（舞台按原样写，和接入前一样）
  function isOn() { try { return !(root.localStorage && root.localStorage.getItem('nekoModelFit.off') === '1'); } catch (e) { return true; } }
  function setOn(on) { try { if (on) root.localStorage.removeItem('nekoModelFit.off'); else root.localStorage.setItem('nekoModelFit.off', '1'); } catch (e) { } }
  function fitted(model) {
    const core = coreOf(model);
    if (!core) return core;
    if (!isOn()) return core;
    const prof = CACHE ? CACHE.get(core) : null;
    if (prof) return wrap(core, prof);
    if (PENDING && !PENDING.has(core)) {
      PENDING.add(core);
      // 不在模型更新回调里量：挪到下一个宏任务，量完再生效
      setTimeout(() => { build(model).then(p => { if (CACHE) CACHE.set(core, p); }).catch(() => {}); }, 0);
    }
    return core;
  }
  function profileOf(model) { const c = coreOf(model); return c && CACHE ? CACHE.get(c) || null : null; }
  // 清掉某个模型的存档（换了模型版本、想重新量时用）
  function forget(model) { try { root.localStorage.removeItem(KEY + modelKey(model)); } catch (e) { } const c = coreOf(model); if (c && CACHE) CACHE.delete(c); if (c && PENDING) PENDING.delete(c); }

  return { VERSION, REF, fitted, build, resolve, wrap, profileOf, forget, isOn, setOn, log: LOG };
});
