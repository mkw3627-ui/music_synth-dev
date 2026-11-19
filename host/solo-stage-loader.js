/* =============================================================================
 * 「为她而唱」独唱舞台 · 宿主加载器（T3-B）
 *
 * 宿主只加载这一个 <script>。收到 bridge.js 派发的 neko-solo-stage 事件后：
 *   1) 从 show_url GET 编排（show.json）
 *   2) 从 stage_base GET 舞台运行时（skins/stage-scenes/stage-lyrics/stage-runtime + CSS + fonts.json）
 *   3) 用 window.__nekoMusicLips 作为真实时钟、宿主已有的 Live2D 模型作为人物，
 *      只写模型变换与参数，不新建/替换模型
 *   4) 退出（neko-solo-stage-end / Esc / ✕）时 NekoStage.stop() 还原参数、把
 *      #live2d-container 放回原位、移除舞台 DOM
 *
 * 舞台资源拉取失败时退回现有 solo-stage.js 行为（动态补 3 个 canvas 并加载旧脚本）。
 * 不刷新页面、不导航、不改宿主模型实例。
 *
 * 诊断接口：window.__NEKO_SOLO_STAGE_LOADER__（只读 state + stop）
 * ========================================================================== */
(function (global) {
  'use strict';
  if (global.__NEKO_SOLO_STAGE_LOADER__) { return; }

  var STAGE_BUILD = '20261119r69';
  var state = {
    version: 't3b-1',
    active: false,
    legacy: false,
    showUrl: '',
    stageBase: '',
    loaded: [],
    fonts: null,
    model: false,
    containerMoved: false,
    startedAt: 0,
    lastError: '',
    lastStop: '',
    events: 0,
    starts: 0,
    ends: 0
  };

  var rootEl = null;
  var styleEl = null;
  var exitEl = null;
  var stageCssEls = [];
  var savedContainer = null;
  var starting = false;
  var startGen = 0;
  var keyHandler = null;

  function log() {
    try {
      var a = Array.prototype.slice.call(arguments);
      a.unshift('[solo-loader]');
      console.log.apply(console, a);
    } catch (e) { /* ignore */ }
  }
  function errText(e) { return String((e && (e.stack || e.message)) || e || 'unknown'); }

  /* ------------------------------ 样式 / 退出按钮 ------------------------------ */
  function ensureStyle() {
    if (styleEl) { return; }
    var st = document.createElement('style');
    st.id = 'neko-solo-loader-style';
    st.textContent = [
      '#neko-solo-stage-root{position:fixed;inset:0;width:100%;height:100%;',
      'pointer-events:none;z-index:12000;overflow:hidden;}',
      '#neko-solo-stage-root .ns-root{position:absolute;inset:0;}',
      '#neko-solo-stage-root #live2d-container{position:absolute!important;',
      'inset:0!important;right:auto!important;bottom:auto!important;',
      'width:100%!important;height:100%!important;z-index:4!important;',
      'visibility:visible!important;opacity:1!important;transform:none!important;',
      'transition:none!important;}',
      'body.neko-solo-stage #chat-container,',
      'body.neko-solo-stage #status-toast,',
      'body.neko-solo-stage #subtitle-display,',
      'body.neko-solo-stage #react-chat-window-overlay,',
      'body.neko-solo-stage #chat-avatar-preview-popup,',
      'body.neko-solo-stage .home-avatar-floating-guide-player,',
      'body.neko-solo-stage [id$="-floating-buttons"],',
      'body.neko-solo-stage #agent-task-hud,',
      'body.neko-solo-stage [id$="-return-button-container"],',
      'body.neko-solo-stage [id$="-lock-icon"]{display:none!important;}'
    ].join('');
    document.head.appendChild(st);
    styleEl = st;
  }

  function ensureExit() {
    if (exitEl) { return; }
    var b = document.createElement('div');
    b.id = 'neko-solo-exit';
    b.textContent = '✕';
    b.title = '退出独唱舞台 (Esc)';
    b.style.cssText = [
      'position:fixed;right:18px;top:18px;width:38px;height:38px;',
      'border-radius:50%;display:flex;align-items:center;justify-content:center;',
      'font:600 17px/1 system-ui,sans-serif;color:#fff;cursor:pointer;',
      'background:rgba(18,18,22,.55);backdrop-filter:blur(6px);',
      'border:1px solid rgba(255,255,255,.18);pointer-events:auto;',
      'z-index:2147483000;user-select:none;'
    ].join('');
    b.addEventListener('click', function (ev) {
      ev.stopPropagation();
      stopStage('exit-button');
      try { global.dispatchEvent(new CustomEvent('neko-solo-stage-end')); } catch (e) { }
    });
    document.body.appendChild(b);
    exitEl = b;
  }

  /* ------------------------------ 资源加载 ------------------------------ */
  function loadCss(href, id) {
    var old = id && document.getElementById(id);
    if (old) { return Promise.resolve(true); }
    return new Promise(function (res) {
      var l = document.createElement('link');
      l.rel = 'stylesheet';
      l.href = href;
      if (id) { l.id = id; }
      l.onload = function () { res(true); };
      l.onerror = function () { res(false); };
      document.head.appendChild(l);
      stageCssEls.push(l);
    });
  }

  function loadScriptOnce(url) {
    if (document.querySelector('script[data-solo-loader-src="' + url + '"]')) { return Promise.resolve(true); }
    return new Promise(function (res) {
      var s = document.createElement('script');
      s.src = url;
      s.async = false;
      s.setAttribute('data-solo-loader-src', url);
      s.onload = function () { res(true); };
      s.onerror = function () { res(false); };
      document.head.appendChild(s);
    });
  }

  function fetchJson(url, ms) {
    var c = new AbortController();
    var timer = setTimeout(function () { c.abort(); }, ms || 15000);
    return fetch(url, { cache: 'no-store', signal: c.signal })
      .then(function (r) {
        if (!r.ok) { throw new Error('HTTP ' + r.status + ' ' + url); }
        return r.json();
      })
      .finally(function () { clearTimeout(timer); });
  }

  async function ensureStageLib(base) {
    var v = '?v=' + STAGE_BUILD;
    var cssOk = await loadCss(base + 'stage.css' + v, 'neko-solo-css');
    var skinCssOk = await loadCss(base + 'skins/skins.css' + v, 'neko-solo-skins-css');
    var files = [
      ['skins/skins.js', 'NekoStageSkins'],
      ['stage-scenes.js', 'NekoStageScenes'],
      ['stage-scenes-rich.js', 'NekoStageScenes'],
      ['stage-scenes-more.js', 'NekoStageScenes'],
      ['stage-scenes-pack2.js', 'NekoStageScenes'],
      ['stage-lines.js', 'NekoStageLines'],
      ['stage-director.js', 'NekoStageDirector'],
      ['stage-learned.js', 'NekoStageLearned'],
      ['stage-templates.js', 'NekoStageTemplates'],
      ['stage-art.js', 'NekoStageArt'],
      ['stage-frames.js', 'NekoStageFrames'],
      ['stage-stinger.js', 'NekoStageStinger'],
      ['stage-look.js', 'NekoStageLook'],
      ['stage-planner.js', 'NekoStagePlanner'],
      ['stage-lyrics.js', 'NekoStageLyrics'],
      // 第六十三轮：多模型适配——转换器（vtube.json）+ 尺子 + 自适应校正
      ['vts_map.js', 'NekoVtsMap'],
      ['ruler.js', 'NekoRuler'],
      ['calib.js', 'NekoCalib'],
      ['model_fit.js', 'NekoModelFit'],
      ['stage-runtime.js', 'NekoStage']
    ];
    // 第四十五轮：开源 pixi-filters（人物 GPU 滤镜）。按宿主 PIXI 大版本挑：7 → v5，6 → v4；加载失败不影响演出
    try {
      var pv = String((global.PIXI && global.PIXI.VERSION) || '');
      var pf = pv.charAt(0) === '7' ? 'vendor/pixi-filters.v5.js' : pv.charAt(0) === '6' ? 'vendor/pixi-filters.v4.js' : '';
      if (pf && !(global.PIXI.filters && global.PIXI.filters.AdjustmentFilter)) { state.pixiFilters = (await loadScriptOnce(base + pf + v)) ? pf : 'fail'; }
    } catch (e) { state.pixiFilters = 'err'; }
    // 第五十二轮：Rough.js（MIT）手绘笔触，分镜层用；加载失败不影响演出
    try { if (!global.rough) { state.rough = (await loadScriptOnce(base + 'vendor/rough.js' + v)) ? 'ok' : 'fail'; } } catch (e) { state.rough = 'err'; }
    for (var i = 0; i < files.length; i++) {
      var url = base + files[i][0] + v;
      var ok = await loadScriptOnce(url);
      if (!ok) { throw new Error('script load failed: ' + url); }
      state.loaded.push(files[i][0]);
      if (!global[files[i][1]]) { throw new Error('global missing: ' + files[i][1]); }
    }
    if (!cssOk || !skinCssOk) { throw new Error('css load failed (stage/skins)'); }
    // 第四十轮：素材库学来的图案（没有也不影响演出）
    try {
      var lf = await fetchJson(base + 'learned_effects.json' + v, 5000);
      var n = global.NekoStageLearned ? global.NekoStageLearned.register((lf && lf.learned) || []) : 0;
      state.learned = n;
      state.templates = global.NekoStageTemplates ? global.NekoStageTemplates.register((lf && lf.templates) || []) : 0;
      global.NekoStageFramesEvolved = (lf && lf.frames) || [];
      state.frames = global.NekoStageFramesEvolved.length;
      global.NekoStageTags = (lf && lf.tags) || {};
    } catch (e) { state.learned = 0; }
    global.__NEKO_STAGE_BASE = base;
  }

  /* ------------------------------ 模型 / 时钟 ------------------------------ */
  function getModel() {
    var mgr = global.live2dManager;
    if (mgr && typeof mgr.getCurrentModel === 'function') {
      var m = mgr.getCurrentModel();
      if (m) { return m; }
    }
    if (global.LanLan1) { return global.LanLan1.live2dModel || global.LanLan1.currentModel || null; }
    return null;
  }

  function clockNow() {
    var lp = global.__nekoMusicLips;
    return lp ? (Number(lp.t) || 0) : 0;
  }

  function moveModelContainer() {
    var cont = document.getElementById('live2d-container');
    if (!cont || !rootEl) { return false; }
    var world = rootEl.querySelector('.ns-world');
    var front = rootEl.querySelector('.ns-lyfront');
    if (!world) { return false; }
    savedContainer = {
      el: cont,
      parent: cont.parentNode,
      next: cont.nextSibling,
      style: cont.getAttribute('style')
    };
    var cv = document.getElementById('live2d-canvas');
    savedContainer.canvas = cv ? { el: cv, style: cv.getAttribute('style') } : null;
    world.insertBefore(cont, front || null);
    if (cv) {
      // 重启后宿主可能处于「请她离开」状态（canvas visibility:hidden）；演出期间强制可见，退出时还原原值
      cv.style.visibility = 'visible';
      cv.style.opacity = '1';
    }
    cont.style.position = 'absolute';
    cont.style.inset = '0';
    cont.style.right = 'auto';
    cont.style.bottom = 'auto';
    cont.style.width = '100%';
    cont.style.height = '100%';
    cont.style.zIndex = '4';
    cont.style.visibility = 'visible';
    cont.style.opacity = '1';
    cont.style.transform = 'none';
    cont.style.transition = 'none';
    state.containerMoved = true;
    return true;
  }

  function restoreModelContainer() {
    var sv = savedContainer;
    savedContainer = null;
    state.containerMoved = false;
    if (!sv || !sv.el) { return; }
    try {
      if (sv.canvas && sv.canvas.el) {
        if (sv.canvas.style === null) { sv.canvas.el.removeAttribute('style'); }
        else { sv.canvas.el.setAttribute('style', sv.canvas.style); }
      }
      if (sv.style === null) { sv.el.removeAttribute('style'); } else { sv.el.setAttribute('style', sv.style); }
      if (sv.parent) {
        if (sv.next && sv.next.parentNode === sv.parent) { sv.parent.insertBefore(sv.el, sv.next); }
        else { sv.parent.appendChild(sv.el); }
      }
    } catch (e) { log('restore container failed', errText(e)); }
  }

  /* ------------------------------ 退出 ------------------------------ */
  function stopStage(reason) {
    if (!state.active && !starting) { return; }
    startGen += 1;          // 作废正在进行的 startStage，避免收场后又被异步启动拉回来
    starting = false;
    try {
      if (global.NekoStage) { global.NekoStage.stop(); }
    } catch (e) { log('NekoStage.stop failed', errText(e)); }
    restoreModelContainer();
    if (rootEl && rootEl.parentNode) { rootEl.parentNode.removeChild(rootEl); }
    rootEl = null;
    if (exitEl && exitEl.parentNode) { exitEl.parentNode.removeChild(exitEl); }
    exitEl = null;
    try { document.body.classList.remove('neko-solo-stage'); } catch (e) { }
    if (keyHandler) { document.removeEventListener('keydown', keyHandler, true); keyHandler = null; }
    state.active = false;
    state.lastStop = reason || 'stop';
    state.ends += 1;
    log('stop', state.lastStop);
  }

  /* ------------------------------ 旧舞台回退 ------------------------------ */
  function fallbackLegacy(detail, why) {
    if (state.legacy) { return; }
    state.legacy = true;
    state.legacyUrl = String(detail && detail.show_url || '');
    state.lastError = 'fallback: ' + why;
    log('fallback to legacy solo-stage:', why);
    stopStage('fallback');
    ['solo-bg-a', 'solo-bg-b', 'solo-fx'].forEach(function (id, i) {
      if (document.getElementById(id)) { return; }
      var c = document.createElement('canvas');
      c.id = id;
      c.className = 'neko-solo-layer';
      c.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;pointer-events:none;display:none;z-index:' + (i === 2 ? 2 : 1) + ';';
      document.body.appendChild(c);
    });
    Promise.all([
      loadScriptOnce('/static/libs/GlslCanvas.min.js'),
      loadScriptOnce('/static/live2d/solo-stage.glsl.js'),
      loadScriptOnce('/static/live2d/solo-stage.js')
    ]).then(function () {
      setTimeout(function () {
        try {
          global.dispatchEvent(new CustomEvent('neko-solo-stage', { detail: detail || {} }));
        } catch (e) { log('legacy re-dispatch failed', errText(e)); }
      }, 60);
    });
  }

  /* ------------------------------ 启动 ------------------------------ */
  async function startStage(detail) {
    var showUrl = String(detail && detail.show_url || '');
    var base = String(detail && detail.stage_base || '');
    if (!showUrl || !base) {
      fallbackLegacy(detail, 'missing show_url/stage_base');
      return;
    }
    if (state.active && state.showUrl === showUrl) { return; }
    if (state.active) { stopStage('switch-song'); }
    if (starting) { return; }
    starting = true;
    var gen = ++startGen;
    state.lastError = '';
    state.showUrl = showUrl;
    state.stageBase = base;
    try {
      // 新歌第一次唱时编排还在后台生成：本地模型读歌词要 50 s+（实测 54 s），以前只等 40 s 就退旧舞台 → 黑屏只剩人物。
      // 第四十七轮：最多等 3 分钟（36 次 × 5 s）
      var show = null, lastErr = null;
      for (var attempt = 0; attempt < 36; attempt++) {
        try { show = await fetchJson(showUrl, 15000); break; } catch (e1) {
          lastErr = e1;
          if (!/HTTP 404/.test(errText(e1)) || gen !== startGen) { throw e1; }
          await new Promise(function (r) { setTimeout(r, 5000); });
          if (gen !== startGen) { return; }
        }
      }
      if (!show) { throw lastErr || new Error('show.json missing'); }
      if (gen !== startGen) { return; }
      if (!show || !show.duration) { throw new Error('show.json invalid'); }
      await ensureStageLib(base);
      if (gen !== startGen) { return; }
      var fonts = null;
      try { fonts = await global.NekoStage.loadFonts(show); } catch (e) { log('loadFonts failed', errText(e)); }
      if (gen !== startGen) { return; }
      state.fonts = fonts ? (fonts.loaded || 0) : 0;

      ensureStyle();
      ensureExit();
      rootEl = document.createElement('div');
      rootEl.id = 'neko-solo-stage-root';
      document.body.appendChild(rootEl);
      document.body.classList.add('neko-solo-stage');

      var model = getModel();
      state.model = !!model;
      global.NekoStage.start({
        show: show,
        fonts: fonts,
        root: rootEl,
        fps: 30,
        hud: false,
        // 底部字幕条保留（用户：底部歌词要有；叠字指的是上面自由排版的字）
        sub: true,
        clock: { now: clockNow }
      });
      if (model) {
        // 只写宿主已有模型的变换/参数（NekoStage.stop() 会完整还原）
        global.NekoStage.setModel(model, model.parent || null);
      }
      moveModelContainer();
      state.active = true;
      state.startedAt = Date.now();
      state.starts += 1;
      keyHandler = function (ev) {
        if (ev.key === 'Escape' || ev.keyCode === 27) {
          stopStage('esc');
          try { global.dispatchEvent(new CustomEvent('neko-solo-stage-end')); } catch (e) { }
        }
      };
      document.addEventListener('keydown', keyHandler, true);
      try {
        global.dispatchEvent(new CustomEvent('neko-solo-stage-ready', {
          detail: {
            show_url: showUrl, stage_base: base, model: !!model,
            duration: Number(show.duration) || 0, fonts: state.fonts
          }
        }));
      } catch (e) { }
      log('started', showUrl, 'model=' + (!!model));
    } catch (e) {
      state.lastError = errText(e);
      log('start failed', state.lastError);
      stopStage('error');
      fallbackLegacy(detail, state.lastError);
    } finally {
      if (gen === startGen) { starting = false; }
    }
  }

  /* ------------------------------ 事件 ------------------------------ */
  function onStageEvent(ev) {
    state.events += 1;
    var d = (ev && ev.detail) || {};
    // 退旧舞台只对出问题的那一首：换了一首（show_url 不同）、或同一首重新开唱，都重新尝试新舞台
    if (state.legacy && d.show_url && (d.show_url !== state.legacyUrl || state.legacyEnded)) { state.legacy = false; state.legacyEnded = false; }
    if (state.legacy) { return; }
    if (d.show_url && d.stage_base) {
      startStage(d);
    } else {
      // 旧 app-websocket 路径（只有 title/duration）：交给旧舞台
      fallbackLegacy(d, 'legacy event without urls');
    }
  }

  global.addEventListener('neko-solo-stage', onStageEvent);
  global.addEventListener('neko-solo-stage-end', function () {
    if (state.legacy) { state.legacyEnded = true; return; }
    stopStage('bridge-end');
  });

  var api = {
    version: state.version,
    get state() {
      var out = {};
      for (var k in state) { out[k] = state[k]; }
      out.hasModel = !!getModel();
      out.html = {
        root: !!rootEl,
        exit: !!exitEl,
        nsRoot: !!(rootEl && rootEl.querySelector('.ns-root')),
        nsWorld: !!(rootEl && rootEl.querySelector('.ns-world')),
        containerInStage: !!(savedContainer && savedContainer.el && rootEl && rootEl.contains(savedContainer.el))
      };
      return out;
    },
    stop: stopStage
  };
  global.__NEKO_SOLO_STAGE_LOADER__ = api;
  log('loaded', state.version);
})(window);
