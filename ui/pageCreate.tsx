// 为她而唱 · 原创歌曲（ACE-Step text2music，2026-11-05）
//
// 流程：风格描述 + 歌词 → ACE-Step 本地生成整首歌 → 入库成一条曲目。
// 「生成并让她唱」会直接复用现有链路：分离 → DDSP 变声 → 混音。
import { Alert, StatusBadge, useEffect, useRef, useState } from "@neko/plugin-ui"
import { callEntry } from "./api"
import { BRAND_DEEP, INK, INK_2, LINE, Btn, Col, Field, FoldCard, Grid, Input, Muted, Panel, PRIMARY_BTN, Pill, Row, Section, Select, Soft, Toggle } from "./ui"

const TA_STYLE = {
  border: "1px solid transparent", borderRadius: 12, padding: "9px 14px", fontSize: 13,
  background: "rgba(248,244,248,0.95)", color: INK, minWidth: 0, outline: "none",
  boxShadow: "inset 0 1px 2px rgba(40,37,50,0.04)", fontFamily: "inherit",
  resize: "vertical", lineHeight: 1.7, width: "100%", boxSizing: "border-box",
} as any

const LANGS = [
  ["auto", "自动判断"], ["zh", "中文"], ["ja", "日语"], ["en", "英语"], ["ko", "韩语"],
]

