/* =============================================================================
 * 「为她而唱」舞台 · 第三十六轮：全屏遮挡转场（stinger）
 *
 * 和背景层的转场（stage-scenes.js 的 entry：iris/sweep/columns/flash/fade，只换背景）不同，
 * 这一层盖在**整个画面最上面**（背景 + 人物 + 歌词之上），做法同直播/MV 常见的 stinger：
 *   ① 进：图形从一侧/中心铺满全屏（约半拍~一拍）
 *   ② 全遮住的那一刻：后面的场景/镜头正好在这时切换（段落起点）
 *   ③ 出：图形退开，露出新段落
 * 只在段落交界、且是「大」切换（进副歌 / 最终副歌 / 桥段）时用，其余段落交界只用背景转场，
 * 避免一首歌被遮挡太多次。全部由时间 t 决定（同一 t 画面一致），不依赖外部素材。
 *
 * 种类（按风格挑）：
 *   blinds    斜向百叶条依次扫过（流行/可爱）
 *   petals    花瓣/爱心团从中心炸开铺满（可爱/抒情）
 *   shutter   上下两片快门合拢再打开，中缝一道亮光（摇滚/电子）
 *   halftone  网点圆从一角扩散铺满（流行/波普）
 *   ink       墨团从中间晕开（古风/抒情）
 *   glitch    横向错位色条闪过（电子）
 *   stripes   多色粗斜条带从左扫到右（摇滚/流行）
 * ========================================================================== */
