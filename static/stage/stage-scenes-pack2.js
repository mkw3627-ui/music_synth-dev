/* =============================================================================
 * 「为她而唱」舞台 · 第三十八轮：背景素材扩展包（再加 12 个场景）
 * 全部用 Canvas 2D 程序绘制（没有图片文件），只由 t 和参数决定：
 *   ocean    深海光柱（水面焦散光 + 光柱 + 气泡 + 鱼群剪影）
 *   forest   萤火森林（层叠树影 + 雾带 + 萤火虫）
 *   cafe     咖啡馆窗景（窗格 + 雨后街灯虚化 + 吊灯）
 *   space    星球轨道（大行星 + 光环 + 卫星 + 星尘带）
 *   synth    合成器波（复古日落 + 透视网格地面 + 山脉线框）
 *   crystal  水晶洞（多边形晶簇 + 折射光点）
 *   classroom 黄昏教室（窗框 + 夕照光斑 + 黑板 + 课桌剪影）
 *   rooftop  天台夜景（栏杆 + 远景城市 + 流星）
 *   hearts   爱心气球（层叠爱心气球上升 + 丝带）
 *   kaleido  万花筒（中心对称的旋转几何花纹）
 *   wavefield 声波地形（多层音频波线，随拍子起伏）
 *   festival 夏日祭（烟花大轮 + 屋台灯串 + 远山）
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
  function heart(c, x, y, r) {
    c.beginPath(); c.moveTo(x, y + r * .9);
    c.bezierCurveTo(x - r * 1.6, y - r * .1, x - r * .7, y - r * 1.3, x, y - r * .45);
    c.bezierCurveTo(x + r * .7, y - r * 1.3, x + r * 1.6, y - r * .1, x, y + r * .9);
    c.fill();
  }
  const C = (P, k, d) => (P.pal && P.pal[k]) || d;

  const NEW = {
    ocean(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#0b4a6e'], [.45, '#063152'], [1, '#020c1f']]);
      c.save(); c.globalCompositeOperation = 'lighter';
      for (let i = 0; i < 9; i++) {
        const x0 = W * (h(i * 3.1) * 1.2 - .1) + Math.sin(t * .2 + i) * W * .03;
        const g = c.createLinearGradient(0, 0, 0, H * .9);
        g.addColorStop(0, 'rgba(160,230,255,' + (.16 + .08 * Math.sin(t * .7 + i)).toFixed(3) + ')'); g.addColorStop(1, 'rgba(160,230,255,0)');
        c.fillStyle = g; c.beginPath(); c.moveTo(x0 - W * .01, 0); c.lineTo(x0 + W * .03, 0); c.lineTo(x0 + W * .12, H * .9); c.lineTo(x0 - W * .04, H * .9); c.fill();
      }
      for (let k = 0; k < 60; k++) {   // 水面焦散
        const x = h(k * 1.7) * W, y = H * .05 * h(k * 2.9);
        c.strokeStyle = 'rgba(200,245,255,' + (.25 * Math.abs(Math.sin(t * 1.4 + k))).toFixed(3) + ')'; c.lineWidth = 1.2;
        c.beginPath(); c.ellipse(x, y, W * .03, H * .008, Math.sin(t + k), 0, 6.283); c.stroke();
      }
      c.restore();
      for (let i = 0; i < Math.round(40 * P.dens); i++) {
        const x = h(i * 4.3) * W + Math.sin(t + i) * 6, y = H - U.fract(h(i * 2.2) + t * (.03 + .04 * h(i))) * H * 1.1;
        const r = H * (.003 + .008 * h(i * 6.1));
        c.strokeStyle = 'rgba(220,250,255,.55)'; c.lineWidth = 1; c.beginPath(); c.arc(x, y, r, 0, 6.283); c.stroke();
      }
      c.fillStyle = 'rgba(2,20,40,.55)';
      for (let i = 0; i < 26; i++) {
        const ph = U.fract(t * .03 + h(i * 1.3) * .3), x = W * (1.2 - ph * 1.4) + h(i * 7) * W * .1, y = H * (.55 + .12 * h(i * 3.3)) + Math.sin(t * 2 + i) * 3;
        c.beginPath(); c.ellipse(x, y, H * .014, H * .005, 0, 0, 6.283); c.fill();
        c.beginPath(); c.moveTo(x + H * .012, y); c.lineTo(x + H * .022, y - H * .005); c.lineTo(x + H * .022, y + H * .005); c.fill();
      }
    },

    forest(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#08201c'], [.5, '#0f3328'], [1, '#04100c']]);
      glow(c, W * .5, H * .35, H * .7, '#9cf0c0', .18);
      for (let layer = 0; layer < 4; layer++) {
        const sh = ['#0d2a22', '#0a231c', '#071a15', '#04100c'][layer], n = 8 + layer * 3;
        c.fillStyle = sh;
        for (let i = 0; i < n; i++) {
          const x = (i + h(i * 3 + layer)) / n * W * 1.1 - W * .05, w = W * (.02 + .02 * layer);
          c.fillRect(x - w / 2, 0, w, H);
          c.beginPath(); c.ellipse(x, H * (.1 + .1 * h(i + layer)), W * (.05 + .03 * layer), H * .12, 0, 0, 6.283); c.fill();
        }
        c.fillStyle = 'rgba(180,240,210,' + (.05 + .02 * layer) + ')';
        c.fillRect(0, H * (.55 + layer * .08) + Math.sin(t * .3 + layer) * H * .01, W, H * .05);
      }
      for (let i = 0; i < Math.round(55 * P.dens); i++) {
        const x = h(i * 2.3) * W + Math.sin(t * .5 + i * 2) * W * .03, y = H * (.2 + .7 * h(i * 5.7)) + Math.cos(t * .4 + i) * H * .03;
        const a = Math.max(0, Math.sin(t * (1 + h(i)) + i * 3)) * (.7 + .3 * P.pulse);
        glow(c, x, y, H * .02, '#e8ff9a', a * .9);
      }
    },

    cafe(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#2a1e2e'], [1, '#120c14']]);
      // 窗外：街灯虚化
      for (let i = 0; i < 46; i++) {
        const x = h(i * 3.7) * W, y = H * (.15 + .55 * h(i * 2.9)), r = H * (.02 + .05 * h(i * 1.1));
        glow(c, x, y, r, ['#ffb86b', '#ff7aa8', '#8fd3ff', '#ffe08a'][i % 4], .35 + .15 * Math.sin(t * .6 + i));
      }
      // 雨痕
      c.strokeStyle = 'rgba(200,220,255,.18)'; c.lineWidth = 1;
      for (let i = 0; i < 70; i++) { const x = h(i * 9.1) * W, y = U.fract(h(i) + t * (.2 + .3 * h(i * 2))) * H; c.beginPath(); c.moveTo(x, y); c.lineTo(x - 2, y + H * .03); c.stroke(); }
      // 窗框
      c.fillStyle = '#1b1218';
      const fw = W * .012;
      [0, .33, .66, 1].forEach(u => c.fillRect(W * u - fw / 2, 0, fw, H));
      [.0, .5].forEach(u => c.fillRect(0, H * u, W, fw));
      c.fillRect(0, H * .8, W, H * .2);
      c.fillStyle = '#3a2620'; c.fillRect(0, H * .8, W, H * .02);
      // 吊灯
      for (let i = 0; i < 3; i++) {
        const x = W * (.2 + .3 * i), y = H * .12 + Math.sin(t * .8 + i) * 2;
        c.strokeStyle = 'rgba(30,20,20,.9)'; c.beginPath(); c.moveTo(x, 0); c.lineTo(x, y); c.stroke();
        glow(c, x, y + H * .02, H * .12, '#ffcf8a', .5 + .1 * P.pulse);
        c.fillStyle = '#2a1a14'; c.beginPath(); c.moveTo(x - H * .03, y + H * .02); c.lineTo(x + H * .03, y + H * .02); c.lineTo(x, y - H * .01); c.fill();
      }
    },

    space(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#04030f'], [1, '#110a2a']]);
      stars(c, W, H, t, Math.round(260 * P.dens), 1);
      glow(c, W * .7, H * .3, H * .6, C(P, 'c2', '#7a5cff'), .2);
      const px = W * .78, py = H * .62, R = H * .32;
      const g = c.createRadialGradient(px - R * .4, py - R * .4, R * .1, px, py, R);
      g.addColorStop(0, '#ffd2a8'); g.addColorStop(.6, '#c8628a'); g.addColorStop(1, '#2a1238');
      c.save(); c.translate(px, py); c.rotate(-.35);
      c.strokeStyle = 'rgba(255,220,240,.35)'; c.lineWidth = H * .012;
      c.beginPath(); c.ellipse(0, 0, R * 1.8, R * .35, 0, Math.PI, 6.283); c.stroke();
      c.restore();
      c.fillStyle = g; c.beginPath(); c.arc(px, py, R, 0, 6.283); c.fill();
      c.save(); c.translate(px, py); c.rotate(-.35);
      c.strokeStyle = 'rgba(255,230,245,.55)'; c.lineWidth = H * .012;
      c.beginPath(); c.ellipse(0, 0, R * 1.8, R * .35, 0, 0, Math.PI); c.stroke();
      c.restore();
      const ma = t * .25;
      c.fillStyle = '#d8d0ff'; c.beginPath(); c.arc(px + Math.cos(ma) * R * 2.2, py + Math.sin(ma) * R * .5, H * .025, 0, 6.283); c.fill();
      for (let i = 0; i < 120; i++) {   // 星尘带
        const u = h(i * 1.9), x = W * u, y = H * (.2 + .25 * u) + (h(i * 4.1) - .5) * H * .08;
        c.fillStyle = rgba(C(P, 'c1', '#9ad'), .25 * h(i * 2.7)); c.fillRect(x, y, 2, 2);
      }
    },

    synth(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#1a0533'], [.5, '#55127a'], [.56, '#ff6fa8'], [.57, '#140428'], [1, '#08010f']]);
      stars(c, W, H, t, 90, .45);
      const sx = W / 2, sy = H * .5, R = H * .2;
      const g = c.createLinearGradient(0, sy - R, 0, sy);
      g.addColorStop(0, '#ffe76b'); g.addColorStop(1, '#ff3fa4');
      c.fillStyle = g; c.beginPath(); c.arc(sx, sy, R, Math.PI, 0); c.fill();
      c.fillStyle = '#55127a';
      for (let k = 0; k < 6; k++) c.fillRect(sx - R, sy - R * (.12 + k * .13), R * 2, R * (.03 + k * .012));
      c.strokeStyle = 'rgba(255,90,200,.8)'; c.lineWidth = 1.5;   // 山脉线框
      c.beginPath(); c.moveTo(0, H * .56);
      for (let x = 0; x <= W; x += W / 30) c.lineTo(x, H * (.56 - .12 * Math.abs(Math.sin(x / W * 9)) * h(x * .01)));
      c.stroke();
      c.strokeStyle = 'rgba(0,229,255,.75)'; c.lineWidth = 1.2;   // 透视网格
      const hz = H * .57;
      for (let k = -14; k <= 14; k++) { c.beginPath(); c.moveTo(W / 2 + k * W * .01, hz); c.lineTo(W / 2 + k * W * .12, H); c.stroke(); }
      for (let k = 0; k < 12; k++) {
        const z = U.fract(k / 12 + t * .2), y = hz + (H - hz) * z * z;
        c.globalAlpha = .3 + .7 * z; c.beginPath(); c.moveTo(0, y); c.lineTo(W, y); c.stroke();
      }
      c.globalAlpha = 1;
    },

    crystal(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#120a2a'], [1, '#0a1a2e']]);
      const cols = [C(P, 'c1', '#b48cff'), C(P, 'c2', '#7ae0ff'), '#ff9ad5'];
      for (let i = 0; i < 26; i++) {
        const side = i % 2, x = side ? W * (.75 + .3 * h(i)) : W * (-.05 + .3 * h(i)), base = H * (.75 + .3 * h(i * 3));
        const ht = H * (.2 + .45 * h(i * 5.3)), w = W * (.02 + .04 * h(i * 7.1)), lean = (h(i * 2.2) - .5) * .5;
        const col = cols[i % 3];
        const g = c.createLinearGradient(x, base - ht, x + w, base);
        g.addColorStop(0, rgba(col, .85)); g.addColorStop(1, rgba(col, .15));
        c.fillStyle = g;
        c.beginPath(); c.moveTo(x - w, base); c.lineTo(x - w * .7 + lean * ht, base - ht * .85); c.lineTo(x + lean * ht, base - ht); c.lineTo(x + w * .7 + lean * ht, base - ht * .85); c.lineTo(x + w, base); c.fill();
        c.strokeStyle = 'rgba(255,255,255,.35)'; c.lineWidth = 1; c.stroke();
      }
      for (let i = 0; i < 50; i++) {
        const x = h(i * 3.3) * W, y = h(i * 6.1) * H, a = Math.max(0, Math.sin(t * 2 + i * 1.7));
        c.fillStyle = 'rgba(255,255,255,' + (a * .8).toFixed(2) + ')';
        c.beginPath(); c.moveTo(x, y - 6); c.lineTo(x + 1.5, y); c.lineTo(x, y + 6); c.lineTo(x - 1.5, y); c.fill();
      }
    },

    classroom(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#ffb07a'], [.6, '#e8806a'], [1, '#5a3048']]);
      // 窗外天空/云
      for (let i = 0; i < 6; i++) { c.fillStyle = 'rgba(255,230,210,.35)'; c.beginPath(); c.ellipse(U.fract(h(i) + t * .004) * W * 1.2 - W * .1, H * (.1 + .08 * i), W * .12, H * .03, 0, 0, 6.283); c.fill(); }
      // 窗框
      c.fillStyle = '#4a2a30';
      for (let k = 0; k <= 4; k++) c.fillRect(W * k / 4 - W * .006, 0, W * .012, H * .62);
      c.fillRect(0, H * .3, W, H * .012); c.fillRect(0, H * .6, W, H * .03);
      // 夕照光斑（斜方格投影）
      c.save(); c.globalCompositeOperation = 'lighter';
      for (let k = 0; k < 4; k++) {
        c.fillStyle = 'rgba(255,200,140,' + (.14 + .04 * Math.sin(t * .5 + k)).toFixed(3) + ')';
        const x0 = W * (.05 + k * .25);
        c.beginPath(); c.moveTo(x0, H * .63); c.lineTo(x0 + W * .2, H * .63); c.lineTo(x0 + W * .35, H); c.lineTo(x0 + W * .1, H); c.fill();
      }
      c.restore();
      c.fillStyle = '#3a2028'; c.fillRect(0, H * .63, W, H * .37);
      c.fillStyle = 'rgba(255,200,140,.12)'; c.fillRect(0, H * .63, W, H * .37);
      c.fillStyle = '#2a161c';
      for (let i = 0; i < 6; i++) { const x = W * (.05 + i * .17); c.fillRect(x, H * .78, W * .12, H * .02); c.fillRect(x + W * .01, H * .8, W * .008, H * .12); c.fillRect(x + W * .1, H * .8, W * .008, H * .12); }
      for (let i = 0; i < 40; i++) { const x = h(i * 3.1) * W, y = H * (.1 + .5 * U.fract(h(i * 5) + t * .01)); c.fillStyle = 'rgba(255,240,220,' + (.4 * Math.abs(Math.sin(t + i))).toFixed(2) + ')'; c.fillRect(x, y, 2, 2); }
    },

    rooftop(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#0b1030'], [.6, '#2a2050'], [1, '#120c22']]);
      stars(c, W, H, t, Math.round(140 * P.dens), .5);
      for (let k = 0; k < 3; k++) {   // 流星
        const ph = U.fract(t / (5 + k * 2) + k * .3);
        if (ph < .15) {
          const q = ph / .15, x = W * (.9 - .5 * q - k * .1), y = H * (.05 + .25 * q + k * .05);
          const g = c.createLinearGradient(x, y, x + W * .12, y - H * .06);
          g.addColorStop(0, 'rgba(255,255,255,.9)'); g.addColorStop(1, 'rgba(255,255,255,0)');
          c.strokeStyle = g; c.lineWidth = 2; c.beginPath(); c.moveTo(x, y); c.lineTo(x + W * .12, y - H * .06); c.stroke();
        }
      }
      for (let i = 0; i < 40; i++) {   // 远景城市
        const x = (i / 40) * W, w = W / 40 + 1, ht = H * (.08 + .2 * h(i * 3.3));
        c.fillStyle = '#151030'; c.fillRect(x, H * .62 - ht, w, ht + H * .1);
        for (let k = 0; k < 6; k++) if (h(i * 9 + k) > .55) { c.fillStyle = rgba(['#ffd27a', '#8fd3ff', '#ff9ad5'][k % 3], .7); c.fillRect(x + w * .3, H * .62 - ht + k * ht / 7 + 3, w * .25, 2); }
      }
      c.fillStyle = '#0a0818'; c.fillRect(0, H * .72, W, H * .28);
      c.strokeStyle = '#5a5a7a'; c.lineWidth = 3;   // 栏杆
      c.beginPath(); c.moveTo(0, H * .7); c.lineTo(W, H * .7); c.stroke();
      c.lineWidth = 2;
      for (let x = 0; x < W; x += W / 28) { c.beginPath(); c.moveTo(x, H * .7); c.lineTo(x, H * .8); c.stroke(); }
      c.beginPath(); c.moveTo(0, H * .8); c.lineTo(W, H * .8); c.stroke();
    },

    hearts(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#ffd6ea'], [.6, '#ffb3d6'], [1, '#ffe8f2']]);
      const cols = [C(P, 'hot', '#ff4f8b'), '#ff8ab8', '#ffffff', C(P, 'c2', '#ffc0dd'), '#ff6fa8'];
      const N = Math.round(36 * P.dens);
      for (let i = 0; i < N; i++) {
        const depth = .4 + .6 * h(i * 2.2), r = H * .045 * depth;
        const x = h(i * 3.9) * W + Math.sin(t * .6 + i) * W * .02, y = H * 1.15 - U.fract(h(i * 1.7) + t * .025 * depth) * H * 1.35;
        c.strokeStyle = 'rgba(255,255,255,.5)'; c.lineWidth = 1;
        c.beginPath(); c.moveTo(x, y + r); c.quadraticCurveTo(x + Math.sin(t + i) * 8, y + r * 3, x, y + r * 5); c.stroke();
        c.globalAlpha = .55 + .45 * depth; c.fillStyle = cols[i % 5]; heart(c, x, y, r);
        c.fillStyle = 'rgba(255,255,255,.55)'; c.beginPath(); c.ellipse(x - r * .45, y - r * .35, r * .18, r * .1, -.6, 0, 6.283); c.fill();
        c.globalAlpha = 1;
      }
      c.strokeStyle = 'rgba(255,255,255,.6)'; c.lineWidth = H * .006;   // 丝带
      for (let k = 0; k < 2; k++) {
        c.beginPath();
        for (let x = 0; x <= W; x += W / 40) { const y = H * (.2 + .6 * k) + Math.sin(x / W * 8 + t * .8 + k) * H * .04; if (x) c.lineTo(x, y); else c.moveTo(x, y); }
        c.stroke();
      }
    },

    kaleido(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#120626'], [1, '#05020e']]);
      const cx = W / 2, cy = H / 2, n = 12, R = Math.hypot(W, H) * .6;
      const cols = [C(P, 'c1', '#ff7ad9'), C(P, 'c2', '#7ac8ff'), C(P, 'c3', '#ffe066'), C(P, 'hot', '#ff4f8b')];
      c.save(); c.translate(cx, cy); c.rotate(t * .06);
      for (let k = 0; k < n; k++) {
        c.save(); c.rotate(k * 6.283 / n); if (k % 2) c.scale(1, -1);
        for (let j = 0; j < 7; j++) {
          const d = R * (.08 + j * .13) * (1 + .05 * Math.sin(t * .8 + j)), s = R * (.03 + .02 * j);
          c.fillStyle = rgba(cols[(j + k) % 4], .22 + .12 * Math.sin(t + j));
          c.beginPath(); c.moveTo(d, 0); c.lineTo(d + s, s * .5); c.lineTo(d + s * 1.8, 0); c.lineTo(d + s, -s * .2); c.fill();
          c.strokeStyle = rgba('#ffffff', .18); c.lineWidth = 1; c.stroke();
        }
        c.restore();
      }
      c.restore();
      glow(c, cx, cy, H * .35, C(P, 'c1', '#ff7ad9'), .25 + .2 * P.pulse);
    },

    wavefield(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#060818'], [1, '#141038']]);
      const cols = [C(P, 'c1', '#ff7ad9'), C(P, 'c2', '#7ac8ff'), C(P, 'c3', '#ffe066')];
      const L = 22;
      for (let k = 0; k < L; k++) {
        const y0 = H * (.35 + .6 * k / L), amp = H * (.02 + .07 * (1 - k / L)) * (1 + .6 * P.pulse);
        c.strokeStyle = rgba(cols[k % 3], .25 + .5 * (k / L)); c.lineWidth = 1 + 1.5 * k / L;
        c.beginPath();
        for (let x = 0; x <= W; x += W / 90) {
          const u = x / W, env = Math.exp(-Math.pow((u - .5) * 2.4, 2));
          const y = y0 - env * amp * (Math.sin(u * 22 + t * 2 + k * .4) * .6 + Math.sin(u * 51 - t * 3 + k) * .4);
          if (x) c.lineTo(x, y); else c.moveTo(x, y);
        }
        c.stroke();
      }
      stars(c, W, H, t, 70, .3);
    },

    festival(c, W, H, t, P) {
      vgrad(c, W, H, [[0, '#0a0c26'], [.7, '#24164a'], [1, '#160a1e']]);
      stars(c, W, H, t, 60, .5);
      for (let f = 0; f < 4; f++) {   // 烟花大轮
        const cyc = 3.5 + f * .9, ph = U.fract(t / cyc + f * .27), fx = W * (.15 + .23 * f), fy = H * (.18 + .1 * (f % 2));
        if (ph < .7) {
          const q = ph / .7, r = H * .2 * (1 - (1 - q) * (1 - q));
          const col = [C(P, 'hot', '#ff5a7a'), C(P, 'c2', '#ffd25a'), C(P, 'c1', '#7adfff'), '#c38aff'][f];
          for (let k = 0; k < 40; k++) {
            const a = k / 40 * 6.283, px = fx + Math.cos(a) * r, py = fy + Math.sin(a) * r + q * q * H * .05;
            c.strokeStyle = rgba(col, (1 - q) * .9); c.lineWidth = 2;
            c.beginPath(); c.moveTo(fx + Math.cos(a) * r * .85, fy + Math.sin(a) * r * .85 + q * q * H * .05); c.lineTo(px, py); c.stroke();
          }
          glow(c, fx, fy, r * .6, col, (1 - q) * .3);
        }
      }
      c.fillStyle = '#100a1c'; c.beginPath(); c.moveTo(0, H * .72);   // 远山
      for (let x = 0; x <= W; x += W / 12) c.lineTo(x, H * (.62 + .08 * h(x * .02)));
      c.lineTo(W, H); c.lineTo(0, H); c.fill();
      for (let row = 0; row < 2; row++) {   // 屋台灯串
        const y0 = H * (.68 + row * .1);
        for (let i = 0; i < 24; i++) {
          const x = (i + .5) / 24 * W, y = y0 + Math.sin(i * .9) * H * .01;
          const col = ['#ff6b4a', '#ffd25a', '#ff9ad5'][(i + row) % 3];
          glow(c, x, y, H * .03, col, .55 + .15 * Math.sin(t * 3 + i));
          c.fillStyle = col; c.beginPath(); c.arc(x, y, H * .006, 0, 6.283); c.fill();
        }
      }
    }
  };
  Object.assign(S.SCENES, NEW);
  S.RICH_SCENES = (S.RICH_SCENES || []).concat(Object.keys(NEW));
  if (S.FRONT_OF) Object.assign(S.FRONT_OF, {
    ocean: 'bubble', forest: 'spark', cafe: null, space: 'spark', synth: null, crystal: 'spark',
    classroom: 'petal', rooftop: 'spark', hearts: 'petal', kaleido: 'spark', wavefield: 'note', festival: 'spark'
  });
})(window);
