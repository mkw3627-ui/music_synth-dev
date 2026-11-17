/* =============================================================================
 * 「为她而唱」舞台 · 第三十六轮：再加 6 个高密度场景 + 人物前景层粒子
 * 纯程序绘制（不用外部素材），只由 t 与参数决定。
 * 场景：aurora 极光雪原 / sunset 黄昏海面 / neoncity 霓虹街道 / starstage 星光舞台（台阶+灯架）/
 *       bubbles 梦幻泡泡 / lantern 灯笼夜祭
 * 前景粒子（盖在人物之上、很稀疏、半透明，营造景深）：drawFront(ctx, W, H, t, kind, P)
 *   kind：petal 花瓣 / snow 雪 / spark 金粉 / bubble 泡泡 / note 音符
 * ========================================================================== */
(function (global) {
  'use strict';
  const U = global.NekoStageU;
  const S = global.NekoStageScenes;
  if (!U || !S) return;
  const h = U.h01;
  const rgba = (col, a) => U.rgba(col, U.clamp(a, 0, 1));
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
  function stars(c, W, H, t, n, yMax) {
    for (let i = 0; i < n; i++) {
      const x = h(i * 2.71) * W, y = h(i * 5.93) * H * yMax;
      const tw = .35 + .65 * Math.abs(Math.sin(t * (.6 + h(i) * 2) + i));
      c.fillStyle = 'rgba(255,255,255,' + (tw * (.35 + .6 * h(i * 9.1))).toFixed(3) + ')';
      const r = (h(i * 4.4) < .07 ? 2 : .9) * H / 540;
      c.fillRect(x, y, r * 1.6, r * 1.6);
    }
  }

  const NEW = {
    // 极光雪原：极光帘幕 + 远山剪影 + 雪地 + 小松树
    aurora(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#050a1f'], [.55, '#0d2240'], [1, '#1b3550']]);
      stars(c, W, H, t, Math.round(160 * P.dens), .6);
      for (let b = 0; b < 3; b++) {
        const col = [P.pal.c1 || '#6fffd2', P.pal.c2 || '#9a7bff', '#5effa0'][b];
        c.save(); c.globalCompositeOperation = 'lighter';
        for (let x = 0; x < W; x += W / 120) {
          const y0 = H * (.12 + .08 * b) + Math.sin(x / W * 6 + t * .4 + b * 2) * H * .06;
          const len = H * (.22 + .1 * Math.sin(x / W * 11 + t * .7 + b));
          const g = c.createLinearGradient(0, y0, 0, y0 + len);
          const a = (.10 + .08 * Math.sin(x / W * 17 + t * 1.3 + b)) * (1 + .4 * P.pulse);
          g.addColorStop(0, rgba(col, 0)); g.addColorStop(.3, rgba(col, a)); g.addColorStop(1, rgba(col, 0));
          c.fillStyle = g; c.fillRect(x, y0, W / 110, len);
        }
        c.restore();
      }
      c.fillStyle = '#0a1426';
      c.beginPath(); c.moveTo(0, H * .7);
      for (let x = 0; x <= W; x += W / 24) c.lineTo(x, H * (.6 + .08 * h(x * .013 + 2)));
      c.lineTo(W, H); c.lineTo(0, H); c.fill();
      vgrad(c, W, H * 1, [[0, 'rgba(0,0,0,0)'], [.78, 'rgba(0,0,0,0)'], [.8, 'rgba(210,230,255,.55)'], [1, 'rgba(240,248,255,.85)']]);
      c.fillStyle = '#0c1a2c';
      for (let i = 0; i < 18; i++) {
        const x = h(i * 3.9) * W, s = H * (.05 + .06 * h(i * 1.7)), y = H * (.78 + .03 * h(i * 6.1));
        c.beginPath(); c.moveTo(x, y - s * 2); c.lineTo(x - s * .6, y); c.lineTo(x + s * .6, y); c.fill();
      }
    },

    // 黄昏海面：大落日 + 云带 + 波光粼粼的海 + 远处小岛
    sunset(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#2b1b4f'], [.35, '#c4527a'], [.55, '#ffad6b'], [.62, '#ffd59a'], [.63, '#5b4a8a'], [1, '#1e2550']]);
      const sx = W * (.5 + .1 * (P.cx - .5)), sy = H * .55;
      glow(c, sx, sy, H * .5, '#ffcf8a', .55 + .2 * P.pulse);
      c.fillStyle = '#fff1c9'; c.beginPath(); c.arc(sx, sy, H * .12, Math.PI, 0); c.fill();
      for (let i = 0; i < 7; i++) {
        const y = H * (.12 + i * .055), x = ((h(i * 2.3) * 1.4 - .2) * W + t * W * .006 * (1 + i % 3)) % (W * 1.4) - W * .2;
        c.fillStyle = 'rgba(255,190,200,' + (.18 + .1 * h(i)).toFixed(2) + ')';
        c.beginPath(); c.ellipse(x, y, W * (.12 + .1 * h(i * 3)), H * .018, 0, 0, 6.283); c.fill();
      }
      c.fillStyle = '#2a2350';
      c.beginPath(); c.ellipse(W * .15, H * .63, W * .12, H * .03, 0, Math.PI, 0); c.fill();
      for (let i = 0; i < Math.round(240 * P.dens); i++) {
        const y = H * (.64 + .36 * Math.pow(h(i * 1.37), 1.5));
        const spread = (y - H * .62) / (H * .38);
        const x = sx + (h(i * 4.1) - .5) * W * (.15 + spread * .9);
        const tw = Math.abs(Math.sin(t * (2 + h(i) * 3) + i));
        c.fillStyle = 'rgba(255,230,180,' + (tw * .7 * (1 - spread * .6)).toFixed(3) + ')';
        c.fillRect(x, y, H * .012 * (1 + spread * 2), H * .002 + 1);
      }
    },

    // 霓虹街道：透视路面 + 两侧楼与招牌 + 湿地反光
    neoncity(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#0b0620'], [.55, '#1a0d3a'], [1, '#08040f']]);
      const vx = W * (.5 + .08 * (P.cx - .5)), vy = H * .52;
      const cols = [P.pal.hot || '#ff3fa4', P.pal.c1 || '#33e1ff', P.pal.c2 || '#ffd23f', '#9b5cff'];
      for (let side = -1; side <= 1; side += 2) {
        for (let i = 0; i < 9; i++) {
          const d = 1 - i / 9, x = vx + side * W * (.08 + .5 * d), w = W * .12 * d + 4, top = vy - H * (.15 + .45 * d * (.6 + .4 * h(i * 3 + side)));
          c.fillStyle = '#120a26'; c.fillRect(side > 0 ? x : x - w, top, w, H * .9 - top);
          for (let k = 0; k < 4; k++) {
            if (h(i * 7 + k + side) < .4) continue;
            const col = cols[(i + k + (side > 0 ? 1 : 0)) % 4];
            const sy = top + (k + 1) * (H * .9 - top) / 6, sw = w * .7, sh = Math.max(3, H * .02 * d);
            const fl = .7 + .3 * Math.sin(t * (3 + h(i * k)) + i);
            c.save(); c.shadowColor = col; c.shadowBlur = 12 * d;
            c.fillStyle = rgba(col, .85 * fl * (1 + .3 * P.pulse));
            c.fillRect(side > 0 ? x + w * .15 : x - w * .85, sy, sw, sh);
            c.restore();
          }
        }
      }
      const g = c.createLinearGradient(0, vy, 0, H);
      g.addColorStop(0, 'rgba(40,20,80,.6)'); g.addColorStop(1, 'rgba(10,5,25,.95)');
      c.fillStyle = g; c.beginPath(); c.moveTo(vx - 4, vy); c.lineTo(vx + 4, vy); c.lineTo(W * 1.3, H); c.lineTo(-W * .3, H); c.fill();
      for (let i = 0; i < 40; i++) {
        const col = cols[i % 4], x = vx + (h(i * 3.3) - .5) * W * 1.2, y = vy + (H - vy) * (.3 + .7 * h(i * 5.1));
        c.fillStyle = rgba(col, .08 + .1 * Math.abs(Math.sin(t * 1.5 + i))); c.fillRect(x, y, W * .004, (H - y) * .5);
      }
      for (let k = 0; k < 10; k++) {
        const z = U.fract(k / 10 + t * .25), y = vy + (H - vy) * z * z;
        c.fillStyle = 'rgba(255,255,255,' + (.4 * z).toFixed(2) + ')';
        c.fillRect(vx - W * .004 * z * 8, y, W * .008 * z * 8, H * .01 * z * 3);
      }
    },

    // 星光舞台：台阶 + 侧面灯架 + 顶部灯桁架 + 光柱 + 地面反光
    starstage(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#0c0718'], [.6, '#1c1035'], [1, '#05030a']]);
      stars(c, W, H, t, Math.round(120 * P.dens), .5);
      const cols = [P.pal.c1 || '#ff7ad9', P.pal.c2 || '#7ac8ff', P.pal.hot || '#fff27a'];
      c.save(); c.globalCompositeOperation = 'lighter';
      for (let i = 0; i < 8; i++) {
        const x0 = W * (.06 + .88 * i / 7), sw = Math.sin(t * (.6 + h(i) * .5) + i * 1.3) * W * .2;
        const g = c.createLinearGradient(x0, 0, x0 + sw, H * .85);
        const col = cols[i % 3];
        g.addColorStop(0, rgba(col, .35 * (1 + .5 * P.pulse))); g.addColorStop(1, rgba(col, 0));
        c.fillStyle = g; c.beginPath(); c.moveTo(x0 - 6, H * .08); c.lineTo(x0 + 6, H * .08); c.lineTo(x0 + sw + W * .07, H * .85); c.lineTo(x0 + sw - W * .07, H * .85); c.fill();
      }
      c.restore();
      c.fillStyle = '#2a2140'; c.fillRect(0, H * .06, W, H * .025);
      for (let i = 0; i < 16; i++) { c.fillStyle = rgba(cols[i % 3], .9); c.beginPath(); c.arc(W * (i + .5) / 16, H * .085, H * .012, 0, 6.283); c.fill(); }
      for (let k = 0; k < 4; k++) {
        const y = H * (.74 + k * .065), inset = W * (.22 - k * .06);
        c.fillStyle = k % 2 ? '#1b1430' : '#251b40'; c.fillRect(inset, y, W - inset * 2, H * .065);
        c.fillStyle = rgba(cols[k % 3], .55 + .35 * Math.sin(t * 2 + k)); c.fillRect(inset, y, W - inset * 2, H * .004);
      }
      for (let side = 0; side < 2; side++) {
        const x = side ? W * .93 : W * .07;
        c.fillStyle = '#1a1328'; c.fillRect(x - W * .006, H * .1, W * .012, H * .7);
        for (let k = 0; k < 5; k++) glow(c, x, H * (.16 + k * .12), H * .04, cols[(k + side) % 3], .5 + .3 * Math.sin(t * 3 + k));
      }
    },

    // 梦幻泡泡：柔和渐变 + 大小泡泡缓慢上浮 + 高光
    bubbles(c, W, H, t, P) {
      vgrad(c, W, H, [[0, U.rgba(P.pal.c2 || '#a8d8ff', 1)], [.6, U.rgba(P.pal.c1 || '#ffc4e1', 1)], [1, '#fff3fb']]);
      c.fillStyle = 'rgba(255,255,255,.18)'; c.fillRect(0, 0, W, H);
      const N = Math.round(70 * P.dens);
      for (let i = 0; i < N; i++) {
        const r = H * (.015 + .07 * Math.pow(h(i * 2.9), 2));
        const x = h(i * 3.7) * W + Math.sin(t * .6 + i) * W * .02;
        const y = H * 1.1 - U.fract(h(i * 1.9) + t * (.02 + .03 * h(i * 5))) * H * 1.3;
        c.strokeStyle = 'rgba(255,255,255,.75)'; c.lineWidth = Math.max(1, r * .06);
        const g = c.createRadialGradient(x - r * .3, y - r * .3, r * .1, x, y, r);
        g.addColorStop(0, 'rgba(255,255,255,.35)'); g.addColorStop(.7, rgba(['#ffc2e8', '#c2e6ff', '#fff2b8'][i % 3], .18)); g.addColorStop(1, 'rgba(255,255,255,.45)');
        c.fillStyle = g; c.beginPath(); c.arc(x, y, r, 0, 6.283); c.fill(); c.stroke();
        c.fillStyle = 'rgba(255,255,255,.8)'; c.beginPath(); c.ellipse(x - r * .35, y - r * .4, r * .18, r * .1, -.6, 0, 6.283); c.fill();
      }
    },

    // 灯笼夜祭：深蓝夜 + 一排排红灯笼（透视）+ 屋檐剪影 + 烟花
    lantern(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#0b0d2a'], [.6, '#22183f'], [1, '#2d1430']]);
      stars(c, W, H, t, Math.round(90 * P.dens), .45);
      for (let f = 0; f < 3; f++) {
        const cyc = 4 + f, ph = U.fract(t / cyc + f * .37), fx = W * (.2 + .3 * f), fy = H * (.12 + .06 * f);
        if (ph < .6) {
          const q = ph / .6, r = H * .16 * (1 - (1 - q) * (1 - q));
          const col = [P.pal.hot || '#ff5a7a', P.pal.c2 || '#ffd25a', P.pal.c1 || '#7adfff'][f];
          for (let k = 0; k < 28; k++) {
            const a = k / 28 * 6.283, px = fx + Math.cos(a) * r, py = fy + Math.sin(a) * r + ph * H * .03;
            c.fillStyle = rgba(col, (1 - ph / .6) * .9); c.fillRect(px, py, H * .006, H * .006);
          }
        }
      }
      c.fillStyle = '#120a1e';
      c.beginPath(); c.moveTo(0, H * .78);
      for (let i = 0; i <= 6; i++) { const x = i * W / 6; c.lineTo(x, H * .7); c.quadraticCurveTo(x + W / 12, H * .66, x + W / 6, H * .7); }
      c.lineTo(W, H); c.lineTo(0, H); c.fill();
      for (let row = 0; row < 3; row++) {
        const y0 = H * (.2 + row * .14), sc = 1 - row * .25, n = 7 + row * 3;
        c.strokeStyle = 'rgba(40,20,30,.8)'; c.lineWidth = 1;
        c.beginPath(); c.moveTo(0, y0); c.quadraticCurveTo(W / 2, y0 + H * .06 * sc, W, y0); c.stroke();
        for (let i = 0; i < n; i++) {
          const u = (i + .5) / n, x = u * W, y = y0 + H * .06 * sc * 4 * u * (1 - u) + Math.sin(t * 1.2 + i + row) * H * .004;
          const r = H * .028 * sc;
          glow(c, x, y + r, r * 3.2, '#ff8a4a', .35 + .15 * Math.sin(t * 2 + i));
          c.fillStyle = '#e8402f'; c.beginPath(); c.ellipse(x, y + r, r * .8, r, 0, 0, 6.283); c.fill();
          c.fillStyle = 'rgba(255,220,150,.55)'; c.fillRect(x - r * .8, y + r * .95, r * 1.6, r * .1);
          c.fillStyle = '#3a1a10'; c.fillRect(x - r * .35, y - r * .05, r * .7, r * .15); c.fillRect(x - r * .35, y + r * 1.9, r * .7, r * .15);
        }
      }
    }
  };
  Object.assign(S.SCENES, NEW);
  S.RICH_SCENES = (S.RICH_SCENES || []).concat(Object.keys(NEW));

  /* ---------------- 人物前景层粒子（稀疏、半透明、比背景大，营造景深） ---------------- */
  function drawFront(c, W, H, t, kind, P) {
    c.clearRect(0, 0, W, H);
    if (!kind) return;
    const N = Math.round((kind === 'snow' ? 26 : 14) * (P.dens || 1));
    for (let i = 0; i < N; i++) {
      const sp = .03 + .05 * h(i * 7.7);
      let x = h(i * 3.1) * W, y = U.fract(h(i * 5.3) + t * sp) * H * 1.2 - H * .1;
      if (kind === 'bubble' || kind === 'spark') y = H * 1.1 - U.fract(h(i * 5.3) + t * sp) * H * 1.2;
      x += Math.sin(t * (.4 + h(i)) + i * 2) * W * .03;
      const r = H * (.012 + .03 * h(i * 2.2));
      // 只在画面左右两侧 35% 内放，中间（人物主要区域）少放，免得挡脸
      const u = x / W;
      if (u > .33 && u < .67 && h(i * 9.9) < .8) continue;
      c.save(); c.translate(x, y); c.rotate(t * (.3 + h(i)) + i);
      c.globalAlpha = .45 + .25 * h(i * 4.4);
      if (kind === 'petal') {
        c.fillStyle = P.col || '#ffb7d5';
        c.beginPath(); c.moveTo(0, -r); c.bezierCurveTo(r * .8, -r * .6, r * .7, r * .5, 0, r); c.bezierCurveTo(-r * .7, r * .5, -r * .8, -r * .6, 0, -r); c.fill();
      } else if (kind === 'snow') {
        c.fillStyle = '#ffffff'; c.beginPath(); c.arc(0, 0, r * .45, 0, 6.283); c.fill();
      } else if (kind === 'spark') {
        c.fillStyle = P.col || '#ffe58a'; c.beginPath(); c.moveTo(0, -r); c.quadraticCurveTo(0, 0, r * .5, 0); c.quadraticCurveTo(0, 0, 0, r); c.quadraticCurveTo(0, 0, -r * .5, 0); c.quadraticCurveTo(0, 0, 0, -r); c.fill();
      } else if (kind === 'bubble') {
        c.strokeStyle = 'rgba(255,255,255,.8)'; c.lineWidth = Math.max(1, r * .08); c.beginPath(); c.arc(0, 0, r, 0, 6.283); c.stroke();
      } else if (kind === 'note') {
        c.fillStyle = P.col || '#ffffff'; c.beginPath(); c.ellipse(0, r * .55, r * .42, r * .3, -.4, 0, 6.283); c.fill(); c.fillRect(r * .3, -r * .9, r * .12, r * 1.5);
      }
      c.restore();
    }
  }
  // 场景 → 前景粒子
  const FRONT_OF = {
    sakura: 'petal', pattern: 'note', confetti: 'spark', concert: 'spark', skyline: 'spark',
    sky: 'petal', galaxy: 'spark', rainwin: null, aurora: 'snow', sunset: 'spark', neoncity: null,
    starstage: 'spark', bubbles: 'bubble', lantern: 'spark', bokeh: 'spark'
  };
  S.drawFront = drawFront;
  S.FRONT_OF = FRONT_OF;
})(window);
