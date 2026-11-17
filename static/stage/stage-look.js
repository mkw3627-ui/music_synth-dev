/* =============================================================================
 * 「为她而唱」舞台 · 整体观感层（第三十七轮建，第三十八轮扩展）
 * 参考（r37_refs）：B 站 VTuber 歌回 Shoost/Folia 滤镜预设（绿意蒙版+前景虚化+背后大字、
 *   樱花地铁电影黑边+柔光、复古影院放映框、偶像舞台背光+多色辉光）
 *
 * 第三十八轮：
 *   · 滤镜改成「动态」：每个场景段换一套滤镜（12 套），强度按段落走——
 *     主歌/前奏低（整体比上一轮降一档，避免过曝），副歌/最终副歌升高；切段时 1.2 s 平滑过渡。
 *   · 边框扩到 12 种，每首按风格候选 + 种子挑一种；每个大段（副歌）可换成同风格的第二种。
 *   · 蒙版层扩展：暗角、胶片颗粒+划痕、扫描线、光线束（god rays）、棱镜色散边、
 *     柔焦雾、彩色对角渐变、星点闪光、网点纹、心形/星形遮罩边角，按滤镜套装组合。
 * 只由 t 与 show 决定；所有画布低分辨率 + CSS 拉伸，开销小。
 * ========================================================================== */
