// 为她而唱 · 首次使用引导（2026-11-07 重做）
//
// 五页：① 先听效果（内置音色「沫沫」）② 功能一览 ③ 使用流程
//       ④ 下载依赖与环境（原安装向导的全部内容） ⑤ 完成
// 旧四步向导的每一句说明、8 个依赖项的名字、安装/失败/完成文案都保留在
// 第 4、5 页里，只是排版、分页和节奏全部重做：先体验 → 再介绍 → 最后装环境。
// 依赖仍然只走既有接口：ms_setup_check / ms_setup_install / ms_setup_status。
// 预览（?wizard= / ?bundle=）下检测不到接口时用模拟清单走完流程，页面标「模拟演示」。
import { Alert, StatusBadge, useEffect, useRef, useState } from "@neko/plugin-ui"
import { audioSrcOf, callEntry, callEntryLoose } from "./api"
import { BRAND, BRAND_DEEP, Btn, INK, INK_2, INK_3, LINE, Muted, Panel, Pill, Row, Bar, fmtEta } from "./ui"

type Item = { key: string; label: string; status: string; ok: boolean; required: boolean; installable: boolean; size_gb: number; detail: string; fix: string }
type Report = { ready: boolean; items: Item[]; suggested_root: string; total_missing_gb: number; [k: string]: any }

const STEPS = ["体验", "功能", "用法", "环境", "开始"]
const STEP_TITLES = ["先听听效果", "她能做什么", "怎么用", "准备运行环境", "准备完成"]

/** 只有开发预览（?wizard= / ?bundle=）才允许用示例清单，正式使用必须读真实检测结果。 */
function previewMode(): boolean {
  try {
    const q = String((window as any).location && (window as any).location.search || "")
    return /[?&](wizard|bundle)=/.test(q)
  } catch (e) { return false }
}

// ---- 模拟：preview 里没有安装接口时给一份示例清单，只为验收界面 ------------
function mockReport(root: string, ready: boolean): Report {
  const items: Item[] = [
    { key: "base_python", label: "基础 Python（建虚拟环境用）", status: "ready", ok: true, required: true, installable: false, size_gb: 0, detail: "3.10.11 · 系统已装", fix: "" },
    { key: "synth_env", label: "DDSP-SVC 环境", status: "ready", ok: true, required: true, installable: false, size_gb: 0, detail: "已就绪", fix: "" },
    { key: "sep_env", label: "人声分离环境", status: "ready", ok: true, required: true, installable: false, size_gb: 0, detail: "已就绪", fix: "" },
    { key: "ddsp_repo", label: "人声模型程序", status: "ready", ok: true, required: true, installable: false, size_gb: 0, detail: "已就绪", fix: "" },
    { key: "ddsp_contentvec", label: "声音特征模型（ContentVec）", status: ready ? "ready" : "missing", ok: ready, required: true, installable: !ready, size_gb: 0.2, detail: ready ? "已就绪" : "缺少 1/1 个文件", fix: "自动下载，约 0.2GB" },
    { key: "ddsp_vocoder", label: "音色合成模型（NSF-HiFiGAN）", status: ready ? "ready" : "missing", ok: ready, required: true, installable: !ready, size_gb: 0.05, detail: ready ? "已就绪" : "缺少 1/1 个文件", fix: "自动下载，约 0.05GB" },
    { key: "ddsp_rmvpe", label: "音高识别模型（RMVPE）", status: ready ? "ready" : "missing", ok: ready, required: true, installable: !ready, size_gb: 0.06, detail: ready ? "已就绪" : "缺少 1/1 个文件", fix: "自动下载，约 0.06GB" },
    { key: "sep_karaoke", label: "去伴奏模型（Karaoke）", status: ready ? "ready" : "missing", ok: ready, required: true, installable: !ready, size_gb: 1.75, detail: ready ? "已就绪" : "缺少 1/1 个文件", fix: "自动下载，约 1.75GB" },
  ]
  const total = items.filter((i) => !i.ok).reduce((a, i) => a + i.size_gb, 0)
  return { ready, items, suggested_root: root, total_missing_gb: Math.round(total * 10) / 10,
           mock: true, hardware_brief: "显卡：NVIDIA GeForce RTX 3060（模拟）" }
}

// 模拟安装进度：按清单顺序逐项跑，每项大约 6 秒
function mockProgress(report: Report, t: number) {
  const todo = report.items.filter((i) => i.installable && i.status !== "ready")
  const items: any = {}
  for (const i of report.items) items[i.key] = { label: i.label, state: i.status === "ready" ? "ready" : "wait", pct: null }
  const per = 6
  const idx = Math.floor(t / per)
  for (let k = 0; k < Math.min(idx, todo.length); k++) items[todo[k].key] = { ...items[todo[k].key], state: "done", pct: 100 }
  if (idx < todo.length) {
    const cur = todo[idx]
    const p = Math.min(96, Math.round(((t % per) / per) * 96))
    items[cur.key] = { ...items[cur.key], state: "active", pct: p,
                       done_mb: Math.round(cur.size_gb * 1024 * p / 100), total_mb: Math.round(cur.size_gb * 1024) }
  }
  const doneAll = idx >= todo.length
  return {
    items, current: doneAll ? "" : todo[idx].key,
    job_state: doneAll ? "done" : "running",
    detail: doneAll ? "安装完成" : "下载 " + todo[idx].label + "：" + Math.round((t % per) / per * (todo[idx].size_gb * 1024)) + "/" + Math.round(todo[idx].size_gb * 1024) + " MB",
  }
}

