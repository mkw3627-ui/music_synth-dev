// 为她而唱 · 让她唱歌（2026-11-04）
//
// 原来 home.tsx 的「一键全自动 / 导入并切歌 / 曲目 / 任务」原样搬到这里，
// 接口调用（ms_run_auto / ms_import / ms_track_audio / ms_process_track / ms_identify / ms_delete_track）
// 一字未改，只是换成「人话」说法并按模块分页。
import { Alert, StatusBadge, useEffect, useState } from "@neko/plugin-ui"
import { audioSrcOf, callEntry, dataUriOf } from "./api"
import { BRAND, BRAND_DEEP, INK, INK_2, LINE, Bar, Btn, Col, Empty, Err, Input, Muted, Panel, PRIMARY_BTN, Pill, DeletePrompt, Row, Section, Select, Soft, Toggle } from "./ui"

const PHASE = {
  pending: ["等下就处理", "neutral"], waiting: ["等下就处理", "neutral"],
  recorded: ["录好了 · 等着唱", "success"],
  running: ["正在处理", "warning"], done: ["做好啦", "success"], failed: ["没成功", "error"],
}
const JOB_STATE = {
  queued: ["排队", "neutral"], running: ["进行中", "warning"], done: ["完成", "success"],
  failed: ["失败", "error"], canceled: ["已取消", "neutral"],
}

function tagOf(map: any, key: any): any {
  const hit = map[String(key || "")]
  return hit ? { label: hit[0], tone: hit[1] } : { label: "未知", tone: "neutral" }
}

