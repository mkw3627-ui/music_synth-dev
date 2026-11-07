// 为她而唱 · 面板外壳（2026-11-04，方案第 1、2、3、4 节）
//
// 左边一条侧栏，每个模块单独成页（打开哪页只挂哪页，来回切不会丢当前页的状态）；
// 窄于 720px 时侧栏变成顶部图标条。第一次打开若环境没装全，先弹全屏安装向导挡住其它页面。
// 接口调用与原来的 panel.tsx 一字不差：只用 ms_state（30 秒刷新一次）；向导用 ms_setup_check /
// ms_setup_install / ms_setup_status。
import { Page, StatusBadge, useEffect, useState } from "@neko/plugin-ui"
import { callEntry } from "./api"
import { FONT, PAGE_SKIN } from "./ui"
import { PageStart } from "./pageStart"
import { PageMaterials } from "./pageMaterials"
import { PageTrain } from "./pageTrain"
import { PageModels } from "./pageModels"
import { PageSing } from "./pageSing"
import { PageCreate } from "./pageCreate"
import { PageSettings } from "./pageSettings"
import { SetupWizard } from "./setupWizard"

/** 开发预览用：?wizard=1..5 直接打开引导第几页；宿主里没有这个参数，恒为 0。 */
function wizardStartAt(): number {
  try {
    const q = String((window as any).location && (window as any).location.search || "")
    const m = q.match(/[?&]wizard=([a-z0-9]+)/)
    if (!m) return 0
    const v = m[1]
    if (v === "1" || v === "2" || v === "3" || v === "4" || v === "5") return Number(v)
    if (v === "fail" || v === "install") return 4
    return 0
  } catch (e) { return 0 }
}

/** 新手引导是否已经完整看过（完成或主动跳过）。存在本机 localStorage 里。 */
const ONBOARDING_KEY = "ms_onboarding_v2_done"
function onboarded(): boolean {
  try { return window.localStorage.getItem(ONBOARDING_KEY) === "1" } catch (e) { return true }
}
function markOnboarded(): void {
  try { window.localStorage.setItem(ONBOARDING_KEY, "1") } catch (e) { /* 隐私模式下忽略 */ }
}

const NAV = [
  { key: "start", label: "开始", icon: "home" },
  { key: "materials", label: "准备素材", icon: "music" },
  { key: "train", label: "训练声音", icon: "train" },
  { key: "models", label: "我的声音", icon: "voice" },
  { key: "sing", label: "让她唱歌", icon: "play" },
  { key: "create", label: "原创歌曲", icon: "spark" },
  { key: "settings", label: "设置与环境", icon: "gear" },
]

/** 内置小图标（不引第三方图标库）：统一 20×20 描边，颜色跟着 currentColor。 */
function Icon(props: { name: string; size?: number }) {
  const s = props.size || 20
  const common = { width: s, height: s, viewBox: "0 0 20 20", fill: "none", stroke: "currentColor", strokeWidth: 1.6, strokeLinecap: "round", strokeLinejoin: "round" } as any
  if (props.name === "home") return <svg {...common}><path d="M3 8.6 10 3l7 5.6" /><path d="M5 9.4V16h10V9.4" /></svg>
  if (props.name === "music") return <svg {...common}><circle cx="6" cy="14.5" r="2.2" /><circle cx="14.5" cy="12.5" r="2.2" /><path d="M8.2 14.5V6.2l8.5-1.6v7.9" /></svg>
  if (props.name === "train") return <svg {...common}><path d="M4 15.5V9" /><path d="M10 15.5V4.5" /><path d="M16 15.5v-4.6" /><path d="M2.6 17h14.8" /></svg>
  if (props.name === "voice") return <svg {...common}><rect x="7.6" y="2.6" width="4.8" height="9" rx="2.4" /><path d="M4.8 9.6a5.2 5.2 0 0 0 10.4 0" /><path d="M10 14.8V17.4" /></svg>
  if (props.name === "play") return <svg {...common}><circle cx="10" cy="10" r="7.2" /><path d="M8.4 7.4l4.4 2.6-4.4 2.6z" /></svg>
  if (props.name === "spark") return <svg {...common}><path d="M10 2.8l1.8 5 5 1.8-5 1.8-1.8 5-1.8-5-5-1.8 5-1.8z" /></svg>
  return <svg {...common}><circle cx="10" cy="10" r="2.6" /><path d="M3.2 10a6.8 6.8 0 0 1 1.3-4M16.8 10a6.8 6.8 0 0 1-1.3 4M5.4 15.4a6.8 6.8 0 0 1-1.1-1.4M14.6 4.6a6.8 6.8 0 0 0-1.1-1.4" /></svg>
}

