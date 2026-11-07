// 为她而唱 · 开始（2026-11-04，方案第 2 节）
//
// 打开插件看到的第一页：一句问候 + 下一步该做什么（准备素材 → 训练声音 → 让她唱歌）。
// 下面三张卡：素材够不够、有没有能用的声音、唱好了几首。只读，不调用任何新接口；
// 素材数字走 materials.ts 的共享单例（和「准备素材」页同一份），其余用面板传下来的 ms_state。
import { Alert, StatusBadge } from "@neko/plugin-ui"
import { MATERIALS, useCorpus } from "./materials"
import { BRAND, BRAND_DEEP, INK, INK_2, Bar, Btn, Col, Grid, Muted, Panel, PRIMARY_BTN, Ring, Row, Section, Soft, fmtTime } from "./ui"
import { STEPS_CHOICES, deficitText, effectiveSeconds, materialTitle, num, requirementOf, slotView } from "./trainModel"

function greeting(): string {
  const h = new Date().getHours()
  if (h < 5) return "这么晚还没睡呀"
  if (h < 11) return "早上好"
  if (h < 13) return "中午好"
  if (h < 18) return "下午好"
  return "晚上好"
}

export function PageStart(props: { api: any; snap: any; onGo: (page: string) => void }) {
  const { api, snap, onGo } = props
  const s = snap || {}
  const c = useCorpus(api)
  const eng = s.engine || {}
  const all = Array.isArray(eng.models) ? eng.models : []
  const readyModels = all.filter((m: any) => !m.training)
  const currentVoice = all.find((m: any) => m && m.current) || null
  // 界面只显示用户认得的名字（训练目录名），不显示 model_24000.pt 这种文件名
  const voiceName = currentVoice ? String(currentVoice.exp_dir || "") : ""
  const tracks = Array.isArray(s.tracks) ? s.tracks : []
  const done = tracks.filter((t: any) => t.status === "done").length

  const need = requirementOf(c.req, STEPS_CHOICES[1])
  const have = effectiveSeconds(c.list, c.countSpeech)
  const recS = need.rec > need.min ? need.rec : need.min + 60
  const pct = Math.min(100, Math.round((have / (recS || 1)) * 100))
  const files = MATERIALS.reduce((a: number, m: any) => a + slotView(c.list, m.slot).files, 0)

  // 下一步该做什么：素材不够 → 先放素材；没训过 → 去训练；都齐了 → 去唱歌
  const next = have < need.min
    ? { title: "先把几首歌放进「准备素材」", text: "想让她会唱哪种语言，就投那种语言的素材。放好之后这里会显示够不够。", btn: "去准备素材", page: "materials" }
    : readyModels.length === 0
      ? { title: "素材够了，训练她的声音吧", text: "训练会用上这些素材；训好后就能用她的声音唱任何歌。", btn: "去训练声音", page: "train" }
      : { title: "一切就绪，让她唱一首吧", text: "录下正在听的歌，或者导入一个音频文件。", btn: "让她唱歌", page: "sing" }

  const guide = [
    { n: 1, title: "准备素材", text: "放几首带伴奏的歌、清唱或说话录音", ok: have >= need.min, page: "materials" },
    { n: 2, title: "训练声音", text: "用素材练出属于她的音色", ok: readyModels.length > 0, page: "train" },
    { n: 3, title: "让她唱歌", text: "录歌或导入音频，换成她的声音", ok: done > 0, page: "sing" },
  ]

  return (
    <Col style={{ gap: 24 }}>
      {c.error ? <Alert tone="warning">素材列表暂时没读到：{c.error}</Alert> : null}

      {/* 问候 + 下一步 */}
      <Panel style={{ background: "linear-gradient(135deg,rgba(255,229,239,0.8),rgba(255,255,255,0.94) 62%)" }}>
        <div style={{ display: "flex", gap: 24, flexWrap: "wrap", alignItems: "center", justifyContent: "space-between" }}>
          <div style={{ flex: "1 1 320px", minWidth: 240, display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ fontSize: 20, fontWeight: 700, color: INK }}>{greeting()}，我是为你而唱的。</div>
            <div style={{ fontSize: 13.5, color: BRAND_DEEP, fontWeight: 700 }}>{next.title}</div>
            <div style={{ fontSize: 12.5, color: INK_2, lineHeight: 1.85 }}>{next.text}</div>
            <Row>
              <Btn style={PRIMARY_BTN} onClick={() => onGo(next.page)}>{next.btn} →</Btn>
              {readyModels.length ? <Btn onClick={() => onGo("models")}>看看她的声音</Btn> : null}
            </Row>
          </div>
          <Ring pct={pct} size={112} star={have >= recS} label={pct + "%"} />
        </div>
      </Panel>

      {/* 三步 */}
      <Section title="从这里开始" extra={<Muted>三步就能听到她用另一种声音唱歌</Muted>}>
        <Grid cols={3} min={240} gap={20}>
          {guide.map((g: any) => (
            <Panel key={g.n} hover onClick={() => onGo(g.page)} style={{ display: "flex", flexDirection: "column", gap: 10, cursor: "pointer" }}>
              <Row style={{ justifyContent: "space-between" }}>
                <span style={{ width: 34, height: 34, borderRadius: 12, background: g.ok ? "rgba(28,138,88,0.12)" : "rgba(255,229,239,0.9)", color: g.ok ? "#1c8a58" : BRAND_DEEP, display: "grid", placeItems: "center", fontWeight: 700, fontSize: 14 }}>
                  {g.ok ? "✓" : g.n}
                </span>
                <StatusBadge tone={g.ok ? "success" : "neutral"}>{g.ok ? "已完成" : "待开始"}</StatusBadge>
              </Row>
              <div style={{ fontSize: 15, fontWeight: 700, color: INK }}>{g.title}</div>
              <div style={{ fontSize: 12.5, color: INK_2, lineHeight: 1.8 }}>{g.text}</div>
              <Muted style={{ marginTop: "auto", color: BRAND_DEEP }}>{g.ok ? "再进去看看 →" : "去这里 →"}</Muted>
            </Panel>
          ))}
        </Grid>
      </Section>

      {/* 现在的情况 */}
      <Section title="现在的情况" extra={<Muted>{s.last_run && s.last_run.at ? "上次检查：" + s.last_run.at : ""}</Muted>}>
        <Grid cols={3} min={240} gap={20}>
          <Panel style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ fontSize: 12.5, color: INK_2 }}>素材</div>
            <div style={{ fontSize: 16, fontWeight: 700, color: INK }}>{files} 个文件 · {fmtTime(have)}</div>
            <Bar pct={have >= recS ? 100 : pct} />
            <Muted>{have >= need.min ? "够训练用了" : deficitText(have, need.min) + "就够训练"}</Muted>
            <Muted>带伴奏的歌 {slotView(c.list, "song").files} · 清唱 {slotView(c.list, "dry_sing").files} · 说话 {slotView(c.list, "speech").files}</Muted>
          </Panel>
          <Panel style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ fontSize: 12.5, color: INK_2 }}>她的声音</div>
            <div style={{ fontSize: 16, fontWeight: 700, color: INK }}>{readyModels.length ? readyModels.length + " 个可用" : "还没有训练好的"}</div>
            <Muted>{voiceName ? "现在用的是「" + voiceName + "」" : "训练一个之后就能用她的声音唱"}</Muted>
            {all.length > readyModels.length ? <Muted>{all.length - readyModels.length} 个还在训练</Muted> : null}
          </Panel>
          <Panel style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ fontSize: 12.5, color: INK_2 }}>唱好的歌</div>
            <div style={{ fontSize: 16, fontWeight: 700, color: INK }}>{num(s.tracks_total || tracks.length)} 首</div>
            <Muted>做好了的会出现在「让她唱歌」页</Muted>
            <Muted>{Object.keys(s.jobs || {}).length ? Object.keys(s.jobs || {}).length + " 个后台任务" : "现在没有在跑的任务"}</Muted>
          </Panel>
        </Grid>
      </Section>

      <Soft style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", justifyContent: "space-between" }}>
        <Muted>所有处理都在本机完成，不会把音频上传到网上。小提示：「{materialTitle("song")}」这类素材效果最自然。</Muted>
        <Row style={{ gap: 8 }}>
          <Btn onClick={() => onGo("wizard")}>重新查看引导</Btn>
          <Btn onClick={() => onGo("settings")}>看看电脑状态</Btn>
        </Row>
      </Soft>
    </Col>
  )
}
