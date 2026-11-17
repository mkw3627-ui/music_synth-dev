/* =============================================================================
 * 「为她而唱」舞台 · 第五十轮：线条几何（简洁但不简单）
 *
 * 参考（用户给的三支 MV，逐帧看了分镜）：
 *   湊あくあ『#あくあ色ぱれっと』  —— 纯色底 + 标题卡 + 整屏排字（LOVE LOVE / YOU YOU）+ 圆环/色块转场
 *   音乃瀬奏『You＆合図』          —— 白底/淡彩底 + 线稿小物（节拍器、钟、心跳波形、日历卡）在间奏当主画面
 *   綺々羅々ヴィヴィ『かくれんぼ』  —— 白底大留白 + 胶片齿孔边 + 竖排/大小错落的字 + 线稿人物 + 线框地球/圆
 * 共同点：元素很少（一种线、一两个色），但线会「动」——展开、收缩、叠成形状、跟着拍子和旋律变化；
 * 不靠滤镜和素材堆。间奏时画面交给图形/乐器，而不是一直盯着人物。
 *
 * 这里的一切都由「线」生成：
 *   LINE 场景（背景）：一条或多条线，按拍子/能量变形；底色三档 paper（纯白/米白）、tint（淡彩）、ink（深色）
 *     l_wave    波线组：N 条线同相位→错相位，副歌时叠成莫尔纹
 *     l_strings 琴弦：竖直细线，拍点上被「拨」一下，按衰减正弦振动（音高=位置）
 *     l_keys    钢琴键：一排琴键（线框），跟拍点/能量按下去，按下的键填主色并向上升起音符线
 *     l_staff   五线谱：五条线缓慢流动，音符头（圆）按拍落在线上，连成旋律线
 *     l_rings   同心圆环：环按小节一圈圈展开，环上的缺口旋转（像唱片/雷达）
 *     l_grid    透视网格：线框地面 + 地平线，拍点上网格线亮一条扫过
 *     l_poly    多边形展开：一个 N 边形复制旋转成花（玫瑰线），N 随段落变
 *     l_lissa   李萨如曲线：两个频率比按段落换，画成一笔连续的线
 *     l_tick    节拍器：一根摆杆按 BPM 摆，背后是刻度弧
 *     l_script  手书底：整面淡淡的手写歌词（多行、倾斜、连笔感），当前句加深
 *   每个 LINE 场景都读 P.I（能量）、P.pulse（拍内脉冲）、P.bpm，和 P.pal 里的 1~2 个颜色。
 * ========================================================================== */