export function PageSing(props: { api: any; snap: any; onRefresh: () => void; onGo: (page: string) => void }) {
  const { api, snap, onRefresh } = props
  const s = snap || {}
  const [busy, setBusy] = useState("")
  const [notice, setNotice] = useState<any>(null)
  const [importPath, setImportPath] = useState("")
  const [importSplit, setImportSplit] = useState(false)
  const [audio, setAudio] = useState<any>(null)
  const [audioBusy, setAudioBusy] = useState("")
  const [confirm, setConfirm] = useState<any>(null)
  const [model, setModel] = useState("")
  const [targets, setTargets] = useState<any[]>([])
  const [targetPid, setTargetPid] = useState("")

  const eng = s.engine || {}
  const models = (Array.isArray(eng.models) ? eng.models : []).filter((m: any) => !m.training)
  const current = models.find((m: any) => m.current) || null
  useEffect(() => {
    if (model) return
    const cur = models.find((m: any) => m.current)
    if (cur) setModel(cur.path)
  }, [snap])

  // 只听一次：可录的音乐应用（视频应用后端会排除）
  useEffect(() => {
    let alive = true
    callEntry(api, "ms_capture_targets", {}, 30000)
      .then((r: any) => { if (alive && r && r.ok) setTargets((r.targets || []).filter((x: any) => x.tag !== "video")) })
      .catch(() => {})
    return () => { alive = false }
  }, [])

  async function run(key: string, fn: () => Promise<any>) {
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

  const doImport = () => run("import", async () => {
    const p = importPath.trim()
    if (!p) { setNotice({ tone: "error", text: "先把音频文件的完整路径填进来" }); return }
    const r = await callEntry(api, "ms_import", { path: p, split: importSplit }, 300000)
    setNotice({ tone: r.ok ? "info" : "error", text: r.ok ? (importSplit ? "已切成 " + (r.tracks || []).length + " 首" : "已导入 1 首") : "没导入成功：" + (r.error || "") })
    onRefresh()
  })

  const preview = (t: any) => run("audio:" + t.id, async () => {
    setAudioBusy("正在读取成品…")
    const r = await callEntry(api, "ms_track_audio", { track_id: t.id }, 120000)
    setAudioBusy("")
    if (!r.ok) { setNotice({ tone: "error", text: String(r.error || "读取成品失败") }); return }
    setAudio({ id: t.id, title: String(r.title || t.title || ""), src: audioSrcOf(r) })
  })

  const deleteTrack = (withFiles: boolean) => {
    const t = confirm
    setConfirm(null)
    if (!t) return
    callEntry(api, "ms_delete_track", { track_id: t.id, delete_files: withFiles }, 60000)
      .then((r: any) => {
        if (r && r.cancelling) {
          setNotice({ tone: "info", text: String(r.note || "正在停止这一首，稍等一下…") })
        } else if (r && r.ok === false) {
          setNotice({ tone: "error", text: String(r.error || "没删成功") })
        }
        onRefresh()
      })
      .catch((e: any) => setNotice({ tone: "error", text: String((e && e.message) || e) }))
  }

  const processTrack = (t: any, force: boolean) => run("proc:" + t.id, async () => {
    const r = await callEntry(api, "ms_process_track", { track_id: t.id, force: !!force }, 60000)
    setNotice({ tone: r.ok ? "info" : "error", text: r.ok ? (force ? "重新唱：" : "接着唱：") + (t.title || t.id) : "没能开始：" + (r.error || "") })
    onRefresh()
  })

  const identify = (t: any) => run("id:" + t.id, async () => {
    const r = await callEntry(api, "ms_identify", { track_id: t.id }, 60000)
    setNotice({ tone: r.ok ? "info" : "error", text: r.ok ? "识歌结果：" + (r.title_kept ? "已校准为 " + r.title_kept : "没有找到更可靠的歌名") : "识别失败：" + (r.error || "") })
    onRefresh()
  })

  const remove = (t: any) => setConfirm(t)

  const pickModel = () => run("model", async () => {
    if (!model) { setNotice({ tone: "error", text: "先挑一个声音" }); return }
    const r = await callEntry(api, "ms_set_active_model", { ckpt: model }, 60000)
    const saved = await callEntry(api, "ms_set_config", { ddsp_ckpt: model }, 30000)
    const ok = !!(r && r.ok) || !!(saved && saved.ok)
    setNotice({ tone: ok ? "info" : "error", text: ok ? "已经换成这个声音，下一首就用它唱" : "换声音失败：" + ((saved && saved.error) || (r && r.error) || "未知错误") })
    onRefresh()
  })

  const listener = s.listener || {}
  const live = listener.live || {}
  const captureRuntime = (s.capture && s.capture.runtime) || {}
  const listening = !!listener.running
  const link: any = s.link || {}
  const linkedMode = listener.mode === "linked" || Boolean(
    link.running && link.monitor && link.source_control && link.selected && link.selected.name)
  const autoTracks = (s.tracks || []).filter((t: any) => t.auto_recorded)

  const startListen = () => run("listen", async () => {
    const args: any = targetPid ? { pid: Number(targetPid) } : {}
    const r: any = await callEntry(api, "ms_song_listen_start", args, 30000)
    setNotice({
      tone: r && r.ok ? "info" : "error",
      text: r && r.ok
        ? (r.mode === "linked" ? "已连接「为她所听」，开始听歌录制…" : "开始独立听歌录制…")
        : ("没开始：" + ((r && r.error) || "")),
    })
    onRefresh()
  })

  const stopListen = () => run("unlisten", async () => {
    await callEntry(api, "ms_song_listen_stop", {}, 30000)
    setNotice({ tone: "info", text: "已停止听歌录制；已经录完的歌都留着" })
    onRefresh()
  })

  const installCapture = () => run("capinstall", async () => {
    const r: any = await callEntry(api, "ms_capture_install", {}, 30000)
    setNotice({
      tone: r && r.ok ? "info" : "error",
      text: r && r.ok ? "正在安装独立采集组件（完成后可开始听歌）" : ("安装失败：" + ((r && r.error) || "")),
    })
    onRefresh()
  })

  const singTrack = (t: any) => run("sing:" + t.id, async () => {
    const r: any = await callEntry(api, "ms_sing_anchor", { track_id: t.id }, 30000)
    setNotice({
      tone: r && r.ok ? "info" : "error",
      text: r && r.ok
        ? (r.queued ? "这首歌还在录，录完就唱" : r.already_done ? "这首已经唱好了"
          : r.already ? "这首歌正在处理" : "开始唱：" + (t.title || ""))
        : ("没能开始：" + ((r && r.error) || "")),
    })
    onRefresh()
  })

  const checkNow = () => run("now", async () => {
    const r: any = await callEntry(api, "ms_now_playing", {}, 30000)
    if (!r || !r.ok) { setNotice({ tone: "error", text: "读不到当前播放（系统媒体会话不可用）" }); return }
    if (!r.playing) {
      setNotice({ tone: "info", text: "现在没有正在播放的媒体" })
      return
    }
    const kind = r.is_music ? "音乐" : r.is_video ? "视频（不会录）" : "类型未知"
    setNotice({
      tone: "info",
      text: kind + "：" + (r.title || "（没取到歌名）") + (r.artist ? " · " + r.artist : ""),
    })
  })

  const tracks = (s.tracks || []).slice().reverse()
  const jobs = Object.entries(s.jobs || {})

  return (
    <Col style={{ gap: 24 }}>
      {notice ? <Alert tone={notice.tone === "error" ? "warning" : "info"}>{notice.text}</Alert> : null}

      {/* 用哪个声音唱 */}
      <Panel style={{ background: "linear-gradient(135deg,rgba(255,229,239,0.75),rgba(255,255,255,0.92))" }}>
        <div style={{ display: "flex", gap: 20, flexWrap: "wrap", alignItems: "center", justifyContent: "space-between" }}>
          <div style={{ display: "flex", gap: 14, alignItems: "center", minWidth: 220 }}>
            <span style={{ width: 44, height: 44, borderRadius: 14, background: "#fff", color: BRAND, display: "grid", placeItems: "center", fontSize: 20, flex: "0 0 auto", boxShadow: "0 6px 16px rgba(224,45,118,0.14)" }}>♪</span>
            <div>
              <div style={{ fontSize: 14.5, fontWeight: 700, color: INK }}>用她的声音唱</div>
              <div style={{ fontSize: 12.5, color: INK_2, marginTop: 3 }}>
                {current ? "现在用的是「" + (current.exp_dir || current.name) + "」" : "还没有可用的声音，先去训练一个"}
              </div>
            </div>
          </div>
          <Row style={{ flex: "1 1 320px" }}>
            <Select value={model} onChange={(e: any) => setModel(e.target.value)} style={{ flex: 1, minWidth: 200 }}>
              {models.length === 0 ? <option value="">（还没有训练好的声音）</option> : null}
              {models.map((m: any) => (
                <option key={m.path} value={m.path}>{(m.exp_dir || m.name) + " · " + m.mtime}</option>
              ))}
            </Select>
            <Btn disabled={!!busy || !model} onClick={pickModel}>{busy === "model" ? "切换中…" : "就用这个"}</Btn>
            <Btn onClick={() => props.onGo("models")}>看我的声音</Btn>
          </Row>
        </div>
      </Panel>

      {/* 导入已有音频 */}
      <Panel>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 24 }}>
          <div style={{ flex: "1 1 300px", minWidth: 240, background: "rgba(255,229,239,0.45)", border: "1px solid rgba(224,45,118,0.08)", borderRadius: 18, padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: INK }}>已经有音频文件？</div>
            <Input placeholder="把音频文件的完整路径粘贴到这里" value={importPath}
                   onChange={(e: any) => setImportPath(e.target.value)} />
            <Toggle on={importSplit} onChange={setImportSplit} label="自动切成多首（长录音/多首歌时再开）" />
            <Btn style={PRIMARY_BTN} disabled={!!busy} onClick={doImport}>{busy === "import" ? "导入中…" : (importSplit ? "导入并切开" : "导入整首")}</Btn>
            <Muted>想做素材的话，去「准备素材」页直接拖文件更方便。</Muted>
          </div>
        </div>
      </Panel>

      {/* 听歌录制：应用声道隔离 */}
      <Panel>
        <Row style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <div style={{ fontSize: 15.5, fontWeight: 700, color: INK }}>听歌录制（只录选中的应用）</div>
            <div style={{ fontSize: 12.5, color: INK_2, lineHeight: 1.8 }}>
              只采集一个播放器的声道，通知声、视频和 AI 自己的声音不会混进来。整首录完先存着不翻唱，等你说「唱一下这首」或「上一首」。
            </div>
          </div>
          <Pill tone={linkedMode ? "success" : "neutral"}>
            {linkedMode ? "已联动为她所听" : "独立模式"}
          </Pill>
        </Row>
        <Row style={{ gap: 8, flexWrap: "wrap", marginTop: 12 }}>
          <Btn style={listening ? undefined : PRIMARY_BTN} disabled={!!busy || listening || linkedMode} onClick={startListen}>
            {busy === "listen" ? "正在开始…" : "开始听歌录制"}
          </Btn>
          <Btn disabled={!!busy || !listening || linkedMode} onClick={stopListen}>
            {busy === "unlisten" ? "正在停止…" : "停止"}
          </Btn>
          <Btn disabled={!!busy || linkedMode} onClick={checkNow}>{busy === "now" ? "读取中…" : "识别当前播放"}</Btn>
          {targets.length ? (
            <Select value={targetPid} onChange={(e: any) => setTargetPid(e.target.value)} disabled={linkedMode} style={{ minWidth: 180 }}>
              <option value="">自动选（第一个音乐应用）</option>
              {targets.map((t: any) => (
                <option key={String(t.pid)} value={String(t.pid)}>{(t.name || "应用") + " · PID " + t.pid}</option>
              ))}
            </Select>
          ) : null}
        </Row>
        {linkedMode ? (
          <div style={{ marginTop: 10, fontSize: 12.5, color: INK_2, lineHeight: 1.8 }}>
            已联动为她所听：音源、开始和停止都由她管理，这里不需要操作；她开始监听后会自动录制。
          </div>
        ) : null}
        <div style={{ marginTop: 10, fontSize: 12.5, color: INK_2, lineHeight: 1.8 }}>
          {listening
            ? (String(live.state || "") === "recording"
              ? "正在录：" + (live.title || "（等识别歌名）") + (live.artist ? " · " + live.artist : "")
                + " · " + Math.round(Number(live.seconds || 0)) + " 秒"
              : String(live.state || "") === "waiting_for_source"
                ? "等待音乐应用开始播放…" : "已启动，正在等待第一首…")
            : "没在听。开始后，只有正在播放的歌曲会被录下来。"}
        </div>
        {listener.error ? <div style={{ marginTop: 6, fontSize: 12, color: "#b9750f" }}>{listener.error}</div> : null}
        {!captureRuntime.ok ? (
          <Row style={{ marginTop: 10, gap: 8, flexWrap: "wrap" }}>
            <Muted>独立采集组件未安装：{captureRuntime.error || "缺少运行环境"}</Muted>
            <Btn disabled={!!busy} onClick={installCapture}>{busy === "capinstall" ? "安装中…" : "安装独立采集组件"}</Btn>
          </Row>
        ) : null}
        {autoTracks.length ? (
          <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ fontSize: 12.5, fontWeight: 700, color: INK }}>最近录下的（最多留 3 首）</div>
            {autoTracks.slice(-3).reverse().map((t: any) => (
              <Row key={t.id} style={{ justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
                <span style={{ fontSize: 12.5, color: INK }}>
                  {t.title || "（没歌名）"}{t.artist ? " · " + t.artist : ""}
                </span>
                <Row style={{ gap: 8 }}>
                  <Muted>{tagOf(PHASE, t.status).label}</Muted>
                  <Btn disabled={!!busy || t.status === "running"} onClick={() => singTrack(t)}>唱这一首</Btn>
                </Row>
              </Row>
            ))}
          </div>
        ) : null}
      </Panel>

      {/* 曲目 */}
      <Section title="唱好的歌" extra={<Muted>共 {s.tracks_total || tracks.length} 首</Muted>}>
        {tracks.length === 0 ? (
          <Empty>还没有歌。上面录一段，或者导入一个音频文件，做好之后会出现在这里。</Empty>
        ) : (
          <Col style={{ gap: 12 }}>
            {tracks.map((t: any) => {
              const st = tagOf(PHASE, t.status)
              const pct = Math.max(0, Math.min(100, Number(t.progress || 0)))
              const art = t.artifacts || {}
              return (
                <div key={t.id} style={{ display: "flex", alignItems: "flex-start", gap: 14, flexWrap: "wrap", background: "#fff", border: "1px solid " + LINE, borderRadius: 16, padding: 14, boxShadow: "0 4px 14px rgba(142,159,187,0.06)" }}>
                  <span style={{ width: 44, height: 44, borderRadius: 13, background: "linear-gradient(150deg,#ffe5ef,#f4d6ea)", color: BRAND_DEEP, display: "grid", placeItems: "center", fontSize: 18, flex: "0 0 auto" }}>♪</span>
                  <div style={{ flex: "1 1 240px", minWidth: 180, display: "flex", flexDirection: "column", gap: 6 }}>
                    <Row style={{ gap: 8 }}>
                      <span style={{ fontSize: 13.5, fontWeight: 600, color: INK }}>{t.title || "（没取到歌名）"}</span>
                      {t.artist ? <Muted>{t.artist}</Muted> : null}
                      <Pill tone={st.tone}>{st.label}</Pill>
                    </Row>
                    {t.status === "running" || (pct > 0 && pct < 100) ? (
                      <Row><Bar pct={pct} /><Muted>{pct}%</Muted></Row>
                    ) : null}
                    {t.error ? <Err>{t.error}</Err> : null}
                    {art.final ? <Muted style={{ wordBreak: "break-all" }}>成品在 {art.final}</Muted> : null}
                  </div>
                  <div style={{ display: "flex", gap: 4, alignItems: "center", flexWrap: "wrap", flex: "0 0 auto" }}>
                    {t.playable === false ? <Muted>成品文件不在了</Muted>
                      : art.final ? <Btn disabled={!!busy} onClick={() => preview(t)}>{busy === "audio:" + t.id ? "读取中…" : "试听"}</Btn> : null}
                    {t.status !== "running"
                      ? <Btn disabled={!!busy} onClick={() => processTrack(t, !!art.final)}>{busy === "proc:" + t.id ? "启动中…" : art.final ? "重新唱一遍" : (t.resumable || art.vocal_wav || art.vocal_clean || art.vocals) ? "继续唱" : "让她唱"}</Btn>
                      : null}
                    <Btn disabled={!!busy} onClick={() => identify(t)}>{busy === "id:" + t.id ? "识别中…" : "校准歌名"}</Btn>
                    <Btn style={{ color: "#c93c3c", borderColor: "rgba(201,60,60,0.28)" }} onClick={() => remove(t)}>删除</Btn>
                  </div>
                </div>
              )
            })}
          </Col>
        )}
        {audioBusy && !audio ? <Muted>{audioBusy}</Muted> : null}
        {audio ? (
          <div style={{ marginTop: 12, padding: 12, borderRadius: 14, background: "#fff", border: "1px solid " + LINE }}>
            <Row style={{ justifyContent: "space-between", marginBottom: 6 }}>
              <div style={{ fontSize: 12.5, color: INK, fontWeight: 600 }}>试听 · {audio.title}</div>
              <Btn onClick={() => setAudio(null)}>关掉</Btn>
            </Row>
            {/* preload=metadata：先拿到时长、能显示进度条；浏览器拦了自动播放也能手动点播放 */}
            <audio key={audio.id} controls preload="metadata" autoPlay src={audio.src} style={{ width: "100%" }} />
          </div>
        ) : null}
      </Section>

      <DeletePrompt open={!!confirm}
                    title="删除这首成品？"
                    message={(confirm ? (confirm.title || confirm.id) : "") +
                             " —— 选「连本地文件一起删除」会把成品和它的中间文件移进回收站。"}
                    recordLabel="只删列表记录（成品留着）"
                    onCancel={() => setConfirm(null)}
                    onRecordOnly={() => deleteTrack(false)}
                    onWithFiles={() => deleteTrack(true)} />

      {/* 任务 */}
      <Section title="后台在做什么">
        {jobs.length === 0 ? <Muted>现在没有在跑的任务。</Muted> : (
          <Col style={{ gap: 8 }}>
            {jobs.slice(-6).map(([id, j]: any) => {
              const js = tagOf(JOB_STATE, j.state)
              return (
                <div key={id} style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", background: "rgba(248,250,252,0.9)", border: "1px solid " + LINE, borderRadius: 12, padding: "8px 12px" }}>
                  <StatusBadge tone={js.tone}>{js.label}</StatusBadge>
                  <div style={{ flex: "1 1 120px", minWidth: 100 }}><Bar pct={Number(j.progress || 0)} /></div>
                  <Muted>{j.progress || 0}%</Muted>
                  <Muted>{j.detail || ""}</Muted>
                  {((j.result && j.result.errors) || []).map((e: any, i: number) => <Err key={i}>{e.item + "：" + e.error}</Err>)}
                </div>
              )
            })}
          </Col>
        )}
      </Section>

      <Soft style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", justifyContent: "space-between" }}>
        <Muted>想要她唱得更像某个声音？先训练一个属于她的音色。</Muted>
        <Btn style={PRIMARY_BTN} onClick={() => props.onGo("train")}>去训练声音 →</Btn>
      </Soft>
    </Col>
  )
}
