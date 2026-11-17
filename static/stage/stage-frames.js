/* =============================================================================
 * 「为她而唱」舞台 · 第四十二轮：边框扩展包
 *   1) 整体大边框 24 种（在原有 13 种之外）：每种都是「参数化」的——同一种也会因
 *      线宽/圆角/角饰/颜色/装饰数量不同而长得不一样（genome），所以几乎不会重复。
 *   2) 歌词打底框 20 种：在舞台自由歌词每一句后面画一块底（便签、气泡、胶带、票根、
 *      霓虹框、毛玻璃、对话框、笔刷、圆角胶囊……），由歌词层按句子选用。
 *   3) 进化：学到的边框「基因」（genome）可以和别的基因交叉 / 变异，得到新边框。
 * 全部 Canvas 2D 程序绘制。
 * ========================================================================== */
(function (global) {
  'use strict';
  const U = global.NekoStageU;
  if (!U) return;
  const h = U.h01;
  const rgba = (c, a) => U.rgba(c, U.clamp(a, 0, 1));

  function rr(c, x, y, w, hh, r) { r = Math.max(0, Math.min(r, w / 2, hh / 2)); c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + hh, r); c.arcTo(x + w, y + hh, x, y + hh, r); c.arcTo(x, y + hh, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath(); }
  function hole(c, w, hh, color, x, y, iw, ih, r) { c.save(); c.fillStyle = color; c.fillRect(0, 0, w, hh); c.globalCompositeOperation = 'destination-out'; c.fillStyle = '#000'; rr(c, x, y, iw, ih, r); c.fill(); c.restore(); }   // 第四十五轮修：不透明挖洞，不再给画面中间蒙灰
  function star(c, x, y, r) { c.beginPath(); for (let k = 0; k < 10; k++) { const a = -Math.PI / 2 + k * Math.PI / 5, q = k % 2 ? r * .45 : r; c.lineTo(x + Math.cos(a) * q, y + Math.sin(a) * q); } c.closePath(); c.fill(); }
  function heart(c, x, y, r) { c.beginPath(); c.moveTo(x, y + r * .9); c.bezierCurveTo(x - r * 1.6, y - r * .1, x - r * .7, y - r * 1.3, x, y - r * .45); c.bezierCurveTo(x + r * .7, y - r * 1.3, x + r * 1.6, y - r * .1, x, y + r * .9); c.fill(); }
  function spark(c, x, y, r) { c.beginPath(); c.moveTo(x, y - r); c.quadraticCurveTo(x, y, x + r * .3, y); c.quadraticCurveTo(x, y, x, y + r); c.quadraticCurveTo(x, y, x - r * .3, y); c.quadraticCurveTo(x, y, x, y - r); c.fill(); }
  function flower(c, x, y, r) { for (let k = 0; k < 5; k++) { const a = k * 1.2566; c.beginPath(); c.arc(x + Math.cos(a) * r * .55, y + Math.sin(a) * r * .55, r * .45, 0, 6.283); c.fill(); } }

  /* ------------------------------------------------------------------ 基因 */
  // genome：一个边框的全部可变参数。kind 决定画法，其余决定长相。
  function genome(kind, seed, pal) {
    const r = k => h(seed * .00123 + k * 1.917);
    return {
      kind, seed,
      margin: .012 + .03 * r(1), radius: r(2) < .35 ? 0 : .01 + .06 * r(3), line: 1.5 + 4 * r(4),
      double: r(5) < .35, dash: r(6) < .2, corner: ['none', 'star', 'heart', 'spark', 'flower', 'dot', 'bracket', 'diamond'][Math.floor(r(7) * 8)],
      cornerSize: .02 + .03 * r(8), side: ['none', 'dots', 'ticks', 'waves', 'chain', 'scallop'][Math.floor(r(9) * 6)],
      opacity: .55 + .4 * r(10), glow: r(11) < .4 ? 0 : .006 + .012 * r(12), tint: Math.floor(r(13) * 3),
      bar: r(14) < .5 ? 0 : .04 + .05 * r(15), tilt: (r(16) - .5) * .02, count: 4 + Math.floor(r(17) * 14),
      cols: [pal.hot || '#ff6fae', pal.c1 || '#7fd8ff', pal.c2 || '#ffe066', '#ffffff']
    };
  }
  // 交叉 + 变异：两三个父本 → 新基因（学到的边框的「进化」）
  function breed(parents, seed) {
    const r = k => h(seed * .00077 + k * 2.31);
    const keys = Object.keys(parents[0]);
    const kid = {};
    keys.forEach((k, i) => { kid[k] = parents[Math.floor(r(i) * parents.length)][k]; });
    ['margin', 'radius', 'line', 'cornerSize', 'opacity', 'glow', 'bar'].forEach((k, i) => { if (typeof kid[k] === 'number') kid[k] = Math.max(0, kid[k] * (.8 + .4 * r(50 + i))); });
    kid.seed = (seed >>> 0);
    return kid;
  }

  /* ------------------------------------------------------------------ 大边框 */
  const FRAME_KINDS = ['ribbon', 'stitch', 'washi', 'starline', 'heartline', 'bubbleframe', 'filmstrip2', 'arcade', 'notebook',
    'postcard', 'stamp', 'neontube', 'glitchframe', 'cyberbrackets', 'goldleaf', 'artdeco', 'chinoise', 'cloudframe',
    'flowerwreath', 'pixel', 'comic', 'window', 'ticket', 'gradientring'];

  function decoCorner(c, g, x, y, sz, col) {
    c.fillStyle = col; c.strokeStyle = col;
    if (g.corner === 'star') star(c, x, y, sz);
    else if (g.corner === 'heart') heart(c, x, y, sz * .8);
    else if (g.corner === 'spark') spark(c, x, y, sz);
    else if (g.corner === 'flower') flower(c, x, y, sz);
    else if (g.corner === 'dot') { c.beginPath(); c.arc(x, y, sz * .45, 0, 6.283); c.fill(); }
    else if (g.corner === 'diamond') { c.beginPath(); c.moveTo(x, y - sz); c.lineTo(x + sz * .6, y); c.lineTo(x, y + sz); c.lineTo(x - sz * .6, y); c.fill(); }
  }
  function sideDeco(c, g, W, H, m, col) {
    if (g.side === 'none') return;
    c.fillStyle = col; c.strokeStyle = col; c.lineWidth = 1.5;
    const step = H * .04;
    const pts = [];
    for (let x = m + step; x < W - m - step; x += step) pts.push([x, m], [x, H - m]);
    for (let y = m + step; y < H - m - step; y += step) pts.push([m, y], [W - m, y]);
    pts.forEach(([x, y], i) => {
      if (g.side === 'dots') { c.beginPath(); c.arc(x, y, H * .004, 0, 6.283); c.fill(); }
      else if (g.side === 'ticks' && i % 2 === 0) { c.fillRect(x - 1, y - H * .006, 2, H * .012); }
      else if (g.side === 'scallop') { c.beginPath(); c.arc(x, y, step * .45, 0, 6.283); c.globalAlpha = .4; c.fill(); c.globalAlpha = 1; }
    });
    if (g.side === 'waves') {
      c.beginPath();
      for (let x = m; x <= W - m; x += 4) { const y = m + Math.sin(x / H * 40) * H * .005; if (x === m) c.moveTo(x, y); else c.lineTo(x, y); }
      c.stroke(); c.beginPath();
      for (let x = m; x <= W - m; x += 4) { const y = H - m + Math.sin(x / H * 40) * H * .005; if (x === m) c.moveTo(x, y); else c.lineTo(x, y); }
      c.stroke();
    }
    if (g.side === 'chain') { c.setLineDash([H * .012, H * .008]); c.strokeRect(m - H * .008, m - H * .008, W - 2 * m + H * .016, H - 2 * m + H * .016); c.setLineDash([]); }
  }

  function drawFrame(c, W, H, g, title) {
    c.clearRect(0, 0, W, H);
    c.save();
    const m = H * g.margin * 2.4, r = H * g.radius, col = g.cols[g.tint], acc = g.cols[(g.tint + 1) % 3], lw = g.line * H / 1080;
    const sans = '"Noto Sans SC","Microsoft YaHei",sans-serif', serif = '"Noto Serif SC","Songti SC",serif';
    const strokeMain = () => {
      c.lineWidth = lw; c.strokeStyle = rgba(col, g.opacity);
      if (g.dash) c.setLineDash([lw * 5, lw * 3]);
      if (g.glow) { c.shadowColor = col; c.shadowBlur = H * g.glow; }
      rr(c, m, m, W - 2 * m, H - 2 * m, r); c.stroke();
      if (g.double) { const d = lw * 3; rr(c, m + d, m + d, W - 2 * m - 2 * d, H - 2 * m - 2 * d, Math.max(0, r - d)); c.lineWidth = lw * .5; c.stroke(); }
      c.setLineDash([]); c.shadowBlur = 0;
    };
    const corners = () => { const sz = H * g.cornerSize; [[m, m], [W - m, m], [m, H - m], [W - m, H - m]].forEach(([x, y]) => decoCorner(c, g, x, y, sz, acc)); };
    const titleText = (x, y, align, font) => { if (!title) return; c.font = font; c.textAlign = align; c.textBaseline = 'middle'; c.fillStyle = 'rgba(255,255,255,.92)'; c.fillText(title, x, y); };
    switch (g.kind) {
      case 'ribbon': {   // 上方丝带横幅 + 细边
        strokeMain(); const bw = W * .36, bh = H * .055, x0 = (W - bw) / 2, y0 = m - bh / 2;
        c.fillStyle = rgba(col, .92); c.beginPath(); c.moveTo(x0 - bh * .6, y0 + bh * .1); c.lineTo(x0, y0); c.lineTo(x0 + bw, y0); c.lineTo(x0 + bw + bh * .6, y0 + bh * .1); c.lineTo(x0 + bw + bh * .3, y0 + bh * .5); c.lineTo(x0 + bw + bh * .6, y0 + bh * .9); c.lineTo(x0 + bw, y0 + bh); c.lineTo(x0, y0 + bh); c.lineTo(x0 - bh * .6, y0 + bh * .9); c.lineTo(x0 - bh * .3, y0 + bh * .5); c.closePath(); c.fill();
        titleText(W / 2, y0 + bh / 2, 'center', '700 ' + Math.round(bh * .48) + 'px ' + sans); corners(); break;
      }
      case 'stitch': {   // 缝线（虚线双框 + 布纹角）
        c.setLineDash([H * .012, H * .008]); c.lineWidth = Math.max(2, lw); c.strokeStyle = rgba('#ffffff', .85); rr(c, m, m, W - 2 * m, H - 2 * m, r); c.stroke();
        c.strokeStyle = rgba(col, .8); rr(c, m + H * .012, m + H * .012, W - 2 * m - H * .024, H - 2 * m - H * .024, r); c.stroke(); c.setLineDash([]); corners(); break;
      }
      case 'washi': {    // 纸胶带贴四角
        strokeMain();
        [[m, m, -.6], [W - m, m, .6], [m, H - m, .6], [W - m, H - m, -.6]].forEach(([x, y, a], i) => {
          c.save(); c.translate(x, y); c.rotate(a); c.fillStyle = rgba(g.cols[i % 3], .7); c.fillRect(-H * .08, -H * .022, H * .16, H * .044);
          c.fillStyle = 'rgba(255,255,255,.35)'; for (let k = -3; k <= 3; k++) c.fillRect(k * H * .022, -H * .022, H * .008, H * .044); c.restore();
        }); break;
      }
      case 'starline': case 'heartline': {   // 一圈小星星/爱心
        const f = g.kind === 'starline' ? star : heart;
        const n = 18 + g.count * 2, per = (W + H) * 2;
        for (let i = 0; i < n; i++) {
          let d = i / n * per, x, y;
          if (d < W) { x = d; y = m; } else if ((d -= W) < H) { x = W - m; y = d; } else if ((d -= H) < W) { x = W - d; y = H - m; } else { d -= W; x = m; y = H - d; }
          c.fillStyle = rgba(g.cols[i % 3], g.opacity); f(c, x, y, H * (.008 + .008 * h(i + g.seed)));
        }
        break;
      }
      case 'bubbleframe': {   // 泡泡串
        for (let i = 0; i < 120; i++) {
          const side = i % 4, u = h(i * 3.1 + g.seed);
          const x = side < 2 ? u * W : (side === 2 ? m * (.5 + h(i)) : W - m * (.5 + h(i))), y = side < 2 ? (side === 0 ? m * (.5 + h(i)) : H - m * (.5 + h(i))) : u * H;
          c.strokeStyle = 'rgba(255,255,255,.75)'; c.lineWidth = 1.5; c.beginPath(); c.arc(x, y, H * (.006 + .016 * h(i * 7)), 0, 6.283); c.stroke();
        }
        break;
      }
      case 'filmstrip2': {   // 竖向胶片（左右两侧）
        const bw = W * .045;
        c.fillStyle = 'rgba(12,10,14,.92)'; c.fillRect(0, 0, bw, H); c.fillRect(W - bw, 0, bw, H);
        c.fillStyle = rgba('#fff6e0', .85);
        for (let y = H * .02; y < H; y += H * .055) { rr(c, bw * .3, y, bw * .4, H * .03, 3); c.fill(); rr(c, W - bw * .7, y, bw * .4, H * .03, 3); c.fill(); }
        break;
      }
      case 'arcade': {   // 街机框：粗外框 + 两侧灯点 + 底部「INSERT COIN」
        hole(c, W, H, rgba('#14102a', .9), m, m, W - 2 * m, H - 2 * m, r);
        for (let i = 0; i < 16; i++) { c.fillStyle = rgba(g.cols[i % 3], .9); c.beginPath(); c.arc(m * .5, H * (i + .5) / 16, m * .22, 0, 6.283); c.fill(); c.beginPath(); c.arc(W - m * .5, H * (i + .5) / 16, m * .22, 0, 6.283); c.fill(); }
        c.font = '700 ' + Math.round(m * .45) + 'px "Consolas",monospace'; c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillStyle = rgba(acc, .95); c.fillText(title ? '★ ' + title + ' ★' : 'INSERT COIN', W / 2, H - m / 2); break;
      }
      case 'notebook': {   // 笔记本：左侧线圈 + 横格线
        c.fillStyle = 'rgba(250,248,240,.88)'; c.fillRect(0, 0, W * .05, H);
        c.strokeStyle = 'rgba(120,120,140,.9)'; c.lineWidth = 3;
        for (let y = H * .04; y < H; y += H * .05) { c.beginPath(); c.ellipse(W * .05, y, W * .012, H * .012, 0, 0, 6.283); c.stroke(); }
        c.strokeStyle = rgba(col, .18); c.lineWidth = 1; for (let y = H * .12; y < H; y += H * .06) { c.beginPath(); c.moveTo(W * .06, y); c.lineTo(W, y); c.stroke(); }
        c.strokeStyle = rgba('#ff6a6a', .35); c.beginPath(); c.moveTo(W * .1, 0); c.lineTo(W * .1, H); c.stroke(); break;
      }
      case 'postcard': {   // 明信片：白边 + 右上邮票 + 斜纹航空边
        hole(c, W, H, 'rgba(252,250,246,.95)', m, m, W - 2 * m, H - 2 * m, 0);
        for (let x = -H; x < W + H; x += H * .04) { c.fillStyle = (Math.floor(x / (H * .04)) % 2) ? rgba('#e85a6a', .8) : rgba('#4a7ae8', .8); c.beginPath(); c.moveTo(x, 0); c.lineTo(x + H * .02, 0); c.lineTo(x + H * .02 - m * .4, m * .4); c.lineTo(x - m * .4, m * .4); c.fill(); }
        c.fillStyle = rgba(col, .9); c.fillRect(W - m - W * .07, m * 1.4, W * .055, H * .09); c.strokeStyle = '#fff'; c.setLineDash([3, 3]); c.strokeRect(W - m - W * .07, m * 1.4, W * .055, H * .09); c.setLineDash([]); break;
      }
      case 'stamp': {    // 邮票齿孔边
        hole(c, W, H, rgba('#fbf7ee', .95), m, m, W - 2 * m, H - 2 * m, 0);
        c.save(); c.globalCompositeOperation = 'destination-out'; c.fillStyle = '#000';
        for (let x = 0; x < W; x += H * .03) { c.beginPath(); c.arc(x, 0, H * .01, 0, 6.283); c.fill(); c.beginPath(); c.arc(x, H, H * .01, 0, 6.283); c.fill(); }
        for (let y = 0; y < H; y += H * .03) { c.beginPath(); c.arc(0, y, H * .01, 0, 6.283); c.fill(); c.beginPath(); c.arc(W, y, H * .01, 0, 6.283); c.fill(); }
        c.restore(); break;
      }
      case 'neontube': { // 霓虹灯管（双色两段，带断口）
        c.lineCap = 'round'; c.lineWidth = Math.max(4, lw * 1.6);
        [[col, [[m, H * .3], [m, m], [W * .4, m]]], [acc, [[W * .6, H - m], [W - m, H - m], [W - m, H * .7]]]].forEach(([cc, pts]) => {
          c.shadowColor = cc; c.shadowBlur = H * .02; c.strokeStyle = rgba(cc, .95); c.beginPath(); pts.forEach(([x, y], i) => i ? c.lineTo(x, y) : c.moveTo(x, y)); c.stroke();
          c.shadowBlur = 0; c.strokeStyle = 'rgba(255,255,255,.8)'; c.lineWidth = Math.max(1.5, lw * .5); c.stroke(); c.lineWidth = Math.max(4, lw * 1.6);
        }); break;
      }
      case 'glitchframe': {   // 故障框：错位的 RGB 三层细框
        [['rgba(255,40,90,.7)', -3], ['rgba(40,220,255,.7)', 3], ['rgba(255,255,255,.85)', 0]].forEach(([cc, d]) => { c.strokeStyle = cc; c.lineWidth = 2; rr(c, m + d, m - d, W - 2 * m, H - 2 * m, r); c.stroke(); });
        for (let i = 0; i < 6; i++) { c.fillStyle = rgba(g.cols[i % 3], .6); c.fillRect(h(i + g.seed) * W, h(i * 3 + g.seed) < .5 ? m - 3 : H - m - 3, W * .05 * h(i * 7), 6); } break;
      }
      case 'cyberbrackets': {   // 赛博括号：四角折线 + 中点刻度 + 角标文字
        c.strokeStyle = rgba(col, .9); c.lineWidth = 3; const L = W * .06;
        [[m, m, 1, 1], [W - m, m, -1, 1], [m, H - m, 1, -1], [W - m, H - m, -1, -1]].forEach(([x, y, dx, dy]) => { c.beginPath(); c.moveTo(x + dx * L, y); c.lineTo(x + dx * 10, y); c.lineTo(x, y + dy * 10); c.lineTo(x, y + dy * L); c.stroke(); });
        c.fillStyle = rgba(col, .9); [[W / 2, m], [W / 2, H - m], [m, H / 2], [W - m, H / 2]].forEach(([x, y]) => c.fillRect(x - 12, y - 1.5, 24, 3));
        c.font = Math.round(H * .016) + 'px "Consolas",monospace'; c.textAlign = 'left'; c.textBaseline = 'top'; c.fillText('// ' + (title || 'STAGE.LIVE'), m + 16, m + 12); break;
      }
      case 'goldleaf': { // 金箔：粗金边渐变 + 角花
        const gg = c.createLinearGradient(0, 0, W, H); gg.addColorStop(0, '#f5d58a'); gg.addColorStop(.5, '#a87a2a'); gg.addColorStop(1, '#f5d58a');
        c.strokeStyle = gg; c.lineWidth = H * .012; rr(c, m, m, W - 2 * m, H - 2 * m, r); c.stroke();
        c.lineWidth = 1.5; rr(c, m + H * .018, m + H * .018, W - 2 * m - H * .036, H - 2 * m - H * .036, r); c.stroke();
        c.fillStyle = gg; [[m, m], [W - m, m], [m, H - m], [W - m, H - m]].forEach(([x, y]) => flower(c, x, y, H * .03)); break;
      }
      case 'artdeco': {  // 装饰艺术：阶梯角 + 扇形
        c.strokeStyle = rgba('#e8c87a', .9); c.lineWidth = 2;
        [[m, m, 1, 1], [W - m, m, -1, 1], [m, H - m, 1, -1], [W - m, H - m, -1, -1]].forEach(([x, y, dx, dy]) => {
          for (let k = 0; k < 4; k++) { const s = H * (.03 + k * .025); c.beginPath(); c.moveTo(x, y + dy * s); c.lineTo(x + dx * s, y + dy * s); c.lineTo(x + dx * s, y); c.stroke(); }
          for (let k = 0; k < 5; k++) { c.beginPath(); c.moveTo(x, y); c.lineTo(x + dx * Math.cos(k * .35) * H * .12, y + dy * Math.sin(k * .35) * H * .12); c.stroke(); }
        });
        c.strokeRect(m, m, W - 2 * m, H - 2 * m); break;
      }
      case 'chinoise': { // 中式回纹：四边回字纹带
        c.strokeStyle = rgba('#c8423a', .85); c.lineWidth = 2;
        const s = H * .022;
        const meander = (x0, y0, len, horiz) => { for (let k = 0; k < len / (s * 2); k++) { const x = horiz ? x0 + k * s * 2 : x0, y = horiz ? y0 : y0 + k * s * 2; c.strokeRect(x + s * .25, y + s * .25, s * 1.5, s * 1.5); c.strokeRect(x + s * .6, y + s * .6, s * .8, s * .8); } };
        meander(m, m - s, W - 2 * m, true); meander(m, H - m - s, W - 2 * m, true); meander(m - s, m, H - 2 * m, false); meander(W - m - s, m, H - 2 * m, false);
        c.fillStyle = rgba('#c8423a', .9); c.fillRect(W - m - H * .07, H * .08, H * .05, H * .05); c.fillStyle = '#fff'; c.font = '700 ' + Math.round(H * .03) + 'px ' + serif; c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillText('唱', W - m - H * .045, H * .105); break;
      }
      case 'cloudframe': {   // 云朵边：上下两排白云
        c.fillStyle = 'rgba(255,255,255,.8)';
        for (let x = -H * .05; x < W + H * .05; x += H * .06) { const r1 = H * (.03 + .02 * h(x * .01 + g.seed)); c.beginPath(); c.arc(x, m * .3, r1, 0, 6.283); c.fill(); c.beginPath(); c.arc(x + H * .03, H - m * .3, r1, 0, 6.283); c.fill(); }
        corners(); break;
      }
      case 'flowerwreath': {  // 花环：四角花簇 + 叶子
        [[m, m], [W - m, m], [m, H - m], [W - m, H - m]].forEach(([cx, cy], k) => {
          for (let i = 0; i < 9; i++) { const a = i / 9 * 6.283, d = H * (.02 + .04 * h(i + k * 9)); c.fillStyle = rgba(i % 3 ? g.cols[i % 3] : '#7ac86a', .9); if (i % 3) flower(c, cx + Math.cos(a) * d, cy + Math.sin(a) * d, H * .016); else { c.beginPath(); c.ellipse(cx + Math.cos(a) * d, cy + Math.sin(a) * d, H * .02, H * .008, a, 0, 6.283); c.fill(); } }
        });
        c.strokeStyle = rgba('#7ac86a', .6); c.lineWidth = 2; rr(c, m, m, W - 2 * m, H - 2 * m, r); c.stroke(); break;
      }
      case 'pixel': {    // 像素风：方块阶梯边
        const p = H * .016; c.fillStyle = rgba(col, .95);
        for (let x = 0; x < W; x += p * 2) { c.fillRect(x, (Math.floor(x / (p * 2)) % 2) * p, p * 2, p); c.fillRect(x, H - p - (Math.floor(x / (p * 2)) % 2) * p, p * 2, p); }
        for (let y = 0; y < H; y += p * 2) { c.fillRect((Math.floor(y / (p * 2)) % 2) * p, y, p, p * 2); c.fillRect(W - p - (Math.floor(y / (p * 2)) % 2) * p, y, p, p * 2); }
        break;
      }
      case 'comic': {    // 漫画格：粗黑边 + 速度线角 + 「♪!!」拟声
        c.strokeStyle = '#111'; c.lineWidth = H * .01; c.strokeRect(m, m, W - 2 * m, H - 2 * m);
        c.strokeStyle = 'rgba(17,17,17,.5)'; c.lineWidth = 1.5; for (let i = 0; i < 14; i++) { const a = i * .11; c.beginPath(); c.moveTo(W - m, m); c.lineTo(W - m - Math.cos(a) * H * .2, m + Math.sin(a) * H * .2); c.stroke(); }
        c.font = '900 ' + Math.round(H * .06) + 'px ' + sans; c.fillStyle = rgba(col, 1); c.strokeStyle = '#111'; c.lineWidth = 4; c.textAlign = 'right'; c.textBaseline = 'top'; c.strokeText('♪!!', W - m - H * .02, m + H * .02); c.fillText('♪!!', W - m - H * .02, m + H * .02); break;
      }
      case 'window': {   // 窗户：十字窗棂 + 窗台
        c.strokeStyle = rgba('#ffffff', .55); c.lineWidth = H * .012; c.strokeRect(m, m, W - 2 * m, H - 2 * m);
        c.lineWidth = H * .006; c.beginPath(); c.moveTo(W / 2, m); c.lineTo(W / 2, m + H * .12); c.moveTo(W / 2, H - m - H * .12); c.lineTo(W / 2, H - m); c.stroke();
        c.fillStyle = rgba('#ffffff', .6); c.fillRect(m - H * .02, H - m, W - 2 * m + H * .04, H * .025); break;
      }
      case 'ticket': {   // 票根：左右半圆缺口 + 虚线撕口
        hole(c, W, H, rgba(col, .85), m, m, W - 2 * m, H - 2 * m, r);
        c.save(); c.globalCompositeOperation = 'destination-out'; c.fillStyle = '#000'; [H * .3, H * .7].forEach(y => { c.beginPath(); c.arc(0, y, m * 1.2, 0, 6.283); c.fill(); c.beginPath(); c.arc(W, y, m * 1.2, 0, 6.283); c.fill(); }); c.restore();
        c.setLineDash([H * .01, H * .008]); c.strokeStyle = 'rgba(255,255,255,.7)'; c.lineWidth = 2; c.beginPath(); c.moveTo(W * .88, m); c.lineTo(W * .88, H - m); c.stroke(); c.setLineDash([]);
        titleText(W * .94, H / 2, 'center', '700 ' + Math.round(H * .02) + 'px ' + sans); break;
      }
      case 'gradientring': {   // 渐变光环边（彩虹描边）
        const gg = c.createLinearGradient(0, 0, W, H); g.cols.forEach((cc, i) => gg.addColorStop(i / (g.cols.length - 1), cc));
        c.strokeStyle = gg; c.lineWidth = H * .01; c.shadowColor = col; c.shadowBlur = H * .015; rr(c, m, m, W - 2 * m, H - 2 * m, Math.max(r, H * .04)); c.stroke(); c.shadowBlur = 0; break;
      }
    }
    sideDeco(c, g, W, H, m, rgba(acc, .55));
    if (g.bar && !['arcade', 'postcard', 'stamp', 'ticket'].includes(g.kind)) { c.fillStyle = 'rgba(0,0,0,.55)'; c.fillRect(0, H - H * g.bar * .6, W, H * g.bar * .6); }
    c.restore();
  }

  /* ------------------------------------------------------------------ 歌词打底框 */
  const PLATES = ['memo', 'bubble', 'tape', 'tag', 'neonbox', 'glass', 'dialog', 'brush', 'pill', 'ribbonband', 'underline',
    'bracket', 'highlighter', 'cloud', 'stampbox', 'sticky', 'scroll', 'hud', 'heartbox', 'double'];
  // 歌词句的外框 box=[x0,y0,x1,y1]（屏幕 px），在后排歌词层底下画一块底
  function drawPlate(c, kind, box, cols, a, seed) {
    if (a <= .01) return;
    let [x0, y0, x1, y1] = box;
    const padX = (y1 - y0) * .35, padY = (y1 - y0) * .22;
    x0 -= padX; x1 += padX; y0 -= padY; y1 += padY;
    const w = x1 - x0, hh = y1 - y0, col = cols[0], acc = cols[1];
    c.save(); c.globalAlpha = a;
    const tilt = (h(seed) - .5) * .05;
    c.translate(x0 + w / 2, y0 + hh / 2); c.rotate(tilt); c.translate(-(x0 + w / 2), -(y0 + hh / 2));
    switch (kind) {
      case 'memo': c.fillStyle = 'rgba(255,252,236,.86)'; c.fillRect(x0, y0, w, hh); c.strokeStyle = rgba(acc, .4); for (let y = y0 + hh * .33; y < y1; y += hh * .33) { c.beginPath(); c.moveTo(x0, y); c.lineTo(x1, y); c.stroke(); } break;
      case 'bubble': c.fillStyle = 'rgba(255,255,255,.82)'; rr(c, x0, y0, w, hh, hh * .45); c.fill(); c.beginPath(); c.moveTo(x0 + w * .2, y1 - 2); c.lineTo(x0 + w * .15, y1 + hh * .35); c.lineTo(x0 + w * .3, y1 - 2); c.fill(); break;
      case 'tape': c.fillStyle = rgba(col, .55); c.fillRect(x0, y0 + hh * .1, w, hh * .8); c.fillStyle = 'rgba(255,255,255,.25)'; for (let x = x0; x < x1; x += hh * .4) c.fillRect(x, y0 + hh * .1, hh * .12, hh * .8); break;
      case 'tag': c.fillStyle = rgba(col, .85); c.beginPath(); c.moveTo(x0 + hh * .4, y0); c.lineTo(x1, y0); c.lineTo(x1, y1); c.lineTo(x0 + hh * .4, y1); c.lineTo(x0, y0 + hh / 2); c.closePath(); c.fill(); c.fillStyle = '#fff'; c.beginPath(); c.arc(x0 + hh * .45, y0 + hh / 2, hh * .1, 0, 6.283); c.fill(); break;
      case 'neonbox': c.shadowColor = col; c.shadowBlur = hh * .4; c.strokeStyle = rgba(col, .95); c.lineWidth = 3; rr(c, x0, y0, w, hh, hh * .2); c.stroke(); c.shadowBlur = 0; c.fillStyle = 'rgba(10,6,20,.45)'; c.fill(); break;
      case 'glass': c.fillStyle = 'rgba(255,255,255,.18)'; rr(c, x0, y0, w, hh, hh * .25); c.fill(); c.strokeStyle = 'rgba(255,255,255,.55)'; c.lineWidth = 1.5; c.stroke(); break;
      case 'dialog': c.fillStyle = 'rgba(20,16,30,.62)'; rr(c, x0, y0, w, hh, hh * .15); c.fill(); c.strokeStyle = rgba(acc, .9); c.lineWidth = 2; c.stroke(); c.fillStyle = rgba(acc, .95); c.beginPath(); c.moveTo(x1 - hh * .5, y1 - hh * .25); c.lineTo(x1 - hh * .3, y1 - hh * .25); c.lineTo(x1 - hh * .4, y1 - hh * .1); c.fill(); break;
      case 'brush': c.fillStyle = rgba(col, .7); c.beginPath(); c.moveTo(x0, y0 + hh * .2); for (let k = 0; k <= 12; k++) c.lineTo(x0 + w * k / 12, y0 + hh * (.12 + .12 * h(k + seed))); for (let k = 12; k >= 0; k--) c.lineTo(x0 + w * k / 12, y1 - hh * (.12 + .12 * h(k * 3 + seed))); c.closePath(); c.fill(); break;
      case 'pill': c.fillStyle = rgba(col, .78); rr(c, x0, y0, w, hh, hh / 2); c.fill(); break;
      case 'ribbonband': c.fillStyle = rgba(col, .85); c.beginPath(); c.moveTo(x0 - hh * .3, y0); c.lineTo(x1 + hh * .3, y0); c.lineTo(x1, y0 + hh / 2); c.lineTo(x1 + hh * .3, y1); c.lineTo(x0 - hh * .3, y1); c.lineTo(x0, y0 + hh / 2); c.closePath(); c.fill(); break;
      case 'underline': c.fillStyle = rgba(col, .9); c.fillRect(x0 + padX, y1 - hh * .18, w - 2 * padX, hh * .1); c.fillStyle = rgba(acc, .6); c.fillRect(x0 + padX * 1.4, y1 - hh * .06, (w - 2 * padX) * .5, hh * .05); break;
      case 'bracket': c.strokeStyle = rgba(col, .95); c.lineWidth = 3; c.beginPath(); c.moveTo(x0 + hh * .25, y0); c.lineTo(x0, y0); c.lineTo(x0, y1); c.lineTo(x0 + hh * .25, y1); c.moveTo(x1 - hh * .25, y0); c.lineTo(x1, y0); c.lineTo(x1, y1); c.lineTo(x1 - hh * .25, y1); c.stroke(); break;
      case 'highlighter': c.fillStyle = rgba(cols[2] || '#ffe066', .55); c.fillRect(x0 + padX * .5, y0 + hh * .45, w - padX, hh * .45); break;
      case 'cloud': c.fillStyle = 'rgba(255,255,255,.8)'; for (let k = 0; k < 6; k++) { c.beginPath(); c.arc(x0 + w * (k + .5) / 6, y0 + hh * (.5 + (k % 2 ? -.12 : .12)), hh * .55, 0, 6.283); c.fill(); } break;
      case 'stampbox': c.strokeStyle = rgba('#d8402a', .85); c.lineWidth = 3; c.strokeRect(x0, y0, w, hh); c.lineWidth = 1; c.strokeRect(x0 + 4, y0 + 4, w - 8, hh - 8); break;
      case 'sticky': c.fillStyle = rgba(cols[2] || '#ffe066', .82); c.fillRect(x0, y0, w, hh); c.fillStyle = 'rgba(0,0,0,.12)'; c.beginPath(); c.moveTo(x1 - hh * .3, y1); c.lineTo(x1, y1 - hh * .3); c.lineTo(x1, y1); c.fill(); break;
      case 'scroll': c.fillStyle = 'rgba(245,232,200,.85)'; c.fillRect(x0, y0, w, hh); c.fillStyle = 'rgba(120,70,30,.85)'; c.fillRect(x0 - 4, y0 - 2, 6, hh + 4); c.fillRect(x1 - 2, y0 - 2, 6, hh + 4); break;
      case 'hud': c.strokeStyle = rgba(acc, .9); c.lineWidth = 2; c.beginPath(); c.moveTo(x0 + hh * .3, y0); c.lineTo(x1, y0); c.lineTo(x1, y1 - hh * .3); c.lineTo(x1 - hh * .3, y1); c.lineTo(x0, y1); c.lineTo(x0, y0 + hh * .3); c.closePath(); c.stroke(); c.fillStyle = 'rgba(0,20,40,.4)'; c.fill(); break;
      case 'heartbox': c.fillStyle = 'rgba(255,240,246,.82)'; rr(c, x0, y0, w, hh, hh * .3); c.fill(); c.fillStyle = rgba(col, .9); heart(c, x1 - hh * .1, y0 + hh * .1, hh * .22); break;
      case 'double': c.strokeStyle = 'rgba(255,255,255,.85)'; c.lineWidth = 1.5; c.strokeRect(x0, y0, w, hh); c.strokeRect(x0 + 5, y0 + 5, w - 10, hh - 10); break;
    }
    c.restore();
  }

  global.NekoStageFrames = { FRAME_KINDS, PLATES, genome, breed, drawFrame, drawPlate };
})(window);
