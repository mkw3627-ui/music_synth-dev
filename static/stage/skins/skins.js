/* =============================================================================
 * 「为她而唱」舞台 · 二次元皮肤（T2-B 第 10 维外观）
 *
 * 四套皮肤：anime_pastel / anime_night / anime_neon / anime_ink。
 * 皮肤只改「底子」：背景底色、光晕、装饰层、暗角强度；歌曲自己的
 * show.style.palette 仍叠加在皮肤基调之上，所以同一皮肤下不同歌仍有各自色调。
 *
 * 纯函数：所有随机量来自 h01(seed/t)，不调用 Math.random，同一 t 画面一致。
 * 依赖 window.NekoStageU。
 * ========================================================================== */
(function (global) {
  'use strict';
  // 懒取 NekoStageU：本文件可以排在 stage-scenes.js 之前加载
  const U = {
    get rgba() { return global.NekoStageU.rgba; },
    get h01() { return global.NekoStageU.h01; },
    get fract() { return global.NekoStageU.fract; }
  };

  const SKINS = {
    anime_pastel: {
      id: 'anime_pastel', label: '粉彩',
      base: ['#fff2fa', '#ffe4f4', '#eaf3ff'],
      glow: ['#ffb3d9', '#b9e2ff', '#ffe9a8'],
      vignette: 'rgba(120, 60, 110, .28)',
      band: ['#fff6fb', '#f2e9ff'],
      stickers: 'hearts',
      halftone: .5, light: true
    },
    anime_night: {
      id: 'anime_night', label: '星夜',
      base: ['#0b1236', '#1a1550', '#2a1f66'],
      glow: ['#5b7bff', '#8fd0ff', '#ffcf8a'],
      vignette: 'rgba(2, 4, 18, .58)',
      band: ['#0d143c', '#221a5c'],
      stickers: 'stars',
      halftone: .22
    },
    anime_neon: {
      id: 'anime_neon', label: '霓虹',
      base: ['#120536', '#240a55', '#3c1170'],
      glow: ['#4ef0ff', '#ff4fd8', '#c6ff4a'],
      vignette: 'rgba(8, 0, 30, .5)',
      band: ['#15063e', '#2c0d62'],
      stickers: 'spark',
      halftone: .08
    },
    anime_ink: {
      id: 'anime_ink', label: '淡墨',
      base: ['#fbf7ee', '#f2ecdc', '#e8e0cf'],
      glow: ['#6f7f96', '#c9a9b8', '#8a9b86'],
      vignette: 'rgba(90, 70, 60, .22)',
      band: ['#fbf7ee', '#e6ddc9'],
      stickers: 'sakura',
      halftone: .3, light: true
    }
  };
  const FALLBACK = 'anime_night';

  function resolve(name) {
    const id = String(name || '');
    if (SKINS[id]) return SKINS[id];
    if (global.console && !resolve._warned) {
      resolve._warned = true;
      console.warn('[NekoStage] 未知皮肤 "' + id + '"，回退 ' + FALLBACK);
    }
    return SKINS[FALLBACK];
  }

  /* ------------------------------ 底色 ------------------------------ */
  /* 性能约定（契约 §3-8：1600×900 平均帧耗时增幅 ≤20%）：
     底色层原来是 6 次全画布径向渐变 + 上千次 arc，单帧多出 ~0.37ms。
     改法不改变画面构成，只改画法：
       1) 6 团柔和径向光晕直接画在背景缓冲上（不再另开离屏层，实测离屏
          反而多 ~0.06ms 跨 canvas 同步）；
       2) 半调点阵收成一条 Path2D，一次 fill（原来是每个点一次 fill），
          并按「点阵相位」缓存，相位 floor(t*.3) 不变时直接复用；
       3) 基色竖渐变仍是 1 次全画布 fill（P1 本来就有这一次）。
     全部只依赖 (t, skin, palette)，不含历史状态，仍是 t 的纯函数。 */
  let htCache = { key: '', path: null };

  /* 光晕直接画在背景缓冲上。实测：另开一张离屏 canvas 再贴回来，无论离屏
     多大都会多出 ~0.06ms 的跨 canvas 同步，反而比直接画 6 团径向渐变贵，
     所以这里不分层。 */
  function drawGlows(c, W, H, t, skin, pal, lum) {
    // 皮肤自己的大光晕（缓慢游动）
    for (let k = 0; k < 3; k++) {
      const x = W * (.5 + .34 * Math.sin(t * .06 + k * 2.3));
      const y = H * (.46 + .28 * Math.cos(t * .045 + k * 1.7));
      const rg = c.createRadialGradient(x, y, 0, x, y, W * .48);
      const col = skin.glow[k % skin.glow.length];
      rg.addColorStop(0, U.rgba(col, .16 * (.7 + .3 * lum)));
      rg.addColorStop(1, U.rgba(col, 0));
      c.fillStyle = rg; c.fillRect(0, 0, W, H);
    }
    // 歌曲调色板叠加（在皮肤基调上生效）
    [pal.c1, pal.c2, pal.c3].forEach((col, k) => {
      const x = W * (.5 + .38 * Math.sin(t * .07 + k * 2.1));
      const y = H * (.5 + .32 * Math.cos(t * .05 + k * 1.7));
      const rg = c.createRadialGradient(x, y, 0, x, y, W * .5);
      rg.addColorStop(0, U.rgba(col, .16 * lum)); rg.addColorStop(1, U.rgba(col, 0));
      c.fillStyle = rg; c.fillRect(0, 0, W, H);
    });
    if (skin.id === 'anime_ink') {
      // 淡墨：三团晕开的墨色，压在纸面上
      for (let k = 0; k < 3; k++) {
        const x = W * (.25 + .3 * U.h01(k * 7 + 2)) + Math.sin(t * .03 + k) * W * .02;
        const y = H * (.3 + .45 * U.h01(k * 11 + 5));
        const r = W * (.14 + .1 * U.h01(k * 3 + 9));
        const rg = c.createRadialGradient(x, y, 0, x, y, r);
        rg.addColorStop(0, 'rgba(46, 54, 70, .13)');
        rg.addColorStop(.6, 'rgba(46, 54, 70, .05)');
        rg.addColorStop(1, 'rgba(46, 54, 70, 0)');
        c.fillStyle = rg; c.fillRect(0, 0, W, H);
      }
    }
  }

  // 半调点阵：一条路径一次 fill，按相位缓存
  function halftonePath(skin, W, H, t) {
    const ph = Math.floor(t * .3);
    const key = skin.id + '|' + W + 'x' + H + '|' + ph;
    if (htCache.key === key) return htCache.path;
    const step = 20, n = Math.ceil(W / step), m = Math.ceil(H / step);
    const path = new Path2D();
    for (let j = 0; j < m; j++) for (let i = 0; i < n; i++) {
      if (U.h01(i * 3.3 + j * 7.7 + ph) > skin.halftone + .15) continue;
      const x = i * step + (j % 2) * step / 2, y = j * step;
      path.moveTo(x + 2, y);
      path.arc(x, y, 2, 0, 6.283);
    }
    htCache = { key, path };
    return path;
  }

  /* 夜空星点：原来是 90 次「拼 rgba 字符串 + 改 fillStyle + fillRect」，单帧
     90 次状态切换，实测占 0.16ms。现在几何量与 alpha 字符串都只算一次，
     闪烁亮度量化成 8 档，逐帧按档归类后连续 fillRect（fillStyle 切换 ≤16 次）。
     量化只影响闪烁颗粒度（8 档），与 t 仍是确定关系。 */
  const STAR_LEVELS = 8;
  const STAR_A_MIN = .10, STAR_A_SPAN = .23;
  let starField = { key: '', list: null, alpha: null };

  function nightStarField(W, H) {
    const key = W + 'x' + H;
    if (starField.key === key) return starField;
    const list = [];
    for (let i = 0; i < 90; i++) {
      list.push({
        x: U.h01(i * 2.7 + 11) * W,
        y: U.h01(i * 5.9 + 3) * H * .92,
        r: i % 9 === 0 ? 2.2 : 1.2,
        spd: .6 + U.h01(i) * 1.4,
        ph: i,
        gold: i % 5 === 0
      });
    }
    const alpha = { w: [], g: [] };
    for (let k = 0; k < STAR_LEVELS; k++) {
      const a = STAR_A_MIN + STAR_A_SPAN * k / (STAR_LEVELS - 1);
      alpha.w.push(U.rgba('#ffffff', a));
      alpha.g.push(U.rgba('#ffe9a8', a));
    }
    starField = { key, list, alpha };
    return starField;
  }

  /* 逐帧只做整数分档 + 复用桶数组，不新建 Path2D、不拼字符串：
     先按 16 桶把星归类，再每桶设一次 fillStyle 连续 fillRect，
     fillStyle 切换从 90 次降到 ≤16 次。 */
  const starBuckets = (() => {
    const b = [];
    for (let k = 0; k < STAR_LEVELS * 2; k++) b.push({ n: 0, xs: new Float32Array(90), ys: new Float32Array(90), rs: new Float32Array(90) });
    return b;
  })();

  function drawNightStars(c, W, H, t) {
    const F = nightStarField(W, H);
    for (let k = 0; k < starBuckets.length; k++) starBuckets[k].n = 0;
    for (let i = 0; i < F.list.length; i++) {
      const s = F.list[i];
      const tw = .35 + .55 * (.5 + .5 * Math.sin(t * s.spd + s.ph));
      let lv = Math.round((.35 * tw - STAR_A_MIN) / STAR_A_SPAN * (STAR_LEVELS - 1));
      lv = lv < 0 ? 0 : (lv > STAR_LEVELS - 1 ? STAR_LEVELS - 1 : lv);
      const b = starBuckets[(s.gold ? STAR_LEVELS : 0) + lv];
      const n = b.n++;
      b.xs[n] = s.x; b.ys[n] = s.y; b.rs[n] = s.r;
    }
    for (let k = 0; k < starBuckets.length; k++) {
      const b = starBuckets[k];
      if (!b.n) continue;
      c.fillStyle = k < STAR_LEVELS ? F.alpha.w[k] : F.alpha.g[k - STAR_LEVELS];
      for (let n = 0; n < b.n; n++) c.fillRect(b.xs[n], b.ys[n], b.rs[n], b.rs[n]);
    }
  }

  function drawBase(c, W, H, t, skin, pal, lum) {
    const L = Number(lum) || 1;
    const g = c.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, skin.base[0]); g.addColorStop(.55, skin.base[1]); g.addColorStop(1, skin.base[2]);
    c.fillStyle = g; c.fillRect(0, 0, W, H);

    drawGlows(c, W, H, t, skin, pal, L);

    if (skin.halftone > 0) {
      c.fillStyle = U.rgba(skin.id === 'anime_ink' ? '#8a7c66' : '#ffffff', .05 + .07 * skin.halftone);
      c.fill(halftonePath(skin, W, H, t));
    }

    if (skin.id === 'anime_night') drawNightStars(c, W, H, t);
  }

  /* ------------------------------ 装饰层 ------------------------------ */
  function star(c, x, y, r, col, a) {
    c.fillStyle = U.rgba(col, a);
    c.beginPath();
    c.moveTo(x, y - r); c.quadraticCurveTo(x, y, x + r, y);
    c.quadraticCurveTo(x, y, x, y + r); c.quadraticCurveTo(x, y, x - r, y);
    c.quadraticCurveTo(x, y, x, y - r); c.fill();
  }
  function heart(c, x, y, r, col, a) {
    c.fillStyle = U.rgba(col, a);
    c.beginPath(); c.moveTo(x, y + r * .7);
    c.bezierCurveTo(x - r * 1.4, y - r * .2, x - r * .6, y - r * 1.2, x, y - r * .45);
    c.bezierCurveTo(x + r * .6, y - r * 1.2, x + r * 1.4, y - r * .2, x, y + r * .7);
    c.fill();
  }
  function petal(c, x, y, r, rot, col, a) {
    c.save(); c.translate(x, y); c.rotate(rot);
    c.fillStyle = U.rgba(col, a);
    c.beginPath(); c.ellipse(0, 0, r * .55, r, 0, 0, 6.283); c.fill();
    c.restore();
  }

  /**
   * v = { ctx, W, H, t, skin, pal, intensity, pulse, seed, decor[] }
   * decor 来自 typeface.rules.decor；没有 sticker 时只保留很小的空气感闪光。
   */
  function drawDecor(v) {
    const { ctx: c, W, H, t, skin, pal, intensity: I, pulse: pu, seed } = v;
    const decor = v.decor || [];
    const sticker = decor.indexOf('sticker') >= 0;
    const glow = decor.indexOf('glow') >= 0 || decor.indexOf('softglow') >= 0 || decor.indexOf('soft_glow') >= 0;
    const hot = (pal && pal.hot) || '#ffe066';
    const c1 = (pal && pal.c1) || '#ffb3d9';
    const c2 = (pal && pal.c2) || '#b9e2ff';

    if (skin.stickers === 'hearts') {
      const n = sticker ? 10 : 5;
      for (let i = 0; i < n; i++) {
        const x = U.h01(i * 3.7 + seed) * W;
        const y = (U.fract(U.h01(i * 7.1 + seed) - t * .012 * (.4 + U.h01(i * 2))) * 1.1 - .05) * H;
        heart(c, x, y, (5 + 9 * U.h01(i * 5.3)) * H / 360, i % 2 ? c1 : hot, .16 + .12 * pu);
      }
      if (glow) for (let i = 0; i < 12; i++) {
        const x = U.h01(i * 2.3 + 7) * W, y = U.h01(i * 9.1 + 1) * H;
        const a = (.25 + .35 * Math.hypot(.5 - x / W, .5 - y / H)) * (.4 + .6 * Math.sin(t * 1.2 + i) ** 2);
        star(c, x, y, 3 + 4 * U.h01(i * 4.1), i % 3 ? '#ffffff' : hot, .2 * a);
      }
    } else if (skin.stickers === 'stars') {
      for (let i = 0; i < 14; i++) {
        const x = U.h01(i * 3.3 + seed) * W;
        const y = U.fract(U.h01(i * 6.7 + seed * .7) + t * .006) * H;
        const tw = .5 + .5 * Math.sin(t * (1 + U.h01(i) * 2) + i * 1.7);
        star(c, x, y, (2 + 3 * U.h01(i * 8.1)) * H / 360, i % 4 ? '#ffffff' : hot, .18 + .25 * tw);
      }
    } else if (skin.stickers === 'spark') {
      // 霓虹：副歌速度线 + 细碎闪光，干净不发灰
      if (I > .55) {
        const n = 14;
        for (let i = 0; i < n; i++) {
          const y0 = U.h01(i * 2.9) * H;
          const len = W * (.08 + .18 * U.h01(i * 4.7)) * I;
          const x = U.fract(-t * (.5 + U.h01(i) * .8) + U.h01(i * 6.1)) * W;
          c.strokeStyle = U.rgba(i % 2 ? c2 : '#ffffff', .18 * I);
          c.lineWidth = .8 + .8 * U.h01(i * 3.1);
          c.beginPath(); c.moveTo(x, y0); c.lineTo(x + len, y0); c.stroke();
        }
      }
      for (let i = 0; i < 16; i++) {
        const x = U.h01(i * 5.7 + seed) * W, y = U.h01(i * 8.3 + seed) * H;
        star(c, x, y, (2 + 5 * U.h01(i * 1.9)) * H / 360, i % 3 ? c1 : '#ffffff',
          .18 + .3 * Math.max(0, Math.sin(t * 2 + i)) * (.4 + .6 * pu));
      }
    } else if (skin.stickers === 'sakura') {
      const n = 9;
      for (let i = 0; i < n; i++) {
        const x = U.fract(U.h01(i * 4.3 + seed) + Math.sin(t * .25 + i) * .04) * W;
        const y = U.fract(U.h01(i * 7.9 + seed * .3) + t * .018 * (.6 + U.h01(i))) * H;
        petal(c, x, y, (6 + 7 * U.h01(i * 2.2)) * H / 360, t * .6 + i, i % 2 ? '#f7c6d4' : '#e8b7c8', .5);
      }
    }
  }

  global.NekoStageSkins = { SKINS, FALLBACK, resolve, drawBase, drawDecor };
})(window);
