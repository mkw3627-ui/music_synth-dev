// 为她而唱 · 主页（2026-11-03 TSX 重写）
//
// 覆盖旧 static/index.html 的全部功能：档位/硬件/本次运行参数、一键全自动、导入、
// 变声模型与设置、曲目表（处理/识别/面板内试听/删除）、任务列表、环境体检、首次向导。
// 试听一律用面板里的 <audio>（ms_track_audio），不再调用 ms_play 拉起系统播放器。
import { Alert, Button, Divider, Modal, StatusBadge, useEffect, useRef, useState } from "@neko/plugin-ui"
import { callEntry, dataUriOf, dur, short } from "./api"
import { BRAND, BRAND_DEEP, BRAND_SOFT, INK, INK_2, LINE, Soft, Bar, Btn, Col, Confirm, Empty, Err, Field, FoldCard, Input, KV, Muted, Panel, Pill, Row, Section, Select, fmtTime } from "./ui"

const PHASE = {
  pending: ["待处理", "neutral"], waiting: ["待处理", "neutral"],
  running: ["处理中", "warning"], done: ["已完成", "success"], failed: ["失败", "error"],
}
const JOB_STATE = {
  queued: ["排队", "neutral"], running: ["进行中", "warning"], done: ["完成", "success"],
  failed: ["失败", "error"], canceled: ["已取消", "neutral"],
}

function tagOf(map: any, key: any): any {
  const hit = map[String(key || "")]
  return hit ? { label: hit[0], tone: hit[1] } : { label: String(key || "未知"), tone: "neutral" }
}

