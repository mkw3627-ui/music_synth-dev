// 为她而唱 · 面板与插件之间的调用桥（2026-11-03 重写）
//
// 为什么不用 props.api.call 直接调所有入口：
//   1. 宿主 hosted-ui 只转发 surface 里声明过的 @ui.action，其余入口直接 403；
//      而 @ui.action 转发时 arguments 会被丢掉（实测 kwargs 只剩 _ctx），
//      需要带参数的入口就没法这样调。
//   2. /runs 桥和旧 static 面板用的是同一个接口（POST /runs {plugin_id, entry_id,
//      args} → 轮询 /runs/{id} → /runs/{id}/export 取 data），实测参数能到，
//      且不做任何窗口操作。这里优先走 /runs，失败再退回 props.api.call。
// 约束：只用查询字符串拼参数，不拼路径（文件名/歌名里常有中文和空格）。
export const ORIGIN = "http://127.0.0.1:48916"
export const PLUGIN_ID = "music_synth"

export type Api = {
  call?: (id: string, args?: any, options?: any) => Promise<any>
  refresh?: () => Promise<any>
} | null

export function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms))
}

/** 业务失败（插件明确返回 ok=false）不应该再走备用通道重试。 */
function businessError(message: string): Error {
  const err: any = Error(message)
  err.final = true
  return err
}

/** 一层层剥开宿主/插件的外壳：{success,data}、{value}、{result}，ok=false 视为失败。 */
export function unpack(response: any): any {
  let data = response
  for (let i = 0; i < 8; i++) {
    if (!data || typeof data !== "object") break
    if (data.success === false) throw businessError(String(data.error || "插件返回失败"))
    if (data.value !== undefined && data.data === undefined) { data = data.value; continue }
    if (data.data !== undefined && data.ok === undefined) { data = data.data; continue }
    if (data.result !== undefined && data.ok === undefined) { data = data.result; continue }
    break
  }
  if (data && data.ok === false) throw businessError(String(data.error || data.message || "操作失败"))
  return data || {}
}

export function readExport(payload: any): any {
  const items = Array.isArray(payload?.items) ? payload.items : []
  for (const item of items) {
    if (!item || typeof item !== "object") continue
    if (item.type === "json" && item.json) return unpack(item.json)
    if (item.type === "text" && typeof item.text === "string") {
      try { return unpack(JSON.parse(item.text)) } catch (e) { /* 继续找下一条 */ }
    }
  }
  return {}
}

export async function viaRuns(entryId: string, args: any, timeoutMs: number): Promise<any> {
  return readExport(await runsPayload(entryId, args, timeoutMs))
}

/** 跑一次入口，返回 /runs/{id}/export 的原始载荷（不剥壳）。 */
async function runsPayload(entryId: string, args: any, timeoutMs: number): Promise<any> {
  const resp = await fetch(ORIGIN + "/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plugin_id: PLUGIN_ID, entry_id: entryId, args: args || {} }),
  })
  if (!resp.ok) throw Error("创建任务失败：HTTP " + resp.status)
  const data = await resp.json()
  const runId = data.run_id || data.id
  if (!runId) throw Error("未拿到 run_id")
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    await sleep(350)
    let rec: any = null
    try {
      const pr = await fetch(ORIGIN + "/runs/" + encodeURIComponent(String(runId)))
      if (pr.ok) rec = await pr.json()
    } catch (e) { continue }
    const status = String((rec && rec.status) || "").toLowerCase()
    if (status === "succeeded") {
      const er = await fetch(ORIGIN + "/runs/" + encodeURIComponent(String(runId)) + "/export")
      if (!er.ok) throw Error("读取结果失败：HTTP " + er.status)
      return await er.json()
    }
    if (status === "failed" || status === "canceled" || status === "timeout") {
      const detail: any = rec && (rec.error || rec.message)
      throw Error(typeof detail === "string" && detail
        ? detail
        : (detail ? JSON.stringify(detail).slice(0, 300) : ("任务" + status)))
    }
  }
  throw Error("任务超时（" + Math.round(timeoutMs / 1000) + " 秒）")
}

/** 不按 ok=false 判失败地剥壳：登录/检测类入口常用 ok=false 表示「还没就绪」。 */
function unwrapLoose(response: any): any {
  let data = response
  for (let i = 0; i < 8; i++) {
    if (!data || typeof data !== "object") break
    if (data.value !== undefined && data.data === undefined) { data = data.value; continue }
    if (data.data !== undefined && data.ok === undefined) { data = data.data; continue }
    if (data.result !== undefined && data.ok === undefined) { data = data.result; continue }
    break
  }
  return data || {}
}

