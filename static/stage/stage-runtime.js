/* =============================================================================
 * 「为她而唱」舞台运行时（插件内）
 *
 * 入口：
 *   NekoStage.start({ show, clock, model, container, root, fps, hud, sub })
 *   NekoStage.stop()
 *   NekoStage.renderAt(t)     —— 纯函数：同一个 show + 同一个 t → 同一画面
 *   NekoStage.seek / play / pause / currentT / canvas / stats / info
 *
 * 设计约束（来自《舞台编排实施计划_v3》）：
 *   · 编排全部来自 show.json（组合 / 镜头 / 场景 / 事件），运行时不生成编排；
 *   · 人物只写传入模型对象的变换与参数，不新建、不替换宿主模型；stop() 完整还原；
 *   · 背景后备缓冲宽 ≤1400px、渲染上限 30fps、页面隐藏时停止渲染；
 *   · 人物姿态在 start 时一次性烘焙成时间采样，保证任意跳转画面一致（物理/呼吸关闭）。
 * ========================================================================== */
(function (global) {
  'use strict';
  const U = global.NekoStageU;
  const Scenes = global.NekoStageScenes;
  const Lyrics = global.NekoStageLyrics;

  /* ------------------------------ 常量表 ------------------------------ */
  // font-id → 本机字体族。第 2 阶段由 static/fonts + fonts.json 接管。
  const FONTS = {
    kaiti: '"KaiTi","STKaiti","楷体",serif',
    hei: '"SimHei","Microsoft YaHei",sans-serif',
    'hei-heavy': '"Microsoft YaHei","PingFang SC","Heiti SC",sans-serif',
    serif: '"SimSun","Songti SC",serif',
    mono: 'Consolas,"Courier New",monospace',
    system: '"Microsoft YaHei","PingFang SC",sans-serif'
  };
  // 风格预设里的渲染性格（动作幅度 / 摇摆周期 / 弹跳 / 段落默认情绪 / 基础字号）
  const PRESETS = {
    ballad: { fsBase: .074, motionK: .6, swayBeats: 4, bounceK: .25, emo: { intro: 'tender', verse: 'longing', pre: 'tender', chorus: 'tender', bridge: 'sad', final: 'joy', outro: 'tender' } },
    pop: { fsBase: .07, motionK: .85, swayBeats: 2, bounceK: .9, emo: { intro: 'playful', verse: 'playful', pre: 'joy', chorus: 'joy', bridge: 'playful', final: 'joy', outro: 'tender' } },
    edm: { fsBase: .068, motionK: .95, swayBeats: 2, bounceK: 1.1, emo: { intro: 'determined', verse: 'determined', pre: 'proud', chorus: 'proud', bridge: 'longing', final: 'proud', outro: 'determined' } },
    rock: { fsBase: .072, motionK: 1, swayBeats: 2, bounceK: .8, emo: { intro: 'determined', verse: 'proud', pre: 'proud', chorus: 'proud', bridge: 'longing', final: 'proud', outro: 'determined' } },
    cute: { fsBase: .072, motionK: .8, swayBeats: 3, bounceK: .7, emo: { intro: 'playful', verse: 'playful', pre: 'joy', chorus: 'joy', bridge: 'playful', final: 'joy', outro: 'tender' } },
    ancient: { fsBase: .072, motionK: .55, swayBeats: 4, bounceK: .3, emo: { intro: 'tender', verse: 'longing', pre: 'tender', chorus: 'tender', bridge: 'sad', final: 'tender', outro: 'tender' } },
    jazz: { fsBase: .068, motionK: .7, swayBeats: 4, bounceK: .5, emo: { intro: 'playful', verse: 'playful', pre: 'joy', chorus: 'joy', bridge: 'tender', final: 'joy', outro: 'tender' } },
    dark: { fsBase: .07, motionK: .8, swayBeats: 4, bounceK: .4, emo: { intro: 'determined', verse: 'sad', pre: 'longing', chorus: 'proud', bridge: 'sad', final: 'proud', outro: 'determined' } }
  };
  const EMO = {
    joy: { smile: .6, open: .95, browY: .3, browF: .3, browA: 0, mouth: .6, cheek: .3 },
    tender: { smile: .35, open: .85, browY: .1, browF: .2, browA: 0, mouth: .35, cheek: .45 },
    longing: { smile: .08, open: .78, browY: .4, browF: -.2, browA: .5, mouth: .05, cheek: .1 },
    sad: { smile: 0, open: .65, browY: .5, browF: -.5, browA: .7, mouth: -.4, cheek: 0 },
    proud: { smile: .2, open: 1, browY: .2, browF: .4, browA: -.1, mouth: .4, cheek: 0 },
    determined: { smile: 0, open: 1, browY: -.3, browF: -.3, browA: -.4, mouth: .05, cheek: 0 },
    playful: { smile: .5, open: .92, browY: .25, browF: .4, browA: 0, mouth: .55, cheek: .35 }
  };
  const SHOT = { close: { sf: 4.0, headY: .05 }, medium: { sf: 2.75, headY: .085 }, base: { sf: 2.0, headY: .124 }, wide: { sf: 1.02, headY: .07 } };
  // 共用表情映射（choreo/expression.py 同一组数值）：眉/脸颊/嘴/开合，两档一致。
  // 眼形（笑眼）不在共用表里：桌面档 g60 起禁写，舞台档仍用 EMO.smile 限幅。
  const EXPR_MAP = {
    joy: { browY: .45, browF: .45, browA: 0, mouth: .9, cheek: .45, open: .95 },
    playful: { browY: .375, browF: .6, browA: 0, mouth: .825, cheek: .525, open: .92 },
    tender: { browY: .15, browF: .3, browA: 0, mouth: .525, cheek: .675, open: .85 },
    longing: { browY: .6, browF: -.3, browA: .75, mouth: .075, cheek: .15, open: .78 },
    sad: { browY: .75, browF: -.75, browA: 1, mouth: -.6, cheek: 0, open: .65 },
    proud: { browY: .3, browF: .6, browA: -.15, mouth: .6, cheek: 0, open: 1 },
    determined: { browY: -.45, browF: -.45, browA: -.6, mouth: .075, cheek: 0, open: 1 }
  };
  // 强度分档：< .45 弱、< .70 中、其余强（与 expression.py TIERS 一致）
  function exprTier(intensity) {
    const x = Number(intensity);
    if (!isFinite(x)) return { name: 'mid', mult: .85 };
    if (x < .45) return { name: 'weak', mult: .6 };
    if (x < .70) return { name: 'mid', mult: .85 };
    return { name: 'strong', mult: 1 };
  }
  // 计划 v3 §7-11：Blue cat 的 EyeSmile 上限见 EYE_SMILE_CAP（bridge.js:439 = 0.12）。
  // 舞台按同一上限限幅写入，不是删除。
  const EYE_SMILE_CAP = 0.12;
  const CN = {
    shot: { close: '面部特写', medium: '近景', base: '半身', wide: '全身远景' },
    scene: { bokeh: '散景光斑', wave: '声线波纹', rings: '光环', beams: '舞台光束', led: 'LED 点阵', tunnel: '几何隧道' },
    entry: { iris: '圆形扩散', sweepL: '左侧扫入', sweepR: '右侧扫入', columns: '逐列点亮', flash: '闪白切入', fade: '淡入' },
    emo: { joy: '喜悦', tender: '温柔', longing: '思念', sad: '伤感', proud: '昂扬', determined: '坚定', playful: '俏皮' },
    gaze: { camera: '看镜头', far: '望向远处', down: '垂眸', side: '侧看', closed: '闭眼' },
    reveal: { rise: '深处浮出', ink: '墨迹晕开', wipe: '遮罩擦除', type: '逐字打出', drop: '坠落回弹', scatter: '碎片聚合', slam: '冲击砸入', glitch: '故障闪现' },
    stagger: { char: '逐字均匀', word: '按词', center: '中心扩散', random: '随机', cue: '卡唱词时间', whole: '整句' },
    layout: { h: '横排', v: '竖排', stack: '错落叠放', arc: '弧形', giant: '巨型关键词', center: '居中' },
    material: { solid: '实心', outline: '镂空描边', neon: '霓虹发光', grad: '渐变' },
    exit: { sink: '沉入背景', burst: '粒子散开', push: '被推走', dissolve: '溶解' }
  };
  const BAKE_FPS = 60;
  const GROOVE_IDS = ['ParamAngleX', 'ParamAngleY', 'ParamAngleZ', 'ParamBodyAngleX', 'ParamBodyAngleY', 'ParamBodyAngleZ'];
  let MAX_BG_W = 1920;   // 第三十八轮：背景画布 1400→1920 宽（素材发糊的主因之一）；第五十五轮按机器档位调
  /* 第五十五轮：舞台自适应画质。开演前按本机（逻辑核数 / 内存 / 显卡渲染器）定档，
   * 演出中再看实际帧时：连续 3 秒 p90 超过 22 ms 就降一档（只降背景/特效分辨率和帧率上限，不砍功能、不动歌词清晰度）。
   * full：背景 1920、特效满分辨率、帧率跟显示器；balanced：1440、3/4、上限 60；lite：1280、1/2、上限 30。 */
  const QUALITY = { full: { bg: 1920, fx: 1, cap: 0 }, balanced: { bg: 1440, fx: .75, cap: 60 }, lite: { bg: 1280, fx: .5, cap: 30 } };
  function detectQuality() {
    const forced = global.__NEKO_STAGE_QUALITY; if (forced && QUALITY[forced]) return { tier: forced, why: 'forced' };
    const cores = navigator.hardwareConcurrency || 4, mem = navigator.deviceMemory || 8;
    let renderer = '';
    try { const cv = document.createElement('canvas'), gl = cv.getContext('webgl'); const ext = gl && gl.getExtension('WEBGL_debug_renderer_info'); renderer = ext ? String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)) : ''; } catch (e) { }
    const soft = /swiftshader|llvmpipe|basic render|microsoft basic/i.test(renderer);
    const igpu = /intel.*(uhd|hd graphics|iris)|radeon\(tm\) graphics|vega \d+ graphics/i.test(renderer);
    let tier = 'full';
    if (soft || cores <= 2 || mem <= 2) tier = 'lite';
    else if (igpu || cores <= 4 || mem <= 4) tier = 'balanced';
    return { tier, why: 'cores=' + cores + ' mem=' + mem + ' gpu=' + renderer };
  }
  function applyQuality(tier) {
    const q = QUALITY[tier] || QUALITY.full; S.quality = tier; MAX_BG_W = q.bg; S.fxScale = q.fx; S.fpsCap = q.cap;
    global.__NEKO_STAGE_TIER = tier;
  }
  function watchQuality(frameMs) {
    const w = S._qw || (S._qw = { buf: [], since: performance.now() });
    w.buf.push(frameMs); if (w.buf.length > 240) w.buf.shift();
    const now = performance.now(); if (now - w.since < 3000 || w.buf.length < 90) return;
    const sorted = w.buf.slice().sort((x, y) => x - y), p90 = sorted[Math.floor(sorted.length * .9)];
    w.since = now; w.buf.length = 0;
    if (p90 > 22 && S.quality !== 'lite') { applyQuality(S.quality === 'full' ? 'balanced' : 'lite'); S._qDown = (S._qDown || 0) + 1; applySize(); }
  }

  /* ------------------------- 字体资产（第 10 维） ------------------------- */
  // static/fonts/fonts.json 与舞台同走 /stage/ 路由；启动时预加载 primary/accent，
  // 全部就绪或 3 秒超时才开演。兜底：主字体 → 同 persona 第一个 → 系统字体。
  const FONT_TIMEOUT = 3000;
  let fontManifest = null;

  function stageBase() { return global.__NEKO_STAGE_BASE || './'; }

  async function fetchManifest() {
    if (fontManifest) return fontManifest;
    try {
      const res = await fetch(stageBase() + 'fonts.json', { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      fontManifest = Array.isArray(data) ? { fonts: data } : data;
    } catch (e) {
      fontManifest = { fonts: [], error: String((e && e.message) || e) };
    }
    return fontManifest;
  }

  function pickFontEntry(entries, id, persona) {
    const byId = entries.find(e => e && e.id === id);
    if (id && id !== 'sys-default' && byId) return { entry: byId, how: 'id' };
    const byPersona = entries.find(e => e && e.persona === persona);
    if (byPersona) return { entry: byPersona, how: (id === 'sys-default' || !byId) ? 'persona' : 'persona' };
    return null;
  }

  async function ensureFont(sel, loaded) {
    if (!sel) return null;
    const e = sel.entry;
    if (loaded[e.id]) return loaded[e.id];
    const face = new FontFace(e.family, 'url("' + stageBase() + e.file + '")', { weight: String(e.weight || 400) });
    const timeout = new Promise(res => setTimeout(() => res('timeout'), FONT_TIMEOUT));
    const got = await Promise.race([face.load().then(() => 'ok').catch(err => 'error:' + err), timeout]);
    if (got === 'ok') {
      document.fonts.add(face);
      loaded[e.id] = { id: e.id, family: e.family, weight: e.weight };
      return loaded[e.id];
    }
    return { id: e.id, failed: true, why: got };
  }

  // 同 persona 的全部登记字体：主选在前，其余按 fonts.json 顺序作为按字符回退
  // （F-1 返工：cute_round / bold_pop 的假名与日文汉字由第二款字体兜住）
  function personaChain(entries, sel, persona) {
    if (!sel) return [];
    const out = [sel.entry];
    for (const e of entries) {
      if (e && e.persona === persona && e.id !== sel.entry.id) out.push(e);
    }
    return out;
  }

  // 只读 show + fonts.json，不碰渲染；预览页在 start 前 await 它
  async function loadFonts(show) {
    const tf = (show.style && show.style.typeface) || {};
    const persona = tf.persona || '';
    const manifest = await fetchManifest();
    const entries = manifest.fonts || [];
    const loaded = {};
    const psel = pickFontEntry(entries, tf.primary, persona);
    const asel = pickFontEntry(entries, tf.accent, persona) || psel;
    const pChain = personaChain(entries, psel, persona);
    const aChain = personaChain(entries, asel, persona);
    const all = [];
    for (const e of pChain.concat(aChain)) if (!all.some(x => x.id === e.id)) all.push(e);
    await Promise.all(all.map(e => ensureFont({ entry: e }, loaded)));
    const ok = x => !!(x && !x.failed);
    const fams = c => c.filter(e => ok(loaded[e.id])).map(e => '"' + e.family + '"');
    const pFams = fams(pChain), aFams = fams(aChain);
    const pGot = psel ? loaded[psel.entry.id] : null;
    const aGot = asel ? loaded[asel.entry.id] : null;
    return {
      primary: ok(pGot) ? pGot : null,
      accent: ok(aGot) ? aGot : (ok(pGot) ? pGot : null),
      primaryStack: pFams.length ? pFams.join(',') + ',' + FONTS.system : null,
      accentStack: aFams.length ? aFams.join(',') + ',' + FONTS.system : null,
      primaryChain: pFams.map(s => s.slice(1, -1)),
      accentChain: aFams.map(s => s.slice(1, -1)),
      primaryId: tf.primary || '', accentId: tf.accent || '',
      primaryHow: psel ? psel.how : 'none',
      accentHow: asel ? asel.how : 'none',
      persona, rules: tf.rules || {}, loaded: Object.keys(loaded).length
    };
  }

  function resolveCfg(show, fonts) {
    const tf = (show.style && show.style.typeface) || {};
    const pr = fonts && fonts.primary, ac = fonts && fonts.accent;
    const legacy = FONTS[tf.primary] || null;
    const rules = Object.assign({}, tf.rules || {});
    return {
      font: pr ? (fonts.primaryStack || ('"' + pr.family + '",' + FONTS.system)) : (legacy || FONTS.system),
      accentFont: ac ? (fonts.accentStack || ('"' + ac.family + '",' + FONTS.system)) : null,
      weight: tf.weight || 400,
      skew: tf.skew || 0,
      fsBase: S.preset.fsBase,
      kwScale: rules.kw_scale || 1.25,
      preset: show.style.preset,
      persona: (fonts && fonts.persona) || tf.persona || '',
      rules,
      decor: Array.isArray(rules.decor) ? rules.decor.slice() : [],
      maxChars: Math.max(1, Math.round(+rules.max_chars_per_row || 40)),
      verticalOk: rules.vertical_ok !== false,
      fontInfo: fonts ? {
        primary: pr ? pr.family : null, accent: ac ? ac.family : null,
        primaryId: fonts.primaryId, accentId: fonts.accentId,
        primaryHow: fonts.primaryHow, accentHow: fonts.accentHow,
        primaryChain: fonts.primaryChain, accentChain: fonts.accentChain,
        primaryStack: fonts.primaryStack, accentStack: fonts.accentStack,
        loaded: fonts.loaded
      } : null
    };
  }

  /* ------------------------------ 运行时状态 ------------------------------ */
  // P6 诊断计数（只读，供 NekoStage.info() 带出）：包装是否在调用链上、写入是否发生、写入后读回值
  const DIAG = { wrapCalls: 0, writeFrames: 0, readBackBrowLY: null, readBackCheek: null, writtenBrowLY: null, rebinds: 0, cbEnter: 0, cbErrors: 0, lastError: null, locPre: null, locPost: null, locAfterFrame: null, amu: 0, renderVals: null, renderT: null, renderPose: null, eyeBaked: null, eyeRunGaze: null, eyeT: null };
  // P6 第五轮埋点（只记录，不改行为）：时间线、实例 id、异常原文、解绑调用栈
  // P6 第十二轮：眼神状态（平滑跟随，只存状态，不直接写参数）。
  // 眼球一阶低通快跟随目标（先动）；头偏转慢跟随（后跟）；扫视是注视期内的小幅平滑包络。
  const GAZE_DIR = { camera: [0, 0], far: [.25, .35], down: [0, -.45], side: [.55, 0], closed: [0, 0] };
  const gazeSt = { ex: 0, ey: 0, hx: 0, hy: 0, lastGaze: null, t: -1 };
  function gazeStep(t, gaze, gs) {
    const dt = gazeSt.t < 0 ? 0 : Math.min(.1, Math.max(0, t - gazeSt.t));
    gazeSt.t = t;
    const d = GAZE_DIR[gaze] || GAZE_DIR.camera;
    const tx = d[0] * gs, ty = d[1];
    gazeSt.ex += (tx - gazeSt.ex) * (1 - Math.exp(-dt / .10));
    gazeSt.ey += (ty - gazeSt.ey) * (1 - Math.exp(-dt / .10));
    gazeSt.hx += (tx * .6 - gazeSt.hx) * (1 - Math.exp(-dt / .45));
    gazeSt.hy += (ty * .6 - gazeSt.hy) * (1 - Math.exp(-dt / .45));
    return { ex: gazeSt.ex, ey: gazeSt.ey, hx: gazeSt.hx, hy: gazeSt.hy };
  }
  const TRACE = [];
  const traceIds = new WeakMap();
  let traceSeq = 0;
  function traceId(o) {
    if (!o || typeof o !== 'object') return 'none';
    if (!traceIds.has(o)) traceIds.set(o, ++traceSeq);
    return 'm' + traceIds.get(o);
  }
  function traceLog(kind, extra) {
    if (TRACE.length < 300) TRACE.push(Object.assign({ k: kind, t: Date.now() }, extra || {}));
  }
  function traceStack() {
    try { return String(new Error().stack).split('\n').slice(2, 5).map(function (x) { return x.trim(); }).join(' | '); } catch (e) { return ''; }
  }
  // 定位（只读）：读取三个参数的当前值
  // 标准区间（Live2D 官方标准参数表的常用区间）；模型区间不同则线性换算；没有这个参数就原样（写入会被忽略）
  const STD_RANGE = { ParamAngleX: 30, ParamAngleY: 30, ParamAngleZ: 30, ParamBodyAngleX: 10, ParamBodyAngleY: 10, ParamBodyAngleZ: 10, ParamEyeBallX: 1, ParamEyeBallY: 1 };
  const RANGE_CACHE = new WeakMap();
  function stdToModel(c, id, v) {
    const std = STD_RANGE[id]; if (!std) return v;
    let m = RANGE_CACHE.get(c); if (!m) { m = {}; RANGE_CACHE.set(c, m); }
    let r = m[id];
    if (r === undefined) {
      r = null;
      try { const ix = c.getParameterIndex(id); if (ix >= 0) { const lo = Number(c.getParameterMinimumValue(ix)), hi = Number(c.getParameterMaximumValue(ix)); if (hi > lo) r = Math.max(Math.abs(lo), Math.abs(hi)); } } catch (e) { }
      m[id] = r;
    }
    return r ? v * (r / std) : v;
  }
  function clampParam(c, id, v) {
    const idx = c.getParameterIndex(id);
    if (idx < 0) return v;
    const lo = Number(c.getParameterMinimumValue(idx)), hi = Number(c.getParameterMaximumValue(idx));
    return Math.min(hi, Math.max(lo, v));
  }
  function locRead(c) {
    const o = {};
    for (const id of ['ParamBrowLY', 'ParamCheek', 'ParamEyeLSmile']) {
      try { o[id] = Number(c.getParameterValueById(id)); } catch (e) { o[id] = null; }
    }
    return o;
  }
  // 诊断：把异常的 message 与 stack 前几行记入 DIAG（只读记录，不改流程）
  // 边框上显示的歌名：口型服务报的歌名（播放器标题）优先，「final」这类文件名不算；其次编排主题里的「歌名」
  function stageTitle() {
    const lp = global.__nekoMusicLips;
    let s = String((lp && lp.song) || '').trim();
    if (!s || /^(final|final\.wav|song|track|untitled)$/i.test(s)) s = S.title || '';
    if (/^(final|song|track)$/i.test(s)) s = '';
    return s;
  }
  // 每种风格可用的场景（与 choreo/presets.py 的 scenePool 同步）
  const SCENE_POOLS = {"ballad": ["skyline", "galaxy", "rainwin", "sky", "sakura", "sunset", "aurora", "lantern", "bubbles", "ocean", "forest", "cafe", "rooftop", "classroom", "space", "starstage"], "pop": ["concert", "confetti", "skyline", "pattern", "sky", "starstage", "bubbles", "sunset", "neoncity", "hearts", "synth", "festival", "classroom", "kaleido", "wavefield"], "edm": ["concert", "galaxy", "skyline", "neoncity", "starstage", "tunnel", "aurora", "beams", "synth", "space", "wavefield", "kaleido", "crystal", "festival"], "rock": ["concert", "skyline", "neoncity", "starstage", "rainwin", "beams", "galaxy", "sunset", "synth", "rooftop", "wavefield", "festival", "crystal", "space"], "cute": ["pattern", "sakura", "confetti", "bubbles", "sky", "starstage", "lantern", "concert", "hearts", "festival", "classroom", "crystal", "kaleido", "ocean"], "ancient": ["sakura", "lantern", "sky", "aurora", "galaxy", "sunset", "rainwin", "forest", "festival", "classroom", "ocean", "cafe", "crystal"], "jazz": ["skyline", "rainwin", "sunset", "galaxy", "neoncity", "concert", "aurora", "cafe", "rooftop", "classroom", "space", "ocean", "starstage"], "dark": ["rainwin", "galaxy", "neoncity", "skyline", "aurora", "concert", "tunnel", "crystal", "space", "forest", "synth", "rooftop", "ocean"]};
  const BG_ENTRIES = {
    intro: ['iris', 'diamond', 'clock', 'split'], verse: ['sweepL', 'sweepR', 'columns', 'diag', 'blinds', 'dots', 'sweepU'],
    pre: ['sweepR', 'iris', 'split', 'diag', 'sweepD', 'dots'], chorus: ['diamond', 'split', 'clock', 'iris'],
    final: ['diamond', 'clock', 'split'], bridge: ['blinds', 'sweepD', 'dots', 'split'], outro: ['iris', 'sweepU', 'split']
  };
  // 第四十九轮：背景段太短会「一秒一换」：短于 6 s 的并进前一段；同一主题相邻同场景也并成一段
  function mergeShortScenes(list) {
    const out = [];
    for (const sc of list) {
      const prev = out[out.length - 1];
      if (prev && (sc.t1 - sc.t0 < 6 || prev.scene === sc.scene)) { prev.t1 = sc.t1; continue; }
      out.push(Object.assign({}, sc));
    }
    if (out.length > 1 && out[0].t1 - out[0].t0 < 6) { out[1].t0 = out[0].t0; out.shift(); }
    return out;
  }

  function artScenes(scenes, art) {
    const nBy = {}; let prevE = '';
    const ok = n => !global.NekoStageScenes || global.NekoStageScenes.SCENES[n];
    return scenes.map((sc, i) => {
      const role = ({ intro: 'calm', verse: 'calm', pre: 'calm', chorus: 'peak', final: 'peak', bridge: 'alt', outro: 'calm' })[sc.section] || 'calm';
      const n = nBy[role] = (nBy[role] == null ? 0 : nBy[role] + 1);
      let scene = art.sceneFor(sc.section, n);
      if (!ok(scene)) scene = art.sceneFor(sc.section, n + 1);
      if (!ok(scene)) scene = sc.scene;
      let entry = art.entryFor(sc.section, i);
      if (entry === prevE) entry = art.entryFor(sc.section, i + 1);
      prevE = entry;
      return Object.assign({}, sc, { scene, entry, var: U.h01(S.runSeed * .00031 + i * 1.913), theme: art.themeOf(sc.section).name });
    });
  }
  function remixScenes(scenes, show, rs) {
    const preset = (show.style && show.style.preset) || 'pop';
    let pool = (SCENE_POOLS[preset] || SCENE_POOLS.pop || []).filter(n => !global.NekoStageScenes || global.NekoStageScenes.SCENES[n]);
    // 第四十轮：素材库学来的图案也进池（标签匹配这种风格的；最多占 1/3，避免全是学来的）
    try {
      const SC = global.NekoStageScenes || {};
      const tpls = SC.templatesFor ? SC.templatesFor(preset) : [];
      // 混合模板优先（它们是新组合），学来的配色图案其次
      const learned = tpls.concat(SC.learnedFor ? SC.learnedFor(preset) : []);
      if (learned.length) {
        const r0 = k => U.h01(rs * .00043 + k * 3.17);
        const take = learned.slice().sort((a, b) => r0(a.length + a.charCodeAt(9)) - r0(b.length + b.charCodeAt(9))).slice(0, Math.max(1, Math.round(pool.length / 3)));
        pool = pool.concat(take);
      }
    } catch (e) { }
    if (!pool.length) return scenes;
    const r = k => U.h01(rs * .00031 + k * 1.913);
    // 洗牌后按段落顺序取；副歌和最终副歌共用一个场景（呼应），尾声回到前奏
    const order = pool.slice();
    for (let i = order.length - 1; i > 0; i--) { const j = Math.floor(r(i) * (i + 1)); const x = order[i]; order[i] = order[j]; order[j] = x; }
    const bySec = {}; let k = 0, prevE = '';
    // 第四十二轮：按标签匹配段落——效果库标签（本地模型打的：适合哪些段落 + 能量）决定这一段优先用哪些背景
    const TAGS = global.NekoStageTags || {};
    const secMap = { pre: 'verse', final: 'chorus' };
    const fitScore = (name, sec, energy) => {
      const t = TAGS[name + '@scenes'];
      if (!t) return .5;
      let sc0 = (t.sections || []).includes(secMap[sec] || sec) ? 1 : 0;
      if (t.energy != null) sc0 += 1 - Math.abs(t.energy - energy);
      return sc0 / 2;
    };
    const used = new Set();
    return scenes.map((sc, i) => {
      let scene;
      const energy = sc.section === 'chorus' || sc.section === 'final' ? .85 : (sc.section === 'bridge' || sc.section === 'intro' || sc.section === 'outro' ? .3 : .55);
      if (sc.section === 'final' && bySec.chorus) scene = bySec.chorus;
      else if (sc.section === 'outro' && bySec.intro) scene = bySec.intro;
      else if ((sc.section === 'chorus') && bySec.chorus && r(50 + i) < .5) scene = bySec.chorus;
      else {
        // 从洗过牌的池子里看接下来 5 个候选，挑标签最贴合这一段的（再加一点随机），用过的尽量不重复
        const look = [];
        for (let j = 0; j < Math.min(5, order.length); j++) look.push(order[(k + j) % order.length]);
        look.sort((a, b) => (fitScore(b, sc.section, energy) + r(300 + i * 7 + b.length) * .35 - (used.has(b) ? .6 : 0))
          - (fitScore(a, sc.section, energy) + r(300 + i * 7 + a.length) * .35 - (used.has(a) ? .6 : 0)));
        scene = look[0]; k++;
      }
      used.add(scene);
      if (!bySec[sc.section]) bySec[sc.section] = scene;
      const cand = (BG_ENTRIES[sc.section] || ['split']).filter(e => e !== prevE);
      const entry = cand[Math.floor(r(100 + i) * cand.length)] || 'split';
      prevE = entry;
      return Object.assign({}, sc, { scene, entry, var: r(200 + i) });
    });
  }
  function recordErr(where, e) {
    DIAG.cbErrors += 1;
    const st = e && e.stack ? String(e.stack).split('\n').slice(0, 3).join(' | ') : '';
    DIAG.lastError = where + ': ' + String(e && e.message ? e.message : e) + ' @ ' + st;
  }
  const S = {
    api: null, show: null, root: null, wrap: null, els: null,
    W: 1280, H: 720, BW: 640, BH: 360,
    beat: .5, bar: 2, dur: 0, seed: 0,
    sections: [], shots: [], scenes: [], events: [],
    preset: PRESETS.pop, cfg: null, lyrics: null,
    model: null, container: null, modelSaved: null, poseOut: null,
    clock: null, playing: false, pausedT: 0, wall0: 0, show0: 0,
    raf: 0, lastPaint: 0, dirty: true, fps: 30, hidden: false,
    hud: true, subOn: true, statsOn: false, stats: [],
    lastT: 0, lastInfo: null
  };

  /* ------------------------------ show 派生 ------------------------------ */
  function sectionAt(t) {
    for (const s of S.sections) if (t < s.t1) return s;
    return S.sections[S.sections.length - 1];
  }
  function pulseAt(t) {
    const bp = t / S.beat, ph = U.fract(bp);
    return Math.exp(-ph * 6) * (U.fract(bp / 4) < .25 ? 1.35 : 1);
  }
  function intensityAt(t) {
    const iv = S.show.intensity;
    if (iv && iv.v && iv.v.length) {
      const hop = iv.hop || .1;
      const f = t / hop;
      const i = U.clamp(Math.floor(f), 0, iv.v.length - 2);
      const a = U.clamp(f - i, 0, 1);
      const i2 = Math.min(i + 1, iv.v.length - 1);
      return U.lerp(iv.v[i], iv.v[i2], a);
    }
    const s = sectionAt(t);
    return s.energy != null ? s.energy : .5;
  }
  function evAmp(t, type, decay) {
    let a = 0;
    for (const e of S.events) {
      if (e.t > t) break;
      if (e.type !== type) continue;
      const d = t - e.t;
      if (d < 1.5) a = Math.max(a, (e.amp || 0) * Math.exp(-d * decay));
    }
    return a;
  }
  // 第四十六轮：编排给的镜头表里常有一个镜头拿 20~30 秒不换（例如繁星之海 17.6→44.6 s 一直是面部特写），
  // 叠加的运镜在特写上又被封顶压住 → 看起来「没有运镜」。这里把超过 ~10 s 的镜头按 8 小节拆开，
  // 插入景别交替（特写↔近景↔半身）和左右构图变化，用缓动推拉过去（不硬切），每次演出按种子不同。
  // 第四十七轮：把时间吸附到最近的小节线（重拍）——插入的景别变化、运镜小段边界都落在重拍上
  function snapBar(t) {
    const D = S.downbeats; if (!D || !D.length) return t;
    let lo = 0, hi = D.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (D[mid] < t) lo = mid + 1; else hi = mid; }
    const a = D[Math.max(0, lo - 1)], b2 = D[lo];
    const best = Math.abs(a - t) < Math.abs(b2 - t) ? a : b2;
    return Math.abs(best - t) < 2.5 ? best : t;
  }
  // 拍内相位：0 在拍点，用来做「卡拍推进」——每拍开头快、拍尾慢
  // 返回「已经走过的拍数」，拍内用 ease-out（拍点上起步快、拍尾停稳），整体单调递增
  function beatEase(t) {
    const bt = S.beat || .5, x = t / bt, k = Math.floor(x), ph = x - k;
    return k + (1 - Math.pow(1 - ph, 3));
  }

  // 第四十七轮：底色皮肤跟段落主题（深色主题配深色底，不再整首奶白）；浅/深切换时同步字色类
  function skinFor(t) {
    const Sk = global.NekoStageSkins;
    const id = S.art && S.art.skinAt ? S.art.skinAt(t) : null;
    const sk = (id && Sk && Sk.SKINS[id]) || S.skin;
    if (sk && S.root && S._skinId !== sk.id) {
      if (S._skinId) S.root.classList.remove('ns-skin-' + S._skinId);
      S.root.classList.add('ns-skin-' + sk.id);
      S.root.classList.toggle('ns-light', !!sk.light);
      S._skinId = sk.id;
    }
    // 第五十轮：线系主题的字色类（浅底墨字 / 深底白字）
    const g = S.art && S.art.isLineAt && S.art.isLineAt(t) ? (S.art.groundAt(t) === 'ink' ? 'ink' : 'paper') : '';
    if (S.root && S._lineCls !== g) {
      S.root.classList.remove('ns-line-paper', 'ns-line-ink');
      if (g) S.root.classList.add('ns-line-' + g);
      S._lineCls = g;
    }
    return sk;
  }

  function splitLongShots(shots, show) {
    if (!shots.length) return shots;
    const bpm = (show.music && show.music.bpm) || 120, bar = 240 / bpm;
    const step = Math.max(7, Math.min(12, bar * 8)), dur = +show.duration || 0;
    const NEXT = { close: ['medium', 'base'], medium: ['close', 'base'], base: ['medium', 'close', 'wide'], wide: ['base', 'medium'] };
    const out = [];
    for (let i = 0; i < shots.length; i++) {
      const s0 = shots[i]; out.push(s0);
      const end = i + 1 < shots.length ? shots[i + 1].t : dur;
      if (!(end - s0.t > step * 1.35)) continue;
      let prev = s0, k = 1;
      for (let tt = s0.t + step; tt < end - step * .5; tt += step, k++) {
        const r = U.h01((show.seed || 1) * .0013 + s0.t * .77 + k * 3.1);
        const cand = NEXT[prev.kind] || NEXT.base;
        const kind = cand[Math.floor(r * cand.length)];
        const cxs = [.36, .5, .64].filter(x => Math.abs(x - prev.cx) > .05);
        const cx = cxs[Math.floor(U.h01(r * 91.7 + k) * cxs.length)];
        const sh = { t: tt, kind, cx, rot: 0, cut: false, ease_beats: 6, shake: 0, _split: true };
        out.push(sh); prev = sh;
      }
    }
    return out;
  }

  // 第五十轮：当前是不是伴奏等待段。返回 { a: 0..1（进出场 1.2 s），kind, side, n }
  function vignetteAt(t) {
    // 第五十五轮：用户要求撤掉伴奏段线稿小画（心电波/钟/唱片等），伴奏时回到第五十轮之前的舞台走法。
    // 只有 show.style.vignettes === true 才会再打开（面板不提供）。
    if (!(S.show && S.show.style && S.show.style.vignettes === true)) return { a: 0, n: 0, side: 1, kind: '' };
    const G = S.gaps || [];
    for (let i = 0; i < G.length; i++) {
      const g = G[i];
      if (t < g[0] - .2 || t > g[1] + .2) continue;
      const a = U.clamp(Math.min((t - g[0]) / 1.2, (g[1] - t) / 1.2));
      return { a, n: i, side: U.h01(S.seed * .0031 + i * 5.7) < .5 ? -1 : 1,
        kind: S.art && S.art.vignetteFor ? S.art.vignetteFor(i, (g[0] + g[1]) / 2) : ['pulse', 'clock', 'record', 'piano'][i % 4] };
    }
    return { a: 0, n: 0, side: 1, kind: 'pulse' };
  }

  // 第四十九轮：镜头平滑器（临界阻尼弹簧，按真实帧间隔积分，帧率高低都一样丝滑）
  function smoothCam(c, t) {
    const st = S._camSm;
    // 积分步长用真实墙钟（音频时钟会前后抖几毫秒，dt≤0 时以前直接重置 → 人物一帧跳到目标位置）
    const now = performance.now() / 1000;
    const dt = st ? Math.min(.1, now - st.wall) : 0;
    const seek = st && Math.abs(t - st.t) > 1.2;   // 真正的跳时间（拖进度/换歌）才直接对齐
    if (!st || seek) { S._camSm = { t, wall: now, sf: c.sf, headY: c.headY, cx: c.cx, rot: c.rot, v: { sf: 0, headY: 0, cx: 0, rot: 0 } }; return c; }
    st.wall = now;
    if (dt <= 0) return Object.assign({}, c, { sf: st.sf, headY: st.headY, cx: st.cx, rot: st.rot });
    const w = 14;   // 角频率：约 0.25 s 跟上目标
    for (const k of ['sf', 'headY', 'cx', 'rot']) {
      const x = st[k], v = st.v[k], target = c[k];
      const f = 1 + 2 * dt * w + dt * dt * w * w;
      const nv = (v - dt * w * w * (x - target)) / f;
      st[k] = x + dt * nv; st.v[k] = nv;
    }
    st.t = t;
    return Object.assign({}, c, { sf: st.sf, headY: st.headY, cx: st.cx, rot: st.rot });
  }

  function cameraAt(t) {
    let i = 0;
    for (let k = 0; k < S.shots.length; k++) if (S.shots[k].t <= t) i = k;
    const s = S.shots[i], p = S.shots[Math.max(0, i - 1)];
    const base = SHOT[s.kind] || SHOT.base;
    let sf = base.sf, headY = base.headY, cx = s.cx, rot = s.rot;
    // 第四十九轮：硬切也不瞬移——用 0.5 s 的快速推拉过去（以前一帧跳 350~720 px，看起来像人物瞬移）
    const ease = s.cut ? Math.min(.55, Math.max(.35, S.beat || .5)) : (s.ease_beats || 4) * S.beat;
    if (i > 0 && ease > 0) {
      const u = s.cut ? U.easeInOut(U.easeInOut(U.clamp((t - s.t) / ease))) : U.easeInOut(U.clamp((t - s.t) / ease));
      const pb = SHOT[p.kind] || SHOT.base;
      sf = U.lerp(pb.sf, base.sf, u);
      headY = U.lerp(pb.headY, base.headY, u);
      cx = U.lerp(p.cx, s.cx, u);
      rot = U.lerp(p.rot || 0, s.rot || 0, u);
    }
    // 第三十七~三十八轮：运镜（叠加在镜头表上，不改切点）。8 种动作，按段落候选 + 种子挑，每个段落不同：
    //   push 慢推 / pull 慢拉 / truck 横移 / orbit 小弧环绕 / tilt 摇头（上下） / dutch 荷兰角摆动 /
    //   bob 节拍上下浮动 / spiral 推进+环绕 / 进副歌一拍推拉变焦 / 全程极轻手持呼吸
    {
      const sec0 = sectionAt(t), bt = S.beat || .5, ph = t / bt;
      // 第四十六轮：段内按 8 小节（32 拍，最短 8 s）再切小段，每小段一个运镜——
      // 以前一整段（常常 20 s+）只用一个动作、幅度又小，看起来就是「没有运镜」
      const subLen = Math.max(8, bt * 32), secDur = Math.max(1, sec0.t1 - sec0.t0);
      const nSub = Math.max(1, Math.round(secDur / subLen)), subD = secDur / nSub;
      const sub = Math.min(nSub - 1, Math.floor((t - sec0.t0) / subD));
      const sec = { id: sec0.id, t0: snapBar(sec0.t0 + sub * subD), t1: snapBar(sec0.t0 + (sub + 1) * subD) };
      if (sec.t1 <= sec.t0 + 1) sec.t1 = sec.t0 + subD;
      const dur = Math.max(1, sec.t1 - sec.t0);
      // 第四十七轮：镜头按节奏走——进度按拍子一步一步推（每拍前 1/3 走大半，后面缓），不再匀速滑
      const nb = Math.max(1, dur / (S.beat || .5));
      const u = U.clamp((beatEase(t) - beatEase(sec.t0)) / nb);
      const CAND = {
        intro: ['push', 'truck', 'tilt'], verse: ['push', 'truck', 'tilt', 'pull'], pre: ['push', 'spiral', 'truck'],
        chorus: ['orbit', 'bob', 'dutch', 'spiral'], final: ['orbit', 'spiral', 'dutch', 'bob'],
        bridge: ['pull', 'tilt', 'truck'], outro: ['pull', 'truck']
      };
      // 第四十五轮：运镜跟主题性格走（美术指导给出 calm / idol / mv / cyber），并加入新动作：
      //   corner 角落特写（推到画面一角——手、裙摆、发饰、脸——再慢慢拉回全景）/ reveal 先特写后拉开揭示 /
      //   whip 甩镜（段首快速横甩后稳住）/ stepzoom 跟拍子一格一格推近 / sweep 大幅横摇扫过人物 / float 失重漂浮
      const STYLE_CAND = {
        calm: { intro: ['reveal', 'push', 'float'], verse: ['push', 'truck', 'float', 'corner'], pre: ['push', 'corner'], chorus: ['orbit', 'push', 'float'], final: ['orbit', 'reveal'], bridge: ['pull', 'float', 'corner'], outro: ['pull', 'float'] },
        idol: { intro: ['reveal', 'sweep'], verse: ['truck', 'bob', 'corner'], pre: ['stepzoom', 'spiral'], chorus: ['orbit', 'bob', 'stepzoom', 'sweep'], final: ['orbit', 'stepzoom', 'spiral'], bridge: ['pull', 'corner'], outro: ['pull', 'sweep'] },
        mv: { intro: ['reveal', 'corner'], verse: ['corner', 'truck', 'sweep', 'push'], pre: ['corner', 'push'], chorus: ['sweep', 'orbit', 'dutch', 'corner'], final: ['reveal', 'orbit', 'sweep'], bridge: ['corner', 'float', 'pull'], outro: ['pull', 'reveal'] },
        cyber: { intro: ['whip', 'stepzoom'], verse: ['whip', 'truck', 'corner'], pre: ['stepzoom', 'whip'], chorus: ['stepzoom', 'dutch', 'whip', 'spiral'], final: ['stepzoom', 'spiral', 'whip'], bridge: ['corner', 'pull'], outro: ['pull', 'whip'] }
      };
      const camStyle = S.art && S.art.camFor ? S.art.camFor(sec.id) : null;
      const list = (camStyle && STYLE_CAND[camStyle] && STYLE_CAND[camStyle][sec.id]) || CAND[sec.id] || ['push', 'truck'];
      const si = Math.max(0, S.sections.indexOf(sec0)) * 16 + sub;
      // 相邻小段不重复同一个动作
      let mvi = Math.floor(U.h01(S.seed * .0011 + si * 2.37) * list.length);
      if (sub > 0 && list.length > 1 && mvi === Math.floor(U.h01(S.seed * .0011 + (si - 1) * 2.37) * list.length)) mvi = (mvi + 1) % list.length;
      const mv = list[mvi];
      const dir = U.h01(S.seed * .0017 + si) < .5 ? -1 : 1;
      let push = 0, pan = 0, roll = 0, lift = 0;
      if (mv === 'push') push = .16 * U.easeInOut(u);
      else if (mv === 'pull') push = .14 - .2 * U.easeInOut(u);
      else if (mv === 'truck') { pan = dir * (.08 - .16 * U.easeInOut(u)); push = .05; }
      else if (mv === 'orbit') { pan = Math.sin(ph * Math.PI / 8) * .035; roll = Math.sin(ph * Math.PI / 8 + .6) * 1.3; push = .025 * Math.sin(ph * Math.PI / 4); }
      else if (mv === 'tilt') { lift = Math.sin(t * .35) * .03 + .025 * dir * U.easeInOut(u); push = .06 * U.easeInOut(u); }
      else if (mv === 'dutch') { roll = dir * (1.6 * Math.sin(ph * Math.PI / 8)); push = .03; }
      else if (mv === 'bob') { lift = Math.sin(ph * Math.PI) * .012 * (1 + intensityAt(t)); push = .06 * U.easeInOut(u) + .04 * Math.abs(Math.sin(ph * Math.PI / 2)); pan = Math.sin(ph * Math.PI / 8) * .05 * dir; roll = Math.sin(ph * Math.PI / 8 + .5) * .8; }
      else if (mv === 'spiral') { push = .08 * U.easeInOut(u); pan = Math.sin(u * Math.PI * 2) * .03 * dir; roll = Math.sin(u * Math.PI * 2) * dir; }
      else if (mv === 'corner') {
        // 角落特写：前 35% 推到选中的一角（偏移 + 放大），停一会儿，后 35% 拉回；落点按种子挑（左上发饰/右上脸侧/左下手/右下裙摆）
        const spots = [[-.12, .06], [.12, .05], [-.1, -.08], [.11, -.1]];
        const sp = spots[Math.floor(U.h01(S.seed * .0023 + si * 5.1) * spots.length)];
        const k = u < .35 ? U.easeInOut(u / .35) : u > .65 ? 1 - U.easeInOut((u - .65) / .35) : 1;
        push = .32 * k; pan = -sp[0] * k; lift = sp[1] * .5 * k;
      }
      else if (mv === 'reveal') { const k = 1 - U.easeInOut(U.clamp(u / .55)); push = .38 * k - .02; lift = -.03 * k; }
      else if (mv === 'whip') {
        const k = U.clamp((t - sec.t0) / (bt * 1.5));
        pan = dir * .18 * (1 - U.easeInOut(k)) + dir * .01 * Math.sin(ph * Math.PI / 4);
        roll = dir * 2.5 * (1 - U.easeInOut(k));
        push = .04;
      }
      else if (mv === 'stepzoom') {
        // 每 2 拍推近一格（快速 0.2 拍到位），8 格后回到起点
        const st = Math.floor(ph / 2) % 8, f = U.clamp(U.fract(ph / 2) * 10);
        push = .035 * (st + U.easeInOut(f)) - .06;
      }
      else if (mv === 'sweep') { pan = dir * (.09 - .18 * U.easeInOut(u)); push = .05; roll = dir * .6 * Math.sin(u * Math.PI); }
      else if (mv === 'float') { lift = Math.sin(t * .45) * .02; pan = Math.sin(t * .27 + 1) * .025; roll = Math.sin(t * .31) * .8; push = .03; }
      // 进副歌：推拉变焦（前 1 拍拉远 6%，接着 1 拍推近回来）
      if ((sec.id === 'chorus' || sec.id === 'final') && t - sec0.t0 < bt * 2) {
        const k = (t - sec0.t0) / bt;
        push += k < 1 ? -.06 * Math.sin(k * Math.PI / 2) : -.06 * Math.cos((k - 1) * Math.PI / 2);
      }
      // 段落交界 0.6 s 内把上一段的运镜平滑交给这一段（避免跳）
      const blend = U.clamp((t - sec.t0) / .6);
      const prev = S._camMove || { push: 0, pan: 0, roll: 0, lift: 0 };
      if (blend < 1 && S._camMoveSec !== si) { push = U.lerp(prev.push, push, blend); pan = U.lerp(prev.pan, pan, blend); roll = U.lerp(prev.roll, roll, blend); lift = U.lerp(prev.lift || 0, lift, blend); }
      else S._camMoveSec = si;
      const breathe = Math.sin(t * .9) * .004 + Math.sin(t * 1.7 + 1) * .002;
      {
        const D = S.downbeats || []; let last = -9;
        for (let k = D.length - 1; k >= 0; k--) if (D[k] <= t) { last = D[k]; break; }
        const d = t - last, hit = (sec.id === 'chorus' || sec.id === 'final') ? .03 : .015;
        if (d >= 0 && d < .45) push += hit * Math.exp(-d * 9) * (1 - Math.exp(-d * 60));
      }
      // 第四十六轮：上限前留余量——特写镜头上再推时按剩余空间缩放推近量，画面仍在动，不会卡在上限一动不动
      if (push > 0) { const room = Math.max(.04, 4.6 / sf - 1); push = room * Math.tanh(push / room); }
      sf *= 1 + push + breathe;
      headY -= (push + breathe) * .35 * headY - lift;
      cx += pan;
      rot += roll;
      if (S._camMoveSec === si || blend >= 1) S._camMove = { push, pan, roll, lift, mv };
    }
    const punch = evAmp(t, 'punch', 5);
    sf *= 1 + punch;
    headY -= punch * .5;
    const sh = (s.shake || 0) * intensityAt(t);
    const hx = (Math.sin(t * 1.7) * .6 + Math.sin(t * 3.1 + 1) * .4) * sh * S.W * .004;
    const hy = (Math.sin(t * 2.3 + 2) * .6 + Math.sin(t * 3.7) * .4) * sh * S.H * .004;
    return { sf, headY, cx, rot, hx, hy, kind: s.kind, cut: s.cut };
  }


  /* ------------------------------ 第二十四轮：动作库 / 演唱事件 / 风格幅度 ------------------------------ */
  // 情绪 -> 动作性格：amp 幅度、slow 摆动周期倍数（伤感的歌慢而小，元气的快而多）、w 动作库权重
  // 动作库顺序：sway 左右摇 / nod 点头 / lean 侧倾 / bounce 上下弹 / groove 8 字律动 / still 轻微静摆
  const EMO_MOTION = {
    joy:        { amp: 1.3,  slow: 1,  w: [3, 3, 2, 3, 3, 0] },
    playful:    { amp: 1.4,  slow: 1,  w: [2, 3, 3, 4, 4, 0] },
    proud:      { amp: 1.2,  slow: 1,  w: [3, 4, 2, 2, 2, 0] },
    determined: { amp: 1.15, slow: 1,  w: [2, 5, 1, 2, 1, 0] },
    tender:     { amp: .95,  slow: 2,  w: [4, 1, 3, 0, 1, 3] },
    longing:    { amp: .9,   slow: 2,  w: [3, 0, 3, 0, 0, 4] },
    sad:        { amp: .85,  slow: 2,  w: [2, 0, 3, 0, 0, 5] }
  };
  // 段落 -> 幅度（副歌、最后一次副歌最大，桥段和尾声收）
  const SEC_MOTION = { intro: .6, verse: .82, pre: .95, chorus: 1.15, final: 1.3, bridge: .65, outro: .55 };
  const MOVES = ['sway', 'nod', 'lean', 'bounce', 'groove', 'still'];
  function pickWeighted(w, r) {
    let tot = 0; for (const x of w) tot += x;
    let a = r * tot; for (let i = 0; i < w.length; i++) { a -= w[i]; if (a < 0) return i; }
    return w.length - 1;
  }
  // 当前动作段：有句子时按句切换（句前 .35 s 起），无句时每 2 小节换一次
  function segAt(t, L, beat) {
    // 按段落切换（主歌/副歌…），同一段内动作不变；段内相位锁在小节线上
    const sec = sectionAt(t), i = S.sections.indexOf(sec);
    return { id: 'S' + i, t0: sec.t0, seed: (i + 1) * 7.13 };
  }
  // 单个动作的单位输出（ax/ay/az 头，bx/by/bz 身），每段相位与幅度略有不同，避免「循环播放」感
  function moveOut(kind, bp, sw, seg) {
    const TAU = 6.2832, ph = (U.h01(S.seed * .001 + seg.seed) < .5 ? 0 : Math.PI), va = .9 + .2 * U.h01(seg.seed * 1.9 + 5);
    const beatB = .5 * Math.cos(TAU * bp);
    const o = { ax: 0, ay: 0, az: 0, bx: 0, by: 0, bz: 0 };
    if (kind === 'sway') { o.ax = Math.sin(TAU * bp / sw + ph); o.bx = .6 * Math.sin(TAU * bp / sw + ph - .5); o.az = .45 * Math.sin(TAU * bp / (2 * sw) + ph); o.ay = .25 * beatB; }
    else if (kind === 'nod') { o.ay = 1.2 * beatB; o.ax = .35 * Math.sin(TAU * bp / (2 * sw) + ph); o.by = .5 * beatB; }
    else if (kind === 'lean') { const l = Math.sin(TAU * bp / (2 * sw) + ph); o.bz = l; o.az = .8 * l; o.ax = .3 * l; o.ay = .3 * beatB; }
    else if (kind === 'bounce') { o.by = 1.4 * beatB; o.ay = .7 * beatB; o.bx = .35 * Math.sin(TAU * bp / sw + ph); o.az = .3 * Math.sin(TAU * bp / sw + ph + 1); }
    else if (kind === 'groove') { o.ax = Math.sin(TAU * bp / sw + ph); o.ay = .7 * Math.sin(2 * TAU * bp / sw + ph); o.bz = .5 * Math.sin(TAU * bp / sw + ph + .8); o.bx = .4 * Math.sin(TAU * bp / sw + ph - .4); }
    else { o.ax = .5 * Math.sin(TAU * bp / (2 * sw) + ph); o.az = .3 * Math.sin(TAU * bp / (3 * sw) + ph); o.bx = .25 * Math.sin(TAU * bp / (2 * sw) + ph - .6); }
    for (const k in o) o[k] *= va;
    return o;
  }
  // 当前段与上一段的动作交叉淡化（0.75 拍），换动作不跳
  // 一首歌只有两种动作：主动作（主歌/前奏/桥段/尾声）与副歌动作，按预设性格与 seed 定，整首保持一致
  function songMoves(sty) {
    if (S._songMoves && S._songMovesSeed === S.seed) return S._songMoves;
    const a = pickWeighted(sty.w, U.h01(S.seed * .001 + 11.3));
    const w2 = sty.w.slice(); w2[a] = 0;
    const b = pickWeighted(w2.some(x => x > 0) ? w2 : sty.w, U.h01(S.seed * .001 + 23.9));
    S._songMoves = [MOVES[a], MOVES[b]]; S._songMovesSeed = S.seed;
    return S._songMoves;
  }
  function moveMix(t, bp, sw, L, sty, beat) {
    const seg = segAt(t, L, beat);
    const sec = sectionAt(t);
    const pair = songMoves(EMO_MOTION[S.preset.emo.chorus] || sty);
    const kind = (sec.id === 'chorus' || sec.id === 'final') ? pair[1] : pair[0];
    const cur = moveOut(kind, bp, sw, seg);
    const w = U.sstep(seg.t0, seg.t0 + 2 * beat, t);
    if (w >= 1) return { o: cur, kind };
    const prevSeg = segAt(seg.t0 - .05, null, beat);
    const ps = sectionAt(seg.t0 - .05);
    const prev = moveOut((ps.id === 'chorus' || ps.id === 'final') ? pair[1] : pair[0], bp, sw, prevSeg);
    const o = {}; for (const k in cur) o[k] = U.lerp(prev[k], cur[k], w);
    return { o, kind };
  }
  // 非周期的慢漂移（三个不可通约的正弦），让静止段也不像定格
  function drift(t, k) { return Math.sin(t * .31 + k) * .25; }
  // 事件索引（按类型，已按时间排序）
  function evIndex() {
    if (S._evIdxSrc === S.events) return S._evIdx;
    const idx = {}; for (const e of S.events) (idx[e.type] = idx[e.type] || []).push(e);
    S._evIdx = idx; S._evIdxSrc = S.events; return idx;
  }
  function evAround(type, t) {
    const a = evIndex()[type]; if (!a || !a.length) return [null, null];
    let lo = 0, hi = a.length - 1, r = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (a[m].t <= t) { r = m; lo = m + 1; } else hi = m - 1; }
    return [r >= 0 ? a[r] : null, r + 1 < a.length ? a[r + 1] : null];
  }
  // 事件包络：attack 提前起势、hold=音长、release 回落
  function evSpaced(type, gap) {
    const key = '_sp_' + type;
    if (S[key] && S[key + 'src'] === S.events) return S[key];
    const a = (evIndex()[type] || []), out = []; let last = -1e9;
    for (const e of a) { if (e.t - last >= gap) { out.push(e); last = e.t; } }
    S[key] = out; S[key + 'src'] = S.events; return out;
  }
  // 闭眼长音：句尾、≥1.2 s、彼此间隔 ≥12 s（其余长音只微眯，不闭）
  function closeNoteK(t) {
    if (S._cnSrc !== S.events) {
      const ends = (S.lyrics && S.lyrics.prep ? S.lyrics.prep : []).filter(L => L.kind === 'line').map(L => L.exitAt);
      const out = []; let last = -1e9;
      for (const e of (evIndex().long_note || [])) {
        if ((e.dur || 0) < 1.2 || e.t - last < 12) continue;
        if (!ends.some(x => Math.abs(e.t + (e.dur || 0) - x) < .8)) continue;
        out.push(e); last = e.t;
      }
      S._cn = out; S._cnSrc = S.events;
    }
    let k = 0;
    for (const e of S._cn) { if (e.t - .45 > t) break; const v = U.sstep(e.t - .45, e.t + .3, t) * (1 - U.sstep(e.t + e.dur, e.t + e.dur + .9, t)); if (v > k) k = v; }
    return k;
  }
  function evEnv(type, t, att, rel, minHold) {
    let pn;
    if (type === 'high_note' || type === 'long_note') {
      const a = evSpaced(type, type === 'high_note' ? 1.5 : 2.5);
      let lo = 0, hi = a.length - 1, r = -1;
      while (lo <= hi) { const m = (lo + hi) >> 1; if (a[m].t <= t) { r = m; lo = m + 1; } else hi = m - 1; }
      pn = [r >= 0 ? a[r] : null, r + 1 < a.length ? a[r + 1] : null];
    } else pn = evAround(type, t);
    let k = 0, amp = 0;
    for (const e of pn) {
      if (!e) continue;
      const hold = Math.max(minHold || .05, e.dur || 0);
      const v = U.sstep(e.t - att, e.t, t) * (1 - U.sstep(e.t + hold, e.t + hold + rel, t));
      if (v > k) { k = v; amp = U.clamp(e.amp == null ? .7 : e.amp, 0, 1); }
    }
    return k * (.55 + .45 * amp);
  }
  // 空窗期小动作库（每 2 小节一个）：环顾 / 对镜头笑 / 闭眼轻摆 / 跟拍点头 / 歪头好奇 / 深呼吸
  const IDLE_ACTS = ['look', 'smileCam', 'closeSway', 'beatNod', 'tilt', 'breathe'];
  function idleAct(t, beat, soft) {
    const slot = Math.floor(t / (beat * 8));
    const w = soft ? [4, 3, 1, 1, 3, 3] : [3, 4, 1, 4, 3, 1];
    let kind = IDLE_ACTS[pickWeighted(w, U.h01(S.seed * .001 + slot * 3.1 + 7))];
    // 第三十轮（通用）：闭眼轻摆只给慢歌、整首最多 2 次（按出现顺序记）
    if (kind === 'closeSway') {
      S._csSlots = S._csSlots || new Set();
      if (!soft || (!S._csSlots.has(slot) && S._csSlots.size >= 2)) kind = 'breathe'; else S._csSlots.add(slot);
    }
    const ph = (t - slot * beat * 8) / (beat * 8);
    const k = U.sstep(0, .2, ph) * (1 - U.sstep(.8, 1, ph));
    const side = U.h01(slot * 1.37 + S.seed * .0007) < .5 ? -1 : 1;
    return { kind, k, ph, side };
  }


  /* ------------------------------ 第二十七轮：情绪单动作 + 模型表情层 ------------------------------ */
  // 模型表情层（Blue cat 自带 exp3 的参数，主程序表情系统用的同一批）；只写 0..峰值，舞台退出时归零
  // 第二十八轮：表情层不再写死参数号；fx 为「情绪类别 → 权重」，写入时按当前模型的 exp3 叠加
  const FX_BLOCK = /^(Param20[2-6]|Param88|ParamMouth)/;
  const FX_LIB = { model: null, cats: {}, loading: false, written: new Set() };
  async function loadFxLib(model) {
    if (!model || FX_LIB.model === model || FX_LIB.loading) return;
    FX_LIB.loading = true;
    const cats = {};
    try {
      const mgr = global.live2dManager;
      const map = mgr && mgr.emotionMapping && mgr.emotionMapping.expressions;
      const st = model.internalModel && model.internalModel.settings;
      const base = st && st.url ? new URL(st.url, location.href) : null;
      if (map && base) {
        for (const cat of ['happy', 'sad', 'surprised', 'angry', 'neutral']) {
          const files = Array.isArray(map[cat]) ? map[cat] : [];
          const list = [];
          for (const f of files) {
            try {
              const r = await fetch(new URL(f, base).href, { cache: 'force-cache' });
              if (!r.ok) continue;
              const j = await r.json();
              const ps = (j.Parameters || []).filter(x => x && x.Id && !FX_BLOCK.test(x.Id))
                .map(x => ({ id: x.Id, v: Number(x.Value) || 0, b: x.Blend || 'Add' }));
              if (ps.length) list.push({ file: f, ps });
            } catch (e) { }
          }
          if (list.length) cats[cat] = list;
        }
      }
    } catch (e) { recordErr('fxlib', e); }
    FX_LIB.cats = cats; FX_LIB.model = model; FX_LIB.loading = false;
    DIAG.fxCats = Object.keys(cats).map(k => k + ':' + cats[k].length).join(',');
  }
  // 写入时叠加表情层：w 为 0..1；Add 加 Value·w，Multiply 按 1→Value 插值，Overwrite 当前值→Value 插值
  function applyFx(c, fx, w, seed) {
    if (!fx || !(w > 0.001)) return;
    for (const cat in fx) {
      const list = FX_LIB.cats[cat]; if (!list || !list.length) continue;
      const ex = list[Math.floor(U.h01(seed * 1.37 + cat.length) * list.length) % list.length];
      const k = U.clamp(fx[cat] * w, 0, 1);
      for (const p of ex.ps) {
        try {
          if (c.getParameterIndex(p.id) < 0) continue;
          const cur = Number(c.getParameterValueById(p.id)) || 0;
          const v = p.b === 'Multiply' ? cur * (1 + (p.v - 1) * k) : p.b === 'Overwrite' ? cur + (p.v - cur) * k : cur + p.v * k;
          c.setParameterValueById(p.id, clampParam(c, p.id, v));
          FX_LIB.written.add(p.id);
        } catch (e) { }
      }
    }
  }
  // 情绪 -> 单动作：头 hx/hy/hz、身 bx/by/bz（度，峰值），眉 brow、眼睁 open、眼神 ex/ey、时长 dur（秒）、表情层 fx
  const EMO_ACTS = {
    joy:        [{ n: 'bright-pop', hy: 9, hz: 8, by: 4, bz: 3, brow: .5, open: .05, ex: 0, ey: .15, dur: 1.8, fx: { happy: 1 } },
                 { n: 'happy-sway', hx: 10, hz: 10, bx: 6, bz: 5, brow: .3, ex: .35, dur: 2.4, fx: { happy: .8 } }],
    playful:    [{ n: 'cute-tilt', hx: 6, hz: 16, hy: 4, bz: 6, bx: 4, brow: .45, ex: .4, ey: .1, dur: 2, fx: { surprised: 1 } },
                 { n: 'bounce-peek', hy: 8, hx: -8, by: 5, bx: -5, hz: -8, brow: .35, ex: -.45, dur: 1.8, fx: { happy: 1 } }],
    proud:      [{ n: 'chin-up', hy: 11, hx: 6, by: 3, bx: 4, brow: .4, ex: .25, ey: .15, dur: 2.2, fx: {} }],
    determined: [{ n: 'firm-nod', hy: -9, by: -3, bz: 3, hz: 4, brow: -.4, dur: 1.6, fx: {} },
                 { n: 'lean-in', hy: 5, by: 6, hz: 5, bz: 4, brow: -.3, ey: .05, dur: 2, fx: {} }],
    tender:     [{ n: 'warm-lean', hz: 11, hx: 5, bz: 7, bx: 3, hy: -3, brow: .2, ex: .2, ey: -.1, dur: 2.8, fx: { happy: .5 } }],
    longing:    [{ n: 'look-far', hx: 14, hy: 6, bx: 6, bz: -4, hz: -5, brow: .35, ex: .55, ey: .35, dur: 3, fx: {} },
                 { n: 'hug-sway', hz: 9, bz: 6, hy: -4, by: -2, brow: .45, ey: -.15, dur: 3.2, fx: { sad: .8 } }],
    sad:        [{ n: 'soft-sink', hy: -10, by: -4, hz: -6, bz: -4, brow: .7, ey: -.3, dur: 3, fx: { sad: 1 } }]
  };
  // 慢歌 / 伤感歌的另一套（真人唱抒情歌：动作慢、幅度靠身体不靠头、闭眼沉浸、仰望、低头收句、起句前吸气）
  // close: 闭眼深度（0 睁 … 1 闭），在动作中段慢慢闭上、末段慢慢睁开
  const SOFT_ACTS = {
    joy:        [{ n: 'soft-smile-sway', hz: 8, hx: 6, bz: 6, bx: 5, hy: 3, brow: .25, ex: .3, ey: .1, dur: 4, fx: { happy: .6 } },
                 { n: 'look-up-warm', hy: 8, hx: 6, by: 2, bz: 3, brow: .35, ex: .35, ey: .45, dur: 4, fx: { happy: .4 } }],
    playful:    [{ n: 'soft-smile-sway', hz: 9, hx: 6, bz: 6, bx: 5, hy: 3, brow: .3, ex: .35, ey: .1, dur: 3.6, fx: { happy: .6 } }],
    proud:      [{ n: 'look-up-far', hy: 10, hx: 8, by: 3, bx: 3, brow: .4, ex: .45, ey: .5, dur: 4.4, fx: {} }],
    determined: [{ n: 'breath-in', hy: 6, by: 5, brow: .25, ey: .2, dur: 3.5, fx: {} }],
    tender:     [{ n: 'close-feel', hz: 10, hx: 4, bz: 7, bx: 3, hy: -2, brow: .3, close: .9, dur: 4.6, fx: { happy: .35 } },
                 { n: 'soft-smile-sway', hz: 8, hx: 6, bz: 6, bx: 5, hy: 2, brow: .2, ex: .3, ey: -.05, dur: 4.2, fx: { happy: .4 } }],
    longing:    [{ n: 'look-up-far', hy: 9, hx: 10, by: 2, bx: 4, bz: -3, brow: .5, ex: .5, ey: .5, dur: 4.6, fx: {} },
                 { n: 'close-feel', hz: 9, bz: 6, hy: -3, by: -2, brow: .45, close: .95, dur: 5, fx: { sad: .6 } }],
    sad:        [{ n: 'sink-down', hy: -11, by: -4, hz: -7, bz: -5, brow: .8, ey: -.4, close: .4, dur: 4.6, fx: { sad: 1 } },
                 { n: 'close-feel', hz: -8, bz: -5, hy: -5, by: -2, brow: .7, close: .95, dur: 5, fx: { sad: .8 } }]
  };
  // 整首是否按「慢歌」处理：预设本身慢（motionK<.65），或句子里 tender/longing/sad 占六成以上
  function softSong() {
    if (S._softSongSrc === S.lyrics) return S._softSong;
    const prep = (S.lyrics && S.lyrics.prep ? S.lyrics.prep : []).filter(L => L.kind === 'line');
    const softN = prep.filter(L => L.emotion === 'tender' || L.emotion === 'longing' || L.emotion === 'sad').length;
    S._softSong = (S.preset && S.preset.motionK < .65) || (prep.length > 0 && softN / prep.length >= .6);
    S._softSongSrc = S.lyrics;
    return S._softSong;
  }
  // 每句是否起一个单动作：第一句、情绪变了、或每第 3 句；与上一个间隔 ≥5 s（先排好，播放时只查表）
  function emoActs() {
    if (S._emoActs && S._emoActsSrc === S.lyrics) return S._emoActs;
    const prep = (S.lyrics && S.lyrics.prep ? S.lyrics.prep : []).filter(L => L.kind === 'line').sort((a, b) => a.t0 - b.t0);
    const out = []; let lastT = -99, lastEnd = -99, lastEmo = null, k = 0;
    const soft = softSong(), TBL = soft ? SOFT_ACTS : EMO_ACTS, gap = soft ? 6 : 5;
    const chorusIn = (S.sections || []).filter(x => x.id === 'chorus' || x.id === 'final').map(x => x.t0);
    // 第三十轮（通用）：闭眼类动作整首累计 ≤6% 时长、相邻 ≥20 s，超出就换同情绪的不闭眼动作
    let closeSum = 0, lastClose = -99; const closeBudget = .06 * (S.dur || 0);
    const pickAct = (pool, seed, t0) => {
      const order = pool.map((_, i) => (i + Math.floor(U.h01(seed) * pool.length)) % pool.length);
      for (const i of order) { const a = pool[i]; if (!a.close || (closeSum + a.dur * .5 <= closeBudget && t0 - lastClose >= 20)) return a; }
      return null;
    };
    // 候选：歌词句 + 歌词稀疏处的段落起点 / 每 4 小节（用该段的情绪）
    const cands = prep.map(L => ({ t0: L.t0, emo: L.emotion || 'tender', i: L.i, inten: L.intensity }));
    const bar4 = 16 * (S.beat || .5);
    for (const sec of (S.sections || [])) {
      for (let t = sec.t0 + .5; t < sec.t1 - 2; t += bar4) {
        if (prep.some(L => Math.abs(L.t0 - t) < bar4 * .75)) continue;
        cands.push({ t0: t, emo: (S.preset && S.preset.emo && S.preset.emo[sec.id]) || 'tender', i: 1000 + Math.round(t * 10), inten: .5, sec: true });
      }
    }
    cands.sort((a, b) => a.t0 - b.t0);
    for (const L of cands) {
      const emo = L.emo, pool = TBL[emo] || TBL.tender;
      const want = L.sec || lastEmo === null || emo !== lastEmo || (k % (soft ? 2 : 3) === 0);
      const a = pickAct(pool, S.seed * .001 + L.i * 2.17, L.t0) || (TBL.tender || pool).find(x => !x.close) || pool[0];
      const st0 = L.t0 - (soft ? .6 : .25);
      // 上一个收完 + 0.8 s 才起；不压副歌入口（入口前 1 拍到入口后 2 拍留给副歌入口动作）
      const clash = chorusIn.some(c => st0 < c + 2 * S.beat && st0 + a.dur > c - S.beat);
      if (want && L.t0 - lastT >= gap && st0 >= lastEnd + .8 && !clash) {
        const side = U.h01(L.i * 4.3 + 1) < .5 ? -1 : 1;
        const amp = .7 + .45 * U.clamp(L.inten || .5, 0, 1);
        out.push({ t0: st0, act: a, side, amp, emo, seed: L.i });
        lastT = L.t0; lastEnd = st0 + a.dur;
        if (a.close) { closeSum += a.dur * .5; lastClose = L.t0; }
      }
      lastEmo = emo; k++;
    }
    S._emoActs = out; S._emoActsSrc = S.lyrics;
    return out;
  }
  // 单动作包络：预备（反方向 15%）→ 到位 → 保持 → 回收，全程 smoothstep，速度连续
  function emoActAt(t) {
    const acts = emoActs(); let cur = null;
    for (const a of acts) { if (a.t0 <= t && t < a.t0 + a.act.dur) { cur = a; break; } if (a.t0 > t) break; }
    if (!cur) return null;
    const u = (t - cur.t0) / cur.act.dur;
    let e;
    if (u < .12) e = -.15 * U.sstep(0, 1, u / .12);
    else if (u < .36) e = U.lerp(-.15, 1, U.sstep(0, 1, (u - .12) / .24));
    else if (u < .52) e = 1 - .12 * U.sstep(0, 1, (u - .36) / .16);
    else e = .88 * (1 - U.sstep(0, 1, (u - .52) / .4));
    // 表情层比动作慢一点进、多留一会儿再退
    const fe = U.sstep(.1, .35, u) * (1 - U.sstep(.7, .92, u));
    // 占用度：单动作在场的程度（预备开始 → 收完），其它点缀动作按 1-occ 让位
    const occ = U.sstep(0, .12, u) * (1 - U.sstep(.8, .95, u));
    // 闭眼深度：动作 25%~45% 慢慢闭上，75%~100% 慢慢睁开
    const ce = (cur.act.close || 0) * U.sstep(.25, .45, u) * (1 - U.sstep(.75, 1, u));
    return { a: cur.act, e: e * cur.amp, fe, side: cur.side, emo: cur.emo, close: ce, seed: cur.seed, occ };
  }

  /* ------------------------------ 人物姿态：目标 → 烘焙 ------------------------------ */
  // 纯目标函数（不含平滑），对应演示页 poseAt 的上半段
  function poseTarget(t) {
    const preset = S.preset, beat = S.beat;
    const I = intensityAt(t), sec = sectionAt(t);
    const L = S.lyrics.lineAt(t);
    let env = .8;
    if (L) {
      const u = U.clamp((t - L.t0) / (L.exitAt - L.t0));
      env = .75 + .25 * U.sstep(0, .3, u) - .15 * U.sstep(.85, 1, u);
    }
    // 第二十四轮：幅度 = 预设 × 强度 × 句内包络 × 情绪性格 × 段落
    const emo0 = L ? L.emotion : (preset.emo[sec.id] || 'tender');
    const sty = EMO_MOTION[emo0] || EMO_MOTION.tender;
    const soft = sty.slow > 1.5 || preset.motionK < .65;
    const A = Math.max(soft ? .55 : .35, preset.motionK * (.35 + .85 * (L ? Math.max(I, L.intensity) : I)) * env * sty.amp * (SEC_MOTION[sec.id] || 1));
    const sw = Math.max(preset.swayBeats * sty.slow, soft ? 4 : 2), TAU = 6.2832;
    const bp = t / beat;
    // 动作库：每句（或空窗每 2 小节）按情绪权重挑一种，段间 0.75 拍交叉淡化；同一种动作每段相位、幅度都不同
    const mv = moveMix(t, bp, sw, L, sty, beat).o;
    const tg = {
      ParamAngleX: A * (13 * mv.ax + 2.2 * drift(t, 1)),
      ParamAngleY: A * (preset.bounceK * 7 * mv.ay + 1.5 * drift(t, 2)),
      ParamAngleZ: A * (7 * mv.az + 1.8 * drift(t, 3)),
      ParamBodyAngleX: A * (6 * mv.bx + 1.2 * drift(t, 4)),
      ParamBodyAngleY: A * (preset.bounceK * 4 * mv.by + .8 * drift(t, 5)),
      ParamBodyAngleZ: A * (4 * mv.bz + 1 * drift(t, 6)),
      ParamBreath: .5 + .5 * Math.sin(t * (soft ? 1.1 : 1.6))
    };
    // 第二十五轮 b：身体带头——身体先摇、头跟着（身体超前 0.12 拍），慢歌身体占比更大，避免「只有头在点」
    const leadBp = bp + .12, mvL = moveMix(t + .12 * beat, leadBp, sw, L, sty, beat).o, bk = soft ? .5 : .32;
    tg.ParamBodyAngleX += A * 13 * bk * mvL.ax;
    tg.ParamBodyAngleZ += A * 7 * bk * mvL.az;
    // 第二十六轮：舞步分量单独记一份（写入时按桌面律动是否在跑来让位）；不写进模型
    tg.DanceParamAngleX = A * (13 * mv.ax + 2.2 * drift(t, 1));
    tg.DanceParamAngleY = A * (preset.bounceK * 7 * mv.ay + 1.5 * drift(t, 2));
    tg.DanceParamAngleZ = A * (7 * mv.az + 1.8 * drift(t, 3));
    tg.DanceParamBodyAngleX = A * (6 * mv.bx + 1.2 * drift(t, 4)) + A * 13 * bk * mvL.ax;
    tg.DanceParamBodyAngleY = A * (preset.bounceK * 4 * mv.by + .8 * drift(t, 5));
    tg.DanceParamBodyAngleZ = A * (4 * mv.bz + 1 * drift(t, 6)) + A * 7 * bk * mvL.az;
    // 演唱事件（show.json events）：高音 = 抬下巴、扬眉、身体后仰；长音 = 慢慢仰头 + 颤音微摆；用力 = 身体前送
    // 第二十九轮：仲裁——单动作在场时，其它点缀动作让位（同一时刻只一个「主动作」）
    const ea = emoActAt(t);
    const yk = ea ? 1 - ea.occ : 1;
    const hiK = evEnv('high_note', t, .12, .35, .2) * yk;
    const lnK = Math.max(evEnv('long_note', t, .25, .5, .3), L && L.chars.some(c => c.long && t >= c.cs && t < c.ce) ? .8 : 0) * yk;
    const effortK = U.clamp(A / (preset.motionK * 1.2), 0, 1);
    tg.ParamAngleY += 6 * hiK + 5 * lnK * (.6 + .4 * effortK);
    tg.ParamBodyAngleY += 2.5 * hiK + 2 * lnK * effortK;
    const hiSide = L && (L.i % 2) ? 1 : -1;
    tg.ParamBodyAngleX += 2.5 * hiK * hiSide;
    tg.ParamAngleZ += lnK * .6 * Math.sin(t * TAU * 5.5) + 3 * hiK * hiSide;
    // 第二十七轮：情绪单动作（全身 + 头），叠在律动/舞步之上，不参与「舞步让位」
    let eaBrow = 0, eaOpen = 0, eaEx = 0, eaEy = 0;
    if (ea) {
      const a = ea.a, e = ea.e, sd = ea.side;
      tg.ParamAngleX += (a.hx || 0) * e * sd; tg.ParamAngleY += (a.hy || 0) * e; tg.ParamAngleZ += (a.hz || 0) * e * sd;
      tg.ParamBodyAngleX += (a.bx || 0) * e * sd; tg.ParamBodyAngleY += (a.by || 0) * e; tg.ParamBodyAngleZ += (a.bz || 0) * e * sd;
      eaBrow = (a.brow || 0) * e; eaOpen = (a.open || 0) * e; eaEx = (a.ex || 0) * e * sd; eaEy = (a.ey || 0) * e;
    }
    const emoKey = L ? L.emotion : (preset.emo[sec.id] || 'tender');
    const em = EXPR_MAP[emoKey] || EXPR_MAP.tender;
    const emTier = exprTier(L ? L.intensity : I);
    const emK = emTier.mult;
    let open = U.lerp(1, em.open, emK) + eaOpen, browY = em.browY * emK + eaBrow, eyeX = 0, eyeY = 0;
    const gz = L ? L.gaze : (sec.id === 'intro' || sec.id === 'outro' ? 'down' : 'far');
    const gs = (L ? L.i : 0) % 2 ? 1 : -1;
    // 视线：平滑状态（眼球先动、头后跟），不再硬编码跳变；闭眼/垂眸的眼睑语义保留
    const gsm = gazeStep(t, gz, gs);
    const fp = focusAt(t, softSong());
    gsm.ex += fp[0]; gsm.ey += fp[1]; gsm.hx += fp[0] * .5; gsm.hy += fp[1] * .4;
    eyeX = gsm.ex; eyeY = gsm.ey;
    eyeX += eaEx; eyeY += eaEy;
    tg.ParamEyeBallX = gsm.ex + eaEx;
    tg.ParamEyeBallY = gsm.ey + eaEy;
    tg.ParamAngleX += gsm.hx * 9;
    tg.ParamAngleY += gsm.hy * 6;
    if (gz === 'down') { open = Math.min(open, .8); }
    else if (gz === 'closed') { open = .05; }
    const openFloor = gz === 'closed' ? 0 : .92;
    // 高音：扬眉、眼睛略眯；长音：柔的歌慢慢闭眼、结束后慢慢睁开（氛围感），其他歌半眯
    browY += .55 * hiK;
    open = U.lerp(open, .9, hiK * .5);
    const lnClose = closeNoteK(t);
    if (soft || emo0 === 'tender' || emo0 === 'sad' || emo0 === 'longing') open = U.lerp(open, .1, lnClose * .9);
    else open = U.lerp(open, .85, lnClose * .5);
    // 不刻意闭眼时睁眼不低于 .85（眼睑盖住虹膜就是「眼珠变小」）；慢歌长音闭眼、动作闭眼另算
    if (lnClose < .05) open = Math.max(open, gz === 'down' ? .8 : openFloor);
    if (ea && ea.close > 0) open = U.lerp(open, .04, ea.close);
    // 桥段开头：闭眼 1.5 拍后慢慢睁开（留给场景/字幕联动的钩子）
    if (sec.id === 'bridge') { const db = t - sec.t0; open = U.lerp(open, .05, (1 - U.sstep(beat * 1.5, beat * 4, db)) * U.sstep(-.3, .2, db)); }

    let mo = 0, mf = 0;
    if (L) {
      if (t < L.t0 && t > L.t0 - .35) {
        const k = (1 - (L.t0 - t) / .35) * yk;
        tg.ParamAngleY += 3 * k; tg.ParamBodyAngleY += 2 * k;
        open = Math.min(1, open + .1 * k);
      }
      const last = L.chars[L.chars.length - 1];
      if (last && last.long && t >= last.cs && t < last.ce) {
        const u = (t - last.cs) / (last.ce - last.cs);
        const k = U.sstep(0, .35, u) * (1 - U.sstep(.92, 1, u)) * yk;
        tg.ParamAngleY += 9 * k * (S.preset === PRESETS.edm ? .6 : 1);
        tg.ParamAngleZ += 5 * gs * k + .9 * Math.sin(t * TAU * 5.6) * U.sstep(.3, .5, u);
        open = U.lerp(open, .85, k);
        browY += .5 * k; eyeY = U.lerp(eyeY, .3, k);
      }
      if (t > L.exitAt && t < L.exitAt + .6) {
        const k = Math.sin((t - L.exitAt) / .6 * Math.PI) * yk;
        tg.ParamAngleY -= 4 * k; eyeX = U.lerp(eyeX, -.3 * gs, k);
      }
      for (const ch of L.chars) {
        if (ch.kw) { if (t >= ch.cs && t < ch.cs + .45) tg.ParamAngleY -= 4 * yk * Math.sin((t - ch.cs) / .45 * Math.PI); break; }
      }
      for (const ch of L.chars) {
        if (t >= ch.cs && t < ch.ce) {
          const u = (t - ch.cs) / Math.max(.05, ch.ce - ch.cs);
          mo = (.45 + .45 * U.h01(ch.c.codePointAt(0))) * (ch.long ? .85 + .15 * Math.sin(t * 35) : 1) * U.sstep(0, 1, (t - ch.cs) / .06);
          mf = (U.h01(ch.c.codePointAt(0) * 1.7) - .3);
          // 第二十六轮：重音字像真人唱歌一样用力——眉毛上扬、眼睛微眯、下巴轻扬
          const sk = U.clamp(ch.stress || 0, 0, 1) * Math.sin(Math.PI * U.clamp(u, 0, 1));
          browY += .4 * sk; tg.ParamAngleY += 3 * sk;
          break;
        }
      }
    }
    // 副歌入口：前一拍蓄力，进副歌直视镜头、身体大摆
    for (const id of ['chorus', 'final']) {
      const sec0 = S.sections.find(s => s.id === id);
      if (!sec0) continue;
      const d = t - sec0.t0;
      if (d > -beat && d < 0) {
        const k = 1 + d / beat;
        tg.ParamAngleY -= 6 * k; tg.ParamBodyAngleY -= 3 * k;
        open = U.lerp(open, .7, k);
      }
      if (d >= 0 && d < beat * 2) {
        const k = Math.exp(-d * 2);
        tg.ParamAngleY += 10 * k; tg.ParamBodyAngleX += 8 * k * gs;
        open = 1; eyeX *= 1 - k; eyeY *= 1 - k; browY += .4 * k;
      }
    }
    if (sec.id === 'outro') {
      const u = U.clamp((t - (S.dur - 8)) / 8);
      open = U.lerp(open, .1, U.sstep(.3, .9, u));
      tg.ParamAngleY -= 8 * U.sstep(.2, 1, u);
    }
    // 空窗期（无句）候选小动作：点头 / 看向别处 / 调整姿态，按段号与 seed 选一个，缓入缓出
    // （笑眼不在空窗里做：笑眼统一由下方 EYE_SMILE_CAP 限幅，避免被覆盖）
    let idleSmile = 0, idleCheek = 0, idleBrow = 0;
    if (!L && sec.id !== 'outro') {
      const ia = idleAct(t, beat, soft), k = ia.k * yk, sd = ia.side;
      if (ia.kind === 'look') {
        // 眼睛先动、头后跟：先看一侧，后半段回看镜头并笑一下
        const back = U.sstep(.55, .75, ia.ph);
        const ex = U.lerp(.45 * sd, 0, back), ey = U.lerp(.15, 0, back);
        eyeX = U.lerp(eyeX, ex, k); eyeY = U.lerp(eyeY, ey, k);
        tg.ParamEyeBallX = eyeX; tg.ParamEyeBallY = eyeY;
        tg.ParamAngleX += 10 * sd * k * (1 - back); tg.ParamAngleY += 3 * k * (1 - back);
        idleSmile += .6 * k * back; idleCheek += .15 * k * back;
      } else if (ia.kind === 'smileCam') {
        eyeX = U.lerp(eyeX, 0, k); eyeY = U.lerp(eyeY, 0, k); tg.ParamEyeBallX = eyeX; tg.ParamEyeBallY = eyeY;
        tg.ParamAngleZ += 5 * sd * k; tg.ParamAngleY += 3 * k * Math.sin(TAU * bp / 2);
        idleSmile += k; idleCheek += .3 * k; idleBrow += .25 * k;
      } else if (ia.kind === 'closeSway') {
        open = U.lerp(open, .3, k * U.sstep(.1, .3, ia.ph) * (1 - U.sstep(.7, .9, ia.ph)));
        tg.ParamAngleX += 6 * k * Math.sin(TAU * bp / 8); tg.ParamBodyAngleZ += 3 * k * Math.sin(TAU * bp / 8);
        idleCheek += .1 * k;
      } else if (ia.kind === 'beatNod') {
        const nb = (-Math.abs(Math.sin(Math.PI * bp)) + .5) * 2;
        tg.ParamAngleY += 7 * k * nb; tg.ParamBodyAngleY += 3 * k * nb;
        idleSmile += .4 * k;
      } else if (ia.kind === 'tilt') {
        tg.ParamAngleZ += 9 * sd * k; tg.ParamAngleX += 4 * sd * k; idleBrow += .4 * k;
        eyeX = U.lerp(eyeX, .2 * sd, k); tg.ParamEyeBallX = eyeX;
      } else {
        const b = Math.sin(TAU * U.clamp(ia.ph * 1.25, 0, 1));
        tg.ParamBreath = U.lerp(tg.ParamBreath, .5 + .5 * b, k); tg.ParamBodyAngleY += 3 * k * b; tg.ParamAngleY += 2 * k * b;
      }
    }
    // 笑眼：按计划 v3 §7-11 限幅到 EYE_SMILE_CAP（不是不写，也不是无限幅）。
    tg.ParamEyeLSmile = tg.ParamEyeRSmile = Math.min((EMO[emoKey] || EMO.tender).smile * emK + EYE_SMILE_CAP * idleSmile, EYE_SMILE_CAP);
    tg.ParamBrowLY = tg.ParamBrowRY = browY + idleBrow;
    tg.ParamBrowLForm = tg.ParamBrowRForm = em.browF * emK;
    tg.ParamBrowLAngle = tg.ParamBrowRAngle = em.browA * emK;
    tg.ParamCheek = U.clamp(em.cheek * emK + idleCheek, 0, 1);
    // 第三十轮（通用）：多层相加后可能顶到模型上限；软限幅——接近上限时平滑收住，不出现平顶
    { const sl = (v, m) => { const k = m * .85; if (Math.abs(v) <= k) return v; const e = Math.abs(v) - k, r = m - k; return Math.sign(v) * (k + r * Math.tanh(e / r)); };
      for (const id of ['ParamAngleX', 'ParamAngleY', 'ParamAngleZ']) tg[id] = sl(tg[id], 28);
      for (const id of ['ParamBodyAngleX', 'ParamBodyAngleY', 'ParamBodyAngleZ']) tg[id] = sl(tg[id], 9.3); }
    return { tg, open, eyeX, eyeY, mouth: mo, form: mf + em.mouth * .5, A, gz, effort: U.clamp(A / (preset.motionK * 1.2) + (L && L.chars.some(c => c.long && t >= c.cs && t < c.ce) ? .25 : 0) + .2 * hiK, 0, 1), emo: L ? L.emotion : (preset.emo[sec.id] || 'tender') };
  }

  // 把平滑滤波一次性烘焙进采样表：之后任意 t 都只是插值，不再带历史状态
  function bakePose() {
    const dt = 1 / BAKE_FPS;
    const N = Math.ceil(S.dur * BAKE_FPS) + 2;
    const names = Object.keys(poseTarget(0).tg);
    const arr = {};
    for (const k of names) arr[k] = new Float32Array(N);
    arr._open = new Float32Array(N); arr._ex = new Float32Array(N); arr._ey = new Float32Array(N);
    arr._mouth = new Float32Array(N); arr._form = new Float32Array(N); arr._effort = new Float32Array(N);
    const P = {};
    let open, ex, ey, mouth, form, effort;
    for (let n = 0; n < N; n++) {
      const t = n * dt, T = poseTarget(t);
      for (const k of names) {
        const tau = /EyeBall/.test(k) ? .1 : /Eye|Brow|Cheek/.test(k) ? .55 : .09;
        P[k] = P[k] === undefined ? T.tg[k] : P[k] + (T.tg[k] - P[k]) * (1 - Math.exp(-dt / tau));
        if (/Angle|Breath/.test(k)) {
          const k2 = k + '#2';
          P[k2] = P[k2] === undefined ? P[k] : P[k2] + (P[k] - P[k2]) * (1 - Math.exp(-dt / tau));
          arr[k][n] = P[k2];
        } else arr[k][n] = P[k];
      }
      open = open === undefined ? T.open : open + (T.open - open) * (1 - Math.exp(-dt / .12));
      ex = ex === undefined ? T.eyeX : ex + (T.eyeX - ex) * (1 - Math.exp(-dt / .18));
      ey = ey === undefined ? T.eyeY : ey + (T.eyeY - ey) * (1 - Math.exp(-dt / .18));
      mouth = mouth === undefined ? T.mouth : mouth + (T.mouth - mouth) * (1 - Math.exp(-dt / (T.mouth > mouth ? .045 : .12)));
      form = form === undefined ? T.form : form + (T.form - form) * (1 - Math.exp(-dt / .09));
      effort = effort === undefined ? T.effort : effort + (T.effort - effort) * (1 - Math.exp(-dt / .2));
      arr._open[n] = open; arr._ex[n] = ex; arr._ey[n] = ey; arr._mouth[n] = mouth; arr._form[n] = form; arr._effort[n] = effort;
    }
    S.poseTrack = { arr, names, N, fps: BAKE_FPS };
  }

  // 眨眼时长按「事件自身」决定：事件时刻落在某句 exitAt 的 -.2..+.6 内用长窗口（句尾眨眼），否则短窗口。
  // 不依赖采样时刻的 lineAt 归属（那会在句间切换时漏掉句尾眨眼）。
  function blinkWindowFor(evT) {
    const prep = S.lyrics && S.lyrics.prep ? S.lyrics.prep : null;
    if (prep) {
      for (const L of prep) if (L.kind === 'line' && evT >= L.exitAt - .2 && evT <= L.exitAt + .6) return .26;
    }
    return .17;
  }

  // 扫视（Eyes Alive 思路）：每 0.6~2.5 s 一次小跳，幅度 ±.12/±.07，跳变 50 ms，期间保持不动；头 0.2 s 后跟一点
  function saccadeAt(t) {
    if (t < 0) return [0, 0];
    const cell = .35, c = Math.floor(t / cell);
    const hit = i => U.h01(S.seed * .0013 + i * 1.618) < .36;
    const tgt = i => [(U.h01(i * 3.1 + 1) - .5) * .36, (U.h01(i * 5.7 + 2) - .5) * .2];
    for (let j = c; j >= c - 8 && j >= 0; j--) {
      if (!hit(j)) continue;
      const tj = j * cell + U.h01(j * 2.3) * cell * .5;
      if (tj > t) continue;
      let pv = [0, 0];
      for (let i = j - 1; i >= j - 9 && i >= 0; i--) if (hit(i)) { pv = tgt(i); break; }
      const tv = tgt(j), a = U.clamp((t - tj) / .05, 0, 1);
      return [U.lerp(pv[0], tv[0], a), U.lerp(pv[1], tv[1], a)];
    }
    return [0, 0];
  }

  // 注视点：在若干「看点」之间换（镜头、左前、右前、上方远处、下方），每段 1.5~3 s（慢歌 2.5~4.5 s），
  // 换点用 0.25 s 平滑转过去（眼睛快，头由 gazeStep 慢跟），段内保持不动——人眼是「看一处、停、再换」
  function focusAt(t, soft) {
    if (t < 0) return [0, 0];
    const L0 = soft ? 2.5 : 1.5, L1 = soft ? 4.5 : 3;
    const seg = (i) => L0 + (L1 - L0) * U.h01(S.seed * .0007 + i * 2.31);
    const PTS = soft ? [[0, 0], [.35, .25], [-.35, .25], [0, .4], [.25, -.2], [-.25, -.2]]
                     : [[0, 0], [.45, .1], [-.45, .1], [.3, .35], [-.3, .35], [0, -.25], [0, 0]];
    let i = 0, t0 = 0;
    // 段长有界（≥1.5 s），从 0 走到 t：曲长几百秒也只几百步；缓存上一次位置加速
    if (S._fcT0 != null && S._fcT0 <= t && S._fcSeed === S.seed) { i = S._fcI; t0 = S._fcT0; }
    while (t0 + seg(i) <= t) { t0 += seg(i); i++; }
    S._fcI = i; S._fcT0 = t0; S._fcSeed = S.seed;
    const pick = (j) => PTS[Math.floor(U.h01(S.seed * .0011 + j * 3.77) * PTS.length) % PTS.length];
    const a = pick(i - 1 < 0 ? 0 : i - 1), b = pick(i), u = U.sstep(0, .25, t - t0);
    return [U.lerp(a[0], b[0], u), U.lerp(a[1], b[1], u)];
  }

  function poseAt(t) {
    const T = S.poseTrack;
    if (!T) return null;
    const f = U.clamp(t, 0, (T.N - 1) / T.fps) * T.fps;
    const i0 = Math.min(T.N - 1, Math.floor(f)), i1 = Math.min(T.N - 1, i0 + 1), a = f - i0;
    const val = k => T.arr[k][i0] + (T.arr[k][i1] - T.arr[k][i0]) * a;
    const out = {};
    for (const k of T.names) out[k] = val(k);
    if (S.dirPlan) { for (const k of T.names) if (/Angle|Body|Sway|Lean|Step|Shoulder/i.test(k)) out[k] *= .3; }   // 第五十一轮：导演模式下人物是「画」，身体只留三成幅度
    const open = val('_open'), ex = val('_ex'), ey = val('_ey');
    S.poseEffort = val('_effort');
    // 微扫视：跳变、不平滑（只由 t 决定）
    // 扫视：平滑的小幅包络（±.12），用时间相位而非分段随机跳
    const sac = saccadeAt(t), sacH = saccadeAt(t - .2);
    const sx = sac[0], sy = sac[1];
    void sacH;
    // 眨眼：放在 show.json 的 blink 事件上；句尾/换气点的眨眼时长略长（不是固定 .17 s）
    let blink = 0, blinkHit = null, blinkDur = .17;
    for (const e of S.events) {
      if (e.t > t) break;
      if (e.type !== 'blink') continue;
      const d = t - e.t;
      const dur = blinkWindowFor(e.t);
      if (d >= 0 && d < dur) { blink = Math.sin(d / dur * Math.PI); blinkHit = e; blinkDur = dur; break; }
    }
    // 睁眼度随用力联动：用力大时眼睛略收（专注），弱段保持开
    const effort = U.clamp(S.poseEffort || 0, 0, 1);
    const openEff = open * (1 - .02 * effort);
    out.ParamEyeLOpen = openEff * (1 - blink);
    out.ParamEyeROpen = openEff * (1 - blink * .97);
    out.ParamEyeBallX = U.clamp(val('ParamEyeBallX') + sx, -.65, .65);
    DIAG.eyeBaked = val('ParamEyeBallX'); DIAG.eyeRunGaze = gazeSt.ex; DIAG.eyeT = t;
    DIAG.poseEffort = S.poseEffort; DIAG.blinkDur = blinkDur; DIAG.blinkNow = blink;
    // 运行时计数（读数用）：按事件计数（以事件时刻为键），不按包络是否为 0 计数
    if (blinkHit && !(S._blinkSeenSet && S._blinkSeenSet.has(blinkHit.t))) {
      if (!S._blinkSeenSet) S._blinkSeenSet = new Set();
      S._blinkSeenSet.add(blinkHit.t);
      DIAG.blinkTotal = (DIAG.blinkTotal || 0) + 1;
      if (blinkDur > .2) DIAG.blinkEnd = (DIAG.blinkEnd || 0) + 1;
      // 事件级记录：每次眨眼记下事件时刻与当时的窗口长度（用于核对，不影响行为）
      DIAG.blinkLog = (DIAG.blinkLog || []).concat([[+blinkHit.t.toFixed(2), +t.toFixed(2), blinkDur, blinkHit.type, blinkHit.arg || '']]).slice(-2000);
    }
    out.ParamEyeBallY = U.clamp(val('ParamEyeBallY') + sy, -.65, .65);
    out.ParamMouthOpenY = val('_mouth');
    out.ParamMouthForm = U.clamp(val('_form'), -1, 1);
    return out;
  }

  /* ------------------------------ 模型绑定 ------------------------------ */
  // 宿主当前渲染的模型（与 loader 的 getModel 同一来源：live2dManager 优先，其次 LanLan1）
  function currentHostModel() {
    try {
      const mgr = global.live2dManager;
      if (mgr && typeof mgr.getCurrentModel === 'function') {
        const m = mgr.getCurrentModel();
        if (m) return m;
      }
      if (global.LanLan1) return global.LanLan1.live2dModel || global.LanLan1.currentModel || null;
    } catch (e) { }
    return null;
  }
  // 模型被替换时：先从旧实例卸载（unbindModel），再挂到当前实例；容器沿用进舞台时的容器
  function rebindModel(model) {
    const container = S.container;
    try { setModel(model, container); } catch (e) { recordErr('rebind', e); }
    DIAG.rebinds = (DIAG.rebinds || 0) + 1;
  }
  function setModel(model, container) {
    (function () {
      // P6 第五轮：同一性记录（只记录）
      try {
        const cur = currentHostModel();
        const curIm = cur && cur.internalModel;
        const lst = curIm && curIm._events && curIm._events.beforeModelUpdate;
        traceLog('setModel-enter', { inst: traceId(model && model.internalModel), hasModel: !!model,
          eqCurrentModel: model === cur, eqCurrentIm: !!model && model.internalModel === curIm,
          curIm: traceId(curIm), curBeforeCount: lst ? lst.length : 0,
          loaderStateModelIsArg: !!(global.__NEKO_SOLO_STAGE_LOADER__ && global.__NEKO_SOLO_STAGE_LOADER__.state) && global.__NEKO_SOLO_STAGE_LOADER__.state.model === model });
      } catch (e) { traceLog('setModel-enter-idErr', { err: String(e && e.message) }); }
    })();
    if (!model) return;
    unbindModel();
    S.model = model; S.container = container || null;
    const im = model.internalModel, core = im.coreModel;
    const saved = S.modelSaved = {
      anchor: { x: model.anchor.x, y: model.anchor.y },
      scale: { x: model.scale.x, y: model.scale.y },
      position: { x: model.position.x, y: model.position.y },
      rotation: model.rotation,
      coreUpdateRaw: core.update,
      coreUpdate: core.update.bind(core),
      eyeBlink: im.eyeBlink ? im.eyeBlink.updateParameters : null,
      natural: im.updateNaturalMovements,
      physicsEval: im.physics ? im.physics.evaluate : null,
      motionStartRandom: im.motionManager ? im.motionManager.startRandomMotion : null,
      pivot: container ? { x: container.pivot.x, y: container.pivot.y } : null,
      containerPos: container ? { x: container.position.x, y: container.position.y } : null,
      containerRot: container ? container.rotation : 0
    };
    try { if (im.eyeBlink) im.eyeBlink.updateParameters = function () { }; } catch (e) { }
    try { im.updateNaturalMovements = function () { }; } catch (e) { }
    try { if (im.physics) im.physics.evaluate = function () { }; } catch (e) { }
    try {
      if (im.motionManager) {
        im.motionManager.stopAllMotions();
        if (im.motionManager.groups) im.motionManager.groups.idle = '';
        im.motionManager.startRandomMotion = () => Promise.resolve(false);
      }
    } catch (e) { }
    core.update = function () {
      // P6：写入已挪到 internalModel 的 beforeModelUpdate 事件（主程序表情/眨眼/口型算完之后、
      // 主 update 之前）。这里只保留只读计数，证明包装是否还在调用链上。
      DIAG.wrapCalls += 1;
      // P6 第九轮：渲染用的值（第 5 步 coreModel.update 之前记录；只记录，不改行为）
      try {
        DIAG.renderVals = {
          ParamBrowLY: Number(core.getParameterValueById('ParamBrowLY')),
          ParamCheek: Number(core.getParameterValueById('ParamCheek')),
          ParamEyeLSmile: Number(core.getParameterValueById('ParamEyeLSmile')),
          ParamEyeRSmile: Number(core.getParameterValueById('ParamEyeRSmile')),
          ParamMouthForm: Number(core.getParameterValueById('ParamMouthForm'))
        };
        DIAG.renderT = S.lastInfo ? S.lastInfo.t : null;
        DIAG.renderPose = S.poseOut ? { ParamBrowLY: S.poseOut.ParamBrowLY, ParamCheek: S.poseOut.ParamCheek, ParamEyeLSmile: S.poseOut.ParamEyeLSmile } : null;
        // 同一帧配对：写入值 vs 渲染入口值（差 = 写入到渲染之间的偏差，应接近 0）
        if (DIAG.pairWroteBrow != null) DIAG.pairDiffBrow = DIAG.renderVals.ParamBrowLY - DIAG.pairWroteBrow;
        if (DIAG.pairWroteCheek != null) DIAG.pairDiffCheek = DIAG.renderVals.ParamCheek - DIAG.pairWroteCheek;
      } catch (e) { }
      // 定位（只读）：coreModel.update 入口与出口各读一次眉/颊/眼笑
      const pre = locRead(core);
      const r = saved.coreUpdate();
      // P6 第十二轮：眼球在原始 update 之后写入（恢复已完成，不会被抹掉）；数值仍是烘焙的 S.poseOut
      try { if (S.poseOut && !S.eyeWriteOff) saved.eyeOverride(core); } catch (e) { }
      DIAG.locPre = pre;
      DIAG.locPost = locRead(core);
      return r;
    };
    // P6：挂到 beforeModelUpdate 事件上写脸部参数。不会被主程序重装 coreModel.update 挤掉。
    // P6 第十二轮：舞台自有的眼球覆盖源（priority 36，排在 bridge 的 nekoMusicGaze=35 之后），每帧写眼球
    saved.eyeOverride = function (c) {
      const o = S.poseOut;
      if (!o || !c || typeof c.getParameterIndex !== 'function') return;
      for (const id of ['ParamEyeBallX', 'ParamEyeBallY']) {
        try {
          if (c.getParameterIndex(id) < 0) continue;
          c.setParameterValueById(id, o[id]);
          DIAG['eyeWrote_' + id] = o[id];
          DIAG['eyeReadBack_' + id] = Number(c.getParameterValueById(id));
        } catch (e) { }
      }
      DIAG.eyeOverrideCalls = (DIAG.eyeOverrideCalls || 0) + 1;
    };
    try {
      const mgr = global.live2dManager;
      // 眼球改由 core.update 包装在原始 update 之后写入（见下），不再登记 poseOverride
      DIAG.eyeOverrideSet = false;
    } catch (e) { DIAG.eyeOverrideSet = false; }
    saved.onBeforeModelUpdate = function () {
      DIAG.cbEnter += 1;
      try {
        // 结构修复：每帧确认挂着的实例仍是宿主当前渲染的模型；被替换（换模/重载）就重绑到新实例。
        // 用的是 live2dManager 的当前模型，不写任何宿主对象标记。
        const cur = currentHostModel();
        if (cur && S.model && cur !== S.model) { rebindModel(cur); return; }
        // 第二十五轮：渲染循环只有 30 fps，模型是 60 Hz；每帧按当前时刻现取姿态，慢动作不再一顿一顿
        try { if (S.poseTrack && S.playing) S.poseOut = poseAt(currentT()); } catch (e) { }
        const o = S.poseOut;
        if (!S.model) traceLog('cb-skip-noModel', {});
        if (!o) traceLog('cb-skip-noPose', {});
        if (!o || !S.model) return;
        const c = S.model.internalModel.coreModel;
        DIAG.writeFrames += 1;
        // 第二十六轮：桌面律动（music_groove 在 afterMotionUpdate 已写入本帧）做底，舞台舞步让位
        const now = performance.now();
        const fdt = S._cbLast ? U.clamp((now - S._cbLast) / 1000, .004, .1) : 1 / 60;
        S._cbLast = now;
        const GR = S._gr || (S._gr = { ema: 0, prev: null, hx: null, hy: null });
        const gv = {}; let act = 0;
        for (const id of GROOVE_IDS) {
          let v = 0; try { v = Number(c.getParameterValueById(id)) || 0; } catch (e) { }
          const sc = stdToModel(c, id, 1); if (sc && sc !== 1) v = v / sc;
          gv[id] = v; if (GR.prev) act += Math.abs(v - GR.prev[id]);
        }
        GR.prev = gv;
        GR.ema += (act / fdt - GR.ema) * (1 - Math.exp(-fdt / .8));
        const gp = U.clamp((GR.ema - 4) / 16, 0, 1);
        DIAG.grooveGp = gp; DIAG.grooveRate = GR.ema;
        // 慢歌：律动底子低通 0.45 s，逐拍点头被滤掉，只留慢摇
        if (softSong()) {
          GR.lp = GR.lp || {};
          for (const id of GROOVE_IDS) { const p0 = GR.lp[id]; GR.lp[id] = p0 == null ? gv[id] : p0 + (gv[id] - p0) * (1 - Math.exp(-fdt / .45)); gv[id] = GR.lp[id]; }
        }
        const fin = {};
        for (const k in o) {
          if (k.charCodeAt(0) === 68 && k.indexOf('Dance') === 0) continue;
          let v = o[k];
          if (gv[k] !== undefined) v = v - .75 * gp * (o['Dance' + k] || 0) + gp * gv[k];
          fin[k] = v;
        }
        // 第三十一轮：口型兜底——舞台的嘴只按歌词逐字时间算；这一刻没有逐字时间（无歌词/歌词没对上/句间有唱）时，
        // 用桥口型轨（按干声 cue + 包络算的 lastOpen/lastForm，和桌面档同一份）而不是写 0
        try {
          const Lp = global.__nekoMusicLips;
          if (Lp && Lp.active && typeof Lp.lastOpen === 'number') {
            const stageMo = Number(fin.ParamMouthOpenY) || 0;
            if (stageMo < .02 && Lp.lastOpen > .02) {
              fin.ParamMouthOpenY = Lp.lastOpen;
              if (typeof Lp.lastForm === 'number' && isFinite(Lp.lastForm)) fin.ParamMouthForm = U.lerp(fin.ParamMouthForm || 0, Lp.lastForm, .7);
              DIAG.mouthFromLips = (DIAG.mouthFromLips || 0) + 1;
            }
          }
        } catch (e) { }
        // 眼睛领先头部：头往哪边转，眼睛先看过去（头慢跟 0.3 s 的差值）
        const ax = fin.ParamAngleX || 0, ay = fin.ParamAngleY || 0;
        GR.hx = GR.hx == null ? ax : GR.hx + (ax - GR.hx) * (1 - Math.exp(-fdt / .3));
        GR.hy = GR.hy == null ? ay : GR.hy + (ay - GR.hy) * (1 - Math.exp(-fdt / .3));
        if (fin.ParamEyeBallX !== undefined) fin.ParamEyeBallX = o.ParamEyeBallX = U.clamp(fin.ParamEyeBallX + U.clamp((ax - GR.hx) * .04, -.3, .3), -.7, .7);
        if (fin.ParamEyeBallY !== undefined) fin.ParamEyeBallY = o.ParamEyeBallY = U.clamp(fin.ParamEyeBallY + U.clamp((ay - GR.hy) * .03, -.2, .2), -.7, .7);
        // 第三十轮（多模型）：按标准区间算好的值，换算到当前模型自己的区间再写（头 ±30、身 ±10、眼球 ±1 为标准）
        for (const k in fin) {
          try { c.setParameterValueById(k, clampParam(c, k, stdToModel(c, k, fin[k]))); } catch (e) { recordErr('setParam', e); }
        }
        // 第二十八轮：表情层（当前模型自己的 exp3，按主程序情绪映射）
        try {
          if (FX_LIB.model !== S.model) loadFxLib(S.model);
          const ea0 = emoActAt(currentT());
          if (ea0 && ea0.a.fx) applyFx(c, ea0.a.fx, ea0.fe, ea0.seed || 0);
          DIAG.fxAct = ea0 ? ea0.a.n : null; DIAG.fxW = ea0 ? +ea0.fe.toFixed(2) : 0;
        } catch (e) { recordErr('fx', e); }
        // 物理（头发/耳朵/衣服）：SDK 那一步已被舞台关掉（输入是写之前的姿态），这里按最终姿态再算一次
        try {
          const sv0 = S.modelSaved, im0 = S.model.internalModel;
          if (sv0 && sv0.physicsEval && im0.physics) { sv0.physicsEval.call(im0.physics, c, fdt); DIAG.physicsCalls = (DIAG.physicsCalls || 0) + 1; }
          // 第三十四轮：眼部物理（只由睁眼度驱动的那几组）平时固定在默认值，只有惊讶时放开
          const eyeIds = S._eyePhys || (S._eyePhys = eyePhysicsOutputs(im0));
          const eaS = emoActAt(currentT());
          const surprised = !!(eaS && eaS.a.fx && eaS.a.fx.surprised && eaS.fe > .2);
          if (!surprised && eyeIds.length) {
            for (const id of eyeIds) {
              try { const ix = c.getParameterIndex(id); if (ix >= 0) c.setParameterValueById(id, Number(c.getParameterDefaultValue(ix)) || 0); } catch (e) { }
            }
          }
          DIAG.eyePhysHeld = surprised ? 0 : eyeIds.length;
        } catch (e) { recordErr('physics', e); }
        // 第十二轮：同一帧配对记录（写入时的 S.poseOut 值，与 core.update 入口的渲染值比较）
        DIAG.pairWroteBrow = clampParam(c, 'ParamBrowLY', o.ParamBrowLY);
        DIAG.pairWroteCheek = clampParam(c, 'ParamCheek', o.ParamCheek); DIAG.pairWroteT = S.lastInfo ? S.lastInfo.t : null;
        try {
          DIAG.readBackBrowLY = Number(c.getParameterValueById('ParamBrowLY'));
          DIAG.readBackCheek = Number(c.getParameterValueById('ParamCheek'));
          DIAG.writtenBrowLY = o.ParamBrowLY;
        } catch (e) { recordErr('readBack', e); }
      } catch (e) { recordErr('callback', e); }
    };
    try { im.on('beforeModelUpdate', saved.onBeforeModelUpdate); traceLog('sub-before-ok', { inst: traceId(im) }); } catch (e) { traceLog('sub-before-err', { inst: traceId(im), err: String(e && e.message) }); }
    // 定位（只读）：模型 update 完成之后（afterModelUpdate）再读一次，看帧末值
    saved.onAfterModelUpdate = function () { DIAG.locAfterFrame = locRead(core); };
    try { im.on('afterModelUpdate', saved.onAfterModelUpdate); traceLog('sub-after-ok', { inst: traceId(im) }); } catch (e) { traceLog('sub-after-err', { inst: traceId(im), err: String(e && e.message) }); }
    // P6 第六轮：只记录 internalModel.update 每帧是否完成 afterMotionUpdate 一次
    saved.onAfterMotionUpdate = function () { DIAG.amu += 1; };
    try { im.on('afterMotionUpdate', saved.onAfterMotionUpdate); } catch (e) { traceLog('sub-amu-err', { err: String(e && e.message) }); }
    // P6 第八轮：进舞台时若宿主 ticker 已停着，用宿主公开接口 resumeRendering 开起来；只有舞台开的，退出时才停回
    S.resumedHostTicker = false;
    try {
      const mgr = global.live2dManager;
      const tk = mgr && mgr.pixi_app && mgr.pixi_app.ticker;
      const wasStarted = !!(tk && tk.started);
      // 宿主空闲省电时（_idleTickMode）用定时器驱动渲染，ticker.started 也是 false——那不是「暂停」。
      // 以前把它当暂停，退场时 pauseRendering() 真把渲染停了，人物定格在桌面上。
      const hostIdleTick = !!(mgr && mgr._idleTickMode);
      if (tk && tk.started === false && typeof mgr.resumeRendering === 'function') {
        mgr.resumeRendering();
        S.resumedHostTicker = !hostIdleTick;
      }
      traceLog('host-ticker', { wasStarted: wasStarted, idleTick: hostIdleTick, resumed: S.resumedHostTicker });
    } catch (e) { traceLog('host-ticker-err', { err: String(e && e.message) }); }
    try { model.anchor.set(.5, 0); } catch (e) { }
  }

  // 舞台退出时的面部复位：舞台运行时每帧用 setParameterValueById 绝对写这些
  // 表情/口型参数，unbindModel 只还原模型变换与 update 包装，不会把参数写回
  // 默认值；主程序也不一定重写它们（EyeSmile 已停写），于是最后一帧的笑眼/
  // 嘴形会留在脸上。退出时显式写回模型默认值 0（eye_dump 的 d0），
  // 不碰眼睑开合（归主程序眨眼器）与眼球位置（归 applyGazeLimit）。
  const STAGE_FACE_RESET = [
    'ParamEyeLSmile', 'ParamEyeRSmile',
    'ParamBrowLY', 'ParamBrowRY',
    'ParamBrowLForm', 'ParamBrowRForm',
    'ParamBrowLAngle', 'ParamBrowRAngle',
    'ParamCheek', 'ParamMouthOpenY', 'ParamMouthForm'
  ];
  function resetStageFace(core) {
    if (!core) return;
    for (const id of STAGE_FACE_RESET) {
      try { core.setParameterValueById(id, 0); } catch (e) { /* 模型没有这个参数 */ }
    }
    for (const id of FX_LIB.written) {
      try { const ix = core.getParameterIndex(id); if (ix >= 0) core.setParameterValueById(id, Number(core.getParameterDefaultValue(ix)) || 0); } catch (e) { }
    }
    FX_LIB.written.clear();
  }

  function unbindModel() {
    try { dropModelFx(); } catch (e) { }
    const m = S.model, sv = S.modelSaved;
    traceLog('unbind-enter', { inst: traceId(m && m.internalModel), hadSaved: !!sv, stack: traceStack() });
    if (!m || !sv) { S.model = null; S.modelSaved = null; return; }
    const im = m.internalModel, core = im.coreModel;
    try { core.update = sv.coreUpdateRaw; } catch (e) { }
    try { if (sv.onBeforeModelUpdate) im.off('beforeModelUpdate', sv.onBeforeModelUpdate); traceLog('off-before-ok', { inst: traceId(im) }); } catch (e) { traceLog('off-before-err', { err: String(e && e.message) }); }
    try { if (sv.onAfterModelUpdate) im.off('afterModelUpdate', sv.onAfterModelUpdate); } catch (e) { }
    try { const mgr2 = global.live2dManager; if (mgr2 && typeof mgr2.clearTemporaryPoseOverride === 'function') mgr2.clearTemporaryPoseOverride('nekoStageEyes'); } catch (e) { }
    try { if (sv.onAfterMotionUpdate) im.off('afterMotionUpdate', sv.onAfterMotionUpdate); } catch (e) { }
    try { if (sv.eyeBlink) im.eyeBlink.updateParameters = sv.eyeBlink; } catch (e) { }
    try { im.updateNaturalMovements = sv.natural; } catch (e) { }
    try { if (sv.physicsEval) im.physics.evaluate = sv.physicsEval; } catch (e) { }
    try { if (sv.motionStartRandom) im.motionManager.startRandomMotion = sv.motionStartRandom; } catch (e) { }
    try {
      m.anchor.set(sv.anchor.x, sv.anchor.y);
      m.scale.set(sv.scale.x, sv.scale.y);
      m.position.set(sv.position.x, sv.position.y);
      m.rotation = sv.rotation;
    } catch (e) { }
    try {
      if (S.container && sv.pivot) {
        S.container.pivot.set(sv.pivot.x, sv.pivot.y);
        S.container.position.set(sv.containerPos.x, sv.containerPos.y);
        S.container.rotation = sv.containerRot;
      }
    } catch (e) { }
    // 退出复位：把舞台最后一帧写进去的笑眼/眉/脸颊/嘴形写回默认 0
    try { resetStageFace(core); } catch (e) { }
    // P6 第八轮：只有舞台开的宿主 ticker 才由舞台停回；其他情况一律不碰
    // 第三十六轮：退场后人物还看得见（桌宠在显示）就让宿主继续渲染——以前一律停回，人物会定格在桌面上不动。
    // 只有宿主本来就是「请她离开」（画布隐藏）时才停回去。
    try {
      if (S.resumedHostTicker && typeof global.live2dManager.pauseRendering === 'function') {
        const cv = document.getElementById('live2d-canvas'), cont = document.getElementById('live2d-container');
        const hidden = el => { if (!el) return true; const cs = getComputedStyle(el); return cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity === 0; };
        const stillShown = !hidden(cv) && !hidden(cont);
        traceLog('host-ticker-exit', { stillShown });
        if (!stillShown) {
          global.live2dManager.pauseRendering();
        } else {
          // 还原后再刷新一次待机：清掉舞台留下的姿势，让宿主自己的待机动作接上
          try { if (typeof global.live2dManager.smoothResetToInitialState === 'function') global.live2dManager.smoothResetToInitialState(); } catch (e2) { }
        }
      }
    } catch (e) { traceLog('host-ticker-restore-err', { err: String(e && e.message) }); }
    S.resumedHostTicker = false;
    S.model = null; S.modelSaved = null;
  }

  // 人物占用格：48x27，按模型网格顶点（只看不透明的部件），膨胀 1 格；脸部（上 40% 高度）再左右各加 1 格
  const OCC_W = 48, OCC_H = 27;
  function modelOccupancy() {
    const m = S.model;
    if (!m || !S.W) return null;
    const now = performance.now();
    if (S._occ && now - S._occAt < 90) return S._occ;   // 约 11 次/秒就够（人物动得慢）
    const im = m.internalModel, core = im && im.coreModel, md = core && core._model, dr = md && md.drawables;
    const ct = im && im.centeringTransform, wt = m.worldTransform;
    if (!dr || !ct || !wt) return null;
    const g = new Uint8Array(OCC_W * OCC_H);
    let minY = 1e9, maxY = -1e9;
    const W = S.W, H = S.H;
    for (let i = 0; i < dr.count; i++) {
      if (dr.opacities[i] < .15) continue;
      const p = dr.vertexPositions[i];
      for (let k = 0; k < p.length; k += 6) {           // 每 3 个顶点取 1 个，足够描出轮廓
        const px = ct.a * p[k] + ct.tx, py = ct.ty - ct.a * p[k + 1];
        const sx = wt.a * px + wt.c * py + wt.tx, sy = wt.b * px + wt.d * py + wt.ty;
        const gx = Math.floor(sx / W * OCC_W), gy = Math.floor(sy / H * OCC_H);
        if (gx < 0 || gy < 0 || gx >= OCC_W || gy >= OCC_H) continue;
        g[gy * OCC_W + gx] = 1;
        if (sy < minY) minY = sy; if (sy > maxY) maxY = sy;
      }
    }
    // 膨胀 1 格（留边）；脸部区域左右再多 1 格
    const out = new Uint8Array(OCC_W * OCC_H);
    const faceY = minY + (Math.min(maxY, H) - minY) * .4;
    for (let y = 0; y < OCC_H; y++) for (let x = 0; x < OCC_W; x++) {
      if (!g[y * OCC_W + x]) continue;
      const face = (y + .5) / OCC_H * H < faceY;
      const r = face ? 2 : 1;
      for (let dy = -1; dy <= 1; dy++) for (let dx = -r; dx <= r; dx++) {
        const xx = x + dx, yy = y + dy;
        if (xx >= 0 && yy >= 0 && xx < OCC_W && yy < OCC_H) out[yy * OCC_W + xx] = 1;
      }
    }
    S._occ = { w: OCC_W, h: OCC_H, g: out, W, H };
    S._occAt = now;
    return S._occ;
  }

  // 物理设置里「输入只有左右眼睁眼度」的那几组 → 它们的输出参数（眼珠/瞳孔/高光的弹性）。通用：按模型自己的 physics3 找
  function eyePhysicsOutputs(im) {
    const out = [];
    try {
      const rig = im.physics && im.physics._physicsRig;
      if (!rig) return out;
      const at = (v, i) => (v && typeof v.at === 'function') ? v.at(i) : v[i];
      const n = rig.settings.getSize ? rig.settings.getSize() : rig.settings.length;
      for (let k = 0; k < n; k++) {
        const st = at(rig.settings, k);
        let eyeOnly = st.inputCount > 0;
        for (let i = 0; i < st.inputCount; i++) {
          const src = at(rig.inputs, st.baseInputIndex + i).source;
          if (!/^ParamEye[LR]Open$/.test(String(src && src.id || ''))) { eyeOnly = false; break; }
        }
        if (!eyeOnly) continue;
        for (let j = 0; j < st.outputCount; j++) {
          const d = at(rig.outputs, st.baseOutputIndex + j).destination;
          if (d && d.id) out.push(String(d.id));
        }
      }
    } catch (e) { }
    return out;
  }

  // 第三十五轮：人物剪影（模型局部像素坐标、相对锚点 (0.5,0)），进舞台时量一次；按镜头换算
  // 第四十七轮：人物配色（用于滤镜匹配）。把模型单独渲染到 64x64 小纹理，读回像素算平均亮度 / 饱和 / 主色相。
  function measureFigureColor() {
    const m = S.model, P = global.PIXI;
    if (!m || !P) return null;
    const app = (global.live2dManager && (global.live2dManager.pixi_app || global.live2dManager.app)) || global.__app || null;
    const renderer = (app && app.renderer) || (m.parent && m.parent.renderer) || null;
    if (!renderer || !renderer.extract) return null;
    const b = m.getBounds(); if (!b || b.width < 4 || b.height < 4) return null;
    const rt = P.RenderTexture.create({ width: 64, height: 64 });
    const sx = m.scale.x, sy = m.scale.y, px = m.position.x, py = m.position.y;
    try {
      const k = 64 / Math.max(b.width, b.height);
      m.scale.set(sx * k, sy * k); const b2 = m.getBounds();
      m.position.set(px - b2.x, py - b2.y);
      renderer.render(m, { renderTexture: rt, clear: true });
    } finally { m.scale.set(sx, sy); m.position.set(px, py); }
    const pix = renderer.extract.pixels(rt); rt.destroy(true);
    let n = 0, Ls = 0, Ss = 0, hx = 0, hy = 0;
    for (let i = 0; i < pix.length; i += 4) {
      const a = pix[i + 3]; if (a < 200) continue;
      const r = pix[i] / 255, g = pix[i + 1] / 255, bl = pix[i + 2] / 255;
      const mx = Math.max(r, g, bl), mn = Math.min(r, g, bl), l = (mx + mn) / 2, d = mx - mn;
      const sat = d < 1e-3 ? 0 : d / (1 - Math.abs(2 * l - 1) + 1e-6);
      let hue = 0;
      if (d > 1e-3) { hue = mx === r ? ((g - bl) / d) % 6 : mx === g ? (bl - r) / d + 2 : (r - g) / d + 4; hue *= 60; }
      n++; Ls += l; Ss += sat; hx += Math.cos(hue * Math.PI / 180) * sat; hy += Math.sin(hue * Math.PI / 180) * sat;
    }
    if (n < 50) return null;
    const f = { lum: Ls / n, sat: Ss / n, hue: ((Math.atan2(hy, hx) * 180 / Math.PI) + 360) % 360, n };
    DIAG.figure = f;
    return f;
  }

  function measureSilhouette() {
    const m = S.model;
    if (!m) return null;
    const im = m.internalModel, md = im && im.coreModel && im.coreModel._model, dr = md && md.drawables, ct = im && im.centeringTransform;
    if (!dr || !ct) return null;
    const ow = im.originalWidth || 1, oh = im.originalHeight || 1;
    const pts = [];
    for (let i = 0; i < dr.count; i++) {
      if (dr.opacities[i] < .15) continue;
      const p = dr.vertexPositions[i];
      for (let k = 0; k < p.length; k += 4) {
        const px = ct.a * p[k] + ct.tx, py = ct.ty - ct.a * p[k + 1];
        pts.push((px - ow / 2) / oh, py / oh);     // 以「模型高度」为单位，相对锚点（宽度中点、顶边）
      }
    }
    return { pts: new Float32Array(pts) };
  }
  // 给定镜头，人物在屏幕上的占用格（48x27）：与 placeModel 同一套换算（缩放 sf*H，锚点 cx*W, (headY-.049*sf)*H）
  function occAtCam(cam) {
    const sil = S._sil;
    if (!sil || !S.W) return null;
    const W = S.W, H = S.H, GW = 48, GH = 27, g = new Uint8Array(GW * GH);
    const sc = cam.sf * H, ax = cam.cx * W, ay = (cam.headY - .049 * cam.sf) * H;
    const P = sil.pts;
    for (let i = 0; i < P.length; i += 2) {
      const x = ax + P[i] * sc, y = ay + P[i + 1] * sc;
      const gx = Math.floor(x / W * GW), gy = Math.floor(y / H * GH);
      if (gx >= 0 && gy >= 0 && gx < GW && gy < GH) g[gy * GW + gx] = 1;
    }
    // 补洞：网格顶点是点，按行把最左到最右之间填满（人物是实心的）
    for (let y = 0; y < GH; y++) {
      let a = -1, b = -1;
      for (let x = 0; x < GW; x++) if (g[y * GW + x]) { if (a < 0) a = x; b = x; }
      if (a >= 0) for (let x = a; x <= b; x++) g[y * GW + x] = 1;
    }
    return { w: GW, h: GH, g };
  }
  function planLyrics() {
    if (!S.lyrics || !global.NekoStagePlanner || !S._sil || !S.W) return;
    const cuts = (S.shots || []).filter(s => s.cut).map(s => s.t);
    S.lyrics.plan({
      W: () => S.W, H: () => S.H, cameraAt, occAt: occAtCam, cuts,
      preset: (S.show.style && S.show.style.preset) || 'pop', seed: S.seed >>> 0, fsBase: S.cfg.fsBase || .07
    });
    S._planned = true;
  }

  /* 第四十五轮：人物 GPU 滤镜（开源 pixi-filters，MIT）。只在显卡上算，每个主题一套：
   *   AdjustmentFilter 调饱和/亮度/对比/色偏；RGBSplitFilter 色差（随拍子抖）。高光溢出（AdvancedBloom）会把白衣服糊成一片，已弃用。
   * 宿主没有 pixi-filters 时整个跳过，不影响演出。退出时把原来的 filters 还回去。 */
  function applyModelFx(t, climax, pulse) {
    const m = S.model, F = global.PIXI && global.PIXI.filters;
    if (!m || !F || !F.AdjustmentFilter || !S.look || !S.look.px) return;
    if (!S._mfx) {
      try {
        S._mfx = { saved: m.filters || null, adj: new F.AdjustmentFilter(), bloom: null,   // 高光溢出试过：人物衣服大面积白色，会整个糊成白（过曝），不用
          rgb: F.RGBSplitFilter ? new F.RGBSplitFilter([0, 0], [0, 0], [0, 0]) : null };
        m.filters = [S._mfx.adj].concat(S._mfx.bloom ? [S._mfx.bloom] : [], S._mfx.rgb ? [S._mfx.rgb] : []);
      } catch (e) { recordErr('mfx-init', e); S._mfx = { broken: true }; return; }
    }
    const X = S._mfx; if (X.broken) return;
    const P = S.look.px, mix = P.mix;
    const g = (k, d) => { const a = P.cur && P.cur[k] != null ? P.cur[k] : d, b = P.prev && P.prev[k] != null ? P.prev[k] : d; return P.prev ? U.lerp(b, a, mix) : a; };
    X.adj.saturation = g('sat', 1); X.adj.brightness = g('bright', 1); X.adj.contrast = g('contrast', 1);
    X.adj.red = g('red', 1); X.adj.blue = g('blue', 1); X.adj.green = 1;
    if (X.bloom) X.bloom.bloomScale = g('bloom', 0) * (climax ? 1.25 : .8) * (S.H / 1080);
    if (X.rgb) {
      const r = g('rgb', 0) * (climax ? 1 + 1.5 * pulse : .6) * (S.H / 1080);
      X.rgb.red = [-r, 0]; X.rgb.green = [0, 0]; X.rgb.blue = [r, 0];
      X.rgb.enabled = r > .05;
    }
  }
  function dropModelFx() {
    const X = S._mfx; S._mfx = null;
    if (!X || X.broken) return;
    try { if (S.model) S.model.filters = X.saved; } catch (e) { }
    try { X.adj.destroy(); if (X.bloom) X.bloom.destroy(); if (X.rgb) X.rgb.destroy(); } catch (e) { }
  }

  function placeModel(cam) {
    const m = S.model;
    if (!m) return;
    const W = S.W, H = S.H;
    const im = m.internalModel;
    const oh = im.originalHeight || m.height / (m.scale.y || 1);
    m.scale.set(cam.sf * H / oh);
    m.position.set(cam.cx * W + cam.hx, (cam.headY - .049 * cam.sf) * H + cam.hy);
    if (S.container) {
      S.container.pivot.set(W / 2, H / 2);
      S.container.position.set(W / 2, H / 2);
      S.container.rotation = cam.rot * Math.PI / 180;
    } else {
      m.rotation = cam.rot * Math.PI / 180;
    }
  }

  /* ------------------------------ 尺寸 / DOM ------------------------------ */
  function applySize() {
    const W = S.W = S.root.clientWidth || global.innerWidth || 1280;
    const H = S.H = S.root.clientHeight || global.innerHeight || 720;
    const BW = S.BW = Math.min(MAX_BG_W, Math.max(960, Math.round(W * .6)));
    const BH = S.BH = Math.max(1, Math.round(BW * H / W));
    const els = S.els;
    els.bg.width = BW; els.bg.height = BH;
    els.off.width = BW; els.off.height = BH;
    const fxs = S.fxScale || 1; els.fx.width = Math.round(W * fxs); els.fx.height = Math.round(H * fxs);
    // 全屏遮挡转场：按半分辨率画（大色块，不需要满分辨率），CSS 拉满
    if (els.stinger) { els.stinger.width = Math.round(W / 2); els.stinger.height = Math.round(H / 2); }
    if (els.frontp) { els.frontp.width = Math.round(W * .75); els.frontp.height = Math.round(H * .75); }
    if (els.vgn) { els.vgn.width = Math.round(W * .75); els.vgn.height = Math.round(H * .75); }
    if (els.dir) { els.dir.width = W; els.dir.height = H; }   // 字卡要清楚：满分辨率
    if (els.acc) { els.acc.width = Math.round(W * .75); els.acc.height = Math.round(H * .75); }
    if (els.dir2) { els.dir2.width = W; els.dir2.height = H; }
    if (S.lyrics) S.lyrics.layout(W, H);
    if (S.look) S.look.resize(W, H);
    S._planned = false;   // 尺寸变了：下一帧重新规划
    S.dirty = true;
  }

  function buildDom(root) {
    const wrap = document.createElement('div');
    wrap.className = 'ns-wrap';
    wrap.style.cssText = 'position:absolute;inset:0;overflow:hidden;';
    wrap.innerHTML =
      '<div class="ns-world"><canvas class="ns-bg"></canvas><canvas class="ns-vgn" style="position:absolute;inset:0;width:100%;height:100%;z-index:1;pointer-events:none"></canvas><canvas class="ns-acc" style="position:absolute;inset:0;width:100%;height:100%;z-index:2;pointer-events:none"></canvas><canvas class="ns-dir" style="position:absolute;inset:0;width:100%;height:100%;z-index:3;pointer-events:none"></canvas><canvas class="ns-dir2" style="position:absolute;inset:0;width:100%;height:100%;z-index:6;pointer-events:none"></canvas><canvas class="ns-fx"></canvas>' +
      '<div class="ns-ly ns-lyback"></div><canvas class="ns-l2d"></canvas><canvas class="ns-front"></canvas><div class="ns-ly ns-lyfront"></div>' +
      '<div class="ns-bars"><i></i><i></i></div></div>' +
      '<div class="ns-vig"></div><div class="ns-grain"></div>' +
      '<div class="ns-sub"><div class="row cur"><span class="base"></span><span class="fill"></span></div><div class="row nxt"></div></div>' +
      '<div class="ns-flash"></div><div class="ns-black"></div><canvas class="ns-stinger"></canvas><div class="ns-hud"></div>';
    root.appendChild(wrap);
    root.classList.add('ns-root');
    const q = sel => wrap.querySelector(sel);
    const els = {
      wrap, world: q('.ns-world'), bg: q('.ns-bg'), fx: q('.ns-fx'),
      back: q('.ns-lyback'), l2d: q('.ns-l2d'), front: q('.ns-lyfront'),
      bars: wrap.querySelectorAll('.ns-bars i'), grain: q('.ns-grain'),
      sub: q('.ns-sub'), flash: q('.ns-flash'), black: q('.ns-black'), hud: q('.ns-hud'),
      stinger: q('.ns-stinger'), frontp: q('.ns-front')
    };
    els.fpc = els.frontp ? els.frontp.getContext('2d') : null;
    els.vgn = q('.ns-vgn'); els.vgc = els.vgn ? els.vgn.getContext('2d') : null;
    els.dir = q('.ns-dir'); els.dic = els.dir ? els.dir.getContext('2d') : null;
    els.acc = q('.ns-acc'); els.acx = els.acc ? els.acc.getContext('2d') : null;
    els.dir2 = q('.ns-dir2'); els.dic2 = els.dir2 ? els.dir2.getContext('2d') : null;
    els.stc = els.stinger ? els.stinger.getContext('2d') : null;
    els.bgc = els.bg.getContext('2d');
    els.fxc = els.fx.getContext('2d');
    els.off = document.createElement('canvas');
    els.offc = els.off.getContext('2d');
    // 颗粒：固定的噪声贴图（只生成一次，不随时间变）
    const g = document.createElement('canvas');
    g.width = g.height = 200;
    const gc = g.getContext('2d');
    const img = gc.createImageData(200, 200);
    const rng = U.mulberry32(1337);
    for (let i = 0; i < img.data.length; i += 4) {
      const v = Math.round(rng() * 255);
      img.data[i] = img.data[i + 1] = img.data[i + 2] = v; img.data[i + 3] = 255;
    }
    gc.putImageData(img, 0, 0);
    els.grain.style.background = 'url(' + g.toDataURL() + ')';
    return els;
  }

  /* ------------------------------ 渲染 ------------------------------ */
  function renderAt(t) {
    if (!S.show) return null;
    t = U.clamp(+t || 0, 0, S.dur);
    // 第五十轮：伴奏等待段——画面交给一幅线稿小画，人物让到一角缩小（不再一直盯着人物等）
    // 第五十一轮：分镜导演——人物不是主角。导演层画铺色/字卡/物件，并告诉我们这一镜头有没有人物、定格在哪
    let DIR = null;
    if (S.dirPlan && S.els.dic && global.NekoStageDirector) {
      try {
        const envD = { pulse: pulseAt(t), I: intensityAt(t), bpm: S.show.music.bpm };
        DIR = global.NekoStageDirector.draw(S.els.dic, S.els.dir.width, S.els.dir.height, t, S.dirPlan, envD);
        if (S.els.dic2) global.NekoStageDirector.drawTop(S.els.dic2, S.els.dir2.width, S.els.dir2.height, t, S.dirPlan, envD, DIR);
        if (DIR) { DIR.shot = DIR.scene; DIR.p = DIR.k || 0; }
      } catch (e) { recordErr('director', e); DIR = null; }
    }
    const VG = DIR ? { a: 0, n: 0, side: 1, kind: 'pulse' } : vignetteAt(t);
    let camRaw = cameraAt(t);
    if (DIR && DIR.frame) {
      // 人物定格：镜头固定在这个构图，只做很慢的推近（Ken Burns 2.5%），不跟运镜飘
      const f = DIR.frame, kb = 1 + .04 * DIR.p, sc = DIR.scene, ein = U.easeOut(U.clamp((t - sc.t0) / .5));
      const dx = (f.cx < .5 ? -1 : 1) * .08 * (1 - ein);          // 从画面外侧滑进来一点
      camRaw = { sf: f.sf * kb, headY: f.headY + .02 * (1 - ein), cx: f.cx + dx, rot: 0, hx: 0, hy: 0, kind: 'base', cut: false };
      // 换镜头时直接到位（定格画不该滑进来）：重置平滑器
      if (S._dirShot !== DIR.shot) { S._camSm = null; S._dirShot = DIR.shot; }
    } else if (DIR) { S._dirShot = DIR.shot; }
    if (VG.a > 0) {
      const k = U.easeInOut(VG.a), side = VG.side;
      camRaw = Object.assign({}, camRaw, { sf: U.lerp(camRaw.sf, 1.15, k), cx: U.lerp(camRaw.cx, side > 0 ? .86 : .14, k), headY: U.lerp(camRaw.headY, .5, k), rot: camRaw.rot * (1 - k) });
    }
    const cam = smoothCam(camRaw, t);
    const I = intensityAt(t), pu = pulseAt(t), sec = sectionAt(t);
    // 线系主题：把当前底色、手书底要用的歌词交给线条场景
    if (S.art) {
      S.art.groundNow = S.art.groundAt ? S.art.groundAt(t) : null;
      if (S.art.isLineAt && S.art.isLineAt(t) && S.lyrics && S.lyrics.lineAt) {
        if (!S.art.scriptLines) S.art.scriptLines = ((S.show && S.show.lines) || []).map(l => String(l.text || '').trim()).filter(x => x.length > 1).slice(0, 80);
        const Lc0 = S.lyrics.lineAt(t); if (Lc0 && Lc0.i != null) S.art.scriptCur = Lc0.i;
      }
    }
    const idx = S.scenes.findIndex(s => t < s.t1);
    const si = idx < 0 ? S.scenes.length - 1 : idx;
    const sceneInfo = Scenes.drawBackground({
      bgCtx: S.els.bgc, off: S.els.off, offCtx: S.els.offc,
      W: S.BW, H: S.BH, t, show: S.show, cam, bar: S.bar,
      sceneIndex: si,
      intensity: I, pulse: pu, seed: S.seed,
      sceneVar: U.h01(S.seed * .001 + si * 7.77),
      prevSceneVar: U.h01(S.seed * .001 + (si - 1) * 7.77),
      skin: skinFor(t), decor: S.cfg.decor
    });
    // CSS 层：模糊/亮度/轻微推拉跟着段落和镜头走
    // 亮度直接用场景层返回的 lum（已在 stage-scenes.js 里乘过一次脉冲项，与原型一致）
    // 第四十七轮：背景亮度按人物配色调（白发白衣 → 背景压暗、更饱和，人物更跳；深色人物 → 背景提亮）
    const bgDim = S.look && S.look.bgDim ? S.look.bgDim : 0;
    const lineNow = !!(S.art && S.art.isLineAt && S.art.isLineAt(t));
    S.els.bg.style.filter = lineNow ? 'none' : 'blur(' + (sceneInfo.blur * .45 * S.H / 1080).toFixed(1) + 'px) brightness('
      + Math.max(.72, Math.min(1.05, sceneInfo.lum) * (1 - bgDim)).toFixed(2) + ') saturate(' + (1.12 + bgDim * .8).toFixed(2) + ')';
    S.els.bg.style.transform = 'translate(' + (-(cam.cx - .5) * S.W * .05).toFixed(1) + 'px,0) scale('
      + (1 + (cam.sf - 2) * .02).toFixed(3) + ')';

    if (S.els.acx) {
      if (S.accPlan && !S.dirPlan) { try { global.NekoStageDirector.drawAccent(S.els.acx, S.els.acc.width, S.els.acc.height, t, S.accPlan, { pulse: pu, bpm: S.show.music.bpm }); } catch (e) { recordErr('accent', e); } }
      else if (S._accDrawn) S.els.acx.clearRect(0, 0, S.els.acc.width, S.els.acc.height);
      S._accDrawn = !!S.accPlan;
    }
    if (S.els.vgc && global.NekoStageLines) {
      const Ln = global.NekoStageLines, vw = S.els.vgn.width, vh = S.els.vgn.height;
      const Pv = { pal: S.show.style.palette || {}, I, pulse: pu, bpm: S.show.music.bpm, cx: 1 - cam.cx, v: U.h01(S.seed * .0007 + VG.n * 3.3), ground: S.art && S.art.groundNow };
      Ln.drawVignette(S.els.vgc, vw, vh, t, VG.kind, VG.a, Pv);
      // 线条过场：线系主题里，段落交界前后一小节，一条线画过 → 分裂成多条 → 收回（不遮人物、不换黑屏）
      if (lineNow && VG.a <= 0) {
        const bar = Math.max(1.2, 4 * (S.beat || .5));
        for (const sx of S.sections) { if (sx.t0 > 2 && Math.abs(t - sx.t0) < bar * 1.05) { Ln.drawLineBridge(S.els.vgc, vw, vh, t, sx.t0, bar, Pv); break; } }
      }
    }
    S.els.fxc.setTransform(S.fxScale || 1, 0, 0, S.fxScale || 1, 0, 0);
    if (!lineNow) Scenes.drawForeground({
      ctx: S.els.fxc, W: S.W, H: S.H, t, show: S.show, intensity: I, pulse: pu,
      sectionId: sec.id, anchors: S.lyrics.anchors
    });
    else S.els.fxc.clearRect(0, 0, S.W, S.H);

    // 第三十六轮：人物前面的一层稀疏粒子（花瓣/雪/金粉/泡泡/音符，跟着场景走），营造景深
    if (S.els.fpc && Scenes.drawFront) {
      const kind = (Scenes.FRONT_OF || {})[sceneInfo.scene] || null;
      const pal = S.show.style.palette || {};
      Scenes.drawFront(S.els.fpc, S.els.frontp.width, S.els.frontp.height, t, kind,
        { dens: (sceneInfo.density || 1) * (.6 + .6 * I), col: pal.hot || pal.c1 });
      // 换场景时粒子种类会变：在切点前后 0.6 s 淡出再淡入，不硬换
      const sc0 = S.scenes[si], dt = sc0 ? Math.min(Math.abs(t - sc0.t0), Math.abs(sc0.t1 - t)) : 9;
      S.els.frontp.style.opacity = lineNow ? '0' : U.clamp(dt / .6).toFixed(3);
    }
    S.poseOut = poseAt(t);
    placeModel(cam);
    if (S.dirPlan) {
      const show = !!(DIR && DIR.figure), hostCont = document.getElementById('live2d-container');
      // 宿主里人物画在 live2d-container；预演页里画在舞台自己的 ns-l2d 画布
      const cont = (hostCont && S.root && S.root.contains(hostCont)) ? hostCont : S.els.l2d;
      if (cont) {
        // 人物镜头：整张定格淡入 0.25 s；非人物镜头：淡出。分屏时人物只露出字卡另一侧
        const want = show ? 1 : 0, cur = S._figA == null ? want : S._figA, dtw = Math.min(.1, Math.max(0, (performance.now() - (S._figWall || performance.now())) / 1000));
        S._figWall = performance.now();
        S._figA = want;   // 硬切：参考 MV 都是切镜头；淡出会让人物半透明地叠在字卡上（实测截图里出现过）
        // 宿主加载器的样式对人物容器写了 opacity:1!important，这里必须用行内 !important 才盖得过
        cont.style.setProperty('opacity', S._figA.toFixed(3), 'important');
        const k1 = Math.max(2, Math.round(S.H / 300)), pp = (DIR && DIR.P) || { paper: '#ffffff' };
        const stick = 'drop-shadow(' + k1 + 'px 0 0 ' + pp.paper + ') drop-shadow(-' + k1 + 'px 0 0 ' + pp.paper + ') drop-shadow(0 ' + k1 + 'px 0 ' + pp.paper + ') drop-shadow(0 -' + k1 + 'px 0 ' + pp.paper + ') drop-shadow(0 ' + (k1 * 2) + 'px ' + (k1 * 3) + 'px rgba(40,30,60,.28))';
        if (cont._stick !== stick) { cont.style.filter = stick; cont._stick = stick; }
        const sp = '';
        if (cont._clip !== sp) { cont.style.clipPath = sp; cont._clip = sp; }
      }
      const hide = true;   // 手书分镜下，旧的飘字层始终不用（歌词由分镜层画在字位里）
      if (S._dirHide !== hide) { S._dirHide = hide; S.els.back.style.visibility = S.els.front.style.visibility = hide ? 'hidden' : 'visible'; }
    }
    // 第三十五轮：歌词位置由规划器一次排好（见 planLyrics），这里只在第一次拿到模型剪影时排一次
    if (!S._planned && S.model) { try { if (!S._sil) S._sil = measureSilhouette(); planLyrics(); } catch (e) { recordErr('plan', e); S._planned = true; } }
    if (S.model && !S._figTried && S.look && S.look.setFigure && t > .6) { S._figTried = true; try { const f = measureFigureColor(); if (f) S.look.setFigure(f); } catch (e) { recordErr('figcolor', e); } }
    S.lyrics.render(t);
    S.lyrics.subtitle(t, S.subOn);

    // 世界层：冲击震屏
    const shake = evAmp(t, 'shake', 14) * ((S.shots.find(s => s.t <= t) || { shake: 0 }).shake + .3);
    S.els.world.style.transform = shake > .2
      ? 'translate(' + ((U.h01(t * 91) - .5) * shake * 2).toFixed(1) + 'px,' + ((U.h01(t * 57) - .5) * shake * 2).toFixed(1) + 'px) rotate(' + (cam.rot * .15).toFixed(2) + 'deg)'
      : 'rotate(' + (cam.rot * .15).toFixed(2) + 'deg)';
    S.els.back.style.transform = '';
    S.els.front.style.transform = S.els.back.style.transform;

    // 第三十九轮：闪白不再硬闪（以前一帧全白再弹回，看着像背景「闪一下又闪回去」）——
    // 0.12 s 渐起、峰值压到 .35、慢慢退；转场遮挡期间不叠闪白
    {
      let fa = 0;
      for (const e of S.events) {
        if (e.t - .12 > t) break;
        if (e.type !== 'flash') continue;
        const d = t - e.t;
        const up = U.clamp((d + .12) / .12), down = d > 0 ? Math.exp(-d * 2.6) : 1;
        if (d > -.12 && d < 1.6) fa = Math.max(fa, (e.amp || 0) * up * down);
      }
      const stingOn = S.els.stinger && S.els.stinger.style.display === 'block';
      S.els.flash.style.opacity = (stingOn ? 0 : Math.min(.35, fa * .4)).toFixed(3);
    }
    let bl = 0;
    for (const e of S.events) {
      if (e.t - (e.dur || 0) - .25 > t) break;
      if (e.type !== 'black') continue;
      const a0 = e.t - (e.dur || 0);
      bl = Math.max(bl, U.clamp((t - (a0 - .25)) / .25) * U.clamp((e.t + .25 - t) / .25));
    }
    S.els.black.style.opacity = bl.toFixed(3);

    // 第三十七轮：整体观感（调色/漏光/虚化光斑/上下模糊蒙版/几何线/边框/背后大字/人物辉光）
    if (S.look) {
      try {
        const Lc = S.lyrics.lineAt(t);
        S.look.update(t, { intensity: I, pulse: pu, section: sec.id, camCx: cam.cx, title: stageTitle(),
          line: Lc ? { kw: Lc.kw, text: Lc.text, t0: Lc.t0, t1: Lc.t1 } : null });
        try { applyModelFx(t, sec.id === 'chorus' || sec.id === 'final', pu); } catch (e) { recordErr('mfx', e); }
        const cont = document.getElementById('live2d-container');
        if (cont && S.root && S.root.contains(cont)) {
          const f = S.look.modelFilter(I, sec.id === 'chorus' || sec.id === 'final');
          if (!S.dirPlan && cont.style.filter !== f) cont.style.filter = f;
        }
      } catch (e) { recordErr('look-update', e); }
    }

    // 第三十六轮：全屏遮挡转场（盖住背景+人物+歌词），只在进副歌/最终副歌/桥段的段落交界
    if (S.els.stc && global.NekoStageStinger) {
      const on = !S.dirPlan && global.NekoStageStinger.draw(S.els.stc, S.els.stinger.width, S.els.stinger.height, t, S.stingers);
      S.els.stinger.style.display = on ? 'block' : 'none';
    }

    // 桥段遮幅：上下黑边缓缓压进来
    const bs = S.sections.find(s => s.id === 'bridge');
    let lb = 0;
    if (bs) lb = U.sstep(bs.t0 - .3, bs.t0 + .5, t) * (1 - U.sstep(bs.t1 - S.beat, bs.t1, t));
    // 第五十三轮：边框已经有上下条（胶片/电影/VHS/剧场/胶片条扩展）时，不再压黑边——否则两层上下条叠在一起
    if (/^(film|cinema|vhs|theater|ext:filmstrip2)$/.test(String(global.__NEKO_FRAME_KIND || ''))) lb = 0;
    S.els.bars.forEach(b => { b.style.height = (lb * 9).toFixed(2) + 'vh'; });

    // 颗粒位移：由 t 派生，保证同一 t 画面一致
    const gf = Math.floor(t * 15);
    S.els.grain.style.transform = 'translate(' + ((U.h01(gf * 3.1) - .5) * 10).toFixed(1) + '%,'
      + ((U.h01(gf * 5.7) - .5) * 10).toFixed(1) + '%)';

    if (S.hud) renderHud(t, cam, sceneInfo, sec, I);
    S.lastT = t;
    // P6 诊断：当前句的情绪、强度与分档（只读，供 CDP 取值，不影响渲染）
    const curL = S.lyrics.lineAt(t);
    S.lastInfo = { t, cam, scene: sceneInfo, section: sec.id, intensity: I, preset: S.show.style.preset,
      line: curL ? { emotion: curL.emotion, inten: curL.intensity, i: curL.i } : null,
      face: { emoKey: curL ? curL.emotion : null, emK: curL ? exprTier(curL.intensity).mult : null,
        poseBrowLY: S.poseOut ? S.poseOut.ParamBrowLY : null, poseCheek: S.poseOut ? S.poseOut.ParamCheek : null,
        poseEyeSmile: S.poseOut ? S.poseOut.ParamEyeLSmile : null, diag: Object.assign({}, DIAG), trace: TRACE.slice(-60) } };
    return S.lastInfo;
  }

  function renderHud(t, cam, sc, sec, I) {
    const L = S.lyrics.lineAt(t);
    const c = L ? L.combo : null;
    const pose = S.poseOut || {};
    S.els.hud.innerHTML =
      '<b>' + (S.show.style.theme || '') + '</b> · ' + (S.show.style.preset || '') + ' · ' + S.show.music.bpm + ' BPM · 种子 <span class="t">' + S.seed + '</span><br>'
      + '<span class="k">段落</span><b>' + (sec.label || sec.id) + '</b>　<span class="k" style="width:3em">强度</span><span class="bar" style="width:' + (I * 90).toFixed(0) + 'px"></span><br>'
      + '<span class="k">镜头</span>' + (CN.shot[cam.kind] || cam.kind) + ' · 构图' + (cam.cx < .45 ? '偏左' : cam.cx > .55 ? '偏右' : '居中') + (Math.abs(cam.rot) > .5 ? ' · 荷兰角' : '') + '<br>'
      + '<span class="k">背景</span>' + (CN.scene[sc.scene] || sc.scene) + ' · ' + (CN.entry[sc.entry] || sc.entry) + '出场' + (sc.density > 1 ? ' · 升级版' : '') + '<br>'
      + (c
        ? '<span class="k">歌词</span>' + (L.kind === 'title' ? '开场标题 · ' : L.kind === 'flash' ? '关键词闪回 · ' : '')
          + (CN.reveal[c.reveal] || c.reveal) + ' · ' + (CN.stagger[L.stagger] || L.stagger) + ' · '
          + (CN.layout[c.layout] || c.layout) + ' · ' + (CN.material[c.material] || c.material) + ' · '
          + (CN.exit[c.exit] || c.exit) + (c.echo ? ' · <span class="t">重复句升级+回声</span>' : '') + (c.front ? ' · <span class="t">前排爆点</span>' : '') + '<br>'
        : '<span class="k">歌词</span>—<br>')
      + '<span class="k">人物</span>嘴 ' + (pose.ParamMouthOpenY || 0).toFixed(2) + ' · ' + (CN.emo[L ? L.emotion : ''] || (L ? L.emotion : '—')) + ' · ' + (CN.gaze[L ? L.gaze : ''] || '—');
  }

  /* ------------------------------ 时钟 / 主循环 ------------------------------ */
  function currentT() {
    if (S.clock) {
      if (typeof S.clock === 'function') return +S.clock() || 0;
      if (typeof S.clock.now === 'function') return +S.clock.now() || 0;
    }
    if (S.playing) return S.show0 + (performance.now() - S.wall0) / 1000;
    return S.pausedT;
  }

  function loop(ts) {
    S.raf = requestAnimationFrame(loop);
    if (document.hidden) { S.hidden = true; return; }
    if (S.hidden) { S.hidden = false; S.lastPaint = 0; }
    // 第四十九轮：舞台更新跟显示器帧同步（上限 60）。以前固定 30 fps，人物按宿主 60~144 fps 画，
    // 背景/镜头/歌词却 33 ms 才动一次，两者错拍 → 看起来一顿一顿的
    if (ts - S.lastPaint < 1000 / (S.fpsCap ? S.fpsCap : Math.max(S.fps, 60)) - 2) return;
    S.lastPaint = ts;
    if (!S.playing && !S.dirty) return;
    S.dirty = false;
    const t0 = performance.now();
    renderAt(currentT());
    if (S.playing) watchQuality(performance.now() - t0);
    if (S.statsOn) { S.stats.push(performance.now() - t0); if (S.stats.length > 600) S.stats.shift(); }
  }

  /* ------------------------------ 生命周期 ------------------------------ */
  function start(opts) {
    opts = opts || {};
    stop();
    S.show = opts.show;
    // 歌名：加载器传进来的（播放器标题），没有就用编排主题「「歌名」唱的是…」里的歌名
    S.title = String(opts.title || '');
    if (!S.title) { const m = String((S.show.style && S.show.style.theme) || '').match(/「([^」]{1,40})」/); if (m) S.title = m[1]; }
    S.root = typeof opts.root === 'string' ? document.querySelector(opts.root) : (opts.root || document.body);
    if (!S.root) throw new Error('NekoStage: root not found');
    S.fps = Math.max(1, Math.min(60, opts.fps || 30));
    { const dq = detectQuality(); applyQuality(dq.tier); S.qualityWhy = dq.why; S._qw = null; S._qDown = 0; }
    S.hud = opts.hud !== false;
    S.subOn = opts.sub !== false;
    S.els = buildDom(S.root);

    const show = S.show;
    S.dur = +show.duration || 0;
    S.beat = 60 / (show.music.bpm || 100);
    S.bar = S.beat * 4;
    S.seed = (show.seed >>> 0) || 1;
    S.sections = (show.music.sections || []).slice().sort((a, b) => a.t0 - b.t0);
    S.downbeats = ((show.music && show.music.downbeats) || []).slice().sort((a, b) => a - b);
    S.shots = splitLongShots((show.camera || []).slice().sort((a, b) => a.t - b.t), show).map(sh => sh._split ? Object.assign(sh, { t: snapBar(sh.t) }) : sh);
    S.scenes = (show.scenes || []).slice().sort((a, b) => a.t0 - b.t0);
    // 第三十九轮：每次演出都不一样——编排（段落/歌词时间/镜头切点）不变，
    // 用每次新生成的「演出种子」重新抽：背景场景、背景转场、全屏转场、滤镜、边框、运镜、背景大字的位置等
    S.runSeed = (opts.runSeed >>> 0) || ((Math.random() * 4294967295) >>> 0) || 1;
    S.seed = ((S.seed ^ S.runSeed) >>> 0) || 1;
    show.seed = S.seed;
    // 第四十四轮：美术指导——用户在工作室选定风格(show.style.chosen)就整首一个主题；否则融合（主歌/副歌/桥段各一个主题，复现时一致）
    S.art = global.NekoStageArt ? global.NekoStageArt.create(show, { seed: S.runSeed, fused: !(show.style && show.style.chosen) }) : null;
    global.__NEKO_ART = S.art;
    S.scenes = mergeShortScenes(S.art ? artScenes(mergeShortScenes(S.scenes), S.art) : remixScenes(S.scenes, show, S.runSeed));
    show.scenes = S.scenes;   // 背景层、观感层都从 show.scenes 读——换成这次演出的版本
    S.gaps = global.NekoStageLines ? global.NekoStageLines.findGaps(show, 7) : [];
    // 第五十一轮：手书分镜（人物不是主角）。默认开；show.style.director === false 时回到原来的舞台
    S.dirPlan = null; S._figA = null; S._dirHide = null; S._dirShot = null; S.accPlan = null;
    const modeWant = String((show.style || {}).mode || '');
    // 第五十三轮：两种演出模式分开——默认是「原舞台」（人物在场上），手书分镜要在面板里选了才用
    const useDir = modeWant === 'tegaki';
    if (global.NekoStageDirector && useDir && (show.lines || []).length >= 4) {
      try {
        const tf0 = (opts.fonts || {}), fontD = tf0.accentStack || tf0.primaryStack || '"Microsoft YaHei","PingFang SC",sans-serif';
        S.dirPlan = global.NekoStageDirector.plan(show, S.runSeed, { title: stageTitle(), font: fontD });
        global.__NEKO_DIR = S.dirPlan;
      } catch (e) { recordErr('director-plan', e); S.dirPlan = null; }
    }
    // 第五十三轮：原舞台融合一点手书色块（只在画面边缘动，不压人物、不画边框和字）
    // 第五十三轮（用户改主意）：两种模式完全分开，原舞台不混入任何手书元素。只有显式 style.accents === true 才开（默认关）
    if (!S.dirPlan && global.NekoStageDirector && global.NekoStageDirector.accentPlan && (show.style || {}).accents === true) {
      try { S.accPlan = global.NekoStageDirector.accentPlan(show, S.runSeed); } catch (e) { recordErr('accent-plan', e); S.accPlan = null; }
    }
    S.stingers = global.NekoStageStinger ? global.NekoStageStinger.plan(show, S.seed || 0) : [];
    // 第四十九轮：全屏转场和背景切换对齐——转场全遮住的那一刻正好换背景（不再遮一半时背景还在慢慢转，露出「切一半」）。
    // 离转场 1.5 s 内的场景边界吸到转场时刻，并标记 stingerCut（被遮住时直接换，不做背景自己的过渡）
    for (const st of S.stingers) {
      let best = -1, bd = 1.5;
      for (let i = 1; i < S.scenes.length; i++) { const d = Math.abs(S.scenes[i].t0 - st.t); if (d < bd) { bd = d; best = i; } }
      if (best > 0) { S.scenes[best].t0 = st.t; S.scenes[best - 1].t1 = st.t; S.scenes[best].stingerCut = true; }
    }
    show.scenes = S.scenes;
    try { S.look = global.NekoStageLook ? global.NekoStageLook.create(S.els.wrap, show, {}) : null; } catch (e) { recordErr('look', e); S.look = null; }
    if (S.look) S.look.resize(S.W, S.H);
    S.events = (show.events || []).slice().sort((a, b) => a.t - b.t);
    // 测量状态随新曲目清空（只影响 blinkLog 的去重与记录，不影响渲染）；停播后日志保留可读
    S._blinkSeenSet = null; DIAG.blinkLog = []; DIAG.blinkTotal = 0; DIAG.blinkEnd = 0;
    S.preset = PRESETS[show.style.preset] || PRESETS.pop;

    S.cfg = resolveCfg(show, opts.fonts || null);
    const skin = global.NekoStageSkins ? global.NekoStageSkins.resolve((show.style || {}).skin) : null;
    S.skin = skin;
    // 皮肤类挂在 ns-root 上（与 skins.css 的选择器一致）；重开时先清掉上一次的
    const skinIds = global.NekoStageSkins ? Object.keys(global.NekoStageSkins.SKINS) : [];
    S.root.classList.remove('ns-light', 'ns-deco-outline', 'ns-line-paper', 'ns-line-ink', 'ns-director');
    if (S.dirPlan) S.root.classList.add('ns-director');
    S._lineCls = ''; S._camSm = null;
    skinIds.forEach(id => S.root.classList.remove('ns-skin-' + id));
    if (skin) {
      S.root.classList.add('ns-skin-' + skin.id);
      if (skin.light) S.root.classList.add('ns-light');
    }
    S._skinId = skin ? skin.id : null;
    if (S.cfg.decor.indexOf('outline') >= 0) S.root.classList.add('ns-deco-outline');
    // 调色板写到 CSS 变量
    const pal = show.style.palette || {};
    for (const k of ['ink', 'ink2', 'hot', 'bgA', 'bgB', 'c1', 'c2', 'c3']) {
      if (pal[k]) S.els.wrap.style.setProperty('--' + k, pal[k]);
    }

    S.lyrics = Lyrics.create();
    S.lyrics.build(show, { back: S.els.back, front: S.els.front, sub: S.els.sub }, S.cfg);
    applySize();
    bakePose();

    if (opts.model) setModel(opts.model, opts.container);

    S.els.hud.style.display = S.hud ? 'block' : 'none';
    S.clock = opts.clock || null;
    S.playing = opts.clock ? true : !!opts.autoplay;
    S.pausedT = opts.t0 || 0;
    S.show0 = S.pausedT; S.wall0 = performance.now();
    S.dirty = true;
    S.lastPaint = 0;
    S.stats = []; S.statsOn = !!opts.stats;
    renderAt(S.playing ? currentT() : S.pausedT);

    const onResize = () => applySize();
    const onVis = () => { S.lastPaint = 0; S.dirty = true; };
    global.addEventListener('resize', onResize);
    document.addEventListener('visibilitychange', onVis);
    S._off = () => {
      global.removeEventListener('resize', onResize);
      document.removeEventListener('visibilitychange', onVis);
    };
    S.raf = requestAnimationFrame(loop);
    return api;
  }

  function stop() {
    if (S.raf) cancelAnimationFrame(S.raf);
    try { if (S.look) S.look.destroy(); } catch (e) { }
    S.look = null;
    try { const cont = document.getElementById('live2d-container'); if (cont) { cont.style.filter = ''; cont.style.clipPath = ''; cont._clip = ''; cont._stick = ''; if (S.dirPlan) cont.style.removeProperty('opacity'); } } catch (e) { }
    try { if (S.root) S.root.classList.remove('ns-director'); if (S.els && S.els.l2d) S.els.l2d.style.filter = ''; } catch (e) { }
    try { if (S.els && S.els.l2d) { S.els.l2d.style.opacity = ''; S.els.l2d.style.clipPath = ''; } } catch (e) { }
    S.dirPlan = null;
    S.raf = 0;
    if (S._off) { S._off(); S._off = null; }
    unbindModel();
    if (S.els && S.els.wrap && S.els.wrap.parentNode) S.els.wrap.parentNode.removeChild(S.els.wrap);
    if (S.root) S.root.classList.remove('ns-root');
    S.els = null; S.lyrics = null; S.show = null; S.poseOut = null; S.poseTrack = null;
    S.playing = false;
    S.playing = false;
  }

  const api = {
    start, stop, renderAt, setModel, loadFonts,
    get skin() { return S.skin || null; },
    get fonts() { return S.cfg ? S.cfg.fontInfo : null; },
    get canvas() { return S.els ? S.els.l2d : null; },
    get rootEl() { return S.root; },
    get duration() { return S.dur; },
    get playing() { return S.playing; },
    get t() { return S.dur ? currentT() : 0; },
    currentT,
    play() {
      if (S.clock) { S.clock.play && S.clock.play(); return; }
      if (S.playing) return;
      S.show0 = S.pausedT; S.wall0 = performance.now(); S.playing = true; S.dirty = true;
    },
    pause() {
      if (S.clock) { S.clock.pause && S.clock.pause(); return; }
      if (!S.playing) return;
      S.pausedT = currentT(); S.playing = false;
    },
    seek(t) {
      if (S.clock) { S.clock.seek && S.clock.seek(t); return; }
      S.pausedT = U.clamp(+t || 0, 0, S.dur);
      if (S.playing) { S.show0 = S.pausedT; S.wall0 = performance.now(); }
      S.dirty = true;
      renderAt(S.pausedT);
    },
    setHud(on) { S.hud = !!on; if (S.els) S.els.hud.style.display = S.hud ? 'block' : 'none'; S.dirty = true; },
    setSub(on) { S.subOn = !!on; S.dirty = true; },
    setFps(n) { S.fps = Math.max(1, Math.min(60, +n || 30)); },
    info() { return S.lastInfo; },
    // P6 第六轮：实时副本（不依赖渲染帧刷新的 lastInfo 快照）
    // 第三十五轮诊断：某时刻规划器用的人物占用格（字符串画）
    debugOcc(t) {
      const o = occAtCam(cameraAt(+t || 0));
      if (!o) return null;
      const rows = [];
      for (let y = 0; y < o.h; y++) { let r = ''; for (let x = 0; x < o.w; x++) r += o.g[y * o.w + x] ? '#' : '.'; rows.push(r); }
      return rows.join(String.fromCharCode(10));
    },
    diagLive() { return Object.assign({}, DIAG, { wrapCalls: DIAG.wrapCalls }); },
    // 只用于视觉对照：关闭时不再写眼球（宿主 bridge 会回默认值）
    setEyeWriteOff(v) { S.eyeWriteOff = !!v; return S.eyeWriteOff; },
    stats() {
      const a = S.stats.slice().sort((x, y) => x - y);
      const sum = a.reduce((x, y) => x + y, 0);
      return {
        n: a.length, avg: a.length ? sum / a.length : 0,
        p50: a.length ? a[Math.floor(a.length * .5)] : 0,
        p95: a.length ? a[Math.floor(a.length * .95)] : 0,
        max: a.length ? a[a.length - 1] : 0,
        bgW: S.BW, bgH: S.BH, W: S.W, H: S.H, fps: S.fps, quality: S.quality, qualityWhy: S.qualityWhy, qualityDown: S._qDown || 0
      };
    },
    clearStats() { S.stats = []; },
    enableStats(on) { S.statsOn = !!on; }
  };
  S.api = api;
  global.NekoStage = api;
})(window);