export function Home(props: { api: any; snap: any; error: string; onRefresh: () => void; onGoTrain: () => void }) {
  const { api, snap, error, onRefresh } = props
  const s = snap || {}
  const [busy, setBusy] = useState("")
  const [notice, setNotice] = useState(null)
  const [env, setEnv] = useState(null)
  const [envBusy, setEnvBusy] = useState(false)
  const [wizard, setWizard] = useState(false)
  const [setup, setSetup] = useState(null)
  const [setupPath, setSetupPath] = useState("")
  const [setupJob, setSetupJob] = useState(null)
  const [setupBusy, setSetupBusy] = useState(false)
  const [recordSec, setRecordSec] = useState(90)
  const [importPath, setImportPath] = useState("")
  const [keyShift, setKeyShift] = useState("0")
  const [inferStep, setInferStep] = useState("50")
  const [ckpt, setCkpt] = useState("")
  const [ckptPath, setCkptPath] = useState("")
  // 曲目试听：面板内 <audio>
  const [audio, setAudio] = useState(null)
  const [audioBusy, setAudioBusy] = useState("")
  const [confirm, setConfirm] = useState(null)
  // 折叠加卡片默认收起：本机状态、本次运行参数（k0/v0 那一堆）
  const [openInfo, setOpenInfo] = useState(false)
  const [openRun, setOpenRun] = useState(false)
  const engineInit = useRef(false)

  const eng = s.engine || {}
  useEffect(() => {
    if (engineInit.current || !snap) return
    engineInit.current = true
    setKeyShift(String(eng.key_shift ?? 0))
    setInferStep(String(eng.infer_step ?? 50))
    const cur = (eng.models || []).find((m: any) => m.current)
    setCkpt(cur ? cur.path : "")
  }, [snap])

  async function run(key, fn) {
    if (busy) return null
    setBusy(key)
    setNotice(null)
    try {
      return await fn()
    } catch (e: any) {
      setNotice({ tone: "error", text: String((e && e.message) || e) })
      return null
    } finally {
      setBusy("")
    }
  }

  // ---- 一键全自动 / 导入 -------------------------------------------------
  const doAuto = () => run("auto", async () => {
    const sec = Math.max(10, Math.min(600, Number(recordSec) || 90))
    setNotice({ tone: "info", text: "正在录 " + sec + " 秒并切歌…（录完后自动逐首出曲）" })
    const r = await callEntry(api, "ms_run_auto", { duration: sec }, sec * 1000 + 180000)
    setNotice({ tone: r.ok ? "info" : "error", text: r.ok ? "切出 " + (r.tracks || []).length + " 首，已开始逐首处理" : "失败：" + (r.error || "") })
    onRefresh()
  })

  const doImport = () => run("import", async () => {
    const p = importPath.trim()
    if (!p) { setNotice({ tone: "error", text: "先填一个本地音频路径" }); return }
    const r = await callEntry(api, "ms_import", { path: p, split: true }, 300000)
    setNotice({ tone: r.ok ? "info" : "error", text: r.ok ? "已切出 " + (r.tracks || []).length + " 首" : "失败：" + (r.error || "") })
    onRefresh()
  })

  // ---- 曲目 -------------------------------------------------------------
  const preview = (t: any) => run("audio:" + t.id, async () => {
    setAudioBusy("正在读取成品…")
    const r = await callEntry(api, "ms_track_audio", { track_id: t.id }, 120000)
    setAudioBusy("")
    if (!r.ok) { setNotice({ tone: "error", text: String(r.error || "读取成品失败") }); return }
    setAudio({ id: t.id, title: String(r.title || t.title || ""), src: dataUriOf(r.audio_base64, r.mime) })
  })

  const processTrack = (t: any) => run("proc:" + t.id, async () => {
    const r = await callEntry(api, "ms_process_track", { track_id: t.id }, 60000)
    setNotice({ tone: r.ok ? "info" : "error", text: r.ok ? "已开始处理：" + (t.title || t.id) : "失败：" + (r.error || "") })
    onRefresh()
  })

  const identify = (t: any) => run("id:" + t.id, async () => {
    const r = await callEntry(api, "ms_identify", { track_id: t.id }, 60000)
    setNotice({ tone: r.ok ? "info" : "error", text: r.ok ? "识歌结果：" + (r.title_kept ? "已校准为 " + r.title_kept : "未发现更可靠的歌名") : "失败：" + (r.error || "") })
    onRefresh()
  })

  const remove = (t: any) => setConfirm({
    title: "从列表移除这首？", ok: "移除",
    text: "只从曲目列表移除记录，磁盘上的音频不会删除：" + (t.title || t.id),
    run: () => { callEntry(api, "ms_delete_track", { track_id: t.id }, 30000).then(onRefresh).catch((e) => setNotice({ tone: "error", text: String(e.message || e) })) },
  })

  // ---- 探测 / 环境 / 设置 -------------------------------------------------
  const probe = () => run("probe", async () => {
    const r = await callEntry(api, "ms_probe", {}, 60000)
    setNotice({ tone: "info", text: "已重新探测：" + short(r.brief, "完成", 200) })
    onRefresh()
  })

  const checkEnv = async () => {
    setEnvBusy(true)
    setEnv(null)
    try {
      const r = await callEntry(api, "ms_env_check", {}, 120000)
      setEnv(r.report || {})
    } catch (e: any) {
      setNotice({ tone: "error", text: "环境体检失败：" + String((e && e.message) || e) })
    }
    setEnvBusy(false)
  }

  const saveEngine = () => run("engine", async () => {
    const key = Number(keyShift), step = Number(inferStep)
    if (!Number.isInteger(key) || key < -24 || key > 24) { setNotice({ tone: "error", text: "变调须为 -24～24 的整数" }); return }
    if (!Number.isInteger(step) || step < 5 || step > 200) { setNotice({ tone: "error", text: "推理步数须为 5～200 的整数" }); return }
    const args: any = { key_shift: key, infer_step: step }
    if (ckptPath.trim()) args.ddsp_ckpt = ckptPath.trim()
    else if (ckpt) args.ddsp_ckpt = ckpt
    const r = await callEntry(api, "ms_set_config", args, 30000)
    setNotice({ tone: r.ok ? "info" : "error", text: r.ok ? "已保存变声设置" : "保存失败：" + (r.error || "未知错误") })
    onRefresh()
  })

  // ---- 首次向导 ----------------------------------------------------------
  const setupCheck = async () => {
    setSetupBusy(true)
    try {
      const r = await callEntry(api, "ms_setup_check", {}, 120000)
      setSetup(r)
      if (!setupPath) setSetupPath(r.suggested_root || (s.paths && s.paths.compute_root) || "")
    } catch (e: any) {
      setNotice({ tone: "error", text: "检测失败：" + String((e && e.message) || e) })
    }
    setSetupBusy(false)
  }

  const setupInstall = async () => {
    setSetupBusy(true)
    setSetupJob({ state: "starting", pct: 0, detail: "正在启动安装…" })
    try {
      const r: any = await callEntry(api, "ms_setup_install", { path: setupPath.trim() }, 60000)
      if (r && r.already_ready) { setSetupJob({ state: "done", pct: 100, detail: "环境已就绪，无需安装。" }); setSetupBusy(false); return }
      if (!r || !r.job_id) {
        throw Error(String((r && r.error) || "安装任务没有启动"))
      }
      const deadline = Date.now() + 6 * 3600 * 1000
      let idle = 0
      while (Date.now() < deadline) {
        await new Promise((res) => setTimeout(res, 2500))
        let st: any = {}
        try { st = await callEntry(api, "ms_setup_status", {}, 30000) } catch (e) { st = {} }
        const job = (st && st.job) || {}
        const state = String(job.state || "")
        if (!state) {
          // 任务被重载/线程死掉后 job 会变空；别演 6 小时的「安装中」。
          idle += 1
          if (idle >= 24) {
            setSetupJob({ job_id: r.job_id, state: "failed", pct: 0, detail: "安装任务已丢失（插件可能重载过），请重试。" })
            break
          }
          setSetupJob({ job_id: r.job_id, state: "running", pct: Number(job.progress || 0), detail: job.detail || "等待安装线程…" })
          continue
        }
        idle = 0
        setSetupJob({ job_id: r.job_id, state: state, pct: Number(job.progress || 0), detail: job.detail || "" })
        if (state === "done" || state === "failed") {
          const report = (st.setup && st.setup.report) || null
          if (report) setSetup(report)
          break
        }
      }
    } catch (e: any) {
      setSetupJob({ state: "failed", pct: 0, detail: String((e && e.message) || e) })
    }
    setSetupBusy(false)
    onRefresh()
  }

  // ---- 渲染 --------------------------------------------------------------
  const plan = s.plan || {}
  const hw = s.hardware || {}
  const gpu = hw.gpu
  const tracks = (s.tracks || []).slice().reverse()
  const jobs = Object.entries(s.jobs || {})
  const models = Array.isArray(eng.models) ? eng.models : []
  const usable = models.filter((m: any) => !m.training)
  const training = models.filter((m: any) => m.training)
  const setupReport = setup || (s.setup && s.setup.report) || null

  return (
    <Col style={{ gap: 24 }}>
      {error ? <Alert tone="warning">读取状态失败：{error}</Alert> : null}
      {notice ? <Alert tone={notice.tone === "error" ? "warning" : "info"}>{notice.text}</Alert> : null}

      <FoldCard title="电脑状态" open={openInfo} onToggle={() => setOpenInfo(!openInfo)}
                sub={<Pill tone={plan.tier === "high" ? "success" : plan.tier === "low" ? "warning" : "neutral"}>{plan.label || plan.tier || "未知档位"}</Pill>}>
        <Row style={{ marginBottom: 14 }}>
          <Btn disabled={!!busy} onClick={() => onRefresh()}>刷新状态</Btn>
          <Btn disabled={envBusy} onClick={checkEnv}>{envBusy ? "检查中…" : "环境体检"}</Btn>
          <Btn onClick={() => setWizard(true)}>首次使用向导</Btn>
          <Btn disabled={!!busy} onClick={probe}>重新探测</Btn>
        </Row>
        {snap ? (
          <Col style={{ gap: 8 }}>
            <Row>
              <Pill tone={plan.tier === "high" ? "success" : plan.tier === "low" ? "warning" : "neutral"}>
                {plan.label || plan.tier || "未知档位"}
              </Pill>
              <Muted>{plan.basis || ""}</Muted>
            </Row>
            <KV items={[
              ["显卡", gpu ? gpu.name + " · sm_" + (gpu.compute_cap || "?") + " · 驱动 " + (gpu.driver || "?") : "未检测到 NVIDIA 显卡（CPU 模式）"],
              ["显存", gpu ? "总 " + gpu.total_gib + "GB · 已用 " + gpu.used_gib + "GB · 空闲 " + gpu.free_gib + "GB" : "—"],
              ["内存", "总 " + (hw.ram_total_gib ?? "?") + "GB · 空闲 " + (hw.ram_available_gib ?? "?") + "GB"],
              ["执行设备", (plan.device || "?") + " · 并发 " + (plan.parallel || 1)],
              ["分离", (plan.sep_device || "?") + " · 步长 " + (plan.sep_step || "?") + " · 分段≤" + (plan.max_segment_s || "?") + "s"],
            ]} />
            {(plan.warnings || []).map((w: string, i: number) => (
              <div key={i} style={{ fontSize: 12, color: "#c8861a" }}>⚠ {w}</div>
            ))}
            <Muted>{(s.last_run && s.last_run.at) ? "上次运行探测：" + s.last_run.at : "本次会话尚未探测；每次出曲前会自动探测。"}</Muted>
          </Col>
        ) : <Empty>正在读取硬件与路径…</Empty>}

        {env ? (
          <Col>
            <Divider />
            <div style={{ fontSize: 13, fontWeight: 600 }}>环境体检</div>
            {(env.items || []).map((it: any, i: number) => (
              <Col key={i} style={{ gap: 0 }}>
                <Row style={{ gap: 6 }}>
                  <StatusBadge tone={it.ok ? "success" : "error"}>{it.ok ? "✓ 就绪" : "✗ 缺失"}</StatusBadge>
                  <span style={{ fontSize: 12.5 }}>{it.label}</span>
                  {it.detail ? <Muted>（{it.detail}）</Muted> : null}
                </Row>
                {!it.ok && it.fix ? <Muted style={{ paddingLeft: 14 }}>修复：{it.fix}</Muted> : null}
              </Col>
            ))}
            <Muted>缺失 {(env.missing || []).length} 项{env.total_missing_gb ? " · 约 " + env.total_missing_gb + "GB" : ""}</Muted>
          </Col>
        ) : null}
      </FoldCard>

      <FoldCard title="本次运行参数" open={openRun} onToggle={() => setOpenRun(!openRun)}
                sub={<Muted>{(plan.label || plan.tier || "?") + (plan.device ? " · " + plan.device : "")}</Muted>}>
        <KV items={[
          ["档位", (plan.label || plan.tier || "?") + "（按空闲显存自动）"],
          ["执行设备", plan.device || "?"],
          ["变声步数", String(eng.infer_step ?? "?")],
          ["并发", String(plan.parallel || 1)],
          ["分离", (plan.sep_device || "?") + " · 步长 " + (plan.sep_step || "?")],
          ["分段上限", String(plan.max_segment_s || "?") + "s"],
          ["空闲显存", String(plan.vram_free_gib ?? "?") + "GB / " + String(plan.vram_total_gib ?? "?") + "GB"],
          ["路径", (s.paths && s.paths.compute_root) || ""],
        ]} />
      </FoldCard>

      {/* 一键全自动：宽横幅卡片，左边文字引导，右边输入 + 导入切歌 */}
      <Panel style={{ padding: 0, overflow: "hidden" }}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 24, alignItems: "stretch" }}>
          <div style={{ flex: "1 1 300px", minWidth: 260, padding: "28px 0 28px 28px", display: "flex", flexDirection: "column", gap: 10 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <span style={{ width: 34, height: 34, borderRadius: 11, background: BRAND_SOFT, color: BRAND, display: "grid", placeItems: "center", fontSize: 16 }}>♪</span>
              <div style={{ fontSize: 17, fontWeight: 700, color: INK }}>一键全自动</div>
            </div>
            <div style={{ fontSize: 12.5, color: INK_2, lineHeight: 1.8 }}>
              录下正在听的歌 → 自动切歌 → 分离 → 变声 → 混音，全程本地（不联网、不上传）。
            </div>
            <Muted>切歌按静音能量谷 + 媒体标题变化；低于 25 秒的片段会合并。</Muted>
            <Row>
              <Muted>录音秒数</Muted>
              <Input type="number" min={10} max={600} value={recordSec} style={{ width: 92 }}
                     onChange={(e: any) => setRecordSec(Number(e.target.value) || 90)} />
              <Btn style={{ border: 0, background: BRAND, color: "#fff", borderRadius: 12, boxShadow: "0 6px 16px rgba(224,45,118,0.22)" }} disabled={!!busy} onClick={doAuto}>
                {busy === "auto" ? "录制中…" : "录下正在听的歌并出曲"}
              </Btn>
            </Row>
          </div>
          <div style={{ flex: "1 1 320px", minWidth: 280, background: "rgba(255,229,239,0.55)", border: "1px solid rgba(224,45,118,0.08)", borderRadius: 14, margin: 14, padding: 18, display: "flex", flexDirection: "column", gap: 12, justifyContent: "center" }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: INK }}>已经有音频文件？</div>
            <Input placeholder="粘贴本地音频路径…" value={importPath}
                   onChange={(e: any) => setImportPath(e.target.value)} />
            <Btn style={{ border: 0, background: BRAND, color: "#fff", borderRadius: 12, boxShadow: "0 6px 16px rgba(224,45,118,0.22)" }} disabled={!!busy} onClick={doImport}>
              {busy === "import" ? "导入中…" : "导入并切歌"}
            </Btn>
          </div>
        </div>
      </Panel>

      {/* 训练新音色：虚线「+ 新建」方块卡片 */}
      <div
        onClick={props.onGoTrain}
        onMouseEnter={(e: any) => { e.currentTarget.style.boxShadow = "0 14px 34px rgba(133,153,189,0.16)"; e.currentTarget.style.background = "#ffffff"; e.currentTarget.style.transform = "translateY(-3px)" }}
        onMouseLeave={(e: any) => { e.currentTarget.style.boxShadow = "0 7px 24px rgba(142,159,187,0.06)"; e.currentTarget.style.background = "rgba(255,255,255,0.72)"; e.currentTarget.style.transform = "none" }}
        style={{
          border: "1.5px dashed rgba(224,45,118,0.36)", borderRadius: 16, padding: 24, cursor: "pointer",
          display: "flex", alignItems: "center", gap: 16, background: "rgba(255,255,255,0.72)", transition: "box-shadow .19s ease, background .19s ease, transform .19s ease",
          boxShadow: "0 7px 24px rgba(142,159,187,0.06)",
        }}
      >
        <span style={{ width: 52, height: 52, borderRadius: 16, background: BRAND_SOFT, color: BRAND, display: "grid", placeItems: "center", fontSize: 26, flex: "0 0 auto", boxShadow: "inset 0 0 0 1px rgba(224,45,118,0.10)" }}>＋</span>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 15, fontWeight: 700, color: INK }}>训练新音色</div>
          <div style={{ fontSize: 12.5, color: INK_2, marginTop: 3 }}>投递素材 → 自动训练 → 到节点试听挑选 → 保存模型</div>
        </div>
      </div>

      <Section title="变声模型（DDSP-SVC）">
        <Col style={{ gap: 12 }}>
          <div style={{ fontSize: 12.5, color: INK_2 }}>
            引擎 {eng.name || "?"} {eng.version || ""} · 当前模型 {eng.ckpt_name || "未找到"} · 变调 {eng.key_shift ?? 0}
            · 推理步数 {eng.infer_step ?? "?"} · 音高 {eng.pitch_extractor || "?"}
          </div>
          {eng.note ? <Muted>{eng.note}</Muted> : null}
          <Row>
            <Field label="变调（半音，-24～24）" style={{ width: 130 }}>
              <Input type="number" min={-24} max={24} step={1} value={keyShift} onChange={(e: any) => setKeyShift(e.target.value)} />
            </Field>
            <Field label="推理步数（5～200）" style={{ width: 130 }}>
              <Input type="number" min={5} max={200} step={1} value={inferStep} onChange={(e: any) => setInferStep(e.target.value)} />
            </Field>
            <Field label="模型" style={{ flex: 1, minWidth: 260 }}>
              <Select value={ckpt} onChange={(e: any) => setCkpt(e.target.value)}>
                <option value="">自动（最新）</option>
                {usable.map((m: any) => (
                  <option key={m.path} value={m.path}>
                    {m.exp_dir} · {m.steps}步{(m.spks && m.spks.length) ? " · " + m.spks.join("/") : ""} · {m.size_mb}MB{m.has_config ? "" : " · 缺 config.yaml"}
                  </option>
                ))}
                {training.length ? (
                  <optgroup label="训练中（尚未保留）">
                    {training.map((m: any) => <option key={m.path} value={m.path}>{m.exp_dir} · {m.steps}步</option>)}
                  </optgroup>
                ) : null}
              </Select>
            </Field>
          </Row>
          <Input placeholder="高级：手填模型 .pt 路径；留空=用上面选中的模型" value={ckptPath}
                 onChange={(e: any) => setCkptPath(e.target.value)} />
          <Row>
            <Btn style={{ border: 0, background: BRAND, color: "#fff", borderRadius: 12, boxShadow: "0 6px 16px rgba(224,45,118,0.22)" }} disabled={!!busy} onClick={saveEngine}>
              {busy === "engine" ? "保存中…" : "保存变声设置"}
            </Btn>
            {usable.length === 0 && !training.length ? <Muted>exp 下还没有可用的 model_*.pt，先去「训练声音模型」页训练一个。</Muted> : null}
          </Row>
        </Col>
      </Section>

      <Section title="曲目" extra={<Muted>共 {s.tracks_total || tracks.length} 首</Muted>}>
        {tracks.length === 0 ? (
          <Empty>还没有曲目。上面「录下正在听的歌并出曲」或「导入并切歌」之后会出现在这里。</Empty>
        ) : (
          <Col style={{ gap: 12 }}>
            {tracks.map((t: any) => {
              const st = tagOf(PHASE, t.status)
              const pct = Math.max(0, Math.min(100, Number(t.progress || 0)))
              const art = t.artifacts || {}
              const step = t.step && t.step !== "waiting" ? " · " + t.step : ""
              return (
                <div key={t.id} style={{ display: "flex", alignItems: "flex-start", gap: 14, flexWrap: "wrap", background: "#fff", border: "1px solid " + LINE, borderRadius: 14, padding: 14, boxShadow: "0 4px 14px rgba(142,159,187,0.06)" }}>
                  <span style={{ width: 44, height: 44, borderRadius: 13, background: "linear-gradient(150deg,#ffe5ef,#f4d6ea)", color: BRAND_DEEP, display: "grid", placeItems: "center", fontSize: 18, flex: "0 0 auto" }}>♪</span>
                  <div style={{ flex: "1 1 240px", minWidth: 200, display: "flex", flexDirection: "column", gap: 6 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                      <span style={{ fontSize: 13.5, fontWeight: 600 }}>{t.title || "(未命名)"}</span>
                      {t.artist ? <Muted>{t.artist}</Muted> : null}
                      <Pill tone={st.tone}>{st.label}{step}</Pill>
                    </div>
                    {t.status === "running" || pct > 0 && pct < 100 ? (
                      <Row>
                        <Bar pct={pct} tone={t.status === "failed" ? "#d64545" : undefined} />
                        <Muted>{pct}%</Muted>
                      </Row>
                    ) : null}
                    {t.error ? <Err>{t.error}</Err> : null}
                    <Row>
                      <Muted>模型 {((t.steps || {}).convert || {}).model || "—"}</Muted>
                      <Muted>设备 {(t.plan || {}).device || "—"} · 并发 {(t.plan || {}).parallel || 1}</Muted>
                    </Row>
                    {art.final ? <Muted style={{ wordBreak: "break-all" }}>成品 {art.final}</Muted> : null}
                    {art.vocals ? <Muted style={{ wordBreak: "break-all" }}>干声 {art.vocals}</Muted> : null}
                  </div>
                  <div style={{ display: "flex", gap: 4, alignItems: "center", flexWrap: "wrap", flex: "0 0 auto" }}>
                    {art.final ? (
                      <Btn disabled={!!busy} onClick={() => preview(t)}>{busy === "audio:" + t.id ? "读取中…" : "试听"}</Btn>
                    ) : null}
                    {t.status !== "running" ? (
                      <Btn disabled={!!busy} onClick={() => processTrack(t)}>{busy === "proc:" + t.id ? "启动中…" : art.final ? "重新出曲" : "出曲"}</Btn>
                    ) : null}
                    <Btn disabled={!!busy} onClick={() => identify(t)}>{busy === "id:" + t.id ? "识别中…" : "识歌校准"}</Btn>
                    <Btn disabled={!!busy} onClick={() => remove(t)}>从列表移除</Btn>
                  </div>
                </div>
              )
            })}
          </Col>
        )}
        {audioBusy && !audio ? <Muted>{audioBusy}</Muted> : null}
      </Section>

      <Section title="任务">
        {jobs.length === 0 ? <Muted>空闲</Muted> : (
          <Col style={{ gap: 6 }}>
            {jobs.slice(-6).map(([id, j]: any) => {
              const js = tagOf(JOB_STATE, j.state)
              return (
                <div key={id} style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", background: "rgba(248,244,248,0.9)", border: "1px solid " + LINE, borderRadius: 12, padding: "8px 12px" }}>
                  <StatusBadge tone={js.tone}>{js.label}</StatusBadge>
                  <span style={{ fontSize: 12, color: "#8a97ad" }}>{id}</span>
                  <Bar pct={Number(j.progress || 0)} />
                  <Muted>{j.progress || 0}%</Muted>
                  <Muted>{j.detail || ""}</Muted>
                </div>
              )
            })}
            {jobs.slice(-6).map(([id, j]: any) => ((j.result && j.result.errors) || []).map((e: any, i: number) => (
              <Err key={id + i}>{e.item + "：" + e.error}</Err>
            )))}
          </Col>
        )}
      </Section>

      <Section title="首次使用向导" extra={wizard || (s.setup && s.setup.completed === false) ? <Btn onClick={() => setWizard(!wizard)}>{wizard ? "收起" : "展开"}</Btn> : null}>
        {wizard ? (
          <Col style={{ gap: 12 }}>
            <Soft style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <div style={{ fontSize: 13, fontWeight: 600 }}>① 检测本机依赖与模型</div>
              <Row>
                <Btn disabled={setupBusy} onClick={setupCheck}>{setupBusy ? "检测中…" : "开始检测"}</Btn>
                <Muted>{setupReport ? (setupReport.ready ? "全部就绪" : "缺失 " + (setupReport.missing || []).length + " 项 · 还需约 " + setupReport.total_missing_gb + "GB") : ""}</Muted>
              </Row>
            </Soft>
            <Soft style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <div style={{ fontSize: 13, fontWeight: 600 }}>② 安装路径</div>
              <Input placeholder="例如 D:\\NEKOMusicSynth" value={setupPath} onChange={(e: any) => setSetupPath(e.target.value)} />
              <Row>
                <Btn style={{ border: 0, background: BRAND, color: "#fff", borderRadius: 12, boxShadow: "0 6px 16px rgba(224,45,118,0.22)" }} disabled={setupBusy || !setupReport} onClick={setupInstall}>{setupBusy ? "安装中…" : "一键安装缺失项"}</Btn>
                <Muted>{setupReport && setupReport.total_missing_gb ? "本次将下载约 " + setupReport.total_missing_gb + "GB" : ""}</Muted>
              </Row>
            </Soft>
            {setupJob ? (
              <Col style={{ gap: 4 }}>
                <Row><Bar pct={Number(setupJob.pct || 0)} /><Muted>{Number(setupJob.pct || 0)}%</Muted></Row>
                <Muted>{setupJob.state} · {setupJob.detail}</Muted>
                {setupJob.state === "failed" ? <Err>{setupJob.detail}</Err> : null}
              </Col>
            ) : null}
            {setupReport ? (
              <Col style={{ gap: 6 }}>
                {(setupReport.items || []).map((it: any, i: number) => (
                  <Row key={i} style={{ gap: 6 }}>
                    <StatusBadge tone={it.status === "ready" || it.ok ? "success" : it.status === "optional" ? "neutral" : it.status === "mismatch" ? "warning" : "error"}>
                      {it.status === "ready" ? "就绪" : it.status === "optional" ? "可选" : it.status === "mismatch" ? "版本不符" : "缺失"}
                    </StatusBadge>
                    <span style={{ fontSize: 12.5 }}>{it.label}</span>
                    <Muted>{it.detail || ""}{it.size_gb ? " · " + it.size_gb + "GB" : ""}</Muted>
                  </Row>
                ))}
              </Col>
            ) : null}
          </Col>
        ) : (
          <Row style={{ justifyContent: "space-between" }}>
            <Muted>依赖或模型还没装全，点右边「展开」按向导补齐。</Muted>
            <Btn onClick={() => setWizard(true)}>展开向导</Btn>
          </Row>
        )}
      </Section>

      {audio ? (
        <Modal title={"试听 · " + audio.title} onClose={() => setAudio(null)}>
          <Col>
            <audio key={audio.id} controls preload="none" autoPlay src={audio.src} onLoadedMetadata={(e: any) => { try { e.currentTarget.currentTime = 0; e.currentTarget.play() } catch (err) {} }} style={{ width: "100%" }} />
            <Muted>在面板内播放，不会打开系统播放器。</Muted>
          </Col>
        </Modal>
      ) : null}

      {confirm ? (
        <Confirm open={true} title={confirm.title} okText={confirm.ok} onCancel={() => setConfirm(null)}
                 onOk={() => { const r = confirm.run; setConfirm(null); r() }}>
          {confirm.text}
        </Confirm>
      ) : null}
    </Col>
  )
}