export function PageCreate(props: { api: any; snap: any; onRefresh: () => void; onGo: (page: string) => void }) {
  const { api } = props
  const [status, setStatus] = useState<any>(null)
  const [prompt, setPrompt] = useState("")
  const [lyrics, setLyrics] = useState("")
  const [name, setName] = useState("")
  const [duration, setDuration] = useState("0")
  const [bpm, setBpm] = useState("0")
  const [language, setLanguage] = useState("auto")
  const [instrumental, setInstrumental] = useState(false)
  const [lora, setLora] = useState("")
  const [postEngine, setPostEngine] = useState("")
  const [soulxStatus, setSoulxStatus] = useState<any>(null)
  const [installConfirm, setInstallConfirm] = useState<any>(null)
  const [openAdv, setOpenAdv] = useState(false)
  const [advSteps, setAdvSteps] = useState("0")
  const [advGuidance, setAdvGuidance] = useState("7")
  const [advSeed, setAdvSeed] = useState("-1")
  const [model, setModel] = useState("")
  const modelTouched = useRef(false)
  const loraTouched = useRef(false)
  const [busy, setBusy] = useState("")
  const [jobId, setJobId] = useState("")
  const [job, setJob] = useState<any>(null)
  const [notice, setNotice] = useState<any>(null)
  const poll = useRef<any>(null)

  const check = async () => {
    try {
      const r: any = await callEntry(api, "ms_ace_status", {}, 30000)
      setStatus(r)
      if (r && r.model && !modelTouched.current) setModel(String(r.model))
      const cfgLora = String((r && r.ace_lora) || "")
      if (cfgLora && !loraTouched.current) {
        setLora(cfgLora)
        // 当前模型没有这个音色的版本时，自动切到它有版本的底模
        const hit = (r.loras || []).find((x: any) => x.name === cfgLora)
        const bases = (hit && Array.isArray(hit.variants))
          ? hit.variants.map((v: any) => String(v.base || "")).filter(Boolean) : []
        if (!modelTouched.current && bases.length && !bases.includes(String(r.model || ""))) {
          const pick = bases.find((b: string) =>
            Array.isArray(r.models) && r.models.includes(b))
          if (pick) { modelTouched.current = true; setModel(pick) }
        }
      }
    } catch (e: any) {
      setStatus({ ok: false, problems: [String((e && e.message) || e)] })
    }
  }
  useEffect(() => { check(); return () => { if (poll.current) clearInterval(poll.current) } }, [])
  useEffect(() => {
    let alive = true
    callEntry(api, "ms_soulx_status", {}, 30000)
      .then((r: any) => { if (alive) setSoulxStatus(r) })
      .catch(() => {})
    return () => { alive = false }
  }, [])

  useEffect(() => {
    if (!jobId) return
    const tick = async () => {
      try {
        const r: any = await callEntry(api, "ms_ace_job", { job_id: jobId }, 30000)
        const j = (r && r.job) || null
        // 插件重载/重启会把内存里的任务带走；ms_ace_job 会返回 found=false。
        // 以前这里只认 done/failed，于是面板会永远停在「正在生成」且没有进度条。
        if (!j || (r && r.found === false)) {
          if (poll.current) { clearInterval(poll.current); poll.current = null }
          setJob(null)
          setBusy("")
          setJobId("")
          setNotice({
            tone: "error",
            text: "生成任务丢了（插件重载/重启）。已经做好的歌可在「让她唱歌」里找到；请重新点一次生成。",
          })
          check()
          props.onRefresh()
          return
        }
        setJob(j)
        if (j.state === "done" || j.state === "failed") {
          if (poll.current) { clearInterval(poll.current); poll.current = null }
          setBusy("")
          check()
          props.onRefresh()
        }
      } catch (e) { /* 下一拍再试 */ }
    }
    tick()
    poll.current = setInterval(tick, 3000)
    return () => { if (poll.current) { clearInterval(poll.current); poll.current = null } }
  }, [jobId])

  const start = async (sing: boolean) => {
    if (busy) return
    if (!prompt.trim() && !lyrics.trim()) {
      setNotice({ tone: "error", text: "至少写一句风格描述或歌词" })
      return
    }
    setBusy(sing ? "sing" : "gen")
    setNotice(null)
    setJob(null)
    setJobId("")
    try {
      const r: any = await callEntry(api, "ms_ace_generate", {
        prompt: prompt.trim(),
        lyrics: instrumental ? "" : lyrics.trim(),
        name: name.trim(),
        duration: Number(duration) || 0,
        bpm: Number(bpm) || 0,
        language,
        instrumental,
        auto_sing: sing && !lora.trim(),
        post_engine: sing ? postEngine : "",
        inference_steps: Number(advSteps) || 0,
        guidance_scale: Number(advGuidance) || 7,
        seed: String(advSeed).trim() === "" ? -1 : Number(advSeed),
        model,
        lora,
      }, 60000)
      if (r && r.ok && r.job_id) {
        setJobId(String(r.job_id))
        setNotice({ tone: "info", text: "开始生成：" + (r.name || "") + " · 约 " + (r.duration || "?") + " 秒" })
      } else {
        setBusy("")
        setNotice({ tone: "error", text: "没能开始：" + ((r && r.error) || "未知错误") })
      }
    } catch (e: any) {
      setBusy("")
      setNotice({ tone: "error", text: String((e && e.message) || e) })
    }
  }

  const install = async () => {
    if (busy) return
    setBusy("install")
    setNotice(null)
    setJob(null)
    setJobId("")
    try {
      const r: any = await callEntry(api, "ms_ace_install", {}, 30000)
      if (r && r.ok && r.job_id) {
        setJobId(String(r.job_id))
        setNotice({ tone: "info", text: "开始安装 ACE 依赖（约 100MB，需要联网）…" })
      } else {
        setBusy("")
        setNotice({ tone: "error", text: "没能开始安装：" + ((r && r.error) || "未知错误") })
      }
    } catch (e: any) {
      setBusy("")
      setNotice({ tone: "error", text: String((e && e.message) || e) })
    }
  }

  const downloadModel = async (force = false) => {
    if (busy) return
    const target = String(model || "").trim()
    if (!target) { setNotice({ tone: "error", text: "先选一个模型" }); return }
    setBusy("download")
    setNotice(null)
    setJob(null)
    setJobId("")
    try {
      const r: any = await callEntry(api, "ms_ace_download",
        { model: target, force: !!force }, 60000)
      if (r && r.ok && r.job_id) {
        setJobId(String(r.job_id))
        setNotice({ tone: "info", text: "开始下载：" + target + "（可断点续传）" })
      } else if (r && r.ok && r.already) {
        setBusy("")
        setNotice({ tone: "info", text: "这个模型已经下载好了" })
        check()
      } else if (r && r.need_confirm) {
        setBusy("")
        setInstallConfirm(r)
      } else {
        setBusy("")
        setNotice({ tone: "error", text: "没能开始下载：" + ((r && r.error) || "未知错误") })
      }
    } catch (e: any) {
      setBusy("")
      setNotice({ tone: "error", text: String((e && e.message) || e) })
    }
  }

  const loras = (status && Array.isArray(status.loras)) ? status.loras : []
  const catalog = (status && Array.isArray(status.model_catalog)) ? status.model_catalog : []
  const modelMeta = (name: string) => catalog.find((m: any) => m.name === name) || null
  const modelChoices = catalog.length
    ? catalog.map((m: any) => m.name)
    : (status && Array.isArray(status.model_choices) && status.model_choices.length)
      ? status.model_choices
      : (status && status.model ? [status.model] : ["acestep-v15-turbo"])
  const MODEL_HINT: any = {
    "acestep-v15-turbo": "2B · 8 步 · 快",
    "acestep-v15-turbo-shift1": "2B · 8 步 · shift=1",
    "acestep-v15-turbo-shift3": "2B · 8 步 · shift=3",
    "acestep-v15-turbo-continuous": "2B · 8 步 · 连续 shift",
    "acestep-v15-xl-turbo": "4B · 8 步 · 音质更高",
    "acestep-v15-xl-sft": "4B · 50 步 · 最高质量，慢",
    "acestep-v15-xl-base": "4B · 50 步 · 全能编辑",
    "acestep-v15-sft": "2B · 50 步",
    "acestep-v15-base": "2B · 50 步 · 全能编辑",
  }
  const modelReady = !status || !model || (Array.isArray(status.models) && status.models.includes(model))
  const otherProblems = (status && Array.isArray(status.problems) ? status.problems : [])
    .filter((p: any) => !String(p).startsWith("ACE 模型 "))
  const ready = Boolean(status && modelReady && otherProblems.length === 0)
  const loraVariantsOf = (name: string) => {
    const hit = loras.find((l: any) => l.name === name)
    const variants = hit && Array.isArray(hit.variants) ? hit.variants : []
    if (variants.length) return variants
    return hit && hit.base ? [{ name: hit.name, base: hit.base, path: hit.path }] : []
  }
  const loraBasesOf = (name: string) => loraVariantsOf(name)
    .map((v: any) => String(v.base || "")).filter(Boolean)
  const pickLora = (name: string) => {
    loraTouched.current = true
    setLora(name)
    if (!name) return
    const variants = loraVariantsOf(name)
    if (!variants.length) return
    if (variants.some((v: any) => v.base === model)) return
    const target = variants.find((v: any) =>
      Array.isArray(status?.models) && status.models.includes(v.base))
    if (target && target.base) {
      modelTouched.current = true
      setModel(String(target.base))
      setNotice({ tone: "info", text: `「${name}」没有 ${model} 版本，已自动切到 ${target.base}` })
    } else {
      setNotice({ tone: "error", text: `「${name}」需要的底模还没下载：${variants.map((v: any) => v.base).join("、")}` })
    }
  }

  const isDownloadJob = Boolean(job && job.job === "ace-download")
  const stateText = !job ? "" : job.state === "queued" ? "排队中" : job.state === "running" ? (isDownloadJob ? "下载中" : "生成中") : job.state === "done" ? "已完成" : job.state === "failed" ? "失败" : job.state

  return (
    <Col style={{ gap: 24 }}>
      {notice ? <Alert tone={notice.tone === "error" ? "warning" : "info"}>{notice.text}</Alert> : null}
      {status && !ready ? (
        <Alert tone="warning">
          <Col style={{ gap: 8 }}>
            <div>原创生成还没就绪：{(otherProblems.length ? otherProblems : ["ACE 模型 " + (model || status.model)]).join("、")}</div>
            <Row style={{ gap: 8, flexWrap: "wrap" }}>
              <Btn disabled={!!busy} onClick={install}>{busy === "install" ? "安装中…" : "安装 ACE 依赖"}</Btn>
              <Btn disabled={!!busy || modelReady} onClick={downloadModel}>{busy === "download" ? "下载中…" : "下载当前模型"}</Btn>
              <Muted>依赖约 100MB；模型在「{model || "acestep-v15-turbo"}」没下载时会提示单独下载（XL 约 18.6GB）。</Muted>
            </Row>
          </Col>
        </Alert>
      ) : null}

      <Panel title="原创歌曲（ACE-Step）"
             extra={<Pill tone={ready ? "success" : "warning"}>{ready ? "本地模型已就绪" : "环境未就绪"}</Pill>}>
        <Col style={{ gap: 14 }}>
          <div style={{ fontSize: 12.5, color: INK_2, lineHeight: 1.8 }}>
            用文字生成一首完整的歌（人声 + 伴奏），生成后可以：
            <b>只生成</b>（先试听原曲），或 <b>生成并让她唱</b>（直接走分离 → 变声 → 混音，用当前音色）。
          </div>
          <Grid cols={2} min={240} gap={14}>
            <Field label="歌名（留空自动命名）">
              <Input value={name} placeholder="例如 夏夜的风" onChange={(e: any) => setName(e.target.value)} />
            </Field>
            <Field label="时长（秒；0 = 按歌词估算）">
              <Input type="number" min={10} max={600} step={5} value={duration} onChange={(e: any) => setDuration(e.target.value)} />
            </Field>
          </Grid>
          <Field label="风格 / 编曲描述">
            <textarea style={TA_STYLE} rows={3} value={prompt}
                      placeholder="例如：温暖的女声流行，钢琴 + 弦乐，中速，情绪逐渐上扬"
                      onChange={(e: any) => setPrompt(e.target.value)} />
          </Field>
          <Field label="歌词（留空或勾选纯音乐则生成纯伴奏）">
            <textarea style={TA_STYLE} rows={7} value={lyrics}
                      placeholder={"支持 [verse] / [chorus] 结构标签\n第一句歌词…\n第二句歌词…"}
                      onChange={(e: any) => setLyrics(e.target.value)} />
          </Field>
          <Grid cols={3} min={180} gap={14}>
            <Field label="BPM（0 = 自动）">
              <Input type="number" min={0} max={300} step={1} value={bpm} onChange={(e: any) => setBpm(e.target.value)} />
            </Field>
            <Field label="演唱语言">
              <Select value={language} onChange={(e: any) => setLanguage(e.target.value)}>
                {LANGS.map(([v, t]: any) => <option key={v} value={v}>{t}</option>)}
              </Select>
            </Field>
            <Field label="生成方式">
              <Row style={{ paddingTop: 6 }}>
                <Toggle on={instrumental} onChange={setInstrumental} label="纯音乐（不唱）" />
              </Row>
            </Field>
          </Grid>
          <Field label="生成模型（质量 / 速度）">
            <Select value={model} onChange={(e: any) => {
              const v = e.target.value
              modelTouched.current = true
              setModel(v)
              const variants = loraVariantsOf(lora)
              if (lora && variants.length && !variants.some((x: any) => x.base === v)) {
                setLora("")
                setNotice({
                  tone: "info",
                  text: `已取消音色「${lora}」（它没有 ${v} 版本，可用于 ${variants.map((x: any) => x.base).join("、")}）`,
                })
              }
              // 已下载的模型顺手记为默认，AI 之后不带 model 调用也会用它
              if (Array.isArray(status?.models) && status.models.includes(v)) {
                callEntry(api, "ms_set_config", { ace_model: v }, 30000).catch(() => {})
              }
            }}>
              {modelChoices.map((m: any) => {
                const meta = modelMeta(m)
                return (
                  <option key={m} value={m}>
                    {m}{meta
                      ? " · " + meta.family + " · " + meta.steps + "步 · 音质" + meta.quality
                        + " · 需≥" + meta.vram_min_gb + "GB" + (meta.downloaded ? " · 已下载" : " · 未下载")
                      : MODEL_HINT[m] ? " · " + MODEL_HINT[m] : ""}
                  </option>
                )
              })}
            </Select>
          </Field>
          {(() => {
            const meta = modelMeta(model)
            if (!meta) return null
            return (
              <Muted style={{ lineHeight: 1.8 }}>
                能提升：{(meta.gains || []).join("；")}。适合：{meta.best_for}。
                {" "}{meta.downloaded
                  ? "已下载。"
                  : "还没下载，约 " + meta.download_gb + "GB，需要至少 " + meta.vram_min_gb + "GB 显存。"}
              </Muted>
            )
          })()}
          {status && status.model_recommended ? (
            <Muted>这台电脑推荐：{status.model_recommended}（按显存自动判断）</Muted>
          ) : null}
          {status && status.gpu && status.gpu.cuda_ok === false ? (
            <Alert tone="warning">
              显卡不兼容：{(status.gpu.notes || []).join("；")}。原创生成会退回 CPU（很慢）。
            </Alert>
          ) : null}
          {status && status.gpu && status.gpu.cuda_ok ? (
            <Muted style={{ lineHeight: 1.7 }}>
              {status.gpu.name || "GPU"} · {status.gpu.vram_total_gib}GB · sm_{status.gpu.compute_cap} · {status.gpu.dtype} 精度
              {Array.isArray(status.gpu.quantization) && status.gpu.quantization.length
                ? " · 可用量化：" + status.gpu.quantization.join(" / ")
                : " · 不支持量化"}
              {Number(status.gpu.vram_total_gib || 0) <= 6 ? " · 小显存：自动 int8 量化 + VAE 放内存解码" : ""}
            </Muted>
          ) : null}
          {!modelReady ? (
            <Row style={{ flexWrap: "wrap", gap: 10, alignItems: "center" }}>
              <Muted>这个模型还没下载（XL 仓库约 18.6GB，需要 ≥12GB 显存；可断点续传）。</Muted>
              <Btn disabled={!!busy} onClick={downloadModel}>{busy === "download" ? "启动下载…" : "下载这个模型"}</Btn>
            </Row>
          ) : null}
          <Field label="ACE 音色（LoRA；按模型自动选版本）">
            <Select value={lora} onChange={(e: any) => pickLora(e.target.value)}>
              <option value="">默认音色（不加载 LoRA）</option>
              {loras.map((l: any) => (
                <option key={l.name} value={l.name}>
                  {l.name}{loraBasesOf(l.name).length ? " · " + loraBasesOf(l.name).join(" / ") : ""}
                </option>
              ))}
            </Select>
          </Field>
          {lora ? (
            <Muted>
              已选 ACE 音色「{lora}」
              {loraBasesOf(lora).length ? "（可用于 " + loraBasesOf(lora).join("、") + "）" : ""}
              ：生成时会自动挑当前模型对应的版本；默认直接用 ACE 唱这个音色。
            </Muted>
          ) : null}
          <Field label="唱完后再处理干声">
            <Select value={postEngine} onChange={(e: any) => setPostEngine(e.target.value)}>
              <option value="">不用（ACE 直接出成品）</option>
              <option value="ddsp">用翻唱声音（DDSP）</option>
              <option value="soulx">SoulX-Singer SVC（实验）</option>
            </Select>
          </Field>
          {postEngine ? (
            <Muted style={{ lineHeight: 1.75 }}>
              {postEngine === "soulx"
                ? "ACE 先生成整首，再取干声走 SoulX-Singer SVC，最后和伴奏混回去。"
                : "ACE 先生成整首，再取干声用当前翻唱声音变声，最后和伴奏混回去。"}
              点「只生成」时不会后处理。
            </Muted>
          ) : null}
          {postEngine === "soulx" && soulxStatus && !soulxStatus.ready ? (
            <Row style={{ flexWrap: "wrap", gap: 8, alignItems: "center" }}>
              <Muted>SoulX 还没就绪：{(soulxStatus.problems || []).join("、")}</Muted>
              <Btn onClick={() => callEntry(api, "ms_soulx_status", {}, 30000).then(setSoulxStatus).catch(() => {})}>重新检查</Btn>
            </Row>
          ) : null}
          <Row style={{ flexWrap: "wrap", alignItems: "center", gap: 14 }}>
            <Muted>首次加载 ACE 模型会慢一些；同一时间只跑一个生成/唱的任务。</Muted>
          </Row>
          <Row style={{ flexWrap: "wrap", gap: 8 }}>
            <Btn disabled={!!busy || !ready} onClick={() => start(false)}>只生成</Btn>
            <Btn style={PRIMARY_BTN} disabled={!!busy || !ready} onClick={() => start(true)}>
              {busy ? "正在生成…" : (lora ? "用这个 ACE 音色生成" : "生成并让她唱")}
            </Btn>
            <Btn disabled={!!busy} onClick={check}>重新检查环境</Btn>
          </Row>
        </Col>
      </Panel>

      {job ? (
        <Section title={isDownloadJob ? "模型下载" : "生成进度"} extra={<Muted>{stateText}</Muted>}>
          <Panel>
            <Col style={{ gap: 10 }}>
              <Row style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
                <div style={{ fontSize: 14, fontWeight: 700, color: INK }}>
                  {job.state === "done" ? (isDownloadJob ? "模型下载完成" : "生成完成")
                    : job.state === "failed" ? (isDownloadJob ? "模型下载失败" : "生成失败")
                    : (isDownloadJob ? "正在下载模型" : "ACE-Step 正在生成")}
                </div>
                <StatusBadge tone={job.state === "done" ? "success" : job.state === "failed" ? "error" : "warning"}>
                  {stateText}
                </StatusBadge>
              </Row>
              <div style={{ height: 8, borderRadius: 999, background: "rgba(40,37,50,0.08)", overflow: "hidden" }}>
                <div style={{ width: (Number(job.progress) || (job.state === "done" ? 100 : 15)) + "%", height: "100%", background: BRAND_DEEP }} />
              </div>
              <Muted>{job.detail || job.message || "正在准备…"}</Muted>
              {job.state === "running" && Number(job.progress || 0) <= 5 ? (
                <Muted>ACE 首次加载模型较慢；若长时间停在 5% 且没有变化，可能是插件被重载了，请重新点一次生成。</Muted>
              ) : null}
              {job.state === "done" ? (
                <Row style={{ flexWrap: "wrap", gap: 8 }}>
                  {isDownloadJob ? <Btn style={PRIMARY_BTN} onClick={check}>刷新环境</Btn> : null}
                  {isDownloadJob ? <Btn style={PRIMARY_BTN} onClick={check}>刷新环境</Btn> : null}
                  {!isDownloadJob ? <Btn style={PRIMARY_BTN} onClick={() => props.onGo("sing")}>去「让她唱歌」看看</Btn> : null}
                </Row>
              ) : null}
            </Col>
          </Panel>
        </Section>
      ) : null}

      <FoldCard open={openAdv} onToggle={() => setOpenAdv(!openAdv)}
                title="高级选项"
                sub={<Muted>采样步数、引导强度、随机种子</Muted>}>
        <Col style={{ gap: 14 }}>
          <Grid cols={3} min={200} gap={14}>
            <Field label="采样步数（0 = 自动）">
              <Input type="number" min={0} max={60} step={1} value={advSteps}
                     onChange={(e: any) => setAdvSteps(e.target.value)} />
            </Field>
            <Field label="引导强度（turbo 忽略）">
              <Input type="number" min={1} max={15} step={0.5} value={advGuidance}
                     onChange={(e: any) => setAdvGuidance(e.target.value)} />
            </Field>
            <Field label="随机种子（-1 = 随机）">
              <Input type="number" min={-1} step={1} value={advSeed}
                     onChange={(e: any) => setAdvSeed(e.target.value)} />
            </Field>
          </Grid>
          <Muted>
            默认即可：turbo 模型固定 8 步且不使用 CFG；sft/base 默认 50 步。固定种子后，同样的描述和歌词会得到更接近的结果。
          </Muted>
        </Col>
      </FoldCard>

      <Section title="说明" extra={<Muted>本地运行，不联网、不需要账号</Muted>}>
        <Panel>
          <Col style={{ gap: 8 }}>
            <Muted>· ACE 生成的是“整首歌”；「生成并让她唱」会自动分离人声和伴奏，再用当前音色变声并混回去。</Muted>
            <Muted>· 歌词越完整、风格描述越具体，生成越稳定；纯音乐请勾选“纯音乐”。</Muted>
            <Muted>· 同一时间只跑一个重活；正在训练或唱别的歌时，这里会排队等待。</Muted>
            {status && status.paths ? <Muted>· 模型目录：{status.paths.checkpoints}</Muted> : null}
          </Col>
        </Panel>
      </Section>

      {installConfirm ? (
        <Soft style={{ position: "fixed", inset: 0, zIndex: 1160, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(40,37,50,0.38)" }}>
          <div style={{ background: "#fff", borderRadius: 20, padding: 22, maxWidth: 460, boxShadow: "0 18px 44px rgba(40,37,50,0.22)" }}>
            <div style={{ fontSize: 15, fontWeight: 700, color: INK }}>这台电脑可能带不动这个模型</div>
            <div style={{ fontSize: 12.5, color: INK_2, lineHeight: 1.85, margin: "10px 0 16px" }}>
              {installConfirm.warning}
              {" "}仍然可以安装，只是生成时可能很慢或失败。
            </div>
            <Row style={{ justifyContent: "flex-end" }}>
              <Btn onClick={() => setInstallConfirm(null)}>先不装</Btn>
              <Btn style={PRIMARY_BTN} onClick={() => {
                const target = installConfirm.model
                setInstallConfirm(null)
                setModel(String(target || ""))
                downloadModel(true)
              }}>仍然安装</Btn>
            </Row>
          </div>
        </Soft>
      ) : null}
    </Col>
  )
}
