/* =============================================================================
 * 「为她而唱」舞台 · 第五十二轮：手书分镜（重写）
 *
 * 第五十一轮的问题（用户反馈）：过于简单、一句歌词换一个画面、字超出屏幕/被色块挡住、像套模板、
 * 换个颜色就是一首歌、伴奏过场只有几条线、图层都不动、不看歌词意思。
 *
 * 重新按时间顺序看了参考 MV 的连续分镜（浮気したらあかんで / だいしきゅーだいしゅき / PPPP …）：
 *   · 一个「场景」维持 8~20 秒：街景、房间、网页窗口、游戏选单……歌词在场景里一句句换，
 *     贴纸/小物一样样加进来；不是一句一个画面。
 *   · 画面 5~7 层，每层自己在动：底纹滚动、色带滑动、贴纸弹跳旋转、窗口依次弹出、标签摇摆。
 *   · 场景跟歌词意思走：夜/雨/海/恋/电话/游戏……各有各的画面和配色。
 *   · 每支 MV 一套「套路」（kit）：缝线甜系 / 贴纸拼贴街头系 / 游戏界面系 / 电波窗口系 / 稿纸手写系 / 霓虹夜系。
 *
 * 结构：
 *   KIT（套路，按曲风选）→ 决定边框语言、贴纸种类、字卡容器、图案、配色池、运动性格
 *   SCENE（场景，按段落 + 歌词意思切，8~20 s 一个）→ 一个布景（stage）+ 一组会动的图层
 *   LINE（歌词）→ 在当前场景的「字位」里出现；位置由版面槽位决定，保证不出屏、不被挡
 *   BREAK（伴奏）→ 一整套会动的图形秀（乐器/波形/几何群），不是几条线
 *
 * 所有绘制用 Canvas 2D；有 Rough.js（MIT）时，描边和填充走手绘笔触。
 * ========================================================================== */
