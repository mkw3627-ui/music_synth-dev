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

  const DUR_BY = { rise: .8, ink: .85, slam: .36, scatter: .9, glitch: .5, type: .06, drop: .7, wipe: .45 };

  function charW(c, fs) { return /[\x00-\x7f]/.test(c) ? fs * .62 : fs; }

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
        drop: beat * 1.5, scatter: beat * 2, slam: beat, glitch: beat * 1.2
      };
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
        chars.forEach((ch, i) => {
          const x = L.chars[i] || [ch.c, L.t0, L.t1, 0, 0];
          ch.cs = +x[1]; ch.ce = +x[2]; ch.stress = +x[3] || 0; ch.kw = !!+x[4];
          ch.long = i === chars.length - 1 && (ch.ce - ch.cs) > .55;
          ch.on = false; ch.glc = false;
        });
        const n = chars.length;
        const reveal = kind === 'title' ? 'rise' : combo.reveal;
        const lead = (leadBy[reveal] || 1) * (combo.slow ? 1.6 : 1);
        const appear = L.t0 - lead;
        const dur = DUR_BY[reveal] || .6;
        const stagger = reveal === 'type' ? 'cue' : combo.stagger;
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
      const lay = (c.layout === 'v' && cfg.verticalOk === false) ? 'h' : c.layout;

      if (lay === 'center' || L.kind === 'title') {
        fs = H * (L.kind === 'title' || c.big ? .11 : .058);
        const tot = L.chars.reduce((a, ch) => a + charW(ch.c, fs) * 1.08, 0);
        let x = W * .5 - tot / 2;
        const y = H * (L.kind === 'title' ? .4 : .2);
        L.chars.forEach(ch => { const w = charW(ch.c, fs) * 1.08; put(ch, x + w / 2, y, fs * (ch.kw ? cfg.kwScale : 1), 0); x += w; });
      } else if (lay === 'giant') {
        let kw = L.kw;
        const flat = L.words.join('');
        if (!kw) kw = L.words[L.words.length - 1] || '';
        const at = flat.indexOf(kw);
        const gfs = H * (L.combo.front ? .3 : .28), k = kw.length;
        const xs = k === 1
          ? [U.h01(L.i * 3.7 + show.seed) < .5 ? .24 : .76]
          : Array.from({ length: k }, (_, j) => U.lerp(k > 3 ? .13 : .2, k > 3 ? .87 : .8, k > 1 ? j / (k - 1) : 0));
        const rfs = H * .045;
        const rest = L.chars.filter((ch, i) => i < at || i >= at + k);
        const rtot = rest.reduce((a, ch) => a + charW(ch.c, rfs) * 1.1, 0);
        let rxp = W * .5 - rtot / 2;
        L.chars.forEach((ch, i) => {
          if (i >= at && i < at + k) {
            put(ch, W * xs[i - at], H * (.42 + (U.h01(i + L.i) - .5) * .08), gfs * (/[\x00-\x7f]/.test(ch.c) ? .8 : 1), (U.h01(i * 3 + L.i) - .5) * 8);
            ch.giant = true;
          } else {
            const w = charW(ch.c, rfs) * 1.1;
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
        const tot = ws.reduce((a, w) => a + w * 1.05, 0);
        const Rr = W * .45, span = tot / Rr;
        let a = -span / 2;
        const cx = rx * W, cy = ry * H + Rr;
        L.chars.forEach((ch, i) => {
          const w = ws[i] * 1.05;
          const aa = a + w / 2 / Rr;
          put(ch, cx + Math.sin(aa) * Rr, cy - Math.cos(aa) * Rr, fs * (ch.kw ? cfg.kwScale : 1), aa * 57.3);
          a += w / Rr;
        });
      } else {
        // 横排：超过 max_chars_per_row 就分行（每行各自缩放到 maxW 内）
        const maxW = W * .42, M = cfg.maxChars || 40;
        const rows = [];
        if (n > M) { for (let i = 0; i < n; i += M) rows.push(L.chars.slice(i, i + M)); }
        else rows.push(L.chars);
        const sized = rows.map(row => {
          let tot = row.reduce((a, ch) => a + charW(ch.c, fs) * 1.04, 0);
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
            const w = charW(ch.c, f) * 1.04;
            put(ch, x + w / 2, y, f * (ch.kw ? cfg.kwScale : 1), ch.jit[0]);
            x += w;
          });
        });
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
        case 'ink': { const q = Math.pow(1 - p, 1.5); blur = 22 * q; s *= 1 + .4 * q; op = Math.pow(p, .7); x += (ch.x - L.cx) * .25 * q; break; }
        case 'slam': { s *= p < 1 ? U.lerp(3.4, 1, U.easeOutBack(p)) : 1; op = U.clamp(p * 5); blur = 7 * (1 - p); break; }
        case 'scatter': { const q = 1 - U.springP(p); x += ch.scat[0] * W * q; y += ch.scat[1] * H * q; rot += ch.scat[2] * q; op = U.clamp(p * 2); blur = 6 * q; break; }
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
        case 'drop': { y -= H * .35 * (1 - U.bounceOut(p)); op = U.clamp(p * 3); break; }
        case 'wipe': { op = U.easeOut(p); x -= 40 * (1 - U.easeOut(p)); s *= U.lerp(.9, 1, p); blur = 4 * (1 - p); break; }
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
            x += dx / d * W * .3 * U.easeOut(k2);
            y += dy / d * W * .3 * U.easeOut(k2);
            op *= 1 - k2; blur += 6 * k2;
          } else if (L.fadeAt) op *= 1 - U.sstep(L.fadeAt, L.fadeAt + 1.2, t);
        } else {
          const q = U.clamp((t - L.exitAt) / .7);
          if (ex === 'burst') {
            const dx = ch.x - L.cx + ch.scat[0] * 80, dy = ch.y - L.cy + ch.scat[1] * 80, d = Math.hypot(dx, dy) || 1;
            x += dx / d * W * .22 * U.easeOut(q);
            y += dy / d * W * .22 * U.easeOut(q);
            rot += ch.scat[2] * q; op *= 1 - q; blur += 3 * q; s *= 1 + .3 * q;
          } else if (ex === 'push') {
            y -= H * .14 * U.easeIn(q); op *= 1 - q; blur += 4 * q;
          } else {
            blur += 14 * q; s *= 1 + .25 * q; op *= 1 - q;
          }
        }
      }
      out.x = x; out.y = y; out.s = s; out.rot = rot; out.op = op; out.blur = blur; out.br = br; out.glitch = glitch;
      out.on = singing || (sung && t < ch.ce + .25);
      return out;
    }

    function applyEl(el, s, skew, extraS, extraOp, extraBlur) {
      const op = s.op * (extraOp == null ? 1 : extraOp);
      if (op <= .004) { el.style.opacity = 0; return; }
      el.style.opacity = op.toFixed(3);
      el.style.transform = 'translate3d(' + s.x.toFixed(1) + 'px,' + s.y.toFixed(1) + 'px,0) translate(-50%,-50%) rotate('
        + s.rot.toFixed(2) + 'deg) skewX(' + skew + 'deg) scale(' + (s.s * (extraS == null ? 1 : extraS)).toFixed(3) + ')';
      const b = s.blur + (extraBlur || 0);
      el.style.filter = b > .4
        ? 'blur(' + b.toFixed(1) + 'px) brightness(' + s.br.toFixed(2) + ')'
        : (Math.abs(s.br - 1) > .02 ? 'brightness(' + s.br.toFixed(2) + ')' : 'none');
    }

    function render(t) {
      for (const L of prep) {
        const vis = t >= L.vis0 && t <= L.gone;
        if (!vis) { if (L.g.style.display !== 'none') L.g.style.display = 'none'; continue; }
        if (L.g.style.display !== 'block') L.g.style.display = 'block';
        for (let i = 0; i < L.chars.length; i++) {
          const ch = L.chars[i];
          const s = charState(L, ch, i, t, st);
          applyEl(ch.el, s, cfg.skew);
          if (s.on !== ch.on) { ch.on = s.on; ch.el.classList.toggle('on', s.on); }
          if (s.glitch !== ch.glc) { ch.glc = s.glitch; ch.el.classList.toggle('gl', s.glitch); }
          for (let gi = 0; gi < ch.ghosts.length; gi++) {
            const g2 = charState(L, ch, i, t - .09 * (gi + 1), {});
            applyEl(ch.ghosts[gi], g2, cfg.skew, 1.25 + gi * .3, .28 - gi * .12, 5 + gi * 6);
          }
        }
      }
    }

    /* ------------------------------ 字幕条 ------------------------------ */
    let subCache = '';
    function subtitle(t, on) {
      if (!els.sub) return;
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

    const api = {
      build, layout, render, subtitle, lineAt,
      get prep() { return prep; },
      get anchors() { return anchors; },
      charW
    };
    return api;
  }

  global.NekoStageLyrics = { create };
})(window);