(function (global) {
  'use strict';
  const U = global.NekoStageU;
  if (!U) return;
  const h = U.h01;

  // 第三十八轮：15 种；每种风格 6~8 种可选，每首歌按种子洗牌后轮流用（同一首不重复，不同歌顺序不同）
  const KINDS_BY_PRESET = {
    pop: ['stripes', 'halftone', 'blinds', 'diamonds', 'stars', 'bars', 'paper', 'zoom'],
    cute: ['petals', 'halftone', 'stars', 'diamonds', 'paper', 'blinds', 'zoom'],
    ballad: ['petals', 'ink', 'blinds', 'iris', 'curtain', 'paper'],
    jazz: ['curtain', 'ink', 'iris', 'blinds', 'halftone', 'slices'],
    ancient: ['ink', 'petals', 'paper', 'iris', 'blinds', 'slices'],
    edm: ['glitch', 'shutter', 'stripes', 'zoom', 'slices', 'bars', 'diamonds'],
    rock: ['shutter', 'stripes', 'glitch', 'slices', 'bars', 'zoom', 'iris'],
    dark: ['shutter', 'ink', 'glitch', 'iris', 'curtain', 'slices']
  };

  // 由 show 生成转场表：[{t（全遮住的时刻 = 段落起点）, kind, inDur, outDur, cols}]
  function plan(show, seed) {
    const secs = ((show.music && show.music.sections) || []).slice().sort((a, b) => a.t0 - b.t0);
    const preset = (show.style && show.style.preset) || 'pop';
    const kinds = KINDS_BY_PRESET[preset] || KINDS_BY_PRESET.pop;
    const beat = 60 / ((show.music && show.music.bpm) || 120);
    const pal = (show.style && show.style.palette) || {};
    const cols = [pal.hot || pal.c1 || '#ff6fae', pal.c1 || '#7fd8ff', pal.c2 || '#ffe066', pal.c3 || '#ffffff'];
    // 按种子洗牌（每首歌顺序不同）
    const order = kinds.slice();
    for (let i = order.length - 1; i > 0; i--) { const j = Math.floor(h((seed >>> 0) * .0007 + i * 1.37) * (i + 1)); const tmp = order[i]; order[i] = order[j]; order[j] = tmp; }
    const out = [];
    let n = 0, lastT = -1e9;
    for (let i = 1; i < secs.length; i++) {
      const s = secs[i];
      const big = s.id === 'chorus' || s.id === 'final' || s.id === 'bridge';
      if (!big) continue;
      if (s.t0 - lastT < 20) continue;               // 两次遮挡至少隔 20 s
      if (s.t0 < 4 || s.t0 > (show.duration || 1e9) - 4) continue;
      // 第四十四轮：转场跟着这一段的主题走（美术指导），不再整首乱换
      const art = global.__NEKO_ART;
      const kind = art ? art.stingerFor(s.id, n) : order[n % order.length];
      // 第四十九轮：进/出都加长一点（以前 0.3 s 内只画了一两帧就过去，看起来「切一半」）
      const inDur = U.clamp(beat * (kind === 'shutter' || kind === 'glitch' ? .9 : 1.2), .45, .75);
      const outDur = U.clamp(inDur * 1.25, .55, .9);
      out.push({ t: s.t0, kind, inDur, outDur, cols, v: h((seed >>> 0) * .001 + i * 3.3) });
      lastT = s.t0; n++;
    }
    return out;
  }

  // 覆盖度 0..1：进 → 1（在 t 时刻）→ 出
  function phaseAt(tr, t) {
    const hold = .12;   // 全遮住保持 0.12 s
    if (t < tr.t - tr.inDur || t > tr.t + hold + tr.outDur) return null;
    if (t <= tr.t) return { dir: 'in', k: U.easeInOut(U.clamp((t - (tr.t - tr.inDur)) / tr.inDur)) };
    if (t <= tr.t + hold) return { dir: 'in', k: 1 };
    return { dir: 'out', k: U.easeInOut(U.clamp((t - tr.t - hold) / tr.outDur)) };
  }

  function heart(c, x, y, r) {
    c.beginPath();
    c.moveTo(x, y + r * .9);
    c.bezierCurveTo(x - r * 1.6, y - r * .1, x - r * .7, y - r * 1.3, x, y - r * .45);
    c.bezierCurveTo(x + r * .7, y - r * 1.3, x + r * 1.6, y - r * .1, x, y + r * .9);
    c.fill();
  }

  // 画一帧转场（ctx 是全屏 canvas）。cover = 当前覆盖度（进：0→1，出：1→0，但出场方向继续前进不倒放）
  function draw(c, W, H, t, list) {
    c.clearRect(0, 0, W, H);
    if (!list || !list.length) return false;
    let tr = null, ph = null;
    for (const x of list) { ph = phaseAt(x, t); if (ph) { tr = x; break; } }
    if (!tr) return false;
    const k = ph.k, D = Math.hypot(W, H), cols = tr.cols;
    c.save();
    switch (tr.kind) {
      case 'stripes': {
        // 粗斜条带（旋转坐标系里是竖条）：进 = 各条的右沿从左扫到最右（铺满）；出 = 左沿继续往右扫走
        const n = 11, L = D * .75, bw = (2 * L) / n;
        c.translate(W / 2, H / 2); c.rotate(-.42);
        for (let i = 0; i < n; i++) {
          const lag = (i % 2 ? .12 : 0) + (i / n) * .18;
          const kk = U.clamp((k - lag) / (1 - lag));
          const y0 = -L + i * bw, y1 = y0 + bw + 1;
          const xr = -L + kk * 2 * L;
          c.fillStyle = cols[i % 3];
          if (ph.dir === 'in') { c.fillRect(-L, y0, xr + L, y1 - y0); c.fillStyle = 'rgba(255,255,255,.85)'; c.fillRect(xr - bw * .08, y0, bw * .08, y1 - y0); }
          else { c.fillRect(xr, y0, L - xr, y1 - y0); c.fillStyle = 'rgba(255,255,255,.85)'; c.fillRect(xr, y0, bw * .08, y1 - y0); }
        }
        break;
      }
      case 'blinds': {
        const n = 9, bh = H / n;
        for (let i = 0; i < n; i++) {
          const lag = (i / n) * .35;
          const kk = U.clamp((k - lag) / .65);
          const hh = ph.dir === 'in' ? bh * kk : bh * (1 - kk);
          c.fillStyle = cols[i % 2 ? 1 : 0];
          c.save(); c.translate(W / 2, i * bh + bh / 2); c.transform(1, 0, -.25, 1, 0, 0);
          c.fillRect(-W, -hh / 2 - .5, W * 2, hh + 1);
          c.restore();
        }
        break;
      }
      case 'shutter': {
        const cover = ph.dir === 'in' ? k : 1 - k;
        const hh = H / 2 * cover;
        c.fillStyle = '#0b0b12';
        c.fillRect(0, 0, W, hh + 1); c.fillRect(0, H - hh - 1, W, hh + 1);
        c.fillStyle = cols[0];
        c.fillRect(0, hh - H * .012, W, H * .012); c.fillRect(0, H - hh, W, H * .012);
        if (cover > .9) {
          const g = c.createLinearGradient(0, H / 2 - H * .05, 0, H / 2 + H * .05);
          g.addColorStop(0, 'rgba(255,255,255,0)'); g.addColorStop(.5, 'rgba(255,255,255,.9)'); g.addColorStop(1, 'rgba(255,255,255,0)');
          c.fillStyle = g; c.globalAlpha = (cover - .9) * 10; c.fillRect(0, H / 2 - H * .05, W, H * .1);
        }
        break;
      }
      case 'halftone': {
        // 网点：从左下角向右上铺开；出场时点从另一侧缩小消失
        const cs = H / 11;
        for (let y = -1; y < H / cs + 1; y++) for (let x = -1; x < W / cs + 1; x++) {
          const px = x * cs + (y % 2) * cs / 2, py = y * cs;
          const d = (px / W + (1 - py / H)) / 2;             // 0..1：左下 → 右上
          const kk = ph.dir === 'in' ? U.clamp(k * 1.6 - d * .6) : 1 - U.clamp(k * 1.6 - d * .6);
          if (kk <= 0) continue;
          c.fillStyle = cols[(x + y) % 3 === 0 ? 1 : 0];
          c.beginPath(); c.arc(px, py, cs * .75 * kk, 0, 6.283); c.fill();
        }
        break;
      }
      case 'petals': {
        const cover = ph.dir === 'in' ? k : 1 - k;
        const N = 60;
        for (let i = 0; i < N; i++) {
          const a = h(i * 3.7 + tr.v) * 6.283, dist = h(i * 1.3) * D * .55 * Math.min(1, cover * 1.4);
          const x = W / 2 + Math.cos(a) * dist, y = H * .5 + Math.sin(a) * dist;
          const r = (H * .09 + H * .1 * h(i * 5.1)) * cover;
          c.fillStyle = cols[i % 3];
          heart(c, x, y, r);
        }
        if (cover > .75) { c.globalAlpha = (cover - .75) * 4; c.fillStyle = cols[0]; c.fillRect(0, 0, W, H); }
        break;
      }
      case 'ink': {
        const cover = ph.dir === 'in' ? k : 1 - k;
        c.fillStyle = '#14131a';
        const N = 9;
        for (let i = 0; i < N; i++) {
          const a = i / N * 6.283 + tr.v * 3;
          const x = W / 2 + Math.cos(a) * W * .18 * h(i * 2.2), y = H / 2 + Math.sin(a) * H * .2 * h(i * 4.4);
          const r = D * .62 * cover * (.55 + .45 * h(i * 7.7));
          c.beginPath();
          for (let s = 0; s <= 24; s++) {
            const aa = s / 24 * 6.283, rr = r * (1 + .12 * Math.sin(aa * 5 + i));
            const px = x + Math.cos(aa) * rr, py = y + Math.sin(aa) * rr;
            if (s) c.lineTo(px, py); else c.moveTo(px, py);
          }
          c.fill();
        }
        break;
      }
      case 'iris': {
        // 圆形遮罩收拢到中心再张开，边缘一圈亮色描边
        const cover = ph.dir === 'in' ? k : 1 - k, R = D * .6 * (1 - cover);
        c.fillStyle = '#0b0912';
        c.beginPath(); c.rect(0, 0, W, H); c.arc(W / 2, H * .45, Math.max(0, R), 0, 6.283, true); c.fill('evenodd');
        c.strokeStyle = cols[0]; c.lineWidth = H * .012; c.beginPath(); c.arc(W / 2, H * .45, Math.max(0, R), 0, 6.283); c.stroke();
        break;
      }
      case 'diamonds': {
        // 菱形格从中心向外依次长大铺满
        const cs = H / 7;
        for (let y = -1; y < H / cs + 2; y++) for (let x = -1; x < W / cs + 2; x++) {
          const px = x * cs + (y % 2) * cs / 2, py = y * cs * .5;
          const d = Math.hypot(px - W / 2, py - H / 2) / (D / 2);
          const kk = ph.dir === 'in' ? U.clamp(k * 1.7 - d * .7) : 1 - U.clamp(k * 1.7 - d * .7);
          if (kk <= 0) continue;
          const r = cs * .75 * kk;
          c.fillStyle = cols[(x + y) & 1 ? 0 : 1];
          c.beginPath(); c.moveTo(px, py - r); c.lineTo(px + r, py); c.lineTo(px, py + r); c.lineTo(px - r, py); c.fill();
        }
        break;
      }
      case 'curtain': {
        // 舞台幕布：两片红幕从两侧合拢（带褶皱）再拉开
        const cover = ph.dir === 'in' ? k : 1 - k, half = W / 2 * cover;
        for (let side = 0; side < 2; side++) {
          const x0 = side ? W - half : 0;
          const g = c.createLinearGradient(x0, 0, x0 + half, 0);
          for (let j = 0; j <= 8; j++) g.addColorStop(j / 8, j % 2 ? '#7a0f22' : '#b0213a');
          c.fillStyle = g; c.fillRect(x0, 0, half + 1, H);
        }
        c.fillStyle = '#5a0818'; c.fillRect(0, 0, W, H * .06);
        c.fillStyle = 'rgba(255,215,140,.9)'; c.fillRect(0, H * .06, W, H * .006);
        break;
      }
      case 'slices': {
        // 竖条错位：奇数条从上落下、偶数条从下升起
        const n = 12, sw = W / n;
        for (let i = 0; i < n; i++) {
          const lag = h(i * 3.7 + tr.v) * .3, kk = U.clamp((k - lag) / (1 - lag));
          const cover = ph.dir === 'in' ? kk : 1 - kk, hh = H * cover;
          c.fillStyle = cols[i % 3];
          if (i % 2) c.fillRect(i * sw, 0, sw + 1, hh); else c.fillRect(i * sw, H - hh, sw + 1, hh);
        }
        break;
      }
      case 'stars': {
        // 星星爆开铺满（大小不一的四角星）
        const cover = ph.dir === 'in' ? k : 1 - k, N = 70;
        for (let i = 0; i < N; i++) {
          const x = h(i * 2.9 + tr.v) * W, y = h(i * 5.1 + tr.v) * H, r = (H * .06 + H * .14 * h(i * 1.3)) * cover;
          c.fillStyle = cols[i % 4];
          c.beginPath(); c.moveTo(x, y - r); c.quadraticCurveTo(x, y, x + r, y); c.quadraticCurveTo(x, y, x, y + r); c.quadraticCurveTo(x, y, x - r, y); c.quadraticCurveTo(x, y, x, y - r); c.fill();
        }
        if (cover > .8) { c.globalAlpha = (cover - .8) * 5; c.fillStyle = cols[0]; c.fillRect(0, 0, W, H); }
        break;
      }
      case 'zoom': {
        // 同心圆环从中心放大（隧道感），最后整屏亮色
        const cover = ph.dir === 'in' ? k : 1 - k;
        for (let i = 7; i >= 0; i--) {
          const r = D * .6 * U.clamp(cover * 1.4 - i * .07);
          if (r <= 0) continue;
          c.fillStyle = cols[i % 3]; c.beginPath(); c.arc(W / 2, H / 2, r, 0, 6.283); c.fill();
        }
        break;
      }
      case 'paper': {
        // 纸片撕边：一张带锯齿边的纸从右向左扫过
        const kk = k, x = ph.dir === 'in' ? W * (1 - kk * 1.15) : -W * kk * 1.15;
        c.fillStyle = '#fbf6ee';
        c.beginPath(); c.moveTo(x, 0);
        for (let y = 0; y <= H; y += H / 30) c.lineTo(x + (h(y * .1 + tr.v) - .5) * W * .03, y);
        c.lineTo(x + W * 1.2, H); c.lineTo(x + W * 1.2, 0); c.fill();
        c.fillStyle = 'rgba(0,0,0,.12)'; c.fillRect(x - W * .01, 0, W * .01, H);
        c.fillStyle = cols[0]; c.font = '900 ' + Math.round(H * .18) + 'px "Noto Sans SC",sans-serif';
        c.textAlign = 'center'; c.textBaseline = 'middle'; c.globalAlpha = .9;
        c.fillText('♪', x + W * .55, H / 2);
        break;
      }
      /* 第五十轮：线系过场（由线生成，不是色块糊一下）*/
      case 'keys': {
        // 钢琴键：一排竖键从左到右依次「按下」（落下铺满），出场时依次「抬起」；白键纸色、每隔几键一个强调色
        const n = 22, kw = W / n;
        for (let i = 0; i < n; i++) {
          const lag = (i / n) * .55, kk = U.easeOut(U.clamp((k - lag) / .45));
          const hh2 = ph.dir === 'in' ? H * kk : H * (1 - kk);
          const y0 = ph.dir === 'in' ? 0 : H - hh2;
          c.fillStyle = i % 7 === 3 ? cols[0] : (i % 7 === 5 ? cols[1] : '#fbfaf6');
          c.fillRect(i * kw, y0, kw + 1, hh2);
          c.strokeStyle = 'rgba(58,52,64,.55)'; c.lineWidth = Math.max(1, H / 500);
          c.strokeRect(i * kw + .5, y0, kw, hh2);
          if (![2, 6].includes(i % 7) && hh2 > H * .3) { c.fillStyle = 'rgba(58,52,64,.9)'; c.fillRect((i + 1) * kw - kw * .28, y0, kw * .56, Math.min(hh2, H) * .58); }
        }
        break;
      }
      case 'lines': {
        // 线：很多条横线从中间一条开始向上下铺开、加粗直到连成面；出场时变细散开
        const n = 36, cover = ph.dir === 'in' ? k : 1 - k;
        c.fillStyle = '#fbfaf6';
        for (let i = 0; i < n; i++) {
          const u = (i + .5) / n, d = Math.abs(u - .5) * 2;
          const kk = U.clamp((cover - d * .5) / .5), th = (H / n) * (1.04 * U.easeInOut(kk));
          if (th <= .2) continue;
          c.fillStyle = i % 9 === 4 ? cols[0] : (i % 9 === 7 ? cols[1] : '#fbfaf6');
          const wob = Math.sin(i * 1.3 + t * 9) * W * .01 * (1 - kk);
          c.fillRect(wob - 4, u * H - th / 2, W + 8, th);
        }
        break;
      }
      case 'rings': {
        // 圆环：从中心一圈圈向外展开的环带，环带之间的缝随进度合上；出场时由内向外打开
        const R = D * .56, n = 9, cover = ph.dir === 'in' ? k : 1 - k;
        c.translate(W / 2, H / 2);
        for (let i = n - 1; i >= 0; i--) {
          const lag = ph.dir === 'in' ? (i / n) * .5 : ((n - 1 - i) / n) * .5;
          const kk = U.easeInOut(U.clamp((cover - lag) / .5));
          if (kk <= 0) continue;
          const r0 = R * i / n, r1 = R * (i + 1) / n;
          c.strokeStyle = i % 3 === 1 ? cols[0] : (i % 3 === 2 ? cols[1] : '#fbfaf6');
          c.lineWidth = (r1 - r0) * 1.04 * kk;
          c.beginPath(); c.arc(0, 0, (r0 + r1) / 2, 0, 6.2832); c.stroke();
        }
        break;
      }
      case 'bars': {
        // 横向快速条带依次从左右交替插入
        const n = 8, bh = H / n;
        for (let i = 0; i < n; i++) {
          const lag = (i / n) * .4, kk = U.clamp((k - lag) / .6);
          const cover = ph.dir === 'in' ? kk : 1 - kk, w = W * cover;
          c.fillStyle = cols[i % 3];
          if (i % 2) c.fillRect(W - w, i * bh, w, bh + 1); else c.fillRect(0, i * bh, w, bh + 1);
        }
        break;
      }
      case 'glitch': {
        const cover = ph.dir === 'in' ? k : 1 - k;
        const n = 22, fr = Math.floor(t * 30);
        for (let i = 0; i < n; i++) {
          if (h(i * 9.1 + fr * .37) > cover * 1.2) continue;
          const y = h(i * 3.3 + fr * .11) * H, hh = H * (.02 + .09 * h(i * 5.5 + fr * .2));
          const off = (h(i * 7.7 + fr) - .5) * W * .3;
          c.fillStyle = cols[i % 3];
          c.fillRect(off, y, W, hh);
        }
        if (cover > .85) { c.globalAlpha = (cover - .85) / .15; c.fillStyle = '#08080c'; c.fillRect(0, 0, W, H); }
        break;
      }
    }
    c.restore();
    return true;
  }

  // 现在是不是「全遮住」（用于让镜头/场景在遮住时切换，看不见跳变）
  function coveredAt(list, t) {
    for (const x of list || []) if (Math.abs(t - x.t) < .06) return true;
    return false;
  }

  global.NekoStageStinger = { plan, draw, coveredAt, KINDS_BY_PRESET };
})(window);