export default function Panel(props: any) {
  const api: any = (props as any).api
  const [page, setPage] = useState<string>(() => {
    // 开发预览用 ?page=settings 之类直接落在某一页；宿主里没有这个参数，照常从「开始」进
    // 宿主里地址不带查询串（或带别的参数）时一律回落到「开始」，不会白屏
    try {
      const q = String((window as any).location && (window as any).location.search || "")
      const m = q.match(/[?&]page=([a-z]+)/)
      const hit = m ? m[1] : ""
      return NAV.some((n: any) => n.key === hit) ? hit : "start"
    } catch (e) { return "start" }
  })
  const [snap, setSnap] = useState<any>(null)
  const [error, setError] = useState<string>("")
  const [wizard, setWizard] = useState<boolean>(false)

  const refresh = async () => {
    try {
      const r: any = await callEntry(api, "ms_state", {}, 30000)
      setSnap(r)
      setError("")
    } catch (e: any) {
      setError(String((e && e.message) || e))
    }
  }
  useEffect(() => { refresh(); const id = setInterval(refresh, 30000); return () => clearInterval(id) }, [])

  const hostState = (props as any).state
  const view = snap || (hostState && typeof hostState === "object" ? hostState : null)

  // 第一次打开：先走五页新手引导（体验 → 功能介绍 → 用法 → 装环境 → 完成）。
  //   · 没看过引导（本机没有记录）→ 从第 1 页开始
  //   · 已经看过、但环境缺必需项 → 直接落在第 4 页，并把其它页面挡住
  //   · 预览 ?wizard=… → 直接打开指定页
  const setupReady = !view || !view.setup || view.setup.completed === true
  useEffect(() => {
    if (wizardStartAt() || !setupReady || !onboarded()) setWizard(true)
  }, [setupReady])
  const wizardStart = (() => {
    const forced = wizardStartAt()
    if (forced) return forced
    if (!setupReady && onboarded()) return 4
    return undefined
  })()
  const closeWizard = () => { markOnboarded(); setWizard(false); refresh() }

  const go = (k: string) => {
    if (k === "wizard") { setWizard(true); return }
    setPage(k)
    window.scrollTo({ top: 0, behavior: "smooth" })
  }

  return (
    <div>
      <div className="ms-shell" style={{ ...PAGE_SKIN, fontFamily: FONT, color: "#282532", minHeight: 560, margin: -8, padding: 8, borderRadius: 20 }}>
        <style>{`
          .ms-shell * { box-sizing: border-box; }
          .ms-layout { display: flex; gap: 22px; align-items: flex-start; }
          .ms-side { flex: 0 0 214px; position: sticky; top: 8px; border-radius: 28px; }
          .ms-sidebar { display: flex; flex-direction: column; gap: 5px; padding: 14px 12px;
            background: linear-gradient(165deg, #ffffff 0%, #fffafc 58%, #fff3f8 100%);
            backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
            border-radius: 28px; border: 1px solid rgba(255,255,255,0.98);
            box-shadow: 0 12px 32px rgba(142,159,187,0.16), 0 2px 6px rgba(224,45,118,0.05);
            overflow: hidden; }
          .ms-brand { display: flex; align-items: center; gap: 10px; padding: 4px 6px 12px; }
          .ms-brand-mark { width: 34px; height: 34px; border-radius: 999px; display: grid; place-items: center;
            background: linear-gradient(150deg,#ffe5ef,#f6cfe4); color: #e02d76; font-size: 17px;
            box-shadow: 0 4px 12px rgba(224,45,118,0.14); }
          .ms-navitem { display: flex; align-items: center; gap: 10px; padding: 10px 14px; border: 0; cursor: pointer;
            border-radius: 999px; background: transparent; color: #74717e; font-size: 13px; font-family: inherit; text-align: left;
            transition: background-color .18s ease, color .18s ease, transform .18s ease; }
          .ms-navitem:hover { background: rgba(255,229,239,0.5); color: #9c204e; }
          .ms-navitem[aria-current="page"] { background: #ffe5ef; color: #e02d76; font-weight: 700; box-shadow: 0 6px 16px rgba(224,45,118,0.14); }
          .ms-main { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; gap: 24px; }
          .ms-side-foot { padding: 10px 8px 2px; }
          @media (max-width: 720px) {
            .ms-layout { flex-direction: column; gap: 14px; }
            .ms-side { flex: 1 1 auto; position: static; width: 100%; }
            .ms-sidebar { flex-direction: row; overflow-x: auto; padding: 8px 10px; gap: 6px; border-radius: 22px; }
            .ms-brand { display: none; }
            .ms-navitem { flex-direction: column; gap: 3px; font-size: 10.5px; padding: 7px 10px; border-radius: 18px; white-space: nowrap; }
          }
        `}</style>

        <div className="ms-layout">
          <div className="ms-side">
            <div className="ms-sidebar">
              <div className="ms-brand">
                <span className="ms-brand-mark">♪</span>
                <span>
                  <span style={{ display: "block", fontSize: 14, fontWeight: 700, color: "#282532" }}>为她而唱</span>
                  <span style={{ display: "block", fontSize: 10.5, color: "#9a97a4" }}>本地唱歌 · 不联网</span>
                </span>
              </div>
              {NAV.map((n: any) => (
                <button key={n.key} type="button" className="ms-navitem" aria-current={page === n.key ? "page" : undefined}
                        onClick={() => go(n.key)}>
                  <Icon name={n.icon} />
                  <span>{n.label}</span>
                </button>
              ))}
              <div className="ms-side-foot">
                {error ? <StatusBadge tone="error">状态读取失败</StatusBadge>
                  : !setupReady ? <StatusBadge tone="warning">环境未装全</StatusBadge>
                    : <span style={{ fontSize: 10.5, color: "#9a97a4" }}>全部在本机完成</span>}
              </div>
            </div>
          </div>

          <div className="ms-main">
            {page === "start" ? <PageStart api={api} snap={view} onGo={go} /> : null}
            {page === "materials" ? <PageMaterials api={api} onGo={go} /> : null}
            {page === "train" ? <PageTrain api={api} snap={view} onRefresh={refresh} onGo={go} /> : null}
            {page === "models" ? <PageModels api={api} snap={view} onRefresh={refresh} onGo={go} /> : null}
            {page === "sing" ? <PageSing api={api} snap={view} onRefresh={refresh} onGo={go} /> : null}
            {page === "create" ? <PageCreate api={api} snap={view} onRefresh={refresh} onGo={go} /> : null}
            {page === "settings" ? <PageSettings api={api} snap={view} onRefresh={refresh} onOpenWizard={() => setWizard(true)} /> : null}
          </div>
        </div>
      </div>

      {wizard ? <SetupWizard api={api} startAt={wizardStart} onDone={closeWizard} onSkip={closeWizard} /> : null}
    </div>
  )
}
