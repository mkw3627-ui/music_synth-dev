/* =============================================================================
 * 「为她而唱」舞台 · 第三十五轮：歌词版式规划器（先规划、后摆放；不做每帧碰撞）
 *
 * 思路（用户给的规则）：
 *   1) 人物在哪里：舞台用 show.json 的镜头表（cameraAt）决定人物每一时刻的缩放/位置；
 *      人物的「轮廓」来自模型网格，在标准镜头下量一次，按镜头参数换算到任意时刻，不用每帧测。
 *   2) 这一句显示的整段时间（出现 → 消失）里，把人物会占到的区域取并集——包括这段时间里的镜头切换，
 *      切上来会压住的位置一开始就不用。
 *   3) 剩下的空位（左侧带、右侧带、上方带、两侧对称）里，按版式模板挑一个最合适的。
 *   4) 同时在屏幕上的句子各占一个区，不叠字。
 *   5) 位置一次定好，整句显示期间不动；人物挡住一点点可以，挡住大半不行（按面积比判断）。
 *
 * 版式模板（参考动态歌词排版 / MV 字幕 / 封面字体设计的常见手法）：
 *   side      侧栏横排：靠空的一侧，左对齐/右对齐，长句按词折行，行距紧
 *   stack     错落叠排：两到三行，字号一大一小，左缘错开
 *   flank     两侧对开：关键词（2~4 字）拆成两半分列人物两侧，大字号；其余字小字一行放在上方
 *   vertical  竖排：一到两列，贴空侧；适合抒情/古风
 *   banner    上方横幅：整句一行放在头顶上方空带（人物不顶天时）
 *   corner    角落小字：很短的句子放在角落，配大号首字
 * ========================================================================== */
