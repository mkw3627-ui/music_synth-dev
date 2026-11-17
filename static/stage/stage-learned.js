/* =============================================================================
 * 「为她而唱」舞台 · 第四十轮：学来的风格 → 程序生成的新图案（learned 场景）
 * 素材库（asset_lib.py）从参考图里只学到数值：5 色调色盘+占比、明暗、饱和、对比、
 * 线条方向、光点密度、形状倾向（lines / dots / blobs）、是否「上亮下暗」（天空构图）。
 * 这里用这些数值重新画一张完全不同的图：渐变底 + 按形状倾向生成的几何/光斑/流线层。
 * 原图不参与绘制（学完已移入回收站），所以画出来的是「借了配色和氛围」的自有图案。
 * 舞台启动时从 /stage/learned_effects.json（插件把效果库同步到这里）读取。
 * ========================================================================== */
(function (global) {
  'use strict';
  const U = global.NekoStageU;
  const S = global.NekoStageScenes;
  if (!U || !S) return;
  const h = U.h01;
  const rgba = (col, a) => U.rgba(col, U.clamp(a, 0, 1));
  let EFFECTS = [];

  function drawLearned(fx, c, W, H, t, P) {
    const pal = fx.palette && fx.palette.length >= 3 ? fx.palette : ['#202040', '#6060a0', '#e0c0ff'];
    // 按明度排序：暗色做底、亮色做光
    const lumOf = col => { const r = U.hexRgb(col); return .299 * r[0] + .587 * r[1] + .114 * r[2]; };
    const byLum = pal.slice().sort((a, b) => lumOf(a) - lumOf(b));
    const dark = byLum[0], mid = byLum[Math.floor(byLum.length / 2)], light = byLum[byLum.length - 1];
    const g = c.createLinearGradient(0, 0, 0, H);
    if (fx.sky) { g.addColorStop(0, mid); g.addColorStop(.65, dark); g.addColorStop(1, dark); }
    else { g.addColorStop(0, dark); g.addColorStop(.6, mid); g.addColorStop(1, dark); }
    c.fillStyle = g; c.fillRect(0, 0, W, H);
    const dens = (fx.density || 1) * (P.dens || 1);
    const seed = h((fx.id || '').length * 3.7 + (P.v || 0) * 11);
    if (fx.shape === 'lines') {
      // 流线：沿学到的主方向（横/竖）铺多条柔和光带，慢慢流动
      const n = Math.round(18 * dens);
      for (let i = 0; i < n; i++) {
        const col = pal[i % pal.length], a = .12 + .25 * h(i * 2.3 + seed);
        c.strokeStyle = rgba(col, a); c.lineWidth = H * (.002 + .01 * h(i * 3.1));
        c.beginPath();
        for (let k = 0; k <= 40; k++) {
          const u = k / 40;
          let x, y;
          if (fx.dir === 'v') { y = u * H; x = W * h(i * 1.7 + seed) + Math.sin(u * 6 + t * .5 + i) * W * .04; }
          else { x = u * W; y = H * h(i * 1.7 + seed) + Math.sin(u * 6 + t * .5 + i) * H * .05; }
          if (k) c.lineTo(x, y); else c.moveTo(x, y);
        }
        c.stroke();
      }
    } else if (fx.shape === 'dots') {
      // 光点/星屑：数量按学到的亮点密度，颜色取亮色
      const n = Math.round((60 + 3000 * (fx.sparkle || .01)) * dens);
      for (let i = 0; i < Math.min(n, 400); i++) {
        const x = h(i * 3.3 + seed) * W, y = h(i * 5.9 + seed) * H;
        const tw = .4 + .6 * Math.abs(Math.sin(t * (.5 + h(i)) + i));
        const r = H * (.002 + .012 * Math.pow(h(i * 7.1), 3));
        c.fillStyle = rgba(i % 4 ? light : pal[i % pal.length], tw * (.4 + .5 * h(i * 1.1)));
        c.beginPath(); c.arc(x, y, r, 0, 6.283); c.fill();
      }
      for (let i = 0; i < 6; i++) {
        const x = W * h(i * 9.1 + seed), y = H * h(i * 4.4 + seed), r = H * (.15 + .2 * h(i));
        const gg = c.createRadialGradient(x, y, 0, x, y, r);
        gg.addColorStop(0, rgba(pal[(i + 1) % pal.length], .25)); gg.addColorStop(1, rgba(pal[(i + 1) % pal.length], 0));
        c.fillStyle = gg; c.fillRect(x - r, y - r, r * 2, r * 2);
      }
    } else {
      // 色块/光斑：大小不一的柔边圆斑，按调色盘占比分配颜色，缓慢漂移
      const w = fx.weights || [1];
      const n = Math.round(22 * dens);
      for (let i = 0; i < n; i++) {
        let pick = h(i * 2.9 + seed), k = 0, acc = 0;
        for (; k < w.length; k++) { acc += w[k]; if (pick <= acc) break; }
        const col = pal[Math.min(k, pal.length - 1)];
        const x = (h(i * 3.7 + seed) * 1.2 - .1) * W + Math.sin(t * .15 + i) * W * .03;
        const y = (h(i * 6.1 + seed) * 1.2 - .1) * H + Math.cos(t * .12 + i) * H * .03;
        const r = H * (.06 + .22 * h(i * 1.9));
        const gg = c.createRadialGradient(x, y, 0, x, y, r);
        gg.addColorStop(0, rgba(col, .45)); gg.addColorStop(.6, rgba(col, .18)); gg.addColorStop(1, rgba(col, 0));
        c.fillStyle = gg; c.fillRect(x - r, y - r, r * 2, r * 2);
      }
    }
    // 统一加一层与节拍同步的亮色光晕
    const gg = c.createRadialGradient(W * .5, H * .4, 0, W * .5, H * .4, H * .7);
    gg.addColorStop(0, rgba(light, .12 + .1 * (P.pulse || 0))); gg.addColorStop(1, rgba(light, 0));
    c.fillStyle = gg; c.fillRect(0, 0, W, H);
  }

  // 注册：每个学到的效果是一个场景 learned_<id>
  function register(list) {
    EFFECTS = (list || []).filter(e => e && e.id && e.palette);
    for (const fx of EFFECTS) {
      S.SCENES[fx.id] = (c, W, H, t, P) => drawLearned(fx, c, W, H, t, P);
      if (S.FRONT_OF) S.FRONT_OF[fx.id] = fx.shape === 'dots' ? 'spark' : null;
    }
    S.LEARNED = EFFECTS;
    return EFFECTS.length;
  }
  // 按风格筛选可用的学来场景（标签 genres 包含该风格）
  function forPreset(preset) {
    return EFFECTS.filter(e => !e.tags || !e.tags.genres || e.tags.genres.includes(preset)).map(e => e.id);
  }

  S.registerLearned = register;
  S.learnedFor = forPreset;
  global.NekoStageLearned = { register, forPreset, get effects() { return EFFECTS; } };
})(window);
