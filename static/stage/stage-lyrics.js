/* =============================================================================
 * 「为她而唱」舞台运行时 · 歌词层
 * 读 show.json 的 lines（9 维组合 + 逐字时间轴），负责：
 *   · 每句字的排版（横排/竖排/叠放/弧形/巨型关键词/居中）
 *   · 出场动画（深处浮出/墨迹/擦除/逐字打出/坠落/聚合/砸入/故障）
 *   · 卡拉 OK 弹跳、长音拉伸、退场（沉入/散开/推走/溶解）
 *   · 底部字幕条（直播歌回风格兜底）
 * 所有函数都只由 (show, t) 决定；随机量全部来自 show.seed，排版后不再变化。
 * 依赖：window.NekoStageU
 * ========================================================================== */
(function (global) {
  'use strict';
  const U = global.NekoStageU;

  const DUR_BY = { unfold: .45, track: .7, lineup: .5, maskin: .22, settle: .55, rise: .8, ink: .85, slam: .36, scatter: .9, glitch: .5, type: .06, drop: .7, wipe: .45,
    // 第四十轮新增入场 20 种
    float: .9, fadeup: .7, zoomin: .6, zoomout: .7, spin: .7, flip: .6, swingin: .8, slideL: .55, slideR: .55, rollup: .65,
    wave: .8, bounce: .75, stretch: .55, blurin: .9, typewriter: .08, rainbow: .7, pop: .4, sway: .9, shutter: .5, cascade: .7 };
  // 入场按气质分组（director 给的 reveal 先映射成气质，再从组里按演出种子抽）
  const REVEAL_MOOD = {
    soft: ['rise', 'ink', 'float', 'fadeup', 'blurin', 'sway', 'zoomout', 'wipe', 'cascade'],
    bright: ['pop', 'bounce', 'zoomin', 'spin', 'flip', 'wave', 'rainbow', 'drop', 'stretch', 'swingin'],
    hard: ['slam', 'glitch', 'shutter', 'slideL', 'slideR', 'stretch', 'zoomin', 'rollup'],
    plain: ['type', 'typewriter', 'wipe', 'rollup', 'fadeup', 'slideL']
  };
  // 第五十轮：文字预设族（线系主题用）。每族 = 这一首里文字「说话的方式」：入场、退场各 2~3 种，整首只在族内换
  const TEXT_PRESET = {
    editorial: { in: ['lineup', 'track', 'maskin'], out: ['fadeout', 'slideoutL', 'wipeout'] },        // 杂志排版：从线后升起、字距收拢
    poster: { in: ['unfold', 'settle', 'track'], out: ['shrink', 'popout', 'wipeout'] },                 // 海报大字：展开、落位
    handwrite: { in: ['maskin', 'typewriter', 'lineup'], out: ['fadeout', 'dissolve', 'typeback'] },     // 手写：一笔一笔写出来
    vertical: { in: ['lineup', 'maskin', 'unfold'], out: ['fadeout', 'rolldown', 'floatup'] }            // 竖排：逐字落下
  };
  const MOOD_OF = { rise: 'soft', ink: 'soft', wipe: 'soft', drop: 'bright', scatter: 'bright', slam: 'hard', glitch: 'hard', type: 'plain' };
  // 退场 20 种新增（原有 sink / burst / push / 其它=放大糊掉）
  const EXIT_MOOD = {
    soft: ['sink', 'fadeout', 'blurout', 'floatup', 'dissolve', 'drift', 'shrink', 'evaporate'],
    bright: ['burst', 'popout', 'spinout', 'flipout', 'bounceout', 'confetti', 'zoomthru'],
    hard: ['push', 'slideoutL', 'slideoutR', 'glitchout', 'shatter', 'crush', 'zoomthru'],
    plain: ['fadeout', 'rolldown', 'wipeout', 'typeback', 'slideoutL']
  };

  // 第三十四轮：按实际字体测字宽（缓存），中文略紧排；测不了（离线沙箱）退回估计值
  let MEASURE_FONT = '', MEASURE_WT = 400;
  let _mc = null;
  try { const cv = document.createElement('canvas'); _mc = cv && cv.getContext ? cv.getContext('2d') : null; } catch (e) { _mc = null; }
  const _mcache = new Map();
  function charW(c, fs) {
    const key = MEASURE_WT + '|' + MEASURE_FONT + '|' + c;
    let w = _mcache.get(key);
    if (w == null) {
      w = 0;
      if (_mc && MEASURE_FONT) {
        try { _mc.font = MEASURE_WT + ' 100px ' + MEASURE_FONT; w = _mc.measureText(c).width / 100; } catch (e) { w = 0; }
      }
      const ascii = /[\x00-\x7f]/.test(c);
      if (!(w > 0)) w = ascii ? .58 : 1;
      if (!ascii) w *= .94;               // 中日文字身自带留白，紧排 6%
      else if (c === ' ') w = Math.max(w, .26);
      _mcache.set(key, w);
    }
    return w * fs;
  }

  function create() {
    let show = null, prep = null, cfg = null, els = null;
    let W = 1280, H = 720;
    let anchors = [];
    const st = {};   // 复用对象，避免每帧分配

    /* ------------------------------ 准备 ------------------------------ */
    function build(showArg, elsArg, cfgArg) {
      show = showArg; els = elsArg; cfg = cfgArg;
      const beat = 60 / show.music.bpm;
      const seed = show.seed >>> 0;
      const leadBy = {
        rise: Math.max(.9, beat * 2), ink: beat * 2, wipe: beat * 1.5, type: .3,
        drop: beat * 1.5, scatter: beat * 2, slam: beat, glitch: beat * 1.2,
        float: beat * 2, fadeup: beat * 1.5, zoomin: beat, zoomout: beat * 1.5, spin: beat * 1.5, flip: beat * 1.2,
        swingin: beat * 1.6, slideL: beat * 1.2, slideR: beat * 1.2, rollup: beat * 1.3, wave: beat * 1.8, bounce: beat * 1.5,
        stretch: beat, blurin: beat * 2, typewriter: .3, rainbow: beat * 1.5, pop: beat, sway: beat * 2, shutter: beat, cascade: beat * 1.8
      };
      // 第四十轮：每次演出重抽入场/退场（同一句的气质不变，具体效果变）
      const runR = k => U.h01(seed * .00071 + k * 2.113);
      const byId = {};
      for (const s of show.music.sections) byId[s.id] = s;
      const clears = ['chorus', 'bridge', 'final', 'outro']
        .map(id => (byId[id] ? byId[id].t0 : null)).filter(v => v != null).sort((a, b) => a - b);

      prep = show.lines.map(L => {
        const kind = L.i === -1 ? 'title' : (L.i <= -2 ? 'flash' : 'line');
        const c = L.lyric || {};
        const combo = {
          reveal: c.reveal || 'rise', stagger: c.stagger || 'char', layout: c.layout || 'h',
          material: c.material || 'solid', exit: c.exit || 'sink', echo: !!c.echo,
          front: !!c.front, boost: c.boost || 1, slow: kind !== 'line' || L.section === 'bridge',
          repeat_of: c.repeat_of
        };
        const words = String(L.text).split(' ').filter(Boolean);
        const chars = [];
        words.forEach((w, wi) => {
          [...w].forEach((ch, ci) => {
            chars.push({ c: ch, wi, wEnd: ci === [...w].length - 1 });
          });
        });
        // 逐字时间、重音、关键词严格按 show.json 的 chars 顺序对齐
        // 逐字时间表可能不含标点（「,」「、」等）：按字本身对齐，标点跟前一个字走，避免后面的字整体错位、句尾字提前亮
        const src = L.chars || [];
        let sj = 0, prevX = null;
        const timing = chars.map(ch => {
          const want = String(ch.c).toLowerCase();
          let k = sj;
          while (k < src.length && k < sj + 3 && String(src[k][0]).toLowerCase() !== want) k++;
          if (k < src.length && String(src[k][0]).toLowerCase() === want) { sj = k + 1; prevX = src[k]; return src[k]; }
          if (/[\p{P}\p{S}\s]/u.test(ch.c)) return prevX ? [ch.c, +prevX[1], +prevX[2], 0, 0] : [ch.c, L.t0, L.t0, 0, 0];
          const x = src[sj] || null; if (x) { sj++; prevX = x; }
          return x || [ch.c, L.t0, L.t1, 0, 0];
        });
        chars.forEach((ch, i) => {
          const x = timing[i];
          ch.cs = +x[1]; ch.ce = +x[2]; ch.stress = +x[3] || 0; ch.kw = !!+x[4];
          ch.long = i === chars.length - 1 && (ch.ce - ch.cs) > .55;
          ch.on = false; ch.glc = false;
        });
        const n = chars.length;
        if (kind === 'line') {
          const mood = MOOD_OF[combo.reveal] || 'soft';
          const ARTP = global.__NEKO_ART, tp = ARTP && ARTP.textAt ? TEXT_PRESET[ARTP.textAt(L.t0)] : null;
          const pool = tp ? tp.in : REVEAL_MOOD[mood];
          combo.reveal = pool[Math.floor(runR(L.i * 3 + 1) * pool.length)] || combo.reveal;
          if (tp) { combo.exit = tp.out[Math.floor(runR(L.i * 3 + 2) * tp.out.length)]; combo._preset = true; }
          const emood = combo.exit === 'burst' ? 'bright' : combo.exit === 'push' ? 'hard' : combo.exit === 'sink' ? 'soft' : mood;
          const ep = EXIT_MOOD[emood] || EXIT_MOOD.soft;
          if (!combo._preset) combo.exit = ep[Math.floor(runR(L.i * 3 + 2) * ep.length)] || combo.exit;
        }
        const reveal = kind === 'title' ? 'rise' : combo.reveal;
        const lead = (leadBy[reveal] || 1) * (combo.slow ? 1.6 : 1);
        const appear = L.t0 - lead;
        const dur = DUR_BY[reveal] || .6;
        const stagger = (reveal === 'type' || reveal === 'typewriter') ? 'cue' : (reveal === 'cascade' || reveal === 'wave' ? 'char' : combo.stagger);
        const spread = Math.max(.05, lead - dur * .5);
        chars.forEach((ch, i) => {
          let k = 0;
          if (stagger === 'char') k = i / Math.max(1, n - 1);
          else if (stagger === 'word') k = ch.wi / Math.max(1, words.length - 1);
          else if (stagger === 'center') k = Math.abs(i - (n - 1) / 2) / Math.max(.5, (n - 1) / 2);
          else if (stagger === 'random') k = U.h01(seed * .01 + L.i * 31 + i * 13.7);
          if (kind === 'title') { ch.es = ch.cs - .5; ch.ed = 1.3; }
          else if (stagger === 'cue') { ch.es = ch.cs - dur * .9 - .03; ch.ed = dur; }
          else if (reveal === 'wipe') { ch.es = appear + (i / n) * lead * .85; ch.ed = .35; }
          else { ch.es = appear + k * spread; ch.ed = dur; }
          ch.scat = [(U.h01(L.i * 7 + i * 3.3 + seed) - .5) * .7, (U.h01(L.i * 11 + i * 5.1 + seed) - .5) * .6, (U.h01(i * 9.9 + L.i) - .5) * 120];
          ch.jit = [(U.h01(i * 4.4 + L.i * 2.2 + seed) - .5) * 5, (U.h01(i * 2.7 + L.i * 6.1) - .5) * .08];
        });

        const exitAt = L.exit_at != null ? +L.exit_at : (chars.length ? chars[n - 1].ce + .15 : L.t1);
        const vis0 = chars.length ? Math.min(...chars.map(ch => ch.es)) - .05 : L.t0;
        let fadeAt = null, killAt = null, gone;
        if (combo.exit === 'sink') {
          const linger = 3 + U.h01(seed * .013 + L.i * 17.31 + 5) * 4;
          fadeAt = exitAt + linger;
          const nextClear = clears.find(t => t > exitAt + .1);
          killAt = nextClear && nextClear < fadeAt ? nextClear : null;
          gone = killAt ? killAt + .8 : fadeAt + 1.3;
        } else gone = exitAt + .9;

        const kw = L.keywords && L.keywords.length ? L.keywords[0] : null;
        const role = c.font_role || 'primary';
        const fam = (role === 'accent' && cfg.accentFont) ? cfg.accentFont : cfg.font;
        return {
          font: fam, fontRole: role,
          raw: L, i: L.i, text: L.text, section: L.section, t0: +L.t0, t1: +L.t1, exitAt, vis0, gone,
          fadeAt, killAt, chars, words, nw: words.length, kw, kind, combo, reveal, stagger,
          intensity: +L.intensity || 0, emotion: L.emotion, gaze: L.gaze,
          pos: (L.lyric && L.lyric.pos) || null, dur
        };
      });

      // DOM：每句一个 .ns-line，前后两层由 combo.front 决定
      els.back.innerHTML = '';
      els.front.innerHTML = '';
      // 第四十二轮：歌词打底框画布（在后排歌词之下）
      plateCv = document.createElement('canvas');
      plateCv.className = 'ns-plates';
      plateCv.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none;';
      els.back.appendChild(plateCv);
      plateCtx = plateCv.getContext('2d');
      {
        const FX = global.NekoStageFrames;
        const kinds = FX ? FX.PLATES : [];
        const pal = (show.style && show.style.palette) || {};
        const cols = [pal.hot || '#ff6fae', pal.c1 || '#7fd8ff', pal.c2 || '#ffe066'];
        // 每首歌（每次演出）挑 4~6 种底做「这首的打底家族」，句子在家族里轮换，整体统一不杂乱
        const fam = kinds.slice().sort((a, b) => U.h01(seed * .00091 + a.length * 3.1 + a.charCodeAt(0)) - U.h01(seed * .00091 + b.length * 3.1 + b.charCodeAt(0))).slice(0, 4 + Math.floor(U.h01(seed * .0013) * 3));
        // 第四十四轮：文字底框严格少用。规则：
        //   ① 主题不允许（大部分主题）→ 一句都不加，层次靠字体/描边/阴影/大小；
        //   ② 主题允许（如粉彩贴纸风）→ 只给「每次进副歌的第一句」加，整首最多 3 句，且整首只用同一种底；
        //   ③ 两行以上的长句、特写段不加（底框会挡人物、显得挤）。
        const ART = global.__NEKO_ART;
        const SOFT_PLATES = ['underline', 'highlighter', 'tape', 'bracket'];   // 允许时也只用最克制的几种
        const plateKind = SOFT_PLATES[Math.floor(U.h01(seed * .0023) * SOFT_PLATES.length)];
        let used = 0, lastSec = '';
        prep.forEach(L => {
          L.plate = null;
          if (L.kind !== 'line' || !kinds.length) { lastSec = L.section || lastSec; return; }
          const deco = ART ? ART.themeOf(L.section).deco : { plates: false };
          const firstOfChorus = (L.section === 'chorus' || L.section === 'final') && L.section !== lastSec;
          lastSec = L.section;
          if (!deco.plates || !firstOfChorus || used >= 3 || L.chars.length > 14) return;
          L.plate = plateKind; L.plateCols = [cols[0], cols[1], cols[2]]; used++;
        });
      }
      for (const L of prep) {
        const g = document.createElement('div');
        g.className = 'ns-line';
        (L.combo.front ? els.front : els.back).appendChild(g);
        L.g = g;
        L.chars.forEach(ch => {
          const mk = ghost => {
            const e = document.createElement('span');
            e.className = 'ns-ch m-' + L.combo.material + (ch.kw ? ' kw' : '') + (ghost ? ' ghost' : '');
            e.textContent = ch.c;
            e.style.fontFamily = L.font;
            e.style.fontWeight = cfg.weight;
            g.appendChild(e);
            return e;
          };
          ch.ghosts = L.combo.echo ? [mk(true), mk(true)] : [];
          ch.el = mk(false);
        });
      }
      anchors = [];
    }

    function shotAt(t) {
      const S = show.camera;
      let s = S[0];
      for (const x of S) { if (x.t <= t + 1e-6) s = x; else break; }
      return s;
    }

    /* ------------------------------ 排版 ------------------------------ */
    function layout(w, h) {
      W = w; H = h;
      for (const L of prep) layoutLine(L);
      buildAnchors();
    }

    function layoutLine(L) {
      MEASURE_FONT = L.font || cfg.font || ''; MEASURE_WT = cfg.weight || 400;
      const c = L.combo, n = L.chars.length;
      const rng = U.mulberry32((U.hstr(L.text) ^ (show.seed >>> 0) ^ (L.i * 977)) >>> 0);
      let fs = H * cfg.fsBase * (.85 + .35 * L.intensity) * c.boost;
      const sh = shotAt(L.t0 + .01);
      // 先按演示页的顺序消耗 rng（保证 stack 等分支的随机量与原型一致），再用 show.json 里的锚点
      const ry0 = .22 + rng() * .45;
      let rx0;
      if (sh.cx < .45) rx0 = .62 + rng() * .2;
      else if (sh.cx > .55) rx0 = .18 + rng() * .2;
      else rx0 = rng() < .5 ? .16 + rng() * .1 : .74 + rng() * .1;
      let rx = L.pos ? L.pos[0] : rx0;
      let ry = L.pos ? L.pos[1] : ry0;
      if (L.kind === 'flash') fs = H * .06;

      const ws = L.chars.map(ch => charW(ch.c, fs));
      const put = (ch, x, y, f, rot) => { ch.x = x; ch.y = y; ch.fs = f; ch.rot = rot; };
      // typeface.rules：vertical_ok=false 时竖排退回横排
      let lay = (c.layout === 'v' && cfg.verticalOk === false) ? 'h' : c.layout;
      // 第三十四轮：巨型字只给短关键词（≤4 字）；没有关键词就退回横排（以前会把整句都放成巨型，铺满全屏）
      if (lay === 'giant') {
        const kw0 = L.kw || '';
        if (!kw0 || [...kw0].length > 4 || L.words.join('').indexOf(kw0) < 0) lay = 'h';
      }
      // 居中排版正好压在人物身上（人物站在画面中间）：只给标题和很短的句子（≤6 字），其余改横排放到一侧
      if (lay === 'center' && L.kind !== 'title' && n > 6) lay = 'h';
      // 弧形长句会横跨整屏：超过 10 字改横排
      if (lay === 'arc' && n > 10) lay = 'h';

      if (lay === 'center' || L.kind === 'title') {
        fs = H * (L.kind === 'title' || c.big ? .11 : .058);
        const tot = L.chars.reduce((a, ch) => a + charW(ch.c, fs), 0);
        let x = W * .5 - tot / 2;
        const y = H * (L.kind === 'title' ? .4 : .2);
        L.chars.forEach(ch => { const w = charW(ch.c, fs); put(ch, x + w / 2, y, fs * (ch.kw ? cfg.kwScale : 1), 0); x += w; });
      } else if (lay === 'giant') {
        let kw = L.kw;
        const flat = L.words.join('');
        if (!kw) kw = L.words[L.words.length - 1] || '';
        const at = flat.indexOf(kw);
        let gfs = H * (L.combo.front ? .26 : .24);
        const k = kw.length;
        let gw = Array.from({ length: k }, (_, j) => charW(flat[at + j] || '', gfs) * .98);
        let gtot = gw.reduce((a, b) => a + b, 0);
        if (gtot > W * .42) { const f = W * .42 / gtot; gfs *= f; gw = gw.map(w => w * f); gtot = W * .42; }
        let gx = U.clamp(rx * W - gtot / 2, W * .04, W * .96 - gtot);
        const xs = gw.map(w => { const cx = (gx + w / 2) / W; gx += w; return cx; });
        const rfs = H * .045;
        const rest = L.chars.filter((ch, i) => i < at || i >= at + k);
        const rtot = rest.reduce((a, ch) => a + charW(ch.c, rfs), 0);
        let rxp = W * .5 - rtot / 2;
        L.chars.forEach((ch, i) => {
          if (i >= at && i < at + k) {
            put(ch, W * xs[i - at], H * (.42 + (U.h01(i + L.i) - .5) * .05), gfs * (/[\x00-\x7f]/.test(ch.c) ? .8 : 1), (U.h01(i * 3 + L.i) - .5) * 5);
            ch.giant = true;
          } else {
            const w = charW(ch.c, rfs);
            put(ch, rxp + w / 2, H * .08, rfs, 0);
            rxp += w;
          }
        });
      } else if (lay === 'v') {
        const step = fs * 1.06;
        let perCol = n > 7 ? Math.ceil(n / 2) : n;
        perCol = Math.max(1, Math.min(perCol, cfg.maxChars || perCol));
        perCol = Math.max(1, Math.min(perCol, Math.floor(H * .84 / step)));
        const colH = perCol * step;
        const y0 = U.clamp(ry * H - colH / 2, H * .08, H * .92 - colH);
        L.chars.forEach((ch, i) => {
          const col = Math.floor(i / perCol), r = i % perCol;
          put(ch, rx * W - col * fs * 1.35 + (rx < .5 ? fs * 1.35 * (perCol < n ? 1 : 0) : 0), y0 + r * step + step / 2,
            fs * (ch.kw ? cfg.kwScale : 1), ch.jit[0] * .3);
        });
      } else if (lay === 'stack') {
        let words = L.words.slice();
        if (words.length === 1 && n > 4) {
          const cut = Math.ceil(n / 2), f = words[0];
          words = [f.slice(0, cut), f.slice(cut)];
        }
        const sizes = words.map(w => (L.kw && L.kw.includes(w)) || (L.kw && w.includes(L.kw)) ? 1.5 : .75 + rng() * .6);
        const maxW = Math.max(...words.map((w, j) => [...w].reduce((a, ch) => a + charW(ch, fs * sizes[j]), 0)));
        let y = ry * H - words.reduce((a, w, j) => a + fs * sizes[j] * 1.1, 0) / 2;
        let ci = 0;
        words.forEach((w, j) => {
          const f = fs * sizes[j];
          let x = U.clamp(rx * W - maxW / 2, W * .03, W * .97 - maxW) + (rng() - .3) * fs * .8;
          [...w].forEach(c2 => {
            const ch = L.chars[ci++]; if (!ch) return;
            const cw = charW(c2, f);
            put(ch, x + cw / 2, y + f * .55, f * (ch.kw ? cfg.kwScale : 1), ch.jit[0] * .5);
            x += cw;
          });
          y += f * 1.1;
        });
      } else if (lay === 'arc') {
        const tot = ws.reduce((a, w) => a + w, 0);
        const Rr = W * .45, span = tot / Rr;
        let a = -span / 2;
        const cx = rx * W, cy = ry * H + Rr;
        L.chars.forEach((ch, i) => {
          const w = ws[i];
          const aa = a + w / 2 / Rr;
          put(ch, cx + Math.sin(aa) * Rr, cy - Math.cos(aa) * Rr, fs * (ch.kw ? cfg.kwScale : 1), aa * 57.3);
          a += w / Rr;
        });
      } else {
        // 横排：超过 max_chars_per_row 就分行（每行各自缩放到 maxW 内）
        const maxW = W * .4, M = cfg.maxChars || 40;
        const rows = [];
        // 先按「一行放不下就折成两行」：从中间附近的词边界（空格/标点）断开
        const totW = L.chars.reduce((a, ch) => a + charW(ch.c, fs), 0);
        if (n > M) { for (let i = 0; i < n; i += M) rows.push(L.chars.slice(i, i + M)); }
        else if (totW > maxW * 1.15 && n >= 6) {
          let cut = Math.round(n / 2), bestD = 1e9;
          L.chars.forEach((ch, i) => { if (i > 1 && i < n - 1 && (ch.wEnd || /[、，,。！!？?　\s]/.test(ch.c)) && Math.abs(i + 1 - n / 2) < bestD) { bestD = Math.abs(i + 1 - n / 2); cut = i + 1; } });
          rows.push(L.chars.slice(0, cut), L.chars.slice(cut));
        } else rows.push(L.chars);
        const sized = rows.map(row => {
          let tot = row.reduce((a, ch) => a + charW(ch.c, fs), 0);
          let f = fs;
          if (tot > maxW) { f = fs * maxW / tot; tot = maxW; }
          return { f, tot };
        });
        const gap = sized[0].f * 1.28;
        const totalH = rows.length * gap;
        const y0 = rows.length > 1
          ? U.clamp(ry * H - totalH / 2 + gap / 2, H * .12, H * .9 - totalH + gap)
          : ry * H;
        rows.forEach((row, ri) => {
          const { f, tot } = sized[ri];
          let x = U.clamp(rx * W - tot / 2, W * .03, W * .97 - tot);
          const y = y0 + ri * gap;
          row.forEach(ch => {
            const w = charW(ch.c, f);
            put(ch, x + w / 2, y, f * (ch.kw ? cfg.kwScale : 1), ch.jit[0]);
            x += w;
          });
        });
      }
      // 第三十四轮：整句兜底——超出 0.44W 宽 / 0.5H 高就等比缩小；整体夹回屏幕上方 80% 内
      {
        let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
        for (const ch of L.chars) { const hw = charW(ch.c, ch.fs) / 2, hh = ch.fs * .55; x0 = Math.min(x0, ch.x - hw); x1 = Math.max(x1, ch.x + hw); y0 = Math.min(y0, ch.y - hh); y1 = Math.max(y1, ch.y + hh); }
        const bw = x1 - x0, bh = y1 - y0;
        const f = Math.min(1, (W * .44) / Math.max(1, bw), (H * .5) / Math.max(1, bh));
        const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
        if (f < 1) for (const ch of L.chars) { ch.x = cx + (ch.x - cx) * f; ch.y = cy + (ch.y - cy) * f; ch.fs *= f; }
        const nw = bw * f, nh = bh * f;
        const ncx = U.clamp(cx, W * .03 + nw / 2, W * .97 - nw / 2), ncy = U.clamp(cy, H * .05 + nh / 2, H * .9 - nh / 2);
        if (ncx !== cx || ncy !== cy) for (const ch of L.chars) { ch.x += ncx - cx; ch.y += ncy - cy; }
      }
      L.chars.forEach(ch => {
        ch.el.style.fontSize = ch.fs.toFixed(1) + 'px';
        for (const g of ch.ghosts) g.style.fontSize = ch.fs.toFixed(1) + 'px';
      });
      L.cx = L.chars.reduce((a, ch) => a + ch.x, 0) / Math.max(1, n);
      L.cy = L.chars.reduce((a, ch) => a + ch.y, 0) / Math.max(1, n);
    }

    // 意象事件按时间找到对应的字（show.json 的事件只带时间与意象名）
    function buildAnchors() {
      anchors = [];
      if (!show.events) return;
      for (const e of show.events) {
        if (e.type !== 'imagery' && e.type !== 'img') continue;
        const img = e.arg || e.img;
        let hit = null;
        for (const L of prep) {
          if (L.kind !== 'line') continue;
          for (const ch of L.chars) {
            if (ch.kw && Math.abs(ch.cs - e.t) < .04) { hit = ch; break; }
          }
          if (hit) break;
        }
        if (hit) anchors.push({ t: e.t, img, x: hit.x, y: hit.y });
        else anchors.push({ t: e.t, img, x: W * .5, y: H * .42 });
      }
      anchors.sort((a, b) => a.t - b.t);
    }

    /* ------------------------------ 每帧渲染 ------------------------------ */
    function charState(L, ch, i, t, out) {
      let x = ch.x, y = ch.y, s = 1, rot = ch.rot, op = 1, blur = 0, br = 1, glitch = false;
      const p = U.clamp((t - ch.es) / ch.ed);
      if (t < ch.es) { out.op = 0; return out; }
      switch (L.kind === 'title' ? 'rise' : L.combo.reveal) {
        case 'rise': { const z = 1 - U.easeOut(p); s *= U.lerp(1, .45, z); blur = 18 * z; op = U.sstep(0, .55, p); y += H * .04 * z; break; }
        // 第五十轮：线系文字预设（不模糊、不发光，只有位移/缩放/字距——干净）
        case 'unfold': { const q = U.easeOut(p); out.sy = Math.max(.02, q); op = p > 0 ? 1 : 0; break; }                       // 竖向从一条线展开成字
        case 'track': { const q = 1 - U.easeOut(p); x += (ch.x - L.cx) * 1.4 * q; op = U.easeOut(p); break; }                   // 字距从很开收拢到位
        case 'lineup': { const q = 1 - U.easeOut(p); y += ch.fs * .9 * q; op = U.clamp(p * 3); out.clipB = q; break; }          // 从基线下面升上来（像从线后面冒出）
        case 'maskin': { op = U.clamp(p * 6); x -= ch.fs * .25 * (1 - U.easeOut(p)); break; }                                    // 逐字干脆地亮起，带一点左移
        case 'settle': { const q = 1 - U.easeOutBack(p); y -= ch.fs * .5 * q; rot += (i % 2 ? 6 : -6) * q; op = U.clamp(p * 4); break; }   // 轻轻落位，带一点回弹
        case 'ink': { const q = Math.pow(1 - p, 1.5); blur = 22 * q; s *= 1 + .4 * q; op = Math.pow(p, .7); x += (ch.x - L.cx) * .25 * q; break; }
        case 'slam': { s *= p < 1 ? U.lerp(3.4, 1, U.easeOutBack(p)) : 1; op = U.clamp(p * 5); blur = 7 * (1 - p); break; }
        case 'scatter': { const q = 1 - U.springP(p); x += ch.scat[0] * W * .35 * q; y += ch.scat[1] * H * .35 * q; rot += ch.scat[2] * .5 * q; op = U.clamp(p * 2); blur = 6 * q; break; }
        case 'glitch': {
          if (p < 1) {
            const on = U.h01(Math.floor(t * 28) * 1.3 + i * 7.1 + L.i) < .25 + .75 * p;
            op = on ? 1 : 0;
            x += (U.h01(Math.floor(t * 30) + i) - .5) * 26 * (1 - p);
            glitch = true;
          }
          break;
        }
        case 'type': { op = p > 0 ? 1 : 0; br = 1 + 1.5 * (1 - U.clamp((t - ch.es) / .25)); break; }
        case 'drop': { y -= H * .12 * (1 - U.bounceOut(p)); op = U.clamp(p * 3); break; }
        case 'wipe': { op = U.easeOut(p); x -= 40 * (1 - U.easeOut(p)); s *= U.lerp(.9, 1, p); blur = 4 * (1 - p); break; }
        // —— 第四十轮新增 20 种 ——
        case 'float': { const q = 1 - U.easeOut(p); y += H * .06 * q; op = U.sstep(0, .7, p); blur = 8 * q; x += Math.sin(i * 1.7 + t * 2) * 6 * q; break; }
        case 'fadeup': { const q = 1 - U.easeOut(p); y += H * .025 * q; op = U.easeOut(p); break; }
        case 'zoomin': { s *= U.lerp(.2, 1, U.easeOutBack(p)); op = U.clamp(p * 3); blur = 5 * (1 - p); break; }
        case 'zoomout': { s *= U.lerp(2.2, 1, U.easeOut(p)); op = U.clamp(p * 1.6); blur = 14 * (1 - p); break; }
        case 'spin': { rot += 360 * (1 - U.easeOut(p)); s *= U.lerp(.3, 1, U.easeOut(p)); op = U.clamp(p * 2.5); break; }
        case 'flip': { const q = U.easeOutBack(p); s *= Math.max(.02, Math.abs(Math.cos((1 - U.clamp(q)) * Math.PI / 2))); op = U.clamp(p * 3); rot += 12 * (1 - p); break; }
        case 'swingin': { rot += 70 * Math.cos(p * Math.PI * 2.2) * (1 - p); y -= H * .03 * (1 - p); op = U.clamp(p * 2.5); break; }
        case 'slideL': { x -= W * .12 * (1 - U.easeOut(p)); op = U.easeOut(p); blur = 6 * (1 - p); break; }
        case 'slideR': { x += W * .12 * (1 - U.easeOut(p)); op = U.easeOut(p); blur = 6 * (1 - p); break; }
        case 'rollup': { y += H * .05 * (1 - U.easeOut(p)); s *= U.lerp(.85, 1, p); op = U.easeOut(p); rot -= 25 * (1 - U.easeOut(p)); break; }
        case 'wave': { y += Math.sin(p * Math.PI * 2 + i * .7) * H * .025 * (1 - p); op = U.clamp(p * 2); break; }
        case 'bounce': { y -= H * .08 * Math.abs(Math.sin(p * Math.PI * 2.5)) * (1 - p); op = U.clamp(p * 3); s *= U.lerp(.7, 1, U.easeOut(p)); break; }
        case 'stretch': { s *= p < 1 ? U.lerp(.1, 1, U.easeOutBack(p)) : 1; x += (ch.x - L.cx) * .6 * (1 - U.easeOut(p)); op = U.clamp(p * 4); break; }
        case 'blurin': { blur = 24 * Math.pow(1 - p, 1.4); op = Math.pow(p, .6); s *= 1 + .08 * (1 - p); break; }
        case 'typewriter': { op = p > 0 ? 1 : 0; y -= 4 * (1 - U.clamp((t - ch.es) / .1)); br = 1 + 1.2 * (1 - U.clamp((t - ch.es) / .2)); break; }
        case 'rainbow': { op = U.easeOut(p); br = 1 + 1.4 * (1 - p); s *= U.lerp(1.3, 1, U.easeOut(p)); out.hue = (1 - p) * 300 + i * 25; break; }
        case 'pop': { s *= p < 1 ? (p < .6 ? U.lerp(0, 1.35, p / .6) : U.lerp(1.35, 1, (p - .6) / .4)) : 1; op = U.clamp(p * 4); break; }
        case 'sway': { x += Math.sin(p * Math.PI * 1.5) * 20 * (1 - p); rot += Math.sin(p * Math.PI * 2) * 8 * (1 - p); op = U.easeOut(p); blur = 5 * (1 - p); break; }
        case 'shutter': { const q = U.easeOut(p); s *= Math.max(.02, q); op = q > .05 ? 1 : 0; br = 1 + 1.5 * (1 - q); break; }
        case 'cascade': { y -= H * .1 * (1 - U.bounceOut(p)); rot += 20 * (1 - p) * (i % 2 ? 1 : -1); op = U.clamp(p * 2.5); break; }
        default: break;
      }
      if (cfg.preset === 'edm' && t > ch.ce && t < ch.ce + .12 && U.h01(i + Math.floor(t * 40)) > .7) glitch = true;

      // 卡拉 OK：没唱到的半透明，唱到的那一下弹起来，长音持续放大并颤动
      const sung = t >= ch.cs, singing = t >= ch.cs && t < ch.ce;
      if (L.kind === 'line') {
        if (!sung) op *= .5;
        if (sung) {
          const k = t - ch.cs;
          s *= 1 + Math.exp(-k * 7) * Math.sin(k * 22 + .3) * .22 * (.5 + ch.stress);
          br *= 1 + .35 * Math.exp(-k * 4);
        }
        if (singing && ch.long) {
          const u = (t - ch.cs) / (ch.ce - ch.cs);
          s *= 1 + .22 * u;
          x += Math.sin(t * 2 * Math.PI * 5.6) * 1.6 * U.sstep(.25, .5, u);
        }
      } else if (sung) br *= 1 + .5 * Math.exp(-(t - ch.cs) * 3);

      if (ch.giant) s *= 1 + .03 * pulseAtT(t);

      // 退场
      if (t > L.exitAt) {
        const ex = L.combo.exit;
        if (ex === 'sink') {
          const q = U.easeInOut(U.clamp((t - L.exitAt) / .9));
          s *= U.lerp(1, .78, q); blur += 6 * q; op *= U.lerp(1, .28, q); y += 10 * q; br *= U.lerp(1, .8, q);
          if (L.killAt && t >= L.killAt) {
            const k2 = U.clamp((t - L.killAt) / .7);
            const dx = ch.x - W / 2, dy = ch.y - H / 2, d = Math.hypot(dx, dy) || 1;
            x += dx / d * W * .08 * U.easeOut(k2);
            y += dy / d * W * .08 * U.easeOut(k2);
            op *= 1 - k2; blur += 6 * k2;
          } else if (L.fadeAt) op *= 1 - U.sstep(L.fadeAt, L.fadeAt + 1.2, t);
        } else {
          const q = U.clamp((t - L.exitAt) / .7);
          if (ex === 'burst') {
            const dx = ch.x - L.cx + ch.scat[0] * 80, dy = ch.y - L.cy + ch.scat[1] * 80, d = Math.hypot(dx, dy) || 1;
            // 粒子散开：幅度收小（不再飞出屏幕），并且边飞边快速消失
            x += dx / d * W * .07 * U.easeOut(q);
            y += dy / d * W * .07 * U.easeOut(q);
            rot += ch.scat[2] * .4 * q; op *= Math.pow(1 - q, 1.6); blur += 3 * q; s *= 1 + .15 * q;
          } else if (ex === 'push') {
            y -= H * .14 * U.easeIn(q); op *= 1 - q; blur += 4 * q;
          // —— 第四十轮新增 20 种退场 ——
          } else if (ex === 'fadeout') { op *= 1 - U.easeIn(q);
          } else if (ex === 'blurout') { blur += 18 * q; op *= 1 - q;
          } else if (ex === 'floatup') { y -= H * .08 * U.easeIn(q); op *= 1 - q; blur += 5 * q; x += Math.sin(i * 1.3 + t * 3) * 5 * q;
          } else if (ex === 'dissolve') { op *= U.h01(i * 7.3 + Math.floor(t * 20)) > q ? 1 - q * .5 : 0;
          } else if (ex === 'drift') { x += W * .05 * U.easeIn(q) * (i % 2 ? 1 : -1); y -= H * .02 * q; op *= 1 - q; blur += 6 * q;
          } else if (ex === 'shrink') { s *= 1 - .85 * U.easeIn(q); op *= 1 - q * .6;
          } else if (ex === 'evaporate') { const qi = U.clamp(q * 1.6 - (i / Math.max(1, L.chars.length)) * .6); y -= H * .05 * qi; blur += 10 * qi; op *= 1 - qi;
          } else if (ex === 'popout') { s *= q < .3 ? 1 + q : Math.max(0, 1.3 - (q - .3) * 2); op *= 1 - U.clamp((q - .3) / .7);
          } else if (ex === 'spinout') { rot += 300 * U.easeIn(q); s *= 1 - .8 * q; op *= 1 - q;
          } else if (ex === 'flipout') { s *= Math.max(.02, Math.cos(q * Math.PI / 2)); op *= 1 - q * .5;
          } else if (ex === 'bounceout') { y += H * .12 * U.easeIn(q) - H * .03 * Math.sin(q * Math.PI); op *= 1 - U.clamp((q - .4) / .6);
          } else if (ex === 'confetti') { x += ch.scat[0] * W * .2 * q; y += H * .15 * q * q - H * .05 * q; rot += ch.scat[2] * 2 * q; op *= 1 - q;
          } else if (ex === 'zoomthru') { s *= 1 + 2.2 * U.easeIn(q); blur += 10 * q; op *= 1 - q;
          } else if (ex === 'slideoutL') { x -= W * .14 * U.easeIn(q); op *= 1 - q; blur += 5 * q;
          } else if (ex === 'slideoutR') { x += W * .14 * U.easeIn(q); op *= 1 - q; blur += 5 * q;
          } else if (ex === 'glitchout') { glitch = true; x += (U.h01(Math.floor(t * 30) + i) - .5) * 40 * q; op *= U.h01(Math.floor(t * 25) * 1.7 + i) > q ? 1 : 0;
          } else if (ex === 'shatter') { x += ch.scat[0] * W * .12 * q; y += ch.scat[1] * H * .12 * q + H * .06 * q * q; rot += ch.scat[2] * q; op *= 1 - q; s *= 1 - .3 * q;
          } else if (ex === 'crush') { s *= 1 - .9 * U.easeIn(q); br *= 1 + 1.5 * q; op *= 1 - U.clamp((q - .5) * 2);
          } else if (ex === 'rolldown') { y += H * .05 * U.easeIn(q); rot += 25 * q; op *= 1 - q;
          } else if (ex === 'wipeout') { const n2 = L.chars.length; op *= (i / Math.max(1, n2)) > q * 1.05 ? 1 : 0;
          } else if (ex === 'typeback') { const n2 = L.chars.length; op *= ((n2 - 1 - i) / Math.max(1, n2)) > q * 1.05 ? 1 : 0;
          } else {
            blur += 14 * q; s *= 1 + .25 * q; op *= 1 - q;
          }
        }
      }
      if (out.hue == null) out.hue = 0;
      if (out.sy == null) out.sy = 1;
      if (out.clipB == null) out.clipB = 0;
      out.x = x; out.y = y; out.s = s; out.rot = rot; out.op = op; out.blur = blur; out.br = br; out.glitch = glitch;
      out.on = singing || (sung && t < ch.ce + .25);
      return out;
    }

    function applyEl(el, s, skew, extraS, extraOp, extraBlur) {
      const op = s.op * (extraOp == null ? 1 : extraOp);
      if (op <= .004) { el.style.opacity = 0; return; }
      el.style.opacity = op.toFixed(3);
      el.style.transform = 'translate3d(' + s.x.toFixed(1) + 'px,' + s.y.toFixed(1) + 'px,0) translate(-50%,-50%) rotate('
        + s.rot.toFixed(2) + 'deg) skewX(' + skew + 'deg) scale(' + (s.s * (extraS == null ? 1 : extraS)).toFixed(3)
        + (s.sy !== 1 ? ',' + (s.s * (extraS == null ? 1 : extraS) * s.sy).toFixed(3) : '') + ')';
      // lineup：字从基线下面升上来时，基线以下的部分裁掉（像从一条线后面冒出来）
      const cb = s.clipB > .01 ? 'inset(0 0 ' + Math.min(100, s.clipB * 100).toFixed(0) + '% 0)' : '';
      if (el._cb !== cb) { el.style.clipPath = cb; el._cb = cb; }
      s.sy = 1; s.clipB = 0;
      const b = s.blur + (extraBlur || 0);
      const hue = s.hue > 1 ? ' hue-rotate(' + s.hue.toFixed(0) + 'deg)' : '';
      el.style.filter = b > .4
        ? 'blur(' + b.toFixed(1) + 'px) brightness(' + s.br.toFixed(2) + ')' + hue
        : (Math.abs(s.br - 1) > .02 ? 'brightness(' + s.br.toFixed(2) + ')' + hue : (hue || 'none'));
      s.hue = 0;
    }

    /* ------------------------------ 第三十三轮：避开人物 ------------------------------ */
    // occ = { w, h, g:Uint8Array, W, H }：人物在屏幕上的占用格（由运行时每帧按网格顶点算）
    let occ = null, lastT = null;
    let layerDx = 0;
    function setOccupancy(o, dx) { occ = o; layerDx = +dx || 0; }
    // 句子的包围框（按排版位置 + 字号；只看静态位置，入场/退场动画不算）
    function lineBox(L) {
      let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
      for (const ch of L.chars) {
        const hw = charW(ch.c, ch.fs) * .55, hh = ch.fs * .6;
        x0 = Math.min(x0, ch.x - hw); x1 = Math.max(x1, ch.x + hw);
        y0 = Math.min(y0, ch.y - hh); y1 = Math.max(y1, ch.y + hh);
      }
      return [x0, y0, x1, y1];
    }
    // 某个框（平移 dx,dy、缩放 sc 后）和人物重叠多少格；出界也算重叠（不许跑出屏幕）
    function boxHits(b, dx, dy, sc) {
      if (!occ) return 0;
      const cx = (b[0] + b[2]) / 2, cy = (b[1] + b[3]) / 2;
      const hw = (b[2] - b[0]) / 2 * sc, hh = (b[3] - b[1]) / 2 * sc;
      const x0 = cx + dx - hw + layerDx, x1 = cx + dx + hw + layerDx, y0 = cy + dy - hh, y1 = cy + dy + hh;
      const mW = W * .02, mH = H * .03;
      let out = 0;
      if (x0 < mW || x1 > W - mW || y0 < mH || y1 > H * .9) out += 1000;   // 底部留 10%（字幕条已关）
      const gx0 = Math.max(0, Math.floor(x0 / W * occ.w)), gx1 = Math.min(occ.w - 1, Math.floor(x1 / W * occ.w));
      const gy0 = Math.max(0, Math.floor(y0 / H * occ.h)), gy1 = Math.min(occ.h - 1, Math.floor(y1 / H * occ.h));
      for (let y = gy0; y <= gy1; y++) for (let x = gx0; x <= gx1; x++) out += occ.g[y * occ.w + x];
      return out;
    }
    // 其它正在显示的句子占的框（新句要避开它们，不叠字）
    function placedBoxes(except) {
      const out = [];
      for (const L of prep) {
        if (L === except || !L.place || L.g.style.display !== 'block') continue;
        const b = lineBox(L), p = L.place;
        const cx = (b[0] + b[2]) / 2, cy = (b[1] + b[3]) / 2, hw = (b[2] - b[0]) / 2 * p.sc, hh = (b[3] - b[1]) / 2 * p.sc;
        out.push([cx + p.dx - hw, cy + p.dy - hh, cx + p.dx + hw, cy + p.dy + hh]);
      }
      return out;
    }
    function scoreAt(b, dx, dy, sc, others) {
      let h = boxHits(b, dx, dy, sc) * 10;
      const cx = (b[0] + b[2]) / 2 + dx, cy = (b[1] + b[3]) / 2 + dy, hw = (b[2] - b[0]) / 2 * sc, hh = (b[3] - b[1]) / 2 * sc;
      for (const o of others) {
        const ix = Math.min(cx + hw, o[2]) - Math.max(cx - hw, o[0]), iy = Math.min(cy + hh, o[3]) - Math.max(cy - hh, o[1]);
        if (ix > 0 && iy > 0) h += 40 + ix * iy / (W * H) * 4000;
      }
      return h;
    }
    // 一次性定位：原位可用就不动；否则在网格候选里找「不压人物、不压别的句子、不出屏」且离原位最近的；
    // 实在放不下才缩小（最小 0.5）
    function placeLine(L) {
      const b = lineBox(L);
      if (!(b[2] > b[0])) return { dx: 0, dy: 0, sc: 1 };
      const others = placedBoxes(L);
      if (scoreAt(b, 0, 0, 1, others) === 0) return { dx: 0, dy: 0, sc: 1 };
      let best = null;
      for (const sc of [1, .86, .72, .6, .5]) {
        for (let gy = 0; gy <= 16; gy++) for (let gx = 0; gx <= 20; gx++) {
          const tx = W * (.04 + .92 * gx / 20), ty = H * (.06 + .78 * gy / 16);
          const dx = tx - (b[0] + b[2]) / 2, dy = ty - (b[1] + b[3]) / 2;
          const h = scoreAt(b, dx, dy, sc, others);
          const cost = h * 1000 + Math.hypot(dx / W, dy / H) + (1 - sc) * 1.5;
          if (!best || cost < best.cost) best = { dx, dy, sc, cost, h };
        }
        if (best && best.h === 0) break;
      }
      return best ? { dx: best.dx, dy: best.dy, sc: best.sc } : { dx: 0, dy: 0, sc: 1 };
    }

    let shotKey = '';
    let plateCv = null, plateCtx = null;
    function drawPlates(t) {
      if (!plateCtx) return;
      const dpr = 1, Wp = W, Hp = H;
      if (plateCv.width !== Wp || plateCv.height !== Hp) { plateCv.width = Wp; plateCv.height = Hp; }
      plateCtx.clearRect(0, 0, Wp, Hp);
      const FX = global.NekoStageFrames;
      if (!FX) return;
      for (const L of prep) {
        if (!L.plate || L.g.style.display !== 'block' || L.g.parentNode !== els.back) continue;
        // 这句当前可见的字的外框（用字的规划位置 + 字号算，不读 DOM，避免每帧回流）
        let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9, n = 0, op = 0;
        for (const ch of L.chars) {
          const o = +ch.el.style.opacity || 0;
          if (o < .05) continue;
          const f = ch.fs || 20;
          x0 = Math.min(x0, ch.x - f * .55); x1 = Math.max(x1, ch.x + f * .55); y0 = Math.min(y0, ch.y - f * .6); y1 = Math.max(y1, ch.y + f * .6);
          n++; op = Math.max(op, o);
        }
        if (n < 2) continue;
        FX.drawPlate(plateCtx, L.plate, [x0, y0, x1, y1], L.plateCols, Math.min(1, op) * .95, L.i * 7.3);
      }
    }

    function render(t) {
      lastT = t;
      const sh = shotAt(t), key = sh ? (sh.t + ':' + sh.kind) : '';
      const camChanged = key !== shotKey;
      shotKey = key;
      for (const L of prep) {
        const vis = t >= L.vis0 && t <= L.gone;
        if (!vis) { if (L.g.style.display !== 'none') L.g.style.display = 'none'; L.place = null; continue; }
        if (L.g.style.display !== 'block') L.g.style.display = 'block';
        // 第一次出现：定位一次。镜头切换：重新定位一次，0.45 s 缓动过去。其余时间位置固定
        // 第三十五轮：位置由规划器一次定好，显示期间不动（没有规划结果前先不显示，最多一帧）
        if (!L0Planned) { L.g.style.visibility = 'hidden'; continue; }
        // 没有规划结果的句子（极少）：只在被唱的那段显示，不长时间停留，避免压住别的
        if (L.planned === false && t > (L.exitAt || L.t1) + .4) { L.g.style.display = 'none'; continue; }
        if (L.g.style.visibility === 'hidden') L.g.style.visibility = '';
        // 镜头硬切分段：每个字直接取当前段的规划位置；切点处 0.35 s 从上一段位置过渡（字号只在变化时写）
        let curPos = (L.segs && L.segs.length) ? L.segs[0].pos : null;
        if (L.segs && L.segs.length > 1) {
          let si = 0;
          for (let k = 0; k < L.segs.length; k++) if (t >= L.segs[k].t0) si = k;
          const cur = L.segs[si].pos, prv = si > 0 ? L.segs[si - 1].pos : cur;
          const u = si > 0 ? U.easeInOut(U.clamp((t - L.segs[si].t0) / .35)) : 1;
          curPos = cur;
          for (let i = 0; i < L.chars.length; i++) {
            const ch = L.chars[i], b = cur[i];
            if (!b || b.hide) continue;
            const a = (prv[i] && !prv[i].hide) ? prv[i] : b;
            ch.x = U.lerp(a.x, b.x, u); ch.y = U.lerp(a.y, b.y, u); ch.rot = b.rot;
            const f = U.lerp(a.fs, b.fs, u);
            if (Math.abs(f - (ch.fs || 0)) > .5) { ch.fs = f; ch.el.style.fontSize = f.toFixed(1) + 'px'; for (const g of ch.ghosts) g.style.fontSize = f.toFixed(1) + 'px'; }
          }
        }
        if (L.g.style.transform) L.g.style.transform = '';
        for (let i = 0; i < L.chars.length; i++) {
          const ch = L.chars[i];
          const s = charState(L, ch, i, t, st);
          applyEl(ch.el, s, cfg.skew);
          // 第三十八轮：长句拆两半——当前段里不属于这一半的字隐藏
          const hidden = !!(curPos && curPos[i] && curPos[i].hide);
          if (hidden) { ch.el.style.opacity = '0'; for (const g of ch.ghosts) g.style.opacity = '0'; continue; }
          if (s.on !== ch.on) { ch.on = s.on; ch.el.classList.toggle('on', s.on); }
          if (s.glitch !== ch.glc) { ch.glc = s.glitch; ch.el.classList.toggle('gl', s.glitch); }
          for (let gi = 0; gi < ch.ghosts.length; gi++) {
            const g2 = charState(L, ch, i, t - .09 * (gi + 1), {});
            applyEl(ch.ghosts[gi], g2, cfg.skew, 1.25 + gi * .3, .28 - gi * .12, 5 + gi * 6);
          }
        }
      }
    }

    const _render0 = render;
    render = function (t) { _render0(t); try { drawPlates(t); } catch (e) { } };
    /* ------------------------------ 字幕条 ------------------------------ */
    let subCache = '';
    function subtitle(t, on) {
      if (!els.sub) return;
      // 第四十四轮：有美术指导时舞台歌词就是主歌词，底部字幕条会把同一句再写一遍 → 关掉，画面只留一处歌词
      if (global.__NEKO_ART) { els.sub.style.opacity = 0; return; }
      const sung = prep.filter(L => L.kind === 'line');
      let cur = null;
      for (const L of sung) if (t >= L.t0 - .8 && t <= L.exitAt + .3) cur = L;
      const nx = cur ? sung[sung.indexOf(cur) + 1] : sung.find(L => L.t0 > t);
      els.sub.style.opacity = on && (cur || (nx && nx.t0 - t < 2)) ? 1 : 0;
      const curT = cur ? cur.text : '', key = curT + '|' + (nx ? nx.text : '');
      if (key !== subCache) {
        subCache = key;
        els.sub.querySelector('.base').textContent = curT;
        els.sub.querySelector('.fill').textContent = curT;
        els.sub.querySelector('.nxt').textContent = nx && nx !== cur ? nx.text : '';
      }
      if (cur) {
        const n = cur.chars.length;
        let prog = 0;
        cur.chars.forEach(ch => { prog += U.clamp((t - ch.cs) / Math.max(.05, ch.ce - ch.cs)); });
        const flatIdx = [];
        let k = 0;
        for (const c of cur.text) { if (c !== ' ') flatIdx.push(k); k++; }
        const whole = Math.floor(prog), frac = prog - whole;
        const pos = whole >= n ? cur.text.length : (flatIdx[whole] + frac);
        els.sub.querySelector('.fill').style.clipPath = 'inset(0 ' + (100 - pos / cur.text.length * 100).toFixed(2) + '% 0 0)';
      }
    }

    /* ------------------------------ 查询 ------------------------------ */
    function lineAt(t) {
      let r = null;
      for (const L of prep) {
        if (L.kind !== 'line') continue;
        if (t >= L.t0 - .35 && t <= L.exitAt + .6) r = L;
      }
      return r;
    }

    // 节拍脉冲：强拍更亮一点（只由 t 与 bpm 决定）
    function pulseAtT(t) {
      const beat = 60 / show.music.bpm;
      const bp = t / beat, ph = U.fract(bp);
      return Math.exp(-ph * 6) * (U.fract(bp / 4) < .25 ? 1.35 : 1);
    }

    // 第三十五轮：版式规划（人物位置 + 时间段 + 同屏句 → 区域 + 模板），结果写回每个字的 x/y/fs
    let planner = null;
    function plan(ctx) {
      if (!global.NekoStagePlanner) return;
      planner = planner || global.NekoStagePlanner.create({
        W: ctx.W, H: ctx.H, cameraAt: ctx.cameraAt, occAt: ctx.occAt,
        charW: (c, fs, font) => { MEASURE_FONT = font || cfg.font || ''; MEASURE_WT = cfg.weight || 400; return charW(c, fs); }
      });
      const plans = planner.planAll(prep, ctx);
      const sty = global.NekoStagePlanner.STYLE_SETS[ctx.preset] || global.NekoStagePlanner.STYLE_SETS.pop;
      let k = 0;
      for (const L of prep) {
        const p = plans.get(L);
        if (!p) { L.planned = false; continue; }
        L.planned = true; L.tpl = p.tpl;
        L.segs = (p.segs || []).map(sg => ({ t0: sg.t0, t1: sg.t1, pos: sg.pos.map(q => q ? { x: q.x, y: q.y, fs: q.fs, rot: q.rot || 0, hide: !!q.hide } : null) }));
        p.pos.forEach((q, i) => {
          const ch = L.chars[i]; if (!q || !ch || q.hide) return;
          ch.x = q.x; ch.y = q.y; ch.fs = q.fs; ch.rot = q.rot || 0; ch.giant = !!q.big;
          ch.el.style.fontSize = ch.fs.toFixed(1) + 'px';
          for (const g of ch.ghosts) g.style.fontSize = ch.fs.toFixed(1) + 'px';
        });
        L.cx = L.chars.reduce((a, ch) => a + ch.x, 0) / Math.max(1, L.chars.length);
        L.cy = L.chars.reduce((a, ch) => a + ch.y, 0) / Math.max(1, L.chars.length);
        // 字体样式：整首一组（按音乐风格），逐句轮换；对开/标题用组里最「重」的那种
        const ARTL = global.__NEKO_ART;
        const st = ARTL ? ARTL.lyricMatFor(L.section, p.tpl === 'flank' || L.kind === 'title' || (k++ % 3) !== 2)
          : ((p.tpl === 'flank' || L.kind === 'title') ? sty[0] : sty[(k++) % sty.length]);
        const ink = st === 'ink' || st === 'serifglow' ? (cfg.accentFont || L.font) : L.font;
        L.chars.forEach(ch => {
          for (const el of [ch.el].concat(ch.ghosts)) {
            el.className = el.className.replace(/\bm-[a-z]+\b/, 'm-' + st);
            if (ink) el.style.fontFamily = ink;
          }
        });
      }
      L0Planned = true;
      buildAnchors();
    }
    let L0Planned = false;

    const api = {
      build, layout, render, subtitle, lineAt, setOccupancy, plan,
      get prep() { return prep; },
      get anchors() { return anchors; },
      charW
    };
    return api;
  }

  global.NekoStageLyrics = { create };
})(window);