(function (global) {
  'use strict';
  const U = global.NekoStageU;

  // 字体风格（材质）按音乐风格挑：每种预设一组，可轮换（不同句不同材质，但整首风格统一）
  const STYLE_SETS = {
    ballad:  ['soft', 'outline', 'serifglow', 'soft'],
    jazz:    ['serifglow', 'soft', 'outline', 'serifglow'],
    ancient: ['ink', 'soft', 'ink', 'outline'],
    pop:     ['sticker', 'grad', 'pop', 'outline'],
    cute:    ['sticker', 'candy', 'pop', 'grad'],
    edm:     ['neon', 'chrome', 'glitchy', 'outline'],
    rock:    ['chrome', 'outline', 'stamp', 'neon'],
    dark:    ['outline', 'neon', 'stamp', 'serifglow']
  };

  function create(api) {
    // api: { W(), H(), cameraAt(t), occAt(cam) -> {w,h,g}, charW(c, fs, font) }
    let plans = new Map();

    // 人物占用格（48x27）在某个时间段内的并集：按 0.2 s 采样镜头 + 段内所有镜头切点
    function occUnion(t0, t1, cuts) {
      const ts = [];
      for (let t = t0; t <= t1; t += .2) ts.push(t);
      for (const c of cuts) if (c > t0 && c < t1) { ts.push(c + .02); ts.push(c + .3); }
      let G = null, w = 0, h = 0;
      for (const t of ts) {
        const o = api.occAt(api.cameraAt(t));
        if (!o) continue;
        w = o.w; h = o.h;
        if (!G) G = new Uint8Array(w * h);
        for (let i = 0; i < G.length; i++) if (o.g[i]) G[i] = 1;
      }
      return G ? { w, h, g: G } : null;
    }
    // 一个矩形里被人物占的比例
    function coverFrac(occ, r, W, H) {
      if (!occ) return 0;
      const gx0 = Math.max(0, Math.floor(r[0] / W * occ.w)), gx1 = Math.min(occ.w - 1, Math.floor((r[2] - 1) / W * occ.w));
      const gy0 = Math.max(0, Math.floor(r[1] / H * occ.h)), gy1 = Math.min(occ.h - 1, Math.floor((r[3] - 1) / H * occ.h));
      let n = 0, hit = 0;
      for (let y = gy0; y <= gy1; y++) for (let x = gx0; x <= gx1; x++) { n++; hit += occ.g[y * occ.w + x]; }
      return n ? hit / n : 0;
    }
    // 空区：从占用格里找出人物左右两侧的空带、头顶上方的空带
    function freeZones(occ, W, H) {
      const top = H * .06, bot = H * .82, mx = W * .035;
      if (!occ) return [{ id: 'L', r: [mx, top, W * .45, bot] }, { id: 'R', r: [W * .55, top, W - mx, bot] }];
      let minX = occ.w, maxX = -1, minY = occ.h;
      for (let y = 0; y < occ.h; y++) for (let x = 0; x < occ.w; x++) if (occ.g[y * occ.w + x]) {
        if (x < minX) minX = x; if (x > maxX) maxX = x; if (y < minY) minY = y;
      }
      if (maxX < 0) return [{ id: 'L', r: [mx, top, W * .45, bot] }, { id: 'R', r: [W * .55, top, W - mx, bot] }];
      // 每行最左/最右的占用；取分位数，尾巴/发梢这类少数几行伸出去的部分允许被轻压（「挡一点没关系」）
      const lefts = [], rights = [];
      for (let y = 0; y < occ.h; y++) { let a = -1, b = -1; for (let x = 0; x < occ.w; x++) if (occ.g[y * occ.w + x]) { if (a < 0) a = x; b = x; } if (a >= 0) { lefts.push(a); rights.push(b); } }
      lefts.sort((p, q) => p - q); rights.sort((p, q) => p - q);
      const q = (arr, f) => arr[Math.min(arr.length - 1, Math.floor(f * (arr.length - 1)))];
      const lx = q(lefts, .15) / occ.w * W, rx = (q(rights, .85) + 1) / occ.w * W, ty = minY / occ.h * H;
      const pad = W * .02;
      const zones = [];
      if (lx - pad - mx > W * .14) zones.push({ id: 'L', r: [mx, top, lx - pad, bot] });
      if (W - mx - (rx + pad) > W * .14) zones.push({ id: 'R', r: [rx + pad, top, W - mx, bot] });
      if (ty - top > H * .14) zones.push({ id: 'T', r: [W * .12, top, W * .88, ty - H * .02] });
      // 两侧都窄（特写）：允许压住人物的外缘（头发/肩），只要不压脸——用人物外侧 18% 作为「可轻压」区
      if (!zones.length) {
        const ew = (rx - lx) * .18;
        zones.push({ id: 'L', r: [mx, top, Math.max(mx + W * .2, lx + ew), bot], soft: true });
        zones.push({ id: 'R', r: [Math.min(W - mx - W * .2, rx - ew), top, W - mx, bot], soft: true });
      }
      return zones;
    }

    // 把一句的字摆进区域：返回 [{x, y, fs, rot}]（屏幕坐标，字中心）与外框
    function fit(chars, zone, tpl, opts) {
      const r0 = fit0(chars, zone, tpl, opts);
      if (!r0) return null;
      const minFs = api.H() * (opts.minFs || .034);
      return r0.pos.every(q => !q || q.fs >= minFs * .8) ? r0 : null;
    }
    function fit0(chars, zone, tpl, opts) {
      const [zx0, zy0, zx1, zy1] = zone.r, zw = zx1 - zx0, zh = zy1 - zy0;
      const n = chars.length, out = new Array(n);
      const font = opts.font, base = opts.fs;
      const latinGap = (ch, fs) => (ch.wEnd && /[A-Za-z0-9,.'!?]/.test(ch.c) ? fs * .3 : 0);
      const cw = (c, fs) => api.charW(c, fs, font);
      const cwc = (ch, fs) => cw(ch.c, fs) + latinGap(ch, fs);
      const lineW = (arr, fs) => arr.reduce((a, ch, i) => a + cw(ch.c, fs) + (i < arr.length - 1 ? latinGap(ch, fs) : 0), 0);
      // 按词边界折行，目标每行最多 maxW
      function wrap(fs, maxW, maxRows) {
        const rows = [[]];
        let w = 0;
        chars.forEach((ch, i) => {
          const cwid = cwc(ch, fs);
          if (w + cwid > maxW && rows[rows.length - 1].length) {
            // 拉丁字母只能在词尾断（不能把一个单词拆开）；中日文优先在标点/词尾断
            const row = rows[rows.length - 1];
            const latin = /[A-Za-z]/.test(ch.c);
            let cut = latin ? -1 : row.length;
            for (let k = row.length - 1; k >= (latin ? 0 : row.length * .5); k--) if (row[k].wEnd || /[、，,。！!？?　\s]/.test(row[k].c)) { cut = k + 1; break; }
            if (cut <= 0) cut = row.length;
            const carry = row.splice(cut);
            rows.push(carry);
            w = lineW(carry, fs);
          }
          rows[rows.length - 1].push(Object.assign(ch, { _i: i }));
          w += cwid;
          if (w > maxW * 1.6) w = maxW * 1.6;
        });
        // 不留孤字：末行太短（中日文 <3 字）时从上一行末尾挪字过来，且不拆开拉丁单词
        for (let r = rows.length - 1; r > 0; r--) {
          const last = rows[r], prev = rows[r - 1];
          const latin = last.some(ch => /[A-Za-z]/.test(ch.c));
          if (latin) continue;
          while (last.length < 3 && prev.length > 3) last.unshift(prev.pop());
        }
        return rows.length <= maxRows ? rows : null;
      }
      const align = zone.id === 'R' ? 'left' : zone.id === 'L' ? 'right' : 'center';
      let fs = base, rows = null;
      if (tpl === 'vertical') {
        // 竖排：每列最多 colN 字，最多 2 列
        for (let k = 0; k < 14; k++) {
          const step = fs * 1.02, colN = Math.max(1, Math.floor(zh * .9 / step));
          const cols = Math.ceil(n / colN);
          if (cols <= 2 && cols * fs * 1.3 <= zw) {
            const colH = Math.min(n, colN) * step;
            const x0 = align === 'right' ? zx1 - fs * .6 : zx0 + fs * .6 + (cols - 1) * fs * 1.3;
            const y0 = zy0 + (zh - colH) * .3;
            chars.forEach((ch, i) => {
              const col = Math.floor(i / colN), r = i % colN;
              out[i] = { x: x0 - col * fs * 1.3, y: y0 + r * step + step / 2, fs: fs, rot: 0 };
            });
            return { pos: out, fs };
          }
          fs *= .9;
        }
        tpl = 'side';
        fs = base;
      }
      if (tpl === 'stack') {
        for (let k = 0; k < 14; k++) {
          rows = wrap(fs, zw * .9, 3);
          if (rows && rows.length >= 2) {
            const sizes = rows.map((r, j) => (j === 0 ? 1.18 : j === 1 ? .82 : 1));
            const heights = sizes.map(s => fs * s * 1.12);
            const tot = heights.reduce((a, b) => a + b, 0);
            if (tot <= zh * .8 && rows.every((r, j) => lineW(r, fs * sizes[j]) <= zw * .96)) {
              let y = zy0 + (zh - tot) * .32;
              rows.forEach((row, j) => {
                const f = fs * sizes[j], wRow = lineW(row, f);
                const shift = (j % 2 ? 1 : 0) * Math.min(zw - wRow, f * 1.6);
                let x = align === 'right' ? zx1 - wRow - shift : zx0 + shift;
                if (align === 'center') x = zx0 + (zw - wRow) / 2 + (j % 2 ? f : -f) * .6;
                row.forEach(ch => { const w = cw(ch.c, f); out[ch._i] = { x: x + w / 2, y: y + f * .55, fs: f, rot: 0 }; x += w + latinGap(ch, f); });
                y += heights[j];
              });
              return { pos: out, fs };
            }
          }
          if (!rows) fs *= .9; else if (rows.length < 2) { tpl = 'side'; break; } else fs *= .92;
        }
        tpl = 'side'; fs = base;
      }
      // side / banner：横排，按区宽折行（最多 3 行），对齐空侧
      const maxRows = tpl === 'banner' ? 1 : 3;
      for (let k = 0; k < 20; k++) {
        rows = wrap(fs, zw * (tpl === 'banner' ? 1 : .94), maxRows);
        if (rows) {
          const gap = fs * 1.16, tot = rows.length * gap;
          if (tot <= zh * .85) {
            let y = zy0 + (zh - tot) * (zone.id === 'T' ? .55 : .3) + gap / 2;
            rows.forEach(row => {
              const wRow = lineW(row, fs);
              let x = align === 'right' ? zx1 - wRow : align === 'left' ? zx0 : zx0 + (zw - wRow) / 2;
              row.forEach(ch => { const w = cw(ch.c, fs); out[ch._i] = { x: x + w / 2, y, fs: fs, rot: 0 }; x += w + latinGap(ch, fs); });
              y += gap;
            });
            return { pos: out, fs };
          }
        }
        fs *= .9;
      }
      return null;
    }

    // 两侧对开：关键词拆成两半，分列人物左右（大字），其余字小字放上方空带或左侧顶部
    function fitFlank(L, zones, opts, W, H) {
      const Lz = zones.find(z => z.id === 'L'), Rz = zones.find(z => z.id === 'R');
      if (!Lz || !Rz || !L.kw) return null;
      const flat = L.chars.map(c => c.c).join('');
      const at = flat.indexOf(L.kw);
      const k = [...L.kw].length;
      if (at < 0 || k < 2 || k > 4) return null;
      const half = Math.ceil(k / 2);
      const cw = (c, fs) => api.charW(c, fs, opts.font);
      const out = new Array(L.chars.length);
      // 大字字号：两侧各自放得下
      const big = Math.min(H * .2, (Lz.r[2] - Lz.r[0]) * .9 / half, (Rz.r[2] - Rz.r[0]) * .9 / (k - half));
      if (big < H * .08) return null;
      const yBig = H * .46;
      let x = Lz.r[2] - L.chars.slice(at, at + half).reduce((a, ch) => a + cw(ch.c, big), 0);
      for (let i = at; i < at + half; i++) { const w = cw(L.chars[i].c, big); out[i] = { x: x + w / 2, y: yBig, fs: big, rot: -4, big: true }; x += w; }
      x = Rz.r[0];
      for (let i = at + half; i < at + k; i++) { const w = cw(L.chars[i].c, big); out[i] = { x: x + w / 2, y: yBig + big * .18, fs: big, rot: 4, big: true }; x += w; }
      // 其余字：小字一行，放在左侧大字上方
      const rest = L.chars.map((c, i) => i).filter(i => i < at || i >= at + k);
      if (rest.length) {
        let sf = H * .042;
        const rw = () => rest.reduce((a, i) => a + cw(L.chars[i].c, sf), 0);
        while (rw() > (Lz.r[2] - Lz.r[0]) && sf > H * .022) sf *= .9;
        let rx2 = Lz.r[2] - rw();
        for (const i of rest) { const w = cw(L.chars[i].c, sf); out[i] = { x: rx2 + w / 2, y: yBig - big * .75, fs: sf, rot: 0 }; rx2 += w; }
      }
      return { pos: out, fs: big };
    }

    // 给一句做规划：时间段内人物并集 → 空区 → 选模板 → 摆字；避开已规划的同屏句
    function planLine(L, ctx) {
      const k0 = ctx.k0 || 0;
      const W = api.W(), H = api.H();
      const tEnd = Math.min(L.gone, (L.exitAt || L.t1 || L.vis0) + 1.5);
      const occ = ctx.occOverride || occUnion(L.vis0, tEnd, ctx.cuts);
      const zones = freeZones(occ, W, H);
      // 同屏的已规划句占掉的区：区内去掉它们的框（简单做法：被占的区标记为用过，优先选别的区）
      const w0 = ctx.win ? ctx.win[0] : L.vis0, w1 = ctx.win ? ctx.win[1] : L.gone;
      const busy = ctx.busy.filter(b => b.t1 > w0 && b.t0 < w1);
      const used = new Set(busy.map(b => b.zone));
      const n = L.chars.length;
      const preset = ctx.preset;
      // 模板顺序：按句长、风格、关键词决定；同首歌轮换（第 i 句起始模板不同）
      const cand = [];
      if (L.kind === 'title') cand.push('banner', 'side');
      else {
        const weak = /^(って|して|から|けど|のに|だけ|まで|ない|ている|てる|です|ます|の|が|は|を|に|で|と|も|了|的|吧|呢|啊)$/;
        const kwOk = L.kw && [...L.kw].length >= 2 && [...L.kw].length <= 4 && !weak.test(L.kw);
        const climax = L.section === 'chorus' || L.section === 'final';
        // 对开大字是「重点」：副歌每隔一句用一次，或强度很高的关键词句；整首不超过 1/4 的句子
        if (kwOk && ctx.budget && ctx.budget.n > 0 && k0 === 0 && ((climax && (L.i % 2 === 0)) || L.intensity > .7)) cand.push('flank');
        const lyrical = /ballad|ancient|jazz/.test(preset);
        const rot = lyrical ? ['vertical', 'side', 'stack'] : ['stack', 'side', 'stack'];
        let first = rot[(L.i * 7 + (ctx.seed % 5)) % rot.length];
        if (first === 'vertical' && /[A-Za-z]{2}/.test(L.chars.map(c => c.c).join(''))) first = 'stack';
        cand.push(first, 'side', 'stack');
        const latinLine = /[A-Za-z]{2}/.test(L.chars.map(c => c.c).join(''));
        if (n <= 8 && !latinLine) cand.push('vertical');
        cand.push('banner');
      }
      const order = zones.slice().sort((a, b) => (used.has(a.id) - used.has(b.id)) || ((b.r[2] - b.r[0]) * (b.r[3] - b.r[1]) - (a.r[2] - a.r[0]) * (a.r[3] - a.r[1])));
      const fs0 = H * ctx.fsBase * (.9 + .3 * L.intensity) * (L.kind === 'title' ? 1.5 : 1);
      // 校验：每个字的框被人物覆盖的平均比例 ≤ 0.22，且任何一个字不超出屏幕
      const ok = pos => {
        let cov = 0, k = 0;
        for (const q of pos) {
          if (!q) continue;
          const r = [q.x - q.fs * .5, q.y - q.fs * .5, q.x + q.fs * .5, q.y + q.fs * .5];
          if (r[0] < 0 || r[2] > W || r[1] < 0 || r[3] > H) return false;
          const c1 = coverFrac(occ, r, W, H);
          if (c1 > (ctx.loose ? .7 : .5)) return false;     // 单个字被挡一半以上就不行（挡一点可以）
          cov += c1; k++;
        }
        return k > 0 && cov / k <= (ctx.loose ? .3 : .15);
      };
      const minFs = ctx.loose ? .026 : .034;
      for (const tpl of cand) {
        if (tpl === 'flank') {
          if (used.has('L') || used.has('R')) continue;
          const r = fitFlank(L, zones, { font: L.font }, W, H);
          if (r && ok(r.pos)) { ctx.budget.n--; return { tpl, zone: 'LR', pos: r.pos, occ }; }
          continue;
        }
        for (const z of order) {
          if (tpl === 'banner' && z.id !== 'T') continue;
          if (used.has(z.id)) {
            // 同区已有句子：只在区的另一半放（上下分）
            const r = z.r.slice();
            const taken = busy.filter(b => b.zone === z.id && b.box).map(b => [b.box[1] - H * .03, b.box[3] + H * .03]).sort((a, b) => a[0] - b[0]);
            let best = null, cur = r[1];
            for (const [a, b2] of taken) { if (a - cur > (best ? best[1] - best[0] : 0)) best = [cur, a]; cur = Math.max(cur, b2); }
            if (r[3] - cur > (best ? best[1] - best[0] : 0)) best = [cur, r[3]];
            if (!best) continue;
            r[1] = best[0]; r[3] = best[1];
            if (r[3] - r[1] < H * .12) continue;
            const res = fit(L.chars, { id: z.id, r }, tpl, { font: L.font, fs: fs0, minFs });
            if (res && ok(res.pos)) return { tpl, zone: z.id, pos: res.pos, occ };
            continue;
          }
          const res = fit(L.chars, z, tpl, { font: L.font, fs: fs0, minFs });
          if (res && ok(res.pos)) return { tpl, zone: z.id, pos: res.pos, occ };
        }
      }
      return null;
    }

    function planLineSafe(L, ctx) {
      let p = planLine(L, ctx);
      if (p) return p;
      const t0 = L.t0 || L.vis0;
      p = planLine(L, Object.assign({}, ctx, { occOverride: occUnion(t0 - .2, t0 + .6, ctx.cuts) }));
      if (p) return p;
      return planLine(L, Object.assign({}, ctx, { occOverride: occUnion(t0 - .2, t0 + .6, ctx.cuts), loose: true }));
    }
    function boxOf(pos) {
      let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
      for (const q of pos) if (q) { const hw = q.fs * .6, hh = q.fs * .6; x0 = Math.min(x0, q.x - hw); x1 = Math.max(x1, q.x + hw); y0 = Math.min(y0, q.y - hh); y1 = Math.max(y1, q.y + hh); }
      return [x0, y0, x1, y1];
    }
    function planAll(prep, ctx) {
      plans = new Map();
      const busy = [];
      const lines = prep.filter(L => L.kind !== 'flash').slice().sort((a, b) => a.vis0 - b.vis0);
      const budget = { n: Math.max(2, Math.floor(lines.length / 4)) };
      for (const L of lines) {
        // 第三十八轮：长句拆两半先后显示（前半唱完前只摆前半，后半开唱时换成后半）——
        // 特写镜头下长句没地方放、压人物的主因。拆在词/标点边界附近，两半各自规划位置。
        const n = L.chars.length;
        const latin = /[A-Za-z]{2}/.test(L.chars.map(c => c.c).join(''));
        const words = latin ? L.chars.filter(c => c.wEnd).length : 0;
        let split = -1;
        if (L.kind !== 'title' && ((!latin && n >= 11) || (latin && words >= 6))) {
          let best = 1e9;
          for (let i = 3; i < n - 3; i++) {
            const ch = L.chars[i - 1];
            const boundary = ch.wEnd || /[、，,。！!？?　\s]/.test(ch.c);
            const d = Math.abs(i - n / 2) - (boundary ? n * .2 : 0);
            if ((latin ? ch.wEnd : true) && d < best) { best = d; split = i; }
          }
        }
        let tSplit = split > 0 ? Math.max(L.vis0 + .6, (+L.chars[split].cs || L.t0) - .2) : 1e9;
        if (split > 0 && !(tSplit < L.gone - .6)) { split = -1; tSplit = 1e9; }
        const halves = split > 0 ? [[0, split], [split, n]] : [[0, n]];
        const tEnd = L.gone;
        const cuts = (ctx.cuts || []).filter(c => c > L.vis0 + .3 && c < tEnd - .3);
        const bset = [L.vis0].concat(cuts, split > 0 && tSplit < tEnd - .3 ? [tSplit] : [], [tEnd]).sort((a, b) => a - b);
        const bounds = bset.filter((x, i) => i === 0 || x - bset[i - 1] > .05);
        const segs = [];
        for (let k = 0; k + 1 < bounds.length; k++) {
          const a = bounds[k], b = bounds[k + 1];
          const hv = halves[(split > 0 && a >= tSplit - .01) ? 1 : 0];
          const sub0 = L.chars.slice(hv[0], hv[1]);
          const LL = split > 0 ? Object.assign({}, L, { chars: sub0, kw: (L.kw && sub0.map(c => c.c).join('').includes(L.kw)) ? L.kw : null }) : L;
          // 读的时间（唱完后 1.5 s）以外的尾段：人物遮一点没关系，用宽松判定
          const loose = a > (L.exitAt || L.t1) + 1.5;
          const sub = Object.assign({}, ctx, { k0: k, busy, budget, win: [a, b], occOverride: occUnion(a, b, []), loose });
          let p = planLine(LL, sub) || planLine(LL, Object.assign(sub, { loose: true }));
          if (!p) continue;
          if (split > 0) {
            // 子句的位置映射回整句：另一半的字标记隐藏
            const vis = p.pos.filter(Boolean);
            const fs0 = vis.length ? vis[0].fs : 20;
            const full = L.chars.map(() => ({ x: 0, y: 0, fs: fs0, rot: 0, hide: true }));
            p.pos.forEach((q, j) => { if (q) full[hv[0] + j] = q; });
            p = Object.assign({}, p, { pos: full });
          }
          p.box = boxOf(p.pos.filter(q => q && !q.hide));
          segs.push({ t0: a, t1: b, pos: p.pos, tpl: p.tpl, zone: p.zone, box: p.box });
          if (p.zone === 'LR') { busy.push({ t0: a, t1: b, zone: 'L', box: p.box }); busy.push({ t0: a, t1: b, zone: 'R', box: p.box }); }
          else busy.push({ t0: a, t1: b, zone: p.zone, box: p.box });
        }
        if (!segs.length) continue;
        // 段内没规划成的时间：沿用相邻段
        plans.set(L, { tpl: segs[0].tpl, zone: segs[0].zone, pos: segs[0].pos, box: segs[0].box, segs });
      }
      return plans;
    }

    return { planAll, get plans() { return plans; }, get stats() { return { planned: plans.size }; }, STYLE_SETS };
  }

  global.NekoStagePlanner = { create, STYLE_SETS };
})(window);