(function (global) {
  'use strict';
  const U = global.NekoStageU;
  if (!U) return;
  const h = U.h01;

  // 风格 → 基础观感（边框候选、大字字体、人物辉光色）
  const LOOKS = {
    ballad:  { frames: ['cinema', 'polaroid', 'film', 'lace'],      glow: '#ffe6f2', ghostFont: 'serif', lines: 'tri' },
    jazz:    { frames: ['theater', 'film', 'gold', 'cinema'],       glow: '#ffd9a0', ghostFont: 'serif', lines: 'none' },
    ancient: { frames: ['scroll', 'corner', 'gold', 'lace'],        glow: '#fff1d6', ghostFont: 'serif', lines: 'none' },
    pop:     { frames: ['screen', 'sticker', 'corner', 'polaroid'], glow: '#ffffff', ghostFont: 'sans',  lines: 'slash' },
    cute:    { frames: ['sticker', 'lace', 'corner', 'polaroid'],   glow: '#ffeaf6', ghostFont: 'sans',  lines: 'slash' },
    edm:     { frames: ['screen', 'hud', 'neonbar', 'vhs'],         glow: '#9ee8ff', ghostFont: 'sans',  lines: 'tri' },
    rock:    { frames: ['cinema', 'vhs', 'neonbar', 'hud'],         glow: '#ffd2b8', ghostFont: 'sans',  lines: 'slash' },
    dark:    { frames: ['theater', 'film', 'vhs', 'gold'],          glow: '#d6c2ff', ghostFont: 'serif', lines: 'tri' }
  };

  // 滤镜套装（每段一套）：grade 调色两端色 + 强度；leak 漏光；masks 蒙版组合
  // 强度 base 是主歌值（比第三十七轮低约 30%），副歌 ×climax
  const FILTERS = {
    softpink:  { grade: ['#ffd6ea', '#b8d4ff'], gA: .13, leak: '#ffcfe0', leakA: .12, climax: 1.6, masks: ['vignette', 'haze', 'sparkle'] },
    dreamblue: { grade: ['#cfe6ff', '#8a9cff'], gA: .14, leak: '#bfe0ff', leakA: .12, climax: 1.5, masks: ['vignette', 'haze', 'prism'] },
    golden:    { grade: ['#ffe2a8', '#8a5a7a'], gA: .15, leak: '#ffc874', leakA: .14, climax: 1.5, masks: ['vignette', 'rays', 'grain'] },
    sunset:    { grade: ['#ffb38a', '#6a4aa8'], gA: .15, leak: '#ff9a7a', leakA: .13, climax: 1.5, masks: ['vignette', 'rays'] },
    mint:      { grade: ['#d6ffe8', '#6ab8c8'], gA: .12, leak: '#c8ffe0', leakA: .1,  climax: 1.6, masks: ['vignette', 'haze', 'sparkle'] },
    neonpop:   { grade: ['#ff7ad9', '#3ad6ff'], gA: .14, leak: '#c27aff', leakA: .12, climax: 1.7, masks: ['vignette', 'scan', 'prism'] },
    cyber:     { grade: ['#00e5ff', '#7a2cff'], gA: .15, leak: '#5a7aff', leakA: .1,  climax: 1.6, masks: ['scan', 'prism', 'vignette'] },
    noir:      { grade: ['#d0d0e0', '#1a1a2a'], gA: .16, leak: '#ffffff', leakA: .06, climax: 1.4, masks: ['vignette', 'grain', 'scratch'] },
    vintage:   { grade: ['#f2d9a8', '#5a4a3a'], gA: .16, leak: '#ffd9a0', leakA: .1,  climax: 1.4, masks: ['vignette', 'grain', 'scratch'] },
    sakura:    { grade: ['#ffd1e1', '#d6b8ff'], gA: .13, leak: '#ffe1ec', leakA: .12, climax: 1.6, masks: ['vignette', 'haze', 'halftone'] },
    crimson:   { grade: ['#ff8a8a', '#2a1a4a'], gA: .15, leak: '#ff6a6a', leakA: .1,  climax: 1.6, masks: ['vignette', 'rays', 'scan'] },
    starlit:   { grade: ['#e6e0ff', '#2a3a7a'], gA: .14, leak: '#d6d0ff', leakA: .1,  climax: 1.6, masks: ['vignette', 'sparkle', 'haze'] },
    // 第五十轮：线系主题的「无滤镜」——不调色、不漏光、不叠蒙版，画面只有线和留白
    plain:     { grade: ['#808080', '#808080'], gA: 0, leak: '#ffffff', leakA: 0, climax: 1, masks: [], px: { sat: 1 } },
    // 第四十五轮：20 套虚拟主播歌回 / 3D LIVE / 歌ってみた MV 常见的观感（参考 YouTube「虚拟主播 唱歌 3D live」「歌枠 歌ってみた」检索结果）
    // tone：整体去色/褪色（mono 黑白 MV、sepia 回忆）；px：给人物本身的 GPU 滤镜（pixi-filters）参数
    holo_idol:     { grade: ['#ffb3f0', '#7af0ff'], gA: .14, leak: '#e0b0ff', leakA: .12, climax: 1.6, masks: ['vignette', 'chroma', 'starburst'], px: { sat: 1.12, bloom: .5, rgb: 1 } },
    karaoke_night: { grade: ['#b38cff', '#1a0f3a'], gA: .15, leak: '#9a7aff', leakA: .1,  climax: 1.6, masks: ['vignette', 'spot', 'dust'], px: { sat: 1.05, bloom: .45 } },
    milk_pastel:   { grade: ['#fff2f8', '#d8e8ff'], gA: .12, leak: '#fff0f6', leakA: .12, climax: 1.4, masks: ['milk', 'sparkle'], px: { sat: .95, bright: 1.04, bloom: .3 } },
    dream_bloom:   { grade: ['#ffe8ff', '#a8c8ff'], gA: .13, leak: '#ffd8f4', leakA: .14, climax: 1.5, masks: ['bloom', 'haze', 'sparkle'], px: { bright: 1.03, bloom: .7 } },
    vhs_tape:      { grade: ['#ffd0a0', '#204060'], gA: .16, leak: '#ffb080', leakA: .08, climax: 1.4, masks: ['scan', 'chroma', 'vhsbar', 'grain'], px: { sat: .9, rgb: 1.5 } },
    crt_arcade:    { grade: ['#7affb0', '#1a2a40'], gA: .15, leak: '#7affd0', leakA: .08, climax: 1.5, masks: ['crt', 'scan', 'vignette'], px: { sat: 1.1, rgb: .8 } },
    lofi_room:     { grade: ['#ffd9b0', '#4a5a7a'], gA: .15, leak: '#ffcf9a', leakA: .1,  climax: 1.3, masks: ['grain', 'dust', 'vignette'], px: { sat: .9, contrast: .96 } },
    anime_ed:      { grade: ['#d8ecff', '#ffd8e8'], gA: .12, leak: '#ffffff', leakA: .1,  climax: 1.4, masks: ['haze', 'lensflare'], px: { sat: .92, bright: 1.03, bloom: .35 } },
    summer_soda:   { grade: ['#a8f0ff', '#fff2a8'], gA: .13, leak: '#c8faff', leakA: .13, climax: 1.6, masks: ['lensflare', 'sparkle', 'vignette'], px: { sat: 1.12, bright: 1.03, bloom: .4 } },
    film_burn:     { grade: ['#ffb070', '#3a1a2a'], gA: .16, leak: '#ff9050', leakA: .12, climax: 1.5, masks: ['burn', 'grain', 'scratch'], px: { sat: .95, contrast: 1.05 } },
    moonlight:     { grade: ['#c8d8ff', '#101a3a'], gA: .16, leak: '#b8ccff', leakA: .1,  climax: 1.5, masks: ['vignette', 'moonrays', 'dust'], px: { sat: .9, blue: 1.06, bloom: .4 } },
    cyber_glitch:  { grade: ['#ff2fd0', '#00f0ff'], gA: .15, leak: '#8a5aff', leakA: .1,  climax: 1.7, masks: ['chroma', 'scan', 'glitchbar'], px: { sat: 1.15, rgb: 2.2 } },
    golden_hour:   { grade: ['#ffd28a', '#ff7aa8'], gA: .15, leak: '#ffc070', leakA: .15, climax: 1.5, masks: ['lensflare', 'rays', 'dust'], px: { sat: 1.05, red: 1.05, bloom: .45 } },
    aqua_dream:    { grade: ['#9ff0ff', '#3a6aff'], gA: .14, leak: '#9fe8ff', leakA: .12, climax: 1.5, masks: ['caustic', 'haze', 'vignette'], px: { blue: 1.05, bloom: .4 } },
    mono_mv:       { grade: ['#ffffff', '#202020'], gA: .16, leak: '#ffffff', leakA: .05, climax: 1.3, masks: ['vignette', 'grain'], tone: ['mono', .85], px: { sat: .15, contrast: 1.08 } },
    spring_fade:   { grade: ['#ffd6e6', '#ffe9c8'], gA: .12, leak: '#ffe0ec', leakA: .13, climax: 1.5, masks: ['petalsoft', 'haze'], px: { sat: 1.02, bright: 1.03, bloom: .35 } },
    neon_rain:     { grade: ['#ff4fa8', '#3ad0ff'], gA: .15, leak: '#c060ff', leakA: .1,  climax: 1.6, masks: ['rainstreak', 'chroma', 'vignette'], px: { sat: 1.1, rgb: 1 } },
    star_concert:  { grade: ['#fff0b8', '#5a3aa8'], gA: .14, leak: '#fff0c0', leakA: .12, climax: 1.7, masks: ['starburst', 'spot', 'sparkle'], px: { sat: 1.08, bloom: .6 } },
    sepia_memory:  { grade: ['#f0d8b0', '#6a4a3a'], gA: .15, leak: '#ffd9a0', leakA: .1,  climax: 1.3, masks: ['grain', 'vignette', 'scratch'], tone: ['sepia', .55], px: { sat: .6, red: 1.04 } },
    ice_crystal:   { grade: ['#e0f6ff', '#8ab0ff'], gA: .13, leak: '#e0f4ff', leakA: .12, climax: 1.5, masks: ['frost', 'sparkle', 'vignette'], px: { sat: .95, blue: 1.05, bloom: .45 } }
  };
  const FILTERS_BY_PRESET = {
    ballad:  ['softpink', 'dreamblue', 'starlit', 'golden', 'sakura'],
    jazz:    ['golden', 'vintage', 'noir', 'sunset'],
    ancient: ['vintage', 'mint', 'sakura', 'golden'],
    pop:     ['softpink', 'neonpop', 'mint', 'sakura', 'dreamblue'],
    cute:    ['sakura', 'softpink', 'mint', 'dreamblue'],
    edm:     ['cyber', 'neonpop', 'starlit', 'crimson'],
    rock:    ['crimson', 'sunset', 'noir', 'cyber'],
    dark:    ['noir', 'crimson', 'starlit', 'cyber']
  };

  function create(wrap, show, opts) {
    const preset = (show.style && show.style.preset) || 'pop';
    const L = Object.assign({}, LOOKS.pop, LOOKS[preset] || {});
    const pal = (show.style && show.style.palette) || {};
    const seed = (show.seed >>> 0) || 1;
    // 每个场景段一套滤镜：按种子从风格候选里轮换（相邻段不同）
    const fcand = FILTERS_BY_PRESET[preset] || FILTERS_BY_PRESET.pop;
    const scenes = (show.scenes || []).slice().sort((a, b) => a.t0 - b.t0);
    // 第三十九轮：滤镜不是固定套装——每段从候选里抽一套（相邻不同），再把数值随机拼一点（强度、色相偏移、蒙版取舍）
    const allF = Object.keys(FILTERS);
    const segFilter = [], segVar = [];
    const ART = global.__NEKO_ART;
    scenes.forEach((sc, i) => {
      if (ART) { segFilter.push(ART.filterFor(sc.section)); segVar.push({ gk: 1, lk: 1, drop: 99 }); return; }
      const pool = (h(seed * .0019 + i * 7.1) < .75 ? fcand : allF).filter(n => n !== segFilter[i - 1]);
      segFilter.push(pool[Math.floor(h(seed * .0013 + i * 3.3) * pool.length)] || fcand[0]);
      segVar.push({ gk: .8 + .4 * h(seed * .0029 + i), lk: .75 + .5 * h(seed * .0031 + i * 2), drop: Math.floor(h(seed * .0037 + i * 5) * 4) });
    });
    // 边框：风格候选里随机两种（主歌/副歌），偶尔从其它风格借一种
    const allFrames = ['cinema', 'theater', 'corner', 'screen', 'polaroid', 'film', 'lace', 'gold', 'sticker', 'hud', 'neonbar', 'vhs', 'scroll'];
    const f0 = Math.floor(h(seed * .0021) * L.frames.length);
    // 第四十二轮：边框池 = 风格候选（原生 13 种里的 4 种）+ 扩展包 24 种（参数化，长相每次都不同）+ 素材库进化出的边框基因
    const FX = global.NekoStageFrames;
    const evolved = (global.NekoStageFramesEvolved || []).filter(g => !g.genres || g.genres.includes(preset));
    const extKinds = FX ? FX.FRAME_KINDS : [];
    const roll = h(seed * .0051);
    let frameMain = L.frames[f0], frameGenome = null;
    if (FX && evolved.length && roll < .3) { frameGenome = evolved[Math.floor(h(seed * .0057) * evolved.length)]; frameMain = 'ext'; }
    else if (FX && roll < .8) { frameGenome = FX.genome(extKinds[Math.floor(h(seed * .0061) * extKinds.length)], seed, pal); frameMain = 'ext'; }
    if (ART && ART.frame) {
      if (ART.frame.ext && FX) { frameGenome = FX.genome(ART.frame.ext, seed, pal); frameMain = 'ext'; }
      else if (ART.frame.kind) { frameMain = ART.frame.kind; frameGenome = null; }
    }
    const frameAlt = h(seed * .0023) < .8 ? L.frames[(f0 + 1 + Math.floor(h(seed * .0027) * (L.frames.length - 1))) % L.frames.length]
      : allFrames.filter(x => x !== frameMain)[Math.floor(h(seed * .0041) * 12)];

    const sub = wrap.querySelector('.ns-sub');
    const mk = (tag, cls, css) => { const e = document.createElement(tag); e.className = cls; e.style.cssText = css; wrap.insertBefore(e, sub); return e; };
    const base = 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none;';
    const els = {
      grade: mk('div', 'ns-look-grade', base + 'z-index:7;mix-blend-mode:soft-light;'),
      leak: mk('canvas', 'ns-look-leak', base + 'z-index:7;mix-blend-mode:screen;filter:blur(18px);'),
      lines: mk('canvas', 'ns-look-lines', base + 'z-index:6;opacity:.45;'),
      bokeh: mk('canvas', 'ns-look-bokeh', base + 'z-index:7;filter:blur(5px);'),
      mask: mk('canvas', 'ns-look-mask', base + 'z-index:7;'),
      haze: mk('div', 'ns-look-haze', base + 'z-index:7;backdrop-filter:blur(2.5px);-webkit-backdrop-filter:blur(2.5px);' +
        '-webkit-mask-image:radial-gradient(ellipse 60% 70% at 50% 45%,transparent 55%,#000 100%);mask-image:radial-gradient(ellipse 60% 70% at 50% 45%,transparent 55%,#000 100%);'),
      tiltT: mk('div', 'ns-look-tilt', 'position:absolute;left:0;right:0;top:0;height:14%;pointer-events:none;z-index:7;' +
        'backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);' +
        '-webkit-mask-image:linear-gradient(to bottom,#000,transparent);mask-image:linear-gradient(to bottom,#000,transparent);'),
      tiltB: mk('div', 'ns-look-tilt', 'position:absolute;left:0;right:0;bottom:0;height:12%;pointer-events:none;z-index:7;' +
        'backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);' +
        '-webkit-mask-image:linear-gradient(to top,#000,transparent);mask-image:linear-gradient(to top,#000,transparent);'),
      frame: mk('canvas', 'ns-look-frame', base + 'z-index:8;'),
      frame2: mk('canvas', 'ns-look-frame', base + 'z-index:8;opacity:0;'),
      grade2: mk('div', 'ns-look-grade', base + 'z-index:7;mix-blend-mode:soft-light;opacity:0;'),
      // 第四十五轮：整体色调层（黑白 MV 用 saturation 混合去色；回忆用 color 混合染成棕褐）
      tone: mk('div', 'ns-look-tone', base + 'z-index:7;opacity:0;')
    };
    // 背后大字：放在后排歌词层（人物之后）
    const back = wrap.querySelector('.ns-lyback');
    const ghost = document.createElement('div');
    ghost.className = 'ns-look-ghost';
    ghost.style.cssText = 'position:absolute;z-index:2;left:0;right:0;top:12%;text-align:center;white-space:nowrap;pointer-events:none;' +
      'font-weight:900;letter-spacing:-.04em;line-height:1;color:rgba(255,255,255,.0);' +
      '-webkit-text-stroke:2px rgba(255,255,255,.26);filter:blur(1.2px);transition:none;' +
      'font-family:' + (L.ghostFont === 'serif' ? '"Noto Serif SC","Source Han Serif SC","Songti SC",serif' : '"Noto Sans SC","Microsoft YaHei",sans-serif') + ';';
    if (back && back.parentNode) back.parentNode.insertBefore(ghost, back);
    let W = 0, H = 0, frameDrawn = '';
    const ctx = k => els[k].getContext('2d');
    const ctxLeak = ctx('leak'), ctxLines = ctx('lines'), ctxBokeh = ctx('bokeh'), ctxFrame = ctx('frame'), ctxMask = ctx('mask');

    function resize(w, hh) {
      W = w; H = hh;
      els.leak.width = Math.round(W / 8); els.leak.height = Math.round(H / 8);
      els.lines.width = Math.round(W / 2); els.lines.height = Math.round(H / 2);
      els.bokeh.width = Math.round(W / 4); els.bokeh.height = Math.round(H / 4);
      els.mask.width = Math.round(W / 2); els.mask.height = Math.round(H / 2);
      els.frame.width = W; els.frame.height = H;      // 边框按满分辨率画（文字、细线要清楚）
      els.frame2.width = W; els.frame2.height = H;
      frameDrawn = '';
      ghost.style.fontSize = Math.round(H * .3) + 'px';
    }

    /* ------------------------------ 边框（12 种） ------------------------------ */
    function holeFill(c, w, hh, color, x, y, iw, ih, r) {
      c.save(); c.fillStyle = color; c.fillRect(0, 0, w, hh);
      // 第四十五轮修：挖洞要用不透明色——以前沿用半透明边框色，只挖掉一半，整个画面中间蒙一层灰（screen/拍立得等边框发灰的根因）
      c.globalCompositeOperation = 'destination-out'; c.fillStyle = '#000'; roundRectPath(c, x, y, iw, ih, r); c.fill(); c.restore();
    }
    function roundRectPath(c, x, y, w, hh, r) {
      c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + hh, r); c.arcTo(x + w, y + hh, x, y + hh, r);
      c.arcTo(x, y + hh, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath();
    }
    function drawFrame(kind, title, cv) {
      cv = cv || els.frame;
      const c = cv.getContext('2d'), w = cv.width, hh = cv.height;
      c.clearRect(0, 0, w, hh);
      if (kind === 'none') return;
      if ((kind === 'ext' || String(kind).indexOf('ext:') === 0) && frameGenome && global.NekoStageFrames) { global.NekoStageFrames.drawFrame(c, w, hh, frameGenome, title); return; }
      const acc = pal.hot || pal.c1 || '#ffffff', acc2 = pal.c2 || '#9fe0ff';
      const fs = n => Math.round(hh * n);
      const sans = '"Noto Sans SC","Microsoft YaHei",sans-serif', serif = '"Noto Serif SC","Songti SC",serif';
      c.save();
      if (kind === 'cinema') {
        const bar = hh * .085;
        c.fillStyle = 'rgba(0,0,0,.92)'; c.fillRect(0, 0, w, bar); c.fillRect(0, hh - bar, w, bar);
        c.fillStyle = 'rgba(255,255,255,.85)'; c.font = '600 ' + fs(.026) + 'px ' + sans;
        c.textBaseline = 'middle'; c.textAlign = 'right';
        if (title) c.fillText('♪ ' + title, w - bar * .5, bar * .5);
        c.textAlign = 'left'; c.fillStyle = 'rgba(255,255,255,.55)'; c.font = fs(.02) + 'px ' + sans;
        c.fillText('LIVE  ·  为她而唱', bar * .5, hh - bar * .5);
        c.fillStyle = U.rgba(acc, .9); c.fillRect(bar * .5, hh - bar * .5 + bar * .22, w * .06, 2);
      } else if (kind === 'theater') {
        const side = w * .045;
        for (const right of [0, 1]) {
          const g = c.createLinearGradient(right ? w : 0, 0, right ? w - side : side, 0);
          g.addColorStop(0, '#2a0508'); g.addColorStop(1, 'rgba(90,10,20,0)');
          c.fillStyle = g; c.fillRect(right ? w - side : 0, 0, side, hh);
        }
        c.fillStyle = '#3a0a10';
        const n = 14, sw = w / n;
        c.beginPath(); c.moveTo(0, 0); c.lineTo(w, 0); c.lineTo(w, hh * .03);
        for (let i = n; i > 0; i--) c.quadraticCurveTo((i - .5) * sw, hh * .075, (i - 1) * sw, hh * .03);
        c.closePath(); c.fill();
        c.fillStyle = 'rgba(255,215,150,.85)';
        for (let i = 0; i < n; i++) { c.beginPath(); c.arc((i + .5) * sw, hh * .052, hh * .004, 0, 6.283); c.fill(); }
        const bh = hh * .06;
        c.fillStyle = 'rgba(8,4,6,.75)'; c.fillRect(0, hh - bh, w, bh);
        c.fillStyle = 'rgba(255,225,190,.85)'; c.textAlign = 'center'; c.textBaseline = 'middle';
        c.font = fs(.024) + 'px ' + serif;
        if (title) c.fillText('— ' + title + ' —', w / 2, hh - bh / 2);
      } else if (kind === 'corner') {
        const m = w * .025, L2 = w * .07;
        c.strokeStyle = 'rgba(255,255,255,.88)'; c.lineWidth = Math.max(2, hh * .003);
        const corner = (x, y, dx, dy) => { c.beginPath(); c.moveTo(x + dx * L2, y); c.lineTo(x, y); c.lineTo(x, y + dy * L2); c.stroke(); };
        corner(m, m, 1, 1); corner(w - m, m, -1, 1); corner(m, hh - m, 1, -1); corner(w - m, hh - m, -1, -1);
        c.strokeStyle = U.rgba(acc, .4); c.lineWidth = 1;
        c.strokeRect(m + 10, m + 10, w - 2 * m - 20, hh - 2 * m - 20);
        c.fillStyle = 'rgba(255,255,255,.92)'; c.font = '600 ' + fs(.022) + 'px ' + sans;
        c.textAlign = 'left'; c.textBaseline = 'top'; if (title) c.fillText('✦ ' + title, m + 22, m + 18);
      } else if (kind === 'screen') {
        const m = w * .018, r = hh * .05;
        holeFill(c, w, hh, 'rgba(0,0,0,.5)', m, m, w - 2 * m, hh - 2 * m, r);
        c.shadowColor = acc; c.shadowBlur = hh * .015; c.strokeStyle = U.rgba(acc, .9); c.lineWidth = Math.max(2.5, hh * .003);
        roundRectPath(c, m, m, w - 2 * m, hh - 2 * m, r); c.stroke();
        c.shadowBlur = 0; c.fillStyle = 'rgba(255,255,255,.92)'; c.font = '700 ' + fs(.02) + 'px ' + sans;
        c.textAlign = 'right'; c.textBaseline = 'top'; c.fillText('● LIVE' + (title ? '  ' + title : ''), w - m - r, m + hh * .015);
      } else if (kind === 'polaroid') {
        // 拍立得：白色厚边，底边更宽写歌名，略微倾斜的胶带
        const m = hh * .03, mb = hh * .1;
        holeFill(c, w, hh, 'rgba(252,250,246,.96)', m, m, w - 2 * m, hh - m - mb, 2);
        c.fillStyle = '#3a3040'; c.font = fs(.032) + 'px "Segoe Print","Comic Sans MS",' + sans; c.textAlign = 'left'; c.textBaseline = 'middle';
        if (title) c.fillText(title, m * 2, hh - mb / 2);
        c.fillStyle = U.rgba(acc, .9); c.font = fs(.026) + 'px ' + sans; c.textAlign = 'right'; c.fillText('♡', w - m * 2, hh - mb / 2);
        c.save(); c.translate(w * .5, m * .9); c.rotate(-.04);
        c.fillStyle = 'rgba(255,220,235,.75)'; c.fillRect(-w * .06, -hh * .02, w * .12, hh * .04); c.restore();
      } else if (kind === 'film') {
        // 胶片：上下齿孔条
        const bh = hh * .07;
        c.fillStyle = 'rgba(10,8,8,.95)'; c.fillRect(0, 0, w, bh); c.fillRect(0, hh - bh, w, bh);
        c.fillStyle = 'rgba(255,240,220,.85)';
        const hw = bh * .45, gap = bh * .9;
        for (let x = gap / 2; x < w; x += gap) { roundRectPath(c, x, bh * .25, hw, bh * .5, 3); c.fill(); roundRectPath(c, x, hh - bh * .75, hw, bh * .5, 3); c.fill(); }
        c.fillStyle = 'rgba(255,200,120,.75)'; c.font = fs(.016) + 'px "Consolas",monospace'; c.textAlign = 'left'; c.textBaseline = 'bottom';
        c.fillText('KODAK 400  ▸ ' + (title || ''), bh * .4, hh - bh - 4);
      } else if (kind === 'lace') {
        // 蕾丝花边：四周一圈扇贝小圆 + 点
        const m = hh * .028, r = hh * .016;
        c.fillStyle = 'rgba(255,255,255,.88)';
        for (let x = r; x < w; x += r * 2) { c.beginPath(); c.arc(x, m, r, 0, Math.PI); c.fill(); c.beginPath(); c.arc(x, hh - m, r, Math.PI, 6.283); c.fill(); }
        for (let y = r; y < hh; y += r * 2) { c.beginPath(); c.arc(m, y, r, -Math.PI / 2, Math.PI / 2); c.fill(); c.beginPath(); c.arc(w - m, y, r, Math.PI / 2, Math.PI * 1.5); c.fill(); }
        c.fillRect(0, 0, w, m - r * .2); c.fillRect(0, hh - m + r * .2, w, m); c.fillRect(0, 0, m - r * .2, hh); c.fillRect(w - m + r * .2, 0, m, hh);
        c.fillStyle = U.rgba(acc, .85); c.font = fs(.022) + 'px ' + serif; c.textAlign = 'center'; c.textBaseline = 'top';
        if (title) c.fillText('❀ ' + title + ' ❀', w / 2, m + r * 1.6);
      } else if (kind === 'gold') {
        // 金色细双线 + 角花
        const m = hh * .03;
        c.strokeStyle = 'rgba(232,196,120,.9)'; c.lineWidth = 2; c.strokeRect(m, m, w - 2 * m, hh - 2 * m);
        c.lineWidth = 1; c.strokeRect(m + 8, m + 8, w - 2 * m - 16, hh - 2 * m - 16);
        c.fillStyle = 'rgba(232,196,120,.95)';
        for (const [x, y] of [[m, m], [w - m, m], [m, hh - m], [w - m, hh - m]]) { c.save(); c.translate(x, y); c.rotate(Math.PI / 4); c.fillRect(-hh * .012, -hh * .012, hh * .024, hh * .024); c.restore(); }
        c.font = fs(.022) + 'px ' + serif; c.textAlign = 'center'; c.textBaseline = 'middle';
        if (title) { const tw = c.measureText(title).width + hh * .04; c.fillStyle = 'rgba(10,8,12,.85)'; c.fillRect(w / 2 - tw / 2, m - hh * .02, tw, hh * .04); c.fillStyle = 'rgba(240,210,140,.95)'; c.fillText(title, w / 2, m); }
      } else if (kind === 'sticker') {
        // 贴纸风：圆角粗白边 + 彩色描边 + 角落贴纸（星/心/音符）
        const m = hh * .025, r = hh * .06;
        holeFill(c, w, hh, 'rgba(255,255,255,.95)', m, m, w - 2 * m, hh - 2 * m, r);
        c.strokeStyle = U.rgba(acc, .95); c.lineWidth = Math.max(3, hh * .005); roundRectPath(c, m, m, w - 2 * m, hh - 2 * m, r); c.stroke();
        const deco = ['★', '♥', '♪', '✿'];
        c.font = '900 ' + fs(.05) + 'px ' + sans; c.textAlign = 'center'; c.textBaseline = 'middle';
        [[m * 1.6, m * 1.6], [w - m * 1.6, m * 1.6], [m * 1.6, hh - m * 1.6], [w - m * 1.6, hh - m * 1.6]].forEach(([x, y], i) => {
          c.lineWidth = hh * .008; c.strokeStyle = '#fff'; c.strokeText(deco[i], x, y); c.fillStyle = i % 2 ? acc : acc2; c.fillText(deco[i], x, y);
        });
        if (title) { c.font = '800 ' + fs(.024) + 'px ' + sans; c.lineWidth = hh * .007; c.strokeStyle = '#fff'; c.strokeText(title, w / 2, m * 1.2); c.fillStyle = acc; c.fillText(title, w / 2, m * 1.2); }
      } else if (kind === 'hud') {
        // 科技 HUD：四角折线 + 刻度 + 小字信息
        const m = w * .02;
        c.strokeStyle = U.rgba(acc2, .85); c.lineWidth = 2;
        const L3 = w * .05;
        for (const [x, y, dx, dy] of [[m, m, 1, 1], [w - m, m, -1, 1], [m, hh - m, 1, -1], [w - m, hh - m, -1, -1]]) {
          c.beginPath(); c.moveTo(x + dx * L3, y); c.lineTo(x + dx * 8, y); c.lineTo(x, y + dy * 8); c.lineTo(x, y + dy * L3); c.stroke();
        }
        c.lineWidth = 1;
        for (let i = 0; i < 30; i++) { const y = hh * .3 + i * hh * .013; c.beginPath(); c.moveTo(m, y); c.lineTo(m + (i % 5 ? 6 : 14), y); c.stroke(); }
        c.fillStyle = U.rgba(acc2, .9); c.font = fs(.016) + 'px "Consolas",monospace'; c.textAlign = 'left'; c.textBaseline = 'top';
        c.fillText('REC ● ' + (title || 'LIVE'), m + 20, m + 12);
        c.textAlign = 'right'; c.fillText('SYNC 100%  ·  4K', w - m - 20, m + 12);
      } else if (kind === 'neonbar') {
        // 霓虹条：左右两条竖霓虹灯管 + 顶部标题牌
        c.shadowColor = acc; c.shadowBlur = hh * .02;
        c.fillStyle = U.rgba(acc, .95); c.fillRect(w * .012, hh * .1, hh * .008, hh * .8);
        c.shadowColor = acc2; c.fillStyle = U.rgba(acc2, .95); c.fillRect(w - w * .012 - hh * .008, hh * .1, hh * .008, hh * .8);
        if (title) {
          c.shadowColor = acc; c.font = '800 ' + fs(.03) + 'px ' + sans; c.textAlign = 'center'; c.textBaseline = 'top';
          c.strokeStyle = U.rgba(acc, .95); c.lineWidth = 2; c.strokeText(title, w / 2, hh * .03);
        }
      } else if (kind === 'vhs') {
        // 录像带：左上 PLAY ▶、右下时间码、轻微黑边
        c.fillStyle = 'rgba(0,0,0,.6)'; c.fillRect(0, 0, w, hh * .02); c.fillRect(0, hh - hh * .02, w, hh * .02);
        c.font = '700 ' + fs(.03) + 'px "Consolas",monospace'; c.textBaseline = 'top'; c.textAlign = 'left';
        c.fillStyle = 'rgba(255,255,255,.9)'; c.fillText('PLAY ▶', w * .03, hh * .05);
        c.font = fs(.022) + 'px "Consolas",monospace'; c.fillText(title || '', w * .03, hh * .1);
      } else if (kind === 'scroll') {
        // 卷轴：上下深色木轴 + 米色纸边
        const bh = hh * .045;
        for (const y of [0, hh - bh]) {
          const g = c.createLinearGradient(0, y, 0, y + bh);
          g.addColorStop(0, '#3a2414'); g.addColorStop(.5, '#7a4a28'); g.addColorStop(1, '#2a180c');
          c.fillStyle = g; c.fillRect(0, y, w, bh);
        }
        c.fillStyle = 'rgba(240,226,196,.55)'; c.fillRect(0, bh, w * .02, hh - 2 * bh); c.fillRect(w * .98, bh, w * .02, hh - 2 * bh);
        c.fillStyle = 'rgba(180,30,30,.9)'; c.fillRect(w - w * .07, hh * .1, hh * .05, hh * .05);
        c.fillStyle = '#fff'; c.font = '700 ' + fs(.03) + 'px ' + serif; c.textAlign = 'center'; c.textBaseline = 'middle';
        c.fillText('唱', w - w * .07 + hh * .025, hh * .125);
        if (title) { c.fillStyle = 'rgba(60,30,20,.9)'; c.font = fs(.024) + 'px ' + serif; c.fillText(title, w / 2, bh + hh * .03); }
      }
      c.restore();
    }

    /* ------------------------------ 第四十五轮：新蒙版（16 种，歌回 / 3D LIVE / MV 常见） ------------------------------ */
    function drawMask45(c, m, w, hh, t, amt, pulse) {
      const c1 = pal.c1 || '#ffd1ec';
      if (m === 'chroma') {          // 色差边：左上偏品红、右下偏青，随拍抖一点
        c.globalCompositeOperation = 'lighter';
        const k = (.08 + .06 * amt) * (1 + .6 * pulse);
        const g1 = c.createLinearGradient(0, 0, w * .25, hh * .25); g1.addColorStop(0, 'rgba(255,40,160,' + k.toFixed(3) + ')'); g1.addColorStop(1, 'rgba(255,40,160,0)');
        c.fillStyle = g1; c.fillRect(0, 0, w, hh);
        const g2 = c.createLinearGradient(w, hh, w * .75, hh * .75); g2.addColorStop(0, 'rgba(40,220,255,' + k.toFixed(3) + ')'); g2.addColorStop(1, 'rgba(40,220,255,0)');
        c.fillStyle = g2; c.fillRect(0, 0, w, hh);
      } else if (m === 'starburst') { // 十字星芒（舞台灯直射镜头）
        c.globalCompositeOperation = 'lighter';
        for (let i = 0; i < 3; i++) {
          const a = Math.max(0, Math.sin(t * (.7 + .3 * i) + i * 2.1)) * (.4 + .6 * amt);
          if (a < .1) continue;
          const x = w * (.15 + .7 * h(i * 4.1 + seed * .001)), y = hh * (.08 + .2 * h(i * 2.7)), L2 = hh * (.18 + .1 * pulse) * a;
          for (const [dx, dy] of [[1, 0], [0, 1], [.7, .7], [.7, -.7]]) {
            const g = c.createLinearGradient(x - dx * L2, y - dy * L2, x + dx * L2, y + dy * L2);
            g.addColorStop(0, 'rgba(255,255,255,0)'); g.addColorStop(.5, 'rgba(255,255,255,' + (.35 * a).toFixed(3) + ')'); g.addColorStop(1, 'rgba(255,255,255,0)');
            c.strokeStyle = g; c.lineWidth = dx && dy ? 1 : 2; c.beginPath(); c.moveTo(x - dx * L2, y - dy * L2); c.lineTo(x + dx * L2, y + dy * L2); c.stroke();
          }
        }
      } else if (m === 'spot') {     // 追光：人物周围亮、四周压暗
        const g = c.createRadialGradient(w * .5, hh * .45, hh * .25, w * .5, hh * .5, hh * .95);
        g.addColorStop(0, 'rgba(0,0,0,0)'); g.addColorStop(1, 'rgba(0,0,0,' + (.28 + .12 * amt).toFixed(3) + ')');
        c.fillStyle = g; c.fillRect(0, 0, w, hh);
      } else if (m === 'dust') {     // 光里的浮尘
        for (let i = 0; i < 40; i++) {
          const x = U.fract(h(i * 3.7) + t * .008 * (h(i) - .5)) * w, y = U.fract(h(i * 5.1) - t * .01 * (.3 + h(i * 2))) * hh;
          c.fillStyle = 'rgba(255,245,225,' + (.15 + .25 * Math.max(0, Math.sin(t + i))).toFixed(3) + ')'; c.fillRect(x, y, 1.5, 1.5);
        }
      } else if (m === 'milk') {     // 奶白柔光：整体提亮发雾
        c.fillStyle = 'rgba(255,248,252,' + (.07 + .04 * amt).toFixed(3) + ')'; c.fillRect(0, 0, w, hh);
        const g = c.createRadialGradient(w / 2, hh / 2, hh * .3, w / 2, hh / 2, hh);
        g.addColorStop(0, 'rgba(255,255,255,0)'); g.addColorStop(1, 'rgba(255,240,248,.22)'); c.fillStyle = g; c.fillRect(0, 0, w, hh);
      } else if (m === 'bloom') {    // 梦幻高光溢出（四角柔光晕）
        c.globalCompositeOperation = 'lighter';
        for (const [x, y] of [[0, 0], [w, 0], [0, hh], [w, hh]]) {
          const g = c.createRadialGradient(x, y, 0, x, y, hh * .6); g.addColorStop(0, U.rgba(c1, .14 + .08 * amt)); g.addColorStop(1, U.rgba(c1, 0));
          c.fillStyle = g; c.fillRect(0, 0, w, hh);
        }
      } else if (m === 'vhsbar') {   // 录像带跟踪条：一条慢慢往下走的噪声带
        const y = U.fract(t * .07) * hh * 1.2 - hh * .1, bh2 = hh * .05, fr = Math.floor(t * 20);
        c.fillStyle = 'rgba(255,255,255,.06)'; c.fillRect(0, y, w, bh2);
        for (let i = 0; i < 60; i++) { c.fillStyle = 'rgba(255,255,255,' + (.1 * h(i + fr)).toFixed(3) + ')'; c.fillRect(h(i * 7.3 + fr) * w, y + h(i * 3.1) * bh2, w * .05, 1); }
      } else if (m === 'crt') {      // 显像管：四边暗角 + 竖向细纹
        c.fillStyle = 'rgba(0,0,0,.05)';
        for (let x = 0; x < w; x += 3) c.fillRect(x, 0, 1, hh);
        const g = c.createRadialGradient(w / 2, hh / 2, hh * .5, w / 2, hh / 2, Math.hypot(w, hh) * .55);
        g.addColorStop(0, 'rgba(0,0,0,0)'); g.addColorStop(1, 'rgba(0,0,0,.45)'); c.fillStyle = g; c.fillRect(0, 0, w, hh);
      } else if (m === 'lensflare') { // 镜头光斑：从右上角斜穿画面的一串光圈
        c.globalCompositeOperation = 'lighter';
        const sx = w * (.85 + .05 * Math.sin(t * .2)), sy = hh * .12, ex = w * .5, ey = hh * .5;
        const g0 = c.createRadialGradient(sx, sy, 0, sx, sy, hh * .25); g0.addColorStop(0, 'rgba(255,250,230,' + (.35 + .15 * amt).toFixed(3) + ')'); g0.addColorStop(1, 'rgba(255,250,230,0)');
        c.fillStyle = g0; c.fillRect(0, 0, w, hh);
        [.4, .7, 1.1, 1.4].forEach((k, i) => {
          const x = sx + (ex - sx) * k * 1.4, y = sy + (ey - sy) * k * 1.4, r = hh * (.02 + .03 * h(i * 3.3));
          c.fillStyle = U.rgba(['#ffe0a0', '#a0e0ff', '#ffb0e0', '#c0ffd0'][i], .12 + .06 * amt); c.beginPath(); c.arc(x, y, r, 0, 6.283); c.fill();
        });
      } else if (m === 'burn') {     // 胶片烧边：边缘一团橙红，每 4 秒换个位置
        c.globalCompositeOperation = 'lighter';
        const q = Math.floor(t * .25), x = h(q * 1.7) < .5 ? 0 : w, y = hh * (.2 + .6 * h(q * 2.3));
        const g = c.createRadialGradient(x, y, 0, x, y, hh * (.5 + .1 * Math.sin(t * 2)));
        g.addColorStop(0, 'rgba(255,140,60,' + (.3 + .1 * amt).toFixed(3) + ')'); g.addColorStop(.5, 'rgba(255,60,40,.08)'); g.addColorStop(1, 'rgba(255,60,40,0)');
        c.fillStyle = g; c.fillRect(0, 0, w, hh);
      } else if (m === 'moonrays') { // 月光：从右上斜照下来的冷色光带
        c.globalCompositeOperation = 'lighter';
        for (let i = 0; i < 4; i++) {
          const ox = w * (.65 + .1 * i), a = 2.0 + i * .08 + Math.sin(t * .15 + i) * .02, len = hh * 1.5;
          const g = c.createLinearGradient(ox, 0, ox + Math.cos(a) * len, Math.sin(a) * len);
          g.addColorStop(0, 'rgba(200,220,255,' + (.07 + .04 * amt).toFixed(3) + ')'); g.addColorStop(1, 'rgba(200,220,255,0)');
          c.fillStyle = g; c.beginPath(); c.moveTo(ox, 0); c.lineTo(ox + Math.cos(a - .05) * len, Math.sin(a - .05) * len); c.lineTo(ox + Math.cos(a + .05) * len, Math.sin(a + .05) * len); c.fill();
        }
      } else if (m === 'glitchbar') { // 故障条：偶发错位色块（副歌更频繁）
        const fr = Math.floor(t * 10);
        if (h(fr * .31) < .25 + .25 * amt) for (let i = 0; i < 3; i++) {
          c.fillStyle = ['rgba(255,40,200,.14)', 'rgba(40,240,255,.14)', 'rgba(255,255,255,.08)'][i];
          c.fillRect(h(fr + i * 3.3) * w * .3, h(fr * 1.7 + i) * hh, w * (.3 + .5 * h(fr + i)), hh * (.01 + .03 * h(fr * 2 + i)));
        }
      } else if (m === 'caustic') {  // 水下焦散：缓慢流动的亮纹
        c.globalCompositeOperation = 'lighter'; c.strokeStyle = 'rgba(180,240,255,' + (.06 + .04 * amt).toFixed(3) + ')'; c.lineWidth = 2;
        for (let k = 0; k < 7; k++) {
          c.beginPath();
          for (let x = 0; x <= w; x += w / 24) {
            const y = hh * (k / 7) + Math.sin(x / w * 9 + t * .8 + k * 1.3) * hh * .03 + Math.sin(x / w * 23 - t * .5 + k) * hh * .012;
            if (x) c.lineTo(x, y); else c.moveTo(x, y);
          }
          c.stroke();
        }
      } else if (m === 'petalsoft') { // 失焦花瓣（大而虚，前景）
        for (let i = 0; i < 6; i++) {
          const x = U.fract(h(i * 4.3 + seed * .001) + t * .01 * (i % 2 ? 1 : -1)) * w, y = U.fract(h(i * 2.9) + t * .02) * hh, r = hh * (.03 + .03 * h(i));
          c.fillStyle = U.rgba(c1, .18); c.beginPath(); c.ellipse(x, y, r, r * .6, t * .4 + i, 0, 6.283); c.fill();
        }
      } else if (m === 'rainstreak') { // 霓虹雨丝
        c.strokeStyle = 'rgba(200,220,255,.14)'; c.lineWidth = 1;
        for (let i = 0; i < 50; i++) { const x = h(i * 3.3) * w, y = U.fract(h(i * 7.7) + t * (.6 + .4 * h(i))) * hh; c.beginPath(); c.moveTo(x, y); c.lineTo(x - hh * .01, y + hh * .05); c.stroke(); }
      } else if (m === 'frost') {    // 冰霜：四角冷白结晶纹
        c.strokeStyle = 'rgba(230,248,255,' + (.18 + .08 * amt).toFixed(3) + ')'; c.lineWidth = 1;
        for (const [x0, y0, sx, sy] of [[0, 0, 1, 1], [w, 0, -1, 1], [0, hh, 1, -1], [w, hh, -1, -1]]) {
          for (let i = 0; i < 9; i++) {
            const a = h(i * 3.1 + x0 * .01) * 1.4 + .1, L2 = hh * (.06 + .12 * h(i * 5.3 + y0 * .01));
            const ex2 = x0 + sx * Math.cos(a) * L2, ey2 = y0 + sy * Math.sin(a) * L2;
            c.beginPath(); c.moveTo(x0, y0); c.lineTo(ex2, ey2); c.stroke();
            c.beginPath(); c.moveTo(ex2, ey2); c.lineTo(ex2 + sx * L2 * .2, ey2 - sy * L2 * .15); c.stroke();
          }
        }
      }
    }

    /* ------------------------------ 蒙版层 ------------------------------ */
    function drawMasks(list, t, amt, pulse) {
      const c = ctxMask, w = els.mask.width, hh = els.mask.height;
      c.clearRect(0, 0, w, hh);
      let hazeA = 0;
      for (const [m, wgt] of list) {
        if (wgt <= .01) continue;
        if (m === 'haze') { hazeA = Math.max(hazeA, wgt); continue; }
        c.save();
        c.globalAlpha = wgt;
        if (m === 'vignette') {
          const g = c.createRadialGradient(w / 2, hh * .48, hh * .35, w / 2, hh * .5, Math.hypot(w, hh) * .62);
          g.addColorStop(0, 'rgba(0,0,0,0)'); g.addColorStop(1, 'rgba(0,0,0,' + (.32 + .1 * amt).toFixed(3) + ')');
          c.fillStyle = g; c.fillRect(0, 0, w, hh);
        } else if (m === 'grain') {
          const fr = Math.floor(t * 12);
          c.fillStyle = 'rgba(255,255,255,.05)';
          for (let i = 0; i < 400; i++) c.fillRect(h(i * 3.1 + fr) * w, h(i * 7.7 + fr * 1.3) * hh, 1, 1);
          c.fillStyle = 'rgba(0,0,0,.06)';
          for (let i = 0; i < 400; i++) c.fillRect(h(i * 5.3 + fr * .7) * w, h(i * 2.9 + fr) * hh, 1, 1);
        } else if (m === 'scratch') {
          const fr = Math.floor(t * 8);
          c.strokeStyle = 'rgba(255,255,255,.12)'; c.lineWidth = 1;
          for (let i = 0; i < 2; i++) if (h(fr * .37 + i) > .5) { const x = h(fr + i * 9) * w; c.beginPath(); c.moveTo(x, 0); c.lineTo(x + 3, hh); c.stroke(); }
        } else if (m === 'scan') {
          c.fillStyle = 'rgba(0,0,0,' + (.06 + .03 * amt).toFixed(3) + ')';
          for (let y = 0; y < hh; y += 3) c.fillRect(0, y, w, 1);
          const yy = U.fract(t * .12) * hh; c.fillStyle = 'rgba(255,255,255,.05)'; c.fillRect(0, yy, w, hh * .04);
        } else if (m === 'rays') {
          c.globalCompositeOperation = 'lighter';
          const ox = w * (.2 + .6 * h(seed * .003)), oy = -hh * .1;
          for (let i = 0; i < 7; i++) {
            const a = .5 + i * .18 + Math.sin(t * .2 + i) * .03, len = hh * 1.4;
            const g = c.createLinearGradient(ox, oy, ox + Math.cos(a) * len, oy + Math.sin(a) * len);
            g.addColorStop(0, 'rgba(255,240,210,' + ((.06 + .04 * amt) * (1 + .5 * pulse)).toFixed(3) + ')'); g.addColorStop(1, 'rgba(255,240,210,0)');
            c.fillStyle = g; c.beginPath(); c.moveTo(ox, oy);
            c.lineTo(ox + Math.cos(a - .04) * len, oy + Math.sin(a - .04) * len); c.lineTo(ox + Math.cos(a + .04) * len, oy + Math.sin(a + .04) * len); c.fill();
          }
        } else if (m === 'prism') {
          // 棱镜色散：左右边缘一红一青的细色带
          c.globalCompositeOperation = 'lighter';
          const gL = c.createLinearGradient(0, 0, w * .08, 0); gL.addColorStop(0, 'rgba(255,60,120,' + (.12 + .08 * amt).toFixed(3) + ')'); gL.addColorStop(1, 'rgba(255,60,120,0)');
          c.fillStyle = gL; c.fillRect(0, 0, w * .08, hh);
          const gR = c.createLinearGradient(w, 0, w * .92, 0); gR.addColorStop(0, 'rgba(60,220,255,' + (.12 + .08 * amt).toFixed(3) + ')'); gR.addColorStop(1, 'rgba(60,220,255,0)');
          c.fillStyle = gR; c.fillRect(w * .92, 0, w * .08, hh);
        } else if (m === 'sparkle') {
          for (let i = 0; i < 16; i++) {
            const a = Math.max(0, Math.sin(t * (1.2 + h(i)) + i * 2.3));
            if (a < .2) continue;
            const x = h(i * 3.3 + seed * .001) * w, y = h(i * 7.1) * hh * .8, r = hh * .02 * a;
            c.fillStyle = 'rgba(255,255,255,' + (a * .7).toFixed(2) + ')';
            c.beginPath(); c.moveTo(x, y - r); c.quadraticCurveTo(x, y, x + r * .35, y); c.quadraticCurveTo(x, y, x, y + r); c.quadraticCurveTo(x, y, x - r * .35, y); c.quadraticCurveTo(x, y, x, y - r); c.fill();
          }
        } else if (m === 'halftone') {
          // 角落网点纹（左下 / 右上）
          const cs = hh * .03;
          for (const [cx0, cy0, sx, sy] of [[0, hh, 1, -1], [w, 0, -1, 1]]) {
            for (let i = 0; i < 9; i++) for (let j = 0; j < 9; j++) {
              const r = cs * .45 * (1 - (i + j) / 16);
              if (r <= 0) continue;
              c.fillStyle = U.rgba(pal.hot || '#ff6fae', .22); c.beginPath(); c.arc(cx0 + sx * (i + .5) * cs, cy0 + sy * (j + .5) * cs, r, 0, 6.283); c.fill();
            }
          }
        }
        else drawMask45(c, m, w, hh, t, amt, pulse);
        c.restore();
      }
      els.haze.style.opacity = (hazeA * (.6 + .3 * amt)).toFixed(2);
    }

    let curF = null, prevF = null, fT0 = 0, lastSeg = -1;
    // 第四十七轮：人物配色自适应——人物偏白（亮度高、饱和低）时，背景层整体压暗、漏光减弱、调色更饱和，
    // 让白发白衣从背景里「跳出来」；人物偏暗时反过来提亮背景。主色相用于漏光配补色，增加氛围。
    let FIG = null;
    function setFigure(f) { FIG = f; }
    function figAdjust() {
      if (!FIG) return { bgDim: 0, leakK: 1, gradeK: 1 };
      const L2 = FIG.lum, S2 = FIG.sat;
      // 亮度接近 1 时 HSL 饱和度会被放大（白发带粉影也能到 .3），所以饱和门槛放宽
      const white = U.clamp((L2 - .6) / .25) * U.clamp((.6 - S2) / .35);
      const dark = U.clamp((.35 - L2) / .2);
      return { bgDim: .16 * white - .12 * dark, leakK: 1 - .55 * white, gradeK: 1 + .35 * white, white, dark };
    }
    let lineOn = null;
    function setLineMode(on) {
      if (lineOn === on) return;
      lineOn = on;
      const disp = on ? 'none' : '';
      for (const k of ['tiltT', 'tiltB', 'haze', 'leak', 'bokeh', 'mask', 'lines']) if (els[k]) els[k].style.display = disp;
      const vig = wrap.querySelector('.ns-vig'), grain = wrap.querySelector('.ns-grain');
      if (vig) vig.style.display = disp;
      if (grain) grain.style.display = disp;
    }
    function update(t, info) {
      if (!W) return;
      const lineNow = !!(ART && ART.isLineAt && ART.isLineAt(t));
      setLineMode(lineNow);
      const I = info.intensity || 0, pulse = info.pulse || 0, sec = info.section || '';
      const climax = sec === 'chorus' || sec === 'final';
      // 当前场景段 → 滤镜套装（切段时 1.2 s 过渡）
      let si = 0;
      for (let k = 0; k < scenes.length; k++) if (scenes[k].t0 <= t) si = k;
      if (si !== lastSeg) { prevF = curF; curF = FILTERS[segFilter[si]] || FILTERS.softpink; fT0 = t; lastSeg = si; }
      const F = curF || FILTERS.softpink;
      const mix = prevF ? U.clamp((t - fT0) / 1.2) : 1;
      const lerpN = (a, b) => (prevF ? U.lerp(a, b, mix) : b);
      // 强度：主歌低、副歌 ×climax；桥段更淡
      const amt = climax ? Math.min(1, .55 + .45 * I) : (sec === 'bridge' || sec === 'intro' || sec === 'outro' ? .35 : .5);
      const gA = lerpN(prevF ? prevF.gA : F.gA, F.gA) * (climax ? F.climax : 1) * (.8 + .4 * amt);
      // 调色：两层交叉淡化（旧套装层淡出、新套装层淡入），不再瞬间换色
      const V = segVar[si] || { gk: 1, lk: 1, drop: 0 };
      const gradeCss = f => 'linear-gradient(160deg,' + U.rgba(f.grade[0], 1) + ' 0%,rgba(128,128,128,1) 48%,' + U.rgba(f.grade[1], 1) + ' 100%)';
      const FA = lineNow ? { bgDim: 0, leakK: 0, gradeK: 0 } : figAdjust();
      const ga = U.clamp(gA * V.gk * FA.gradeK, 0, .34);
      // 背景压暗层（只在人物后面的世界层生效：放在 ns-world 的背景 canvas 上做 brightness）
      lastBgDim = FA.bgDim;
      if (els.grade._f !== F) { els.grade.style.background = gradeCss(F); els.grade._f = F; }
      if (prevF && els.grade2._f !== prevF) { els.grade2.style.background = gradeCss(prevF); els.grade2._f = prevF; }
      els.grade.style.opacity = (ga * mix).toFixed(3);
      els.grade2.style.opacity = prevF ? (ga * (1 - mix)).toFixed(3) : '0';
      // 第四十五轮：整体色调（黑白 MV / 回忆棕褐），切段时跟着交叉淡化
      {
        const tn = F.tone, tp = prevF && prevF.tone;
        const ta = (tn ? tn[1] : 0) * mix + (tp ? tp[1] : 0) * (prevF ? 1 - mix : 0);
        const kind = tn ? tn[0] : (tp ? tp[0] : '');
        if (els.tone._k !== kind) {
          els.tone._k = kind;
          els.tone.style.background = kind === 'mono' ? '#808080' : kind === 'sepia' ? '#a07850' : 'transparent';
          els.tone.style.mixBlendMode = kind === 'mono' ? 'saturation' : 'color';
        }
        els.tone.style.opacity = U.clamp(ta, 0, .9).toFixed(3);
      }
      // 漏光：副歌更亮，主歌克制（上一轮过曝，这里整体降低）
      {
        const c = ctxLeak, w = els.leak.width, hh = els.leak.height;
        c.clearRect(0, 0, w, hh);
        const leakA = F.leakA * (climax ? F.climax : 1) * (segVar[si] ? segVar[si].lk : 1) * figAdjust().leakK;
        for (let i = 0; i < 3; i++) {
          const x = w * (.15 + .7 * U.fract(h(seed * .001 + i) + t * .012 * (i % 2 ? 1 : -1)));
          const y = hh * (.1 + .25 * h(i * 3.3 + seed * .002) + .05 * Math.sin(t * .3 + i));
          const r = hh * (.5 + .25 * h(i * 7.1));
          const a = U.clamp(leakA * (.8 + .4 * I + .3 * pulse), 0, .3);
          const g = c.createRadialGradient(x, y, 0, x, y, r);
          const col = i === 1 ? (pal.c1 || F.leak) : F.leak;
          g.addColorStop(0, U.rgba(col, a)); g.addColorStop(1, U.rgba(col, 0));
          c.fillStyle = g; c.fillRect(0, 0, w, hh);
        }
      }
      const DECO = (ART && ART.decoAt(t)) || { lines: L.lines, bokeh: 1, ghost: true };
      // 前景虚化光斑：只在画面边缘
      {
        const c = ctxBokeh, w = els.bokeh.width, hh = els.bokeh.height;
        c.clearRect(0, 0, w, hh);
        for (let i = 0; i < 10; i++) {
          const side = i % 2 ? 1 : 0;
          const x = side ? w * (.88 + .14 * h(i * 2.1)) : w * (-.02 + .14 * h(i * 2.1));
          const y = hh * U.fract(h(i * 4.7 + seed * .003) + t * (.006 + .01 * h(i)) * (i % 3 ? 1 : -1));
          const r = hh * (.025 + .05 * h(i * 5.9));
          const col = [pal.c1 || '#ffd1ec', pal.c2 || '#c9e9ff', '#ffffff'][i % 3];
          const a = (.1 + .06 * Math.sin(t * .8 + i) + .05 * pulse) * (climax ? 1.3 : 1) * DECO.bokeh;
          c.fillStyle = U.rgba(col, U.clamp(a, 0, .26));
          c.beginPath(); c.arc(x, y, r, 0, 6.283); c.fill();
          c.strokeStyle = U.rgba('#ffffff', U.clamp(a * .8, 0, .35)); c.lineWidth = 1.5; c.stroke();
        }
      }
      // 细几何线
      if (DECO.lines === 'none') { ctxLines.clearRect(0, 0, els.lines.width, els.lines.height); }
      else {
        const c = ctxLines, w = els.lines.width, hh = els.lines.height;
        c.clearRect(0, 0, w, hh);
        c.strokeStyle = 'rgba(255,255,255,.32)'; c.lineWidth = 1;
        if (DECO.lines === 'slash') {
          const off = (t * 6) % (hh * .12);
          for (let k = -2; k < 8; k++) { const x0 = w * .74 + k * hh * .12 + off; c.beginPath(); c.moveTo(x0, 0); c.lineTo(x0 - hh * .9, hh); c.stroke(); }
          c.beginPath(); c.moveTo(0, hh * .2); c.lineTo(w * .3, hh * .2); c.stroke();
        } else {
          const cx = w * .5, cy = hh * .5, R = hh * .62, rot = t * .03;
          for (let k = 0; k < 3; k++) {
            const r = R * (1 - k * .18);
            c.beginPath();
            for (let j = 0; j <= 3; j++) {
              const a = rot + (k % 2 ? Math.PI : 0) + j * 2.094 - Math.PI / 2;
              const px = cx + Math.cos(a) * r, py = cy + Math.sin(a) * r;
              if (j) c.lineTo(px, py); else c.moveTo(px, py);
            }
            c.stroke();
          }
        }
      }
      // 蒙版：新套装的蒙版按过渡比例淡入，旧套装独有的淡出；每段随机去掉一种（数值拼接，不是固定套装）
      {
        const V2 = segVar[si] || { drop: 0 };
        const cur = F.masks.filter((m, j) => !(F.masks.length > 2 && j === V2.drop % F.masks.length && m !== 'vignette'));
        const old = prevF ? prevF.masks.filter(m => !cur.includes(m)) : [];
        const mk = (ART && ART.decoAt) ? (ART.decoAt(t).maskK || 1) : 1;
        drawMasks(cur.map(m => [m, mix * mk]).concat(old.map(m => [m, (1 - mix) * mk])), t, amt, pulse);
      }
      // 背后大字
      const line = info.line;
      const WEAK = /^(って|して|から|けど|のに|だけ|まで|ない|てる|です|ます|った|ている|の|が|は|を|に|で|と|も|了|的|吧|呢|啊|吗|着)$/;
      let word = '';
      if (line) {
        const kw = line.kw || '';
        if (kw && [...kw].length >= 2 && [...kw].length <= 6 && !WEAK.test(kw)) word = kw;
        else {
          const parts = String(line.text || '').split(/[\s、，,。！!？?・…「」『』（）()]+/).filter(Boolean);
          const best = parts.sort((a, b) => [...b].length - [...a].length)[0] || '';
          word = [...best].slice(0, 6).join('');
          if ([...word].length < 2) word = '';
        }
      }
      if (ghost.textContent !== word) ghost.textContent = word;
      if (line && word && DECO.ghost) {
        const age = t - (line.t0 || t), left = (line.t1 || t) + 1 - t;
        const a = U.clamp(Math.min(age / .6, left / .8), 0, 1);
        ghost.style.opacity = (a * (ART ? (climax ? .38 : .22) : (climax ? .95 : .7))).toFixed(3);
        if (lineNow !== ghost._line) {
          ghost._line = lineNow;
          const light = lineNow && ART.groundAt && ART.groundAt(t) !== 'ink';
          ghost.style.webkitTextStroke = lineNow ? '0' : '2px rgba(255,255,255,.26)';
          // 背景文字：只描一圈很细的线（不填实心），浅底用强调色淡线、深底用白线
          ghost.style.color = 'rgba(255,255,255,0)';
          ghost.style.webkitTextStroke = lineNow ? ('1.5px ' + (light ? U.rgba(pal.hot || pal.c1 || '#ff6fae', .34) : 'rgba(255,255,255,.22)')) : '2px rgba(255,255,255,.26)';
          ghost.style.letterSpacing = lineNow ? '.02em' : '-.04em';
        }
        if (lineNow) { ghost.style.opacity = a.toFixed(3); ghost.style.filter = 'none'; }
        ghost.style.filter = 'blur(' + ((1 - a) * 10 + 1).toFixed(1) + 'px)';
        ghost.style.transform = 'translateX(' + (-(info.camCx - .5) * W * .12).toFixed(1) + 'px) scale(' + (1 + .04 * U.clamp(age / 4, 0, 1)).toFixed(3) + ')';
      } else ghost.style.opacity = '0';
      // 边框：副歌段换第二种
      const title = String((info.title || '')).slice(0, 40);
      // 第四十轮：整场只用一种边框——不同边框留的「窗口」大小不同（屏幕框内缩、拍立得底边宽、电影黑边），
      // 主歌/副歌来回换会让画面看起来一会儿缩小到左上、一会儿又放大（用户反馈的「抽动」）。副歌只加强边框光。
      // 第四十七轮：有美术指导时边框跟段落主题走（极简主题无边框、繁复主题有边框），切换走下面的交叉淡化
      let kind = frameMain;
      if (ART && ART.frameAt) {
        const fa = ART.frameAt(t);
        if (!fa) kind = 'none';
        else if (fa.ext && FX) { const gk = fa.ext; if (!frameGenome || frameGenome.kind !== gk) frameGenome = FX.genome(gk, seed, pal); kind = 'ext:' + gk; }
        else if (fa.kind) kind = fa.kind;
      }
      const key = kind + '|' + W + 'x' + H + '|' + title;
      if (frameDrawn !== key) {
        if (!frameDrawn || frameDrawn.split('|')[1] !== key.split('|')[1]) { drawFrame(kind, title, els.frame); els.frame.style.opacity = '1'; els.frame2.style.opacity = '0'; frameFadeT0 = -1; }
        else {
          // 新边框画到隐藏的那层，然后交叉淡化
          const front = els.frame.style.opacity === '0' ? els.frame : els.frame2;
          const backC = front === els.frame ? els.frame2 : els.frame;
          drawFrame(kind, title, front); frameFront = front; frameBack = backC; frameFadeT0 = t;
        }
        frameDrawn = key;
      }
      if (frameFadeT0 >= 0) {
        // 第五十三轮：两个边框不叠——旧边框前 0.45 s 退完，新边框后 0.45 s 再进（中间 0.1 s 无边框）
        const d = t - frameFadeT0;
        frameBack.style.opacity = (1 - U.easeInOut(U.clamp(d / .45))).toFixed(3);
        frameFront.style.opacity = U.easeInOut(U.clamp((d - .55) / .45)).toFixed(3);
        if (d >= 1) frameFadeT0 = -1;
      }
      global.__NEKO_FRAME_KIND = kind;
      els.frame.style.filter = climax ? 'brightness(1.15) drop-shadow(0 0 ' + Math.round(H * .006) + 'px ' + U.rgba(pal.hot || pal.c1 || '#ffffff', .6) + ')' : '';
      lastPx = F.px || null; lastPxPrev = prevF ? (prevF.px || null) : null; lastPxMix = mix;
      lastInfo = { filter: segFilter[si], frame: kind === 'ext' && frameGenome ? frameGenome.kind : kind, amt: +amt.toFixed(2), bgDim: +lastBgDim.toFixed(2), density: ART && ART.themeAt ? ART.themeAt(t).density : null };
    }
    let lastInfo = null, frameFadeT0 = -1, frameFront = null, frameBack = null, lastPx = null, lastPxPrev = null, lastPxMix = 1, lastBgDim = 0;

    // 人物辉光（描边光 + 柔和提亮），作用在宿主的人物容器上；加载器退出时会把容器样式整体还原
    function modelFilter(intensity, climax) {
      if (lineOn) {
        const light = !(ART && ART.groundNow === 'ink');
        return light
          ? 'drop-shadow(0 ' + (6 * H / 1080).toFixed(1) + 'px ' + (22 * H / 1080).toFixed(1) + 'px rgba(70,50,90,.28)) drop-shadow(0 0 ' + (1.5 * H / 1080).toFixed(1) + 'px rgba(70,50,90,.55))'
          : 'drop-shadow(0 0 ' + (3 * H / 1080).toFixed(1) + 'px rgba(255,255,255,.85))';
      }
      const r = (5 + 7 * intensity * (climax ? 1.3 : 1)) * (H / 1080);
      return 'drop-shadow(0 0 ' + r.toFixed(1) + 'px ' + U.rgba(L.glow, .5) + ') saturate(1.05) contrast(1.03)';
    }

    function destroy() {
      for (const k in els) { const e = els[k]; if (e && e.parentNode) e.parentNode.removeChild(e); }
      if (ghost.parentNode) ghost.parentNode.removeChild(ghost);
    }
    return { resize, update, modelFilter, destroy, look: L, get info() { return lastInfo; },
      get px() { return { cur: lastPx, prev: lastPxPrev, mix: lastPxMix }; }, get bgDim() { return lastBgDim; }, setFigure, get figure() { return FIG; } };
  }

  global.NekoStageLook = { create, LOOKS, FILTERS };
})(window);