const STATE_TEXT: { [k: string]: { text: string; tone: any } } = {
  ready: { text: "已就绪", tone: "success" },
  wait: { text: "等待", tone: "neutral" },
  missing: { text: "待安装", tone: "warning" },
  missing_required: { text: "缺少", tone: "error" },
  active: { text: "安装中", tone: "warning" },
  done: { text: "完成", tone: "success" },
  failed: { text: "失败", tone: "error" },
}

function StateIcon(props: { state: string }) {
  const st = props.state || "wait"
  const color = st === "ready" || st === "done" ? "#1c8a58" : st === "failed" || st === "missing_required" ? "#c93c3c" : st === "active" ? BRAND : st === "missing" ? "#b9750f" : "#b9b3c1"
  const bg = st === "ready" || st === "done" ? "#e8f7ef" : st === "failed" || st === "missing_required" ? "#fdecec" : st === "active" ? "#ffe5ef" : st === "missing" ? "#fff3e0" : "rgba(40,37,50,0.06)"
  return (
    <span aria-hidden style={{ width: 24, height: 24, borderRadius: 999, background: bg, color, display: "inline-grid", placeItems: "center", flex: "0 0 auto" }}>
      {st === "ready" || st === "done" ? (
        <svg width="13" height="13" viewBox="0 0 12 12"><path d="M2 6.4 4.8 9 10 3.2" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>
      ) : st === "failed" || st === "missing_required" ? <b style={{ fontSize: 13, lineHeight: 1 }}>!</b>
        : st === "active" ? <i style={{ width: 9, height: 9, borderRadius: 999, background: color, display: "block" }} />
          : <i style={{ width: 6, height: 6, borderRadius: 999, background: color, display: "block" }} />}
    </span>
  )
}

const primaryBtn = { border: 0, background: BRAND, color: "#fff", borderRadius: 12, boxShadow: "0 6px 16px rgba(224,45,118,0.22)", padding: "11px 22px", fontSize: 13.5 }
const ghostBtn = { border: "1px solid rgba(224,45,118,0.16)", background: "#fff", color: "#9c204e", borderRadius: 12, padding: "10px 18px", fontSize: 13 }

function mmss(sec: number): string {
  const n = Math.max(0, Math.floor(Number(sec) || 0))
  return Math.floor(n / 60) + ":" + (n % 60 < 10 ? "0" : "") + (n % 60)
}

/* ------------------------------ 功能一览数据 ------------------------------ */
const FEATURES = [
  { key: "listen", title: "听歌识曲", text: "电脑上正在放的歌，她也能“听”到，直接录下来。", tag: "无需下载歌曲" },
  { key: "separate", title: "人声分离", text: "自动拆出伴奏和干声，方便重新演唱。", tag: "保留伴奏" },
  { key: "cover", title: "翻唱变声", text: "用她的音色重唱你喜欢的歌。", tag: "换音色" },
  { key: "train", title: "训练自己的音色", text: "放几首歌或录音，就能练出属于你的声音。", tag: "越用越像" },
  { key: "create", title: "原创歌曲", text: "让她的声音唱一首全新的歌。", tag: "AI 作曲" },
  { key: "mix", title: "混音出曲", text: "人声与伴奏自动对齐，混成完整成品。", tag: "一键出歌" },
  { key: "ai", title: "AI 可调用演唱", text: "接入 AI 对话，随时让她为你唱一句。", tag: "联动演奏" },
]

function FeatureIcon(props: { name: string }) {
  const c = { width: 30, height: 30, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.7, strokeLinecap: "round", strokeLinejoin: "round" } as any
  if (props.name === "listen") return <svg {...c}><path d="M4 14v-2a8 8 0 0 1 16 0v2" /><rect x="3" y="14" width="4.2" height="6.4" rx="2.1" /><rect x="16.8" y="14" width="4.2" height="6.4" rx="2.1" /></svg>
  if (props.name === "separate") return <svg {...c}><circle cx="7" cy="7" r="2.4" /><circle cx="17" cy="17" r="2.4" /><path d="M8.8 8.8 15.2 15.2" /><path d="M4 17h4M16 4h4" /></svg>
  if (props.name === "cover") return <svg {...c}><rect x="8" y="2.6" width="5.2" height="10" rx="2.6" /><path d="M5 10.4a5.6 5.6 0 0 0 11.2 0" /><path d="M10.6 16v3" /></svg>
  if (props.name === "train") return <svg {...c}><path d="M4 19V9M9.3 19V4.6M14.6 19v-6.8M20 19V7.4" /></svg>
  if (props.name === "create") return <svg {...c}><path d="M12 3.4l1.9 5.1 5.1 1.9-5.1 1.9L12 17.4l-1.9-5.1L5 10.4l5.1-1.9z" /><path d="M18.4 16.4l.8 2.1 2.1.8-2.1.8-.8 2.1-.8-2.1-2.1-.8 2.1-.8z" /></svg>
  if (props.name === "mix") return <svg {...c}><path d="M4 7h16M4 12h16M4 17h16" /><circle cx="9" cy="7" r="1.8" fill="currentColor" stroke="none" /><circle cx="15" cy="12" r="1.8" fill="currentColor" stroke="none" /><circle cx="8" cy="17" r="1.8" fill="currentColor" stroke="none" /></svg>
  return <svg {...c}><path d="M20 12a8 8 0 1 0-3.1 6.3L20 19l-.6-3.1A7.9 7.9 0 0 0 20 12z" /><path d="M8.4 12h.01M12 12h.01M15.6 12h.01" strokeWidth="2.2" /></svg>
}

