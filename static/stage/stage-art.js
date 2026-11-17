/* =============================================================================
 * 「为她而唱」舞台 · 第四十四轮：美术指导（统一视觉）
 *
 * 问题：之前每一层（背景、滤镜、边框、歌词材质、歌词底框、全屏转场、背后大字、几何线、光斑…）
 * 各自独立随机，同一屏上同时出现 5~6 种互不相干的视觉语言 → 杂乱、没风格。
 *
 * 参考（B 站 MV 动态歌词排版 / VTuber 歌回直播界面套装，r44_refs）：好看的画面都是
 *   ① 一个主题贯穿：一套配色（2 主色 + 1 点缀）、一种字体气质、一种装饰语言；
 *   ② 留白多、层少：背景 + 人物 + 歌词三件主角，装饰只做点缀；
 *   ③ 歌词不加底框，靠字体/字重/描边/阴影/大小对比出层次，底框只给特殊时刻（标题、副歌第一句）；
 *   ④ 变化发生在「段落」之间（主歌安静、副歌亮起来），段内不跳。
 *
 * 这里定义 12 个「主题」（每个主题 = 一组互相搭配的选择：场景族、滤镜、边框族、歌词材质、转场族、装饰开关），
 * 每次演出：
 *   - 用户选了风格 → 整首一个主题（该风格下随机一个）；
 *   - 用户没选（默认「融合」）→ 2~3 个主题按段落分配：主歌一个、副歌一个、桥段一个；
 *     同一主题在整首里复现（副歌每次都回到同一个主题），既有变化又有一致性。
 * 其它层全部向美术指导查询，不再自己随机。
 * ========================================================================== */
