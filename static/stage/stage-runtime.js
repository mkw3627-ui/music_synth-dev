/* =============================================================================
 * 「为她而唱」舞台运行时（插件内）
 *
 * 入口：
 *   NekoStage.start({ show, clock, model, container, root, fps, hud, sub })
 *   NekoStage.stop()
 *   NekoStage.renderAt(t)     —— 纯函数：同一个 show + 同一个 t → 同一画面
 *   NekoStage.seek / play / pause / currentT / canvas / stats / info
 *
 * 设计约束（来自《舞台编排实施计划_v3》）：
 *   · 编排全部来自 show.json（组合 / 镜头 / 场景 / 事件），运行时不生成编排；
 *   · 人物只写传入模型对象的变换与参数，不新建、不替换宿主模型；stop() 完整还原；
 *   · 背景后备缓冲宽 ≤1400px、渲染上限 30fps、页面隐藏时停止渲染；
 *   · 人物姿态在 start 时一次性烘焙成时间采样，保证任意跳转画面一致（物理/呼吸关闭）。
 * ========================================================================== */
(function (global) {
  'use strict';
  const U = global.NekoStageU;
  const Scenes = global.NekoStageScenes;
  const Lyrics = global.NekoStageLyrics;

  /* ------------------------------ 常量表 ------------------------------ */
  // font-id → 本机字体族。第 2 阶段由 static/fonts + fonts.json 接管。
  const FONTS = {
    kaiti: '"KaiTi","STKaiti","楷体",serif',
    hei: '"SimHei","Microsoft YaHei",sans-serif',
    'hei-heavy': '"Microsoft YaHei","PingFang SC","Heiti SC",sans-serif',
    serif: '"SimSun","Songti SC",serif',
    mono: 'Consolas,"Courier New",monospace',
    system: '"Microsoft YaHei","PingFang SC",sans-serif'
  };
  // 风格预设里的渲染性格（动作幅度 / 摇摆周期 / 弹跳 / 段落默认情绪 / 基础字号）
  const PRESETS = {
    ballad: { fsBase: .074, motionK: .6, swayBeats: 4, bounceK: .25, emo: { intro: 'tender', verse: 'longing', pre: 'tender', chorus: 'tender', bridge: 'sad', final: 'joy', outro: 'tender' } },
    pop: { fsBase: .07, motionK: .85, swayBeats: 2, bounceK: .9, emo: { intro: 'playful', verse: 'playful', pre: 'joy', chorus: 'joy', bridge: 'playful', final: 'joy', outro: 'tender' } },
    edm: { fsBase: .068, motionK: .95, swayBeats: 2, bounceK: 1.1, emo: { intro: 'determined', verse: 'determined', pre: 'proud', chorus: 'proud', bridge: 'longing', final: 'proud', outro: 'determined' } },
    rock: { fsBase: .072, motionK: 1, swayBeats: 2, bounceK: .8, emo: { intro: 'determined', verse: 'proud', pre: 'proud', chorus: 'proud', bridge: 'longing', final: 'proud', outro: 'determined' } },
    cute: { fsBase: .072, motionK: .8, swayBeats: 3, bounceK: .7, emo: { intro: 'playful', verse: 'playful', pre: 'joy', chorus: 'joy', bridge: 'playful', final: 'joy', outro: 'tender' } },
    ancient: { fsBase: .072, motionK: .55, swayBeats: 4, bounceK: .3, emo: { intro: 'tender', verse: 'longing', pre: 'tender', chorus: 'tender', bridge: 'sad', final: 'tender', outro: 'tender' } },
    jazz: { fsBase: .068, motionK: .7, swayBeats: 4, bounceK: .5, emo: { intro: 'playful', verse: 'playful', pre: 'joy', chorus: 'joy', bridge: 'tender', final: 'joy', outro: 'tender' } },
    dark: { fsBase: .07, motionK: .8, swayBeats: 4, bounceK: .4, emo: { intro: 'determined', verse: 'sad', pre: 'longing', chorus: 'proud', bridge: 'sad', final: 'proud', outro: 'determined' } }
  };
  const EMO = {
    joy: { smile: .6, open: .95, browY: .3, browF: .3, browA: 0, mouth: .6, cheek: .3 },
    tender: { smile: .35, open: .85, browY: .1, browF: .2, browA: 0, mouth: .35, cheek: .45 },
    longing: { smile: .08, open: .78, browY: .4, browF: -.2, browA: .5, mouth: .05, cheek: .1 },
    sad: { smile: 0, open: .65, browY: .5, browF: -.5, browA: .7, mouth: -.4, cheek: 0 },
    proud: { smile: .2, open: 1, browY: .2, browF: .4, browA: -.1, mouth: .4, cheek: 0 },
    determined: { smile: 0, open: 1, browY: -.3, browF: -.3, browA: -.4, mouth: .05, cheek: 0 },
    playful: { smile: .5, open: .92, browY: .25, browF: .4, browA: 0, mouth: .55, cheek: .35 }
  };
  const SHOT = { close: { sf: 4.0, headY: .05 }, medium: { sf: 2.75, headY: .085 }, base: { sf: 2.0, headY: .124 }, wide: { sf: 1.02, headY: .07 } };
  const CN = {
    shot: { close: '面部特写', medium: '近景', base: '半身', wide: '全身远景' },
    scene: { bokeh: '散景光斑', wave: '声线波纹', rings: '光环', beams: '舞台光束', led: 'LED 点阵', tunnel: '几何隧道' },
    entry: { iris: '圆形扩散', sweepL: '左侧扫入', sweepR: '右侧扫入', columns: '逐列点亮', flash: '闪白切入', fade: '淡入' },
    emo: { joy: '喜悦', tender: '温柔', longing: '思念', sad: '伤感', proud: '昂扬', determined: '坚定', playful: '俏皮' },
    gaze: { camera: '看镜头', far: '望向远处', down: '垂眸', side: '侧看', closed: '闭眼' },
    reveal: { rise: '深处浮出', ink: '墨迹晕开', wipe: '遮罩擦除', type: '逐字打出', drop: '坠落回弹', scatter: '碎片聚合', slam: '冲击砸入', glitch: '故障闪现' },
    stagger: { char: '逐字均匀', word: '按词', center: '中心扩散', random: '随机', cue: '卡唱词时间', whole: '整句' },
    layout: { h: '横排', v: '竖排', stack: '错落叠放', arc: '弧形', giant: '巨型关键词', center: '居中' },
    material: { solid: '实心', outline: '镂空描边', neon: '霓虹发光', grad: '渐变' },
    exit: { sink: '沉入背景', burst: '粒子散开', push: '被推走', dissolve: '溶解' }
  };
  const BAKE_FPS = 30;
  const MAX_BG_W = 1400;

  /* ------------------------- 字体资产（第 10 维） ------------------------- */
  // static/fonts/fonts.json 与舞台同走 /stage/ 路由；启动时预加载 primary/accent，
  // 全部就绪或 3 秒超时才开演。兜底：主字体 → 同 persona 第一个 → 系统字体。
  const FONT_TIMEOUT = 3000;
  let fontManifest = null;

  function stageBase() { return global.__NEKO_STAGE_BASE || './'; }

  async function fetchManifest() {
    if (fontManifest) return fontManifest;
    try {
      const res = await fetch(stageBase() + 'fonts.json', { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      fontManifest = Array.isArray(data) ? { fonts: data } : data;
    } catch (e) {
      fontManifest = { fonts: [], error: String((e && e.message) || e) };
    }
    return fontManifest;
  }

  function pickFontEntry(entries, id, persona) {
    const byId = entries.find(e => e && e.id === id);
    if (id && id !== 'sys-default' && byId) return { entry: byId, how: 'id' };
    const byPersona = entries.find(e => e && e.persona === persona);
    if (byPersona) return { entry: byPersona, how: (id === 'sys-default' || !byId) ? 'persona' : 'persona' };
    return null;
  }

  async function ensureFont(sel, loaded) {
    if (!sel) return null;
    const e = sel.entry;
    if (loaded[e.id]) return loaded[e.id];
    const face = new FontFace(e.family, 'url("' + stageBase() + e.file + '")', { weight: String(e.weight || 400) });
    const timeout = new Promise(res => setTimeout(() => res('timeout'), FONT_TIMEOUT));
    const got = await Promise.race([face.load().then(() => 'ok').catch(err => 'error:' + err), timeout]);
    if (got === 'ok') {
      document.fonts.add(face);
      loaded[e.id] = { id: e.id, family: e.family, weight: e.weight };
      return loaded[e.id];
    }
    return { id: e.id, failed: true, why: got };
  }

  // 同 persona 的全部登记字体：主选在前，其余按 fonts.json 顺序作为按字符回退
  // （F-1 返工：cute_round / bold_pop 的假名与日文汉字由第二款字体兜住）
  function personaChain(entries, sel, persona) {
    if (!sel) return [];
    const out = [sel.entry];
    for (const e of entries) {
      if (e && e.persona === persona && e.id !== sel.entry.id) out.push(e);
    }
    return out;
  }

  // 只读 show + fonts.json，不碰渲染；预览页在 start 前 await 它
  async function loadFonts(show) {
    const tf = (show.style && show.style.typeface) || {};
    const persona = tf.persona || '';
    const manifest = await fetchManifest();
    const entries = manifest.fonts || [];
    const loaded = {};
    const psel = pickFontEntry(entries, tf.primary, persona);
    const asel = pickFontEntry(entries, tf.accent, persona) || psel;
    const pChain = personaChain(entries, psel, persona);
    const aChain = personaChain(entries, asel, persona);
    const all = [];
    for (const e of pChain.concat(aChain)) if (!all.some(x => x.id === e.id)) all.push(e);
    await Promise.all(all.map(e => ensureFont({ entry: e }, loaded)));
    const ok = x => !!(x && !x.failed);
    const fams = c => c.filter(e => ok(loaded[e.id])).map(e => '"' + e.family + '"');
    const pFams = fams(pChain), aFams = fams(aChain);
    const pGot = psel ? loaded[psel.entry.id] : null;
    const aGot = asel ? loaded[asel.entry.id] : null;
    return {
      primary: ok(pGot) ? pGot : null,
      accent: ok(aGot) ? aGot : (ok(pGot) ? pGot : null),
      primaryStack: pFams.length ? pFams.join(',') + ',' + FONTS.system : null,
      accentStack: aFams.length ? aFams.join(',') + ',' + FONTS.system : null,
      primaryChain: pFams.map(s => s.slice(1, -1)),
      accentChain: aFams.map(s => s.slice(1, -1)),
      primaryId: tf.primary || '', accentId: tf.accent || '',
      primaryHow: psel ? psel.how : 'none',
      accentHow: asel ? asel.how : 'none',
      persona, rules: tf.rules || {}, loaded: Object.keys(loaded).length
    };
  }

  function resolveCfg(show, fonts) {
    const tf = (show.style && show.style.typeface) || {};
    const pr = fonts && fonts.primary, ac = fonts && fonts.accent;
    const legacy = FONTS[tf.primary] || null;
    const rules = Object.assign({}, tf.rules || {});
    return {
      font: pr ? (fonts.primaryStack || ('"' + pr.family + '",' + FONTS.system)) : (legacy || FONTS.system),
      accentFont: ac ? (fonts.accentStack || ('"' + ac.family + '",' + FONTS.system)) : null,
      weight: tf.weight || 400,
      skew: tf.skew || 0,
      fsBase: S.preset.fsBase,
      kwScale: rules.kw_scale || 1.25,
      preset: show.style.preset,
      persona: (fonts && fonts.persona) || tf.persona || '',
      rules,
      decor: Array.isArray(rules.decor) ? rules.decor.slice() : [],
      maxChars: Math.max(1, Math.round(+rules.max_chars_per_row || 40)),
      verticalOk: rules.vertical_ok !== false,
      fontInfo: fonts ? {
        primary: pr ? pr.family : null, accent: ac ? ac.family : null,
        primaryId: fonts.primaryId, accentId: fonts.accentId,
        primaryHow: fonts.primaryHow, accentHow: fonts.accentHow,
        primaryChain: fonts.primaryChain, accentChain: fonts.accentChain,
        primaryStack: fonts.primaryStack, accentStack: fonts.accentStack,
        loaded: fonts.loaded
      } : null
    };
  }

  /* ------------------------------ 运行时状态 ------------------------------ */
  const S = {
    api: null, show: null, root: null, wrap: null, els: null,
    W: 1280, H: 720, BW: 640, BH: 360,
    beat: .5, bar: 2, dur: 0, seed: 0,
    sections: [], shots: [], scenes: [], events: [],
    preset: PRESETS.pop, cfg: null, lyrics: null,
    model: null, container: null, modelSaved: null, poseOut: null,
    clock: null, playing: false, pausedT: 0, wall0: 0, show0: 0,
    raf: 0, lastPaint: 0, dirty: true, fps: 30, hidden: false,
    hud: true, subOn: true, statsOn: false, stats: [],
    lastT: 0, lastInfo: null
  };

  /* ------------------------------ show 派生 ------------------------------ */
  function sectionAt(t) {
    for (const s of S.sections) if (t < s.t1) return s;
    return S.sections[S.sections.length - 1];
  }
  function pulseAt(t) {
    const bp = t / S.beat, ph = U.fract(bp);
    return Math.exp(-ph * 6) * (U.fract(bp / 4) < .25 ? 1.35 : 1);
  }
  function intensityAt(t) {
    const iv = S.show.intensity;
    if (iv && iv.v && iv.v.length) {
      const hop = iv.hop || .1;
      const f = t / hop;
      const i = U.clamp(Math.floor(f), 0, iv.v.length - 2);
      const a = U.clamp(f - i, 0, 1);
      const i2 = Math.min(i + 1, iv.v.length - 1);
      return U.lerp(iv.v[i], iv.v[i2], a);
    }
    const s = sectionAt(t);
    return s.energy != null ? s.energy : .5;
  }
  function evAmp(t, type, decay) {
    let a = 0;
    for (const e of S.events) {
      if (e.t > t) break;
      if (e.type !== type) continue;
      const d = t - e.t;
      if (d < 1.5) a = Math.max(a, (e.amp || 0) * Math.exp(-d * decay));
    }
    return a;
  }
  function cameraAt(t) {
    let i = 0;
    for (let k = 0; k < S.shots.length; k++) if (S.shots[k].t <= t) i = k;
    const s = S.shots[i], p = S.shots[Math.max(0, i - 1)];
    const base = SHOT[s.kind] || SHOT.base;
    let sf = base.sf, headY = base.headY, cx = s.cx, rot = s.rot;
    const ease = (s.ease_beats || 4) * S.beat;
    if (!s.cut && i > 0 && ease > 0) {
      const u = U.easeInOut(U.clamp((t - s.t) / ease));
      const pb = SHOT[p.kind] || SHOT.base;
      sf = U.lerp(pb.sf, base.sf, u);
      headY = U.lerp(pb.headY, base.headY, u);
      cx = U.lerp(p.cx, s.cx, u);
      rot = U.lerp(p.rot || 0, s.rot || 0, u);
    }
    const punch = evAmp(t, 'punch', 5);
    sf *= 1 + punch;
    headY -= punch * .5;
    const sh = (s.shake || 0) * intensityAt(t);
    const hx = (Math.sin(t * 1.7) * .6 + Math.sin(t * 3.1 + 1) * .4) * sh * S.W * .004;
    const hy = (Math.sin(t * 2.3 + 2) * .6 + Math.sin(t * 3.7) * .4) * sh * S.H * .004;
    return { sf, headY, cx, rot, hx, hy, kind: s.kind, cut: s.cut };
  }

  /* ------------------------------ 人物姿态：目标 → 烘焙 ------------------------------ */
  // 纯目标函数（不含平滑），对应演示页 poseAt 的上半段
  function poseTarget(t) {
    const preset = S.preset, beat = S.beat;
    const I = intensityAt(t), sec = sectionAt(t);
    const L = S.lyrics.lineAt(t);
    let env = .8;
    if (L) {
      const u = U.clamp((t - L.t0) / (L.exitAt - L.t0));
      env = .75 + .25 * U.sstep(0, .3, u) - .15 * U.sstep(.85, 1, u);
    }
    const A = preset.motionK * (.25 + .95 * (L ? Math.max(I, L.intensity) : I)) * env;
    const sw = preset.swayBeats, TAU = 6.2832;
    const bp = t / beat;
    const sway = Math.sin(TAU * bp / sw), bounce = -Math.abs(Math.sin(Math.PI * bp)) + .5;
    const tg = {
      ParamAngleX: A * 13 * sway,
      ParamAngleY: A * preset.bounceK * 7 * bounce,
      ParamAngleZ: A * 7 * Math.sin(TAU * bp / (sw * 2) + .7),
      ParamBodyAngleX: A * 6 * Math.sin(TAU * bp / sw - .5),
      ParamBodyAngleY: A * preset.bounceK * 4 * bounce,
      ParamBodyAngleZ: A * 4 * Math.sin(TAU * bp / sw + 1.3),
      ParamBreath: .5 + .5 * Math.sin(t * 1.6)
    };
    const em = EMO[L ? L.emotion : (preset.emo[sec.id] || 'tender')] || EMO.tender;
    const emK = .45 + .55 * (L ? L.intensity : I);
    let open = U.lerp(1, em.open, emK), browY = em.browY * emK, eyeX = 0, eyeY = 0;
    const gz = L ? L.gaze : (sec.id === 'intro' || sec.id === 'outro' ? 'down' : 'far');
    const gs = (L ? L.i : 0) % 2 ? 1 : -1;
    if (gz === 'far') { eyeX = .25 * gs; eyeY = .35; tg.ParamAngleY += 4; tg.ParamAngleX += 6 * gs; }
    else if (gz === 'down') { eyeY = -.45; tg.ParamAngleY -= 6; open = Math.min(open, .7); }
    else if (gz === 'side') { eyeX = .55 * gs; tg.ParamAngleX += 9 * gs; }
    else if (gz === 'closed') { open = .05; tg.ParamAngleY -= 3; }

    let mo = 0, mf = 0;
    if (L) {
      if (t < L.t0 && t > L.t0 - .35) {
        const k = 1 - (L.t0 - t) / .35;
        tg.ParamAngleY += 3 * k; tg.ParamBodyAngleY += 2 * k;
        open = Math.min(1, open + .1 * k);
      }
      const last = L.chars[L.chars.length - 1];
      if (last && last.long && t >= last.cs && t < last.ce) {
        const u = (t - last.cs) / (last.ce - last.cs);
        const k = U.sstep(0, .35, u) * (1 - U.sstep(.92, 1, u));
        tg.ParamAngleY += 9 * k * (S.preset === PRESETS.edm ? .6 : 1);
        tg.ParamAngleZ += 5 * gs * k + .9 * Math.sin(t * TAU * 5.6) * U.sstep(.3, .5, u);
        open = U.lerp(open, S.preset === PRESETS.ballad ? .35 : .55, k);
        browY += .5 * k; eyeY = U.lerp(eyeY, .3, k);
      }
      if (t > L.exitAt && t < L.exitAt + .6) {
        const k = Math.sin((t - L.exitAt) / .6 * Math.PI);
        tg.ParamAngleY -= 4 * k; eyeX = U.lerp(eyeX, -.3 * gs, k);
      }
      for (const ch of L.chars) {
        if (ch.kw && t >= ch.cs && t < ch.cs + .3) { tg.ParamAngleY -= 5 * Math.sin((t - ch.cs) / .3 * Math.PI); break; }
      }
      for (const ch of L.chars) {
        if (t >= ch.cs && t < ch.ce) {
          const u = (t - ch.cs) / Math.max(.05, ch.ce - ch.cs);
          mo = (.45 + .45 * U.h01(ch.c.codePointAt(0))) * (ch.long ? .85 + .15 * Math.sin(t * 35) : 1) * U.sstep(0, 1, (t - ch.cs) / .06);
          mf = (U.h01(ch.c.codePointAt(0) * 1.7) - .3);
          break;
        }
      }
    }
    // 副歌入口：前一拍蓄力，进副歌直视镜头、身体大摆
    for (const id of ['chorus', 'final']) {
      const sec0 = S.sections.find(s => s.id === id);
      if (!sec0) continue;
      const d = t - sec0.t0;
      if (d > -beat && d < 0) {
        const k = 1 + d / beat;
        tg.ParamAngleY -= 6 * k; tg.ParamBodyAngleY -= 3 * k;
        open = U.lerp(open, .7, k);
      }
      if (d >= 0 && d < beat * 2) {
        const k = Math.exp(-d * 2);
        tg.ParamAngleY += 10 * k; tg.ParamBodyAngleX += 8 * k * gs;
        open = 1; eyeX *= 1 - k; eyeY *= 1 - k; browY += .4 * k;
      }
    }
    if (sec.id === 'outro') {
      const u = U.clamp((t - sec.t0) / (sec.t1 - sec.t0));
      open = U.lerp(open, .1, U.sstep(.6, .95, u));
      tg.ParamAngleY -= 8 * U.sstep(.6, 1, u);
    }
    tg.ParamEyeLSmile = tg.ParamEyeRSmile = em.smile * emK;
    tg.ParamBrowLY = tg.ParamBrowRY = browY;
    tg.ParamBrowLForm = tg.ParamBrowRForm = em.browF * emK;
    tg.ParamBrowLAngle = tg.ParamBrowRAngle = em.browA * emK;
    tg.ParamCheek = em.cheek * emK;
    return { tg, open, eyeX, eyeY, mouth: mo, form: mf + em.mouth * .5, A, gz, emo: L ? L.emotion : (preset.emo[sec.id] || 'tender') };
  }

  // 把平滑滤波一次性烘焙进采样表：之后任意 t 都只是插值，不再带历史状态
  function bakePose() {
    const dt = 1 / BAKE_FPS;
    const N = Math.ceil(S.dur * BAKE_FPS) + 2;
    const names = Object.keys(poseTarget(0).tg);
    const arr = {};
    for (const k of names) arr[k] = new Float32Array(N);
    arr._open = new Float32Array(N); arr._ex = new Float32Array(N); arr._ey = new Float32Array(N);
    arr._mouth = new Float32Array(N); arr._form = new Float32Array(N);
    const P = {};
    let open, ex, ey, mouth, form;
    for (let n = 0; n < N; n++) {
      const t = n * dt, T = poseTarget(t);
      for (const k of names) {
        const tau = /Eye|Brow|Cheek/.test(k) ? .25 : .12;
        P[k] = P[k] === undefined ? T.tg[k] : P[k] + (T.tg[k] - P[k]) * (1 - Math.exp(-dt / tau));
        arr[k][n] = P[k];
      }
      open = open === undefined ? T.open : open + (T.open - open) * (1 - Math.exp(-dt / .12));
      ex = ex === undefined ? T.eyeX : ex + (T.eyeX - ex) * (1 - Math.exp(-dt / .18));
      ey = ey === undefined ? T.eyeY : ey + (T.eyeY - ey) * (1 - Math.exp(-dt / .18));
      mouth = mouth === undefined ? T.mouth : mouth + (T.mouth - mouth) * (1 - Math.exp(-dt / (T.mouth > mouth ? .045 : .12)));
      form = form === undefined ? T.form : form + (T.form - form) * (1 - Math.exp(-dt / .09));
      arr._open[n] = open; arr._ex[n] = ex; arr._ey[n] = ey; arr._mouth[n] = mouth; arr._form[n] = form;
    }
    S.poseTrack = { arr, names, N, fps: BAKE_FPS };
  }

  function poseAt(t) {
    const T = S.poseTrack;
    if (!T) return null;
    const f = U.clamp(t, 0, (T.N - 1) / T.fps) * T.fps;
    const i0 = Math.min(T.N - 1, Math.floor(f)), i1 = Math.min(T.N - 1, i0 + 1), a = f - i0;
    const val = k => T.arr[k][i0] + (T.arr[k][i1] - T.arr[k][i0]) * a;
    const out = {};
    for (const k of T.names) out[k] = val(k);
    const open = val('_open'), ex = val('_ex'), ey = val('_ey');
    // 微扫视：跳变、不平滑（只由 t 决定）
    const seg = Math.floor(t / (.7 + .9 * U.h01(Math.floor(t / 1.6))));
    const sx = (U.h01(seg * 3.3) - .5) * .18, sy = (U.h01(seg * 5.1) - .5) * .12;
    // 眨眼：放在 show.json 的 blink 事件上
    let blink = 0;
    for (const e of S.events) {
      if (e.t > t) break;
      if (e.type !== 'blink') continue;
      const d = t - e.t;
      if (d >= 0 && d < .17) { blink = Math.sin(d / .17 * Math.PI); break; }
    }
    out.ParamEyeLOpen = open * (1 - blink);
    out.ParamEyeROpen = open * (1 - blink * .97);
    out.ParamEyeBallX = U.clamp(ex + sx, -.8, .8);
    out.ParamEyeBallY = U.clamp(ey + sy, -.8, .8);
    out.ParamMouthOpenY = val('_mouth');
    out.ParamMouthForm = U.clamp(val('_form'), -1, 1);
    return out;
  }

  /* ------------------------------ 模型绑定 ------------------------------ */
  function setModel(model, container) {
    if (!model) return;
    unbindModel();
    S.model = model; S.container = container || null;
    const im = model.internalModel, core = im.coreModel;
    const saved = S.modelSaved = {
      anchor: { x: model.anchor.x, y: model.anchor.y },
      scale: { x: model.scale.x, y: model.scale.y },
      position: { x: model.position.x, y: model.position.y },
      rotation: model.rotation,
      coreUpdateRaw: core.update,
      coreUpdate: core.update.bind(core),
      eyeBlink: im.eyeBlink ? im.eyeBlink.updateParameters : null,
      natural: im.updateNaturalMovements,
      physicsEval: im.physics ? im.physics.evaluate : null,
      motionStartRandom: im.motionManager ? im.motionManager.startRandomMotion : null,
      pivot: container ? { x: container.pivot.x, y: container.pivot.y } : null,
      containerPos: container ? { x: container.position.x, y: container.position.y } : null,
      containerRot: container ? container.rotation : 0
    };
    try { if (im.eyeBlink) im.eyeBlink.updateParameters = function () { }; } catch (e) { }
    try { im.updateNaturalMovements = function () { }; } catch (e) { }
    try { if (im.physics) im.physics.evaluate = function () { }; } catch (e) { }
    try {
      if (im.motionManager) {
        im.motionManager.stopAllMotions();
        if (im.motionManager.groups) im.motionManager.groups.idle = '';
        im.motionManager.startRandomMotion = () => Promise.resolve(false);
      }
    } catch (e) { }
    core.update = function () {
      const o = S.poseOut;
      if (o) for (const k in o) { try { core.setParameterValueById(k, o[k]); } catch (e) { } }
      return saved.coreUpdate();
    };
    try { model.anchor.set(.5, 0); } catch (e) { }
  }

  function unbindModel() {
    const m = S.model, sv = S.modelSaved;
    if (!m || !sv) { S.model = null; S.modelSaved = null; return; }
    const im = m.internalModel, core = im.coreModel;
    try { core.update = sv.coreUpdateRaw; } catch (e) { }
    try { if (sv.eyeBlink) im.eyeBlink.updateParameters = sv.eyeBlink; } catch (e) { }
    try { im.updateNaturalMovements = sv.natural; } catch (e) { }
    try { if (sv.physicsEval) im.physics.evaluate = sv.physicsEval; } catch (e) { }
    try { if (sv.motionStartRandom) im.motionManager.startRandomMotion = sv.motionStartRandom; } catch (e) { }
    try {
      m.anchor.set(sv.anchor.x, sv.anchor.y);
      m.scale.set(sv.scale.x, sv.scale.y);
      m.position.set(sv.position.x, sv.position.y);
      m.rotation = sv.rotation;
    } catch (e) { }
    try {
      if (S.container && sv.pivot) {
        S.container.pivot.set(sv.pivot.x, sv.pivot.y);
        S.container.position.set(sv.containerPos.x, sv.containerPos.y);
        S.container.rotation = sv.containerRot;
      }
    } catch (e) { }
    S.model = null; S.modelSaved = null;
  }

  function placeModel(cam) {
    const m = S.model;
    if (!m) return;
    const W = S.W, H = S.H;
    const im = m.internalModel;
    const oh = im.originalHeight || m.height / (m.scale.y || 1);
    m.scale.set(cam.sf * H / oh);
    m.position.set(cam.cx * W + cam.hx, (cam.headY - .049 * cam.sf) * H + cam.hy);
    if (S.container) {
      S.container.pivot.set(W / 2, H / 2);
      S.container.position.set(W / 2, H / 2);
      S.container.rotation = cam.rot * Math.PI / 180;
    } else {
      m.rotation = cam.rot * Math.PI / 180;
    }
  }

  /* ------------------------------ 尺寸 / DOM ------------------------------ */
  function applySize() {
    const W = S.W = S.root.clientWidth || global.innerWidth || 1280;
    const H = S.H = S.root.clientHeight || global.innerHeight || 720;
    const BW = S.BW = Math.min(MAX_BG_W, Math.max(560, Math.round(W * .4)));
    const BH = S.BH = Math.max(1, Math.round(BW * H / W));
    const els = S.els;
    els.bg.width = BW; els.bg.height = BH;
    els.off.width = BW; els.off.height = BH;
    els.fx.width = W; els.fx.height = H;
    if (S.lyrics) S.lyrics.layout(W, H);
    S.dirty = true;
  }

  function buildDom(root) {
    const wrap = document.createElement('div');
    wrap.className = 'ns-wrap';
    wrap.style.cssText = 'position:absolute;inset:0;overflow:hidden;';
    wrap.innerHTML =
      '<div class="ns-world"><canvas class="ns-bg"></canvas><canvas class="ns-fx"></canvas>' +
      '<div class="ns-ly ns-lyback"></div><canvas class="ns-l2d"></canvas><div class="ns-ly ns-lyfront"></div>' +
      '<div class="ns-bars"><i></i><i></i></div></div>' +
      '<div class="ns-vig"></div><div class="ns-grain"></div>' +
      '<div class="ns-sub"><div class="row cur"><span class="base"></span><span class="fill"></span></div><div class="row nxt"></div></div>' +
      '<div class="ns-flash"></div><div class="ns-black"></div><div class="ns-hud"></div>';
    root.appendChild(wrap);
    root.classList.add('ns-root');
    const q = sel => wrap.querySelector(sel);
    const els = {
      wrap, world: q('.ns-world'), bg: q('.ns-bg'), fx: q('.ns-fx'),
      back: q('.ns-lyback'), l2d: q('.ns-l2d'), front: q('.ns-lyfront'),
      bars: wrap.querySelectorAll('.ns-bars i'), grain: q('.ns-grain'),
      sub: q('.ns-sub'), flash: q('.ns-flash'), black: q('.ns-black'), hud: q('.ns-hud')
    };
    els.bgc = els.bg.getContext('2d');
    els.fxc = els.fx.getContext('2d');
    els.off = document.createElement('canvas');
    els.offc = els.off.getContext('2d');
    // 颗粒：固定的噪声贴图（只生成一次，不随时间变）
    const g = document.createElement('canvas');
    g.width = g.height = 200;
    const gc = g.getContext('2d');
    const img = gc.createImageData(200, 200);
    const rng = U.mulberry32(1337);
    for (let i = 0; i < img.data.length; i += 4) {
      const v = Math.round(rng() * 255);
      img.data[i] = img.data[i + 1] = img.data[i + 2] = v; img.data[i + 3] = 255;
    }
    gc.putImageData(img, 0, 0);
    els.grain.style.background = 'url(' + g.toDataURL() + ')';
    return els;
  }

  /* ------------------------------ 渲染 ------------------------------ */
  function renderAt(t) {
    if (!S.show) return null;
    t = U.clamp(+t || 0, 0, S.dur);
    const cam = cameraAt(t);
    const I = intensityAt(t), pu = pulseAt(t), sec = sectionAt(t);
    const idx = S.scenes.findIndex(s => t < s.t1);
    const si = idx < 0 ? S.scenes.length - 1 : idx;
    const sceneInfo = Scenes.drawBackground({
      bgCtx: S.els.bgc, off: S.els.off, offCtx: S.els.offc,
      W: S.BW, H: S.BH, t, show: S.show, cam, bar: S.bar,
      sceneIndex: si,
      intensity: I, pulse: pu, seed: S.seed,
      sceneVar: U.h01(S.seed * .001 + si * 7.77),
      prevSceneVar: U.h01(S.seed * .001 + (si - 1) * 7.77),
      skin: S.skin, decor: S.cfg.decor
    });
    // CSS 层：模糊/亮度/轻微推拉跟着段落和镜头走
    // 亮度直接用场景层返回的 lum（已在 stage-scenes.js 里乘过一次脉冲项，与原型一致）
    S.els.bg.style.filter = 'blur(' + (sceneInfo.blur * S.H / 1080).toFixed(1) + 'px) brightness('
      + sceneInfo.lum.toFixed(2) + ') saturate(1.25)';
    S.els.bg.style.transform = 'translate(' + (-(cam.cx - .5) * S.W * .05).toFixed(1) + 'px,0) scale('
      + (1 + (cam.sf - 2) * .02).toFixed(3) + ')';

    Scenes.drawForeground({
      ctx: S.els.fxc, W: S.W, H: S.H, t, show: S.show, intensity: I, pulse: pu,
      sectionId: sec.id, anchors: S.lyrics.anchors
    });

    S.lyrics.render(t);
    S.lyrics.subtitle(t, S.subOn);

    S.poseOut = poseAt(t);
    placeModel(cam);

    // 世界层：冲击震屏
    const shake = evAmp(t, 'shake', 14) * ((S.shots.find(s => s.t <= t) || { shake: 0 }).shake + .3);
    S.els.world.style.transform = shake > .2
      ? 'translate(' + ((U.h01(t * 91) - .5) * shake * 2).toFixed(1) + 'px,' + ((U.h01(t * 57) - .5) * shake * 2).toFixed(1) + 'px) rotate(' + (cam.rot * .15).toFixed(2) + 'deg)'
      : 'rotate(' + (cam.rot * .15).toFixed(2) + 'deg)';
    S.els.back.style.transform = 'translate(' + (-(cam.cx - .5) * S.W * .03).toFixed(1) + 'px,0)';
    S.els.front.style.transform = S.els.back.style.transform;

    S.els.flash.style.opacity = evAmp(t, 'flash', 4.5).toFixed(3);
    let bl = 0;
    for (const e of S.events) {
      if (e.t > t) break;
      if (e.type === 'black' && t >= e.t - (e.dur || 0) && t < e.t) bl = 1;
    }
    S.els.black.style.opacity = bl;

    // 桥段遮幅：上下黑边缓缓压进来
    const bs = S.sections.find(s => s.id === 'bridge');
    let lb = 0;
    if (bs) lb = U.sstep(bs.t0 - .3, bs.t0 + .5, t) * (1 - U.sstep(bs.t1 - S.beat, bs.t1, t));
    S.els.bars.forEach(b => { b.style.height = (lb * 9).toFixed(2) + 'vh'; });

    // 颗粒位移：由 t 派生，保证同一 t 画面一致
    const gf = Math.floor(t * 15);
    S.els.grain.style.transform = 'translate(' + ((U.h01(gf * 3.1) - .5) * 10).toFixed(1) + '%,'
      + ((U.h01(gf * 5.7) - .5) * 10).toFixed(1) + '%)';

    if (S.hud) renderHud(t, cam, sceneInfo, sec, I);
    S.lastT = t;
    S.lastInfo = { t, cam, scene: sceneInfo, section: sec.id, intensity: I, preset: S.show.style.preset };
    return S.lastInfo;
  }

  function renderHud(t, cam, sc, sec, I) {
    const L = S.lyrics.lineAt(t);
    const c = L ? L.combo : null;
    const pose = S.poseOut || {};
    S.els.hud.innerHTML =
      '<b>' + (S.show.style.theme || '') + '</b> · ' + (S.show.style.preset || '') + ' · ' + S.show.music.bpm + ' BPM · 种子 <span class="t">' + S.seed + '</span><br>'
      + '<span class="k">段落</span><b>' + (sec.label || sec.id) + '</b>　<span class="k" style="width:3em">强度</span><span class="bar" style="width:' + (I * 90).toFixed(0) + 'px"></span><br>'
      + '<span class="k">镜头</span>' + (CN.shot[cam.kind] || cam.kind) + ' · 构图' + (cam.cx < .45 ? '偏左' : cam.cx > .55 ? '偏右' : '居中') + (Math.abs(cam.rot) > .5 ? ' · 荷兰角' : '') + '<br>'
      + '<span class="k">背景</span>' + (CN.scene[sc.scene] || sc.scene) + ' · ' + (CN.entry[sc.entry] || sc.entry) + '出场' + (sc.density > 1 ? ' · 升级版' : '') + '<br>'
      + (c
        ? '<span class="k">歌词</span>' + (L.kind === 'title' ? '开场标题 · ' : L.kind === 'flash' ? '关键词闪回 · ' : '')
          + (CN.reveal[c.reveal] || c.reveal) + ' · ' + (CN.stagger[L.stagger] || L.stagger) + ' · '
          + (CN.layout[c.layout] || c.layout) + ' · ' + (CN.material[c.material] || c.material) + ' · '
          + (CN.exit[c.exit] || c.exit) + (c.echo ? ' · <span class="t">重复句升级+回声</span>' : '') + (c.front ? ' · <span class="t">前排爆点</span>' : '') + '<br>'
        : '<span class="k">歌词</span>—<br>')
      + '<span class="k">人物</span>嘴 ' + (pose.ParamMouthOpenY || 0).toFixed(2) + ' · ' + (CN.emo[L ? L.emotion : ''] || (L ? L.emotion : '—')) + ' · ' + (CN.gaze[L ? L.gaze : ''] || '—');
  }

  /* ------------------------------ 时钟 / 主循环 ------------------------------ */
  function currentT() {
    if (S.clock) {
      if (typeof S.clock === 'function') return +S.clock() || 0;
      if (typeof S.clock.now === 'function') return +S.clock.now() || 0;
    }
    if (S.playing) return S.show0 + (performance.now() - S.wall0) / 1000;
    return S.pausedT;
  }

  function loop(ts) {
    S.raf = requestAnimationFrame(loop);
    if (document.hidden) { S.hidden = true; return; }
    if (S.hidden) { S.hidden = false; S.lastPaint = 0; }
    if (ts - S.lastPaint < 1000 / S.fps - 1) return;
    S.lastPaint = ts;
    if (!S.playing && !S.dirty) return;
    S.dirty = false;
    const t0 = performance.now();
    renderAt(currentT());
    if (S.statsOn) { S.stats.push(performance.now() - t0); if (S.stats.length > 600) S.stats.shift(); }
  }

  /* ------------------------------ 生命周期 ------------------------------ */
  function start(opts) {
    opts = opts || {};
    stop();
    S.show = opts.show;
    S.root = typeof opts.root === 'string' ? document.querySelector(opts.root) : (opts.root || document.body);
    if (!S.root) throw new Error('NekoStage: root not found');
    S.fps = Math.max(1, Math.min(60, opts.fps || 30));
    S.hud = opts.hud !== false;
    S.subOn = opts.sub !== false;
    S.els = buildDom(S.root);

    const show = S.show;
    S.dur = +show.duration || 0;
    S.beat = 60 / (show.music.bpm || 100);
    S.bar = S.beat * 4;
    S.seed = (show.seed >>> 0) || 1;
    S.sections = (show.music.sections || []).slice().sort((a, b) => a.t0 - b.t0);
    S.shots = (show.camera || []).slice().sort((a, b) => a.t - b.t);
    S.scenes = (show.scenes || []).slice().sort((a, b) => a.t0 - b.t0);
    S.events = (show.events || []).slice().sort((a, b) => a.t - b.t);
    S.preset = PRESETS[show.style.preset] || PRESETS.pop;

    S.cfg = resolveCfg(show, opts.fonts || null);
    const skin = global.NekoStageSkins ? global.NekoStageSkins.resolve((show.style || {}).skin) : null;
    S.skin = skin;
    // 皮肤类挂在 ns-root 上（与 skins.css 的选择器一致）；重开时先清掉上一次的
    const skinIds = global.NekoStageSkins ? Object.keys(global.NekoStageSkins.SKINS) : [];
    S.root.classList.remove('ns-light', 'ns-deco-outline');
    skinIds.forEach(id => S.root.classList.remove('ns-skin-' + id));
    if (skin) {
      S.root.classList.add('ns-skin-' + skin.id);
      if (skin.light) S.root.classList.add('ns-light');
    }
    if (S.cfg.decor.indexOf('outline') >= 0) S.root.classList.add('ns-deco-outline');
    // 调色板写到 CSS 变量
    const pal = show.style.palette || {};
    for (const k of ['ink', 'ink2', 'hot', 'bgA', 'bgB', 'c1', 'c2', 'c3']) {
      if (pal[k]) S.els.wrap.style.setProperty('--' + k, pal[k]);
    }

    S.lyrics = Lyrics.create();
    S.lyrics.build(show, { back: S.els.back, front: S.els.front, sub: S.els.sub }, S.cfg);
    applySize();
    bakePose();

    if (opts.model) setModel(opts.model, opts.container);

    S.els.hud.style.display = S.hud ? 'block' : 'none';
    S.clock = opts.clock || null;
    S.playing = opts.clock ? true : !!opts.autoplay;
    S.pausedT = opts.t0 || 0;
    S.show0 = S.pausedT; S.wall0 = performance.now();
    S.dirty = true;
    S.lastPaint = 0;
    S.stats = []; S.statsOn = !!opts.stats;
    renderAt(S.playing ? currentT() : S.pausedT);

    const onResize = () => applySize();
    const onVis = () => { S.lastPaint = 0; S.dirty = true; };
    global.addEventListener('resize', onResize);
    document.addEventListener('visibilitychange', onVis);
    S._off = () => {
      global.removeEventListener('resize', onResize);
      document.removeEventListener('visibilitychange', onVis);
    };
    S.raf = requestAnimationFrame(loop);
    return api;
  }

  function stop() {
    if (S.raf) cancelAnimationFrame(S.raf);
    S.raf = 0;
    if (S._off) { S._off(); S._off = null; }
    unbindModel();
    if (S.els && S.els.wrap && S.els.wrap.parentNode) S.els.wrap.parentNode.removeChild(S.els.wrap);
    if (S.root) S.root.classList.remove('ns-root');
    S.els = null; S.lyrics = null; S.show = null; S.poseOut = null; S.poseTrack = null;
    S.playing = false;
  }

  const api = {
    start, stop, renderAt, setModel, loadFonts,
    get skin() { return S.skin || null; },
    get fonts() { return S.cfg ? S.cfg.fontInfo : null; },
    get canvas() { return S.els ? S.els.l2d : null; },
    get rootEl() { return S.root; },
    get duration() { return S.dur; },
    get playing() { return S.playing; },
    get t() { return S.dur ? currentT() : 0; },
    currentT,
    play() {
      if (S.clock) { S.clock.play && S.clock.play(); return; }
      if (S.playing) return;
      S.show0 = S.pausedT; S.wall0 = performance.now(); S.playing = true; S.dirty = true;
    },
    pause() {
      if (S.clock) { S.clock.pause && S.clock.pause(); return; }
      if (!S.playing) return;
      S.pausedT = currentT(); S.playing = false;
    },
    seek(t) {
      if (S.clock) { S.clock.seek && S.clock.seek(t); return; }
      S.pausedT = U.clamp(+t || 0, 0, S.dur);
      if (S.playing) { S.show0 = S.pausedT; S.wall0 = performance.now(); }
      S.dirty = true;
      renderAt(S.pausedT);
    },
    setHud(on) { S.hud = !!on; if (S.els) S.els.hud.style.display = S.hud ? 'block' : 'none'; S.dirty = true; },
    setSub(on) { S.subOn = !!on; S.dirty = true; },
    setFps(n) { S.fps = Math.max(1, Math.min(60, +n || 30)); },
    info() { return S.lastInfo; },
    stats() {
      const a = S.stats.slice().sort((x, y) => x - y);
      const sum = a.reduce((x, y) => x + y, 0);
      return {
        n: a.length, avg: a.length ? sum / a.length : 0,
        p50: a.length ? a[Math.floor(a.length * .5)] : 0,
        p95: a.length ? a[Math.floor(a.length * .95)] : 0,
        max: a.length ? a[a.length - 1] : 0,
        bgW: S.BW, bgH: S.BH, W: S.W, H: S.H, fps: S.fps
      };
    },
    clearStats() { S.stats = []; },
    enableStats(on) { S.statsOn = !!on; }
  };
  S.api = api;
  global.NekoStage = api;
})(window);