/* --------------------------------- 主体 --------------------------------- */
export function Onboarding(props: { api: any; onDone: () => void; onSkip?: () => void; startAt?: number }) {
  const [step, setStep] = useState<number>(() => {
    const n = Number(props.startAt || 1)
    return n >= 1 && n <= 5 ? n : 1
  })

  /* ---------------- 第 1 页：体验（内置音色「沫沫」） ---------------- */
  const [src, setSrc] = useState("")
  const [playing, setPlaying] = useState(false)
  const [pos, setPos] = useState(0)
  const [dur, setDur] = useState(0)
  const [loading, setLoading] = useState(true)
  const [audioErr, setAudioErr] = useState("")
  const audioRef = useRef<any>(null)

  const loadDemo = async () => {
    setLoading(true)
    setAudioErr("")
    try {
      const r: any = await callEntry(props.api, "ms_ace_preview", { name: "沫沫" }, 120000)
      const s = audioSrcOf(r, "audio/mpeg")
      if (!s) throw Error("没有拿到试听音频")
      setSrc(s)
      setLoading(false)
      return
    } catch (e: any) {
      try {
        const r2: any = await callEntry(props.api, "ms_train_preview", { run_id: "沫沫" }, 120000)
        const s2 = audioSrcOf(r2, "audio/mpeg")
        if (!s2) throw Error("没有拿到试听音频")
        setSrc(s2)
      } catch (e2: any) {
        setAudioErr("试听加载失败：" + String((e2 && e2.message) || e2))
      }
    }
    setLoading(false)
  }
  useEffect(() => { loadDemo() }, [])
  // 离开第 1 页就停掉试听，回来时按钮不会停在「暂停」上
  useEffect(() => {
    if (step === 1) return
    try { if (audioRef.current) audioRef.current.pause() } catch (e) { /* 元素已卸载 */ }
    setPlaying(false)
  }, [step])

  const toggle = async () => {
    const a = audioRef.current
    if (!a) return
    try {
      if (a.paused) await a.play()
      else a.pause()
    } catch (e: any) {
      setAudioErr("播放失败：" + String((e && e.message) || e))
    }
  }

  const seek = (e: any) => {
    const a = audioRef.current
    if (!a || !dur) return
    const rect = e.currentTarget.getBoundingClientRect()
    const ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width))
    a.currentTime = ratio * dur
    setPos(a.currentTime)
  }

  /* ---------------- 第 2 页：功能横向翻页 ---------------- */
  const [feat, setFeat] = useState(0)
  const featPrev = () => setFeat((i) => Math.max(0, i - 1))
  const featNext = () => setFeat((i) => Math.min(FEATURES.length - 1, i + 1))

  /* ---------------- 第 4 页：依赖与环境（沿用旧安装向导） ---------------- */
  const [report, setReport] = useState<Report | null>(null)
  const [mockMode, setMockMode] = useState(false)
  const [path, setPath] = useState("")
  const [freeGb, setFreeGb] = useState<number | null>(null)
  const [busy, setBusy] = useState("")
  const [err, setErr] = useState("")
  const [job, setJob] = useState<any>(null)
  const [prog, setProg] = useState<any>(null)
  const [installing, setInstalling] = useState(false)
  const [mockT, setMockT] = useState(0)
  const [mockDone, setMockDone] = useState(false)
  const [mustInstall, setMustInstall] = useState(false)
  const jobId = useRef("")

  const check = async (): Promise<Report | null> => {
    setBusy("check")
    setErr("")
    try {
      const r: any = await callEntryLoose(props.api, "ms_setup_check", {}, 180000)
      if (!r || !Array.isArray(r.items)) throw Error("没有拿到清单")
      setReport(r)
      setMockMode(false)
      setPath((p: string) => p || String(r.suggested_root || ""))
      return r
    } catch (e: any) {
      if (previewMode()) {
        const m = mockReport("", false)
        setReport(m)
        setMockMode(true)
        setPath((p: string) => p || "D:\\NEKOMusicSynth")
        return m
      }
      setReport(null)
      setErr("检测失败：" + String((e && e.message) || e))
      return null
    } finally {
      setBusy("")
    }
  }
  useEffect(() => { check() }, [])

  // 安装位置剩余空间（按安装根目录所在盘），数据来源与旧向导一致
  useEffect(() => {
    if (!path) return
    const drive = (path.match(/^[A-Za-z]:/) || [""])[0].toUpperCase()
    if (!drive) { setFreeGb(null); return }
    const drives = (report && report.hardware && report.hardware.drives) || []
    const hit = drives.filter((d: any) => String(d.drive || "").toUpperCase() === drive)[0]
    if (hit && hit.free_gib !== undefined) { setFreeGb(Number(hit.free_gib)); return }
    const s: any = (window as any).__msDiskFree
    if (typeof s === "function") {
      Promise.resolve(s(drive)).then((g: any) => setFreeGb(Number(g))).catch(() => setFreeGb(null))
    } else setFreeGb(null)
  }, [path, report])

  // 安装中每 1 秒轮询；全部完成后自动进入第 5 页
  useEffect(() => {
    if (!installing) return
    let stop = false
    let tick = 0
    const loop = async () => {
      while (!stop) {
        await new Promise((r) => setTimeout(r, 1000))
        if (stop) break
        tick += 1
        if (mockMode || !jobId.current) {
          setMockT(tick)
          const p = mockProgress(report || mockReport(path, false), tick)
          setProg(p)
          if (p.job_state === "done") {
            setInstalling(false)
            setMockDone(true)
            setStep(5)
            return
          }
          continue
        }
        try {
          const st: any = await callEntry(props.api, "ms_setup_status", {}, 30000)
          setJob(st.job || {})
          setProg(st.progress || null)
          if (st.setup && st.setup.report) setReport(st.setup.report)
          const s = String((st.job || {}).state || "")
          if (s === "done") {
            setInstalling(false)
            const fresh = await check()
            if (fresh && fresh.ready) setStep(5)
            else setProg(null)
            return
          }
          if (s === "failed") { setInstalling(false); return }
        } catch (e: any) { /* 单次失败不影响，下一秒再试 */ }
      }
    }
    loop()
    return () => { stop = true }
  }, [installing, mockMode])

  const startInstall = async (only?: string) => {
    setBusy(only || "all")
    setErr("")
    setMustInstall(false)
    try {
      const r: any = await callEntry(props.api, "ms_setup_install", only ? { path: path.trim(), items: [only] } : { path: path.trim() }, 60000)
      if (r && r.already_ready) {
        setProg({ job_state: "done", items: {}, detail: "环境已就绪" })
        setMockDone(true)
        setStep(5)
        return
      }
      if (r && r.job_id) {
        jobId.current = String(r.job_id)
        setJob({ state: "running", progress: 0, detail: "正在准备…" })
        setMockT(0)
        setInstalling(true)
      } else throw Error(String((r && r.error) || "没能启动安装"))
    } catch (e: any) {
      if (mockMode) { setMockT(0); setInstalling(true) }
      else setErr(String((e && e.message) || e))
    }
    setBusy("")
  }

  const items = (report && report.items) || []
  const needGb = report ? Number(report.total_missing_gb || 0) : 0
  const lack = freeGb !== null && needGb > 0 && freeGb < Math.max(0.5, needGb * 1.15)
  const allReady = !!report && report.ready === true
  const canNext = allReady || mockDone
  // 只有在「还能在本页装好」的时候才拦着不让跳过；装不了的项目拦着也没用
  const knownMissing = !!report && !allReady && !mockDone && items.some((i: Item) => i.required && !i.ok && i.installable)

  const requestSkip = () => {
    // 检测还没回来就先别放行：去第 4 页等等看，免得环境缺东西却已经进了其它页面
    if (!report && !err) {
      setMustInstall(true)
      setStep(4)
      return
    }
    if (knownMissing) {
      setMustInstall(true)
      setStep(4)
      return
    }
    if (props.onSkip) props.onSkip()
    else props.onDone()
  }

  /* --------------------------------- 各页 --------------------------------- */

  const page1 = (
    <div style={{ display: "flex", flexDirection: "column", gap: 20, alignItems: "center", textAlign: "center", padding: "8px 0 4px" }}>
      <div>
        <div style={{ fontSize: 21, fontWeight: 800, color: INK, letterSpacing: 0.2 }}>先听听我们做出来的效果</div>
        <div style={{ fontSize: 13, color: INK_2, marginTop: 8, lineHeight: 1.8 }}>
          这首歌就是用插件自带的声音「沫沫」唱出来的。<br />现在就可以听，不用先装任何东西。
        </div>
      </div>

      <div style={{ width: "100%", maxWidth: 520, padding: "22px 24px 20px", borderRadius: 24, backgroundImage: "linear-gradient(150deg,#ffffff 0%,#fff8fb 58%,#ffeef6 100%)", border: "1px solid rgba(255,255,255,0.96)", boxShadow: "0 16px 40px rgba(224,45,118,0.10), 0 2px 8px rgba(142,159,187,0.10)" }}>
        <Row style={{ gap: 14, justifyContent: "flex-start", textAlign: "left" }}>
          <span style={{ width: 52, height: 52, borderRadius: 999, display: "grid", placeItems: "center", background: "linear-gradient(150deg,#ffe5ef,#f6cfe4)", color: BRAND, fontSize: 23, boxShadow: "0 6px 16px rgba(224,45,118,0.15)" }}>♪</span>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 16, fontWeight: 700, color: INK }}>沫沫</div>
            <div style={{ fontSize: 12, color: INK_2, marginTop: 2 }}>内置 · 体验音色</div>
          </div>
          <Pill tone="neutral">试听样曲</Pill>
        </Row>

        <div style={{ display: "flex", alignItems: "center", gap: 18, marginTop: 20 }}>
          <button type="button" aria-label={playing ? "暂停" : "播放"} onClick={toggle} disabled={loading || !src}
                  style={{ width: 74, height: 74, flex: "0 0 auto", borderRadius: 999, border: 0, cursor: loading || !src ? "default" : "pointer",
                           background: loading || !src ? "rgba(224,45,118,0.18)" : "linear-gradient(150deg,#ff7fb0,#e02d76)",
                           color: "#fff", fontSize: 24, display: "grid", placeItems: "center",
                           boxShadow: "0 10px 24px rgba(224,45,118,0.26)", transition: "transform .18s ease" }}>
            {loading ? "…" : !src ? "!" : playing ? "❚❚" : "▶"}
          </button>

          <div style={{ flex: 1, minWidth: 0 }}>
            <div onClick={seek} style={{ height: 10, borderRadius: 999, background: "rgba(224,45,118,0.12)", cursor: src ? "pointer" : "default", position: "relative", overflow: "hidden" }}>
              <i style={{ display: "block", height: "100%", width: (dur ? Math.min(100, pos / dur * 100) : 0) + "%", background: "linear-gradient(90deg,#ff7fb0,#e02d76)", borderRadius: 999 }} />
            </div>
            <Row style={{ justifyContent: "space-between", marginTop: 8 }}>
              <Muted style={{ fontSize: 11.5 }}>{mmss(pos)}</Muted>
              <div style={{ display: "flex", gap: 3, alignItems: "flex-end", height: 18 }} aria-hidden>
                {[0.62, 0.94, 0.45, 0.8, 0.55].map((h: number, i: number) => (
                  <i key={i} style={{ width: 3, height: 18 * h, borderRadius: 3, background: BRAND, opacity: playing ? 0.85 : 0.28,
                                       animation: "ms-ob-wave 1.05s ease-in-out infinite", animationDelay: (i * 0.12) + "s",
                                       animationPlayState: playing ? "running" : "paused", transformOrigin: "bottom" }} />
                ))}
              </div>
              <Muted style={{ fontSize: 11.5 }}>{dur ? mmss(dur) : "—:—"}</Muted>
            </Row>
          </div>
        </div>

        {src ? (
          <audio ref={audioRef} src={src} preload="metadata" style={{ display: "none" }}
                 onLoadedMetadata={() => { try { setDur(Number(audioRef.current.duration) || 0) } catch (e) {} }}
                 onTimeUpdate={() => { try { setPos(Number(audioRef.current.currentTime) || 0) } catch (e) {} }}
                 onPlay={() => setPlaying(true)}
                 onPause={() => setPlaying(false)}
                 onEnded={() => { setPlaying(false); setPos(0) }} />
        ) : null}

        {audioErr ? (
          <div style={{ marginTop: 14 }}>
            <Alert tone="warning">{audioErr}</Alert>
            <Row style={{ marginTop: 10, justifyContent: "flex-end" }}>
              <Btn style={{ ...ghostBtn, padding: "7px 14px", fontSize: 12 }} onClick={loadDemo}>重新加载试听</Btn>
            </Row>
          </div>
        ) : null}
        {loading ? <Muted style={{ display: "block", marginTop: 12, fontSize: 11.5 }}>正在准备试听…</Muted> : null}
      </div>

      <Muted style={{ fontSize: 11.5 }}>所有处理都在你自己的电脑上完成，不会把音频上传到网上。</Muted>

      <Row style={{ justifyContent: "center", marginTop: 2 }}>
        <Btn style={primaryBtn} onClick={() => setStep(2)}>下一步：看看她能做什么 →</Btn>
      </Row>
    </div>
  )

  const page2 = (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div style={{ textAlign: "center" }}>
        <div style={{ fontSize: 19, fontWeight: 800, color: INK }}>她能做什么</div>
        <div style={{ fontSize: 12.5, color: INK_2, marginTop: 6 }}>左右翻一翻，一共 {FEATURES.length} 项能力</div>
      </div>

      <div style={{ position: "relative", borderRadius: 22, overflow: "hidden", border: "1px solid rgba(224,45,118,0.10)", background: "rgba(255,255,255,0.72)" }}>
        <div style={{ display: "flex", transition: "transform .32s cubic-bezier(.22,.61,.36,1)", transform: "translateX(" + (-feat * 100) + "%)" }}>
          {FEATURES.map((f: any, i: number) => (
            <div key={f.key} style={{ flex: "0 0 100%", minWidth: 0, padding: "26px 28px", display: "flex", flexDirection: "column", gap: 12, minHeight: 236 }}>
              <Row style={{ justifyContent: "space-between" }}>
                <span style={{ width: 58, height: 58, borderRadius: 20, display: "grid", placeItems: "center", background: "linear-gradient(150deg,#ffeef6,#ffe0ee)", color: BRAND, boxShadow: "0 6px 16px rgba(224,45,118,0.10)" }}>
                  <FeatureIcon name={f.key} />
                </span>
                <span style={{ fontSize: 12, color: INK_3, fontWeight: 700 }}>{String(i + 1).padStart(2, "0")} / {String(FEATURES.length).padStart(2, "0")}</span>
              </Row>
              <div style={{ fontSize: 20, fontWeight: 800, color: INK }}>{f.title}</div>
              <div style={{ fontSize: 13.5, color: INK_2, lineHeight: 1.85 }}>{f.text}</div>
              <div style={{ marginTop: "auto" }}><Pill tone="neutral">{f.tag}</Pill></div>
            </div>
          ))}
        </div>
      </div>

      <Row style={{ justifyContent: "center", alignItems: "center", gap: 14 }}>
        <button type="button" aria-label="上一项" onClick={featPrev} disabled={feat === 0}
                style={{ width: 32, height: 32, borderRadius: 999, border: "1px solid " + (feat === 0 ? "rgba(40,37,50,0.06)" : "rgba(224,45,118,0.16)"),
                         background: feat === 0 ? "rgba(40,37,50,0.03)" : "#fff", color: feat === 0 ? "#c9c4cf" : BRAND_DEEP,
                         cursor: feat === 0 ? "default" : "pointer", fontSize: 15, lineHeight: 1 }}>‹</button>
        <Row style={{ gap: 7 }}>
          {FEATURES.map((f: any, i: number) => (
            <button key={f.key} type="button" aria-label={f.title} onClick={() => setFeat(i)}
                    style={{ width: i === feat ? 22 : 7, height: 7, borderRadius: 999, border: 0, padding: 0, cursor: "pointer",
                             background: i === feat ? BRAND : "rgba(224,45,118,0.20)", transition: "width .2s ease, background-color .2s ease" }} />
          ))}
        </Row>
        <button type="button" aria-label="下一项" onClick={featNext} disabled={feat === FEATURES.length - 1}
                style={{ width: 32, height: 32, borderRadius: 999, border: "1px solid " + (feat === FEATURES.length - 1 ? "rgba(40,37,50,0.06)" : "rgba(224,45,118,0.16)"),
                         background: feat === FEATURES.length - 1 ? "rgba(40,37,50,0.03)" : "#fff", color: feat === FEATURES.length - 1 ? "#c9c4cf" : BRAND_DEEP,
                         cursor: feat === FEATURES.length - 1 ? "default" : "pointer", fontSize: 15, lineHeight: 1 }}>›</button>
      </Row>

      <Row style={{ justifyContent: "space-between", marginTop: 2 }}>
        <Btn style={ghostBtn} onClick={() => setStep(1)}>上一步</Btn>
        <Btn style={primaryBtn} onClick={() => setStep(3)}>下一步：怎么用 →</Btn>
      </Row>
    </div>
  )

  const USAGE = [
    ["导入歌曲", "导入一个音频文件，或者直接录下电脑上正在播放的歌。"],
    ["选择音色", "挑内置的「沫沫」，或者用你训练好的声音。"],
    ["生成", "一键分离、变声、混音，全部在本机完成。"],
    ["试听导出", "满意就导出成品；不满意可以再唱一遍。"],
  ]
  const page3 = (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div style={{ textAlign: "center" }}>
        <div style={{ fontSize: 19, fontWeight: 800, color: INK }}>四步就能听到她用另一种声音唱歌</div>
        <div style={{ fontSize: 12.5, color: INK_2, marginTop: 6 }}>先熟悉流程，装好环境后照着做就行</div>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {USAGE.map((u: string[], i: number) => (
          <div key={i} style={{ display: "grid", gridTemplateColumns: "40px minmax(0,1fr)", gap: 14, alignItems: "center", padding: "15px 18px", borderRadius: 18, background: "rgba(255,255,255,0.82)", border: "1px solid rgba(255,255,255,0.96)", boxShadow: "0 4px 16px rgba(142,159,187,0.06)" }}>
            <span style={{ width: 40, height: 40, borderRadius: 14, display: "grid", placeItems: "center", background: i === 0 ? BRAND : "rgba(255,229,239,0.85)", color: i === 0 ? "#fff" : BRAND_DEEP, fontWeight: 800, fontSize: 14 }}>{i + 1}</span>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 14.5, fontWeight: 700, color: INK }}>{u[0]}</div>
              <div style={{ fontSize: 12.5, color: INK_2, marginTop: 3, lineHeight: 1.7 }}>{u[1]}</div>
            </div>
          </div>
        ))}
      </div>
      <Muted style={{ textAlign: "center", fontSize: 11.5 }}>想换回原来的声音？随时在「我的声音」里切换。</Muted>
      <Row style={{ justifyContent: "space-between" }}>
        <Btn style={ghostBtn} onClick={() => setStep(2)}>上一步</Btn>
        <Btn style={primaryBtn} onClick={() => setStep(4)}>下一步：准备运行环境 →</Btn>
      </Row>
    </div>
  )

  // 第 4 页：依赖与环境（旧向导第 1～3 步内容全部在这里）
  // 未就绪的排在前面，已就绪的跟在后面；每一项都露出来，状态一眼能扫完。
  const showing = [...items].sort((a: Item, b: Item) => Number(a.status === "ready") - Number(b.status === "ready"))
  const overall = (() => {
    if (!prog) return 0
    const list = items.filter((i) => i.status !== "ready")
    if (!list.length) return 100
    let sum = 0
    for (const i of list) {
      const st = (prog.items || {})[i.key] || {}
      sum += st.state === "done" || st.state === "ready" ? 100 : Number(st.pct || 0)
    }
    return Math.round(sum / list.length)
  })()
  const failed = !!(prog && prog.job_state === "failed")
  const failLabels = items.filter((i) => {
    const st = prog && prog.items && prog.items[i.key]
    return st && st.state === "failed"
  })

  const page4 = (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ textAlign: "center" }}>
        <div style={{ fontSize: 19, fontWeight: 800, color: INK }}>准备运行环境</div>
        <div style={{ fontSize: 12.5, color: INK_2, marginTop: 6, lineHeight: 1.8 }}>
          欢迎使用「为她而唱」。第一次打开需要先准备好她会用到的程序、模型和音频处理工具，之后就不用再装了。
        </div>
      </div>

      {mustInstall ? <Alert tone="warning">必需的程序还没装好，先点「一键安装全部」把它装好才能开始使用。</Alert> : null}
      {mockMode ? <Alert tone="info">当前是模拟演示：没有连上安装接口，进度为示例数据，只用于查看界面流程。</Alert> : null}
      {err ? <Alert tone="warning">{err}</Alert> : null}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", gap: 12 }}>
        {[
          ["要下载什么", "运行程序 + 声音模型 + 去伴奏模型"],
          ["大约多大", report && needGb ? needGb + " GB" : "约 2 GB（以检测结果为准）"],
          ["大概多久", report && needGb ? fmtEta(needGb * 900) : "看网速，一般 10～40 分钟"],
        ].map((row: string[], i: number) => (
          <div key={i} style={{ padding: 14, borderRadius: 16, background: "rgba(255,229,239,0.5)", border: "1px solid rgba(224,45,118,0.10)" }}>
            <div style={{ fontSize: 11.5, color: INK_2 }}>{row[0]}</div>
            <div style={{ fontSize: 14, fontWeight: 700, color: "#9c204e", marginTop: 4 }}>{row[1]}</div>
          </div>
        ))}
      </div>

      {/* 安装位置（旧向导第 2 步） */}
      <div style={{ padding: 16, borderRadius: 18, background: "rgba(255,255,255,0.72)", border: "1px solid " + LINE, display: "flex", flexDirection: "column", gap: 10 }}>
        <div style={{ fontSize: 12.5, color: INK, lineHeight: 1.8 }}>
          程序、模型和临时文件都会放在这里。选一个空间充足的盘就行，装在 D 盘、E 盘都可以。
        </div>
        <label style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 12, color: INK_2 }}>
          <span>安装到哪里</span>
          <input value={path} placeholder="例如 D:\\NEKOMusicSynth" disabled={installing}
                 onChange={(e: any) => setPath(e.target.value)} style={{
                   border: "1px solid " + (lack ? "#e8a3a3" : "transparent"), borderRadius: 12, padding: "10px 14px", fontSize: 13,
                   background: lack ? "#fff6f6" : "rgba(248,250,252,0.95)", color: INK, outline: "none",
                 }} />
        </label>
        <Muted>必须自己填一个绝对路径（例如 D:\NEKOMusicSynth）。插件不会自动挑盘，也不会把模型下到别的盘。</Muted>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
          {!path.trim() ? (
            <StatusBadge tone="warning">请先填写安装目录，否则不会开始下载</StatusBadge>
          ) : freeGb !== null ? (
            lack
              ? <StatusBadge tone="error">这个盘只剩 {freeGb.toFixed(1)} GB，还要约 {needGb} GB，换一个盘吧</StatusBadge>
              : <StatusBadge tone="success">这个盘还剩 {freeGb.toFixed(1)} GB · 需要约 {needGb} GB</StatusBadge>
          ) : <Muted>（无法读取剩余空间，也可以继续）</Muted>}
        </div>
        {mockMode ? <Muted>安装向导暂时读不到检测结果。</Muted> : null}
      </div>

      {/* 总进度（安装中 / 有进度时） */}
      {installing || (prog && prog.items) ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <Row style={{ justifyContent: "space-between" }}>
            <div style={{ fontSize: 13.5, fontWeight: 700, color: INK }}>{prog && prog.job_state === "done" ? "快好了…" : "正在下载并安装"}</div>
            <Muted>{overall}%</Muted>
          </Row>
          <Bar pct={overall} />
          <div style={{ fontSize: 12, color: INK_2, minHeight: 18 }}>{(prog && prog.detail) || (job && job.detail) || "正在准备…"}</div>
        </div>
      ) : null}

      {failed ? (
        <Alert tone="warning">{(prog && prog.detail) || "安装没能全部完成，看看下面哪一项出了问题。"}</Alert>
      ) : null}

      {/* 依赖项列表 */}
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {showing.map((i) => {
          const st = (prog && prog.items && prog.items[i.key]) || { state: i.ok ? "ready" : i.installable ? "missing" : "missing_required", pct: null }
          const tx = STATE_TEXT[st.state] || STATE_TEXT.wait
          const isActive = st.state === "active"
          const isDownload = st.total_mb && st.done_mb !== undefined && st.done_mb !== null
          const canInstall = i.installable && i.status !== "ready" && !installing
          return (
            <div key={i.key} style={{ display: "grid", gridTemplateColumns: "24px minmax(0,1fr) auto", gap: "4px 10px", alignItems: "center", padding: "10px 13px", borderRadius: 15, background: isActive ? "rgba(255,229,239,0.55)" : "rgba(248,250,252,0.9)", border: "1px solid " + (isActive ? "rgba(224,45,118,0.18)" : LINE) }}>
              <StateIcon state={st.state} />
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 12.5, color: INK, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{i.label}</div>
                <div style={{ fontSize: 11.5, color: INK_3, lineHeight: 1.5, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {isDownload ? st.done_mb + " / " + st.total_mb + " MB"
                    : (i.ok ? (i.detail || "已就绪") : (i.fix || i.detail || (i.size_gb ? "约 " + i.size_gb + " GB" : "")))}
                </div>
              </div>
              <Row style={{ gap: 8 }}>
                <Pill tone={tx.tone}>{tx.text}</Pill>
                {canInstall ? (
                  <Btn style={{ ...ghostBtn, padding: "5px 11px", fontSize: 11.5, borderRadius: 9 }} disabled={!!busy}
                       onClick={() => startInstall(i.key)}>安装</Btn>
                ) : null}
                {failed && st.state === "failed" ? (
                  <Btn style={{ ...ghostBtn, padding: "5px 11px", fontSize: 11.5, borderRadius: 9 }} disabled={!!busy}
                       onClick={() => startInstall(i.key)}>重试这一项</Btn>
                ) : null}
              </Row>
              <div style={{ gridColumn: "2 / 4" }}>
                <Bar pct={st.state === "done" || st.state === "ready" ? 100 : Number(st.pct || 0)} tone={st.state === "failed" ? "#e06666" : st.state === "active" ? undefined : "rgba(224,45,118,0.35)"} />
              </div>
            </div>
          )
        })}
      </div>

      {failed && !failLabels.length ? (
        <Row style={{ justifyContent: "flex-end" }}>
          <Btn style={ghostBtn} disabled={!!busy} onClick={() => startInstall()}>重新安装缺失项</Btn>
        </Row>
      ) : null}

      <Muted style={{ fontSize: 11.5, lineHeight: 1.7 }}>
        安装过程中可以继续用电脑；关掉这个窗口不会中断下载，下次打开会接着装。<br />
        可以先去忙别的：关掉窗口不会中断，回来还能接着看。
      </Muted>
    </div>
  )

  // 第 5 页：完成
  const page5 = (
    <div style={{ display: "flex", flexDirection: "column", gap: 18, alignItems: "center", textAlign: "center", padding: "14px 0 6px" }}>
      <span style={{ width: 58, height: 58, borderRadius: 999, background: "#e8f7ef", color: "#1c8a58", display: "grid", placeItems: "center", boxShadow: "0 8px 20px rgba(28,138,88,0.12)" }}>
        <svg width="30" height="30" viewBox="0 0 24 24"><path d="M5 12.5 9.6 17 19 7.2" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" /></svg>
      </span>
      <div style={{ fontSize: 22, fontWeight: 800, color: INK }}>都准备好了</div>
      <div style={{ fontSize: 13.5, color: INK_2, lineHeight: 1.9, maxWidth: 480 }}>
        全部就绪。以后打开插件不用再装东西了。先把几首歌放进「准备素材」，就能开始训练她的声音。
      </div>
      <div style={{ padding: "12px 18px", borderRadius: 16, background: "rgba(255,229,239,0.5)", border: "1px solid rgba(224,45,118,0.10)", fontSize: 12.5, color: "#9c204e", lineHeight: 1.7 }}>
        小提示：第一次可以先在「让她唱歌」里挑一首歌，用内置的「沫沫」试一遍。
      </div>
      <Row style={{ justifyContent: "center", marginTop: 4 }}>
        <Btn style={primaryBtn} onClick={props.onDone}>开始使用</Btn>
      </Row>
      <Muted style={{ fontSize: 11 }}>安装文件只来自官方发布地址，放在你选定的目录里；不会改动系统设置。</Muted>
    </div>
  )

  const body = step === 1 ? page1 : step === 2 ? page2 : step === 3 ? page3 : step === 4 ? page4 : page5

  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 1100, display: "flex", alignItems: "center", justifyContent: "center", padding: 20, background: "rgba(248,250,252,0.94)", backdropFilter: "blur(12px)", WebkitBackdropFilter: "blur(12px)", overflow: "auto" }}>
      <style>{`
        @keyframes ms-ob-wave { 0%,100% { transform: scaleY(.35); } 50% { transform: scaleY(1); } }
        .ms-ob-scroll::-webkit-scrollbar { width: 6px; }
        .ms-ob-scroll::-webkit-scrollbar-thumb { background: rgba(224,45,118,0.18); border-radius: 999px; }
      `}</style>
      <div style={{ width: "100%", maxWidth: 720, margin: "auto" }}>
        <Panel style={{ display: "flex", flexDirection: "column", gap: 16, padding: "22px 26px 24px", borderRadius: 28 }}>
          {/* 顶栏：品牌 + 步骤 + 跳过 */}
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <span style={{ width: 34, height: 34, borderRadius: 999, display: "grid", placeItems: "center", background: "linear-gradient(150deg,#ffe5ef,#f6cfe4)", color: BRAND, fontSize: 17, boxShadow: "0 4px 12px rgba(224,45,118,0.14)" }}>♪</span>
              <div>
                <div style={{ fontSize: 14.5, fontWeight: 700, color: INK }}>为她而唱 · 新手引导</div>
                <div style={{ fontSize: 11.5, color: INK_2, marginTop: 1 }}>第 {step} 步 / 共 5 步 · {STEP_TITLES[step - 1]}</div>
              </div>
            </div>
            {step === 5 ? null : (
              <button type="button" onClick={requestSkip}
                      style={{ border: 0, background: "transparent", color: INK_3, fontSize: 12, cursor: "pointer", padding: "6px 4px", fontFamily: "inherit" }}>
                跳过引导
              </button>
            )}
          </div>

          {/* 顶部进度条 */}
          <div style={{ display: "flex", gap: 6 }}>
            {STEPS.map((t: string, i: number) => {
              const no = i + 1
              const done = step > no
              const on = step === no
              return (
                <div key={t} title={t} style={{ flex: 1, height: 4, borderRadius: 999, background: on ? BRAND : done ? "rgba(224,45,118,0.40)" : "rgba(40,37,50,0.08)", transition: "background-color .2s ease" }} />
              )
            })}
          </div>

          <div className="ms-ob-scroll" style={{ maxHeight: step === 4 ? "calc(100vh - 300px)" : "calc(100vh - 240px)", overflowY: "auto", overflowX: "hidden", paddingRight: 2 }}>
            {body}
          </div>

          {/* 第 4 页的操作栏固定在卡片底部，不用滚到最后才能装 */}
          {step === 4 ? (
            <div style={{ borderTop: "1px solid " + LINE, paddingTop: 12, display: "flex", flexDirection: "column", gap: 6 }}>
              <Row style={{ justifyContent: "space-between", alignItems: "center" }}>
                <Btn style={ghostBtn} onClick={() => setStep(3)} disabled={installing}>上一步</Btn>
                <Row style={{ gap: 10 }}>
                  {allReady || mockDone ? null : (
                    <Btn style={primaryBtn} disabled={!!busy || installing || !path.trim() || lack || !items.some((i: Item) => i.installable && i.status !== "ready")}
                         onClick={() => startInstall()}>
                      {installing ? "正在安装…" : busy === "all" ? "正在启动…" : "一键安装全部"}
                    </Btn>
                  )}
                  <Btn style={canNext ? primaryBtn : ghostBtn} disabled={!canNext || installing} onClick={() => setStep(5)}>
                    {canNext ? "下一步：开始使用 →" : "装好后继续"}
                  </Btn>
                </Row>
              </Row>
              {!canNext && !installing ? <Muted style={{ fontSize: 11.5, textAlign: "right" }}>必需项装好之前，其它页面先不能使用。</Muted> : null}
            </div>
          ) : null}
        </Panel>
      </div>
    </div>
  )
}
