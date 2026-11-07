// 为她而唱 · 我的声音（2026-11-04）
//
// 把她训练好的声音排成卡片：点「设为当前」= ms_set_active_model（之后再多存一次 ms_set_config{ddsp_ckpt}，
// 让设置页里的模型选择也跟过来），点「试听」= ms_train_preview。正在训练中的声音单独一组，标注「还没训完」。
// 只读：这里不删除任何训练结果。
import { Alert, StatusBadge, useEffect, useState } from "@neko/plugin-ui"
import { audioSrcOf, callEntry, dataUriOf } from "./api"
import { BRAND, BRAND_DEEP, INK, INK_2, LINE, Bar, Btn, Col, DeletePrompt, Empty, Grid, Muted, Panel, PRIMARY_BTN, Pill, Row, Section } from "./ui"

export function PageModels(props: { api: any; snap: any; onRefresh: () => void; onGo: (page: string) => void }) {
  const { api, snap, onRefresh } = props
  const s = snap || {}
  const eng = s.engine || {}
  const all = Array.isArray(eng.models) ? eng.models : []
  const ready = all.filter((m: any) => !m.training)
  const working = all.filter((m: any) => m.training)
  const currentVoice = all.find((m: any) => m && m.current) || null
  const currentName = currentVoice ? String(currentVoice.exp_dir || "") : ""
  const [busy, setBusy] = useState("")
  const [notice, setNotice] = useState<any>(null)
  const [open, setOpen] = useState("")
  const [audio, setAudio] = useState<any>(null)
  const [delTarget, setDelTarget] = useState<any>(null)
  const [aceDelTarget, setAceDelTarget] = useState<any>(null)
  const [exported, setExported] = useState<any>(null)
  const [aceExported, setAceExported] = useState<any>(null)
  const [ace, setAce] = useState<any>(null)
  const aceLoras = (ace && Array.isArray(ace.loras)) ? ace.loras : []
  const aceModels = (ace && Array.isArray(ace.models)) ? ace.models : []
  const aceCurrent = String((ace && ace.ace_lora) || "")

  useEffect(() => {
    let alive = true
    callEntry(api, "ms_ace_status", {}, 30000)
      .then((r: any) => { if (alive) setAce(r) })
      .catch(() => {})
    return () => { alive = false }
  }, [snap])

  const setCurrent = async (m: any) => {
    setBusy("cur:" + m.path)
    setNotice(null)
    try {
      const r: any = await callEntry(api, "ms_set_active_model", { ckpt: m.path }, 60000)
      const saved: any = await callEntry(api, "ms_set_config", { ddsp_ckpt: m.path }, 30000)
      const ok = !!(r && r.ok) || !!(saved && saved.ok)
      setNotice({ tone: ok ? "info" : "error", text: ok ? "以后唱歌都用「" + (m.exp_dir || "训练好的声音") + "」了" : "没切换成功：" + ((saved && saved.error) || (r && r.error) || "未知错误") })
      onRefresh()
    } catch (e: any) {
      setNotice({ tone: "error", text: String((e && e.message) || e) })
    }
    setBusy("")
  }

  const exportModel = async (m: any) => {
    setBusy("exp:" + m.path)
    setNotice(null)
    try {
      const r: any = await callEntry(api, "ms_export_model", { path: m.path }, 600000)
      if (r && r.ok) {
        setExported(r)
        setNotice({ tone: "info", text: "已导出「" + (r.name || m.exp_dir || "声音") + "」" })
      } else {
        setNotice({ tone: "error", text: "导出失败：" + ((r && r.error) || "未知错误") })
      }
    } catch (e: any) {
      setNotice({ tone: "error", text: "导出失败：" + String((e && e.message) || e) })
    }
    setBusy("")
  }

  const openExportDir = async () => {
    try {
      const r: any = await callEntry(api, "ms_open_export_dir", {}, 30000)
      if (r && r.ok === false) setNotice({ tone: "error", text: String(r.error || "打不开导出文件夹") })
    } catch (e: any) {
      setNotice({ tone: "error", text: String((e && e.message) || e) })
    }
  }

  const deleteModel = async (withFiles: boolean) => {
    const m = delTarget
    setDelTarget(null)
    if (!m) return
    setBusy("del:" + m.path)
    setNotice(null)
    try {
      const r: any = await callEntry(api, "ms_delete_model", { path: m.path, delete_files: withFiles }, 60000)
      if (r && r.ok === false) setNotice({ tone: "error", text: String(r.error || "没删成功") })
      else setNotice({ tone: "info", text: (m.exp_dir || "训练好的声音") + " 已删除" + (withFiles ? "（文件已移到回收站）" : "") })
      onRefresh()
    } catch (e: any) {
      setNotice({ tone: "error", text: String((e && e.message) || e) })
    }
    setBusy("")
  }

  const preview = async (m: any) => {
    setBusy("pv:" + m.path)
    setNotice(null)
    try {
      const r: any = await callEntry(api, "ms_train_preview", { run_id: String(m.exp_dir || ""), step: Number(m.steps || 0) }, 120000)
      const src = audioSrcOf(r)
      if (src) setAudio({ id: m.path, title: m.exp_dir || "训练好的声音", src })
      else setNotice({ tone: "error", text: "试听暂时不可用：" + ((r && r.error) || "没有拿到音频") })
    } catch (e: any) {
      setNotice({ tone: "error", text: "试听暂时不可用：" + String((e && e.message) || e) })
    }
    setBusy("")
  }

  const setAceCurrent = async (l: any) => {
    setBusy("acecur:" + l.name)
    setNotice(null)
    try {
      const r: any = await callEntry(api, "ms_ace_set_lora", { name: l.name }, 30000)
      setNotice({
        tone: r && r.ok ? "info" : "error",
        text: r && r.ok
          ? "原创生成默认用「" + l.name + "」；换生成模型时会自动挑对应版本"
          : "没设置成功：" + ((r && r.error) || "未知错误"),
      })
      if (r && r.ok) onRefresh()
    } catch (e: any) {
      setNotice({ tone: "error", text: String((e && e.message) || e) })
    }
    setBusy("")
  }

  const previewAce = async (l: any) => {
    setBusy("acepv:" + l.name)
    setNotice({ tone: "info", text: "正在生成试听（首次约 20-60 秒）…" })
    try {
      const r: any = await callEntry(api, "ms_ace_preview", { name: l.name }, 300000)
      const src = audioSrcOf(r)
      if (src) {
        setAudio({ id: "ace:" + l.name, title: l.name + " · 原创音色试听", src })
        setNotice(null)
      } else {
        setNotice({ tone: "error", text: "试听失败：" + ((r && r.error) || "没有拿到音频") })
      }
    } catch (e: any) {
      setNotice({ tone: "error", text: "试听失败：" + String((e && e.message) || e) })
    }
    setBusy("")
  }

  const exportAce = async (l: any) => {
    setBusy("aceexp:" + l.name)
    setNotice(null)
    try {
      const r: any = await callEntry(api, "ms_ace_export_lora", { name: l.name }, 600000)
      if (r && r.ok) {
        setAceExported(r)
        setNotice({ tone: "info", text: "已导出「" + l.name + "」" })
      } else {
        setNotice({ tone: "error", text: "导出失败：" + ((r && r.error) || "未知错误") })
      }
    } catch (e: any) {
      setNotice({ tone: "error", text: "导出失败：" + String((e && e.message) || e) })
    }
    setBusy("")
  }

  const deleteAce = async () => {
    const l = aceDelTarget
    setAceDelTarget(null)
    if (!l) return
    setBusy("acedel:" + l.name)
    setNotice(null)
    try {
      const r: any = await callEntry(api, "ms_ace_delete_lora",
        { name: l.name, delete_files: true }, 120000)
      if (r && r.ok === false) setNotice({ tone: "error", text: String(r.error || "没删成功") })
      else setNotice({ tone: "info", text: "「" + l.name + "」已移进回收站" })
      onRefresh()
    } catch (e: any) {
      setNotice({ tone: "error", text: String((e && e.message) || e) })
    }
    setBusy("")
  }


  const card = (m: any, isWorking: boolean) => (
    <Panel key={m.path} style={{ display: "flex", flexDirection: "column", gap: 12, border: m.current ? "1px solid rgba(224,45,118,0.35)" : undefined }}>
      <Row style={{ justifyContent: "space-between", gap: 8 }}>
        <div style={{ display: "flex", gap: 12, alignItems: "center", minWidth: 0 }}>
          <span style={{ width: 40, height: 40, borderRadius: 13, background: "linear-gradient(150deg,#ffe5ef,#f4d6ea)", color: BRAND, display: "grid", placeItems: "center", fontSize: 18, flex: "0 0 auto" }}>♪</span>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 13.5, fontWeight: 700, color: INK, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{m.exp_dir || "训练好的声音"}</div>
            <div style={{ fontSize: 11.5, color: INK_2 }}>{m.mtime || ""}{m.size_mb ? " · " + m.size_mb + " MB" : ""}</div>
          </div>
        </div>
        <Row style={{ gap: 6 }}>
          {m.bundled ? <Pill tone="success">内置 · 体验</Pill> : null}
          {m.current ? <Pill tone="success">正在使用</Pill> : isWorking ? <Pill tone="warning">还没训完</Pill> : null}
        </Row>
      </Row>
      <Row>
        {m.has_config ? null : <StatusBadge tone="warning">缺少配置文件，可能用不了</StatusBadge>}
        {(m.spks || []).length ? <Muted>歌者：{(m.spks || []).join(" / ")}</Muted> : null}
      </Row>
      {open === m.path ? (
        <div style={{ padding: 10, borderRadius: 12, background: "rgba(248,250,252,0.95)", border: "1px solid " + LINE }}>
          <Muted style={{ wordBreak: "break-all" }}>{m.path}</Muted>
        </div>
      ) : null}
      <Row style={{ marginTop: "auto" }}>
        {!isWorking ? (
          <Btn style={m.current ? undefined : PRIMARY_BTN} disabled={!!busy || m.current} onClick={() => setCurrent(m)}>
            {m.current ? "正在使用" : busy === "cur:" + m.path ? "切换中…" : "设为当前"}
          </Btn>
        ) : null}
        <Btn disabled={!!busy} onClick={() => preview(m)}>{busy === "pv:" + m.path ? "读取中…" : "试听"}</Btn>
        {!isWorking ? (
          <Btn disabled={!!busy} onClick={() => exportModel(m)}>{busy === "exp:" + m.path ? "导出中…" : "导出"}</Btn>
        ) : null}
        <Btn onClick={() => setOpen(open === m.path ? "" : m.path)}>{open === m.path ? "收起位置" : "在哪里"}</Btn>
        {!isWorking && !m.current ? (
          <Btn style={{ color: "#c93c3c", borderColor: "rgba(201,60,60,0.28)" }}
               disabled={!!busy} onClick={() => setDelTarget(m)}>删除</Btn>
        ) : null}
      </Row>
    </Panel>
  )

  return (
    <Col style={{ gap: 24 }}>
      {notice ? <Alert tone={notice.tone === "error" ? "warning" : "info"}>{notice.text}</Alert> : null}

      <Section title="翻唱的声音" extra={<Muted>{ready.length} 个 · 当前：{currentName || "未选"}</Muted>}>
        {ready.length === 0 ? (
          <Empty>还没有翻唱声音。先去「准备素材」放几首歌，再到「训练声音」开始训练。</Empty>
        ) : (
          <Grid cols={3} min={280} gap={20}>
            {ready.map((m: any) => card(m, false))}
          </Grid>
        )}
        {audio ? (
          <div style={{ marginTop: 12, padding: 12, borderRadius: 14, background: "#fff", border: "1px solid " + LINE }}>
            <Row style={{ justifyContent: "space-between", marginBottom: 6 }}>
              <div style={{ fontSize: 12.5, color: INK, fontWeight: 600 }}>试听 · {audio.title}</div>
              <Btn onClick={() => setAudio(null)}>关掉</Btn>
            </Row>
            <audio key={audio.id} controls autoPlay src={audio.src} style={{ width: "100%" }} />
          </div>
        ) : null}
        {exported ? (
          <Row style={{ marginTop: 12, flexWrap: "wrap", gap: 10, alignItems: "center" }}>
            <Muted style={{ wordBreak: "break-all" }}>已导出到：{exported.path}</Muted>
            <Btn onClick={openExportDir}>打开导出文件夹</Btn>
          </Row>
        ) : null}
        <Row style={{ marginTop: 14 }}>
          <Btn style={PRIMARY_BTN} onClick={() => props.onGo("train")}>训练新的翻唱声音 →</Btn>
          <Btn onClick={() => props.onGo("sing")}>去让她唱歌</Btn>
        </Row>
      </Section>

      <Section title="原创歌曲的声音" extra={<Muted>{aceLoras.length} 个音色 · 按生成模型自动选版本</Muted>}>
        {ace === null ? (
          <Muted>正在读取原创音色…</Muted>
        ) : aceLoras.length === 0 ? (
          <Empty>
            还没有原创歌曲音色。到「训练声音」勾选「原创歌曲的声音」训练；或者先去「原创歌曲」下载可用的 ACE 模型
            （{aceModels.length ? aceModels.join("、") : "暂无"}）。
          </Empty>
        ) : (
          <Grid cols={3} min={280} gap={20}>
            {aceLoras.map((l: any) => {
              const isCur = aceCurrent === l.name
              const bases = (Array.isArray(l.bases) && l.bases.length)
                ? l.bases : (l.base ? [l.base] : [])
              return (
                <Panel key={l.name} style={{ display: "flex", flexDirection: "column", gap: 12, border: isCur ? "1px solid rgba(224,45,118,0.35)" : undefined }}>
                  <Row style={{ justifyContent: "space-between", gap: 8 }}>
                    <Row style={{ gap: 10, alignItems: "center", minWidth: 0 }}>
                      <span style={{ width: 40, height: 40, borderRadius: 13, background: "linear-gradient(150deg,#ffe5ef,#f4d6ea)", color: BRAND, display: "grid", placeItems: "center", fontSize: 18, flex: "0 0 auto" }}>✦</span>
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontSize: 13.5, fontWeight: 700, color: INK, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{l.name}</div>
                        <div style={{ fontSize: 11.5, color: INK_2 }}>基于 {bases.join(" / ") || "未知底模"}</div>
                      </div>
                    </Row>
                    <Pill tone={isCur || l.bundled ? "success" : "neutral"}>{isCur ? "正在使用" : l.bundled ? "内置 · 体验" : "原创音色"}</Pill>
                  </Row>
                  {open === "ace:" + l.name ? (
                    <div style={{ padding: 10, borderRadius: 12, background: "rgba(248,250,252,0.95)", border: "1px solid " + LINE }}>
                      <Muted style={{ wordBreak: "break-all" }}>{l.path || ""}</Muted>
                    </div>
                  ) : null}
                  <Row style={{ marginTop: "auto", flexWrap: "wrap" }}>
                    <Btn style={isCur ? undefined : PRIMARY_BTN} disabled={!!busy || isCur} onClick={() => setAceCurrent(l)}>
                      {isCur ? "正在使用" : busy === "acecur:" + l.name ? "设置中…" : "设为当前"}
                    </Btn>
                    <Btn disabled={!!busy} onClick={() => previewAce(l)}>{busy === "acepv:" + l.name ? "生成中…" : "试听"}</Btn>
                    <Btn disabled={!!busy} onClick={() => exportAce(l)}>{busy === "aceexp:" + l.name ? "导出中…" : "导出"}</Btn>
                    <Btn onClick={() => setOpen(open === "ace:" + l.name ? "" : "ace:" + l.name)}>{open === "ace:" + l.name ? "收起位置" : "在哪里"}</Btn>
                    <Btn style={{ color: "#c93c3c", borderColor: "rgba(201,60,60,0.28)" }} disabled={!!busy} onClick={() => setAceDelTarget(l)}>删除</Btn>
                  </Row>
                </Panel>
              )
            })}
          </Grid>
        )}
        {aceExported ? (
          <Row style={{ marginTop: 12, flexWrap: "wrap", gap: 10, alignItems: "center" }}>
            <Muted style={{ wordBreak: "break-all" }}>已导出到：{aceExported.path}</Muted>
            <Btn onClick={openExportDir}>打开导出文件夹</Btn>
          </Row>
        ) : null}
        <Row style={{ marginTop: 14 }}>
          <Btn style={PRIMARY_BTN} onClick={() => props.onGo("train")}>训练原创歌曲声音 →</Btn>
          <Btn onClick={() => props.onGo("create")}>去原创歌曲 →</Btn>
        </Row>
      </Section>

      {working.length ? (
        <Section title="还在训练" extra={<Muted>训完后会自动出现在上面</Muted>}>
          <Grid cols={3} min={280} gap={20}>
            {working.map((m: any) => card(m, true))}
          </Grid>
          <div style={{ marginTop: 10 }}><Bar pct={0} tone="rgba(224,45,118,0.25)" /></div>
          <Muted>进度在「训练声音」页能看到。</Muted>
        </Section>
      ) : null}

      <Muted style={{ color: BRAND_DEEP }}>提示：不要的声音可以直接删；选择「连本地文件一起删除」时会移进回收站，能找回。</Muted>

      <DeletePrompt open={!!delTarget}
                    title="删除这个训练好的声音？"
                    message={(delTarget ? (delTarget.exp_dir || delTarget.name) : "") +
                             " —— 连本地文件一起删会把它的检查点和实验目录移进回收站。"}
                    recordLabel="只从列表移除（文件保留）"
                    onCancel={() => setDelTarget(null)}
                    onRecordOnly={() => deleteModel(false)}
                    onWithFiles={() => deleteModel(true)} />

      <DeletePrompt open={!!aceDelTarget}
                    title="删除这个原创音色？"
                    message={(aceDelTarget ? (aceDelTarget.name || "") : "") +
                             " —— 会把这个音色的所有底模变体（LoRA）一起移进回收站。"}
                    recordLabel="先不删（取消）"
                    onCancel={() => setAceDelTarget(null)}
                    onRecordOnly={() => setAceDelTarget(null)}
                    onWithFiles={deleteAce} />
    </Col>
  )
}
