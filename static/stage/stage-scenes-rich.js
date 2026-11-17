/* =============================================================================
 * 「为她而唱」舞台 · 第三十三轮：高密度二次元场景（参考 VTuber 歌回 / 翻唱 MV 背景）
 * 纯程序绘制（不用外部图片素材，无版权问题），只由 t 与参数决定，没有内部状态。
 * 场景：skyline 夜城灯火 / sky 云海天空 / sakura 樱花雨 / concert 演唱会荧光棒海 /
 *       galaxy 星河 / rainwin 雨夜窗 / confetti 彩带派对 / pattern 可爱格纹贴纸墙
 * 依赖：window.NekoStageU、window.NekoStageScenes（本文件把新场景注册进 SCENES）
 * ========================================================================== */
(function (global) {
  'use strict';
  const U = global.NekoStageU;
  const S = global.NekoStageScenes;
  if (!U || !S) return;

  const h = U.h01;
  function rgba(col, a) { return U.rgba(col, U.clamp(a, 0, 1)); }
  function mix(a, b, k) {
    const x = U.hexRgb(a), y = U.hexRgb(b);
    const r = Math.round(U.lerp(x[0], y[0], k)), g = Math.round(U.lerp(x[1], y[1], k)), bb = Math.round(U.lerp(x[2], y[2], k));
    return '#' + ((1 << 24) | (r << 16) | (g << 8) | bb).toString(16).slice(1);
  }
  function vgrad(c, W, H, stops) {
    const g = c.createLinearGradient(0, 0, 0, H);
    stops.forEach(([p, col]) => g.addColorStop(p, col));
    c.fillStyle = g; c.fillRect(0, 0, W, H);
  }
  function glow(c, x, y, r, col, a) {
    const g = c.createRadialGradient(x, y, 0, x, y, r);
    g.addColorStop(0, rgba(col, a)); g.addColorStop(.45, rgba(col, a * .35)); g.addColorStop(1, rgba(col, 0));
    c.fillStyle = g; c.fillRect(x - r, y - r, r * 2, r * 2);
  }
  function bokehField(c, W, H, t, P, n, yMin, yMax, rMul) {
    for (let i = 0; i < n; i++) {
      const x = (h(i * 3.17 + P.v * 9) * 1.2 - .1) * W + Math.sin(t * .2 + i) * W * .01;
      const y = (yMin + (yMax - yMin) * h(i * 7.31 + P.v)) * H;
      const r = (6 + 26 * h(i * 1.9)) * H / 360 * rMul;
      const col = [P.pal.c1, P.pal.c2, P.pal.c3, P.pal.hot || '#ffffff'][i % 4];
      const a = (.12 + .3 * h(i * 5.3)) * (.75 + .25 * Math.sin(t * 1.3 + i * 2.1)) * (1 + .3 * P.pulse);
      glow(c, x, y, r, col, a);
    }
  }
  function star4(c, x, y, r) {
    c.beginPath(); c.moveTo(x, y - r);
    c.quadraticCurveTo(x, y, x + r, y); c.quadraticCurveTo(x, y, x, y + r);
    c.quadraticCurveTo(x, y, x - r, y); c.quadraticCurveTo(x, y, x, y - r); c.fill();
  }
  function starField(c, W, H, t, n, yMax, col) {
    for (let i = 0; i < n; i++) {
      const x = h(i * 2.71) * W, y = h(i * 5.93) * H * yMax;
      const tw = .35 + .65 * Math.abs(Math.sin(t * (.6 + h(i) * 2) + i));
      c.fillStyle = rgba(col || '#ffffff', tw * (.4 + .6 * h(i * 9.1)));
      const r = (h(i * 4.4) < .06 ? 2.2 : .9) * H / 540;
      if (r > 1.5) star4(c, x, y, r * 2.2); else c.fillRect(x, y, r * 1.6, r * 1.6);
    }
  }
  function petal(c, x, y, r, rot, col, a) {
    c.save(); c.translate(x, y); c.rotate(rot);
    c.fillStyle = rgba(col, a);
    c.beginPath(); c.moveTo(0, -r);
    c.bezierCurveTo(r * .8, -r * .6, r * .7, r * .5, 0, r);
    c.bezierCurveTo(-r * .7, r * .5, -r * .8, -r * .6, 0, -r);
    c.fill(); c.restore();
  }
  function heart(c, x, y, r) {
    c.beginPath(); c.moveTo(x, y + r * .7);
    c.bezierCurveTo(x - r * 1.4, y - r * .2, x - r * .6, y - r * 1.2, x, y - r * .45);
    c.bezierCurveTo(x + r * .6, y - r * 1.2, x + r * 1.4, y - r * .2, x, y + r * .7);
    c.fill();
  }

  const NEW = {
    // 夜城灯火：渐变夜空 + 星 + 月 + 三层楼群剪影（近大远小、窗户亮灯随机闪）+ 前景散景
    skyline(c, W, H, t, P) {
      const top = mix(P.pal.bgA || '#0b1236', '#000814', .4), mid = mix(P.pal.c2 || '#5b7bff', '#1a1550', .7);
      vgrad(c, W, H, [[0, top], [.62, mid], [1, mix(P.pal.c1 || '#ff7ab8', '#2a1f66', .55)]]);
      starField(c, W, H, t, Math.round(160 * P.dens), .55);
      const mx = W * (.18 + .64 * P.v), my = H * .16;
      glow(c, mx, my, H * .22, '#fff4d6', .35);
      c.fillStyle = 'rgba(255,248,230,.95)'; c.beginPath(); c.arc(mx, my, H * .045, 0, 6.283); c.fill();
      const layers = [[.62, .55, '#141a3a', 34], [.7, .75, '#0e1230', 26], [.8, 1, '#070a1e', 18]];
      layers.forEach(([base, sc, col, n], li) => {
        let x = -W * .05;
        let k = 0;
        while (x < W * 1.05) {
          const bw = W * (.03 + .05 * h(li * 31 + k * 1.7)) * sc;
          const bh = H * (.12 + .32 * h(li * 17 + k * 2.3)) * sc;
          const y0 = H * base - bh + H * (1 - base) * .2;
          c.fillStyle = col; c.fillRect(x, y0, bw, H - y0);
          // 窗户：小格亮灯，暖色为主、偶尔主色；少量随时间开关
          const cols = Math.max(2, Math.floor(bw / (W * .008 / sc))), rows = Math.max(3, Math.floor(bh / (H * .018 / sc)));
          const cw = bw / cols, rh = bh / rows;
          for (let r = 1; r < rows; r++) for (let q = 0; q < cols; q++) {
            const seed = li * 1000 + k * 97 + r * 13 + q;
            const on = h(seed) < .38 + .12 * Math.sin(t * .3 + seed);
            if (!on) continue;
            c.fillStyle = rgba(h(seed * 3.3) < .8 ? '#ffd98a' : (P.pal.c2 || '#8fd0ff'), (.45 + .45 * h(seed * 1.1)) * (.85 + .15 * P.pulse));
            c.fillRect(x + q * cw + cw * .25, y0 + r * rh + rh * .2, cw * .5, rh * .45);
          }
          x += bw + W * .004;
          k++;
        }
      });
      bokehField(c, W, H, t, P, Math.round(46 * P.dens), .5, 1.05, 1.6);
    },

    // 云海天空：渐变天空 + 多层蓬松积云缓慢平移 + 光束 + 飘落小光点
    sky(c, W, H, t, P) {
      const night = (P.pal.bgA || '').toLowerCase() < '#3';
      vgrad(c, W, H, night
        ? [[0, '#1b1f4b'], [.55, mix(P.pal.c2 || '#8fa8ff', '#3a3f86', .5)], [1, mix(P.pal.c1 || '#ffb3d9', '#f6c6d6', .5)]]
        : [[0, '#7fb7ff'], [.55, '#bfe0ff'], [1, mix(P.pal.c1 || '#ffd6ea', '#ffffff', .4)]]);
      glow(c, W * (.25 + .5 * P.v), H * .2, H * .6, '#fffbe6', .35 + .1 * P.pulse);
      for (let layer = 0; layer < 4; layer++) {
        const yb = H * (.35 + layer * .17), sp = (6 + layer * 8) * W / 1400;
        const n = 7 + layer * 2, scale = .6 + layer * .25;
        for (let i = 0; i < n; i++) {
          const x = U.fract(h(layer * 11 + i * 3.1) + t * sp / W) * W * 1.4 - W * .2;
          const y = yb + (h(i * 5.7 + layer) - .5) * H * .08;
          const r = H * (.05 + .05 * h(i * 2.2 + layer)) * scale;
          // 底部阴影（偏天空色）+ 顶部高光，云才有体积；每朵 12 个大小不一的圆团
          for (let pass = 0; pass < 2; pass++) {
            c.fillStyle = pass ? rgba('#ffffff', .78 + layer * .05) : rgba(night ? '#9aa8d8' : '#c9dcf5', .5 + layer * .1);
            for (let b = 0; b < 12; b++) {
              const u = h(i * 13 + b * 1.7 + layer), v2 = h(i * 7 + b * 2.9);
              const bx = x + (u - .5) * r * 4.2, by = y - Math.sin(u * Math.PI) * r * (.4 + .6 * v2) - (pass ? r * .12 : -r * .1);
              const rr = r * (.45 + .55 * v2) * (1 - Math.abs(u - .5) * .8);
              c.beginPath(); c.arc(bx, by, rr, 0, 6.283); c.fill();
            }
          }
        }
      }
      c.globalCompositeOperation = 'lighter';
      for (let i = 0; i < 6; i++) {
        const x0 = W * (.1 + .16 * i), a = .06 + .04 * Math.sin(t * .4 + i);
        const g = c.createLinearGradient(x0, 0, x0 + W * .2, H);
        g.addColorStop(0, rgba('#ffffff', a)); g.addColorStop(1, rgba('#ffffff', 0));
        c.fillStyle = g; c.beginPath(); c.moveTo(x0, 0); c.lineTo(x0 + W * .05, 0); c.lineTo(x0 + W * .3, H); c.lineTo(x0 + W * .18, H); c.fill();
      }
      c.globalCompositeOperation = 'source-over';
      starField(c, W, H, t, Math.round(50 * P.dens), 1, '#ffffff');
    },

    // 樱花雨：暖粉渐变 + 两侧枝干剪影与花团 + 三层密集花瓣（近景大、模糊感强）
    sakura(c, W, H, t, P) {
      const pink = P.pal.c1 || '#ffb3d9';
      vgrad(c, W, H, [[0, mix(pink, '#fff4fa', .55)], [.6, mix(pink, '#ffe1ef', .3)], [1, mix(P.pal.c2 || '#b9e2ff', '#ffffff', .4)]]);
      glow(c, W * .5, H * .35, W * .6, '#ffffff', .4);
      // 枝干 + 花团（左右上角）
      [[0, 1], [W, -1]].forEach(([bx, dir], side) => {
        c.strokeStyle = 'rgba(92,60,70,.55)'; c.lineCap = 'round';
        for (let b = 0; b < 5; b++) {
          c.lineWidth = (10 - b * 1.6) * H / 540;
          const y0 = H * (.02 + b * .07), len = W * (.18 + .1 * h(b + side * 7));
          c.beginPath(); c.moveTo(bx, y0);
          c.quadraticCurveTo(bx + dir * len * .5, y0 + H * .08, bx + dir * len, y0 + H * (.03 + .06 * h(b * 2 + side)));
          c.stroke();
          for (let f = 0; f < 14; f++) {
            const u = f / 13, fx = bx + dir * len * u + (h(f * 3 + b) - .5) * W * .03;
            const fy = y0 + H * .05 * Math.sin(u * 3) + (h(f * 7 + b) - .5) * H * .05;
            glow(c, fx, fy, H * (.03 + .03 * h(f + b)), pink, .6);
            c.fillStyle = rgba('#fff0f6', .85); c.beginPath(); c.arc(fx, fy, H * .006, 0, 6.283); c.fill();
          }
        }
      });
      const N = Math.round(150 * P.dens);
      for (let i = 0; i < N; i++) {
        const layer = i % 3, sp = (.025 + .025 * layer) * (.7 + .6 * h(i));
        const y = (U.fract(h(i * 7.7) + t * sp) * 1.15 - .08) * H;
        const x = (h(i * 3.3) * 1.2 - .1) * W + Math.sin(t * (.6 + h(i)) + i) * W * .03 * (1 + layer);
        const r = (3 + 4 * layer + 3 * h(i * 5)) * H / 540;
        petal(c, x, y, r, t * (.8 + h(i * 2)) + i, i % 5 ? pink : '#ffffff', .55 + .25 * layer);
      }
    },

    // 演唱会：深色场馆 + 顶部桁架与追光 + 底部荧光棒海（成千上万个光点按拍子摇）+ 观众剪影
    concert(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#05030f'], [.6, mix(P.pal.bgA || '#120536', '#000000', .2)], [1, '#020108']]);
      const beat = 60 / (P.bpm || 120), ph = (t / beat) * Math.PI;
      // 背后大屏光晕
      glow(c, W * .5, H * .38, W * .45, P.pal.c2 || '#4ef0ff', .22 + .12 * P.pulse);
      // 追光
      c.globalCompositeOperation = 'lighter';
      for (let i = 0; i < 8; i++) {
        const ox = W * (.06 + .125 * i), a = Math.sin(t * .7 + i * 1.3) * .45;
        const ex = ox + Math.sin(a) * H * 1.2, ey = Math.cos(a) * H * 1.2;
        const g = c.createLinearGradient(ox, 0, ex, ey);
        const col = [P.pal.c1, P.pal.c2, P.pal.c3, '#ffffff'][i % 4] || '#ffffff';
        g.addColorStop(0, rgba(col, .35 * (.6 + .4 * P.pulse))); g.addColorStop(1, rgba(col, 0));
        c.fillStyle = g; c.beginPath(); c.moveTo(ox - 2, 0); c.lineTo(ex - W * .05, ey); c.lineTo(ex + W * .05, ey); c.lineTo(ox + 2, 0); c.fill();
      }
      c.globalCompositeOperation = 'source-over';
      // 桁架
      c.strokeStyle = 'rgba(160,170,200,.35)'; c.lineWidth = 2 * H / 540;
      c.beginPath(); c.moveTo(0, H * .05); c.lineTo(W, H * .05); c.moveTo(0, H * .08); c.lineTo(W, H * .08); c.stroke();
      for (let x = 0; x < W; x += W / 40) { c.beginPath(); c.moveTo(x, H * .05); c.lineTo(x + W / 80, H * .08); c.stroke(); }
      // 荧光棒海：远处小而密，近处大而稀；跟拍左右摇
      c.globalCompositeOperation = 'lighter';
      const N = Math.round(900 * P.dens);
      const cols = [P.pal.c1 || '#ff4fd8', P.pal.c2 || '#4ef0ff', P.pal.c3 || '#c6ff4a', P.pal.hot || '#ffe066'];
      const colPick = Math.floor(t / (beat * 16)) % 4;
      for (let i = 0; i < N; i++) {
        const depth = h(i * 1.31);                    // 0 远 → 1 近
        const y = H * (.62 + .36 * depth);
        const x = (h(i * 7.07) * 1.1 - .05) * W;
        const sway = Math.sin(ph + h(i * 3.3) * .8) * (.25 + .2 * P.I);
        const len = H * (.012 + .045 * depth * depth);
        const col = h(i * 9.9) < .7 ? cols[colPick] : cols[i % 4];
        c.strokeStyle = rgba(col, .45 + .4 * depth);
        c.lineWidth = (1 + 3.5 * depth) * H / 540;
        c.beginPath(); c.moveTo(x, y); c.lineTo(x + Math.sin(sway) * len, y - Math.cos(sway) * len); c.stroke();
      }
      c.globalCompositeOperation = 'source-over';
      // 最前排观众剪影
      c.fillStyle = '#020108';
      for (let i = 0; i < 26; i++) {
        const x = (i + h(i)) / 26 * W, r = H * (.035 + .02 * h(i * 3));
        const bob = Math.abs(Math.sin(ph + i)) * H * .006;
        c.beginPath(); c.arc(x, H * .96 - bob, r, 0, 6.283); c.fill();
        c.fillRect(x - r * 1.4, H * .96 - bob, r * 2.8, H * .1);
      }
    },

    // 星河：深空 + 斜向银河带（大量细星 + 星云光晕）+ 流星
    galaxy(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#02030c'], [.5, mix(P.pal.bgA || '#0b1236', '#050a24', .5)], [1, '#090418']]);
      starField(c, W, H, t, Math.round(260 * P.dens), 1);
      const ang = -.45 + P.v * .3;
      c.save(); c.translate(W / 2, H / 2); c.rotate(ang);
      for (let i = 0; i < 40; i++) {
        const x = (h(i * 3.7) - .5) * W * 1.4, y = (h(i * 5.1) - .5) * H * .22;
        glow(c, x, y, H * (.1 + .16 * h(i * 2)), [P.pal.c1, P.pal.c2, P.pal.c3][i % 3] || '#8fd0ff', .26);
      }
      const core = c.createLinearGradient(0, -H * .12, 0, H * .12);
      core.addColorStop(0, 'rgba(255,255,255,0)'); core.addColorStop(.5, 'rgba(255,240,250,.22)'); core.addColorStop(1, 'rgba(255,255,255,0)');
      c.fillStyle = core; c.fillRect(-W * .8, -H * .12, W * 1.6, H * .24);
      for (let i = 0; i < Math.round(1400 * P.dens); i++) {
        const x = (h(i * 1.37) - .5) * W * 1.5, y = (h(i * 2.91) + h(i * 4.1) + h(i * 8.3) - 1.5) * H * .14;
        c.fillStyle = rgba(i % 7 ? '#ffffff' : (P.pal.c2 || '#8fd0ff'), .3 + .65 * h(i * 6.6) * (.7 + .3 * Math.sin(t * 2 + i)));
        const z = h(i * 3.3) < .05 ? 2.4 : 1.3;
        c.fillRect(x, y, z, z);
      }
      c.restore();
      for (let k = 0; k < 3; k++) {
        const cyc = 4 + k * 1.7, u = U.fract(t / cyc + k * .37);
        if (u > .25) continue;
        const p = u / .25, x0 = W * (.2 + .6 * h(Math.floor(t / cyc) + k)), y0 = H * .1;
        const x = x0 + p * W * .3, y = y0 + p * H * .25;
        const g = c.createLinearGradient(x, y, x - W * .08, y - H * .07);
        g.addColorStop(0, rgba('#ffffff', .9 * (1 - p))); g.addColorStop(1, rgba('#ffffff', 0));
        c.strokeStyle = g; c.lineWidth = 2; c.beginPath(); c.moveTo(x, y); c.lineTo(x - W * .08, y - H * .07); c.stroke();
      }
    },

    // 雨夜窗：模糊城市光斑 + 雨滴下滑（带尾迹）+ 玻璃雾气
    rainwin(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#0b1020'], [1, mix(P.pal.bgA || '#101830', '#1a2240', .5)]]);
      bokehField(c, W, H, t, P, Math.round(70 * P.dens), .1, 1, 2.4);
      for (let i = 0; i < 14; i++) {   // 远处霓虹招牌（模糊的竖条和方块光）
        const x = h(i * 5.1) * W, y = H * (.45 + .4 * h(i * 2.7)), w = W * (.015 + .03 * h(i)), hh = H * (.06 + .12 * h(i * 3.3));
        const col = [P.pal.c1, P.pal.c2, P.pal.hot, '#ffd98a'][i % 4] || '#ff7ab8';
        glow(c, x, y, Math.max(w, hh) * 1.4, col, .3);
        c.fillStyle = rgba(col, .35); c.fillRect(x - w / 2, y - hh / 2, w, hh);
      }
      c.fillStyle = 'rgba(200,220,255,.08)'; c.fillRect(0, 0, W, H);
      const N = Math.round(140 * P.dens);
      for (let i = 0; i < N; i++) {
        const sp = .05 + .12 * h(i * 2.2), x = h(i * 3.9) * W;
        const y = (U.fract(h(i * 7.3) + t * sp) * 1.2 - .1) * H;
        const r = (1.2 + 2.6 * h(i * 5.5)) * H / 540;
        c.strokeStyle = 'rgba(210,230,255,.18)'; c.lineWidth = r * .8;
        c.beginPath(); c.moveTo(x, y); c.lineTo(x + Math.sin(i) * 2, y - H * .05 * sp * 6); c.stroke();
        c.fillStyle = 'rgba(230,240,255,.55)'; c.beginPath(); c.arc(x, y, r, 0, 6.283); c.fill();
        c.fillStyle = 'rgba(255,255,255,.8)'; c.fillRect(x - r * .4, y - r * .5, r * .5, r * .5);
      }
      for (let i = 0; i < Math.round(260 * P.dens); i++) {
        const x = h(i * 1.9) * W, y = h(i * 8.3) * H, r = (.5 + 1.2 * h(i)) * H / 540;
        c.fillStyle = 'rgba(220,235,255,.35)'; c.fillRect(x, y, r, r);
      }
    },

    // 彩带派对：亮色渐变 + 密集彩纸（翻转的小矩形）+ 蛇形彩带 + 气球光点
    confetti(c, W, H, t, P) {
      const cols = [P.pal.c1 || '#ff7ab8', P.pal.c2 || '#7ad7ff', P.pal.c3 || '#ffe066', P.pal.hot || '#b07aff', '#ffffff'];
      vgrad(c, W, H, [[0, mix(cols[1], '#ffffff', .55)], [1, mix(cols[0], '#ffffff', .45)]]);
      glow(c, W * .5, H * .45, W * .55, '#ffffff', .45);
      for (let i = 0; i < 10; i++) {
        c.strokeStyle = rgba(cols[i % 5], .55); c.lineWidth = (4 + 3 * h(i)) * H / 540;
        c.beginPath();
        const y0 = (U.fract(h(i * 4.1) + t * .02) * 1.3 - .15) * H, x0 = h(i * 2.7) * W;
        for (let s = 0; s <= 24; s++) {
          const u = s / 24, x = x0 + (u - .5) * W * .25, y = y0 + Math.sin(u * 9 + t * 2 + i) * H * .03 + u * H * .1;
          s ? c.lineTo(x, y) : c.moveTo(x, y);
        }
        c.stroke();
      }
      const N = Math.round(520 * P.dens);
      for (let i = 0; i < N; i++) {
        const sp = .04 + .06 * h(i * 1.3);
        const y = (U.fract(h(i * 6.1) + t * sp) * 1.15 - .08) * H;
        const x = (h(i * 2.3) * 1.1 - .05) * W + Math.sin(t * 1.5 + i) * W * .015;
        const w = (6 + 9 * h(i * 4.4)) * H / 540, fl = Math.abs(Math.sin(t * (3 + 4 * h(i)) + i));
        c.save(); c.translate(x, y); c.rotate(i + t * (1 + h(i)));
        c.fillStyle = rgba(cols[i % 5], .85);
        c.fillRect(-w / 2, -w * .3 * fl, w, w * .6 * fl + .5);
        c.restore();
      }
    },

    // 可爱格纹贴纸墙：大格子 + 斜纹 + 满屏小贴纸（爱心/星星/音符）缓慢斜向平移
    pattern(c, W, H, t, P) {
      const a = mix(P.pal.c1 || '#ffb3d9', '#ffffff', .55), b = mix(P.pal.c2 || '#b9e2ff', '#ffffff', .45);
      c.fillStyle = a; c.fillRect(0, 0, W, H);
      const cs = H / 7, off = (t * H * .02) % (cs * 2);
      c.fillStyle = b;
      for (let y = -2; y < 9; y++) for (let x = -2; x < W / cs + 2; x++) {
        if ((x + y) % 2) continue;
        c.fillRect(x * cs + off, y * cs + off, cs, cs);
      }
      c.fillStyle = 'rgba(255,255,255,.22)';
      for (let x = -H; x < W + H; x += H * .06) { c.beginPath(); c.moveTo(x, 0); c.lineTo(x + H * .02, 0); c.lineTo(x + H * .02 - H, H); c.lineTo(x - H, H); c.fill(); }
      const N = Math.round(90 * P.dens);
      for (let i = 0; i < N; i++) {
        const x = U.fract(h(i * 3.3) + t * .01) * W * 1.1 - W * .05, y = U.fract(h(i * 5.9) + t * .008) * H * 1.1 - H * .05;
        const r = (6 + 10 * h(i * 2)) * H / 540 * (1 + .15 * P.pulse * (i % 3 === 0));
        const col = [P.pal.hot || '#ff6fae', '#ffffff', P.pal.c1 || '#ff9ccc', P.pal.c3 || '#ffe066'][i % 4];
        c.fillStyle = 'rgba(255,255,255,.9)';
        if (i % 3 === 0) { heart(c, x, y, r * 1.25); c.fillStyle = rgba(col, .95); heart(c, x, y, r); }
        else if (i % 3 === 1) { star4(c, x, y, r * 1.5); c.fillStyle = rgba(col, .95); star4(c, x, y, r * 1.15); }
        else {
          c.fillStyle = rgba(col, .9); c.beginPath(); c.arc(x, y + r * .6, r * .45, 0, 6.283); c.fill();
          c.fillRect(x + r * .3, y - r * .9, r * .16, r * 1.5);
        }
      }
    }
  };

  Object.assign(S.SCENES, NEW);
  S.RICH_SCENES = Object.keys(NEW);
})(window);