/** 读原始 JSON 项，不做 unpck 的 ok=false 拦截。 */
function readExportLoose(payload: any): any {
  const items = Array.isArray(payload?.items) ? payload.items : []
  for (const item of items) {
    if (!item || typeof item !== "object") continue
    if (item.type === "json" && item.json) return item.json
    if (item.type === "text" && typeof item.text === "string") {
      try { return JSON.parse(item.text) } catch (e) { /* 继续找下一条 */ }
    }
  }
  return {}
}

/**
 * 宽松调用：ok=false 不算失败（由调用方自己看 ready/items）。
 * 只给 ms_setup_check 这类「未就绪也会返回 ok=false」的入口用。
 */
export async function callEntryLoose(api: Api, entryId: string, args: any = {},
                                     timeoutMs = 120000): Promise<any> {
  try {
    return unwrapLoose(readExportLoose(await runsPayload(entryId, args, timeoutMs)))
  } catch (first: any) {
    if (!api || typeof api.call !== "function") throw first
    return unwrapLoose(await api.call(entryId, args, { userInitiated: true }))
  }
}

/** 调一个插件入口。timeoutMs 是等结果的上限（安装、训练收尾这类要给足）。 */
export async function callEntry(api: Api, entryId: string, args: any = {},
                                timeoutMs = 120000): Promise<any> {
  try {
    return await viaRuns(entryId, args, timeoutMs)
  } catch (first: any) {
    // 插件已经明确说“不行”（例如音色与模型不匹配），换通道重试没有意义，
    // 还会把真正的错误原因盖成“备用通道超时”。
    if (first && first.final) throw first
    if (!api || typeof api.call !== "function") throw first
    try {
      const fallback: any = api.call(entryId, args, { userInitiated: true })
      // 宿主 hosted-ui 的备用通道没有超时；插件重载时它可能永远不返回，
      // 面板就会一直卡在「正在生成」。这里强制给它一个上限。
      try { fallback.catch(() => {}) } catch (e) { /* 非 Promise */ }
      const cap = Math.min(Math.max(timeoutMs, 5000), 20000)
      const guarded: any = await Promise.race([
        fallback,
        new Promise((_, reject) => setTimeout(
          () => reject(Error("备用通道超时（" + Math.round(cap / 1000) + " 秒）")), cap)),
      ])
      return unpack(guarded)
    } catch (second: any) {
      throw Error(String((first && first.message) || first) + "；备用通道：" +
        String((second && second.message) || second))
    }
  }
}

/** 让宿主刷新 dashboard 上下文（只读，不触发任何计算）。 */
export async function refreshContext(api: Api): Promise<void> {
  if (!api || typeof api.refresh !== "function") return
  try { await api.refresh() } catch (e) { /* 下一拍再拉 */ }
}

/** 把 File 读成 data URI（base64 入口要的就是这个格式）。 */
export function fileToDataUri(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ""))
    reader.onerror = () => reject(Error("读取 " + file.name + " 失败"))
    reader.readAsDataURL(file)
  })
}

/** Uint8Array → base64。分块拼接，避免 String.fromCharCode 一次塞几十万个参数。 */
function bytesToBase64(bytes: Uint8Array): string {
  const step = 0x8000
  let text = ""
  for (let i = 0; i < bytes.length; i += step) {
    text += String.fromCharCode.apply(null, Array.prototype.slice.call(bytes.subarray(i, i + step)) as any)
  }
  return btoa(text)
}

/**
 * 分片上传一个音频文件。
 *
 * 为什么不用 fileToDataUri：那会把整首歌读成一个 base64 字符串（体积约 1.33 倍），
 * 一次投 50 个文件时内存会被撑爆。这里每次只切 4MB 一片读到内存 → 发出去 → 丢掉，
 * 峰值内存只有「一片」；后端收到就 append 到磁盘，不会在内存里攒整首。
 */
