/* =============================================================================
 * 「为她而唱」舞台 · 第四十一轮：混合模板渲染（template 场景）
 * 素材库里，本地视觉模型把参考图看成「设计语法」（版式/层次/卡片做法/装饰种类/纹理/配色气质），
 * 两三份语法混合成一个新模板。这里用自己的程序把模板画出来——只用类别，没有任何原图的具体图形。
 *   layout   决定卡片放哪（人物站在中间，卡片放侧边或作窗框）
 *   layers   从后到前：渐变底 / 纯色底 / 纹理 / 卡片 / 框线 / 后景装饰 / 前景装饰 / 漏光 / 文字带
 *   card     形状（圆角/圆/票根/拍立得/撕边）+ 边（细/双/虚线/蕾丝/发光）+ 阴影 + 倾斜
 *   deco     装饰种类：星/心/丝带/花/闪光/泡泡/线/点/胶带/贴纸/云/音符/框线/格/棋盘/条纹
 *   pattern  背景纹理：点/棋盘/条纹/格/格子布/波浪/网点/噪点
 * ========================================================================== */
(function (global) {
  'use strict';
  const U = global.NekoStageU;
  const S = global.NekoStageScenes;
  if (!U || !S) return;
  const h = U.h01;
  const rgba = (col, a) => U.rgba(col, U.clamp(a, 0, 1));
  const MOODPAL = {
    pastel: ['#ffd6e8', '#cfe8ff', '#fff1c4', '#e3d4ff', '#ffffff'], vivid: ['#ff4f8b', '#3ad6ff', '#ffd23f', '#7a5cff', '#ffffff'],
    dark: ['#14101e', '#2a2140', '#6a4a8a', '#c08aff', '#e0d0ff'], mono: ['#1a1a1a', '#444444', '#888888', '#cccccc', '#ffffff'],
    warm: ['#ffb38a', '#ff7a6a', '#ffe0a0', '#8a4a5a', '#fff4e0'], cool: ['#9fd8ff', '#6a8aff', '#c9f0ff', '#2a3a7a', '#ffffff'],
    neon: ['#0a0618', '#ff3fa4', '#33e1ff', '#ffe066', '#9b5cff'], vintage: ['#f2d9a8', '#a8805a', '#5a4a3a', '#d8b890', '#fff6e0']
  };
  function lum(col) { const r = U.hexRgb(col); return (.299 * r[0] + .587 * r[1] + .114 * r[2]) / 255; }

  function star(c, x, y, r) { c.beginPath(); for (let k = 0; k < 10; k++) { const a = -Math.PI / 2 + k * Math.PI / 5, rr = k % 2 ? r * .45 : r; c.lineTo(x + Math.cos(a) * rr, y + Math.sin(a) * rr); } c.closePath(); c.fill(); }
  function heart(c, x, y, r) { c.beginPath(); c.moveTo(x, y + r * .9); c.bezierCurveTo(x - r * 1.6, y - r * .1, x - r * .7, y - r * 1.3, x, y - r * .45); c.bezierCurveTo(x + r * .7, y - r * 1.3, x + r * 1.6, y - r * .1, x, y + r * .9); c.fill(); }
  function flower(c, x, y, r, col, core) { c.fillStyle = col; for (let k = 0; k < 5; k++) { const a = k * 1.2566; c.beginPath(); c.arc(x + Math.cos(a) * r * .55, y + Math.sin(a) * r * .55, r * .45, 0, 6.283); c.fill(); } c.fillStyle = core; c.beginPath(); c.arc(x, y, r * .3, 0, 6.283); c.fill(); }
  function spark(c, x, y, r) { c.beginPath(); c.moveTo(x, y - r); c.quadraticCurveTo(x, y, x + r * .35, y); c.quadraticCurveTo(x, y, x, y + r); c.quadraticCurveTo(x, y, x - r * .35, y); c.quadraticCurveTo(x, y, x, y - r); c.fill(); }
  function rr(c, x, y, w, hh, r) { c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + hh, r); c.arcTo(x + w, y + hh, x, y + hh, r); c.arcTo(x, y + hh, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath(); }

  function pattern(c, W, H, kind, cols, t) {
    const a = .26;
    c.save();
    if (kind === 'dots' || kind === 'halftone') {
      const cs = H / (kind === 'dots' ? 14 : 22);
      for (let y = 0; y < H / cs + 1; y++) for (let x = 0; x < W / cs + 1; x++) {
        const r = kind === 'halftone' ? cs * .45 * (1 - y * cs / H) : cs * .16;
        if (r <= .3) continue;
        c.fillStyle = rgba(cols[1], a + .1); c.beginPath(); c.arc(x * cs + (y % 2) * cs / 2, y * cs, r, 0, 6.283); c.fill();
      }
    } else if (kind === 'checker' || kind === 'gingham') {
      const cs = H / 9, off = (t * H * .01) % (cs * 2);
      c.fillStyle = rgba(cols[1], kind === 'gingham' ? a * .8 : a + .05);
      for (let y = -2; y < H / cs + 2; y++) for (let x = -2; x < W / cs + 2; x++) {
        if (kind === 'checker' && (x + y) % 2) continue;
        if (kind === 'gingham') { if (x % 2 === 0) c.fillRect(x * cs + off, 0, cs, H); if (y % 2 === 0) c.fillRect(0, y * cs + off, W, cs); continue; }
        c.fillRect(x * cs + off, y * cs + off, cs, cs);
      }
    } else if (kind === 'stripes') {
      c.fillStyle = rgba(cols[1], a);
      for (let x = -H; x < W + H; x += H * .08) { c.beginPath(); c.moveTo(x, 0); c.lineTo(x + H * .035, 0); c.lineTo(x + H * .035 - H, H); c.lineTo(x - H, H); c.fill(); }
    } else if (kind === 'grid') {
      c.strokeStyle = rgba(cols[1], a + .1); c.lineWidth = 1;
      const cs = H / 12;
      for (let x = 0; x < W; x += cs) { c.beginPath(); c.moveTo(x, 0); c.lineTo(x, H); c.stroke(); }
      for (let y = 0; y < H; y += cs) { c.beginPath(); c.moveTo(0, y); c.lineTo(W, y); c.stroke(); }
    } else if (kind === 'waves') {
      c.strokeStyle = rgba(cols[1], a + .1); c.lineWidth = H * .006;
      for (let k = 0; k < 9; k++) { c.beginPath(); for (let x = 0; x <= W; x += W / 60) { const y = H * (k + .5) / 9 + Math.sin(x / W * 10 + t * .6 + k) * H * .015; if (x) c.lineTo(x, y); else c.moveTo(x, y); } c.stroke(); }
    } else if (kind === 'noise') {
      for (let i = 0; i < 900; i++) { c.fillStyle = rgba(i % 2 ? '#ffffff' : '#000000', .06); c.fillRect(h(i * 3.1) * W, h(i * 7.3) * H, 2, 2); }
    }
    c.restore();
  }

  function decoLayer(c, W, H, kinds, cols, t, dens, front, seed) {
    const n = Math.round((front ? 12 : 22) * (.6 + dens));
    kinds.forEach((kd, ki) => {
      for (let i = 0; i < n; i++) {
        const id = i * 7.1 + ki * 31 + (front ? 100 : 0) + seed;
        let x = h(id * 1.3) * W, y = h(id * 2.7) * H;
        if (front) { const side = h(id) < .5; x = side ? W * (.02 + .16 * h(id * 3)) : W * (.82 + .16 * h(id * 3)); }   // 前景装饰只在两侧，不挡人物
        y += Math.sin(t * .6 + i) * H * .01;
        const r = H * (front ? .045 : .024) * (.6 + .8 * h(id * 5));
        const col = cols[(i + ki) % cols.length];
        c.save(); c.globalAlpha = front ? .92 : .7; c.fillStyle = col; c.strokeStyle = col;
        switch (kd) {
          case 'stars': star(c, x, y, r); break;
          case 'hearts': heart(c, x, y, r); break;
          case 'sparkles': spark(c, x, y, r * (1 + .3 * Math.sin(t * 3 + i))); break;
          case 'flowers': flower(c, x, y, r, col, '#fff7c0'); break;
          case 'bubbles': c.lineWidth = Math.max(1, r * .1); c.strokeStyle = 'rgba(255,255,255,.8)'; c.beginPath(); c.arc(x, H - U.fract(h(id) + t * .03) * H, r, 0, 6.283); c.stroke(); break;
          case 'dots': c.beginPath(); c.arc(x, y, r * .35, 0, 6.283); c.fill(); break;
          case 'notes': c.beginPath(); c.ellipse(x, y + r * .5, r * .45, r * .32, -.4, 0, 6.283); c.fill(); c.fillRect(x + r * .3, y - r, r * .12, r * 1.5); break;
          case 'clouds': c.globalAlpha = front ? .5 : .35; c.fillStyle = '#ffffff'; for (let k = 0; k < 4; k++) { c.beginPath(); c.arc(x + (k - 1.5) * r * .9, y + (k % 2 ? -r * .3 : 0), r * .8, 0, 6.283); c.fill(); } break;
          case 'ribbons': c.lineWidth = r * .35; c.beginPath(); c.moveTo(x - r * 3, y); c.bezierCurveTo(x - r, y - r * 2, x + r, y + r * 2, x + r * 3, y); c.stroke(); break;
          case 'tape': c.globalAlpha = .6; c.save(); c.translate(x, y); c.rotate((h(id) - .5) * .8); c.fillRect(-r * 2, -r * .45, r * 4, r * .9); c.restore(); break;
          case 'stickers': c.lineWidth = r * .25; c.strokeStyle = '#ffffff'; c.beginPath(); c.arc(x, y, r, 0, 6.283); c.fill(); c.stroke(); break;
          case 'lines': c.lineWidth = 1.5; c.beginPath(); c.moveTo(x - r * 4, y); c.lineTo(x + r * 4, y); c.stroke(); break;
          case 'frames_lines': if (i < 2 && !front && !(global.__NEKO_FRAME_KIND && global.__NEKO_FRAME_KIND !== 'none')) { /* 第五十三轮：舞台已有边框就不再画模板自带的框 */ c.lineWidth = 2; c.strokeStyle = rgba(col, .6); c.strokeRect(W * (.04 + i * .015), H * (.05 + i * .015), W * (.92 - i * .03), H * (.9 - i * .03)); } break;
          default: c.beginPath(); c.arc(x, y, r * .4, 0, 6.283); c.fill();
        }
        c.restore();
      }
    });
  }

  function drawCard(c, W, H, tp, cols, t) {
    const cd = tp.card || {};
    if (cd.shape === 'none') return;
    const L = tp.layout || 'center';
    // 卡片不挡人物：人物在中间，卡片放在侧边或作为整幅窗框
    let x, y, w, hh;
    if (L === 'right_card') { w = W * .3; hh = H * .62; x = W * .66; y = H * .19; }
    else if (L === 'left_card' || L === 'split') { w = W * .3; hh = H * .62; x = W * .04; y = H * .19; }
    else if (L === 'frame_full' || L === 'window' || L === 'center') { w = W * .9; hh = H * .86; x = W * .05; y = H * .07; }
    else if (L === 'diagonal') { w = W * .34; hh = H * .5; x = W * .62; y = H * .1; }
    else { w = W * .28; hh = H * .4; x = W * .06; y = H * .08; }
    const full = w > W * .6;
    c.save();
    if (cd.tilt === 'slight' && !full) { c.translate(x + w / 2, y + hh / 2); c.rotate(-.05); c.translate(-(x + w / 2), -(y + hh / 2)); }
    if (cd.shadow !== 'none') { c.shadowColor = 'rgba(0,0,0,' + (cd.shadow === 'hard' ? .45 : .25) + ')'; c.shadowBlur = cd.shadow === 'hard' ? 0 : H * .03; c.shadowOffsetX = c.shadowOffsetY = cd.shadow === 'hard' ? H * .012 : H * .006; }
    const fillA = full ? 0 : .5;
    if (full) { c.save(); c.lineWidth = H * .018; c.strokeStyle = rgba('#ffffff', .55); rr(c, x, y, w, hh, cd.shape === 'rounded' ? H * .05 : H * .01); c.stroke(); c.restore(); }
    c.fillStyle = rgba(cols[4] || '#ffffff', fillA);
    if (cd.shape === 'circle' && !full) { c.beginPath(); c.ellipse(x + w / 2, y + hh / 2, w / 2, hh / 2, 0, 0, 6.283); }
    else if (cd.shape === 'ticket') { rr(c, x, y, w, hh, H * .02); }
    else if (cd.shape === 'torn') { c.beginPath(); c.moveTo(x, y); for (let k = 0; k <= 20; k++) c.lineTo(x + w * k / 20, y + (h(k * 3.3) - .5) * H * .015); c.lineTo(x + w, y + hh); for (let k = 20; k >= 0; k--) c.lineTo(x + w * k / 20, y + hh + (h(k * 5.1) - .5) * H * .015); c.closePath(); }
    else rr(c, x, y, w, hh, cd.shape === 'rounded' ? H * .04 : H * .008);
    if (fillA) c.fill();
    c.shadowColor = 'transparent';
    const bc = cols[1] || '#ffffff';
    const b = cd.border || 'thin';
    if (b !== 'none') {
      c.strokeStyle = rgba(b === 'glow' ? cols[1] : '#ffffff', .9); c.lineWidth = b === 'double' ? 2 : (b === 'lace' ? H * .006 : 2.5);
      if (b === 'dashed') c.setLineDash([H * .02, H * .012]);
      if (b === 'glow') { c.shadowColor = bc; c.shadowBlur = H * .02; }
      c.stroke();
      if (b === 'double') { c.save(); c.translate(x + w / 2, y + hh / 2); c.scale(.97, .96); c.translate(-(x + w / 2), -(y + hh / 2)); c.stroke(); c.restore(); }
      if (b === 'lace') { c.fillStyle = '#ffffff'; for (let k = 0; k < 40; k++) { c.beginPath(); c.arc(x + w * k / 40, y, H * .006, 0, 6.283); c.fill(); c.beginPath(); c.arc(x + w * k / 40, y + hh, H * .006, 0, 6.283); c.fill(); } }
      c.setLineDash([]);
    }
    if (cd.shape === 'ticket' && !full) { c.globalCompositeOperation = 'destination-out'; c.fillStyle = '#000'; c.beginPath(); c.arc(x, y + hh / 2, H * .03, 0, 6.283); c.arc(x + w, y + hh / 2, H * .03, 0, 6.283); c.fill(); }
    if (cd.shape === 'polaroid' && !full) { c.fillStyle = rgba('#ffffff', .85); c.fillRect(x, y + hh * .82, w, hh * .18); }
    c.restore();
  }

  function drawTemplate(tp, c, W, H, t, P) {
    let cols = (tp.palette && tp.palette.length >= 3) ? tp.palette.slice() : (MOODPAL[tp.palette_mood] || MOODPAL.pastel).slice();
    const mood = MOODPAL[tp.palette_mood] || MOODPAL.pastel;
    // 学来的颜色常偏灰：和配色气质的颜色各取一半，保证舞台上好看
    cols = cols.slice(0, 3).concat(mood.slice(1, 3));
    const byL = cols.slice().sort((a, b) => lum(a) - lum(b));
    const layers = tp.layers && tp.layers.length ? tp.layers : ['gradient_bg', 'deco_front'];
    const dens = (tp.density || .5) * (P.dens || 1);
    const seed = h(((tp.id || '').charCodeAt(5) || 7) * 1.7);
    // 底
    const darkish = tp.palette_mood === 'dark' || tp.palette_mood === 'neon';
    // 人物多为浅色：亮背景压到中间调（明度上限约 .62），保证人物轮廓清楚
    const tame = col => { const r = U.hexRgb(col), l = lum(col); const k = l > .62 ? .62 / l : 1; const sat = 1.25;
      const m = (r[0] + r[1] + r[2]) / 3; const f = v => Math.round(U.clamp((m + (v - m) * sat) * k, 0, 255));
      return '#' + ((1 << 24) | (f(r[0]) << 16) | (f(r[1]) << 8) | f(r[2])).toString(16).slice(1); };
    const b0 = tame(darkish ? byL[0] : byL[Math.max(0, byL.length - 3)]), b1 = tame(darkish ? byL[1] : byL[byL.length - 1]);
    const g = c.createLinearGradient(0, 0, W * .35, H);
    g.addColorStop(0, b1); g.addColorStop(1, b0);
    c.fillStyle = layers.includes('solid_bg') ? b0 : g;
    c.fillRect(0, 0, W, H);
    const vg = c.createRadialGradient(W / 2, H * .45, H * .3, W / 2, H * .5, H * .95);
    vg.addColorStop(0, 'rgba(0,0,0,0)'); vg.addColorStop(1, 'rgba(0,0,0,.28)');
    c.fillStyle = vg; c.fillRect(0, 0, W, H);
    const accents = cols.filter(x => Math.abs(lum(x) - lum(b1)) > .2).map(tame);
    const ac = (accents.length >= 2 ? accents : mood.slice(1)).concat(['#ffffff']);
    if (tp.pattern && tp.pattern !== 'none') pattern(c, W, H, tp.pattern, [byL[0], ac[0], ac[1] || ac[0]], t);
    if (tp.symmetry === 'radial') {
      c.save(); c.translate(W / 2, H / 2); c.rotate(t * .05);
      for (let k = 0; k < 16; k++) { c.rotate(Math.PI / 8); c.fillStyle = rgba(ac[k % ac.length], .07); c.beginPath(); c.moveTo(0, 0); c.lineTo(W, -W * .1); c.lineTo(W, W * .1); c.fill(); }
      c.restore();
    }
    const deco = tp.deco && tp.deco.length ? tp.deco : ['sparkles'];
    if (layers.includes('deco_back') || !layers.includes('deco_front')) decoLayer(c, W, H, deco, ac.concat(['#ffffff']), t, dens, false, seed);
    if (layers.includes('card') || layers.includes('frame') || (tp.card && tp.card.shape !== 'none')) drawCard(c, W, H, tp, [byL[0]].concat(ac, ['#ffffff']), t);
    if (layers.includes('deco_front')) decoLayer(c, W, H, deco, ac.concat(['#ffffff']), t, dens, true, seed);
    if (layers.includes('light_leak')) { const gg = c.createRadialGradient(W * .85, H * .1, 0, W * .85, H * .1, H * .8); gg.addColorStop(0, rgba(ac[0], .3 + .1 * (P.pulse || 0))); gg.addColorStop(1, rgba(ac[0], 0)); c.fillStyle = gg; c.fillRect(0, 0, W, H); }
    if (layers.includes('text_band')) { c.fillStyle = rgba(byL[0], .5); c.fillRect(0, H * .9, W, H * .05); c.fillStyle = rgba(ac[0], .9); c.fillRect(0, H * .9, W * .25, H * .006); }
  }

  let TEMPLATES = [];
  function register(list) {
    TEMPLATES = (list || []).filter(t => t && t.id);
    for (const tp of TEMPLATES) {
      S.SCENES[tp.id] = (c, W, H, t, P) => drawTemplate(tp, c, W, H, t, P);
      if (S.FRONT_OF) S.FRONT_OF[tp.id] = null;
    }
    return TEMPLATES.length;
  }
  function forPreset(preset) { return TEMPLATES.filter(t => !t.genres || t.genres.includes(preset)).map(t => t.id); }
  S.registerTemplates = register;
  S.templatesFor = forPreset;
  global.NekoStageTemplates = { register, forPreset, draw: drawTemplate, get list() { return TEMPLATES; } };
})(window);
