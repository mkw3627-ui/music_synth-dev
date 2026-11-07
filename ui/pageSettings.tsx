// 为她而唱 · 设置与环境（2026-11-04，方案第 2 节）
//
// 「电脑状态」用人话讲：显卡可用 / 不可用、显存、内存、环境体检。
// 变调、推理步数、用哪个声音这些一般不用改的，收进「高级设置」默认收起。
// 接口与原来 home.tsx 完全相同：ms_probe / ms_env_check / ms_set_config（没新增、没改名）。
// 数据优先用面板传下来的 ms_state 快照（plan / paths / engine / env）；快照里没有硬件明细时
// 再单独问一次 ms_hw_profile。
import { Alert, StatusBadge, useEffect, useRef, useState } from "@neko/plugin-ui"
import { callEntry, short } from "./api"
import { BRAND_DEEP, INK, INK_2, LINE, Bar, Btn, Col, Empty, Field, FoldCard, Grid, Input, KV, Muted, Panel, PRIMARY_BTN, Pill, Row, Section, Select, Toggle } from "./ui"

/** 把档位翻成一句人话（不出现 low/mid/high 这种词）。 */
function tierWord(plan: any): { text: string; tone: any } {
  const t = String((plan && plan.tier) || "")
  if (t === "high") return { text: "很好 · 又快又细", tone: "success" }
  if (t === "mid") return { text: "不错 · 日常够用", tone: "success" }
  if (t === "low") return { text: "一般 · 会慢一些", tone: "warning" }
  return { text: "没有可用显卡", tone: "warning" }
}

/** 体检结果里每一项的人话标题（后端给的是技术名）。 */
const ENV_WORD: any = {
  base_python: "基础 Python", python: "基础 Python", synth_env: "唱歌程序的环境", sep_env: "去伴奏程序的环境",
  ddsp_repo: "唱歌程序本体", ddsp_contentvec: "声音特征模型", ddsp_vocoder: "音色合成模型",
  ddsp_rmvpe: "音高识别模型", sep_karaoke: "去伴奏模型", uv: "uv 工具", ffmpeg: "音频工具 ffmpeg",
}

/** 把快照里的 env（按环境分组）翻成一行一项的体检表。 */
function envRowsFrom(env: any): any[] {
  if (!env || typeof env !== "object") return []
  const rows: any[] = []
  const push = (key: string, ok: boolean, detail: string, fix?: string) => rows.push({ key, label: ENV_WORD[key] || key, ok, detail, fix: ok ? "" : (fix || "") })
  if (env.ddsp_env) push("synth_env", !!env.ddsp_env.exists, "Python " + String(env.ddsp_env.python || "").split(/[\\/]/).pop() + (env.ddsp_env.torch ? " · torch " + env.ddsp_env.torch : ""))
  if (env.sep_env) push("sep_env", !!env.sep_env.exists, env.sep_env.audio_separator ? "audio_separator " + env.sep_env.audio_separator : "")
  if (env.ddsp_repo) push("ddsp_repo", !!(env.ddsp_repo.exists && env.ddsp_repo.entry), env.ddsp_repo.path || "", "在安装向导里重新装一次")
  const pre = env.pretrain || {}
  for (const k of ["contentvec", "vocoder", "rmvpe"]) {
    if (pre[k]) push("ddsp_" + k, !!pre[k].exists, pre[k].path || "", "在安装向导里下载")
  }
  if (env.sep_model) push("sep_karaoke", !!(env.sep_model.ckpt && env.sep_model.config), env.sep_model.dir || "", "在安装向导里下载")
  if (env.ffmpeg) push("ffmpeg", !!(env.ffmpeg.ok !== undefined ? env.ffmpeg.ok : env.ffmpeg.exists), env.ffmpeg.path || env.ffmpeg.detail || "", "在安装向导里装")
  return rows
}