(function (global) {
  'use strict';
  const U = global.NekoStageU;
  if (!U) return;
  const h = U.h01, TAU = Math.PI * 2;
  const rgba = (c, a) => U.rgba(c, U.clamp(a, 0, 1));
  const lerp = U.lerp, clamp = U.clamp, eo = U.easeOut, eio = U.easeInOut, back = U.easeOutBack;

  /* ============================== 配色（按歌词意思 + 曲风） ============================== */
  // 每个「意象」一组配色：bg 底、bg2 第二底、a/b 两个主色、pop 点缀、ink 字、paper 纸
  const MOODS = {
    night:  { bg: '#171b3a', bg2: '#262a5c', a: '#5b6cff', b: '#8fd0ff', pop: '#ffd86a', ink: '#f4f1ff', paper: '#fdfbff', dark: true },
    rain:   { bg: '#2b3a55', bg2: '#3d5074', a: '#7fa6d8', b: '#b9d4f0', pop: '#ffe08a', ink: '#f2f7ff', paper: '#f7fbff', dark: true },
    sea:    { bg: '#bfe9ff', bg2: '#8fd3ff', a: '#3aa0f0', b: '#ffffff', pop: '#ff8fa8', ink: '#16466e', paper: '#f6fbff' },
    sky:    { bg: '#d8f0ff', bg2: '#b8e0ff', a: '#6ab8f0', b: '#fff6c8', pop: '#ff9f6a', ink: '#24486a', paper: '#ffffff' },
    love:   { bg: '#ffd6e4', bg2: '#ffb3cc', a: '#ff6f9f', b: '#fff0f5', pop: '#ff3f7f', ink: '#7a2448', paper: '#fff8fb' },
    sweet:  { bg: '#fff0d6', bg2: '#ffdcb0', a: '#ff9f6a', b: '#ffe9f0', pop: '#ff5f8f', ink: '#6a3a24', paper: '#fffaf2' },
    star:   { bg: '#221c4a', bg2: '#3a2f78', a: '#b48cff', b: '#ffd86a', pop: '#ff8fd0', ink: '#f6f0ff', paper: '#fbf8ff', dark: true },
    flower: { bg: '#ffe6f2', bg2: '#ffd0e6', a: '#ff8fbf', b: '#c8f0d8', pop: '#ffb347', ink: '#6a2a52', paper: '#fffafc' },
    summer: { bg: '#c8f4ee', bg2: '#9fe8dc', a: '#2fbfa6', b: '#fff2a8', pop: '#ff7a5a', ink: '#17544a', paper: '#f8fffd' },
    sunset: { bg: '#ffd9b8', bg2: '#ffb38a', a: '#ff7a5a', b: '#8a5ad8', pop: '#ffe066', ink: '#5a2a3a', paper: '#fff8f2' },
    phone:  { bg: '#ffe0f0', bg2: '#f0c8ff', a: '#ff5fb0', b: '#8fe0ff', pop: '#7a5cf0', ink: '#5a1f5a', paper: '#fffaff' },
    game:   { bg: '#d8d0ff', bg2: '#b8a8ff', a: '#6a4cf0', b: '#8fe6ff', pop: '#ffe04a', ink: '#2a1f6a', paper: '#f8f6ff' },
    city:   { bg: '#ffe8a8', bg2: '#8fe0ff', a: '#ff4f7a', b: '#2a2a3a', pop: '#3ad06a', ink: '#1f1f2a', paper: '#ffffff' },
    lonely: { bg: '#e6e6ee', bg2: '#cfd0dc', a: '#8a8ea8', b: '#ffffff', pop: '#ff6f8f', ink: '#2f3040', paper: '#fafafc' },
    dream:  { bg: '#e8dcff', bg2: '#d0c0ff', a: '#a890ff', b: '#ffd6ee', pop: '#6ae0d0', ink: '#3a2a6a', paper: '#fcfaff' },
    fire:   { bg: '#2a1620', bg2: '#4a1f2a', a: '#ff4f4f', b: '#ffb347', pop: '#fff0a8', ink: '#fff4ec', paper: '#fff8f2', dark: true }
  };
  // 歌词关键词 → 意象（中/日/英都认）
  const MOOD_WORDS = [
    ['night', /夜|晚|月|暗|黑|星空|深夜|闇|ナイト|night|moon|dark/i], ['star', /星|銀河|银河|宇宙|流星|きら|光芒|star|galaxy/i],
    ['rain', /雨|泪|淚|涙|哭|泣|濡|tear|rain|cry/i], ['sea', /海|波|浪|潮|岸|船|ocean|sea|wave/i],
    ['sky', /空|云|雲|风|風|飞|飛|翼|晴|sky|wind|fly/i], ['love', /爱|愛|恋|戀|心|好き|すき|大好|kiss|吻|love|heart/i],
    ['sweet', /甜|糖|蛋糕|巧克力|チョコ|ケーキ|スイート|キャンディ|candy|sweet|cake/i], ['flower', /花|桜|樱|櫻|春|咲|bloom|flower/i],
    ['summer', /夏|太阳|太陽|汽水|ソーダ|ひまわり|summer|sun/i], ['sunset', /夕|黄昏|晚霞|落日|帰り|放課後|sunset/i],
    ['phone', /电话|電話|もしもし|信号|電波|メール|短信|消息|通知|スマホ|call|phone|message/i], ['game', /游戏|ゲーム|勝|负|負|レベル|player|round|game|win/i],
    ['city', /街|城|都市|电车|電車|駅|信号灯|ネオン|city|street|town/i], ['lonely', /孤|寂|独|ひとり|一人|さよなら|再见|別れ|lonely|alone|bye/i],
    ['dream', /梦|夢|ゆめ|魔法|まほう|magic|dream|wish|愿/i], ['fire', /火|燃|热|熱|炎|叫|戦|战|fire|burn|fight/i]
  ];
  const EMO_MOOD = { joy: 'summer', playful: 'sweet', tender: 'flower', longing: 'sky', sad: 'rain', proud: 'star', determined: 'fire', angry: 'fire', calm: 'dream' };
  function moodOf(lines, fallback) {
    const txt = lines.map(l => String(l.text || '')).join(' ');
    const score = {};
    for (const [k, re] of MOOD_WORDS) { const m = txt.match(new RegExp(re.source, 'gi')); if (m) score[k] = (score[k] || 0) + m.length; }
    let best = null, bv = 0;
    for (const k in score) if (score[k] > bv) { bv = score[k]; best = k; }
    if (best) return best;
    const emo = lines.map(l => l.emotion).filter(Boolean)[0];
    return EMO_MOOD[emo] || fallback || 'dream';
  }

  /* ============================== 套路（KIT，按曲风） ============================== */
  // border 边框语言 / pat 图案池 / deco 贴纸池 / stages 布景池 / box 字卡容器池 / move 运动性格（0 稳 … 1 跳）/ rough 手绘笔触强度
  const KITS = {
    stitch:  { name: '缝线甜系', border: 'stitch', pat: ['argyle', 'gingham', 'hearts', 'dots'], deco: ['heart', 'ribbon', 'star4', 'candy', 'flower', 'tag'],
               stages: ['room', 'hearts', 'street', 'plate', 'ribbonwall', 'window'], box: ['label', 'bubble', 'plain'], move: .55, rough: .6, moods: ['love', 'sweet', 'flower', 'dream'] },
    collage: { name: '贴纸拼贴街头系', border: 'tape', pat: ['grid', 'confetti', 'checker', 'halftone'], deco: ['burst', 'smile', 'rainbow', 'badge', 'arrow', 'star5', 'word'],
               stages: ['collage', 'gridroom', 'browser', 'bigword', 'checkerfloor', 'burststage'], box: ['pill', 'window', 'tape'], move: .95, rough: .25, moods: ['city', 'summer', 'game', 'sweet'] },
    arcade:  { name: '游戏界面系', border: 'hud', pat: ['pixel', 'grid', 'skulls', 'stripe'], deco: ['pixelheart', 'burst', 'coin', 'cursor', 'star5', 'bar'],
               stages: ['menu', 'loading', 'versus', 'pixelsky', 'gridroom', 'bigword'], box: ['menu', 'pill', 'window'], move: .85, rough: .15, moods: ['game', 'star', 'dream', 'city'] },
    denpa:   { name: '电波窗口系', border: 'crt', pat: ['halftone', 'checker', 'stripe', 'dots'], deco: ['phoneicon', 'antenna', 'wave', 'heart', 'cursor', 'tag'],
               stages: ['browser', 'radar', 'phone', 'hearts', 'ribbonwall', 'loading'], box: ['window', 'bubble', 'pill'], move: .7, rough: .3, moods: ['phone', 'love', 'dream', 'night'] },
    paper:   { name: '稿纸手写系', border: 'film', pat: ['ruled', 'plain', 'dots', 'grid'], deco: ['inkdot', 'leaf', 'star4', 'tag', 'stamp', 'clip'],
               stages: ['desk', 'windowsky', 'horizon', 'moon', 'letter', 'plate'], box: ['plain', 'underline', 'label'], move: .3, rough: .9, moods: ['sky', 'lonely', 'rain', 'sunset', 'sea', 'flower'] },
    neon:    { name: '霓虹夜系', border: 'neon', pat: ['stripe', 'grid', 'stars', 'halftone'], deco: ['star5', 'bolt', 'note', 'burst', 'ring', 'bar'],
               stages: ['cityline', 'moon', 'spotlight', 'radar', 'bigword', 'pixelsky'], box: ['neonbox', 'pill', 'plain'], move: .8, rough: .2, moods: ['night', 'star', 'fire', 'city'] }
  };
  const KIT_BY_PRESET = { cute: ['stitch', 'denpa', 'collage'], pop: ['collage', 'stitch', 'denpa', 'arcade'], edm: ['arcade', 'neon', 'denpa'], rock: ['neon', 'collage', 'arcade'],
    ballad: ['paper', 'stitch', 'neon'], jazz: ['paper', 'neon'], ancient: ['paper', 'stitch'], dark: ['neon', 'paper', 'arcade'] };

  /* ============================== 第五十四轮：手书画质（开源手写字体 + Rough.js 手绘线 + 纸纹） ==============================
   * 字体都在 static/fonts（OFL-1.1）：站酷快乐体、霞鹜文楷、Zen Maru Gothic。按套路选，缺字逐字回退到原字体栈。*/
  const HAND_FONTS = [
    ['ZCOOL KuaiLe', 'zcool-kuaile/ZCOOLKuaiLe-Regular.woff2'], ['Wenkai Subset', 'lxgw-wenkai/WenkaiSubset-Regular.woff2'],
    ['Zen Maru Gothic', 'zen-maru-gothic/ZenMaruGothic-Regular.woff2'], ['Zen Maru Gothic Bold', 'zen-maru-gothic/ZenMaruGothic-Bold.woff2']
  ];
  const KIT_FONT = {
    stitch: '"ZCOOL KuaiLe","Zen Maru Gothic"', denpa: '"ZCOOL KuaiLe","Zen Maru Gothic"', collage: '"ZCOOL KuaiLe","Zen Maru Gothic Bold"',
    paper: '"Wenkai Subset","Zen Maru Gothic"', arcade: '"Zen Maru Gothic Bold","ZCOOL KuaiLe"', neon: '"Zen Maru Gothic Bold","ZCOOL KuaiLe"'
  };
  const HANDF = { loading: false, ok: {} };
  function loadHandFonts() {
    if (HANDF.loading || typeof FontFace === 'undefined' || !global.document) return; HANDF.loading = true;
    const base = (global.__NEKO_STAGE_BASE || './');
    for (const [fam, file] of HAND_FONTS) {
      try { const f = new FontFace(fam, 'url("' + base + file + '")'); f.load().then(ff => { document.fonts.add(ff); HANDF.ok[fam] = true; }).catch(() => { HANDF.ok[fam] = false; }); } catch (e) { }
    }
  }
  // 手绘线：Rough.js 生成，笔迹每秒 8 格在 3 套之间换（手书动画的「抖线」），形状缓存，不每帧重算
  const RGEN = { g: null, cache: new Map() };
  function roughShape(kind, w, hh, r, opt, boil) {
    if (!global.rough) return null; if (!RGEN.g) RGEN.g = global.rough.generator();
    const key = kind + '|' + Math.round(w) + '|' + Math.round(hh) + '|' + Math.round(r) + '|' + boil + '|' + opt.stroke + '|' + (opt.fill || '') + '|' + opt.strokeWidth + '|' + (opt.roughness || 0).toFixed(2);
    let d = RGEN.cache.get(key); if (d) return d;
    const o = Object.assign({ seed: 7 + boil * 131, bowing: 1.4, roughness: 1.2, fillStyle: 'hachure', hachureGap: Math.max(6, hh / 9), hachureAngle: -41, fillWeight: Math.max(1, opt.strokeWidth * .45) }, opt);
    if (kind === 'ellipse') d = RGEN.g.ellipse(w / 2, hh / 2, w, hh, o);
    else if (r > 2) { const R = Math.min(r, w / 2, hh / 2); d = RGEN.g.path('M' + R + ',0 H' + (w - R) + ' Q' + w + ',0 ' + w + ',' + R + ' V' + (hh - R) + ' Q' + w + ',' + hh + ' ' + (w - R) + ',' + hh + ' H' + R + ' Q0,' + hh + ' 0,' + (hh - R) + ' V' + R + ' Q0,0 ' + R + ',0 Z', o); }
    else d = RGEN.g.rectangle(0, 0, w, hh, o);
    if (RGEN.cache.size > 400) RGEN.cache.clear(); RGEN.cache.set(key, d); return d;
  }
  function roughDraw(c, d) { const rc0 = rc(c); if (rc0 && d) { rc0.draw(d); return true; } return false; }
  // 纸纹：256² 噪点 + 纤维贴图，multiply 叠在分镜底层上（生成一次）
  let PAPER = null;
  function paperTex() {
    if (PAPER || !global.document) return PAPER; const cv = document.createElement('canvas'); cv.width = cv.height = 256; const g = cv.getContext('2d');
    const im = g.createImageData(256, 256); let sd = 1234567; const rnd = () => (sd = (sd * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
    for (let i = 0; i < im.data.length; i += 4) { const v = 236 + rnd() * 19; im.data[i] = v; im.data[i + 1] = v - 1; im.data[i + 2] = v - 4; im.data[i + 3] = 255; }
    g.putImageData(im, 0, 0); g.strokeStyle = 'rgba(150,130,110,.10)'; g.lineWidth = .7;
    for (let i = 0; i < 70; i++) { const x = rnd() * 256, y = rnd() * 256, a = rnd() * Math.PI; g.beginPath(); g.moveTo(x, y); g.quadraticCurveTo(x + Math.cos(a) * 9 + 3, y + Math.sin(a) * 9 - 2, x + Math.cos(a) * 18, y + Math.sin(a) * 18); g.stroke(); }
    PAPER = cv; return PAPER;
  }
  function drawPaper(c, W, H, amount) {
    const tx = paperTex(); if (!tx || amount <= 0) return; const pat = c.createPattern(tx, 'repeat'); if (!pat) return;
    c.save(); c.globalCompositeOperation = 'multiply'; c.globalAlpha = amount; c.fillStyle = pat; const sc = Math.max(1, H / 720); if (pat.setTransform && global.DOMMatrix) pat.setTransform(new DOMMatrix().scale(sc)); c.fillRect(0, 0, W, H); c.restore();
  }

  /* ============================== 手绘笔触 ============================== */
  let RC = null, RCcv = null;
  function rc(c) { if (!global.rough) return null; if (RCcv !== c.canvas) { RC = global.rough.canvas(c.canvas); RCcv = c.canvas; } return RC; }
  const ro = (seed, k, extra) => Object.assign({ roughness: 1.1 * k, bowing: 1.2 * k, seed: 1 + Math.floor(seed * 9973) }, extra || {});

  /* ============================== 基础图形 ============================== */
  function rr(c, x, y, w, hh, r) { r = Math.min(r, w / 2, hh / 2); c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + hh, r); c.arcTo(x + w, y + hh, x, y + hh, r); c.arcTo(x, y + hh, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath(); }
  function heart(c, x, y, r) { c.beginPath(); c.moveTo(x, y + r * .9); c.bezierCurveTo(x - r * 1.6, y - r * .1, x - r * .7, y - r * 1.3, x, y - r * .45); c.bezierCurveTo(x + r * .7, y - r * 1.3, x + r * 1.6, y - r * .1, x, y + r * .9); c.closePath(); }
  function starN(c, x, y, r, n, inner) { c.beginPath(); for (let i = 0; i < n * 2; i++) { const a = -Math.PI / 2 + i * Math.PI / n, rad = i % 2 ? r * (inner || .45) : r; c.lineTo(x + Math.cos(a) * rad, y + Math.sin(a) * rad); } c.closePath(); }
  function star4(c, x, y, r) { c.beginPath(); c.moveTo(x, y - r); c.quadraticCurveTo(x, y, x + r, y); c.quadraticCurveTo(x, y, x, y + r); c.quadraticCurveTo(x, y, x - r, y); c.quadraticCurveTo(x, y, x, y - r); c.closePath(); }
  function burst(c, x, y, r, n) { c.beginPath(); for (let i = 0; i < n * 2; i++) { const a = i * Math.PI / n, rad = i % 2 ? r * .78 : r; c.lineTo(x + Math.cos(a) * rad, y + Math.sin(a) * rad); } c.closePath(); }

  /* ============================== 图案（会滚动） ============================== */
  function pattern(c, W, H, kind, col, a, t, sc, spd) {
    const s = H * (sc || .09), off = (t * H * .02 * (spd == null ? 1 : spd));
    c.save(); c.globalAlpha = a; c.fillStyle = col; c.strokeStyle = col;
    if (kind === 'gingham') { for (let x = -2 * s + (off % (2 * s)); x < W + s; x += 2 * s) c.fillRect(x, 0, s, H); c.globalAlpha = a * .8; for (let y = -2 * s + (off * .6 % (2 * s)); y < H + s; y += 2 * s) c.fillRect(0, y, W, s); }
    else if (kind === 'stripe') { c.translate(W / 2, H / 2); c.rotate(-.5); const L = Math.hypot(W, H); for (let x = -L + (off * 1.4 % (2 * s)); x < L; x += 2 * s) c.fillRect(x, -L, s, 2 * L); }
    else if (kind === 'dots') { for (let j = 0, y = -s + (off * .5 % s); y < H + s; y += s, j++) for (let x = -s + (j % 2 ? s / 2 : 0); x < W + s; x += s) { c.beginPath(); c.arc(x, y, s * .16, 0, TAU); c.fill(); } }
    else if (kind === 'hearts') { for (let j = 0, y = -s; y < H + 2 * s; y += s * 1.5, j++) for (let x = -2 * s + (j % 2 ? s * .75 : 0) + (off % (s * 1.5)) * (j % 2 ? 1 : -1); x < W + 2 * s; x += s * 1.5) { heart(c, x, y, s * .3); c.fill(); } }
    else if (kind === 'checker') { const o = off % (2 * s); for (let j = -2, y = -2 * s + o; y < H + s; y += s, j++) for (let i = -2, x = -2 * s + o; x < W + s; x += s, i++) if ((i + j) % 2) c.fillRect(x, y, s, s); }
    else if (kind === 'grid') { c.lineWidth = Math.max(1, H / 700); const g = s * .55, o = off % g; for (let x = -g + o; x <= W + g; x += g) { c.beginPath(); c.moveTo(x, 0); c.lineTo(x, H); c.stroke(); } for (let y = -g + o * .5; y <= H + g; y += g) { c.beginPath(); c.moveTo(0, y); c.lineTo(W, y); c.stroke(); } }
    else if (kind === 'argyle') { const g = s * 1.3, o = off % g; for (let j = -1, y = -g; y < H + g; y += g / 2, j++) for (let x = -g + (j % 2 ? g / 2 : 0) + o; x < W + g; x += g) { c.beginPath(); c.moveTo(x, y - g * .32); c.lineTo(x + g * .22, y); c.lineTo(x, y + g * .32); c.lineTo(x - g * .22, y); c.closePath(); c.fill(); } }
    else if (kind === 'halftone') { for (let j = 0, y = 0; y < H + s; y += s * .5, j++) for (let x = (j % 2 ? s * .25 : 0); x < W + s; x += s * .5) { const r = s * .2 * clamp(1 - (x / W * .6 + y / H * .6) + .25 * Math.sin(t * .6 + j)); if (r > .5) { c.beginPath(); c.arc(x, y, r, 0, TAU); c.fill(); } } }
    else if (kind === 'confetti') { for (let i = 0; i < 90; i++) { const x = h(i * 3.7) * W, y = U.fract(h(i * 7.3) + t * .02 * (1 + h(i))) * (H + s) - s * .5; c.save(); c.translate(x, y); c.rotate(h(i * 9.1) * TAU + t * (h(i) - .5)); c.fillRect(-s * .12, -s * .03, s * .24, s * .06); c.restore(); } }
    else if (kind === 'pixel') { const g = s * .5; for (let i = 0; i < 70; i++) { const x = Math.floor(h(i * 5.1) * W / g) * g, y = Math.floor(U.fract(h(i * 8.9) + Math.floor(t * 2 + i) * .013) * H / g) * g; c.fillRect(x, y, g, g); } }
    else if (kind === 'skulls') { for (let j = 0, y = s * .5; y < H + s; y += s * 1.6, j++) for (let x = (j % 2 ? s * .8 : 0) - (off % (s * 1.6)); x < W + s * 2; x += s * 1.6) { c.beginPath(); c.arc(x, y, s * .3, 0, TAU); c.fill(); c.fillRect(x - s * .17, y + s * .18, s * .34, s * .2); } }
    else if (kind === 'stars') { for (let i = 0; i < 60; i++) { const x = h(i * 3.3) * W, y = h(i * 7.7) * H, tw = .4 + .6 * Math.abs(Math.sin(t * (1 + h(i) * 2) + i)); c.globalAlpha = a * tw; star4(c, x, y, s * (.06 + .12 * h(i * 2))); c.fill(); } }
    else if (kind === 'ruled') { c.lineWidth = Math.max(1, H / 800); for (let y = s * .6; y < H; y += s * .62) { c.beginPath(); c.moveTo(0, y); c.lineTo(W, y); c.stroke(); } c.globalAlpha = a * 1.6; c.beginPath(); c.moveTo(W * .09, 0); c.lineTo(W * .09, H); c.stroke(); }
    c.restore();
  }

  /* ============================== 手绘线稿库（第五十三轮，oreo-design/doodle-icons，MIT） ==============================
   * 204 个手绘风线稿图标（月亮、雨、花、信、电话、心、星……），用 Path2D 画，线宽按尺寸缩放。
   * 按歌词里的词挑图标（月→moon、雨/泪→rain、花→flower……），场景里当会动的贴纸用。*/
  const DOODLE = { icons: null, cache: {}, loading: false };
  function loadDoodles(base) {
    if (DOODLE.icons || DOODLE.loading) return; DOODLE.loading = true;
    const url = (base || (global.__NEKO_STAGE_BASE || './')) + 'vendor/doodles.json';
    try { fetch(url, { cache: 'force-cache' }).then(r => r.json()).then(d => { DOODLE.icons = d.icons || {}; DOODLE.stroke = d.stroke || 3.4; }).catch(() => { DOODLE.loading = false; }); } catch (e) { DOODLE.loading = false; }
  }
  function doodle(c, name, r, col, fillCol) {
    const d = DOODLE.icons && DOODLE.icons[name]; if (!d) return false;
    let ps = DOODLE.cache[name]; if (!ps) { try { ps = DOODLE.cache[name] = d.map(x => new Path2D(x)); } catch (e) { return false; } }
    const sc = (r * 2) / 48; c.save(); c.scale(sc, sc); c.translate(-24, -24);
    if (fillCol) { c.fillStyle = fillCol; c.globalAlpha *= .9; for (const p2 of ps) c.fill(p2); c.globalAlpha /= .9; }
    c.strokeStyle = col; c.lineWidth = (DOODLE.stroke || 3.4); c.lineCap = 'round'; c.lineJoin = 'round'; for (const p2 of ps) c.stroke(p2);
    c.restore(); return true;
  }
  const DOODLE_WORDS = [
    [/月|moon|ムーン/i, ['moon', 'moon-stars']], [/星|銀河|银河|star|流星/i, ['star', 'stars', 'shooting-star', 'moon-stars']], [/雨|泪|淚|涙|哭|rain|cry/i, ['rain', 'umbrella', 'clouds']],
    [/云|雲|天空|空|sky|cloud/i, ['clouds', 'sun', 'bird']], [/花|桜|樱|櫻|春|bloom|flower/i, ['flower', 'cherry-blossom', 'tulip', 'daisy', 'rose', 'sunflower']], [/夏|太阳|太陽|sun|summer/i, ['sun', 'sunflower', 'fish']],
    [/海|波|浪|sea|ocean|wave/i, ['fish', 'umbrella', 'sun']], [/爱|愛|恋|戀|心|好き|kiss|吻|love|heart/i, ['heart', 'hearts', 'sparkle-heart', 'love-letter', 'heart-arrow', 'love-lock', 'ring']],
    [/信|手紙|letter|mail/i, ['love-letter', 'mail', 'send', 'stamp']], [/电话|電話|もしもし|phone|call/i, ['phone', 'call', 'chat', 'mic']], [/梦|夢|ゆめ|dream|wish|魔法/i, ['sparkle-heart', 'stars', 'moon-stars', 'witch-hat']],
    [/歌|唱|音|声|music|song|sing/i, ['mic', 'megaphone', 'play', 'player']], [/甜|糖|蛋糕|ケーキ|cake|candy|sweet/i, ['cupcake', 'candy-cane', 'cherry', 'gift-bag']], [/时间|時間|时|時|time|clock/i, ['calendar', 'gauge', 'archive']],
    [/猫|ねこ|cat/i, ['cat', 'paw-print', 'fish']], [/火|燃|fire|burn/i, ['fire', 'flame', 'bolt']], [/游戏|ゲーム|game|player/i, ['gamepad', 'player', 'star-medal', 'puzzle']], [/叶|葉|秋|leaf|autumn/i, ['leaf', 'maple-leaf', 'falling-leaves', 'autumn-tree']]
  ];
  function doodlesFor(text) {
    const out = []; for (const [re, list] of DOODLE_WORDS) if (re.test(text)) for (const x of list) if (!out.includes(x)) out.push(x);
    return out.length ? out : ['sparkle-heart', 'star', 'flower', 'heart', 'clouds', 'music' === 'x' ? '' : 'mic'];
  }

  /* ============================== 贴纸（deco，每个都会自己动） ==============================
   * 画在 (0,0) 为中心、半径约 r 的范围里；P 配色；t 时间；k 出现进度 0..1 */
  const DECO = {
    heart(c, r, P, t) { c.fillStyle = P.pop; heart(c, 0, 0, r); c.fill(); c.fillStyle = rgba('#ffffff', .55); c.beginPath(); c.ellipse(-r * .35, -r * .35, r * .16, r * .1, -.6, 0, TAU); c.fill(); },
    star4(c, r, P) { c.fillStyle = P.b; star4(c, 0, 0, r); c.fill(); },
    star5(c, r, P) { c.fillStyle = P.pop; starN(c, 0, 0, r, 5); c.fill(); c.strokeStyle = P.ink; c.lineWidth = r * .1; c.stroke(); },
    burst(c, r, P, t, word) { c.fillStyle = P.a; burst(c, 0, 0, r, 12); c.fill(); c.strokeStyle = P.ink; c.lineWidth = r * .07; c.stroke(); if (word) { c.fillStyle = P.paper; c.font = '900 ' + Math.round(r * .5) + 'px ' + P.font; c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillText(word, 0, r * .04); } },
    ribbon(c, r, P) { c.fillStyle = P.a; c.beginPath(); c.moveTo(0, 0); c.lineTo(-r, -r * .55); c.lineTo(-r, r * .55); c.closePath(); c.fill(); c.beginPath(); c.moveTo(0, 0); c.lineTo(r, -r * .55); c.lineTo(r, r * .55); c.closePath(); c.fill(); c.fillStyle = P.pop; c.beginPath(); c.arc(0, 0, r * .24, 0, TAU); c.fill(); },
    candy(c, r, P) { c.fillStyle = P.b; c.beginPath(); c.moveTo(-r * .5, 0); c.lineTo(-r, -r * .4); c.lineTo(-r, r * .4); c.closePath(); c.fill(); c.beginPath(); c.moveTo(r * .5, 0); c.lineTo(r, -r * .4); c.lineTo(r, r * .4); c.closePath(); c.fill(); c.fillStyle = P.pop; c.beginPath(); c.ellipse(0, 0, r * .55, r * .42, 0, 0, TAU); c.fill(); c.strokeStyle = P.paper; c.lineWidth = r * .1; c.beginPath(); c.moveTo(-r * .2, -r * .38); c.lineTo(r * .2, r * .38); c.stroke(); },
    flower(c, r, P, t) { for (let i = 0; i < 5; i++) { const a = i * TAU / 5 + t * .3; c.fillStyle = P.a; c.beginPath(); c.arc(Math.cos(a) * r * .5, Math.sin(a) * r * .5, r * .36, 0, TAU); c.fill(); } c.fillStyle = P.pop; c.beginPath(); c.arc(0, 0, r * .3, 0, TAU); c.fill(); },
    tag(c, r, P, t, word) { c.fillStyle = P.paper; rr(c, -r, -r * .42, r * 2, r * .84, r * .12); c.fill(); c.strokeStyle = P.ink; c.lineWidth = r * .06; c.stroke(); c.fillStyle = P.ink; c.font = '800 ' + Math.round(r * .42) + 'px ' + P.font; c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillText(word || 'LOVE', 0, r * .03); },
    smile(c, r, P) { c.fillStyle = P.pop; c.beginPath(); c.arc(0, 0, r, 0, TAU); c.fill(); c.strokeStyle = P.ink; c.lineWidth = r * .1; c.stroke(); c.fillStyle = P.ink; c.fillRect(-r * .55, -r * .3, r * .42, r * .22); c.fillRect(r * .13, -r * .3, r * .42, r * .22); c.beginPath(); c.arc(0, r * .15, r * .45, .15, Math.PI - .15); c.stroke(); },
    rainbow(c, r, P) { const cols = [P.pop, P.b, P.a, P.paper]; cols.forEach((col, i) => { c.strokeStyle = col; c.lineWidth = r * .2; c.beginPath(); c.arc(0, r * .3, r * (1 - i * .2), Math.PI, TAU); c.stroke(); }); },
    badge(c, r, P, t, word) { c.save(); c.rotate(t * .4); c.fillStyle = P.ink; burst(c, 0, 0, r, 16); c.fill(); c.restore(); c.fillStyle = P.paper; c.font = '900 ' + Math.round(r * .42) + 'px ' + P.font; c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillText(word || '2026', 0, r * .03); },
    arrow(c, r, P) { c.fillStyle = P.a; c.beginPath(); c.moveTo(-r, -r * .25); c.lineTo(r * .2, -r * .25); c.lineTo(r * .2, -r * .6); c.lineTo(r, 0); c.lineTo(r * .2, r * .6); c.lineTo(r * .2, r * .25); c.lineTo(-r, r * .25); c.closePath(); c.fill(); c.strokeStyle = P.ink; c.lineWidth = r * .08; c.stroke(); },
    word(c, r, P, t, word) { c.save(); c.fillStyle = P.a; rr(c, -r * 1.3, -r * .5, r * 2.6, r, r * .1); c.fill(); c.strokeStyle = P.ink; c.lineWidth = r * .08; c.stroke(); c.fillStyle = P.paper; c.font = '900 ' + Math.round(r * .62) + 'px ' + P.font; c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillText(word || 'KEEP', 0, r * .04); c.restore(); },
    pixelheart(c, r, P) { const g = r / 4, m = ['0110110', '1111111', '1111111', '0111110', '0011100', '0001000']; c.fillStyle = P.pop; m.forEach((row, j) => [...row].forEach((v, i) => { if (v === '1') c.fillRect((i - 3.5) * g, (j - 3) * g, g + .5, g + .5); })); },
    coin(c, r, P, t) { const sx = Math.abs(Math.cos(t * 3)); c.fillStyle = P.pop; c.beginPath(); c.ellipse(0, 0, r * Math.max(.12, sx), r, 0, 0, TAU); c.fill(); c.strokeStyle = P.ink; c.lineWidth = r * .1; c.stroke(); },
    cursor(c, r, P) { c.fillStyle = P.paper; c.strokeStyle = P.ink; c.lineWidth = r * .1; c.beginPath(); c.moveTo(-r * .5, -r); c.lineTo(-r * .5, r * .7); c.lineTo(-r * .05, r * .3); c.lineTo(r * .3, r); c.lineTo(r * .55, r * .85); c.lineTo(r * .22, r * .18); c.lineTo(r * .75, r * .18); c.closePath(); c.fill(); c.stroke(); },
    bar(c, r, P, t) { c.strokeStyle = P.ink; c.lineWidth = r * .08; rr(c, -r * 1.4, -r * .22, r * 2.8, r * .44, r * .22); c.stroke(); c.fillStyle = P.pop; rr(c, -r * 1.32, -r * .15, r * 2.64 * U.fract(t * .25), r * .3, r * .15); c.fill(); },
    phoneicon(c, r, P) { c.fillStyle = P.paper; rr(c, -r * .5, -r, r, r * 2, r * .18); c.fill(); c.strokeStyle = P.ink; c.lineWidth = r * .09; c.stroke(); c.fillStyle = P.a; rr(c, -r * .38, -r * .78, r * .76, r * 1.3, r * .08); c.fill(); c.fillStyle = P.ink; c.beginPath(); c.arc(0, r * .76, r * .1, 0, TAU); c.fill(); },
    antenna(c, r, P, t) { c.strokeStyle = P.ink; c.lineWidth = r * .09; c.beginPath(); c.moveTo(0, r); c.lineTo(0, -r * .3); c.moveTo(-r * .5, r); c.lineTo(0, -r * .1); c.lineTo(r * .5, r); c.stroke(); for (let i = 0; i < 3; i++) { const q = U.fract(t * .8 + i / 3); c.strokeStyle = rgba(P.pop, 1 - q); c.beginPath(); c.arc(0, -r * .3, r * (.25 + q * .9), -Math.PI * .8, -Math.PI * .2); c.stroke(); } },
    wave(c, r, P, t) { c.strokeStyle = P.pop; c.lineWidth = r * .12; c.beginPath(); for (let x = -r; x <= r; x += r / 12) { const y = Math.sin(x / r * 6 + t * 5) * r * .35 * (1 - Math.abs(x / r) * .5); if (x <= -r) c.moveTo(x, y); else c.lineTo(x, y); } c.stroke(); },
    inkdot(c, r, P) { c.fillStyle = rgba(P.a, .7); c.beginPath(); c.arc(0, 0, r * .6, 0, TAU); c.fill(); c.beginPath(); c.arc(r * .7, -r * .5, r * .18, 0, TAU); c.fill(); c.beginPath(); c.arc(-r * .6, r * .6, r * .12, 0, TAU); c.fill(); },
    leaf(c, r, P) { c.fillStyle = P.b; c.beginPath(); c.moveTo(-r, 0); c.quadraticCurveTo(0, -r * .9, r, 0); c.quadraticCurveTo(0, r * .9, -r, 0); c.fill(); c.strokeStyle = P.ink; c.lineWidth = r * .06; c.beginPath(); c.moveTo(-r, 0); c.lineTo(r, 0); c.stroke(); },
    stamp(c, r, P, t, word) { c.strokeStyle = P.pop; c.lineWidth = r * .09; c.beginPath(); c.arc(0, 0, r, 0, TAU); c.stroke(); c.beginPath(); c.arc(0, 0, r * .78, 0, TAU); c.stroke(); c.fillStyle = P.pop; c.font = '900 ' + Math.round(r * .5) + 'px ' + P.font; c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillText(word || '済', 0, r * .03); },
    clip(c, r, P) { c.strokeStyle = P.ink; c.lineWidth = r * .1; c.beginPath(); c.moveTo(-r * .3, r); c.lineTo(-r * .3, -r * .6); c.arc(0, -r * .6, r * .3, Math.PI, 0); c.lineTo(r * .3, r * .5); c.arc(r * .05, r * .5, r * .25, 0, Math.PI); c.lineTo(-r * .2, -r * .3); c.stroke(); },
    bolt(c, r, P) { c.fillStyle = P.pop; c.beginPath(); c.moveTo(r * .2, -r); c.lineTo(-r * .5, r * .1); c.lineTo(0, r * .1); c.lineTo(-r * .2, r); c.lineTo(r * .5, -r * .1); c.lineTo(0, -r * .1); c.closePath(); c.fill(); c.strokeStyle = P.ink; c.lineWidth = r * .07; c.stroke(); },
    note(c, r, P) { c.fillStyle = P.b; c.beginPath(); c.ellipse(-r * .2, r * .55, r * .38, r * .28, -.4, 0, TAU); c.fill(); c.fillRect(r * .1, -r * .9, r * .12, r * 1.5); c.beginPath(); c.moveTo(r * .22, -r * .9); c.quadraticCurveTo(r * .9, -r * .6, r * .6, 0); c.quadraticCurveTo(r * .7, -r * .4, r * .22, -r * .5); c.fill(); },
    ring(c, r, P, t) { c.strokeStyle = P.a; c.lineWidth = r * .14; c.beginPath(); c.arc(0, 0, r * .8, t, t + TAU * .8); c.stroke(); }
  };
  const DECO_WORDS = ['LOVE', 'HEY!', 'KEEP', 'YES', 'OK!', 'NEW', 'POP', 'GO!', 'WOW', '♡', '★', 'CUTE', 'NICE', 'HURRY'];

  /* ============================== 边框（KIT.border，整首一种） ============================== */
  function drawBorder(c, W, H, kind, P, t, k) {
    const m = H * .028;
    c.save();
    if (kind === 'stitch') {
      c.strokeStyle = P.a; c.lineWidth = m * 1.6; c.strokeRect(m * .8, m * .8, W - m * 1.6, H - m * 1.6);
      c.strokeStyle = P.paper; c.lineWidth = Math.max(2, H / 300); c.setLineDash([H * .018, H * .012]); c.lineDashOffset = -t * H * .02;
      rr(c, m * 2.2, m * 2.2, W - m * 4.4, H - m * 4.4, H * .03); c.stroke(); c.setLineDash([]);
      // 右上角挂牌（轻轻摇）
      c.save(); c.translate(W - m * 5, m * 1.4); c.rotate(Math.sin(t * 1.6) * .06); c.fillStyle = P.pop; c.beginPath(); c.moveTo(-m * 1.8, 0); c.lineTo(m * 1.8, 0); c.lineTo(m * 1.8, m * 4.2); c.lineTo(0, m * 3.4); c.lineTo(-m * 1.8, m * 4.2); c.closePath(); c.fill();
      c.fillStyle = P.paper; c.font = '800 ' + Math.round(m * .9) + 'px ' + P.font; c.textAlign = 'center'; c.fillText('N.E.K.O', 0, m * 1.5); c.restore();
    } else if (kind === 'tape') {
      c.strokeStyle = P.pop; c.lineWidth = m * 1.1; c.strokeRect(m * .55, m * .55, W - m * 1.1, H - m * 1.1);
      [[W * .12, m * .6, -.12], [W * .86, H - m * .7, .08]].forEach(([x, y, rot], i) => { c.save(); c.translate(x, y); c.rotate(rot + Math.sin(t * 1.2 + i) * .02); c.fillStyle = rgba(i ? P.a : P.b, .9); c.fillRect(-W * .06, -m * .9, W * .12, m * 1.8); c.restore(); });
    } else if (kind === 'hud') {
      c.strokeStyle = P.ink; c.lineWidth = Math.max(2, H / 260); const L = H * .07;
      [[m, m, 1, 1], [W - m, m, -1, 1], [m, H - m, 1, -1], [W - m, H - m, -1, -1]].forEach(([x, y, sx, sy]) => { c.beginPath(); c.moveTo(x + sx * L, y); c.lineTo(x, y); c.lineTo(x, y + sy * L); c.stroke(); });
      c.fillStyle = P.ink; c.font = '700 ' + Math.round(H * .024) + 'px Consolas,monospace'; c.textAlign = 'left'; c.fillText('NOW LOADING' + '.'.repeat(1 + Math.floor(t * 2) % 3), m * 2, H - m * 1.6);
      c.textAlign = 'right'; c.fillText('♥x' + (2 + Math.floor(t / 20) % 3), W - m * 2, m * 2.2);
    } else if (kind === 'crt') {
      const b = H * .05, r0 = H * .05, x0 = b, y0 = b * .7, w0 = W - 2 * b, h0 = H - b * 1.4;
      c.fillStyle = P.dark ? '#05050c' : P.ink;
      c.beginPath(); c.moveTo(0, 0); c.lineTo(W, 0); c.lineTo(W, H); c.lineTo(0, H); c.closePath();
      // 内框逆时针走一圈 → 非零环绕规则下就是一个洞
      c.moveTo(x0 + r0, y0); c.arcTo(x0, y0, x0, y0 + h0, r0); c.arcTo(x0, y0 + h0, x0 + w0, y0 + h0, r0); c.arcTo(x0 + w0, y0 + h0, x0 + w0, y0, r0); c.arcTo(x0 + w0, y0, x0, y0, r0); c.closePath();
      c.fill();
      // 屏幕反光弧 + 右下电源灯闪
      c.strokeStyle = rgba('#ffffff', .25); c.lineWidth = Math.max(2, H / 300); c.beginPath(); c.arc(x0 + w0 * .12, y0 + h0 * .16, H * .09, Math.PI, Math.PI * 1.5); c.stroke();
      c.fillStyle = rgba(P.pop, .5 + .5 * Math.abs(Math.sin(t * 2))); c.beginPath(); c.arc(W - b * .5, H - b * .4, H * .008, 0, TAU); c.fill();
    } else if (kind === 'film') {
      c.fillStyle = P.ink; const bh = H * .055; c.fillRect(0, 0, W, bh); c.fillRect(0, H - bh, W, bh);
      c.fillStyle = P.paper; const hw = H * .022, step = H * .05, o = (t * H * .03) % step;
      for (let x = -step + o; x < W + step; x += step) { c.fillRect(x, bh * .28, hw, bh * .44); c.fillRect(x, H - bh * .72, hw, bh * .44); }
    } else if (kind === 'neon') {
      c.shadowColor = P.a; c.shadowBlur = H * .02; c.strokeStyle = P.a; c.lineWidth = Math.max(3, H / 200); rr(c, m, m, W - 2 * m, H - 2 * m, H * .04); c.stroke();
      c.shadowBlur = 0; c.strokeStyle = rgba('#ffffff', .8); c.lineWidth = Math.max(1, H / 600); rr(c, m, m, W - 2 * m, H - 2 * m, H * .04); c.stroke();
    }
    c.restore();
  }
  const SAFE = { stitch: .085, tape: .05, hud: .055, crt: .075, film: .075, neon: .055 };   // 边框占掉的边距（字不能进去）

  /* ============================== 布景（stage，一个场景的主画面，8~20 s） ==============================
   * 每个布景返回可用的「字位」（textSlots）：{x,y,w,h,align} 归一化矩形，保证不和布景主体重叠。
   * k = 场景内进度 0..1；beat = 拍内脉冲；I = 能量。全部图层自己在动。*/
  const STAGES = {
    // 粉色房间：地板透视 + 墙 + 窗 + 一颗大爱心物件（恋/甜）
    room(c, W, H, t, o) {
      const P = o.P, hz = H * .62;
      c.fillStyle = P.bg2; c.fillRect(0, 0, W, hz); pattern(c, W, hz, o.pat, P.bg, .5, t, .1, .6);
      c.fillStyle = P.bg; c.fillRect(0, hz, W, H - hz);
      c.strokeStyle = rgba(P.a, .5); c.lineWidth = Math.max(1, H / 500);
      for (let i = -8; i <= 8; i++) { c.beginPath(); c.moveTo(W / 2 + i * W * .03, hz); c.lineTo(W / 2 + i * W * .16, H); c.stroke(); }
      for (let k2 = 0; k2 < 6; k2++) { const z = (k2 + (t * .15) % 1) / 6, y = hz + (H - hz) * z * z; c.beginPath(); c.moveTo(0, y); c.lineTo(W, y); c.stroke(); }
      // 窗
      const wx = W * .7, wy = H * .14, ww = W * .2, wh = H * .32; c.fillStyle = P.b; rr(c, wx, wy, ww, wh, H * .02); c.fill(); c.strokeStyle = P.a; c.lineWidth = H * .012; c.stroke();
      c.beginPath(); c.moveTo(wx + ww / 2, wy); c.lineTo(wx + ww / 2, wy + wh); c.moveTo(wx, wy + wh / 2); c.lineTo(wx + ww, wy + wh / 2); c.stroke();
      // 主物件：桌上一颗爱心礼盒，随拍子弹
      const cx = W * .5, cy = hz + H * .1, sc = 1 + .06 * o.beat; c.fillStyle = P.paper; c.beginPath(); c.ellipse(cx, cy + H * .02, W * .13, H * .035, 0, 0, TAU); c.fill();
      c.save(); c.translate(cx, cy - H * .07); c.scale(sc, sc); c.rotate(Math.sin(t * 1.3) * .06); c.fillStyle = P.pop; heart(c, 0, 0, H * .09); c.fill(); c.strokeStyle = P.paper; c.lineWidth = H * .012; c.beginPath(); c.moveTo(-H * .08, -H * .01); c.lineTo(H * .08, -H * .01); c.stroke(); c.restore();
      return [{ x: .08, y: .12, w: .56, h: .34 }, { x: .08, y: .12, w: .56, h: .2 }];
    },
    // 一排爱心（约束 ♥ 厳守 那种）：几颗大爱心依次弹出、各自摇
    hearts(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg; c.fillRect(0, 0, W, H); pattern(c, W, H, o.pat, P.bg2, .55, t, .1, 1);
      const n = 4, cols = [P.pop, P.a, P.b, P.paper];
      for (let i = 0; i < n; i++) { const q = back(clamp(o.k * 6 - i * .5)), x = W * (.2 + .2 * i), y = H * .46 + Math.sin(t * 2 + i) * H * .015;
        if (q <= 0) continue; c.save(); c.translate(x, y); c.rotate(Math.sin(t * 1.4 + i * 1.3) * .12); c.scale(q * (1 + .08 * o.beat * (i % 2 ? 1 : -1)), q);
        c.fillStyle = rgba(P.ink, .18); heart(c, H * .008, H * .012, H * .1); c.fill(); c.fillStyle = cols[i]; heart(c, 0, 0, H * .1); c.fill(); c.strokeStyle = P.paper; c.lineWidth = H * .01; c.stroke(); c.restore(); }
      return [{ x: .1, y: .7, w: .8, h: .18 }, { x: .1, y: .1, w: .8, h: .16 }];
    },
    // 街景：一排房子（色块 + 窗），远近两层视差滚动，路在下面
    street(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg2; c.fillRect(0, 0, W, H);
      c.fillStyle = P.bg; c.fillRect(0, H * .72, W, H * .28);
      for (let layer = 0; layer < 2; layer++) { const sp = layer ? .05 : .025, bw = W * (layer ? .2 : .14), base = H * (layer ? .74 : .66), off = (t * W * sp) % bw;
        for (let i = -1; i < W / bw + 2; i++) { const id = i + Math.floor(t * W * sp / bw), hh = H * (layer ? .34 : .24) * (.7 + .5 * h(id * 3.1 + layer)), x = i * bw - off;
          c.fillStyle = [P.a, P.b, P.paper, P.pop][(id % 4 + 4) % 4]; c.globalAlpha = layer ? 1 : .55; c.fillRect(x, base - hh, bw * .92, hh);
          c.beginPath(); c.moveTo(x - bw * .04, base - hh); c.lineTo(x + bw * .46, base - hh - bw * .22); c.lineTo(x + bw * .96, base - hh); c.closePath(); c.fill();
          c.fillStyle = rgba(P.ink, .55); for (let r = 0; r < 2; r++) for (let q = 0; q < 2; q++) c.fillRect(x + bw * (.16 + q * .4), base - hh + hh * (.2 + r * .36), bw * .2, hh * .18); } }
      c.globalAlpha = 1; c.strokeStyle = P.paper; c.lineWidth = H * .01; c.setLineDash([W * .05, W * .04]); c.lineDashOffset = t * W * .12; c.beginPath(); c.moveTo(0, H * .87); c.lineTo(W, H * .87); c.stroke(); c.setLineDash([]);
      return [{ x: .1, y: .1, w: .8, h: .22 }];
    },
    // 圆桌上的小物（66s 那个）：大圆桌 + 上面一个物件慢转，桌布图案滚动
    plate(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg; c.fillRect(0, 0, W, H); pattern(c, W, H, o.pat, P.bg2, .5, t, .12, .8);
      const cx = W * .5, cy = H * .56; c.fillStyle = rgba(P.ink, .15); c.beginPath(); c.ellipse(cx, cy + H * .05, W * .27, H * .12, 0, 0, TAU); c.fill();
      c.fillStyle = P.paper; c.beginPath(); c.ellipse(cx, cy, W * .27, H * .12, 0, 0, TAU); c.fill(); c.strokeStyle = P.a; c.lineWidth = H * .01; c.stroke();
      c.strokeStyle = rgba(P.a, .4); c.setLineDash([H * .02, H * .015]); c.lineDashOffset = -t * H * .03; c.beginPath(); c.ellipse(cx, cy, W * .22, H * .09, 0, 0, TAU); c.stroke(); c.setLineDash([]);
      c.save(); c.translate(cx, cy - H * .05 + Math.sin(t * 2) * H * .01); c.scale(1 + .05 * o.beat, 1 + .05 * o.beat); (DECO[o.deco[0]] || DECO.heart)(c, H * .075, P, t); c.restore();
      return [{ x: .1, y: .1, w: .8, h: .2 }, { x: .1, y: .76, w: .8, h: .14 }];
    },
    // 丝带墙：很多条彩带从上垂下、长短不一地摆
    ribbonwall(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg; c.fillRect(0, 0, W, H);
      const n = 14; for (let i = 0; i < n; i++) { const x = W * (i + .5) / n, len = H * (.25 + .5 * h(i * 3.3)) * eo(clamp(o.k * 5 - i * .08)), sw = Math.sin(t * (1 + h(i)) + i) * W * .012;
        c.strokeStyle = [P.a, P.b, P.pop, P.bg2][i % 4]; c.lineWidth = W * .028; c.lineCap = 'round'; c.beginPath(); c.moveTo(x, -10); c.quadraticCurveTo(x + sw, len * .5, x + sw * 2, len); c.stroke();
        c.fillStyle = P.paper; c.beginPath(); c.arc(x + sw * 2, len, W * .01, 0, TAU); c.fill(); }
      c.lineCap = 'butt';
      for (let i = 0; i < 6; i++) { const q = back(clamp(o.k * 8 - 1 - i * .3)); if (q <= 0) continue; const x = W * (.12 + .152 * i), y = H * (.3 + .1 * (i % 2)) + Math.sin(t * 1.8 + i) * H * .012;
        c.strokeStyle = rgba(P.ink, .4); c.lineWidth = Math.max(1, H / 500); c.beginPath(); c.moveTo(x, 0); c.lineTo(x, y - H * .045); c.stroke();
        c.save(); c.translate(x, y); c.rotate(Math.sin(t * 1.5 + i * 1.7) * .22); c.scale(q, q); (DECO[o.deco[i % o.deco.length]] || DECO.heart)(c, H * .05, P, t, DECO_WORDS[(i + o.seedI) % DECO_WORDS.length]); c.restore(); }
      return [{ x: .1, y: .62, w: .8, h: .26 }];
    },
    // 窗口：一个大窗（纸色面板）里放字，窗外云飘过
    window(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg2; c.fillRect(0, 0, W, H); pattern(c, W, H, o.pat, P.bg, .5, t, .1, .7);
      const x = W * .14, y = H * .16, w = W * .72, hh = H * .62; c.fillStyle = rgba(P.ink, .15); rr(c, x + H * .015, y + H * .02, w, hh, H * .03); c.fill();
      c.fillStyle = P.paper; rr(c, x, y, w, hh, H * .03); c.fill(); c.strokeStyle = P.a; c.lineWidth = H * .01; c.stroke();
      c.save(); rr(c, x, y, w, hh, H * .03); c.clip(); c.fillStyle = rgba(P.b, .55);
      for (let i = 0; i < 5; i++) { const cx = ((h(i * 3.1) * 1.4 - .2 + t * .02 * (1 + h(i))) % 1.4) * w + x - w * .2, cy = y + hh * (.15 + .7 * h(i * 5.7)), r = hh * .09; for (let q = 0; q < 4; q++) { c.beginPath(); c.arc(cx + q * r * .9, cy + (q % 2 ? -r * .3 : 0), r, 0, TAU); c.fill(); } }
      c.restore();
      return [{ x: .2, y: .26, w: .6, h: .42, bg: P.paper, box: 'plain' }];
    },
    // 贴纸拼贴（だいしきゅー）：大色块标签层层叠，各自旋转漂移
    collage(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.paper; c.fillRect(0, 0, W, H); pattern(c, W, H, 'grid', rgba(P.ink, .16), 1, t, .1, .5);
      const items = 9; for (let i = 0; i < items; i++) { const q = back(clamp(o.k * 7 - i * .35)); if (q <= 0) continue;
        const edge = i % 4, px = edge === 0 ? .08 + .2 * h(i) : edge === 1 ? .92 - .2 * h(i) : h(i * 2.3), py = edge === 2 ? .1 + .12 * h(i) : edge === 3 ? .9 - .12 * h(i) : h(i * 4.1);
        const x = W * px + Math.sin(t * (.5 + h(i)) + i) * W * .012, y = H * py + Math.cos(t * (.4 + h(i * 2)) + i) * H * .012, r = H * (.07 + .07 * h(i * 6.1));
        c.save(); c.translate(x, y); c.rotate((h(i * 8.3) - .5) * .7 + Math.sin(t * .8 + i) * .05); c.scale(q, q);
        const kind = o.deco[i % o.deco.length]; (DECO[kind] || DECO.burst)(c, r, P, t, DECO_WORDS[(i + o.seedI) % DECO_WORDS.length]); c.restore(); }
      return [{ x: .26, y: .34, w: .48, h: .32, bg: P.paper, plate: true }];
    },
    // 格子房间：方格纸墙 + 一块大色块面板（放字），面板旁边贴纸弹跳
    gridroom(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.paper; c.fillRect(0, 0, W, H); pattern(c, W, H, 'grid', rgba(P.ink, .14), 1, t, .1, .4);
      const left = o.side < 0, x = W * (left ? .08 : .44), y = H * .16, w = W * .48, hh = H * .66, e = eo(clamp(o.k * 5));
      c.save(); c.translate(x + w / 2, y + hh / 2); c.rotate((left ? -1 : 1) * .02); c.scale(e, 1); c.fillStyle = P.a; c.fillRect(-w / 2, -hh / 2, w, hh); c.restore();
      c.save(); c.beginPath(); c.rect(x, y, w * e, hh); c.clip(); pattern(c, W, H, 'confetti', P.paper, .8, t, .09, 1); c.restore();
      c.strokeStyle = P.ink; c.lineWidth = H * .008; c.strokeRect(x, y, w * e, hh);
      for (let i = 0; i < 3; i++) { const sx = W * (left ? .62 + .12 * i : .1 + .1 * i), sy = H * (.26 + .22 * i); c.save(); c.translate(sx, sy + Math.abs(Math.sin(t * 2.4 + i)) * -H * .03); c.rotate(Math.sin(t + i) * .15); (DECO[o.deco[(i + 1) % o.deco.length]] || DECO.star5)(c, H * .06, P, t, DECO_WORDS[(i + 3 + o.seedI) % DECO_WORDS.length]); c.restore(); }
      return [{ x: left ? .12 : .48, y: .26, w: .4, h: .46, bg: P.a, box: 'plain' }];
    },
    // 浏览器/聊天窗口：标题栏 + 几条气泡一条条弹出（歌词放在最新一条里）
    browser(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg; c.fillRect(0, 0, W, H); pattern(c, W, H, o.pat, P.bg2, .5, t, .1, .8);
      const x = W * .16, y = H * .12, w = W * .68, hh = H * .72, e = back(clamp(o.k * 6));
      c.save(); c.translate(W / 2, H / 2); c.scale(.9 + .1 * e, .9 + .1 * e); c.translate(-W / 2, -H / 2);
      c.fillStyle = rgba(P.ink, .2); rr(c, x + H * .014, y + H * .018, w, hh, H * .025); c.fill(); c.fillStyle = P.paper; rr(c, x, y, w, hh, H * .025); c.fill();
      c.fillStyle = P.a; rr(c, x, y, w, H * .07, H * .025); c.fill(); c.fillRect(x, y + H * .04, w, H * .03);
      c.fillStyle = P.paper; for (let i = 0; i < 3; i++) { c.beginPath(); c.arc(x + w - H * .035 - i * H * .04, y + H * .035, H * .011, 0, TAU); c.fill(); }
      c.strokeStyle = P.ink; c.lineWidth = H * .006; rr(c, x, y, w, hh, H * .025); c.stroke();
      // 旧消息气泡（灰一点）
      for (let i = 0; i < 2; i++) { const q = eo(clamp(o.k * 8 - i * .6)); if (q <= 0) continue; const bw = w * (.34 + .16 * h(i + o.seedI)), bx = i % 2 ? x + w - bw - w * .06 : x + w * .06, by = y + H * .11 + i * H * .1;
        c.globalAlpha = q; c.fillStyle = i % 2 ? rgba(P.a, .3) : rgba(P.ink, .12); rr(c, bx, by, bw, H * .07, H * .035); c.fill(); }
      c.globalAlpha = 1; c.restore();
      return [{ x: .22, y: .42, w: .56, h: .36, bg: P.paper, box: 'bubble' }];
    },
    // 大字铺底：歌里的关键词做成整面的描线大字墙，缓慢滚动；前面留一条干净的色带放歌词
    bigword(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg; c.fillRect(0, 0, W, H);
      const word = (o.word || 'LOVE') + '　', rows = 5, fs = H / rows * .9; c.font = '900 ' + Math.round(fs) + 'px ' + P.font; c.textBaseline = 'middle'; c.textAlign = 'left'; c.lineJoin = 'round';
      const uw = Math.max(60, c.measureText(word).width);
      for (let r = 0; r < rows; r++) { const y = (r + .5) * H / rows, dir = r % 2 ? 1 : -1, off = ((t * H * .1 * dir) % uw + uw) % uw;
        for (let x = -uw + off; x < W + uw; x += uw) { if (r % 2) { c.fillStyle = rgba(P.bg2, .9); c.fillText(word, x, y); } else { c.strokeStyle = rgba(P.a, .55); c.lineWidth = Math.max(2, H / 300); c.strokeText(word, x, y); } } }
      const bh = H * .26, e = eo(clamp(o.k * 5)); c.save(); c.translate(W / 2, H / 2); c.rotate(-.05 * o.side); c.fillStyle = P.pop; c.fillRect(-W * e, -bh / 2, W * 2 * e, bh); c.fillStyle = P.ink; c.fillRect(-W * e, bh / 2, W * 2 * e, H * .012); c.restore();
      return [{ x: .08, y: .39, w: .84, h: .22, onPop: true }];
    },
    // 棋盘地板 + 天空（128s 那种）：透视棋盘向前流，天上云
    checkerfloor(c, W, H, t, o) {
      const P = o.P, hz = H * .58; c.fillStyle = P.bg2; c.fillRect(0, 0, W, hz);
      c.fillStyle = rgba(P.paper, .8); for (let i = 0; i < 6; i++) { const cx = ((h(i * 3.1) + t * .012 * (1 + h(i))) % 1.3 - .15) * W, cy = hz * (.15 + .6 * h(i * 5.7)), r = hz * .1; for (let q = 0; q < 4; q++) { c.beginPath(); c.arc(cx + q * r * .9, cy + (q % 2 ? -r * .3 : 0), r, 0, TAU); c.fill(); } }
      const rows = 9; for (let r = 0; r < rows; r++) { const z0 = (r + (t * .5) % 1) / rows, z1 = (r + 1 + (t * .5) % 1) / rows, y0 = hz + (H - hz) * z0 * z0, y1 = hz + (H - hz) * Math.min(1, z1 * z1);
        for (let i = -8; i < 8; i++) { if ((i + r + Math.floor(t * .5)) % 2) continue; const xa = W / 2 + i * W * (.02 + .16 * z0 * z0), xb = W / 2 + (i + 1) * W * (.02 + .16 * z0 * z0), xc = W / 2 + (i + 1) * W * (.02 + .16 * z1 * z1), xd = W / 2 + i * W * (.02 + .16 * z1 * z1);
          c.fillStyle = P.paper; c.beginPath(); c.moveTo(xa, y0); c.lineTo(xb, y0); c.lineTo(xc, y1); c.lineTo(xd, y1); c.closePath(); c.fill(); } }
      c.fillStyle = rgba(P.a, .25); c.fillRect(0, hz, W, H - hz);
      return [{ x: .1, y: .1, w: .8, h: .26 }];
    },
    // 集中线舞台：中心放射线 + 网点，线随拍子变长
    burststage(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg; c.fillRect(0, 0, W, H); pattern(c, W, H, 'halftone', P.bg2, .8, t, .09, 1);
      c.save(); c.translate(W / 2, H / 2); const n = 40; for (let i = 0; i < n; i++) { const a = i * TAU / n + t * .05, r0 = H * (.34 + .1 * h(i * 3.3) - .06 * o.beat), r1 = Math.hypot(W, H); c.fillStyle = i % 2 ? P.paper : rgba(P.a, .5);
        c.beginPath(); c.moveTo(Math.cos(a - .03) * r1, Math.sin(a - .03) * r1); c.lineTo(Math.cos(a) * r0, Math.sin(a) * r0); c.lineTo(Math.cos(a + .03) * r1, Math.sin(a + .03) * r1); c.closePath(); c.fill(); }
      c.restore();
      return [{ x: .2, y: .34, w: .6, h: .32, plate: true }];
    },
    // 游戏选单：左边大字位，右边一个选单框，光标在选项上跳
    menu(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg; c.fillRect(0, 0, W, H); pattern(c, W, H, o.pat, P.bg2, .5, t, .1, .8);
      const x = W * .6, y = H * .24, w = W * .3, hh = H * .5, e = back(clamp(o.k * 6));
      c.save(); c.translate(x + w / 2, y + hh / 2); c.scale(e, e); c.fillStyle = P.paper; rr(c, -w / 2, -hh / 2, w, hh, H * .02); c.fill(); c.strokeStyle = P.ink; c.lineWidth = H * .008; c.stroke();
      c.fillStyle = P.a; rr(c, -w * .3, -hh / 2 - H * .03, w * .6, H * .06, H * .03); c.fill(); c.fillStyle = P.paper; c.font = '800 ' + Math.round(H * .03) + 'px ' + P.font; c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillText('Action', 0, -hh / 2);
      const opts = ['Sing', 'Dance', 'Sleep', 'Love'], sel = Math.floor(t * 1.5) % opts.length; c.textAlign = 'left'; c.font = '800 ' + Math.round(H * .042) + 'px ' + P.font;
      opts.forEach((s2, i) => { const yy = -hh * .28 + i * hh * .2; c.fillStyle = i === sel ? P.pop : P.ink; c.fillText(s2, -w * .2, yy); if (i === sel) { c.beginPath(); c.moveTo(-w * .36 + Math.sin(t * 8) * 3, yy - H * .014); c.lineTo(-w * .27 + Math.sin(t * 8) * 3, yy); c.lineTo(-w * .36 + Math.sin(t * 8) * 3, yy + H * .014); c.fill(); } });
      c.restore();
      return [{ x: .08, y: .28, w: .46, h: .44 }];
    },
    // 加载条画面：大进度条 + 上面一行字位（间奏/前奏也用）
    loading(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg2; c.fillRect(0, 0, W, H); pattern(c, W, H, o.pat, P.bg, .4, t, .1, .6);
      const x = W * .2, y = H * .66, w = W * .6, hh = H * .05; c.strokeStyle = P.paper; c.lineWidth = H * .008; rr(c, x, y, w, hh, hh / 2); c.stroke();
      c.fillStyle = P.paper; rr(c, x + hh * .18, y + hh * .18, (w - hh * .36) * clamp(o.k), hh * .64, hh * .32); c.fill();
      c.font = '700 ' + Math.round(H * .028) + 'px Consolas,monospace'; c.textAlign = 'center'; c.fillStyle = P.paper; c.fillText(Math.round(o.k * 100) + '%', W / 2, y + hh * 2);
      return [{ x: .12, y: .22, w: .76, h: .34 }];
    },
    // 对战画面：斜切两色，中间 VS 徽章转
    versus(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.a; c.fillRect(0, 0, W, H); c.fillStyle = P.b; c.beginPath(); c.moveTo(W * .58, 0); c.lineTo(W, 0); c.lineTo(W, H); c.lineTo(W * .42, H); c.closePath(); c.fill();
      c.save(); c.beginPath(); c.moveTo(W * .58, 0); c.lineTo(W, 0); c.lineTo(W, H); c.lineTo(W * .42, H); c.closePath(); c.clip(); pattern(c, W, H, 'stripe', rgba(P.paper, .35), 1, t, .08, 2); c.restore();
      c.save(); c.translate(W * .5, H * .5); c.scale(1 + .1 * o.beat, 1 + .1 * o.beat); DECO.badge(c, H * .1, P, t, 'VS'); c.restore();
      return [{ x: .06, y: .12, w: .38, h: .3 }, { x: .56, y: .58, w: .38, h: .3 }];
    },
    // 像素天空：像素云 + 像素星在闪，地平线像素山
    pixelsky(c, W, H, t, o) {
      const P = o.P, g = H / 36; c.fillStyle = P.bg; c.fillRect(0, 0, W, H);
      for (let i = 0; i < 50; i++) { if (Math.sin(t * 2 + i * 1.7) < 0) continue; c.fillStyle = i % 4 ? P.paper : P.pop; c.fillRect(Math.floor(h(i * 3.3) * W / g) * g, Math.floor(h(i * 7.1) * H * .6 / g) * g, g, g); }
      c.fillStyle = P.bg2; for (let i = 0; i < W / g + 1; i++) { const hh = Math.floor((6 + 5 * Math.sin(i * .35 + 1) + 3 * Math.sin(i * .9)) ) * g; c.fillRect(i * g, H - hh - g * 4, g + 1, hh + g * 4); }
      c.fillStyle = P.a; for (let i = 0; i < W / g + 1; i++) { const hh = Math.floor((3 + 2 * Math.sin(i * .5 + t * .5))) * g; c.fillRect(i * g, H - hh, g + 1, hh); }
      return [{ x: .1, y: .16, w: .8, h: .36 }];
    },
    // 雷达/电波：同心圆 + 扫描线 + 目标点一闪一闪（mosi mosi?）
    radar(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg; c.fillRect(0, 0, W, H); const cx = W * .7, cy = H * .5, R = H * .44;
      c.strokeStyle = rgba(P.b, .7); c.lineWidth = Math.max(1.5, H / 400); for (let i = 1; i <= 5; i++) { c.beginPath(); c.arc(cx, cy, R * i / 5, 0, TAU); c.stroke(); }
      for (let i = 0; i < 12; i++) { const a = i * TAU / 12; c.beginPath(); c.moveTo(cx, cy); c.lineTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R); c.stroke(); }
      const sa = t * 1.6; const g = c.createConicGradient ? c.createConicGradient(sa, cx, cy) : null;
      if (g) { g.addColorStop(0, rgba(P.pop, .55)); g.addColorStop(.12, rgba(P.pop, 0)); g.addColorStop(1, rgba(P.pop, 0)); c.fillStyle = g; c.beginPath(); c.arc(cx, cy, R, 0, TAU); c.fill(); }
      for (let i = 0; i < 5; i++) { const a = h(i * 3.1 + o.seedI) * TAU, r = R * (.25 + .7 * h(i * 5.7)), da = ((sa - a) % TAU + TAU) % TAU, q = Math.exp(-da * 1.2); c.fillStyle = rgba(P.pop, .25 + .75 * q); c.beginPath(); c.arc(cx + Math.cos(a) * r, cy + Math.sin(a) * r, H * (.008 + .012 * q), 0, TAU); c.fill(); }
      return [{ x: .07, y: .26, w: .38, h: .48 }];
    },
    // 手机：大手机在一侧，屏幕里消息气泡往上滚；外面电波圈扩散
    phone(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg; c.fillRect(0, 0, W, H); pattern(c, W, H, o.pat, P.bg2, .5, t, .1, .8);
      const left = o.side > 0, cx = W * (left ? .26 : .74), cy = H * .5, pw = H * .34, ph = H * .7;
      for (let i = 0; i < 3; i++) { const q = U.fract(t * .5 + i / 3); c.strokeStyle = rgba(P.a, (1 - q) * .7); c.lineWidth = H * .008; c.beginPath(); c.arc(cx, cy, pw * (.6 + q * 1.1), 0, TAU); c.stroke(); }
      c.save(); c.translate(cx, cy); c.rotate(Math.sin(t * 1.1) * .04 + (left ? -.06 : .06)); c.fillStyle = P.paper; rr(c, -pw / 2, -ph / 2, pw, ph, pw * .12); c.fill(); c.strokeStyle = P.ink; c.lineWidth = H * .01; c.stroke();
      c.save(); rr(c, -pw * .42, -ph * .42, pw * .84, ph * .8, pw * .06); c.clip(); c.fillStyle = P.b; c.fillRect(-pw / 2, -ph / 2, pw, ph);
      for (let i = 0; i < 7; i++) { const yy = ((i * .18 - t * .12) % 1.26 + 1.26) % 1.26 * ph - ph * .55, bw = pw * (.3 + .3 * h(i * 2.1)); c.fillStyle = i % 2 ? P.a : P.paper; rr(c, i % 2 ? pw * .38 - bw : -pw * .38, yy, bw, ph * .07, ph * .035); c.fill(); }
      c.restore(); c.restore();
      return [{ x: left ? .5 : .08, y: .26, w: .42, h: .48 }];
    },
    // 书桌：稿纸斜放 + 铅笔 + 一圈咖啡渍；字写在稿纸上
    desk(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg2; c.fillRect(0, 0, W, H); pattern(c, W, H, 'grid', rgba(P.ink, .08), 1, t, .14, .2);
      c.save(); c.translate(W * .5, H * .52); c.rotate(-.035); const w = W * .7, hh = H * .7; c.fillStyle = rgba(P.ink, .16); c.fillRect(-w / 2 + H * .012, -hh / 2 + H * .016, w, hh); c.fillStyle = P.paper; c.fillRect(-w / 2, -hh / 2, w, hh);
      c.strokeStyle = rgba(P.a, .35); c.lineWidth = Math.max(1, H / 800); for (let y = -hh / 2 + hh * .12; y < hh / 2; y += hh * .095) { c.beginPath(); c.moveTo(-w / 2, y); c.lineTo(w / 2, y); c.stroke(); }
      c.strokeStyle = rgba(P.pop, .5); c.beginPath(); c.moveTo(-w * .4, -hh / 2); c.lineTo(-w * .4, hh / 2); c.stroke(); c.restore();
      c.save(); c.translate(W * .86, H * .78 + Math.sin(t * .8) * H * .006); c.rotate(-.9); c.fillStyle = P.pop; c.fillRect(-H * .2, -H * .012, H * .36, H * .024); c.fillStyle = P.ink; c.beginPath(); c.moveTo(H * .16, -H * .012); c.lineTo(H * .2, 0); c.lineTo(H * .16, H * .012); c.fill(); c.restore();
      c.strokeStyle = rgba(P.a, .3); c.lineWidth = H * .01; c.beginPath(); c.arc(W * .14, H * .2, H * .06, 0, TAU); c.stroke();
      return [{ x: .26, y: .28, w: .5, h: .46, bg: P.paper, box: 'underline' }];
    },
    // 窗外天空：大窗框 + 流云 + 飞鸟线
    windowsky(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg; c.fillRect(0, 0, W, H); const g = c.createLinearGradient(0, 0, 0, H); g.addColorStop(0, P.bg2); g.addColorStop(1, P.bg); c.fillStyle = g; c.fillRect(0, 0, W, H);
      c.fillStyle = rgba(P.paper, .85); for (let i = 0; i < 7; i++) { const cx = ((h(i * 3.1) + t * .01 * (1 + h(i))) % 1.3 - .15) * W, cy = H * (.12 + .5 * h(i * 5.7)), r = H * (.05 + .05 * h(i * 2.2)); for (let q = 0; q < 5; q++) { c.beginPath(); c.arc(cx + q * r * .85, cy + (q % 2 ? -r * .35 : 0), r, 0, TAU); c.fill(); } }
      c.strokeStyle = P.ink; c.lineWidth = H * .006; for (let i = 0; i < 3; i++) { const bx = ((t * .03 + i * .23) % 1.2 - .1) * W, by = H * (.2 + .1 * i) + Math.sin(t * 2 + i) * H * .01, fl = Math.sin(t * 6 + i) * H * .012; c.beginPath(); c.moveTo(bx - H * .025, by - fl); c.lineTo(bx, by); c.lineTo(bx + H * .025, by - fl); c.stroke(); }
      c.strokeStyle = P.paper; c.lineWidth = H * .03; c.strokeRect(W * .05, H * .06, W * .9, H * .88); c.lineWidth = H * .016; c.beginPath(); c.moveTo(W * .5, H * .06); c.lineTo(W * .5, H * .94); c.moveTo(W * .05, H * .5); c.lineTo(W * .95, H * .5); c.stroke();
      return [{ x: .1, y: .56, w: .36, h: .32 }, { x: .54, y: .12, w: .36, h: .32 }];
    },
    // 地平线：海/大地一条线 + 太阳或月亮缓缓升 + 波光
    horizon(c, W, H, t, o) {
      const P = o.P, hz = H * .62; const g = c.createLinearGradient(0, 0, 0, hz); g.addColorStop(0, P.bg2); g.addColorStop(1, P.bg); c.fillStyle = g; c.fillRect(0, 0, W, hz);
      const sy = hz - H * (.04 + .22 * clamp(o.k)); c.fillStyle = P.pop; c.beginPath(); c.arc(W * .68, sy, H * .11, 0, TAU); c.fill(); c.fillStyle = rgba(P.pop, .25); c.beginPath(); c.arc(W * .68, sy, H * .16 + H * .01 * o.beat, 0, TAU); c.fill();
      c.fillStyle = P.a; c.fillRect(0, hz, W, H - hz);
      c.strokeStyle = rgba(P.paper, .7); c.lineWidth = H * .006; for (let i = 0; i < 26; i++) { const y = hz + (H - hz) * (h(i * 3.3) * .95 + .03), x = ((h(i * 7.1) + t * .02 * (1 + h(i))) % 1.1 - .05) * W, w = W * (.02 + .06 * h(i * 2.9)); c.beginPath(); c.moveTo(x, y); c.lineTo(x + w, y); c.stroke(); }
      return [{ x: .08, y: .12, w: .5, h: .36 }];
    },
    // 月亮：大月亮 + 云纹慢移 + 星点（夜）
    moon(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg; c.fillRect(0, 0, W, H); pattern(c, W, H, 'stars', P.paper, .8, t, .09, 0);
      const cx = W * (o.side > 0 ? .28 : .72), cy = H * .46, R = H * .3; c.fillStyle = rgba(P.b, .18); c.beginPath(); c.arc(cx, cy, R * 1.25 + H * .01 * o.beat, 0, TAU); c.fill(); c.fillStyle = P.paper; c.beginPath(); c.arc(cx, cy, R, 0, TAU); c.fill();
      c.save(); c.beginPath(); c.arc(cx, cy, R, 0, TAU); c.clip(); c.fillStyle = rgba(P.b, .35); for (let i = 0; i < 6; i++) { c.beginPath(); c.arc(cx + (h(i * 3.1) - .5) * R * 1.6 + Math.sin(t * .2 + i) * R * .05, cy + (h(i * 5.7) - .5) * R * 1.6, R * (.1 + .16 * h(i)), 0, TAU); c.fill(); } c.restore();
      c.fillStyle = rgba(P.bg2, .9); for (let i = 0; i < 3; i++) { const x = ((t * .012 * (1 + i * .4) + i * .37) % 1.4 - .2) * W, y = H * (.62 + .1 * i); rr(c, x, y, W * .34, H * .035, H * .018); c.fill(); }
      return [{ x: o.side > 0 ? .52 : .07, y: .22, w: .41, h: .5 }];
    },
    // 信纸：一封信展开（折线），字写在信上；信封在角落
    letter(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg; c.fillRect(0, 0, W, H); pattern(c, W, H, o.pat, P.bg2, .45, t, .1, .4);
      const e = eo(clamp(o.k * 4)), w = W * .62, hh = H * .68 * e, x = W * .19, y = H * .5 - hh / 2; c.fillStyle = rgba(P.ink, .14); c.fillRect(x + H * .012, y + H * .016, w, hh); c.fillStyle = P.paper; c.fillRect(x, y, w, hh);
      c.strokeStyle = rgba(P.a, .35); c.lineWidth = Math.max(1, H / 800); for (let i = 1; i < 3; i++) { c.beginPath(); c.moveTo(x, y + hh * i / 3); c.lineTo(x + w, y + hh * i / 3); c.stroke(); }
      c.save(); c.translate(W * .86, H * .8); c.rotate(.2 + Math.sin(t) * .03); c.fillStyle = P.a; c.fillRect(-H * .09, -H * .06, H * .18, H * .12); c.strokeStyle = P.paper; c.lineWidth = H * .006; c.beginPath(); c.moveTo(-H * .09, -H * .06); c.lineTo(0, H * .01); c.lineTo(H * .09, -H * .06); c.stroke(); c.restore();
      return [{ x: .24, y: .3, w: .52, h: .4, bg: P.paper, box: 'underline' }];
    },
    // 城市天际线：两层楼影视差 + 窗灯闪 + 探照灯扫
    cityline(c, W, H, t, o) {
      const P = o.P; const g = c.createLinearGradient(0, 0, 0, H); g.addColorStop(0, P.bg); g.addColorStop(1, P.bg2); c.fillStyle = g; c.fillRect(0, 0, W, H);
      c.save(); c.globalCompositeOperation = 'lighter'; for (let i = 0; i < 3; i++) { const a = -Math.PI / 2 + Math.sin(t * .5 + i * 2) * .5, ox = W * (.25 + .25 * i); const gg = c.createLinearGradient(ox, H, ox + Math.cos(a) * H, H + Math.sin(a) * H); gg.addColorStop(0, rgba(P.a, .35)); gg.addColorStop(1, rgba(P.a, 0)); c.fillStyle = gg; c.beginPath(); c.moveTo(ox, H); c.lineTo(ox + Math.cos(a - .09) * H * 1.3, H + Math.sin(a - .09) * H * 1.3); c.lineTo(ox + Math.cos(a + .09) * H * 1.3, H + Math.sin(a + .09) * H * 1.3); c.fill(); } c.restore();
      for (let layer = 0; layer < 2; layer++) { const bw = W * (layer ? .09 : .06), off = (t * W * (layer ? .02 : .01)) % bw; for (let i = -1; i < W / bw + 2; i++) { const id = i + Math.floor(t * W * (layer ? .02 : .01) / bw), hh = H * (layer ? .42 : .3) * (.4 + .6 * h(id * 3.7 + layer * 9)), x = i * bw - off; c.fillStyle = layer ? rgba('#0b0c1c', .95) : rgba(P.bg2, .9); c.fillRect(x, H - hh, bw * .9, hh);
          if (layer) for (let r = 0; r < 6; r++) for (let q = 0; q < 2; q++) if (h(id * 5 + r * 3 + q + Math.floor(t * .7 + id)) > .55) { c.fillStyle = rgba(P.pop, .85); c.fillRect(x + bw * (.18 + q * .4), H - hh + hh * (.1 + r * .14), bw * .2, hh * .06); } } }
      return [{ x: .1, y: .1, w: .8, h: .34 }];
    },
    // 聚光灯：三束追光交叉扫，地面光斑；空台（peak 用）
    spotlight(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg; c.fillRect(0, 0, W, H); c.save(); c.globalCompositeOperation = 'lighter';
      for (let i = 0; i < 4; i++) { const ox = W * (.15 + .23 * i), a = Math.PI / 2 + Math.sin(t * (.6 + .15 * i) + i * 1.7) * .4; const gg = c.createLinearGradient(ox, 0, ox + Math.cos(a) * H, Math.sin(a) * H); gg.addColorStop(0, rgba([P.a, P.b, P.pop, P.a][i], .5)); gg.addColorStop(1, rgba(P.a, 0)); c.fillStyle = gg; c.beginPath(); c.moveTo(ox, 0); c.lineTo(ox + Math.cos(a - .1) * H * 1.3, Math.sin(a - .1) * H * 1.3); c.lineTo(ox + Math.cos(a + .1) * H * 1.3, Math.sin(a + .1) * H * 1.3); c.fill(); }
      c.restore(); c.fillStyle = rgba(P.bg2, .9); c.fillRect(0, H * .82, W, H * .18); c.fillStyle = rgba(P.pop, .25 + .2 * o.beat); c.beginPath(); c.ellipse(W / 2, H * .84, W * .3, H * .04, 0, 0, TAU); c.fill();
      return [{ x: .12, y: .3, w: .76, h: .36 }];
    }
  };

  /* ============================== 伴奏秀（BREAK）：一整套会动的图形，不是几条线 ============================== */
  const BREAKS = {
    // 钢琴 + 落键音符瀑布（节奏游戏式）：音符块从上落到键上，键被按下发光，五线谱在背后流
    pianofall(c, W, H, t, o) {
      const P = o.P, n = 24, kw = W / n, top = H * .72, b = t * o.bpm / 60;
      c.fillStyle = P.bg; c.fillRect(0, 0, W, H); pattern(c, W, H, o.pat, P.bg2, .35, t, .1, .5);
      c.strokeStyle = rgba(P.ink, .2); c.lineWidth = Math.max(1, H / 600); for (let i = 0; i <= n; i++) { c.beginPath(); c.moveTo(i * kw, 0); c.lineTo(i * kw, top); c.stroke(); }
      const hitE = new Array(n).fill(0);
      for (let j = Math.floor(b * 2) - 8; j < Math.floor(b * 2) + 14; j++) { const cnt = 1 + Math.floor(h(j * 3.7) * (1 + 2 * o.I)); for (let q = 0; q < cnt; q++) { const key = Math.floor(h(j * 5.31 + q * 9.7 + o.seedI) * n), len = H * (.06 + .12 * h(j + q)), d = j / 2 - b, y = top - d * H * .22 - len;
          if (d < 0) hitE[key] = Math.max(hitE[key], Math.exp(d * 2.2)); if (y > top || y + len < 0) continue; c.fillStyle = [P.a, P.pop, P.b][(key + q) % 3]; rr(c, key * kw + kw * .12, y, kw * .76, Math.min(len, top - y), kw * .2); c.fill(); } }
      for (let i = 0; i < n; i++) { const e = hitE[i]; c.fillStyle = e > .05 ? U.rgba(P.pop, .35 + .65 * e) : P.paper; c.fillRect(i * kw, top + e * H * .012, kw - 1, H - top); c.strokeStyle = P.ink; c.lineWidth = Math.max(1.5, H / 400); c.strokeRect(i * kw, top + e * H * .012, kw - 1, H - top);
        if (e > .2) { c.fillStyle = rgba(P.pop, e * .5); c.fillRect(i * kw, top - H * .1 * e, kw, H * .1 * e); } }
      for (let i = 0; i < n - 1; i++) { if ([2, 6].includes(i % 7)) continue; c.fillStyle = P.ink; c.fillRect((i + 1) * kw - kw * .3, top + hitE[i] * H * .012, kw * .6, (H - top) * .58); }
    },
    // 均衡器城市：几十根柱子像楼群，随能量起伏，顶上小帽缓落；背后大圆盘转 + 环形频谱
    eqcity(c, W, H, t, o) {
      const P = o.P, b = t * o.bpm / 60; c.fillStyle = P.bg; c.fillRect(0, 0, W, H);
      const cx = W / 2, cy = H * .46, R = H * .3; c.save(); c.translate(cx, cy); c.rotate(t * .25); c.fillStyle = P.bg2; c.beginPath(); c.arc(0, 0, R, 0, TAU); c.fill(); c.strokeStyle = rgba(P.a, .6); c.lineWidth = H * .006; for (let i = 3; i < 9; i++) { c.beginPath(); c.arc(0, 0, R * i / 9, i, i + TAU * .8); c.stroke(); } c.fillStyle = P.pop; c.beginPath(); c.arc(0, 0, R * .2, 0, TAU); c.fill(); c.restore();
      c.save(); c.translate(cx, cy); for (let i = 0; i < 64; i++) { const a = i * TAU / 64 - Math.PI / 2, e = (.25 + .75 * o.I) * (.3 + .7 * Math.abs(Math.sin(i * .7 + Math.floor(b * 2) * 1.3))) * (.6 + .4 * o.beat), r0 = R * 1.08, r1 = r0 + H * .14 * e; c.strokeStyle = i % 4 ? P.a : P.pop; c.lineWidth = H * .012; c.lineCap = 'round'; c.beginPath(); c.moveTo(Math.cos(a) * r0, Math.sin(a) * r0); c.lineTo(Math.cos(a) * r1, Math.sin(a) * r1); c.stroke(); } c.restore(); c.lineCap = 'butt';
      const n = 36, bw = W / n; for (let i = 0; i < n; i++) { const e = (.2 + .8 * o.I) * (.25 + .75 * Math.abs(Math.sin(i * .9 + Math.floor(b * 2) * 1.7))) * (.7 + .3 * o.beat), hh = H * .26 * e; c.fillStyle = i % 3 ? rgba(P.ink, P.dark ? .5 : .85) : P.a; c.fillRect(i * bw + bw * .1, H - hh, bw * .8, hh); c.fillStyle = P.pop; c.fillRect(i * bw + bw * .1, H - hh - H * .02, bw * .8, H * .008); }
    },
    // 节拍器 + 钟摆阵：一排钟摆不同长度，摆出波浪（摆波）；刻度弧、落拍闪点
    pendulum(c, W, H, t, o) {
      const P = o.P, b = t * o.bpm / 60; c.fillStyle = P.bg; c.fillRect(0, 0, W, H); pattern(c, W, H, o.pat, P.bg2, .35, t, .1, .4);
      c.fillStyle = P.ink; c.fillRect(W * .08, H * .1, W * .84, H * .016);
      const n = 15; for (let i = 0; i < n; i++) { const x = W * (.12 + .76 * i / (n - 1)), L = H * (.3 + .42 * i / (n - 1)), ang = Math.sin(b * Math.PI * (1 - i * .028)) * .5, bx = x + Math.sin(ang) * L, by = H * .11 + Math.cos(ang) * L;
        c.strokeStyle = rgba(P.ink, .7); c.lineWidth = Math.max(1.5, H / 400); c.beginPath(); c.moveTo(x, H * .11); c.lineTo(bx, by); c.stroke();
        c.fillStyle = [P.a, P.pop, P.b][i % 3]; c.beginPath(); c.arc(bx, by, H * .028, 0, TAU); c.fill(); c.strokeStyle = P.ink; c.stroke(); }
      const edge = Math.pow(Math.abs(Math.sin(b * Math.PI)), 10); c.fillStyle = rgba(P.pop, edge * .5); c.fillRect(0, 0, W, H);
    },
    // 万花镜几何：多层多边形对称旋转，层数随能量长；每拍换一次边数
    kaleido(c, W, H, t, o) {
      const P = o.P, b = t * o.bpm / 60; c.fillStyle = P.bg; c.fillRect(0, 0, W, H); pattern(c, W, H, o.pat, P.bg2, .4, t, .1, .6);
      for (let i = 6; i > 0; i--) { c.strokeStyle = rgba(i % 2 ? P.a : P.b, .35); c.lineWidth = H * .01; c.setLineDash([H * .04, H * .025]); c.lineDashOffset = t * H * .05 * (i % 2 ? 1 : -1); c.beginPath(); c.arc(W / 2, H / 2, H * .09 * i * (1 + .04 * o.beat), 0, TAU); c.stroke(); }
      c.setLineDash([]);
      c.save(); c.translate(W / 2, H / 2); const layers = 5 + Math.round(5 * o.I);
      for (let L = layers; L > 0; L--) { const N = 3 + (Math.floor(b / 4) + L) % 5, R = H * .1 * L * (1 + .06 * o.beat), rot = t * .15 * (L % 2 ? 1 : -1) + L;
        for (let k2 = 0; k2 < N; k2++) { const a = rot + k2 * TAU / N; c.save(); c.rotate(a); c.translate(R, 0); c.rotate(t * .5 + L);
          c.fillStyle = L % 3 === 0 ? P.pop : L % 3 === 1 ? P.a : P.b; c.globalAlpha = .85; c.beginPath(); for (let v = 0; v < N; v++) { const va = v * TAU / N; c.lineTo(Math.cos(va) * H * .035 * (1 + L * .12), Math.sin(va) * H * .035 * (1 + L * .12)); } c.closePath(); c.fill(); c.strokeStyle = P.ink; c.lineWidth = Math.max(1, H / 500); c.stroke(); c.restore(); } }
      c.restore(); c.globalAlpha = 1;
    },
    // 磁带/卡带机：两个卷盘转、磁带走、VU 表指针摆、计数器跳
    cassette(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.bg; c.fillRect(0, 0, W, H); pattern(c, W, H, o.pat, P.bg2, .4, t, .1, .6);
      const x = W * .2, y = H * .2, w = W * .6, hh = H * .5; c.fillStyle = rgba(P.ink, .2); rr(c, x + H * .015, y + H * .02, w, hh, H * .03); c.fill(); c.fillStyle = P.paper; rr(c, x, y, w, hh, H * .03); c.fill(); c.strokeStyle = P.ink; c.lineWidth = H * .01; c.stroke();
      c.fillStyle = P.a; rr(c, x + w * .08, y + hh * .12, w * .84, hh * .2, H * .01); c.fill();
      c.fillStyle = P.ink; rr(c, x + w * .16, y + hh * .42, w * .68, hh * .36, H * .02); c.fill();
      [[x + w * .32, 1], [x + w * .68, -1]].forEach(([cx, dir], i) => { c.save(); c.translate(cx, y + hh * .6); c.rotate(t * 2.4 * dir); c.fillStyle = P.paper; c.beginPath(); c.arc(0, 0, hh * (.1 + .03 * (i ? clamp(o.k) : 1 - clamp(o.k))), 0, TAU); c.fill(); c.fillStyle = P.ink; for (let q = 0; q < 6; q++) { c.save(); c.rotate(q * TAU / 6); c.fillRect(hh * .035, -hh * .008, hh * .04, hh * .016); c.restore(); } c.restore(); });
      for (let i = 0; i < 2; i++) { const cx = W * (.3 + .4 * i), cy = H * .9, ang = -Math.PI / 2 + (o.I - .5 + .4 * o.beat * (i ? 1 : .7)) * 1.2; c.strokeStyle = P.ink; c.lineWidth = H * .008; c.beginPath(); c.arc(cx, cy, H * .1, Math.PI * 1.15, Math.PI * 1.85); c.stroke(); c.strokeStyle = P.pop; c.beginPath(); c.moveTo(cx, cy); c.lineTo(cx + Math.cos(ang) * H * .1, cy + Math.sin(ang) * H * .1); c.stroke(); }
    },
    // 手绘涂鸦接龙：一笔一笔画出一串小图（星、心、花、音符……），画完一个换下一个
    doodle(c, W, H, t, o) {
      const P = o.P; c.fillStyle = P.paper; c.fillRect(0, 0, W, H); pattern(c, W, H, 'ruled', rgba(P.a, .25), 1, t, .1, 0);
      const kinds = o.deco.concat(['note', 'star4', 'heart', 'flower']), n = 10, per = Math.max(.45, Math.min(1.1, (o.t1 - o.t0 - 1) / n));
      if (DOODLE.icons) {
        const pool = (o.doodles && o.doodles.length ? o.doodles : []).concat(['heart', 'star', 'flower', 'moon', 'clouds', 'mic', 'love-letter', 'sparkle-heart', 'cat', 'sun']);
        for (let i = 0; i < n; i++) { const age = (t - o.t0) / per - i; if (age <= 0) break; const q = back(clamp(age * 1.6)), x = W * (.12 + .19 * (i % 5)), y = H * (.3 + .36 * Math.floor(i / 5)) + Math.sin(t * 1.5 + i) * H * .012;
          c.save(); c.translate(x, y); c.rotate((h(i * 3.3 + o.seedI) - .5) * .4 + Math.sin(t + i) * .05); c.scale(q, q);
          // 线稿「描出来」：用虚线偏移模拟一笔一笔画
          const prog = clamp(age * 1.2); c.setLineDash([400 * prog, 400]); doodle(c, pool[(i + o.seedI) % pool.length], H * .085, P.ink, i % 3 === 0 ? rgba(P.a, .35) : null); c.setLineDash([]); c.restore(); }
        return;
      }
      for (let i = 0; i < n; i++) { const age = (t - o.t0) / per - i; if (age <= 0) break; const q = back(clamp(age * 1.6)), x = W * (.12 + .19 * (i % 5)), y = H * (.3 + .36 * Math.floor(i / 5)) + Math.sin(t * 1.5 + i) * H * .012;
        c.save(); c.translate(x, y); c.rotate((h(i * 3.3 + o.seedI) - .5) * .5 + Math.sin(t + i) * .06); c.scale(q, q); (DECO[kinds[i % kinds.length]] || DECO.heart)(c, H * .075, P, t, DECO_WORDS[(i + o.seedI) % DECO_WORDS.length]); c.restore(); }
      const cur = Math.min(n - 1, Math.floor((t - o.t0) / per)), px = W * (.12 + .19 * (cur % 5)) + H * .09, py = H * (.3 + .36 * Math.floor(cur / 5)) + H * .08;
      c.save(); c.translate(px + Math.sin(t * 14) * 3, py + Math.cos(t * 11) * 3); c.rotate(-.8); c.fillStyle = P.pop; c.fillRect(0, -H * .01, H * .16, H * .02); c.fillStyle = P.ink; c.beginPath(); c.moveTo(0, -H * .01); c.lineTo(-H * .03, 0); c.lineTo(0, H * .01); c.fill(); c.restore();
    },
    // 列车窗：窗框固定，窗外电线杆、楼、云三层视差飞过（旅途感）
    trainwindow(c, W, H, t, o) {
      const P = o.P; const g = c.createLinearGradient(0, 0, 0, H); g.addColorStop(0, P.bg2); g.addColorStop(1, P.bg); c.fillStyle = g; c.fillRect(0, 0, W, H);
      c.fillStyle = rgba(P.paper, .8); for (let i = 0; i < 5; i++) { const cx = (1.2 - (h(i * 3.1) + t * .03) % 1.4) * W, cy = H * (.15 + .25 * h(i * 5.7)), r = H * .06; for (let q = 0; q < 4; q++) { c.beginPath(); c.arc(cx + q * r * .9, cy + (q % 2 ? -r * .3 : 0), r, 0, TAU); c.fill(); } }
      const bw = W * .12; for (let i = -1; i < W / bw + 2; i++) { const id = i - Math.floor(t * W * .12 / bw), x = i * bw + (t * W * .12) % bw - bw, hh = H * (.12 + .2 * h(id * 3.7)); c.fillStyle = rgba(P.a, .6); c.fillRect(x, H * .7 - hh, bw * .85, hh); }
      c.fillStyle = P.ink; const pw = W * .45; for (let i = -1; i < 4; i++) { const x = i * pw + (t * W * .5) % pw; c.fillRect(x, H * .12, H * .014, H * .6); c.fillRect(x - H * .05, H * .18, H * .114, H * .01); }
      c.strokeStyle = P.ink; c.lineWidth = H * .004; for (let k2 = 0; k2 < 3; k2++) { c.beginPath(); for (let x = 0; x <= W; x += W / 40) { const ph = ((x - (t * W * .5) % pw) / pw) % 1, y = H * (.18 + k2 * .02) + Math.sin(((ph + 1) % 1) * Math.PI) * H * .05; if (!x) c.moveTo(x, y); else c.lineTo(x, y); } c.stroke(); }
      c.fillStyle = P.bg2; c.fillRect(0, H * .7, W, H * .3);
      c.strokeStyle = P.paper; c.lineWidth = H * .05; rr(c, W * .06, H * .08, W * .88, H * .78, H * .06); c.stroke(); c.fillStyle = P.paper; c.fillRect(W * .495, H * .08, W * .01, H * .78);
    },
    // 烟花：几发按拍子升空炸开，火花拖尾下落
    fireworks(c, W, H, t, o) {
      const P = o.P, b = t * o.bpm / 60; const sky = P.dark ? P.bg : P.ink; const g0 = c.createLinearGradient(0, 0, 0, H); g0.addColorStop(0, sky); g0.addColorStop(1, P.dark ? P.bg2 : P.a); c.fillStyle = g0; c.fillRect(0, 0, W, H); pattern(c, W, H, 'stars', '#ffffff', .6, t, .09, 0);
      for (let j = Math.floor(b) - 5; j <= Math.floor(b); j++) { const age = (b - j) / 2, cx = W * (.12 + .76 * h(j * 3.7 + o.seedI)), cy = H * (.18 + .34 * h(j * 5.1)), col = [P.pop, '#ffffff', P.b, P.a][((j % 4) + 4) % 4];
        if (age < .35) { const q = age / .35; c.strokeStyle = col; c.lineWidth = H * .006; c.beginPath(); c.moveTo(cx, H); c.lineTo(cx, lerp(H, cy, eo(q))); c.stroke(); continue; }
        const q = clamp((age - .35) / 2.2), R = H * (.2 + .16 * h(j * 2.3)) * eo(q); for (let i = 0; i < 44; i++) { const a = i * TAU / 44 + j, rr2 = R * (i % 2 ? 1 : .62), x = cx + Math.cos(a) * rr2, y = cy + Math.sin(a) * rr2 + q * q * H * .12; c.fillStyle = rgba(col, 1 - q); c.beginPath(); c.arc(x, y, H * .011 * (1 - q * .5), 0, TAU); c.fill(); c.strokeStyle = rgba(col, (1 - q) * .5); c.lineWidth = H * .003; c.beginPath(); c.moveTo(cx + Math.cos(a) * R * .7, cy + Math.sin(a) * R * .7 + q * q * H * .08); c.lineTo(x, y); c.stroke(); } }
      c.fillStyle = rgba('#000000', .5); for (let i = 0; i < 20; i++) c.fillRect(i * W / 20, H - H * (.06 + .1 * h(i * 3.3)), W / 20 + 1, H * .2);
    }
  };
  const BREAK_BY_KIT = { stitch: ['doodle', 'pianofall', 'pendulum', 'fireworks'], collage: ['kaleido', 'eqcity', 'doodle', 'cassette'], arcade: ['pianofall', 'eqcity', 'kaleido'],
    denpa: ['cassette', 'eqcity', 'pendulum'], paper: ['trainwindow', 'doodle', 'pendulum', 'pianofall'], neon: ['eqcity', 'fireworks', 'kaleido', 'pianofall'] };

  /* ============================== 字（保证不出屏、不被挡） ==============================
   * 字只画在「字位」矩形里：先按字位宽高算字号（自动换行、自动缩小），再画容器（box），最后画字。
   * 所有字位都先扣掉边框安全边距。*/
  function wrapFit(c, text, w, hh, font, wt, maxFs, vertical) {
    const chars = [...String(text).trim()];
    for (let rows = 1; rows <= 4; rows++) {
      const per = Math.ceil(chars.length / rows), lines = [];
      for (let i = 0; i < chars.length; i += per) lines.push(chars.slice(i, i + per).join(''));
      let fs = Math.min(maxFs, hh / (lines.length * 1.22));
      c.font = wt + ' ' + Math.round(fs) + 'px ' + font;
      const mw = Math.max(...lines.map(l => c.measureText(l).width));
      if (mw > w) fs *= w / mw;
      if (fs >= Math.min(maxFs, hh / (rows + 1) / 1.22) * .82 || rows === 4 || chars.length <= 6) { c.font = wt + ' ' + Math.round(fs) + 'px ' + font; return { lines, fs }; }
    }
  }
  function lumOf(col) { const m = String(col).match(/^#([0-9a-f]{6})$/i); if (!m) return .5; const v = parseInt(m[1], 16); return (.299 * (v >> 16 & 255) + .587 * (v >> 8 & 255) + .114 * (v & 255)) / 255; }
  // 在底色 bgc 上可读的字色：底亮用深色、底暗用浅色；强调色如果和底太近就换成另一头
  function inkOn(bgc, P) {
    const L = lumOf(bgc), dark = lumOf(P.ink) < lumOf(P.paper) ? P.ink : (P.dark ? P.bg : '#2f2a36'), light = '#ffffff';
    const ink = L > .55 ? dark : light, cands = [P.pop, P.a, P.b, L > .55 ? dark : P.paper];
    let hot = cands[0], best = -1; for (const cnd of cands) { const d = Math.abs(lumOf(cnd) - L) + (cnd === P.pop ? .12 : 0); if (d > best && cnd !== ink) { best = d; hot = cnd; } }
    if (Math.abs(lumOf(hot) - L) < .22) hot = L > .55 ? '#e0245e' : '#ffe066';
    return { ink, hot };
  }
  function drawText(c, W, H, slot, line, t, o) {
    const P = o.P, sx = slot.x * W, sy = slot.y * H, sw = slot.w * W, sh = slot.h * H, text = String(line.text).trim();
    let box = slot.onPop ? 'none' : (slot.box || o.box);
    if (!slot.box && slot.plate && (box === 'plain' || box === 'underline')) box = 'label';
    const padX = sw * .07, padY = sh * .14;
    const wt = o.hand ? 400 : (o.climax ? 900 : 800);
    const big = o.climax ? .16 : .11, fit = wrapFit(c, text, sw - 2 * padX, sh - 2 * padY, P.font, wt, H * big);
    const lines = fit.lines, fs = fit.fs, lh = fs * 1.22, th = lines.length * lh;
    c.font = wt + ' ' + Math.round(fs) + 'px ' + P.font;
    const tw = Math.max(...lines.map(l => c.measureText(l).width)), cx = sx + sw / 2, cy = sy + sh / 2;
    const tin = clamp((t - line.t0 + .25) / .35), tout = clamp((o.tEnd - t) / .25), a = Math.min(tin, tout), e = back(tin);
    if (a <= 0) return;
    const bw = tw + padX * 2, bh = th + padY * 1.3, bx = cx - bw / 2, by = cy - bh / 2;
    let ink = slot.onPop ? P.paper : P.ink, hot = slot.onPop ? P.ink : P.pop;
    c.save(); c.globalAlpha = tout < 1 ? tout : 1; c.translate(cx, cy); c.scale(.6 + .4 * e, .6 + .4 * e); c.rotate((slot.rot || 0) + (1 - e) * .03 * (o.side || 1)); c.translate(-cx, -cy);
    // 容器（第五十四轮：手绘套路里 label / bubble / underline 用 Rough.js 手绘线，笔迹 8 格/秒抖动）
    const RK = o.rough || 0, boil = Math.floor(t * 8) % 3, lw0 = Math.max(2.2, H / 240);
    let handDone = false;
    if (RK >= .5 && global.rough && (box === 'label' || box === 'bubble')) {
      const rad = box === 'bubble' ? bh * .45 : bh * .22;
      c.fillStyle = rgba(P.ink, .14); rr(c, bx + H * .008, by + H * .012, bw, bh, rad); c.fill();
      c.fillStyle = P.paper; rr(c, bx, by, bw, bh, rad); c.fill();
      if (box === 'bubble') { c.beginPath(); c.moveTo(bx + bw * .2, by + bh - 2); c.lineTo(bx + bw * .14, by + bh + bh * .22); c.lineTo(bx + bw * .32, by + bh - 2); c.fill(); }
      c.save(); c.translate(bx, by);
      roughDraw(c, roughShape('rr', bw, bh, rad, { stroke: box === 'bubble' ? P.pop : P.a, strokeWidth: lw0, roughness: .9 + RK * .9 }, boil));
      // 左上角一小块马克笔排线
      c.save(); c.beginPath(); rr(c, 0, 0, bw, bh, rad); c.clip(); roughDraw(c, roughShape('rect', bw * .16, bh * .34, 0, { stroke: 'rgba(0,0,0,0)', fill: rgba(P.pop, .55), strokeWidth: lw0, roughness: 1.6 }, boil)); c.restore();
      c.restore();
      if (box === 'bubble') { c.save(); c.strokeStyle = P.pop; c.lineWidth = lw0; c.lineCap = 'round'; c.beginPath(); c.moveTo(bx + bw * .2, by + bh + 1); c.lineTo(bx + bw * .14, by + bh + bh * .22); c.lineTo(bx + bw * .32, by + bh + 1); c.stroke(); c.restore(); }
      handDone = true;
    } else if (RK >= .5 && global.rough && box === 'underline') {
      const prog = clamp((t - line.t0) / Math.max(.4, (line.t1 || o.tEnd) - line.t0)); if (prog > .02) { c.save(); c.translate(cx - tw / 2, cy + th / 2 + H * .006);
        c.beginPath(); c.rect(-H * .01, -H * .03, (tw + H * .02) * prog, H * .06); c.clip();
        roughDraw(c, roughShape('rect', tw, H * .016, 0, { stroke: P.pop, fill: rgba(P.pop, .45), strokeWidth: lw0, roughness: 1.4 }, boil)); c.restore(); }
      handDone = true;
    }
    if (handDone) { }
    else if (box === 'label') { c.fillStyle = rgba(P.ink, .18); rr(c, bx + H * .008, by + H * .012, bw, bh, bh * .22); c.fill(); c.fillStyle = P.paper; rr(c, bx, by, bw, bh, bh * .22); c.fill(); c.strokeStyle = P.a; c.lineWidth = Math.max(2, H / 280); c.setLineDash([H * .014, H * .01]); rr(c, bx + H * .01, by + H * .01, bw - H * .02, bh - H * .02, bh * .18); c.stroke(); c.setLineDash([]); }
    else if (box === 'bubble') { c.fillStyle = P.paper; rr(c, bx, by, bw, bh, bh * .45); c.fill(); c.beginPath(); c.moveTo(bx + bw * .2, by + bh - 2); c.lineTo(bx + bw * .14, by + bh + bh * .22); c.lineTo(bx + bw * .32, by + bh - 2); c.fill(); c.strokeStyle = P.pop; c.lineWidth = Math.max(2, H / 260); rr(c, bx, by, bw, bh, bh * .45); c.stroke(); }
    else if (box === 'pill') { c.fillStyle = P.ink; rr(c, bx, by, bw, bh, bh * .5); c.fill(); ink = P.paper; hot = P.pop === P.ink ? P.b : P.pop; if (P.dark) { c.fillStyle = P.paper; rr(c, bx, by, bw, bh, bh * .5); c.fill(); ink = P.bg; hot = P.a; } }
    else if (box === 'window') { const bar = Math.min(bh * .26, H * .05); c.fillStyle = rgba(P.ink, .2); rr(c, bx + H * .01, by - bar + H * .014, bw, bh + bar, H * .018); c.fill(); c.fillStyle = P.paper; rr(c, bx, by - bar, bw, bh + bar, H * .018); c.fill(); c.fillStyle = P.a; rr(c, bx, by - bar, bw, bar * 1.3, H * .018); c.fill(); c.fillStyle = P.paper; c.fillRect(bx, by, bw, bar * .4); c.strokeStyle = P.ink; c.lineWidth = Math.max(2, H / 300); rr(c, bx, by - bar, bw, bh + bar, H * .018); c.stroke(); ink = P.dark ? P.bg : P.ink; }
    else if (box === 'tape') { c.save(); c.translate(cx, cy); c.rotate(-.025); c.fillStyle = P.pop; c.fillRect(-bw / 2, -bh / 2, bw, bh); c.fillStyle = rgba(P.paper, .35); c.fillRect(-bw / 2, -bh / 2, bw * .04, bh); c.fillRect(bw / 2 - bw * .04, -bh / 2, bw * .04, bh); c.restore(); ink = P.paper; hot = P.ink; }
    else if (box === 'menu') { c.fillStyle = P.paper; rr(c, bx, by, bw, bh, H * .012); c.fill(); c.strokeStyle = P.ink; c.lineWidth = Math.max(3, H / 200); c.stroke(); c.fillStyle = P.pop; c.beginPath(); c.moveTo(bx - H * .03, cy - H * .016); c.lineTo(bx - H * .008, cy); c.lineTo(bx - H * .03, cy + H * .016); c.fill(); ink = P.dark ? P.bg : P.ink; }
    else if (box === 'neonbox') { c.shadowColor = P.a; c.shadowBlur = H * .02; c.strokeStyle = P.a; c.lineWidth = Math.max(2, H / 260); rr(c, bx, by, bw, bh, H * .02); c.stroke(); c.shadowBlur = 0; c.fillStyle = rgba(P.bg, .7); rr(c, bx, by, bw, bh, H * .02); c.fill(); }
    else if (box === 'underline') { c.strokeStyle = P.pop; c.lineWidth = Math.max(2, H / 240); c.beginPath(); c.moveTo(cx - tw / 2, cy + th / 2 + H * .012); c.lineTo(cx - tw / 2 + tw * clamp((t - line.t0) / Math.max(.4, (line.t1 || o.tEnd) - line.t0)), cy + th / 2 + H * .012); c.stroke(); }
    // 字色：按字实际落在的底色算（容器色 / 场景色带 / 场景底）
    const under = box === 'label' || box === 'bubble' || box === 'window' || box === 'menu' ? P.paper : box === 'pill' ? (P.dark ? P.paper : P.ink) : box === 'tape' ? P.pop : box === 'neonbox' ? P.bg : (slot.onPop ? P.pop : (slot.bg || P.bg));
    const IK = inkOn(under, P); ink = IK.ink; hot = IK.hot;
    // 逐字（唱到的变色，带一点弹）
    c.textBaseline = 'middle'; c.textAlign = 'left'; c.lineJoin = 'round';
    let k = 0; const sung = (line.chars || []).filter(ch => t >= ch[1]).length, noBox = box === 'plain' || box === 'underline' || box === 'none' && !slot.onPop;
    lines.forEach((ln, r) => { const lw2 = c.measureText(ln).width; let x = cx - lw2 / 2; const y = cy - th / 2 + lh * (r + .5);
      for (const ch of [...ln]) { const cw = c.measureText(ch).width; if (!/\s/.test(ch)) { const q = clamp((t - line.t0 + .25) * 9 - k * .5), isSung = k < sung, justT = (line.chars && line.chars[k]) ? t - line.chars[k][1] : 9, pop = justT >= 0 && justT < .22 ? Math.sin(justT / .22 * Math.PI) : 0;
          if (q > 0) { const yy = y - (1 - eo(q)) * fs * .35 - pop * fs * .1; c.globalAlpha = (tout < 1 ? tout : 1) * clamp(q * 3);
            if (noBox) { c.strokeStyle = under; c.lineWidth = fs * .22; c.strokeText(ch, x, yy); }
            c.fillStyle = isSung ? hot : ink; c.fillText(ch, x, yy); } k++; } x += cw; } });
    c.restore();
    const bar0 = box === 'window' ? Math.min(bh * .26, H * .05) : 0, tail = box === 'bubble' ? bh * .22 : 0;
    return { x: bx / W, y: (by - bar0) / H, w: bw / W, h: (bh + bar0 + tail) / H, fs: fs / H, box };
  }

  /* ============================== 分镜：场景表 ============================== */
  function plan(show, seed, opts) {
    opts = opts || {};
    const r = k => h((seed >>> 0) * .00041 + k * 2.371);
    const preset = (show.style && show.style.preset) || 'pop';
    const lines = (show.lines || []).filter(l => l.text && String(l.text).trim()).slice().sort((a, b) => a.t0 - b.t0);
    const secs = ((show.music && show.music.sections) || []).slice().sort((a, b) => a.t0 - b.t0);
    const dur = +show.duration || (lines.length ? lines[lines.length - 1].t1 + 4 : 60);
    const beat = 60 / ((show.music && show.music.bpm) || 120), bar = beat * 4;
    const kitPool = KIT_BY_PRESET[preset] || KIT_BY_PRESET.pop, kitId = (show.style && show.style.kit) || kitPool[Math.floor(r(1) * kitPool.length)], kit = KITS[kitId];
    loadHandFonts();
    const fontOf = (KIT_FONT[kitId] ? KIT_FONT[kitId] + ',' : '') + (opts.font || '"Microsoft YaHei",sans-serif');
    const songMood = moodOf(lines, kit.moods[0]);
    // 1) 切场景：段落边界 + 伴奏空档（≥5 s）+ 太长的段每 4~6 小节一切；目标 8~20 s 一个
    const cuts = new Set([0]);
    secs.forEach(s => { if (s.t0 > 1) cuts.add(+s.t0.toFixed(2)); });
    const gaps = []; let prev = 0;
    lines.forEach(l => { if (l.t0 - prev >= 5) { gaps.push([prev + (prev ? .4 : 0), l.t0 - .5]); } prev = Math.max(prev, (l.exit_at || l.t1 || l.t0)); });
    if (dur - prev >= 5) gaps.push([prev + .4, dur]);
    gaps.forEach(g => { cuts.add(+g[0].toFixed(2)); cuts.add(+g[1].toFixed(2)); });
    let cl = [...cuts].sort((a, b) => a - b); const out = [];
    for (let i = 0; i < cl.length; i++) { const a = cl[i], b = i + 1 < cl.length ? cl[i + 1] : dur; out.push(a); const len = b - a, target = Math.max(8, Math.min(16, bar * (4 + Math.floor(r(50 + i) * 3))));
      if (len > target * 1.6) { const n = Math.round(len / target); for (let k = 1; k < n; k++) { let tt = a + len * k / n; const near = lines.find(l => Math.abs(l.t0 - .3 - tt) < 2.5); if (near) tt = near.t0 - .3; out.push(+tt.toFixed(2)); } } }
    cl = [...new Set(out)].sort((a, b) => a - b).filter((v, i, arr) => !i || v - arr[i - 1] >= 3.5);
    // 2) 每个场景：伴奏还是唱段、意象配色、布景、图案、贴纸、字卡容器、人物安排
    const scenes = []; let lastStage = '', lastBreak = '', lastMood = '', figRun = 0;
    const secOf = t => { let s = secs[0]; for (const x of secs) if (x.t0 <= t + .1) s = x; return s ? s.id : 'verse'; };
    for (let i = 0; i < cl.length; i++) {
      const t0 = cl[i], t1 = i + 1 < cl.length ? cl[i + 1] : dur; if (t1 - t0 < 1.2) continue;
      const ls = lines.filter(l => l.t0 >= t0 - .35 && l.t0 < t1 - .35), sec = secOf(t0), climax = sec === 'chorus' || sec === 'final';
      const isBreak = !ls.length || gaps.some(g => g[0] <= t0 + .6 && g[1] >= t1 - .6);
      // 意象：这一场景的歌词说了什么；没有关键词就沿用整首的；相邻场景尽量不同色
      let mood = ls.length ? moodOf(ls, songMood) : (lastMood || songMood);
      if (mood === lastMood && r(70 + i) < .45) { const alt = kit.moods.filter(m => m !== mood); mood = alt[Math.floor(r(71 + i) * alt.length)] || mood; }
      const P = Object.assign({}, MOODS[mood] || MOODS.dream, { font: fontOf });
      const sc = { t0, t1, n: scenes.length, sec, climax, mood, P, lines: ls, side: r(80 + i) < .5 ? -1 : 1, seedI: Math.floor(r(81 + i) * 97),
        pat: kit.pat[Math.floor(r(82 + i) * kit.pat.length)], deco: kit.deco.slice().sort((a, b) => r(a.length * 3 + i) - r(b.length * 5 + i)),
        box: kit.box[Math.floor(r(83 + i) * kit.box.length)] };
      if (isBreak) { const pool = (BREAK_BY_KIT[kitId] || Object.keys(BREAKS)).filter(x => x !== lastBreak); sc.kind = 'break'; sc.brk = pool[Math.floor(r(84 + i) * pool.length)]; lastBreak = sc.brk; figRun = 0; }
      else {
        sc.kind = 'scene';
        const pool = kit.stages.filter(x => x !== lastStage); sc.stage = pool[Math.floor(r(85 + i) * pool.length)]; lastStage = sc.stage;
        sc.doodles = doodlesFor(ls.map(l => l.text).join(' ')).sort((a, b2) => r(a.length * 7 + i) - r(b2.length * 5 + i)).slice(0, 4);
        // 关键词（大字墙/贴纸字用）
        const kw = ls.map(l => (l.keywords || [])[0]).filter(Boolean)[0] || [...String(ls[0].text).replace(/\s/g, '')].slice(0, 3).join('');
        sc.word = kw;
        // 人物：大约一半的场景有人物，定格在一侧（不挡字位）；不连续三个
        const noFig = ['bigword', 'burststage', 'versus', 'collage'];
        const wantFig = !noFig.includes(sc.stage) && figRun < 2 && (r(86 + i) < (climax ? .75 : .62) || figRun === 0 && r(87 + i) < .3);
        if (wantFig) { sc.figure = true; figRun++; } else figRun = 0;
      }
      scenes.push(sc); lastMood = mood;
    }
    for (let i = 0; i < scenes.length - 1; i++) scenes[i].t1 = scenes[i + 1].t0;
    if (scenes.length) { scenes[0].t0 = 0; scenes[scenes.length - 1].t1 = Math.max(scenes[scenes.length - 1].t1, dur); }
    // 过场：相邻场景之间的切换方式（按 kit 性格）
    const WIPES = kit.move > .7 ? ['push', 'bars', 'iris', 'page', 'blinds'] : ['page', 'iris', 'push', 'fade'];
    scenes.forEach((s, i) => { s.wipe = i ? WIPES[Math.floor(r(90 + i) * WIPES.length)] : 'fade'; });
    return { scenes, kit, kitId, songMood, title: opts.title || '', bpm: (show.music && show.music.bpm) || 120, beat };
  }
  function sceneAt(pl, t) { const S = pl.scenes; let lo = 0, hi = S.length - 1, best = S[0] || null; while (lo <= hi) { const m = (lo + hi) >> 1; if (S[m].t0 <= t) { best = S[m]; lo = m + 1; } else hi = m - 1; } return best; }

  /* 人物定格构图：按场景的字位避开（字在左 → 人物在右）*/
  // 半身/特写时这个模型的头发和尾巴很宽（约占 0.5 屏宽），所以人物中心放到 .14/.86，让一部分出画；字位只用另一侧
  const FIG = { L: [{ id: 'bustL', sf: 1.5, cx: .14, headY: .2 }, { id: 'faceL', sf: 2.0, cx: .11, headY: .16 }, { id: 'fullL', sf: 1.08, cx: .14, headY: .1 }],
    R: [{ id: 'bustR', sf: 1.5, cx: .86, headY: .2 }, { id: 'faceR', sf: 2.0, cx: .89, headY: .16 }, { id: 'fullR', sf: 1.08, cx: .86, headY: .1 }] };

  function drawWipe(c, W, H, kind, k, P) {
    if (k <= 0 || k >= 1) return; const e = eio(k < .5 ? k * 2 : (1 - k) * 2), inn = k < .5; c.save();
    if (kind === 'push') { const w = W * e; c.fillStyle = P.pop; c.fillRect(inn ? 0 : W - w, 0, w, H); const w2 = W * Math.max(0, e - .15); c.fillStyle = P.a; c.fillRect(inn ? 0 : W - w2, 0, w2, H); const w3 = W * Math.max(0, e - .3); c.fillStyle = P.paper; c.fillRect(inn ? 0 : W - w3, 0, w3, H); }
    else if (kind === 'page') { c.translate(W / 2, H / 2); c.rotate(-.35); const L = Math.hypot(W, H), w = 2 * L * e; c.fillStyle = P.a; c.fillRect(inn ? -L : L - w, -L, w, 2 * L); c.fillStyle = P.pop; c.fillRect(inn ? -L + w - L * .06 : L - w, -L, L * .06, 2 * L); }
    else if (kind === 'iris') { const r = Math.hypot(W, H) * .6 * e; [P.pop, P.a, P.paper].forEach((col, i) => { c.fillStyle = col; c.beginPath(); c.arc(W / 2, H / 2, Math.max(0, r * (1 - i * .16)), 0, TAU); c.fill(); }); }
    else if (kind === 'bars') { const n = 7, bh = H / n; for (let i = 0; i < n; i++) { const kk = clamp(e * 1.5 - i * .07), w = W * kk; c.fillStyle = [P.pop, P.a, P.paper][i % 3]; c.fillRect(i % 2 ? W - w : 0, i * bh, w, bh + 1); } }
    else if (kind === 'blinds') { const n = 10, bw = W / n; for (let i = 0; i < n; i++) { c.fillStyle = i % 2 ? P.a : P.pop; c.fillRect(i * bw, 0, bw * clamp(e * 1.3 - i * .03) + 1, H); } }
    else { c.globalAlpha = e; c.fillStyle = P.paper; c.fillRect(0, 0, W, H); }
    c.restore();
  }

  /* ============================== 每帧 ============================== */
  function draw(c, W, H, t, pl, env) {
    c.globalAlpha = 1; c.globalCompositeOperation = 'source-over'; c.setLineDash([]); c.shadowBlur = 0; c.clearRect(0, 0, W, H);
    const s = sceneAt(pl, t); if (!s) return { figure: false, frame: null, scene: null };
    const P = s.P, k = clamp((t - s.t0) / Math.max(1, s.t1 - s.t0)), beat = env.pulse || 0, I = env.I == null ? .5 : env.I;
    if (!DOODLE.icons) loadDoodles();
    const o = { P, k, beat, I, pat: s.pat, deco: s.deco, side: s.side, seedI: s.seedI, word: s.word, bpm: pl.bpm, t0: s.t0, t1: s.t1, doodles: s.doodles };
    let slots = [{ x: .1, y: .3, w: .8, h: .4 }];
    // 有人物的场景：布景整体缩到 72% 宽并靠向人物的另一侧（背景先满铺一层同色，避免露边），人物站在让出来的那一侧
    const figShift = s.figure ? (s.side > 0 ? -1 : 1) : 0;     // s.side>0 → 人物在右 → 布景靠左
    if (s.kind === 'break') { (BREAKS[s.brk] || BREAKS.eqcity)(c, W, H, t, o); }
    else if (figShift) {
      c.fillStyle = P.bg2; c.fillRect(0, 0, W, H); pattern(c, W, H, s.pat, P.bg, .45, t, .1, .6);
      const sw = W * .7, sh = H * .7, ox = figShift < 0 ? W * .04 : W * .26, oy = H * .15;
      c.save(); c.fillStyle = rgba(P.ink, .16); rr(c, ox + H * .014, oy + H * .018, sw, sh, H * .03); c.fill();
      rr(c, ox, oy, sw, sh, H * .03); c.clip(); c.translate(ox, oy); c.scale(.7, .7);
      const sl0 = (STAGES[s.stage] || STAGES.window)(c, W, H, t, o) || slots; c.restore();
      c.strokeStyle = P.paper; c.lineWidth = H * .012; rr(c, ox, oy, sw, sh, H * .03); c.stroke();
      slots = sl0.map(sl => Object.assign({}, sl, { x: ox / W + sl.x * .7, y: oy / H + sl.y * .7, w: sl.w * .7, h: sl.h * .7 }));
    }
    else { slots = (STAGES[s.stage] || STAGES.window)(c, W, H, t, o) || slots; }
    c.globalAlpha = 1; c.setLineDash([]); c.shadowBlur = 0;
    drawPaper(c, W, H, P.dark ? .18 : .55);
    // 人物：定格在字位的另一侧；字位只用远离人物的那一半
    let frame = null, figSide = 0;
    if (s.figure) {
      figSide = s.side > 0 ? 1 : -1;      // 布景已经让到另一侧（见上），人物站这一侧
      // 字位跟着布景面板走（面板在人物另一侧的 70% 宽里）；人物占外侧约 26% 宽，面板内的字不会被压到
      slots = slots.map(sl => { const o2 = Object.assign({}, sl); const lo = figSide > 0 ? .05 : .28, hi = figSide > 0 ? .72 : .95;
        const x0 = Math.max(lo, o2.x), x1 = Math.min(hi, o2.x + o2.w); o2.x = x0; o2.w = Math.max(.2, x1 - x0); return o2; });
      const opts2 = FIG[figSide > 0 ? 'R' : 'L']; frame = opts2[s.seedI % opts2.length];
    }
    // 飘动的贴纸层（每个场景 3~5 个，绕着边缘，自己弹跳旋转；避开字位和人物）
    const nDeco = s.kind === 'break' ? 0 : Math.round(2 + pl.kit.move * 3);
    for (let i = 0; i < nDeco; i++) { const q = back(clamp(k * 9 - i * .5 - .3)); if (q <= 0) continue;
      const px = [.07, .93, .12, .88, .5][i % 5], py = [.14, .86, .84, .16, .93][i % 5]; if (frame) continue;
      const inSlot = slots.some(sl => px > sl.x - .04 && px < sl.x + sl.w + .04 && py > sl.y - .05 && py < sl.y + sl.h + .05); if (inSlot) continue;
      const x = W * px + Math.sin(t * (.8 + h(i + s.seedI)) + i) * W * .01, y = H * py - Math.abs(Math.sin(t * (1.6 + pl.kit.move) + i * 1.3)) * H * .025 * pl.kit.move;
      c.save(); c.translate(x, y); c.rotate((h(i * 3.3 + s.seedI) - .5) * .6 + Math.sin(t * 1.2 + i) * .1); c.scale(q * (1 + .08 * beat), q * (1 + .08 * beat));
      const dn = s.doodles && s.doodles[i % s.doodles.length];
      if (dn && i % 2 === 0 && DOODLE.icons) { const R = H * .055; c.fillStyle = rgba(P.ink, .16); c.beginPath(); c.arc(R * .08, R * .12, R, 0, TAU); c.fill(); c.fillStyle = P.paper; c.beginPath(); c.arc(0, 0, R, 0, TAU); c.fill();
        let ringDone = false; if (pl.kit.rough >= .5 && global.rough) { c.save(); c.translate(-R, -R); ringDone = roughDraw(c, roughShape('ellipse', R * 2, R * 2, 0, { stroke: P.a, strokeWidth: Math.max(2, H / 300), roughness: 1.3 }, Math.floor(t * 8) % 3)); c.restore(); }
        if (!ringDone) { c.strokeStyle = P.a; c.lineWidth = Math.max(2, H / 300); c.beginPath(); c.arc(0, 0, R, 0, TAU); c.stroke(); }
        doodle(c, dn, R * .62, P.dark ? P.bg : P.ink, null); }
      else (DECO[s.deco[i % s.deco.length]] || DECO.star4)(c, H * .05, P, t, DECO_WORDS[(i + s.seedI) % DECO_WORDS.length]);
      c.restore(); }
    c.globalAlpha = 1;
    return { figure: !!s.figure, frame, scene: s, slots, P, k, figSide };
  }
  /* 顶层：歌词 + 边框 + 过场（画在人物之上）*/
  function drawTop(c, W, H, t, pl, env, res) {
    c.globalAlpha = 1; c.setLineDash([]); c.shadowBlur = 0; c.clearRect(0, 0, W, H);
    const s = res && res.scene; if (!s) return;
    const P = s.P, safe = SAFE[pl.kit.border] || .06;
    // 当前唱的那句（一个场景里一句句换，不换画面）
    if (s.kind === 'scene') {
      let cur = null, idx = 0; s.lines.forEach((l, i) => { if (t >= l.t0 - .3) { cur = l; idx = i; } });
      if (cur) { const nx = s.lines[idx + 1], tEnd = nx ? nx.t0 - .3 : Math.min(s.t1, (cur.exit_at || cur.t1 || cur.t0 + 3) + .8);
        if (t <= tEnd) { const base = res.slots[idx % res.slots.length]; const sl = Object.assign({}, base);
          sl.x = Math.max(safe * .7, sl.x); sl.y = Math.max(safe * 1.2, sl.y); sl.w = Math.min(1 - safe * .7 - sl.x, sl.w); sl.h = Math.min(1 - safe * 1.2 - sl.y, sl.h);
          res.textRect = drawText(c, W, H, sl, cur, t, { P, box: s.box, climax: s.climax, side: s.side, tEnd, rough: pl.kit.rough, hand: !!KIT_FONT[pl.kitId] }) || null; res.line = cur; } }
    } else if (s.kind === 'break' && s.n === 0 && pl.title) {
      const a = clamp(Math.min((t - s.t0) / .6, (s.t1 - t) / .6)); c.globalAlpha = a; const txt = String(pl.title).replace(/[（(【\[].*$/, '').trim() || pl.title;
      const fit = wrapFit(c, txt, W * .7, H * .2, P.font, 900, H * .12); c.textAlign = 'center'; c.textBaseline = 'middle'; c.lineJoin = 'round';
      const bw = Math.max(...fit.lines.map(l => c.measureText(l).width)) + W * .06, bh = fit.lines.length * fit.fs * 1.25 + H * .05; c.fillStyle = P.paper; rr(c, W / 2 - bw / 2, H * .18 - bh / 2, bw, bh, H * .02); c.fill(); c.strokeStyle = P.pop; c.lineWidth = H * .008; c.stroke();
      fit.lines.forEach((l, i) => { c.fillStyle = P.dark ? P.bg : P.ink; c.fillText(l, W / 2, H * .18 + (i - (fit.lines.length - 1) / 2) * fit.fs * 1.22); }); c.globalAlpha = 1;
    }
    drawBorder(c, W, H, pl.kit.border, P, t, res.k);
    const wd = .26, i = pl.scenes.indexOf(s), nx = pl.scenes[i + 1];
    if (i > 0 && t - s.t0 < wd) drawWipe(c, W, H, s.wipe, .5 + (t - s.t0) / wd * .5, P);
    if (nx && s.t1 - t < wd) drawWipe(c, W, H, nx.wipe, (1 - (s.t1 - t) / wd) * .5, nx.P);
  }

  function audit(pl, W, H) {
    const cv = document.createElement('canvas'); cv.width = W; cv.height = H; const c = cv.getContext('2d');
    const safe = SAFE[pl.kit.border] || .06, out = { lines: 0, offscreen: 0, inBorder: 0, onFigure: 0, tiny: 0, scenes: pl.scenes.length, bad: [] };
    const lens = pl.scenes.map(x => x.t1 - x.t0).sort((a, b) => a - b); out.sceneMedian = +lens[lens.length >> 1].toFixed(1); out.sceneMin = +lens[0].toFixed(1); out.sceneMax = +lens[lens.length - 1].toFixed(1);
    out.linesPerScene = +(pl.scenes.reduce((a, x) => a + x.lines.length, 0) / Math.max(1, pl.scenes.filter(x => x.kind === 'scene').length)).toFixed(1);
    for (const sc of pl.scenes) for (const l of sc.lines) {
      const t = Math.min(sc.t1 - .1, l.t0 + .6), res = draw(c, W, H, t, pl, { pulse: 0, I: .5 }); drawTop(c, W, H, t, pl, { pulse: 0, I: .5 }, res);
      const r = res.textRect; out.lines++; if (!r) continue;
      const why = [];
      if (r.x < -.001 || r.y < -.001 || r.x + r.w > 1.001 || r.y + r.h > 1.001) { out.offscreen++; why.push('offscreen'); }
      else if (r.x < safe * .5 || r.y < safe * .8 || r.x + r.w > 1 - safe * .5 || r.y + r.h > 1 - safe * .8) { out.inBorder++; why.push('border'); }
      if (res.frame) { const fx0 = res.frame.cx < .5 ? 0 : .74, fx1 = res.frame.cx < .5 ? .26 : 1; if (r.x < fx1 && r.x + r.w > fx0) { out.onFigure++; why.push('figure'); } }
      if (r.fs < .034) { out.tiny++; why.push('tiny'); }
      if (why.length && out.bad.length < 8) out.bad.push(Math.round(l.t0) + 's ' + sc.stage + ' ' + why.join('+') + ' [' + [r.x, r.y, r.w, r.h].map(v => v.toFixed(2)).join(',') + ']');
    }
    return out;
  }

  /* ============================== 原舞台用：手书色块点缀（第五十三轮） ==============================
   * 原舞台里人物一直在中间，这里只在画面边缘画会动的色块/图案（斜色带、角落色块、几颗弹跳贴纸），不碰中间人物区，
   * 不画边框、不画字。按段落换一次配色（跟歌词意思），副歌更满、主歌更少。opacity 由调用方给（整体淡入淡出）。*/
  function accentPlan(show, seed) {
    const r = k => h((seed >>> 0) * .00073 + k * 1.913);
    const lines = (show.lines || []).filter(l => l.text && String(l.text).trim());
    const secs = ((show.music && show.music.sections) || []).slice().sort((a, b) => a.t0 - b.t0);
    const kinds = ['band', 'corner', 'stripes', 'dots', 'blocks'];
    return secs.map((s0, i) => {
      const ls = lines.filter(l => l.t0 >= s0.t0 && l.t0 < (secs[i + 1] ? secs[i + 1].t0 : 1e9));
      const mood = moodOf(ls.length ? ls : lines, 'dream');
      const climax = s0.id === 'chorus' || s0.id === 'final';
      return { t0: s0.t0, t1: secs[i + 1] ? secs[i + 1].t0 : (+show.duration || 1e9), id: s0.id, P: MOODS[mood] || MOODS.dream, mood, doodles: doodlesFor(ls.map(l => l.text).join(' ')),
        kind: kinds[Math.floor(r(i * 3 + 1) * kinds.length)], side: r(i * 3 + 2) < .5 ? -1 : 1, deco: ['heart', 'star4', 'star5', 'note', 'flower', 'ribbon'][Math.floor(r(i * 3 + 3) * 6)],
        amount: climax ? 1 : (s0.id === 'bridge' || s0.id === 'intro' || s0.id === 'outro' ? .35 : .6) };
    });
  }
  function drawAccent(c, W, H, t, plan, env) {
    c.globalAlpha = 1; c.clearRect(0, 0, W, H);
    let a = null; for (const x of plan) if (x.t0 <= t) a = x; if (!a) return;
    const P = a.P, k = clamp((t - a.t0) / .8), out = clamp((a.t1 - t) / .6), A = Math.min(k, out) * a.amount, beat = env.pulse || 0;
    if (A <= .01) return;
    c.save(); c.globalAlpha = A;
    const side = a.side, edgeX = side < 0 ? 0 : W;
    if (a.kind === 'band') {
      // 一条斜色带从画面一侧伸进来，上面图案滚动；只占外侧 22%
      c.save(); c.beginPath(); const w0 = W * .22 * eo(k);
      if (side < 0) { c.moveTo(0, 0); c.lineTo(w0 + H * .12, 0); c.lineTo(w0 - H * .12, H); c.lineTo(0, H); } else { c.moveTo(W, 0); c.lineTo(W - w0 - H * .12, 0); c.lineTo(W - w0 + H * .12, H); c.lineTo(W, H); }
      c.closePath(); c.fillStyle = P.a; c.fill(); c.clip(); pattern(c, W, H, 'stripe', rgba(P.paper, .35), 1, t, .06, 2); c.restore();
      c.strokeStyle = P.pop; c.lineWidth = H * .01; c.beginPath(); const w1 = W * .22 * eo(k) + H * .02 * side * -1;
      if (side < 0) { c.moveTo(w1 + H * .12, 0); c.lineTo(w1 - H * .12, H); } else { c.moveTo(W - w1 - H * .12, 0); c.lineTo(W - w1 + H * .12, H); } c.stroke();
    } else if (a.kind === 'corner') {
      // 两个对角的大色块（三角），图案在里面动
      [[0, 0, 1, 1], [W, H, -1, -1]].forEach(([x, y, sx, sy], i) => { const L = H * .42 * eo(k) * (1 + .04 * beat);
        c.save(); c.beginPath(); c.moveTo(x, y); c.lineTo(x + sx * L * 1.5, y); c.lineTo(x, y + sy * L); c.closePath(); c.fillStyle = i ? P.b : P.a; c.fill(); c.clip(); pattern(c, W, H, 'dots', P.paper, .55, t, .06, 1); c.restore(); });
    } else if (a.kind === 'stripes') {
      // 上下两条细长色带，里面的条纹往相反方向流
      [0, 1].forEach(i => { const y = i ? H * .9 : 0, hh = H * .1 * eo(k); c.save(); c.beginPath(); c.rect(0, i ? H - hh : 0, W, hh); c.fillStyle = i ? P.b : P.a; c.fill(); c.clip(); pattern(c, W, H, 'stripe', rgba(P.paper, .4), 1, t * (i ? -1 : 1), .05, 2.5); c.restore(); });
    } else if (a.kind === 'dots') {
      // 外侧一列大圆点，随拍子轮流放大
      const n = 6, x = side < 0 ? W * .07 : W * .93; for (let i = 0; i < n; i++) { const y = H * (i + .5) / n, q = back(clamp(k * 6 - i * .4)), bb = (Math.floor(t * (env.bpm || 120) / 60) % n === i) ? 1.25 : 1;
        c.fillStyle = [P.a, P.pop, P.b][i % 3]; c.beginPath(); c.arc(x, y, H * .045 * q * bb, 0, TAU); c.fill(); }
    } else {
      // 外侧几块错落的方块，各自轻轻旋转
      for (let i = 0; i < 5; i++) { const q = back(clamp(k * 6 - i * .35)); if (q <= 0) continue; const x = side < 0 ? W * (.03 + .1 * h(i * 3.1)) : W * (.97 - .1 * h(i * 3.1)), y = H * (.12 + .18 * i), sz = H * (.07 + .05 * h(i * 5.3));
        c.save(); c.translate(x, y); c.rotate(Math.sin(t * .8 + i) * .2 + i); c.scale(q, q); c.fillStyle = [P.a, P.b, P.pop][i % 3]; c.fillRect(-sz / 2, -sz / 2, sz, sz); c.restore(); }
    }
    // 两三颗贴纸，在色块那一侧弹跳
    for (let i = 0; i < 3; i++) { const q = back(clamp(k * 5 - .6 - i * .5)); if (q <= 0) continue; const x = side < 0 ? W * (.05 + .08 * i) : W * (.95 - .08 * i), y = H * (.25 + .25 * i) - Math.abs(Math.sin(t * 2 + i)) * H * .02;
      c.save(); c.translate(x, y); c.rotate(Math.sin(t * 1.3 + i) * .2); c.scale(q, q);
      if (!DOODLE.icons) loadDoodles();
      const dn = a.doodles && a.doodles[i % a.doodles.length];
      if (!(dn && DOODLE.icons && doodle(c, dn, H * .045, '#ffffff', rgba(P.a, .55)))) (DECO[a.deco] || DECO.heart)(c, H * .04, Object.assign({ font: '"Microsoft YaHei",sans-serif' }, P), t);
      c.restore(); }
    c.restore();
  }

  global.NekoStageDirector = { loadHandFonts, roughShape, drawPaper, HANDF, audit, accentPlan, drawAccent, loadDoodles, doodle, doodlesFor, plan, sceneAt, shotAt: sceneAt, draw, drawTop, KITS, KIT_BY_PRESET, MOODS, STAGES, BREAKS, DECO, moodOf };
})(window);