(function (global) {
  'use strict';
  const U = global.NekoStageU, S = global.NekoStageScenes;
  if (!U || !S) return;
  const h = U.h01, TAU = Math.PI * 2;
  const rgba = (col, a) => U.rgba(col, U.clamp(a, 0, 1));

  // 底色：由美术指导给（paper / tint / ink）；没有就按皮肤明暗
  function ground(P) {
    const A = global.__NEKO_ART, g = (A && A.groundNow) || P.ground || 'ink';
    const pal = P.pal || {};
    if (g === 'paper') return { bg: ['#fdfcf9', '#f6f3ee'], ink: '#3a3440', soft: '#b9b2c4', acc: pal.hot || pal.c1 || '#ff6fae', acc2: pal.c2 || '#6ac8ff', light: true };
    if (g === 'tint') {
      const c = pal.c1 || '#ffc9de';
      return { bg: [mix(c, '#ffffff', .78), mix(pal.c2 || '#cfe6ff', '#ffffff', .7)], ink: mix(c, '#3a2a44', .78), soft: mix(c, '#ffffff', .35), acc: pal.hot || c, acc2: pal.c2 || '#7ac8ff', light: true };
    }
    return { bg: [pal.bgA || '#12101c', pal.bgB || '#1d1830'], ink: '#f4f0ff', soft: 'rgba(255,255,255,.35)', acc: pal.hot || pal.c1 || '#ff7ad9', acc2: pal.c2 || '#7ac8ff', light: false };
  }
  function mix(a, b, k) {
    const pa = hex(a), pb = hex(b);
    return 'rgb(' + pa.map((v, i) => Math.round(v + (pb[i] - v) * k)).join(',') + ')';
  }
  function hex(c) {
    c = String(c || '#888888');
    if (c[0] === '#') { if (c.length === 4) c = '#' + c[1] + c[1] + c[2] + c[2] + c[3] + c[3]; return [1, 3, 5].map(i => parseInt(c.substr(i, 2), 16) || 0); }
    const m = c.match(/\d+/g) || [128, 128, 128]; return m.slice(0, 3).map(Number);
  }
  function base(c, W, H, G) {
    const g = c.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, G.bg[0]); g.addColorStop(1, G.bg[1]);
    c.fillStyle = g; c.fillRect(0, 0, W, H);
  }
  const beatOf = (t, P) => t * (P.bpm || 120) / 60;
  // 拍内包络：拍点上 1，随后指数衰减
  const hit = (t, P, div) => { const b = beatOf(t, P) / (div || 1); return Math.exp(-(b - Math.floor(b)) * 4.5); };
  const lw = (H, k) => Math.max(1, H / 720 * k);

  const LINES = {
    /* 波线组：一条线 → 多条线叠成莫尔纹。能量低时几乎是平行直线，能量高时相位错开、振幅变大 */
    l_wave(c, W, H, t, P) {
      const G = ground(P); base(c, W, H, G);
      const n = 9 + Math.round(14 * P.I), b = beatOf(t, P), k = hit(t, P);
      const spread = .15 + .85 * U.sstep(.25, .8, P.I);          // 线从一束（收）到铺满（展）
      for (let i = 0; i < n; i++) {
        const u = n > 1 ? i / (n - 1) : .5, y0 = H * (.5 + (u - .5) * .78 * spread);
        const amp = H * (.012 + .05 * P.I) * (1 + .7 * k) * (1 - Math.abs(u - .5) * .8);
        c.strokeStyle = rgba(i % 5 === 2 ? G.acc : G.ink, i % 5 === 2 ? .85 : (G.light ? .46 : .4));
        c.lineWidth = lw(H, i % 5 === 2 ? 2.2 : 1.1);
        c.beginPath();
        for (let x = -8; x <= W + 8; x += W / 110) {
          const v = x / W, ph = b * .5 + i * (.12 + .5 * P.I) * (P.v > .5 ? 1 : -1);
          const y = y0 + amp * (Math.sin(v * (5 + 3 * P.v) * TAU * .5 + ph) + .45 * Math.sin(v * 13 - ph * 1.7 + i));
          if (x <= -8) c.moveTo(x, y); else c.lineTo(x, y);
        }
        c.stroke();
      }
    },
    /* 琴弦：竖直细线，每拍拨一根（按拍号轮换位置），拨到的弦加粗并振动 */
    l_strings(c, W, H, t, P) {
      const G = ground(P); base(c, W, H, G);
      const n = 26, b = beatOf(t, P), bi = Math.floor(b), fr = b - bi;
      for (let i = 0; i < n; i++) {
        const x0 = W * (.06 + .88 * i / (n - 1));
        let amp = 0, hot = 0;
        for (let back = 0; back < 6; back++) {
          const who = Math.floor(h((bi - back) * 7.13 + (P.v || 0) * 31) * n);
          if (who === i) { const age = fr + back; const e = Math.exp(-age * 1.1); amp += e; hot = Math.max(hot, e); }
        }
        c.strokeStyle = rgba(hot > .15 ? G.acc : G.ink, hot > .15 ? .5 + .5 * hot : (G.light ? .34 : .26));
        c.lineWidth = lw(H, 1 + 2.2 * hot);
        c.beginPath();
        for (let y = 0; y <= H; y += H / 60) {
          const v = y / H, dx = amp * W * .012 * Math.sin(v * Math.PI) * Math.sin(t * (22 + i * 1.3) + v * 3);
          if (!y) c.moveTo(x0 + dx, y); else c.lineTo(x0 + dx, y);
        }
        c.stroke();
        if (hot > .5) { c.fillStyle = rgba(G.acc, hot); c.beginPath(); c.arc(x0, H * (.3 + .4 * h(i * 3.3)), lw(H, 5) * hot, 0, TAU); c.fill(); }
      }
    },
    /* 钢琴键：线框键盘，拍点上按下 1~3 个键；按下的键填色，并从键上升起一条细线（延音）*/
    l_keys(c, W, H, t, P) {
      const G = ground(P); base(c, W, H, G);
      const n = 28, kw = W / n, top = H * .6, kh = H * .34, b = beatOf(t, P), bi = Math.floor(b * 2), fr = b * 2 - bi;
      const pressed = new Map();
      for (let back = 0; back < 8; back++) {
        const cnt = 1 + Math.floor(h((bi - back) * 3.7) * (1 + 2.4 * P.I));
        for (let q = 0; q < cnt; q++) {
          const key = Math.floor(h((bi - back) * 5.31 + q * 9.7 + (P.v || 0) * 17) * n);
          const e = Math.exp(-(fr + back) * .55);
          pressed.set(key, Math.max(pressed.get(key) || 0, e));
        }
      }
      c.lineWidth = lw(H, 1.4);
      for (let i = 0; i < n; i++) {
        const e = pressed.get(i) || 0, x = i * kw, dy = e * kh * .05;
        if (e > .04) { c.fillStyle = rgba(i % 3 ? G.acc : G.acc2, .18 + .62 * e); c.fillRect(x + 1, top + dy, kw - 2, kh); }
        c.strokeStyle = rgba(G.ink, G.light ? .55 : .6); c.strokeRect(x + .5, top + dy, kw - 1, kh);
        if (e > .04) {      // 延音线：从键顶向上长，越久越高越淡
          const hh = H * .5 * (1 - e) + H * .04;
          c.strokeStyle = rgba(i % 3 ? G.acc : G.acc2, e * .9); c.lineWidth = lw(H, 2);
          c.beginPath(); c.moveTo(x + kw / 2, top - 6); c.lineTo(x + kw / 2, top - 6 - hh); c.stroke();
          c.lineWidth = lw(H, 1.4);
        }
      }
      // 黑键（只画线框的小矩形，2-3 分组）
      for (let i = 0; i < n - 1; i++) {
        if ([2, 6].includes(i % 7)) continue;
        const e = pressed.get(i) || 0;
        c.fillStyle = rgba(G.ink, G.light ? .8 : .85); c.fillRect((i + 1) * kw - kw * .28, top + e * kh * .05, kw * .56, kh * .58);
      }
      c.strokeStyle = rgba(G.ink, .5); c.beginPath(); c.moveTo(0, top); c.lineTo(W, top); c.stroke();
    },
    /* 五线谱：五条线微微起伏流动，音符头按半拍落位，相邻音符用细线连成旋律轮廓 */
    l_staff(c, W, H, t, P) {
      const G = ground(P); base(c, W, H, G);
      const b = beatOf(t, P), gap = H * .045, mid = H * (.42 + .04 * Math.sin(t * .2));
      const yAt = (x, k) => mid + (k - 2) * gap + Math.sin(x / W * 5 + t * .5) * gap * .5 * (.3 + P.I);
      c.strokeStyle = rgba(G.ink, G.light ? .42 : .5); c.lineWidth = lw(H, 1.1);
      for (let k = 0; k < 5; k++) { c.beginPath(); for (let x = 0; x <= W; x += W / 60) { const y = yAt(x, k); if (!x) c.moveTo(x, y); else c.lineTo(x, y); } c.stroke(); }
      // 音符：从右往左流；位置由拍号的哈希决定（像一条旋律）
      const span = 16, pts = [];
      for (let j = Math.floor(b * 2) - 2; j < Math.floor(b * 2) + span * 2; j++) {
        const x = W * (1.05 - ((j / 2 - b) / span + .9) % 1.2);
        const x2 = W * (.1 + ((j / 2 - b)) / span * 1.0 + .0);
        const xx = W - (b * 2 - j) * (W / (span * 2)) - W * .5;
        if (xx < -20 || xx > W + 20) continue;
        const step = Math.round((h(j * 1.37 + (P.v || 0) * 9) - .5) * 9 + 2 * Math.sin(j * .35));
        const y = yAt(xx, 2) + step * gap * .5;
        pts.push([xx, y, j]);
      }
      pts.sort((a, b2) => a[0] - b2[0]);
      c.strokeStyle = rgba(G.acc, .55); c.lineWidth = lw(H, 1.6); c.beginPath();
      pts.forEach((p, i) => { if (!i) c.moveTo(p[0], p[1]); else c.lineTo(p[0], p[1]); }); c.stroke();
      for (const p of pts) {
        const near = Math.exp(-Math.abs(p[0] - W * .5) / (W * .05));
        c.fillStyle = rgba(near > .5 ? G.acc : G.ink, .5 + .5 * near);
        c.beginPath(); c.ellipse(p[0], p[1], gap * (.42 + .25 * near), gap * (.32 + .2 * near), -.35, 0, TAU); c.fill();
        c.strokeStyle = rgba(G.ink, .6); c.lineWidth = lw(H, 1.2); c.beginPath(); c.moveTo(p[0] + gap * .4, p[1]); c.lineTo(p[0] + gap * .4, p[1] - gap * 2.6); c.stroke();
      }
      c.strokeStyle = rgba(G.acc2, .5); c.lineWidth = lw(H, 1); c.beginPath(); c.moveTo(W * .5, mid - gap * 4); c.lineTo(W * .5, mid + gap * 4); c.stroke();
    },
    /* 同心圆环：每小节向外展开一圈；环有缺口，相邻环反向旋转；能量高时环变多、间距变密 */
    l_rings(c, W, H, t, P) {
      const G = ground(P); base(c, W, H, G);
      const cx = W * (.5 + (P.cx - .5) * .2), cy = H * .5, b = beatOf(t, P), R = Math.hypot(W, H) * .56;
      const n = 7 + Math.round(9 * P.I), grow = (b / 4) % 1;
      for (let i = 0; i < n; i++) {
        const u = (i + grow) / n, r = R * u * u * .98 + H * .03;
        const gapA = .25 + 1.6 * h(i * 3.1 + Math.floor(b / 4) * .37), rot = t * (.12 + .05 * i) * (i % 2 ? 1 : -1) + i;
        const hot = i % 4 === 1;
        c.strokeStyle = rgba(hot ? G.acc : G.ink, (hot ? .85 : (G.light ? .44 : .4)) * U.sstep(0, .12, u) * (1 - U.sstep(.85, 1, u)));
        c.lineWidth = lw(H, hot ? 2.4 + 2 * hit(t, P) : 1.1);
        c.beginPath(); c.arc(cx, cy, r, rot, rot + TAU - gapA); c.stroke();
        if (hot) { const a = rot + TAU - gapA; c.fillStyle = rgba(G.acc, .9); c.beginPath(); c.arc(cx + Math.cos(a) * r, cy + Math.sin(a) * r, lw(H, 4), 0, TAU); c.fill(); }
      }
    },
    /* 透视网格：线框地面往前流动；每拍一条横线被点亮向前扫 */
    l_grid(c, W, H, t, P) {
      const G = ground(P); base(c, W, H, G);
      const hz = H * .46, b = beatOf(t, P);
      c.lineWidth = lw(H, 1.1);
      for (let i = -14; i <= 14; i++) {
        c.strokeStyle = rgba(G.ink, G.light ? .26 : .3);
        c.beginPath(); c.moveTo(W / 2 + i * W * .012, hz); c.lineTo(W / 2 + i * W * .16, H); c.stroke();
      }
      for (let k = 0; k < 14; k++) {
        const z = ((k + (b * .5) % 1) / 14), y = hz + (H - hz) * z * z;
        const lit = Math.floor(b) % 14 === (13 - k);
        c.strokeStyle = rgba(lit ? G.acc : G.ink, lit ? .9 : (G.light ? .38 : .3) * (.4 + z));
        c.lineWidth = lw(H, lit ? 2.6 : 1.1);
        c.beginPath(); c.moveTo(0, y); c.lineTo(W, y); c.stroke();
      }
      // 天空里一个线框圆（太阳/月亮），随能量画完整
      const r = H * .16, a1 = TAU * U.clamp(.25 + .75 * P.I);
      c.strokeStyle = rgba(G.acc2, .8); c.lineWidth = lw(H, 2); c.beginPath(); c.arc(W * .5, hz - r * .3, r, -Math.PI / 2, -Math.PI / 2 + a1); c.stroke();
    },
    /* 多边形展开：一个 N 边形（线）复制旋转 m 份 → 花/玫瑰线；m 随能量从 1 长到 24 */
    l_poly(c, W, H, t, P) {
      const G = ground(P); base(c, W, H, G);
      const cx = W * (.5 + (P.cx - .5) * .25), cy = H * .5, N = 3 + Math.floor((P.v || 0) * 4), b = beatOf(t, P);
      const m = 1 + Math.round(23 * U.sstep(.15, .85, P.I)), R = H * (.3 + .08 * hit(t, P, 2));
      for (let j = 0; j < m; j++) {
        const rot = t * .08 + j * TAU / (m * N), k = j / Math.max(1, m - 1);
        c.strokeStyle = rgba(j === Math.floor(b) % m ? G.acc : G.ink, j === Math.floor(b) % m ? .95 : (G.light ? .4 : .34));
        c.lineWidth = lw(H, j === Math.floor(b) % m ? 2.4 : 1);
        c.beginPath();
        for (let i = 0; i <= N; i++) { const a = rot + i * TAU / N, r = R * (1 + .25 * Math.sin(k * Math.PI)); const x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r; if (!i) c.moveTo(x, y); else c.lineTo(x, y); }
        c.stroke();
      }
    },
    /* 李萨如：一笔连续的曲线；频率比按段落换（3:2、5:4、7:4…），相位随时间走，像在「画」*/
    l_lissa(c, W, H, t, P) {
      const G = ground(P); base(c, W, H, G);
      const R = [[3, 2], [5, 4], [3, 4], [7, 4], [5, 3], [2, 3]], q = R[Math.floor((P.v || 0) * R.length) % R.length];
      const cx = W / 2, cy = H / 2, ax = W * .36, ay = H * .36, ph = t * .35, n = 520, drawn = U.clamp(.25 + .75 * P.I);
      for (let layer = 0; layer < 3; layer++) {
        c.strokeStyle = rgba(layer === 0 ? G.acc : G.ink, layer === 0 ? .9 : (G.light ? .38 : .3));
        c.lineWidth = lw(H, layer === 0 ? 2 + 1.5 * hit(t, P) : 1);
        c.beginPath();
        for (let i = 0; i <= n * drawn; i++) {
          const u = i / n * TAU, s = 1 - layer * .16;
          const x = cx + ax * s * Math.sin(q[0] * u + ph + layer * .5), y = cy + ay * s * Math.sin(q[1] * u + layer * .3);
          if (!i) c.moveTo(x, y); else c.lineTo(x, y);
        }
        c.stroke();
      }
    },
    /* 节拍器：摆杆按 BPM 来回，背后刻度弧；到两端时「嗒」一下亮 */
    l_tick(c, W, H, t, P) {
      const G = ground(P); base(c, W, H, G);
      const b = beatOf(t, P), ang = Math.sin(b * Math.PI) * .62, px = W * .5, py = H * .86, L = H * .68;
      c.strokeStyle = rgba(G.ink, G.light ? .32 : .38); c.lineWidth = lw(H, 1.1);
      for (let i = -12; i <= 12; i++) { const a = -Math.PI / 2 + i * .055, r0 = L * (i % 3 ? 1.02 : .98), r1 = L * 1.07; c.beginPath(); c.moveTo(px + Math.cos(a) * r0, py + Math.sin(a) * r0); c.lineTo(px + Math.cos(a) * r1, py + Math.sin(a) * r1); c.stroke(); }
      const a = -Math.PI / 2 + ang, tx = px + Math.cos(a) * L, ty = py + Math.sin(a) * L, edge = Math.pow(Math.abs(Math.sin(b * Math.PI)), 8);
      c.strokeStyle = rgba(G.acc, .9); c.lineWidth = lw(H, 3); c.beginPath(); c.moveTo(px, py); c.lineTo(tx, ty); c.stroke();
      const wy = .45 + .2 * (P.v || 0);
      c.fillStyle = rgba(G.acc, 1); c.fillRect(px + Math.cos(a) * L * wy - lw(H, 14), py + Math.sin(a) * L * wy - lw(H, 9), lw(H, 28), lw(H, 18));
      c.strokeStyle = rgba(G.ink, .7); c.lineWidth = lw(H, 1.6);
      c.beginPath(); c.moveTo(px - H * .16, py + H * .06); c.lineTo(px - H * .07, py - H * .62); c.lineTo(px + H * .07, py - H * .62); c.lineTo(px + H * .16, py + H * .06); c.closePath(); c.stroke();
      c.fillStyle = rgba(G.acc2, edge * .9); c.beginPath(); c.arc(tx, ty, lw(H, 9) * (1 + edge), 0, TAU); c.fill();
    },
    /* 手书底：整面淡淡的手写体歌词（由运行时提供文本），多行倾斜排布；当前这一句加深 */
    l_script(c, W, H, t, P) {
      const G = ground(P); base(c, W, H, G);
      const A = global.__NEKO_ART, lines = (A && A.scriptLines) || [], cur = (A && A.scriptCur) || 0;
      if (!lines.length) { LINES.l_wave(c, W, H, t, P); return; }
      const fs = H * .058, rows = Math.ceil(H / (fs * 1.55)) + 2;
      c.save(); c.translate(W / 2, H / 2); c.rotate(-.06); c.translate(-W / 2, -H / 2);
      c.font = 'italic ' + Math.round(fs) + 'px "Segoe Script","Bradley Hand ITC","STXingkai","KaiTi","Yu Mincho",cursive';
      c.textBaseline = 'middle';
      for (let r = 0; r < rows; r++) {
        const idx = ((cur - Math.floor(rows / 2) + r) % lines.length + lines.length) % lines.length, isCur = r === Math.floor(rows / 2);
        const y = (r - .5) * fs * 1.55 - ((t * 4) % (fs * 1.55)) * 0, off = Math.sin(r * 1.7) * W * .05 - (t * 6 * (r % 2 ? 1 : -1)) % (W * .2);
        c.fillStyle = rgba(isCur ? G.acc : G.ink, isCur ? .55 : (G.light ? .14 : .16));
        c.fillText((lines[idx] + '　　').repeat(3), -W * .1 + off, y);
      }
      c.restore();
      // 页边一条竖线 + 横线（稿纸感）
      c.strokeStyle = rgba(G.acc, .35); c.lineWidth = lw(H, 1.2); c.beginPath(); c.moveTo(W * .08, 0); c.lineTo(W * .08, H); c.stroke();
    }
  };
  Object.assign(S.SCENES, LINES);
  if (S.FRONT_OF) Object.keys(LINES).forEach(k => { S.FRONT_OF[k] = null; });   // 线条场景不加前景粒子（保持干净）
  S.LINE_SCENES = Object.keys(LINES);

  /* ------------------------------ 间奏插画（伴奏等待） ------------------------------
   * 没有歌词的伴奏段：画面交给一幅「线稿小画」，人物缩到一角。每种都是几条线 + 一个强调色，跟拍子动。*/
  const VIGNETTES = {
    piano(c, W, H, t, P, G) {       // 斜俯视的琴键，一只「手」的落点用圆表示，按拍子走
      const b = beatOf(t, P); c.save(); c.translate(W * .5, H * .55); c.transform(1, -.18, .35, .7, 0, 0);
      const n = 16, kw = W * .045, kh = H * .42;
      for (let i = 0; i < n; i++) {
        const e = Math.exp(-((b * 2 - Math.floor(b * 2)) + ((Math.floor(b * 2) * 5 + 3) % n === i ? 0 : 9)) * .6);
        const x = (i - n / 2) * kw;
        if (e > .05) { c.fillStyle = rgba(G.acc, .75 * e); c.fillRect(x, -kh / 2, kw, kh); }
        c.strokeStyle = rgba(G.ink, .75); c.lineWidth = lw(H, 1.6); c.strokeRect(x, -kh / 2, kw, kh);
        if (![2, 6].includes(i % 7) && i < n - 1) { c.fillStyle = rgba(G.ink, .85); c.fillRect(x + kw * .68, -kh / 2, kw * .64, kh * .58); }
      }
      c.restore();
    },
    metronome(c, W, H, t, P, G) { LINES.l_tick(c, W, H, t, Object.assign({}, P, { ground: null })); },
    clock(c, W, H, t, P, G) {       // 钟：秒针按拍跳，分针慢走
      const cx = W * .5, cy = H * .5, R = H * .3, b = beatOf(t, P);
      c.strokeStyle = rgba(G.ink, .8); c.lineWidth = lw(H, 3); c.beginPath(); c.arc(cx, cy, R, 0, TAU); c.stroke();
      c.lineWidth = lw(H, 1.5); c.beginPath(); c.arc(cx, cy, R * 1.12, 0, TAU); c.stroke();
      for (let i = 0; i < 12; i++) { const a = i * TAU / 12; c.lineWidth = lw(H, i % 3 ? 1.5 : 4); c.beginPath(); c.moveTo(cx + Math.cos(a) * R * .84, cy + Math.sin(a) * R * .84); c.lineTo(cx + Math.cos(a) * R * .93, cy + Math.sin(a) * R * .93); c.stroke(); }
      const sa = (Math.floor(b) + U.easeOut(U.clamp((b - Math.floor(b)) * 4))) * TAU / 16 - Math.PI / 2, ma = t * .05 - Math.PI / 2;
      c.strokeStyle = rgba(G.ink, .9); c.lineWidth = lw(H, 5); c.beginPath(); c.moveTo(cx, cy); c.lineTo(cx + Math.cos(ma) * R * .5, cy + Math.sin(ma) * R * .5); c.stroke();
      c.strokeStyle = rgba(G.acc, 1); c.lineWidth = lw(H, 2.5); c.beginPath(); c.moveTo(cx, cy); c.lineTo(cx + Math.cos(sa) * R * .78, cy + Math.sin(sa) * R * .78); c.stroke();
      c.fillStyle = rgba(G.acc, 1); c.beginPath(); c.arc(cx, cy, lw(H, 7), 0, TAU); c.fill();
    },
    pulse(c, W, H, t, P, G) {       // 心电/声波：一条线从左画到右，拍点上起一个峰
      const b = beatOf(t, P), y0 = H * .5;
      c.strokeStyle = rgba(G.acc, .95); c.lineWidth = lw(H, 3.5); c.lineJoin = 'round'; c.beginPath();
      for (let x = 0; x <= W; x += W / 300) {
        const bb = b - (1 - x / W) * 8, f = bb - Math.floor(bb);
        const spike = Math.exp(-Math.pow((f - .12) * 16, 2)) * 1 - Math.exp(-Math.pow((f - .2) * 22, 2)) * .45 + Math.exp(-Math.pow((f - .55) * 9, 2)) * .16;
        const y = y0 - spike * H * .26 * (.6 + .6 * P.I);
        if (!x) c.moveTo(x, y); else c.lineTo(x, y);
      }
      c.stroke();
      c.strokeStyle = rgba(G.ink, .25); c.lineWidth = lw(H, 1); for (let k = 1; k < 8; k++) { c.beginPath(); c.moveTo(W * k / 8, H * .2); c.lineTo(W * k / 8, H * .8); c.stroke(); }
    },
    record(c, W, H, t, P, G) {      // 唱片：同心细纹 + 唱针
      const cx = W * .47, cy = H * .52, R = H * .33;
      c.strokeStyle = rgba(G.ink, .85); c.lineWidth = lw(H, 3); c.beginPath(); c.arc(cx, cy, R, 0, TAU); c.stroke();
      c.lineWidth = lw(H, 1); for (let i = 3; i < 16; i++) { const r = R * i / 17, a0 = t * .9 + i; c.strokeStyle = rgba(G.ink, .4); c.beginPath(); c.arc(cx, cy, r, a0, a0 + TAU * .82); c.stroke(); }
      c.fillStyle = rgba(G.acc, .9); c.beginPath(); c.arc(cx, cy, R * .16, 0, TAU); c.fill();
      c.strokeStyle = rgba(G.ink, .9); c.lineWidth = lw(H, 4); c.beginPath(); c.moveTo(cx + R * 1.25, cy - R * 1.0); c.lineTo(cx + R * .62 + Math.sin(t * 3) * 2, cy - R * .12); c.stroke();
      c.fillStyle = rgba(G.acc2, 1); c.beginPath(); c.arc(cx + R * 1.25, cy - R * 1.0, lw(H, 10), 0, TAU); c.fill();
    },
    globe(c, W, H, t, P, G) {       // 线框地球 + 环（かくれんぼ 里那种）
      const cx = W * .5, cy = H * .5, R = H * .28;
      c.strokeStyle = rgba(G.ink, .75); c.lineWidth = lw(H, 2.2); c.beginPath(); c.arc(cx, cy, R, 0, TAU); c.stroke();
      c.lineWidth = lw(H, 1); for (let i = 1; i < 6; i++) { const k = Math.cos(i / 6 * Math.PI + t * .25); c.beginPath(); c.ellipse(cx, cy, Math.abs(k) * R, R, 0, 0, TAU); c.stroke(); }
      for (let i = 1; i < 5; i++) { const yy = (i / 5 - .5) * 2 * R; c.beginPath(); c.ellipse(cx, cy + yy, Math.sqrt(Math.max(0, R * R - yy * yy)), R * .08, 0, 0, TAU); c.stroke(); }
      c.strokeStyle = rgba(G.acc, .9); c.lineWidth = lw(H, 2.5); c.beginPath(); c.ellipse(cx, cy, R * 1.55, R * .42, -.32, t * .5, t * .5 + TAU * .8); c.stroke();
    },
    equalizer(c, W, H, t, P, G) {   // 均衡器：一排竖线，高度跟能量；顶上一个小横杠缓慢回落
      const n = 32, b = beatOf(t, P);
      for (let i = 0; i < n; i++) {
        const x = W * (.12 + .76 * i / (n - 1)), e = (.25 + .75 * P.I) * (.3 + .7 * Math.abs(Math.sin(i * .9 + Math.floor(b * 2) * 1.7))) * (.6 + .4 * hit(t, P, .5));
        const hh = H * .42 * e;
        c.strokeStyle = rgba(i % 4 ? G.ink : G.acc, i % 4 ? .7 : 1); c.lineWidth = lw(H, 5); c.lineCap = 'round';
        c.beginPath(); c.moveTo(x, H * .5 - hh); c.lineTo(x, H * .5 + hh); c.stroke();
      }
      c.lineCap = 'butt';
    }
  };
  // 伴奏段：找没有歌词的时间窗（≥ minGap 秒）
  function findGaps(show, minGap) {
    const L = ((show.lines || []).filter(l => l.text && String(l.text).trim())).slice().sort((a, b) => a.t0 - b.t0);
    const out = [], dur = +show.duration || 0, mg = minGap || 7;
    let prev = 0;
    for (const l of L) { if (l.t0 - prev >= mg) out.push([prev + (prev ? .6 : 0), l.t0 - .8]); prev = Math.max(prev, l.t1 || l.t0); }
    if (dur - prev >= mg) out.push([prev + .6, dur - .5]);
    return out.filter(g => g[1] - g[0] >= mg - 1.5);
  }
  /* 画间奏插画到一个覆盖层。a = 0..1（进出场），返回人物该让到哪（cx、缩放系数）*/
  function drawVignette(c, W, H, t, kind, a, P) {
    c.clearRect(0, 0, W, H);
    if (a <= 0) return;
    const G = ground(P), k = U.easeInOut(a);
    // 底：纸色面板从中间向两边展开（一条线 → 展开成面）
    c.save();
    const hh = H * k;
    c.beginPath(); c.rect(0, H / 2 - hh / 2, W, hh); c.clip();
    base(c, W, H, G);
    c.globalAlpha = U.clamp((a - .35) / .5);
    (VIGNETTES[kind] || VIGNETTES.pulse)(c, W, H, t, P, G);
    c.restore();
    // 展开时上下两条边线
    c.strokeStyle = rgba(G.acc, .9 * (1 - U.sstep(.85, 1, a) * .6)); c.lineWidth = lw(H, 2);
    c.beginPath(); c.moveTo(0, H / 2 - hh / 2); c.lineTo(W, H / 2 - hh / 2); c.moveTo(0, H / 2 + hh / 2); c.lineTo(W, H / 2 + hh / 2); c.stroke();
  }

  /* ------------------------------ 线条过场（不遮人物的那种：线在画面里生长/收缩） ------------------------------
   * 段落交界前后 1 小节：一条线从一侧画过 → 分裂成多条 → 变成下一个场景的骨架。画在背景层之上、人物之下。*/
  function drawLineBridge(c, W, H, t, t0, bar, P) {
    const d = t - t0, G = ground(P);
    if (d < -bar || d > bar * .9) return false;
    const u = d < 0 ? 1 + d / bar : 1 - d / (bar * .9);        // 0→1→0
    const k = U.easeInOut(U.clamp(u)), n = 1 + Math.round(10 * k);
    for (let i = 0; i < n; i++) {
      const off = (i - (n - 1) / 2) * H * .045 * k, prog = d < 0 ? U.easeOut(U.clamp(1 + d / bar)) : 1;
      c.strokeStyle = rgba(i === Math.floor(n / 2) ? G.acc : G.ink, (i === Math.floor(n / 2) ? .95 : .5) * U.clamp(u * 2.5));
      c.lineWidth = lw(H, i === Math.floor(n / 2) ? 3 : 1.4);
      c.beginPath();
      const x1 = W * prog;
      for (let x = 0; x <= x1; x += W / 90) {
        const y = H * .5 + off + Math.sin(x / W * 9 + t * 5 + i * .6) * H * .03 * k * (d > 0 ? 1 - d / (bar * .9) : 1);
        if (!x) c.moveTo(x, y); else c.lineTo(x, y);
      }
      c.stroke();
    }
    return true;
  }

  global.NekoStageLines = { LINES, VIGNETTES, ground, findGaps, drawVignette, drawLineBridge, kinds: Object.keys(VIGNETTES) };
})(window);