export function PageSettings(props: { api: any; snap: any; onRefresh: () => void; onOpenWizard: () => void }) {
  const { api, snap, onRefresh } = props
  const s = snap || {}
  const plan = s.plan || {}
  const eng = s.engine || {}
  const paths = s.paths || {}
  const link: any = s.link || {}
  const linkOn = Boolean(link.running && link.source_control)
  const linkColor = linkOn ? "#16a34a" : link.running ? "#d97706" : "#94a3b8"
  const linkHalo = linkOn ? "rgba(22,163,74,0.16)" : link.running ? "rgba(217,119,6,0.16)" : "rgba(148,163,184,0.16)"
  const linkText = linkOn ? "已连接" : link.running ? "运行中 · 音源中控不可用" : "未连接"
  const linkSource = link.selected && link.selected.name ? link.selected.name : "未选择"
  const linkStateText = !link.listener_running ? (link.running ? "已让位" : "未启动") : link.listener_state === "recording" ? "正在录制" : link.listener_state === "waiting_for_source" ? "等待音乐应用" : link.listener_mode === "linked" ? "联动待命" : "独立待命"
  const linkFeature = (text: string) => (
    <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
      <span style={{ width: 6, height: 6, borderRadius: 6, background: linkColor, marginTop: 7, flex: "0 0 auto" }} />
      <div style={{ fontSize: 12.5, color: INK_2, lineHeight: 1.7 }}>{text}</div>
    </div>
  )
  const [hw, setHw] = useState<any>(s.hardware || null)
  const [busy, setBusy] = useState("")
  const [notice, setNotice] = useState<any>(null)
  const [env, setEnv] = useState<any>(() => ({ items: envRowsFrom(s.env) }))
  const [openAdv, setOpenAdv] = useState(false)
  const [soulx, setSoulx] = useState<any>(null)
  const [soulxJobId, setSoulxJobId] = useState("")
  const [soulxJob, setSoulxJob] = useState<any>(null)
  const [soulxBusy, setSoulxBusy] = useState("")
  const [keyShift, setKeyShift] = useState("0")
  const [registerShift, setRegisterShift] = useState("0")
  const [inferStep, setInferStep] = useState("50")
  const [previewStep, setPreviewStep] = useState("120")
  const [method, setMethod] = useState("euler")
  const [pitchExtractor, setPitchExtractor] = useState("rmvpe")
  const [f0Min, setF0Min] = useState("65")
  const [f0Max, setF0Max] = useState("800")
  const [threshold, setThreshold] = useState("-60")
  const [autoKey, setAutoKey] = useState(false)
  const [autoKeyMax, setAutoKeyMax] = useState("4")
  const [trainDuration, setTrainDuration] = useState("2")
  const [trainAmp, setTrainAmp] = useState("bf16")
  const [trainBatch, setTrainBatch] = useState("0")
  const [trainAccum, setTrainAccum] = useState("1")
  const [trainCacheFp16, setTrainCacheFp16] = useState(false)
  const [trainPitchAug, setTrainPitchAug] = useState(true)
  const [vocalReverb, setVocalReverb] = useState("0.12")
  const [reverbAuto, setReverbAuto] = useState(true)
  const [reverbAutoStrength, setReverbAutoStrength] = useState("1")
  const [stereoWidth, setStereoWidth] = useState("1")
  const [clarity, setClarity] = useState("1")
  const [vocalLevel, setVocalLevel] = useState("-2")
  const [bgmGain, setBgmGain] = useState("0")
  const [consonant, setConsonant] = useState("0.35")
  const [cacheDays, setCacheDays] = useState("7")
  const [cacheAuto, setCacheAuto] = useState(true)
  const [cacheInfo, setCacheInfo] = useState<any>(null)
  const [ckpt, setCkpt] = useState("")
  const [ckptPath, setCkptPath] = useState("")
  const init = useRef(false)

  useEffect(() => {
    if (init.current || !snap) return
    init.current = true
    setKeyShift(String(eng.key_shift ?? 0))
    setRegisterShift(String(eng.vocal_register_shift ?? 0))
    setInferStep(String(eng.infer_step ?? 50))
    setPreviewStep(String(eng.preview_step ?? 120))
    setMethod(String(eng.method ?? "euler"))
    setPitchExtractor(String(eng.pitch_extractor ?? "rmvpe"))
    setF0Min(String(eng.f0_min ?? 65))
    setF0Max(String(eng.f0_max ?? 800))
    setThreshold(String(eng.threshold ?? -60))
    setAutoKey(eng.auto_key !== undefined ? !!eng.auto_key : false)
    setAutoKeyMax(String(eng.auto_key_max ?? 4))
    const tr = eng.train || {}
    setTrainDuration(String(tr.duration ?? 2))
    setTrainAmp(String(tr.amp ?? "bf16"))
    setTrainBatch(String(tr.batch ?? 0))
    setTrainAccum(String(tr.accum ?? 1))
    setTrainCacheFp16(!!tr.cache_fp16)
    setTrainPitchAug(tr.pitch_aug !== undefined ? !!tr.pitch_aug : true)
    setVocalReverb(String(eng.vocal_reverb ?? 0.12))
    setReverbAuto(eng.reverb_auto !== undefined ? !!eng.reverb_auto : true)
    setReverbAutoStrength(String(eng.reverb_auto_strength ?? 1))
    setStereoWidth(String(eng.stereo_width ?? 1))
    setClarity(String(eng.clarity ?? 1))
    setVocalLevel(String(eng.vocal_level_db ?? -2))
    setBgmGain(String(eng.bgm_gain_db ?? 0))
    setConsonant(String(eng.consonant ?? 0.35))
    setCacheDays(String(eng.cache_days ?? 7))
    setCacheAuto(eng.cache_auto !== undefined ? !!eng.cache_auto : true)
    const cur = (eng.models || []).find((m: any) => m.current)
    setCkpt(cur ? cur.path : "")
  }, [snap])

  // 面板打开时快照可能还没到：等 snap 到了再把 env 填上；已经点过「体检」就用体检结果，别覆盖。
  useEffect(() => {
    if (!snap) return
    setEnv((cur: any) => (cur && Array.isArray(cur.items) && cur.items.length ? cur : { items: envRowsFrom(snap.env) }))
  }, [snap])

  // 快照里没有硬件明细时补问一次（同一个 ms_hw_profile，训练页也在用）
  useEffect(() => {
    if (hw && (hw.gpu || hw.ram_total_gib)) return
    let alive = true
    callEntry(api, "ms_hw_profile", {}, 60000)
      .then((r: any) => { if (alive && r && r.ok) setHw(r) })
      .catch(() => {})
    return () => { alive = false }
  }, [])

  const models = (Array.isArray(eng.models) ? eng.models : []).filter((m: any) => !m.training)
  const working = (Array.isArray(eng.models) ? eng.models : []).filter((m: any) => m.training)
  const tw = tierWord(plan)

  // 显卡：优先看 plan（快照里一定有），没有再退回硬件探测
  const gpuName = String(plan.gpu_name || (hw && hw.gpu && hw.gpu.name) || "")
  const gpuOk = Boolean(gpuName) && String(plan.device || "cuda") !== "cpu"
  const vramTotal = plan.vram_total_gib ?? (hw && hw.gpu && hw.gpu.total_gib)
  const vramFree = plan.vram_free_gib ?? (hw && hw.gpu && hw.gpu.free_gib)
  const ramTotal = plan.ram_total_gib ?? (hw && hw.ram_total_gib)
  const ramFree = plan.ram_available_gib ?? (hw && hw.ram_available_gib)

  async function run(key: string, fn: () => Promise<any>) {
    if (busy) return null
    setBusy(key)
    try { return await fn() } catch (e: any) {
      setNotice({ tone: "error", text: String((e && e.message) || e) })
      return null
    } finally { setBusy("") }
  }

  const probe = () => run("probe", async () => {
    const r: any = await callEntry(api, "ms_probe", {}, 60000)
    setNotice({ tone: "info", text: "已经重新看过这台电脑：" + short(r.brief, "完成", 200) })
    onRefresh()
  })

  const checkEnv = () => run("env", async () => {
    const r: any = await callEntry(api, "ms_env_check", {}, 120000)
    // ms_env_check 给的是 checks:[{item,ok,detail,fix}]；没有就退回快照里的 env 结构
    if (r && Array.isArray(r.checks)) setEnv({ items: r.checks.map((c: any) => ({ key: c.item, label: c.item, ok: !!c.ok, detail: c.detail, fix: c.fix })) })
    else setEnv({ items: envRowsFrom(s.env) })
    setNotice(null)
  })

  const saveEngine = () => run("engine", async () => {
    const key = Number(keyShift), step = Number(inferStep), pstep = Number(previewStep)
    if (!Number.isInteger(key) || key < -24 || key > 24) { setNotice({ tone: "error", text: "变调要填 -24～24 之间的整数" }); return }
    if (!Number.isInteger(step) || step < 5 || step > 200) { setNotice({ tone: "error", text: "推理步数要填 5～200 之间的整数" }); return }
    if (!Number.isInteger(pstep) || pstep < 5 || pstep > 400) { setNotice({ tone: "error", text: "试听步数要填 5～400 之间的整数" }); return }
    const reg = Number(registerShift)
    if (!Number.isInteger(reg) || reg < -12 || reg > 12) { setNotice({ tone: "error", text: "音区偏移要填 -12～12 之间的整数" }); return }
    const lo = Number(f0Min), hi = Number(f0Max), th = Number(threshold), akm = Number(autoKeyMax)
    if (!Number.isInteger(lo) || lo < 30 || lo > 300) { setNotice({ tone: "error", text: "音高下限要填 30～300 Hz" }); return }
    if (!Number.isInteger(hi) || hi < 200 || hi > 1500) { setNotice({ tone: "error", text: "音高上限要填 200～1500 Hz" }); return }
    if (lo >= hi) { setNotice({ tone: "error", text: "音高上限要高于下限" }); return }
    if (!Number.isInteger(th) || th < -100 || th > -10) { setNotice({ tone: "error", text: "静音阈值要填 -100～-10 dB" }); return }
    if (!Number.isInteger(akm) || akm < 0 || akm > 12) { setNotice({ tone: "error", text: "最大移调要填 0～12 半音" }); return }
    const td = Number(trainDuration)
    if (!Number.isFinite(td) || td < 1 || td > 10) { setNotice({ tone: "error", text: "训练片段长度要填 1～10 秒" }); return }
    const tb = Number(trainBatch)
    if (!Number.isInteger(tb) || tb < 0 || tb > 64) { setNotice({ tone: "error", text: "训练 batch 要填 0～64（0=自动）" }); return }
    const ta = Number(trainAccum)
    if (!Number.isInteger(ta) || ta < 1 || ta > 16) { setNotice({ tone: "error", text: "梯度累积要填 1～16" }); return }
    const verb = Number(vocalReverb), width = Number(stereoWidth)
    if (!Number.isFinite(verb) || verb < 0 || verb > 0.6) { setNotice({ tone: "error", text: "人声混响要填 0～0.6" }); return }
    if (!Number.isFinite(width) || width < 1 || width > 2) { setNotice({ tone: "error", text: "伴奏加宽要填 1.0～2.0" }); return }
    const cl = Number(clarity), vl = Number(vocalLevel), bg = Number(bgmGain)
    const ras = Number(reverbAutoStrength)
    if (!Number.isFinite(ras) || ras < 0 || ras > 1) { setNotice({ tone: "error", text: "混响跟随强度要填 0～1" }); return }
    if (!Number.isFinite(cl) || cl < 0 || cl > 1.5) { setNotice({ tone: "error", text: "人声清晰度要填 0～1.5" }); return }
    if (!Number.isFinite(vl) || vl < -12 || vl > 6) { setNotice({ tone: "error", text: "人声电平要填 -12～6 dB" }); return }
    if (!Number.isFinite(bg) || bg < -12 || bg > 12) { setNotice({ tone: "error", text: "伴奏音量要填 -12～12 dB" }); return }
    const cs = Number(consonant)
    if (!Number.isFinite(cs) || cs < 0 || cs > 0.8) { setNotice({ tone: "error", text: "咬字增强要填 0～0.8" }); return }
    const cd = Number(cacheDays)
    if (!Number.isInteger(cd) || cd < 1 || cd > 365) { setNotice({ tone: "error", text: "缓存保留天数要填 1～365" }); return }
    const args: any = {
      key_shift: key, vocal_register_shift: reg, infer_step: step, preview_step: pstep,
      vocal_reverb: verb, stereo_width: width,
      reverb_auto: !!reverbAuto, reverb_auto_strength: ras,
      clarity: cl, vocal_level_db: vl, bgm_gain_db: bg, consonant: cs,
      cache_days: cd, cache_auto: !!cacheAuto,
      method, pitch_extractor: pitchExtractor, f0_min: lo, f0_max: hi, threshold: th,
      auto_key: !!autoKey, auto_key_max: akm,
      train_duration: td, train_amp: trainAmp, train_batch: tb, train_accum: ta,
      train_cache_fp16: !!trainCacheFp16, train_pitch_aug: !!trainPitchAug,
    }
    if (ckptPath.trim()) args.ddsp_ckpt = ckptPath.trim()
    else if (ckpt) args.ddsp_ckpt = ckpt
    const r: any = await callEntry(api, "ms_set_config", args, 30000)
    setNotice({ tone: r.ok ? "info" : "error", text: r.ok ? "已保存" : "没保存成功：" + (r.error || "未知错误") })
    onRefresh()
  })

  const gb = (bytes: any) => (Number(bytes || 0) / 1073741824).toFixed(1) + " GB"
  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
  const waitCacheJob = async (jobId: string) => {
    if (!jobId) return
    for (let i = 0; i < 150; i++) {
      await sleep(2000)
      try {
        const st: any = await callEntry(api, "ms_state", {}, 30000)
        const j: any = st && st.jobs ? st.jobs[jobId] : null
        if (j && String(j.state || "") !== "running") {
          setNotice({
            tone: j.state === "done" ? "info" : "error",
            text: j.state === "done" ? "清理完成：" + (j.detail || "") : "清理失败：" + (j.detail || ""),
          })
          onRefresh()
          return
        }
      } catch (e) { /* 继续轮询 */ }
    }
    setNotice({ tone: "info", text: "清理还在后台进行，稍后点「查看可清理空间」确认。" })
  }

  const scanCache = () => run("cache-scan", async () => {
    const r: any = await callEntry(api, "ms_cache_report", {}, 180000)
    if (!r || !r.ok) { setNotice({ tone: "error", text: "读取磁盘信息失败" }); return }
    setCacheInfo(r)
    const u = r.usage?.items || {}
    setNotice({
      tone: "info",
      text: "总占用 " + gb(r.usage?.total_bytes) + " · 回收站 " + gb(u.trash)
        + " · 训练记录 " + gb(u.train_runs) + " · 曲目/临时 " + gb(u.work)
        + " · 超过 " + r.settings?.days + " 天可清理 " + gb(r.scan?.total_bytes),
    })
  })

  const cleanCache = () => run("cache-clean", async () => {
    const r: any = await callEntry(api, "ms_cache_cleanup", {
      // 手动清理不看天数：能再生成的全部清掉
      days: 0, dry_run: false, include_trash: true,
      include_train_data: true, include_track_stages: true, include_preview_cache: true,
      include_experiments: true, include_splits: true, include_exp_checkpoints: true,
    }, 600000)
    if (!r || !r.ok) {
      setNotice({ tone: "error", text: "清理失败：" + (r?.errors?.[0]?.error || "未知错误") })
      return
    }
    if (r.started) {
      setNotice({ tone: "info", text: "清理已在后台开始，完成后会自动提示。" })
      void waitCacheJob(String(r.job_id || ""))
      onRefresh()
      return
    }
    setNotice({ tone: "info", text: "已释放 " + gb(r.removed_bytes) + "（含回收站与全部可再生成缓存）" })
    onRefresh()
  })

  const emptyTrash = () => run("cache-trash", async () => {
    const r: any = await callEntry(api, "ms_empty_trash", {}, 30000)
    if (!r || !r.ok) {
      setNotice({ tone: "error", text: "清空失败：" + ((r && r.error) || "未知错误") })
      return
    }
    setNotice({ tone: "info", text: "正在后台清空回收站，完成后会自动提示。" })
    void waitCacheJob(String(r.job_id || ""))
    onRefresh()
  })

  const loadSoulx = async () => {
    try {
      const r: any = await callEntry(api, "ms_soulx_status", {}, 30000)
      setSoulx(r)
    } catch (e: any) { /* 下一拍再试 */ }
  }
  useEffect(() => { loadSoulx() }, [])
  // 从 ms_state 的 jobs 里接上正在跑的 SoulX 安装（面板重开/别处启动也能看到进度）
  useEffect(() => {
    if (soulxJobId) return
    const jobs: any = (snap && snap.jobs) || {}
    const hit = Object.keys(jobs).find((id: string) => {
      const j = jobs[id] || {}
      return j.job === "soulx-install" && (j.state === "running" || j.state === "queued")
    })
    if (hit) {
      setSoulxJobId(hit)
      setSoulxJob(jobs[hit] || null)
    }
  }, [snap])
  useEffect(() => {
    if (!soulxJobId) return
    let alive = true
    const tick = async () => {
      try {
        const r: any = await callEntry(api, "ms_ace_job", { job_id: soulxJobId }, 30000)
        if (!alive) return
        const j = (r && r.job) || null
        setSoulxJob(j)
        if (j && (j.state === "done" || j.state === "failed")) {
          setSoulxJobId("")
          setSoulxBusy("")
          loadSoulx()
          onRefresh()
        }
      } catch (e) { /* 下一拍再试 */ }
    }
    tick()
    const id = setInterval(tick, 3000)
    return () => { alive = false; clearInterval(id) }
  }, [soulxJobId])
  const startSoulx = async () => {
    setSoulxBusy("install")
    setNotice(null)
    try {
      const r: any = await callEntry(api, "ms_soulx_install", {}, 60000)
      if (r && r.ok && r.job_id) {
        setSoulxJobId(String(r.job_id))
        setNotice({ tone: "info", text: "开始下载/安装 SoulX，进度在下面。" })
      } else if (r && r.ok && r.ready) {
        setSoulxBusy("")
        setNotice({ tone: "info", text: "SoulX 已经装好了。" })
        loadSoulx()
      } else {
        setSoulxBusy("")
        setNotice({ tone: "error", text: "没能开始：" + ((r && r.error) || "未知错误") })
      }
    } catch (e: any) {
      setSoulxBusy("")
      setNotice({ tone: "error", text: String((e && e.message) || e) })
    }
  }

  const envItems = (env && Array.isArray(env.items)) ? env.items : []
  const envMissing = envItems.filter((it: any) => !it.ok)
  const soulxReady = Boolean(soulx && soulx.ready)

  return (
    <Col style={{ gap: 24 }}>
      {notice ? <Alert tone={notice.tone === "error" ? "warning" : "info"}>{notice.text}</Alert> : null}

      {/* 和她所听联动 */}
      <Section title="和她所听联动" extra={<Row style={{ gap: 8 }}>
        <span style={{ width: 9, height: 9, borderRadius: 9, background: linkColor, boxShadow: "0 0 0 4px " + linkHalo, display: "inline-block" }} />
        <Muted>{linkText}</Muted>
      </Row>}>
        <Col style={{ gap: 14 }}>
          <div style={{ fontSize: 13, color: INK_2, lineHeight: 1.8 }}>
            联动后，本插件直接用「为她所听」选中的那个应用当录制音源，歌名/歌手也优先采用它的识别结果；她没开时自动退回独立模式（本插件自己扫进程 + Windows 媒体会话）。
          </div>
          <Grid cols={3} min={200} gap={12}>
            <div style={{ padding: 14, borderRadius: 14, background: "rgba(248,250,252,0.95)", border: "1px solid " + LINE }}>
              <div style={{ fontSize: 11.5, color: INK_2 }}>当前音源</div>
              <div style={{ fontSize: 13.5, fontWeight: 700, color: INK, marginTop: 4 }}>{linkSource}</div>
              <div style={{ fontSize: 11.5, color: INK_2, marginTop: 3 }}>{link.capture_mode === "application" ? "逐进程应用声道" : (link.capture_mode || "未连接")}</div>
            </div>
            <div style={{ padding: 14, borderRadius: 14, background: "rgba(248,250,252,0.95)", border: "1px solid " + LINE }}>
              <div style={{ fontSize: 11.5, color: INK_2 }}>本插件监听</div>
              <div style={{ fontSize: 13.5, fontWeight: 700, color: INK, marginTop: 4 }}>{linkStateText}</div>
              <div style={{ fontSize: 11.5, color: INK_2, marginTop: 3 }}>她监听时自动让位；点停止后不再自动开</div>
            </div>
            <div style={{ padding: 14, borderRadius: 14, background: "rgba(248,250,252,0.95)", border: "1px solid " + LINE }}>
              <div style={{ fontSize: 11.5, color: INK_2 }}>AI 读取音乐信息</div>
              <div style={{ fontSize: 13.5, fontWeight: 700, color: INK, marginTop: 4 }}>{link.ai_delivery ? "已授权" : "未授权"}</div>
              <div style={{ fontSize: 11.5, color: INK_2, marginTop: 3 }}>{link.ai_delivery ? "歌名/歌手可由她所听校准" : "在为她所听里开启「AI 读取」"}</div>
            </div>
          </Grid>
          <Col style={{ gap: 6 }}>
            {linkFeature("音源跟着她走：不用在本插件重复选应用，她在为她所听里切到哪个，这边就采哪个")}
            {linkFeature("识歌更准：歌名/歌手优先采用她所听的识别结果，再回退到 Windows 媒体会话")}
            {linkFeature("隔离不变：就算联动，也只录那一个应用的进程树，AI 自己的声音和其他应用不会混进来")}
            {linkFeature("互斥保护：她正在监听时本插件自动让位，两边不会同时采集回环")}
            {linkFeature("自动退回：她没运行或没授权时，自动用本插件自己的进程扫描，录制照常")}
          </Col>
          {link.reason ? <div style={{ fontSize: 11.5, color: "#b45309" }}>提示：{link.reason}</div> : null}
          <Row style={{ gap: 10 }}><Btn onClick={onRefresh}>刷新联动状态</Btn></Row>
        </Col>
      </Section>

      {/* 电脑状态 */}
      <Section title="电脑状态" extra={<Pill tone={tw.tone}>{tw.text}</Pill>}>
        {snap ? (
          <Col style={{ gap: 14 }}>
            <Grid cols={3} min={220} gap={14}>
              <div style={{ padding: 16, borderRadius: 16, background: "rgba(248,250,252,0.95)", border: "1px solid " + LINE }}>
                <div style={{ fontSize: 11.5, color: INK_2 }}>显卡</div>
                <Row style={{ marginTop: 4 }}>
                  <StatusBadge tone={gpuOk ? "success" : "warning"}>{gpuOk ? "可用" : "不可用"}</StatusBadge>
                </Row>
                <div style={{ fontSize: 12, color: INK, marginTop: 6, wordBreak: "break-all" }}>
                  {gpuName || "没找到 NVIDIA 显卡，会改用处理器慢慢处理"}
                </div>
                {vramTotal ? <div style={{ fontSize: 11.5, color: INK_2, marginTop: 4 }}>显存 {vramTotal}GB · 现在空了 {vramFree ?? "?"}GB</div> : null}
              </div>
              <div style={{ padding: 16, borderRadius: 16, background: "rgba(248,250,252,0.95)", border: "1px solid " + LINE }}>
                <div style={{ fontSize: 11.5, color: INK_2 }}>内存</div>
                <div style={{ fontSize: 16, fontWeight: 700, color: INK, marginTop: 4 }}>{ramTotal ?? "?"} GB</div>
                <div style={{ fontSize: 11.5, color: INK_2, marginTop: 4 }}>现在空着 {ramFree ?? "?"} GB</div>
              </div>
              <div style={{ padding: 16, borderRadius: 16, background: "rgba(248,250,252,0.95)", border: "1px solid " + LINE }}>
                <div style={{ fontSize: 11.5, color: INK_2 }}>唱歌用的方式</div>
                <div style={{ fontSize: 16, fontWeight: 700, color: INK, marginTop: 4 }}>{plan.device === "cuda" || plan.device === "cuda:0" ? "显卡" : plan.device === "cpu" ? "处理器" : "自动"}</div>
                <div style={{ fontSize: 11.5, color: INK_2, marginTop: 4 }}>{plan.label ? "按空闲显存自动分配（" + plan.label + "）" : "按空闲显存自动选择"}</div>
              </div>
            </Grid>
            {(plan.warnings || []).map((w: string, i: number) => (
              <div key={i} style={{ fontSize: 12, color: "#b9750f", lineHeight: 1.7 }}>注意：{w}</div>
            ))}
            <Row style={{ flexWrap: "wrap" }}>
              <Btn disabled={!!busy} onClick={probe}>{busy === "probe" ? "检查中…" : "重新检查一次"}</Btn>
              <Btn disabled={!!busy} onClick={checkEnv}>{busy === "env" ? "体检中…" : "检查环境是否齐全"}</Btn>
              <Btn onClick={props.onOpenWizard}>重新跑安装向导</Btn>
              <Muted>{s.last_run && s.last_run.at ? "上次检查：" + s.last_run.at : "这次还没检查过"}</Muted>
            </Row>
          </Col>
        ) : <Empty>正在读取电脑信息…</Empty>}

        {envItems.length ? (
          <div style={{ marginTop: 14 }}>
            <Row style={{ justifyContent: "space-between", marginBottom: 8 }}>
              <div style={{ fontSize: 13.5, fontWeight: 700, color: INK }}>环境体检</div>
              <Muted>{envMissing.length ? "有 " + envMissing.length + " 项缺东西" : "全部正常"}</Muted>
            </Row>
            <Col style={{ gap: 8 }}>
              {envItems.map((it: any, i: number) => (
                <div key={i} style={{ display: "flex", gap: 10, alignItems: "flex-start", flexWrap: "wrap", padding: "8px 12px", borderRadius: 12, background: it.ok ? "rgba(232,247,239,0.55)" : "rgba(253,236,236,0.6)" }}>
                  <StatusBadge tone={it.ok ? "success" : "error"}>{it.ok ? "正常" : "缺东西"}</StatusBadge>
                  <div style={{ flex: "1 1 200px", minWidth: 160 }}>
                    <div style={{ fontSize: 12.5, color: INK }}>{ENV_WORD[it.key] || it.label || it.name || it.key || "一项"}</div>
                    {it.detail ? <div style={{ fontSize: 11.5, color: INK_2, wordBreak: "break-all" }}>{it.detail}</div> : null}
                    {!it.ok && it.fix ? <div style={{ fontSize: 11.5, color: "#b23b3b" }}>怎么修：{it.fix}</div> : null}
                  </div>
                </div>
              ))}
            </Col>
            {envMissing.length ? (
              <Row style={{ marginTop: 8 }}>
                <Muted>缺的这几项可以在安装向导里补，点上面的「重新跑安装向导」。</Muted>
              </Row>
            ) : null}
          </div>
        ) : null}
      </Section>

      {/* SoulX-Singer */}
      <Panel title="SoulX-Singer（原创歌曲后处理）"
             extra={<Pill tone={soulxReady ? "success" : "warning"}>{soulxReady ? "已安装" : "未安装"}</Pill>}>
        <Col style={{ gap: 12 }}>
          {soulxReady ? (
            <Muted>已就绪：在「原创歌曲」页把「唱完后再处理干声」选 SoulX-Singer SVC 就能用。</Muted>
          ) : (
            <Muted>
              需要下载 SVC 权重 + RMVPE（约 5.8GB）和运行环境（torch 约 2.9GB）。
              支持断点续传；下载会在这个页面显示进度。
            </Muted>
          )}
          {!soulxReady && soulx && Array.isArray(soulx.problems) && soulx.problems.length ? (
            <Muted>缺少：{soulx.problems.join("、")}</Muted>
          ) : null}
          {soulxJobId || soulxJob ? (
            <Col style={{ gap: 6 }}>
              <Bar pct={Number((soulxJob && soulxJob.progress) || 0)} />
              <Row style={{ justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
                <Muted>{soulxJob ? (soulxJob.detail || "正在准备…") : "正在启动…"}</Muted>
                <Muted>{Number((soulxJob && soulxJob.progress) || 0)}%</Muted>
              </Row>
            </Col>
          ) : null}
          <Row style={{ flexWrap: "wrap", gap: 8 }}>
            {!soulxReady ? (
              <Btn style={PRIMARY_BTN} disabled={!!soulxBusy || !!soulxJobId} onClick={startSoulx}>
                {soulxJobId ? "正在下载/安装…" : soulxJob && soulxJob.state === "failed" ? "继续下载/安装" : "下载并安装 SoulX"}
              </Btn>
            ) : null}
            <Btn onClick={loadSoulx}>重新检查</Btn>
          </Row>
        </Col>
      </Panel>

      {/* 装在哪儿 */}
      <Panel title="文件放在哪里" extra={<Muted>一般不用改</Muted>}>
        <KV items={[
          ["程序与模型", paths.compute_root || "—"],
          ["临时文件", paths.work_dir || "—"],
          ["现在的歌声", eng.ddsp_ckpt || "（还没选）"],
        ]} cols="110px" />
      </Panel>

      {/* 磁盘空间 */}
      <Panel title="磁盘空间" extra={<Muted>{paths.compute_root || "—"}</Muted>}>
        <Col style={{ gap: 12 }}>
          <div style={{ fontSize: 12.5, color: INK_2, lineHeight: 1.8 }}>
            手动「立即清理」会不看天数清空：回收站、训练特征缓存、曲目中间产物、试听缓存、实验目录、切歌临时文件和已结束训练的旧检查点。
            自动清理每天最多跑一次，按下面的保留天数处理。成品、当前模型、语料库和虚拟环境不会被删。
          </div>
          <Grid cols={2} min={220} gap={14}>
            <Field label="自动清理保留天数（1～365）">
              <Input type="number" min={1} max={365} step={1} value={cacheDays}
                     onChange={(e: any) => setCacheDays(e.target.value)} />
            </Field>
            <Field label="自动清理">
              <Toggle on={cacheAuto} onChange={setCacheAuto} label="每天自动清理一次" />
            </Field>
          </Grid>
          <Row style={{ flexWrap: "wrap", gap: 8 }}>
            <Btn disabled={!!busy} onClick={scanCache}>{busy === "cache-scan" ? "扫描中…" : "查看可清理空间"}</Btn>
            <Btn style={PRIMARY_BTN}
                 disabled={!!busy} onClick={cleanCache}>{busy === "cache-clean" ? "清理中…" : "立即清理（全部）"}</Btn>
            <Btn style={{ background: "#c93c3c", color: "#fff", borderColor: "#c93c3c" }}
                 disabled={!!busy} onClick={emptyTrash}>
              {busy === "cache-trash" ? "清空中…" : "清空回收站"}
            </Btn>
          </Row>
          {cacheInfo ? (
            <KV items={[
              ["总占用", gb(cacheInfo.usage?.total_bytes)],
              ["回收站", gb(cacheInfo.usage?.items?.trash)],
              ["训练记录", gb(cacheInfo.usage?.items?.train_runs)],
              ["曲目/临时", gb(cacheInfo.usage?.items?.work)],
              ["超过保留天数可清理", gb(cacheInfo.scan?.total_bytes)],
            ]} cols="150px" />
          ) : null}
        </Col>
      </Panel>

      {/* 高级设置 */}
      <FoldCard title="高级设置（一般不用改）" open={openAdv} onToggle={() => setOpenAdv(!openAdv)}
                sub={<Muted>变调 · 步数 · 音高 · 训练参数</Muted>}>
        <Col style={{ gap: 14 }}>
          <div style={{ fontSize: 12.5, color: INK_2, lineHeight: 1.8 }}>
            变调调高会让声音更高，调低更沉；推理步数越大越细致，也越慢。不知道选什么就保持原样。
          </div>
          <Grid cols={2} min={220} gap={14}>
            <Field label="变调（半音，-24～24）">
              <Input type="number" min={-24} max={24} step={1} value={keyShift} onChange={(e: any) => setKeyShift(e.target.value)} />
            </Field>
            <Field label="推理步数（5～200）">
              <Input type="number" min={5} max={200} step={1} value={inferStep} onChange={(e: any) => setInferStep(e.target.value)} />
            </Field>
            <Field label="音区偏移（-12～12，±3 内高保真）">
              <Input type="number" min={-12} max={12} step={1} value={registerShift} onChange={(e: any) => setRegisterShift(e.target.value)} />
            </Field>
          </Grid>
          {(Number(inferStep) > 80 || method === "rk4") ? (
            <div style={{ fontSize: 11.5, color: "#b9750f", lineHeight: 1.7 }}>
              当前 {inferStep} 步 · {method}。步数太高或选 rk4 会让显卡长时间满载，唱歌时电脑明显变卡；
              一般 50 步 + euler 就够，150 步 + rk4 的计算量约是它的 12 倍。
            </div>
          ) : null}

          <div style={{ fontSize: 13, fontWeight: 700, color: INK }}>出歌细节</div>
          <Grid cols={3} min={200} gap={14}>
            <Field label="试听步数（5～400）">
              <Input type="number" min={5} max={400} step={1} value={previewStep} onChange={(e: any) => setPreviewStep(e.target.value)} />
            </Field>
            <Field label="采样方法">
              <Select value={method} onChange={(e: any) => setMethod(e.target.value)}>
                <option value="euler">euler（默认，快）</option>
                <option value="rk4">rk4（更慢，细节多）</option>
              </Select>
            </Field>
            <Field label="音高提取器">
              <Select value={pitchExtractor} onChange={(e: any) => setPitchExtractor(e.target.value)}>
                <option value="rmvpe">rmvpe（默认）</option>
                <option value="harvest">harvest（颤音更稳）</option>
                <option value="parselmouth">parselmouth</option>
                <option value="fcpe">fcpe</option>
                <option value="crepe">crepe</option>
                <option value="dio">dio</option>
              </Select>
            </Field>
          </Grid>
          <Grid cols={3} min={200} gap={14}>
            <Field label="音高下限（30～300 Hz）">
              <Input type="number" min={30} max={300} step={1} value={f0Min} onChange={(e: any) => setF0Min(e.target.value)} />
            </Field>
            <Field label="音高上限（200～1500 Hz）">
              <Input type="number" min={200} max={1500} step={1} value={f0Max} onChange={(e: any) => setF0Max(e.target.value)} />
            </Field>
            <Field label="静音阈值（-100～-10 dB）">
              <Input type="number" min={-100} max={-10} step={1} value={threshold} onChange={(e: any) => setThreshold(e.target.value)} />
            </Field>
          </Grid>
          <Grid cols={2} min={220} gap={14}>
            <Field label={reverbAuto ? "人声混响（自动跟随原曲）" : "人声混响（0～0.6，0=关）"}>
              <Input type="number" min={0} max={0.6} step={0.02} value={vocalReverb}
                     disabled={reverbAuto}
                     onChange={(e: any) => setVocalReverb(e.target.value)} />
            </Field>
            <Field label="混响跟随强度（0～1）">
              <Input type="number" min={0} max={1} step={0.05} value={reverbAutoStrength}
                     disabled={!reverbAuto}
                     onChange={(e: any) => setReverbAutoStrength(e.target.value)} />
            </Field>
            <Field label="伴奏加宽（1.0=原样，2.0=最宽）">
              <Input type="number" min={1} max={2} step={0.05} value={stereoWidth} onChange={(e: any) => setStereoWidth(e.target.value)} />
            </Field>
            <Field label="人声清晰度（0=关，1=默认，1.5=最亮）">
              <Input type="number" min={0} max={1.5} step={0.1} value={clarity} onChange={(e: any) => setClarity(e.target.value)} />
            </Field>
            <Field label="人声电平（-12～6 dB）">
              <Input type="number" min={-12} max={6} step={0.5} value={vocalLevel} onChange={(e: any) => setVocalLevel(e.target.value)} />
            </Field>
            <Field label="伴奏音量（-12～12 dB）">
              <Input type="number" min={-12} max={12} step={0.5} value={bgmGain} onChange={(e: any) => setBgmGain(e.target.value)} />
            </Field>
            <Field label="咬字增强（0=关，0.35=默认，0.8=最强）">
              <Input type="number" min={0} max={0.8} step={0.05} value={consonant} onChange={(e: any) => setConsonant(e.target.value)} />
            </Field>
          </Grid>
          <Row style={{ flexWrap: "wrap", alignItems: "center", gap: 18 }}>
            <Toggle on={reverbAuto} onChange={setReverbAuto} label="人声混响跟随原曲（自动测量）" />
            <Toggle on={autoKey} onChange={setAutoKey} label="自动配调（把歌挪到模型熟悉的音域）" />
            <Field label="最大移调（半音）" style={{ width: 150 }}>
              <Input type="number" min={0} max={12} step={1} value={autoKeyMax} disabled={!autoKey} onChange={(e: any) => setAutoKeyMax(e.target.value)} />
            </Field>
          </Row>

          <div style={{ fontSize: 13, fontWeight: 700, color: INK, marginTop: 4 }}>下次训练（不影响正在跑的那次）</div>
          <Grid cols={2} min={220} gap={14}>
            <Field label="训练片段长度（1～10 秒）">
              <Input type="number" min={1} max={10} step={0.5} value={trainDuration} onChange={(e: any) => setTrainDuration(e.target.value)} />
            </Field>
            <Field label="训练精度">
              <Select value={trainAmp} onChange={(e: any) => setTrainAmp(e.target.value)}>
                <option value="bf16">bf16（推荐，稳）</option>
                <option value="fp16">fp16（快，略有毛刺）</option>
                <option value="fp32">fp32（最稳，最慢）</option>
              </Select>
            </Field>
            <Field label="训练 batch（0=自动；显存不够会失败）">
              <Input type="number" min={0} max={64} step={1} value={trainBatch} onChange={(e: any) => setTrainBatch(e.target.value)} />
            </Field>
            <Field label="梯度累积（1～16；等效 batch = batch × 累积）">
              <Input type="number" min={1} max={16} step={1} value={trainAccum} onChange={(e: any) => setTrainAccum(e.target.value)} />
            </Field>
          </Grid>
          <Muted>例：batch 12 × 累积 2 = 等效 24，显存只按 12 算；步数/里程碑按参数更新次数算。</Muted>
          <Row style={{ flexWrap: "wrap", gap: 18 }}>
            <Toggle on={trainCacheFp16} onChange={setTrainCacheFp16} label="特征缓存用半精度（省显存）" />
            <Toggle on={trainPitchAug} onChange={setTrainPitchAug} label="音高随机增广（防过拟合）" />
          </Row>
          <div style={{ fontSize: 11.5, color: INK_2, lineHeight: 1.8 }}>
            小提示：觉得声音发“电/沙”时，先把试听步数调到 120～200、音高提取器在 rmvpe / harvest 之间对比；
            下次训练可以试 bf16 + 片段长度 2 秒。设置保存后，出歌类参数立即生效，训练类参数从下一次训练开始生效。
          </div>
          <Field label="用哪个声音">
            <Select value={ckpt} onChange={(e: any) => setCkpt(e.target.value)}>
              <option value="">自动（最新训练好的）</option>
              {models.map((m: any) => (
                <option key={m.path} value={m.path}>{(m.exp_dir || m.name) + (m.has_config ? "" : "（缺配置，可能用不了）")}</option>
              ))}
              {working.length ? (
                <optgroup label="还在训练（暂时不能用）">
                  {working.map((m: any) => <option key={m.path} value={m.path}>{m.exp_dir || m.name}</option>)}
                </optgroup>
              ) : null}
            </Select>
          </Field>
          <Field label="或者直接填模型文件位置（留空 = 用上面选的）">
            <Input placeholder="留空 = 用上面选中的声音" value={ckptPath} onChange={(e: any) => setCkptPath(e.target.value)} />
          </Field>
          <Row>
            <Btn style={PRIMARY_BTN} disabled={!!busy} onClick={saveEngine}>{busy === "engine" ? "保存中…" : "保存"}</Btn>
            {models.length === 0 ? <Muted>还没有训练好的声音，先去「训练声音」练一个。</Muted> : null}
          </Row>
          <Muted style={{ color: BRAND_DEEP }}>提示：想换声音更简单的方式是去「我的声音」页点「设为当前」。</Muted>
        </Col>
      </FoldCard>

      <Muted>所有处理都在本机完成，不会把音频上传到网上。</Muted>
    </Col>
  )
}