(function (global) {
  'use strict';
  const U = global.NekoStageU;
  const h = U ? U.h01 : (x => (Math.sin(x * 127.1) * 43758.5453) % 1);

  // 主题：每项都是「这一主题允许的范围」，同一主题内的东西互相搭
  const THEMES = {
    starry_night: { mood: 'calm', genres: ['ballad', 'jazz', 'dark', 'pop'], scenes: ['galaxy', 'skyline', 'rooftop', 'aurora'],
      filter: 'starlit', frames: ['cinema', 'corner'], ext: ['starline', 'goldleaf'], lyric: ['soft', 'serifglow'], stingers: ['iris', 'blinds'],
      bgEntries: ['split', 'iris', 'diamond'], deco: { lines: 'none', bokeh: .6, ghost: true, plates: false }, accent: '#ffe6a8' },
    sakura_dream: { mood: 'soft', genres: ['ballad', 'cute', 'ancient', 'pop'], scenes: ['sakura', 'sky', 'bubbles', 'classroom'],
      filter: 'sakura', frames: ['lace', 'corner'], ext: ['flowerwreath', 'heartline'], lyric: ['soft', 'candy'], stingers: ['petals', 'iris'],
      bgEntries: ['split', 'iris', 'dots'], deco: { lines: 'none', bokeh: .7, ghost: true, plates: false }, accent: '#ff8fbf' },
    idol_stage: { mood: 'bright', genres: ['pop', 'cute', 'edm', 'rock'], scenes: ['concert', 'starstage', 'confetti', 'beams'],
      filter: 'neonpop', frames: ['screen'], ext: ['gradientring', 'neontube'], lyric: ['pop', 'grad'], stingers: ['stars', 'stripes'],
      bgEntries: ['diamond', 'split', 'clock'], deco: { lines: 'slash', bokeh: .8, ghost: false, plates: false }, accent: '#ff4f8b' },
    neon_city: { mood: 'cool', genres: ['edm', 'rock', 'dark', 'pop'], scenes: ['neoncity', 'synth', 'skyline', 'tunnel'],
      filter: 'cyber', frames: ['hud'], ext: ['cyberbrackets', 'neontube'], lyric: ['neon', 'chrome'], stingers: ['glitch', 'shutter'],
      bgEntries: ['sweepL', 'sweepR', 'blinds'], deco: { lines: 'tri', bokeh: .4, ghost: true, plates: false }, accent: '#33e1ff' },
    sunset_film: { mood: 'warm', genres: ['ballad', 'rock', 'jazz', 'pop'], scenes: ['sunset', 'rooftop', 'classroom', 'cafe'],
      filter: 'golden', frames: ['cinema', 'film'], ext: ['filmstrip2'], lyric: ['serifglow', 'outline'], stingers: ['blinds', 'curtain'],
      bgEntries: ['sweepU', 'split', 'blinds'], deco: { lines: 'none', bokeh: .5, ghost: true, plates: false }, accent: '#ffb36b' },
    pastel_pop: { mood: 'happy', genres: ['cute', 'pop'], scenes: ['pattern', 'hearts', 'bubbles', 'confetti'],
      filter: 'softpink', frames: ['sticker', 'corner'], ext: ['washi', 'heartline'], lyric: ['sticker', 'candy'], stingers: ['halftone', 'diamonds'],
      bgEntries: ['dots', 'diamond', 'split'], deco: { lines: 'none', bokeh: .5, ghost: false, plates: true }, accent: '#ff6fae' },
    ocean_blue: { mood: 'calm', genres: ['ballad', 'dark', 'ancient', 'jazz'], scenes: ['ocean', 'rainwin', 'aurora', 'sky'],
      filter: 'dreamblue', frames: ['corner', 'cinema'], ext: ['bubbleframe'], lyric: ['soft', 'outline'], stingers: ['iris', 'ink'],
      bgEntries: ['sweepU', 'split', 'iris'], deco: { lines: 'none', bokeh: .6, ghost: true, plates: false }, accent: '#9fd8ff' },
    lantern_fest: { mood: 'festive', genres: ['ancient', 'pop', 'cute', 'ballad'], scenes: ['lantern', 'festival', 'sakura', 'forest'],
      filter: 'golden', frames: ['scroll', 'gold'], ext: ['chinoise', 'goldleaf'], lyric: ['ink', 'serifglow'], stingers: ['ink', 'paper'],
      bgEntries: ['split', 'iris', 'clock'], deco: { lines: 'none', bokeh: .5, ghost: true, plates: false }, accent: '#ff7a4a' },
    retro_wave: { mood: 'energetic', genres: ['edm', 'rock', 'pop'], scenes: ['synth', 'wavefield', 'kaleido', 'neoncity'],
      filter: 'crimson', frames: ['vhs', 'neonbar'], ext: ['pixel', 'glitchframe'], lyric: ['chrome', 'neon'], stingers: ['slices', 'bars'],
      bgEntries: ['sweepL', 'columns', 'diag'], deco: { lines: 'slash', bokeh: .3, ghost: false, plates: false }, accent: '#ff3fa4' },
    noir_theater: { mood: 'dark', genres: ['dark', 'jazz', 'rock', 'ballad'], scenes: ['rainwin', 'cafe', 'galaxy', 'space'],
      filter: 'noir', frames: ['theater', 'film'], ext: ['artdeco'], lyric: ['outline', 'serifglow'], stingers: ['curtain', 'shutter'],
      bgEntries: ['split', 'sweepD', 'iris'], deco: { lines: 'none', bokeh: .3, ghost: true, plates: false }, accent: '#e8c87a' },
    crystal_space: { mood: 'cool', genres: ['edm', 'dark', 'cute', 'ballad'], scenes: ['crystal', 'space', 'galaxy', 'aurora'],
      filter: 'dreamblue', frames: ['screen', 'corner'], ext: ['starline', 'gradientring'], lyric: ['neon', 'soft'], stingers: ['zoom', 'stars'],
      bgEntries: ['diamond', 'clock', 'iris'], deco: { lines: 'tri', bokeh: .6, ghost: false, plates: false }, accent: '#b48cff' },
    forest_breeze: { mood: 'fresh', genres: ['ballad', 'ancient', 'pop', 'cute'], scenes: ['forest', 'sky', 'sakura', 'bubbles'],
      filter: 'mint', frames: ['corner', 'lace'], ext: ['cloudframe', 'stitch'], lyric: ['soft', 'outline'], stingers: ['petals', 'blinds'],
      bgEntries: ['sweepU', 'split', 'dots'], deco: { lines: 'none', bokeh: .6, ghost: true, plates: false }, accent: '#7ac86a' },

    /* 第四十五轮：再加 20 套（参考 YouTube 虚拟主播歌回 / 3D LIVE / 歌ってみた MV 的常见做法）。
     * 每套都带：专属滤镜（stage-look 新滤镜）+ 人物 GPU 滤镜（pixi-filters，见 filter.px）+ 运镜性格 cam。
     * cam：calm 慢推慢移；idol 节拍推拉/环绕；mv 角落放大、揭示、横摇；cyber 快速变焦/甩镜 */
    holo_live3d: { mood: 'bright', genres: ['pop', 'cute', 'edm'], scenes: ['starstage', 'concert', 'beams', 'led'],
      filter: 'holo_idol', frames: ['screen'], ext: ['gradientring', 'neontube'], lyric: ['pop', 'grad'], stingers: ['stars', 'diamonds'],
      bgEntries: ['diamond', 'clock', 'split'], deco: { lines: 'none', bokeh: .7, ghost: false, plates: false }, accent: '#e08aff', cam: 'idol' },
    utawaku_night: { mood: 'calm', genres: ['ballad', 'jazz', 'pop'], scenes: ['rainwin', 'cafe', 'skyline', 'galaxy'],
      filter: 'karaoke_night', frames: ['corner'], ext: ['starline'], lyric: ['soft', 'serifglow'], stingers: ['iris', 'blinds'],
      bgEntries: ['split', 'iris'], deco: { lines: 'none', bokeh: .6, ghost: true, plates: false }, accent: '#b38cff', cam: 'calm' },
    milk_tea: { mood: 'soft', genres: ['cute', 'pop', 'ballad'], scenes: ['pattern', 'bubbles', 'sky', 'classroom'],
      filter: 'milk_pastel', frames: ['lace', 'corner'], ext: ['cloudframe', 'ribbon'], lyric: ['candy', 'soft'], stingers: ['petals', 'halftone'],
      bgEntries: ['dots', 'split'], deco: { lines: 'none', bokeh: .5, ghost: false, plates: false }, accent: '#ffb3cf', cam: 'calm' },
    dreamcore: { mood: 'soft', genres: ['ballad', 'cute', 'dark'], scenes: ['aurora', 'sky', 'bubbles', 'crystal'],
      filter: 'dream_bloom', frames: ['corner'], ext: ['bubbleframe', 'starline'], lyric: ['soft', 'outline'], stingers: ['iris', 'zoom'],
      bgEntries: ['iris', 'diamond'], deco: { lines: 'none', bokeh: .8, ghost: true, plates: false }, accent: '#e8c8ff', cam: 'mv' },
    vhs_memory: { mood: 'warm', genres: ['rock', 'pop', 'jazz'], scenes: ['rooftop', 'classroom', 'sunset', 'skyline'],
      filter: 'vhs_tape', frames: ['vhs'], ext: ['filmstrip2'], lyric: ['outline', 'chrome'], stingers: ['slices', 'glitch'],
      bgEntries: ['blinds', 'sweepL'], deco: { lines: 'none', bokeh: .3, ghost: false, plates: false }, accent: '#ffb080', cam: 'mv' },
    arcade_crt: { mood: 'energetic', genres: ['edm', 'cute', 'rock'], scenes: ['led', 'synth', 'pattern', 'kaleido'],
      filter: 'crt_arcade', frames: ['hud'], ext: ['pixel', 'arcade'], lyric: ['neon', 'stamp'], stingers: ['bars', 'slices'],
      bgEntries: ['columns', 'sweepR'], deco: { lines: 'none', bokeh: .2, ghost: false, plates: false }, accent: '#7affb0', cam: 'cyber' },
    lofi_bedroom: { mood: 'calm', genres: ['jazz', 'ballad', 'pop'], scenes: ['cafe', 'rainwin', 'classroom', 'rooftop'],
      filter: 'lofi_room', frames: ['polaroid', 'corner'], ext: ['notebook', 'postcard'], lyric: ['soft', 'outline'], stingers: ['paper', 'blinds'],
      bgEntries: ['sweepU', 'split'], deco: { lines: 'none', bokeh: .4, ghost: true, plates: false }, accent: '#ffcf9a', cam: 'calm' },
    anime_ending: { mood: 'soft', genres: ['ballad', 'pop', 'ancient'], scenes: ['sky', 'sunset', 'sakura', 'ocean'],
      filter: 'anime_ed', frames: ['cinema'], ext: ['starline'], lyric: ['serifglow', 'soft'], stingers: ['iris', 'blinds'],
      bgEntries: ['sweepU', 'split'], deco: { lines: 'none', bokeh: .5, ghost: true, plates: false }, accent: '#a8d0ff', cam: 'mv' },
    summer_live: { mood: 'happy', genres: ['pop', 'cute', 'rock'], scenes: ['ocean', 'sky', 'confetti', 'bubbles'],
      filter: 'summer_soda', frames: ['screen', 'corner'], ext: ['washi', 'ticket'], lyric: ['pop', 'candy'], stingers: ['stripes', 'stars'],
      bgEntries: ['diag', 'sweepR'], deco: { lines: 'slash', bokeh: .6, ghost: false, plates: false }, accent: '#3ad0ff', cam: 'idol' },
    film_reel: { mood: 'warm', genres: ['rock', 'jazz', 'ballad'], scenes: ['sunset', 'rooftop', 'cafe', 'rainwin'],
      filter: 'film_burn', frames: ['film', 'cinema'], ext: ['filmstrip2'], lyric: ['outline', 'serifglow'], stingers: ['curtain', 'shutter'],
      bgEntries: ['sweepD', 'blinds'], deco: { lines: 'none', bokeh: .3, ghost: true, plates: false }, accent: '#ff9050', cam: 'mv' },
    moon_garden: { mood: 'calm', genres: ['ballad', 'ancient', 'dark'], scenes: ['galaxy', 'forest', 'lantern', 'aurora'],
      filter: 'moonlight', frames: ['corner', 'gold'], ext: ['goldleaf', 'starline'], lyric: ['serifglow', 'soft'], stingers: ['ink', 'iris'],
      bgEntries: ['iris', 'split'], deco: { lines: 'none', bokeh: .5, ghost: true, plates: false }, accent: '#c8d8ff', cam: 'calm' },
    glitch_pop: { mood: 'energetic', genres: ['edm', 'rock', 'dark'], scenes: ['neoncity', 'synth', 'wavefield', 'led'],
      filter: 'cyber_glitch', frames: ['hud', 'neonbar'], ext: ['glitchframe', 'cyberbrackets'], lyric: ['glitchy', 'neon'], stingers: ['glitch', 'slices'],
      bgEntries: ['sweepL', 'columns'], deco: { lines: 'tri', bokeh: .3, ghost: false, plates: false }, accent: '#ff2fd0', cam: 'cyber' },
    golden_stage: { mood: 'festive', genres: ['pop', 'ballad', 'jazz'], scenes: ['starstage', 'concert', 'sunset', 'festival'],
      filter: 'golden_hour', frames: ['gold', 'theater'], ext: ['artdeco', 'goldleaf'], lyric: ['serifglow', 'grad'], stingers: ['curtain', 'stars'],
      bgEntries: ['split', 'clock'], deco: { lines: 'none', bokeh: .7, ghost: true, plates: false }, accent: '#ffc070', cam: 'idol' },
    aquarium: { mood: 'calm', genres: ['ballad', 'cute', 'edm'], scenes: ['ocean', 'bubbles', 'crystal', 'aurora'],
      filter: 'aqua_dream', frames: ['corner', 'cinema'], ext: ['bubbleframe'], lyric: ['soft', 'outline'], stingers: ['iris', 'zoom'],
      bgEntries: ['sweepU', 'iris'], deco: { lines: 'none', bokeh: .6, ghost: true, plates: false }, accent: '#9fe8ff', cam: 'mv' },
    mono_mv: { mood: 'dark', genres: ['dark', 'rock', 'ballad', 'jazz'], scenes: ['rainwin', 'space', 'skyline', 'cafe'],
      filter: 'mono_mv', frames: ['cinema'], ext: ['comic'], lyric: ['outline', 'solid'], stingers: ['shutter', 'ink'],
      bgEntries: ['split', 'sweepD'], deco: { lines: 'none', bokeh: .2, ghost: true, plates: false }, accent: '#ffffff', cam: 'mv' },
    spring_letter: { mood: 'soft', genres: ['ballad', 'cute', 'ancient', 'pop'], scenes: ['sakura', 'classroom', 'sky', 'forest'],
      filter: 'spring_fade', frames: ['lace', 'polaroid'], ext: ['postcard', 'flowerwreath'], lyric: ['soft', 'ink'], stingers: ['petals', 'paper'],
      bgEntries: ['split', 'dots'], deco: { lines: 'none', bokeh: .6, ghost: true, plates: false }, accent: '#ffb3cf', cam: 'calm' },
    neon_rain: { mood: 'cool', genres: ['edm', 'dark', 'rock', 'pop'], scenes: ['neoncity', 'rainwin', 'skyline', 'tunnel'],
      filter: 'neon_rain', frames: ['neonbar', 'hud'], ext: ['neontube', 'window'], lyric: ['neon', 'chrome'], stingers: ['shutter', 'glitch'],
      bgEntries: ['sweepR', 'blinds'], deco: { lines: 'none', bokeh: .5, ghost: true, plates: false }, accent: '#ff4fa8', cam: 'cyber' },
    star_arena: { mood: 'bright', genres: ['pop', 'edm', 'rock', 'cute'], scenes: ['concert', 'beams', 'starstage', 'confetti'],
      filter: 'star_concert', frames: ['screen'], ext: ['gradientring', 'starline'], lyric: ['pop', 'chrome'], stingers: ['stars', 'zoom'],
      bgEntries: ['diamond', 'clock'], deco: { lines: 'slash', bokeh: .8, ghost: false, plates: false }, accent: '#fff0b8', cam: 'idol' },
    old_album: { mood: 'warm', genres: ['jazz', 'ballad', 'ancient'], scenes: ['cafe', 'sunset', 'lantern', 'classroom'],
      filter: 'sepia_memory', frames: ['polaroid', 'film'], ext: ['stamp', 'postcard'], lyric: ['ink', 'serifglow'], stingers: ['paper', 'ink'],
      bgEntries: ['sweepU', 'split'], deco: { lines: 'none', bokeh: .3, ghost: true, plates: false }, accent: '#f0d8b0', cam: 'mv' },
    ice_palace: { mood: 'cool', genres: ['ballad', 'edm', 'ancient', 'dark'], scenes: ['crystal', 'aurora', 'galaxy', 'space'],
      filter: 'ice_crystal', frames: ['corner', 'screen'], ext: ['starline', 'cloudframe'], lyric: ['soft', 'chrome'], stingers: ['zoom', 'iris'],
      bgEntries: ['diamond', 'iris'], deco: { lines: 'tri', bokeh: .6, ghost: false, plates: false }, accent: '#e0f4ff', cam: 'mv' },

    /* 第四十七轮：YouTube「歌ってみた MV / VTuber オリジナル曲」第二批参考（甘狼このみ『想わせ♡らぶりー』、
     * フレン『全肯定！』、兎迷夢々『Monster』、結城さくな『Re,bloom*』、海月とうみ MV 制作解说）——
     * 这些 MV 有的极简（大字 + 人物 + 纯色），有的极满（贴纸/字卡/网点/速度线塞满），但同一支 MV 里统一。 */
    lovely_pop: { mood: 'happy', genres: ['cute', 'pop'], scenes: ['pattern', 'hearts', 'confetti', 'bubbles'],
      filter: 'milk_pastel', frames: ['sticker', 'lace'], ext: ['washi', 'heartline', 'ribbon'], lyric: ['candy', 'sticker'], stingers: ['halftone', 'petals'],
      bgEntries: ['dots', 'diamond'], deco: { lines: 'none', bokeh: .5, ghost: false, plates: true }, accent: '#ff6fae', cam: 'idol', density: 'rich' },
    word_impact: { mood: 'energetic', genres: ['pop', 'rock', 'cute'], scenes: ['concert', 'starstage', 'beams'],
      filter: 'golden_hour', frames: [], ext: [], lyric: ['stamp', 'pop'], stingers: ['zoom', 'stripes'],
      bgEntries: ['split', 'diag'], deco: { lines: 'none', bokeh: .3, ghost: true, plates: false }, accent: '#ffcf4a', cam: 'idol', density: 'minimal' },
    moon_gothic: { mood: 'dark', genres: ['dark', 'rock', 'ballad'], scenes: ['galaxy', 'space', 'rainwin'],
      filter: 'moonlight', frames: ['theater'], ext: ['artdeco'], lyric: ['serifglow', 'outline'], stingers: ['ink', 'curtain'],
      bgEntries: ['iris', 'sweepD'], deco: { lines: 'none', bokeh: .3, ghost: true, plates: false }, accent: '#9ab0ff', cam: 'mv', density: 'balanced' },
    anime_op: { mood: 'bright', genres: ['pop', 'rock', 'edm', 'cute'], scenes: ['sky', 'skyline', 'kaleido', 'wavefield'],
      filter: 'summer_soda', frames: [], ext: [], lyric: ['grad', 'chrome'], stingers: ['zoom', 'slices'],
      bgEntries: ['sweepR', 'diag', 'columns'], deco: { lines: 'slash', bokeh: .4, ghost: false, plates: false }, accent: '#ff4f6a', cam: 'cyber', density: 'balanced' },
    motion_graphic: { mood: 'cool', genres: ['edm', 'pop', 'dark'], scenes: ['pattern', 'kaleido', 'led'],
      filter: 'mono_mv', frames: [], ext: [], lyric: ['solid', 'outline'], stingers: ['bars', 'blinds'],
      bgEntries: ['columns', 'blinds'], deco: { lines: 'tri', bokeh: 0, ghost: true, plates: false }, accent: '#f2c84b', cam: 'cyber', density: 'minimal' },
    manga_panel: { mood: 'warm', genres: ['ballad', 'pop', 'cute'], scenes: ['classroom', 'rooftop', 'sky'],
      filter: 'sepia_memory', frames: ['corner'], ext: ['comic'], lyric: ['ink', 'outline'], stingers: ['paper', 'halftone'],
      bgEntries: ['split', 'sweepU'], deco: { lines: 'none', bokeh: 0, ghost: false, plates: false }, accent: '#2a2a2a', cam: 'mv', density: 'balanced' }
  };

  /* 第五十轮：「线系」主题。参考 あくあ色ぱれっと / You＆合図 / かくれんぼ：
   * 元素极少（一种线 + 一两个色 + 大留白），复杂度放在「线怎么动」上——展开、收缩、叠成形状、跟拍子和旋律。
   * line: true 的主题：不叠滤镜蒙版/漏光/光斑/贴纸，不加前景粒子；背景只用线条场景；
   * ground：底色（paper 纯白稿纸 / tint 淡彩 / ink 深色）；vignettes：伴奏段换上的线稿小画；text：文字预设族。 */
  Object.assign(THEMES, {
    paper_score: { mood: 'calm', genres: ['ballad', 'pop', 'jazz', 'cute', 'ancient'], scenes: ['l_staff', 'l_keys', 'l_wave', 'l_script'],
      filter: 'plain', frames: [], ext: [], lyric: ['inkline', 'outline'], stingers: ['keys', 'lines'],
      bgEntries: ['sweepU', 'split'], deco: { lines: 'none', bokeh: 0, ghost: true, plates: false }, accent: '#ff6fae', cam: 'calm',
      density: 'minimal', line: true, ground: 'paper', vignettes: ['piano', 'metronome', 'clock'], text: 'editorial' },
    white_pop: { mood: 'happy', genres: ['pop', 'cute', 'edm'], scenes: ['l_rings', 'l_poly', 'l_wave', 'l_tick'],
      filter: 'plain', frames: [], ext: [], lyric: ['inkline', 'solid'], stingers: ['rings', 'lines'],
      bgEntries: ['iris', 'diamond'], deco: { lines: 'none', bokeh: 0, ghost: true, plates: false }, accent: '#3ad0c8', cam: 'idol',
      density: 'minimal', line: true, ground: 'paper', vignettes: ['record', 'equalizer'], text: 'poster' },
    pastel_line: { mood: 'soft', genres: ['cute', 'pop', 'ballad'], scenes: ['l_wave', 'l_rings', 'l_staff', 'l_script'],
      filter: 'plain', frames: [], ext: [], lyric: ['inkline', 'candy'], stingers: ['lines', 'rings'],
      bgEntries: ['split', 'sweepU'], deco: { lines: 'none', bokeh: 0, ghost: true, plates: false }, accent: '#ff8fbf', cam: 'calm',
      density: 'minimal', line: true, ground: 'tint', vignettes: ['clock', 'metronome'], text: 'handwrite' },
    film_letter: { mood: 'warm', genres: ['ballad', 'rock', 'pop', 'dark'], scenes: ['l_script', 'l_wave', 'l_lissa', 'l_staff'],
      filter: 'plain', frames: ['film'], ext: ['filmstrip2'], lyric: ['inkline', 'serifglow'], stingers: ['lines', 'keys'],
      bgEntries: ['sweepD', 'split'], deco: { lines: 'none', bokeh: 0, ghost: true, plates: false }, accent: '#c96ad8', cam: 'mv',
      density: 'balanced', line: true, ground: 'paper', vignettes: ['globe', 'record'], text: 'vertical' },
    night_wire: { mood: 'cool', genres: ['edm', 'dark', 'rock', 'ballad'], scenes: ['l_grid', 'l_lissa', 'l_strings', 'l_rings'],
      filter: 'plain', frames: [], ext: [], lyric: ['neon', 'outline'], stingers: ['lines', 'rings'],
      bgEntries: ['sweepL', 'columns'], deco: { lines: 'none', bokeh: 0, ghost: true, plates: false }, accent: '#7af0ff', cam: 'cyber',
      density: 'minimal', line: true, ground: 'ink', vignettes: ['equalizer', 'globe'], text: 'poster' },
    string_room: { mood: 'calm', genres: ['ballad', 'jazz', 'ancient', 'dark'], scenes: ['l_strings', 'l_keys', 'l_tick', 'l_wave'],
      filter: 'plain', frames: [], ext: [], lyric: ['serifglow', 'outline'], stingers: ['keys', 'lines'],
      bgEntries: ['split', 'iris'], deco: { lines: 'none', bokeh: 0, ghost: true, plates: false }, accent: '#ffcf8a', cam: 'calm',
      density: 'minimal', line: true, ground: 'ink', vignettes: ['piano', 'metronome', 'record'], text: 'editorial' },
    geo_bloom: { mood: 'bright', genres: ['pop', 'edm', 'cute', 'rock'], scenes: ['l_poly', 'l_rings', 'l_lissa', 'l_grid'],
      filter: 'plain', frames: [], ext: [], lyric: ['inkline', 'pop'], stingers: ['rings', 'keys'],
      bgEntries: ['diamond', 'clock'], deco: { lines: 'none', bokeh: 0, ghost: true, plates: false }, accent: '#ffb347', cam: 'idol',
      density: 'minimal', line: true, ground: 'tint', vignettes: ['equalizer', 'clock', 'record'], text: 'poster' }
  });

  // 第四十七轮：每个主题的「繁简」——决定边框、装饰层（光斑/几何线/背后大字/贴纸）开不开、开多少。
  // 用户要求：有的时候简洁，有的时候可以很满，但满的时候不能丢质感和风格；不能一直有边框或一直没边框，要按风格来。
  const DENSITY_OF = {
    starry_night: 'balanced', sakura_dream: 'balanced', idol_stage: 'rich', neon_city: 'rich', sunset_film: 'minimal', pastel_pop: 'rich',
    ocean_blue: 'minimal', lantern_fest: 'rich', retro_wave: 'rich', noir_theater: 'minimal', crystal_space: 'balanced', forest_breeze: 'minimal',
    holo_live3d: 'rich', utawaku_night: 'minimal', milk_tea: 'balanced', dreamcore: 'minimal', vhs_memory: 'balanced', arcade_crt: 'rich',
    lofi_bedroom: 'minimal', anime_ending: 'minimal', summer_live: 'rich', film_reel: 'balanced', moon_garden: 'minimal', glitch_pop: 'rich',
    golden_stage: 'rich', aquarium: 'minimal', mono_mv: 'minimal', spring_letter: 'balanced', neon_rain: 'balanced', star_arena: 'rich',
    old_album: 'balanced', ice_palace: 'balanced'
  };
  // 第四十七轮：每个主题的底色皮肤（以前整首固定用编排给的皮肤，pop 歌几乎全是奶白 anime_pastel → 整体发白）
  const SKIN_OF = {
    starry_night: 'anime_night', sakura_dream: 'anime_pastel', idol_stage: 'anime_neon', neon_city: 'anime_neon', sunset_film: 'anime_night',
    pastel_pop: 'anime_pastel', ocean_blue: 'anime_night', lantern_fest: 'anime_night', retro_wave: 'anime_neon', noir_theater: 'anime_night',
    crystal_space: 'anime_night', forest_breeze: 'anime_ink', holo_live3d: 'anime_neon', utawaku_night: 'anime_night', milk_tea: 'anime_pastel',
    dreamcore: 'anime_night', vhs_memory: 'anime_night', arcade_crt: 'anime_neon', lofi_bedroom: 'anime_night', anime_ending: 'anime_night',
    summer_live: 'anime_pastel', film_reel: 'anime_night', moon_garden: 'anime_night', glitch_pop: 'anime_neon', golden_stage: 'anime_night',
    aquarium: 'anime_night', mono_mv: 'anime_night', spring_letter: 'anime_ink', neon_rain: 'anime_neon', star_arena: 'anime_neon',
    old_album: 'anime_ink', ice_palace: 'anime_night', lovely_pop: 'anime_pastel', word_impact: 'anime_neon', moon_gothic: 'anime_night',
    anime_op: 'anime_neon', motion_graphic: 'anime_night', manga_panel: 'anime_ink'
  };
  // 繁简 → 各层的开关与强度（rich 不是「什么都加」：加的都是本主题自己的东西，强度也封顶）
  const DENSITY = {
    minimal:  { frame: false, bokeh: .35, lines: false, ghost: false, stickers: false, maskK: .6, particles: .5 },
    balanced: { frame: true,  bokeh: .7,  lines: false, ghost: true,  stickers: false, maskK: .85, particles: .8 },
    rich:     { frame: true,  bokeh: 1,   lines: true,  ghost: true,  stickers: true,  maskK: 1,   particles: 1 }
  };

  // 风格（preset）→ 优先主题
  const LINE_BY_PRESET = {
    ballad: ['paper_score', 'film_letter', 'string_room', 'pastel_line'], pop: ['white_pop', 'paper_score', 'geo_bloom', 'pastel_line', 'film_letter'],
    edm: ['night_wire', 'geo_bloom', 'white_pop'], rock: ['night_wire', 'film_letter', 'geo_bloom'],
    cute: ['pastel_line', 'white_pop', 'geo_bloom'], ancient: ['paper_score', 'string_room', 'film_letter'],
    jazz: ['string_room', 'paper_score', 'film_letter'], dark: ['night_wire', 'string_room', 'film_letter']
  };
  const PRESET_THEMES = {
    ballad: ['starry_night', 'sakura_dream', 'ocean_blue', 'sunset_film', 'forest_breeze', 'utawaku_night', 'dreamcore', 'anime_ending', 'moon_garden', 'aquarium', 'spring_letter', 'ice_palace', 'moon_gothic', 'manga_panel'],
    pop: ['idol_stage', 'pastel_pop', 'sakura_dream', 'sunset_film', 'holo_live3d', 'milk_tea', 'summer_live', 'golden_stage', 'star_arena', 'vhs_memory', 'lovely_pop', 'word_impact', 'anime_op', 'motion_graphic', 'manga_panel', 'neon_city', 'anime_ending', 'moon_garden'],
    edm: ['neon_city', 'retro_wave', 'crystal_space', 'idol_stage', 'holo_live3d', 'arcade_crt', 'glitch_pop', 'neon_rain', 'star_arena', 'ice_palace', 'anime_op', 'motion_graphic'],
    rock: ['retro_wave', 'neon_city', 'idol_stage', 'noir_theater', 'vhs_memory', 'film_reel', 'glitch_pop', 'mono_mv', 'star_arena', 'word_impact', 'anime_op', 'moon_gothic'],
    cute: ['pastel_pop', 'sakura_dream', 'idol_stage', 'crystal_space', 'milk_tea', 'holo_live3d', 'summer_live', 'arcade_crt', 'spring_letter', 'lovely_pop', 'word_impact', 'manga_panel'],
    ancient: ['lantern_fest', 'forest_breeze', 'sakura_dream', 'ocean_blue', 'moon_garden', 'spring_letter', 'old_album', 'anime_ending'],
    jazz: ['noir_theater', 'sunset_film', 'starry_night', 'utawaku_night', 'lofi_bedroom', 'film_reel', 'old_album', 'golden_stage'],
    dark: ['noir_theater', 'neon_city', 'crystal_space', 'ocean_blue', 'mono_mv', 'neon_rain', 'glitch_pop', 'moon_garden', 'dreamcore']
  };

  // 段落 → 角色（融合模式按角色分配主题）
  const ROLE = { intro: 'calm', verse: 'calm', pre: 'calm', chorus: 'peak', final: 'peak', bridge: 'alt', outro: 'calm' };

  function create(show, opts) {
    opts = opts || {};
    const preset = (show.style && show.style.preset) || 'pop';
    const seed = (opts.seed >>> 0) || (show.seed >>> 0) || 1;
    const fused = opts.fused !== false;     // 默认融合；用户在工作室选定风格时 fused=false
    const cands = (PRESET_THEMES[preset] || PRESET_THEMES.pop).slice();
    const r = k => h(seed * .00057 + k * 1.733);
    // 洗牌
    for (let i = cands.length - 1; i > 0; i--) { const j = Math.floor(r(i) * (i + 1)); const t = cands[i]; cands[i] = cands[j]; cands[j] = t; }
    // 第四十五轮：可直接指定主题（show.style.theme，工作室/预演用）
    const pinned = show.style && show.style.theme && THEMES[show.style.theme] ? show.style.theme : null;
    // 第五十轮：整首是「线系」还是「图景系」——同一首里不混（混了就又乱了）。用户可用 show.style.family 指定
    const fam = (show.style && show.style.family) || (r(77) < .6 ? 'line' : 'scene');
    if (fam === 'line' && !(show.style && show.style.theme)) {
      const lc = (LINE_BY_PRESET[preset] || LINE_BY_PRESET.pop).slice();
      for (let i = lc.length - 1; i > 0; i--) { const j = Math.floor(r(90 + i) * (i + 1)); const t2 = lc[i]; lc[i] = lc[j]; lc[j] = t2; }
      cands.length = 0; lc.forEach(x => cands.push(x));
    }
    const dens = n => (THEMES[n] && THEMES[n].density) || DENSITY_OF[n] || 'balanced';
    const pickBy = (want, avoid) => cands.find(n => want.includes(dens(n)) && !avoid.includes(n)) || cands.find(n => !avoid.includes(n)) || cands[0];
    let roles;
    if (pinned) roles = { calm: pinned, peak: pinned, alt: pinned };
    else if (fused && fam === 'line') {
      // 线系：主歌一个线主题，副歌换同族的另一个（线更密/形状更满），桥段回到第三个或主歌的
      roles = { calm: cands[0], peak: cands[1 % cands.length], alt: cands[2 % cands.length] };
    } else if (fused) {
      const calm = pickBy(['minimal', 'balanced'], []);
      const peak = pickBy(['rich'], [calm]);
      const alt = pickBy([dens(calm) === 'minimal' ? 'balanced' : 'minimal'], [calm, peak]);
      roles = { calm, peak, alt };
    } else roles = { calm: cands[0], peak: cands[0], alt: cands[0] };
    const pick = (arr, k) => arr[Math.floor(r(k) * arr.length) % arr.length];
    // 每个主题在这次演出里固定选好具体素材（同一主题复现时完全一致 → 一致性）
    const resolved = {};
    Object.values(roles).forEach((name, i) => {
      if (resolved[name]) return;
      const T = THEMES[name];
      const D = DENSITY[dens(name)];
      resolved[name] = Object.assign({ name }, T, {
        density: dens(name), D,
        frame: T.frames.length ? pick(T.frames, 10 + i) : null, extFrame: T.ext.length ? pick(T.ext, 20 + i) : null,
        useExt: T.ext.length && (!T.frames.length || r(30 + i) < .5),
        lyricMat: T.lyric[0], lyricAlt: T.lyric[1] || T.lyric[0],
        sceneOrder: T.scenes.slice().sort((a, b) => r(a.length * 7 + i) - r(b.length * 7 + i + 3))
      });
    });
    const secs = ((show.music && show.music.sections) || []).slice().sort((a, b) => a.t0 - b.t0);
    function themeOf(sectionId) { return resolved[roles[ROLE[sectionId] || 'calm']]; }
    function themeAt(t) {
      let sec = secs[0];
      for (const s of secs) if (s.t0 <= t) sec = s;
      return themeOf(sec ? sec.id : 'verse');
    }
    // 整场边框只用一个（换边框会让画面窗口忽大忽小）：取「主歌主题」的边框
    const frameTheme = resolved[roles.calm];
    const frameOfTheme = T => (!T.D.frame || (!T.frame && !T.extFrame)) ? null : (T.useExt && T.extFrame ? { ext: T.extFrame } : T.frame ? { kind: T.frame } : { ext: T.extFrame });
    return {
      fused, roles, themes: resolved, themeOf, themeAt,
      frame: frameOfTheme(frameTheme) || { none: true },
      // 第四十七轮：边框跟着段落主题走（极简主题的段落不加边框，满的主题加），切换时由 stage-look 交叉淡化
      // 第五十三轮：整首最多两种边框（主歌一种、副歌一种），而且只在少数段落出现（用户：「素材一般用一两次左右」）。
      // 边框只出现在 peak 角色（副歌/最终副歌）和第一个主歌段；其它段落不画边框。
      frameAt(t) {
        let sec = secs[0]; for (const s2 of secs) if (s2.t0 <= t) sec = s2;
        const id = sec ? sec.id : 'verse', role = ROLE[id] || 'calm';
        if (role === 'peak') return frameOfTheme(resolved[roles.peak]) || frameOfTheme(frameTheme);
        const firstVerse = secs.find(s2 => s2.id === 'verse');
        if (role === 'calm' && firstVerse && sec === firstVerse) return frameOfTheme(frameTheme);
        return null;
      },
      densityAt(t) { return themeAt(t).D; },
      family: fam,
      isLineAt(t) { return !!themeAt(t).line; },
      groundAt(t) { return themeAt(t).ground || null; },
      vignetteFor(n, t) { const T = themeAt(t); const v = T.vignettes || ['clock', 'record']; return v[n % v.length]; },
      textAt(t) { return themeAt(t).text || null; },
      skinAt(t) { const T = themeAt(t); if (T.line) return T.ground === 'ink' ? 'anime_night' : 'anime_ink'; return SKIN_OF[T.name] || null; },
      // 场景：每个主题内部按顺序轮换（同主题复现时依次用它的场景族）
      sceneFor(sectionId, n) { const T = themeOf(sectionId); return T.sceneOrder[n % T.sceneOrder.length]; },
      entryFor(sectionId, n) { const T = themeOf(sectionId); return T.bgEntries[n % T.bgEntries.length]; },
      stingerFor(sectionId, n) { const T = themeOf(sectionId); return T.stingers[n % T.stingers.length]; },
      filterFor(sectionId) { return themeOf(sectionId).filter; },
      lyricMatFor(sectionId, isLead) { const T = themeOf(sectionId); return isLead ? T.lyricMat : T.lyricAlt; },
      decoAt(t) {
        const T = themeAt(t), D = T.D;
        if (T.line) return { lines: 'none', bokeh: 0, ghost: true, plates: false, stickers: false, maskK: 0, particles: 0, line: true, leak: 0 };
        return { lines: D.lines ? T.deco.lines : 'none', bokeh: T.deco.bokeh * D.bokeh, ghost: T.deco.ghost && D.ghost, plates: T.deco.plates && D.stickers, stickers: D.stickers, maskK: D.maskK, particles: D.particles };
      },
      camFor(sectionId) { return themeOf(sectionId).cam || ({ calm: 'calm', soft: 'calm', bright: 'idol', happy: 'idol', festive: 'idol', energetic: 'cyber', cool: 'cyber', warm: 'mv', dark: 'mv', fresh: 'calm' })[themeOf(sectionId).mood] || 'mv'; },
      accentAt(t) { return themeAt(t).accent; }
    };
  }

  global.NekoStageArt = { THEMES, PRESET_THEMES, LINE_BY_PRESET, DENSITY, DENSITY_OF, SKIN_OF, create };
})(window);