export async function uploadFileChunked(
  api: Api, slot: string, file: File,
  timeoutMs = 300000, onPct?: (pct: number) => void,
): Promise<any> {
  const begin: any = unpack(await callEntry(api, "ms_corpus_upload_begin",
    { slot, filename: file.name }, 30000))
  const uploadId = begin && begin.upload_id
  if (!uploadId) throw Error((begin && begin.error) || "没能开始上传")
  const chunk = Number(begin.chunk_size) > 0 ? Number(begin.chunk_size) : 4 * 1024 * 1024
  const total = Math.max(1, Math.ceil(file.size / chunk))
  let seq = 0
  for (; seq < total; seq++) {
    const start = seq * chunk
    const blob = file.slice(start, Math.min(file.size, start + chunk))
    // 读一片、编码一片；离开这一轮循环后 buf / b64 就可以被回收
    const buf = new Uint8Array(await blob.arrayBuffer())
    const b64 = bytesToBase64(buf)
    const r: any = unpack(await callEntry(api, "ms_corpus_upload_chunk",
      { upload_id: uploadId, seq, data_b64: b64 }, timeoutMs))
    if (r && r.ok === false) throw Error(r.error || "分片上传失败")
    if (onPct) onPct(Math.round(((seq + 1) / total) * 100))
  }
  return unpack(await callEntry(api, "ms_corpus_upload_end", { upload_id: uploadId }, 60000))
}

/**
 * 分片上传一个声音包（.zip）。和素材上传同一套切片逻辑，
 * 只是换成 ms_model_import_* 入口，避免把 200MB+ 的包一次读进内存。
 */
export async function uploadModelChunked(
  api: Api, file: File, timeoutMs = 600000,
  onPct?: (pct: number) => void,
): Promise<any> {
  const begin: any = unpack(await callEntry(api, "ms_model_import_begin",
    { filename: file.name }, 30000))
  const uploadId = begin && begin.upload_id
  if (!uploadId) throw Error((begin && begin.error) || "没能开始导入")
  const chunk = Number(begin.chunk_size) > 0 ? Number(begin.chunk_size) : 4 * 1024 * 1024
  const total = Math.max(1, Math.ceil(file.size / chunk))
  for (let seq = 0; seq < total; seq++) {
    const start = seq * chunk
    const blob = file.slice(start, Math.min(file.size, start + chunk))
    const buf = new Uint8Array(await blob.arrayBuffer())
    const r: any = unpack(await callEntry(api, "ms_model_import_chunk",
      { upload_id: uploadId, seq, data_b64: bytesToBase64(buf) }, timeoutMs))
    if (r && r.ok === false) throw Error(r.error || "分片上传失败")
    if (onPct) onPct(Math.round(((seq + 1) / total) * 100))
  }
  return unpack(await callEntry(api, "ms_model_import_end", { upload_id: uploadId }, 600000))
}

export function dataUriOf(b64: string, mime: string): string {
  const text = String(b64 || "")
  if (text.startsWith("data:")) return text
  return "data:" + (mime || "audio/wav") + ";base64," + text
}

/**
 * 试听结果的播放地址。
 * 小文件后端给 base64（data_b64 / audio_base64），大文件给静态 URL——
 * 宿主的 RPC 结果上限约 4.8MB，整首歌的 base64 一定超，所以必须兼容两种返回。
 */
export function audioSrcOf(r: any, fallbackMime = "audio/wav"): string {
  if (!r) return ""
  if (r.url) return String(r.url)
  const b64 = String(r.data_b64 || r.audio_base64 || "")
  return b64 ? dataUriOf(b64, r.mime || fallbackMime) : ""
}

export function dur(seconds: any): string {
  const n = Number(seconds)
  if (!Number.isFinite(n) || n < 0) return "—"
  const m = Math.floor(n / 60)
  const s = Math.round(n % 60)
  return m > 0 ? m + " 分 " + (s < 10 ? "0" : "") + s + " 秒" : Math.max(1, s) + " 秒"
}

export function eta(seconds: any): string {
  const n = Number(seconds)
  if (!Number.isFinite(n) || n < 0) return "—"
  if (n < 60) return Math.max(1, Math.round(n)) + " 秒"
  if (n < 3600) return Math.round(n / 60) + " 分钟"
  return (n / 3600).toFixed(1) + " 小时"
}

export function short(text: any, fallback = "—", max = 160): string {
  if (text === null || text === undefined || text === "") return fallback
  const s = String(text)
  return s.length > max ? s.slice(0, max) + "…" : s
}

export function mb(bytes: any): string {
  const n = Number(bytes)
  return Number.isFinite(n) && n >= 0 ? (n / 1048576).toFixed(1) + " MB" : "—"
}
