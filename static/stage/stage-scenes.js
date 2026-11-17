/* =============================================================================
 * 「为她而唱」舞台运行时 · 背景场景与前景光效
 * 纯绘制层：给定时间 t 与 show.json，画出一帧背景 / 前景。没有内部状态、没有随机数。
 * 依赖：window.NekoStageU（本文件顶部定义）
 * ========================================================================== */
(function (global) {
  'use strict';

  /* ------------------------------ 通用工具 ------------------------------ */
  const U = {
    clamp: (v, a = 0, b = 1) => (v < a ? a : v > b ? b : v),
    lerp: (a, b, k) => a + (b - a) * k,
    sstep: (a, b, x) => { const t = U.clamp((x - a) / (b - a)); return t * t * (3 - 2 * t); },
    easeOut: p => 1 - Math.pow(1 - p, 3),
    easeIn: p => p * p * p,
    easeInOut: p => (p < .5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2),
    easeOutBack: p => { const c1 = 1.9, c3 = c1 + 1; return 1 + c3 * Math.pow(p - 1, 3) + c1 * Math.pow(p - 1, 2); },
    springP: p => (p <= 0 ? 0 : p >= 1 ? 1 : 1 - Math.exp(-6 * p) * Math.cos(11 * p)),
    bounceOut: p => {
      const n = 7.5625, d = 2.75;
      if (p < 1 / d) return n * p * p;
      if (p < 2 / d) return n * (p -= 1.5 / d) * p + .75;
      if (p < 2.5 / d) return n * (p -= 2.25 / d) * p + .9375;
      return n * (p -= 2.625 / d) * p + .984375;
    },
    fract: x => x - Math.floor(x),
    mulberry32: a => function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    },
    hstr: s => {
      let h = 2166136261;
      for (const c of String(s)) { h ^= c.codePointAt(0); h = Math.imul(h, 16777619) >>> 0; }
      return h >>> 0;
    },
    h01: n => U.fract(Math.sin(n * 127.1 + 311.7) * 43758.5453),
    hexRgb: h => { const n = parseInt(h.slice(1), 16); return [n >> 16 & 255, n >> 8 & 255, n & 255]; },
    rgba: (h, a) => { const c = U.hexRgb(h); return `rgba(${c[0]},${c[1]},${c[2]},${a})`; },
    charW: (c, fs) => (/[\x00-\x7f]/.test(c) ? fs * .62 : fs)
  };
  global.NekoStageU = U;

  /* ------------------------------ 背景场景 ------------------------------ */
  const SCENES = {
    bokeh(c, W, H, t, P) {
      const n = Math.round(30 * P.dens);
      for (let i = 0; i < n; i++) {
        const x = (U.h01(i * 3.1 + P.v) * 1.2 - .1) * W + Math.sin(t * .15 + i) * W * .03;
        const y = U.fract(U.h01(i * 7.7 + P.v) - t * .012 * (.5 + U.h01(i))) * H * 1.2 - H * .1;
        const r = (14 + 60 * U.h01(i * 1.3)) * H / 360 * (1 + .15 * P.pulse * (i % 3 === 0));
        const col = [P.pal.c1, P.pal.c2, P.pal.c3][i % 3];
        const a = (.18 + .4 * U.h01(i * 9)) * (.7 + .3 * Math.sin(t * .8 + i * 2));
        const g = c.createRadialGradient(x, y, 0, x, y, r);
        g.addColorStop(0, U.rgba(col, a)); g.addColorStop(.6, U.rgba(col, a * .4)); g.addColorStop(1, U.rgba(col, 0));
        c.fillStyle = g; c.beginPath(); c.arc(x, y, r, 0, 6.283); c.fill();
      }
    },
    wave(c, W, H, t, P) {
      c.lineWidth = 2 * H / 360;
      for (let j = 0; j < 9; j++) {
        c.strokeStyle = U.rgba(j % 2 ? P.pal.c1 : P.pal.c2, .35 + .35 * P.I);
        c.beginPath();
        for (let x = 0; x <= W; x += 6) {
          const y = H * (.5 + (j - 4) * .065)
            + Math.sin(x / W * 6.283 * (1 + j * .15) + t * (.5 + j * .08) * (P.bpm / 90) + j)
            * H * .06 * (.4 + P.I) * (1 + .4 * P.pulse) * Math.sin(Math.PI * x / W);
          x === 0 ? c.moveTo(x, y) : c.lineTo(x, y);
        }
        c.stroke();
      }
    },
    rings(c, W, H, t, P) {
      const cx = P.cx * W, cy = H * .45, n = Math.round(10 * P.dens);
      for (let j = 0; j < n; j++) {
        const r = U.fract(j / n + t * .05 * P.bpm / 100) * W * .75;
        c.strokeStyle = U.rgba(j % 2 ? P.pal.c1 : P.pal.c2, (1 - r / (W * .75)) * .85);
        c.lineWidth = (2 + 5 * P.pulse) * H / 360;
        c.beginPath(); c.arc(cx, cy, r, 0, 6.283); c.stroke();
      }
    },
    led(c, W, H, t, P) {
      const cols = Math.round(46 * Math.min(1.3, P.dens)), sp = W / cols, rows = Math.ceil(H / sp) + 1;
      const cx = P.cx * W, cy = H * .45, spd = 3 * P.bpm / 100;
      for (let r = 0; r < rows; r++) for (let k = 0; k < cols; k++) {
        const x = k * sp + sp / 2, y = r * sp + sp / 2;
        const d = Math.hypot(x - cx, y - cy) / W;
        let v = .5 + .5 * Math.sin(d * 16 - t * spd + (P.v > .5 ? Math.atan2(y - cy, x - cx) * 2 : 0));
        v = v * v * v * (.4 + .8 * P.I) + P.pulse * .5 * Math.max(0, 1 - d * 2) + U.h01(r * 31 + k + Math.floor(t * 5)) * .1;
        if (v < .06) continue;
        c.fillStyle = U.rgba(y / H < .5 ? P.pal.c1 : P.pal.c2, Math.min(1, v));
        const z = sp * (.3 + .35 * Math.min(1, v));
        c.fillRect(x - z / 2, y - z / 2, z, z);
      }
    },
    tunnel(c, W, H, t, P) {
      const sides = 3 + Math.floor(P.v * 4), cx = P.cx * W, cy = H * .45, n = Math.round(14 * P.dens);
      for (let j = 0; j < n; j++) {
        const z = U.fract(j / n + t * .1 * P.bpm / 100);
        const r = W * .03 * Math.pow(22, z), rot = t * .25 * (P.v > .5 ? 1 : -1) + j * .13;
        c.strokeStyle = U.rgba(j % 2 ? P.pal.c1 : P.pal.c2,
          U.sstep(0, .2, z) * (1 - U.sstep(.8, 1, z)) * (.6 + .4 * P.pulse));
        c.lineWidth = (1 + 4 * z) * H / 360 * (1 + P.pulse);
        c.beginPath();
        for (let k = 0; k <= sides; k++) {
          const a = rot + k / sides * 6.283;
          const px = cx + Math.cos(a) * r, py = cy + Math.sin(a) * r;
          k ? c.lineTo(px, py) : c.moveTo(px, py);
        }
        c.stroke();
      }
    },
    beams(c, W, H, t, P) {
      c.globalCompositeOperation = 'lighter';
      const n = Math.round(7 * P.dens);
      for (let i = 0; i < n; i++) {
        const ox = W * (i + .5) / n, a = Math.sin(t * .5 * P.bpm / 90 + i * 1.7) * .55;
        const len = H * 1.5, wd = W * .07;
        const ex = ox + Math.sin(a) * len, ey = H - Math.cos(a) * len;
        const g = c.createLinearGradient(ox, H, ex, ey);
        const col = [P.pal.c1, P.pal.c2, P.pal.c3][i % 3];
        g.addColorStop(0, U.rgba(col, .55 * (.6 + .4 * P.pulse))); g.addColorStop(1, U.rgba(col, 0));
        c.fillStyle = g;
        c.beginPath(); c.moveTo(ox - 4, H); c.lineTo(ex - wd, ey); c.lineTo(ex + wd, ey); c.lineTo(ox + 4, H); c.fill();
      }
      c.globalCompositeOperation = 'source-over';
    }
  };

  function drawScene(name, c, W, H, t, P) {
    (SCENES[name] || SCENES.bokeh)(c, W, H, t, P);
  }

  function drawBase(c, W, H, t, pal, lum, skin) {
    const Skins = global.NekoStageSkins;
    if (Skins && skin) { Skins.drawBase(c, W, H, t, skin, pal, lum); return; }
    const g = c.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, pal.bgA); g.addColorStop(1, pal.bgB);
    c.fillStyle = g; c.fillRect(0, 0, W, H);
    [pal.c1, pal.c2, pal.c3].forEach((col, k) => {
      const x = W * (.5 + .38 * Math.sin(t * .07 + k * 2.1));
      const y = H * (.5 + .32 * Math.cos(t * .05 + k * 1.7));
      const r = W * .5;
      const rg = c.createRadialGradient(x, y, 0, x, y, r);
      rg.addColorStop(0, U.rgba(col, .2 * lum)); rg.addColorStop(1, U.rgba(col, 0));
      c.fillStyle = rg; c.fillRect(0, 0, W, H);
    });
  }

  /* ------------------------------ 背景主入口 ------------------------------ */
  /**
   * v = { bgCtx, off, offCtx, W, H, t, show, cam, bar, sceneIndex, prevIndex,
   *       intensity, pulse, seed, sceneVar }
   * 返回 { scene, entry, density, blur, lum }
   */
  function drawBackground(v) {
    const { bgCtx: c, off, offCtx, W, H, t, show, cam, bar } = v;
    const scenes = show.scenes;
    const pal = show.style.palette;
    let i = v.sceneIndex;
    if (i < 0) i = scenes.length - 1;
    const sc = scenes[i];
    const prev = scenes[Math.max(0, i - 1)];

    const skin = v.skin || (global.NekoStageSkins ? global.NekoStageSkins.resolve((show.style || {}).skin) : null);
    drawBase(c, W, H, t, pal, sc.lum, skin);

    const P = {
      pal, I: v.intensity, pulse: v.pulse, bpm: show.music.bpm,
      cx: 1 - cam.cx, dens: sc.density || 1, v: v.sceneVar
    };
    // 第三十九轮：转场时长至少 1.4 s；遮罩边缘羽化（canvas blur）；转场期间旧背景慢慢推远、新背景从放大推回原位（运镜式过渡）
    const tdur = Math.max(1.4, Math.min(2.4, bar));
    const prog = U.clamp((t - sc.t0) / tdur);
    if (prog < 1 && i > 0 && prev.scene !== sc.scene && !sc.stingerCut) {
      const Pp = Object.assign({}, P, { dens: prev.density || 1, v: v.prevSceneVar });
      const ep = U.easeInOut(prog);
      c.save(); c.translate(W / 2, H / 2); c.scale(1 + .08 * ep, 1 + .08 * ep); c.translate(-W / 2, -H / 2);
      drawScene(prev.scene, c, W, H, t, Pp);
      c.restore();
      offCtx.clearRect(0, 0, W, H);
      offCtx.save(); offCtx.translate(W / 2, H / 2); offCtx.scale(1.12 - .12 * ep, 1.12 - .12 * ep); offCtx.translate(-W / 2, -H / 2);
      drawScene(sc.scene, offCtx, W, H, t, P);
      offCtx.restore();
      // 第四十九轮：遮罩改在 1/4 分辨率小画布上画（羽化也在小图上做，再放大用），
      // 以前整张背景分辨率上做 canvas blur，转场那一两秒每帧 30~90 ms → 明显卡顿
      const MK = drawBackground._mk || (drawBackground._mk = document.createElement('canvas'));
      const mw = Math.max(16, Math.round(W / 4)), mh = Math.max(9, Math.round(H / 4));
      if (MK.width !== mw || MK.height !== mh) { MK.width = mw; MK.height = mh; }
      const m = MK.getContext('2d');
      m.setTransform(1, 0, 0, 1, 0, 0); m.clearRect(0, 0, mw, mh);
      m.setTransform(mw / W, 0, 0, mh / H, 0, 0);
      m.fillStyle = '#fff';
      m.filter = 'blur(' + Math.max(1, Math.round(H * .025 * mh / H)) + 'px)';
      let e = sc.entry, cxp = (1 - cam.cx) * W;
      if (e === 'flash' || e === 'fade') e = 'split';   // 硬切/整体淡入改成「从中间展开」
      if (e === 'iris') { m.beginPath(); m.arc(cxp, H * .45, U.easeInOut(prog) * W * 1.2, 0, 6.283); m.fill(); }
      else if (e === 'sweepL') m.fillRect(0, 0, U.easeInOut(prog) * W, H);
      else if (e === 'sweepR') m.fillRect(W - U.easeInOut(prog) * W, 0, W, H);
      else if (e === 'columns') {
        const n = 12;
        for (let k = 0; k < n; k++) if (prog > U.h01(k * 3.7 + v.sceneVar) * .8) m.fillRect(k * W / n, 0, W / n + 1, H);
      } else if (e === 'flash') m.fillRect(0, 0, W, H);
      // 第三十八轮：更多背景转场（只换背景，人物和歌词不动）
      else if (e === 'sweepU') m.fillRect(0, H - U.easeInOut(prog) * H, W, H);
      else if (e === 'sweepD') m.fillRect(0, 0, W, U.easeInOut(prog) * H);
      else if (e === 'diag') { const q = U.easeInOut(prog) * (W + H) * 1.05; m.beginPath(); m.moveTo(0, 0); m.lineTo(q, 0); m.lineTo(q - H, H); m.lineTo(0, H); m.fill(); }
      else if (e === 'diamond') { const r = U.easeInOut(prog) * (W + H) * .6, cx0 = W / 2, cy0 = H / 2; m.beginPath(); m.moveTo(cx0, cy0 - r); m.lineTo(cx0 + r, cy0); m.lineTo(cx0, cy0 + r); m.lineTo(cx0 - r, cy0); m.fill(); }
      else if (e === 'blinds') { const n = 10, bh = H / n; for (let k = 0; k < n; k++) m.fillRect(0, k * bh, W, bh * U.easeInOut(prog)); }
      else if (e === 'dots') { const cs = H / 8; for (let y = 0; y <= H / cs + 1; y++) for (let x = 0; x <= W / cs + 1; x++) { const d = (x * cs / W + y * cs / H) / 2; const r = cs * .75 * U.clamp(prog * 1.8 - d * .8); if (r > 0) { m.beginPath(); m.arc(x * cs, y * cs, r, 0, 6.283); m.fill(); } } }
      else if (e === 'split') { const q = U.easeInOut(prog) * W / 2; m.fillRect(W / 2 - q, 0, q * 2, H); }
      else if (e === 'clock') { const a = U.easeInOut(prog) * 6.283; m.beginPath(); m.moveTo(W / 2, H / 2); m.arc(W / 2, H / 2, W + H, -Math.PI / 2, -Math.PI / 2 + a); m.closePath(); m.fill(); }
      else { m.fillRect(0, 0, W, H); }
      m.filter = 'none';
      offCtx.globalCompositeOperation = 'destination-in';
      offCtx.imageSmoothingEnabled = true;
      offCtx.drawImage(MK, 0, 0, W, H);
      offCtx.globalCompositeOperation = 'source-over';
      // 转场开头 15% 再叠一点整体淡入，避免第一帧突然出现一块
      c.globalAlpha = U.clamp(prog / .15);
      c.drawImage(off, 0, 0);
      c.globalAlpha = 1;
    } else {
      drawScene(sc.scene, c, W, H, t, P);
    }

    // 皮肤装饰层（贴纸/星屑/樱花/速度线）：画在背景缓冲里，位于人物之后、歌词之前
    if (skin && global.NekoStageSkins) {
      global.NekoStageSkins.drawDecor({
        ctx: c, W, H, t, skin, pal, intensity: v.intensity, pulse: v.pulse,
        seed: v.seed, decor: v.decor || []
      });
    }

    // 模糊和亮度跟着段落走：主歌朦胧、副歌「灯亮了」
    const pb = prev.blur, k = U.clamp((t - sc.t0) / Math.max(1.4, 2 * 60 / show.music.bpm));
    const blur = U.lerp(i > 0 ? pb : sc.blur, sc.blur, U.easeInOut(k));
    const lum = sc.lum * (1 + .12 * v.pulse * v.intensity);
    return { scene: sc.scene, entry: sc.entry, density: sc.density || 1, blur, lum, section: sc.section };
  }

  /* ------------------------------ 前景光效 ------------------------------ */
  function star4(c, x, y, r) {
    c.beginPath(); c.moveTo(x, y - r);
    c.quadraticCurveTo(x, y, x + r, y);
    c.quadraticCurveTo(x, y, x, y + r);
    c.quadraticCurveTo(x, y, x - r, y);
    c.quadraticCurveTo(x, y, x, y - r);
    c.fill();
  }
  function heartPath(c, x, y, r) {
    c.beginPath(); c.moveTo(x, y + r * .7);
    c.bezierCurveTo(x - r * 1.4, y - r * .2, x - r * .6, y - r * 1.2, x, y - r * .45);
    c.bezierCurveTo(x + r * .6, y - r * 1.2, x + r * 1.4, y - r * .2, x, y + r * .7);
  }

  /**
   * v = { ctx, W, H, t, show, intensity, pulse, sectionId, anchors }
   * anchors: [{t, img, x, y}] —— 由歌词层在排版后提供，意象画在对应的字上
   */
  function drawForeground(v) {
    const { ctx: c, W, H, t, show } = v;
    const pal = show.style.palette, I = v.intensity, pu = v.pulse;
    c.clearRect(0, 0, W, H);

    // 副歌：锐利的舞台光束从顶上扫下来
    if (v.sectionId === 'chorus' || v.sectionId === 'final') {
      c.globalCompositeOperation = 'lighter';
      const n = v.sectionId === 'final' ? 10 : 6;
      for (let i = 0; i < n; i++) {
        const ox = W * (i + .5) / n;
        const a = Math.sin(t * .9 * show.music.bpm / 100 + i * 1.3) * .5 + (i < n / 2 ? .2 : -.2);
        const len = H * 1.4, ex = ox + Math.sin(a) * len, ey = Math.cos(a) * len, wd = W * .035;
        const g = c.createLinearGradient(ox, 0, ex, ey);
        g.addColorStop(0, U.rgba(i % 2 ? pal.hot : '#ffffff', .22 * I * (.5 + .5 * pu)));
        g.addColorStop(1, 'rgba(0,0,0,0)');
        c.fillStyle = g;
        c.beginPath(); c.moveTo(ox - 3, 0); c.lineTo(ex - wd, ey); c.lineTo(ex + wd, ey); c.lineTo(ox + 3, 0); c.fill();
      }
      c.globalCompositeOperation = 'source-over';
    }

    // 浮尘
    const nd = Math.round(20 + 50 * I);
    for (let i = 0; i < nd; i++) {
      const x = U.fract(U.h01(i * 5.3) + Math.sin(t * .1 + i) * .02) * W;
      const y = U.fract(U.h01(i * 8.1) - t * .015 * (.4 + U.h01(i * 2))) * H;
      c.fillStyle = U.rgba(pal.c2, .25 + .35 * U.h01(i * 3 + Math.floor(t * 3)));
      c.fillRect(x, y, 2, 2);
    }

    // 意象：唱到某个字，画面跟着出现对应的图形
    const A = v.anchors || [];
    for (let j = 0; j < A.length; j++) {
      const e = A[j];
      if (e.t > t) break;
      const d = t - e.t;
      if (d > 3) continue;
      const ex = e.x, ey = e.y;
      if (e.img === 'stars') {
        c.fillStyle = '#fff';
        for (let k = 0; k < 70; k++) {
          const st = U.h01(k * 7.3 + e.t) * 1.2;
          const life = U.clamp((d - st) / 1.4);
          if (life <= 0 || life >= 1) continue;
          c.globalAlpha = Math.sin(life * Math.PI);
          star4(c, U.h01(k * 2.1 + e.t) * W, U.h01(k * 3.9 + e.t) * H * .8, (3 + 9 * U.h01(k)) * Math.sin(life * Math.PI));
        }
        c.globalAlpha = 1;
      } else if (e.img === 'flare') {
        const a = Math.exp(-d * 2.2);
        const g = c.createLinearGradient(0, ey, W, ey);
        g.addColorStop(0, 'rgba(0,0,0,0)');
        g.addColorStop(Math.max(0, Math.min(1, ex / W)), U.rgba(pal.hot, .85 * a));
        g.addColorStop(1, 'rgba(0,0,0,0)');
        c.fillStyle = g; c.fillRect(0, ey - 3 - 8 * a, W, 6 + 16 * a);
        const rg = c.createRadialGradient(ex, ey, 0, ex, ey, H * .3);
        rg.addColorStop(0, U.rgba('#ffffff', .7 * a)); rg.addColorStop(1, 'rgba(0,0,0,0)');
        c.fillStyle = rg; c.fillRect(ex - H * .3, ey - H * .3, H * .6, H * .6);
      } else if (e.img === 'heart') {
        const a = Math.exp(-d * .9) * (d < .15 ? d / .15 : 1);
        const r = H * (.12 + .02 * pu) * (1 + d * .3);
        c.strokeStyle = U.rgba(pal.c1, a); c.lineWidth = 6;
        c.shadowColor = pal.c1; c.shadowBlur = 25;
        heartPath(c, W * .5, H * .42, r * 2.2); c.stroke();
        c.shadowBlur = 0;
      } else if (e.img === 'digits') {
        c.font = `700 ${Math.round(H * .025)}px Consolas,monospace`;
        const a = Math.min(1, d * 4) * (1 - U.sstep(2, 3, d));
        for (let k = 0; k < 40; k++) {
          const x = (k + .5) / 40 * W, sp = .25 + U.h01(k) * .6;
          for (let j = 0; j < 8; j++) {
            const y = (U.fract(U.h01(k * 3 + j) + d * sp) * 1.2 - .1) * H;
            c.fillStyle = U.rgba(pal.c2, a * (j === 0 ? .9 : .35));
            c.fillText(U.h01(k * 13 + j + Math.floor(t * 8)) > .5 ? '1' : '0', x, y);
          }
        }
      }
    }
  }

  global.NekoStageScenes = { SCENES, drawScene, drawBase, drawBackground, drawForeground, star4, heartPath };
})(window);
