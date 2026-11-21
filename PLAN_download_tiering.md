# 为她所唱（music_synth）· 下载依赖分级 + 下载体验改造 计划书

- 规划 / 测试 / 验收：@claude-opus-5-5
- 执行：EXEC-PRO = @deepseek-v4-pro（后端、并发、断点续传等难点）；EXEC-FLASH = @deepseek-v4-1-flash（入口、界面、测试夹具等量大但边界清楚的活）
- 日期：2026-11-21；基线版本：plugin.toml `version = "0.3.6"`，dev 仓库 HEAD `79f7cea`
- 约束：不破坏已有安装；`plugin.toml` 已有键只扩展不改名；Windows；全本地（音频不上传，下载只拉公开模型/程序）

---

## A. 现状梳理

### A.1 关键文件（均已核实存在）

| 文件 | 作用 | 与本次相关的要点 |
|---|---|---|
| `plugin.toml` | 插件清单 + `[music_synth]` 默认配置 | `download_source = "huggingface"`（注释写 huggingface/mirror）、`auto_setup = true`、`compute_root/envs_dir/models_dir/work_dir/sep_model_dir` |
| `setup.py`（2353 行） | 首次安装与下载的全部后端 | 见 A.2 |
| `compat.py` | 硬件适配 + 部署清单 + 测速 | `deploy_manifest()`（L272）已按功能分组：基础环境 / 翻唱 / 原创 / 歌词识别 / 舞台；`speed_targets()`（L336）+ `speed_test()`（L390）每源 Range 拉 4MB 测速 |
| `__init__.py`（约 8.9 千行） | 插件入口 | `ms_setup_check`(L990)、`ms_setup_install`(L1028)、`ms_setup_status`(L1179)+`_setup_progress`、`ms_setup_redownload`(L1150)、`ms_dl_diag`(L981)、`ms_deploy_overview`(L940)、`ms_base_download`(L2115)、`ms_ace_install`(L8854)、`ms_ace_download`(L7918)、`_ace_install_model_sync`(L8911)、`ms_soulx_install`(L7541)；作业表 `_job_begin/_job_update`(L601) 只存内存、只留最近 8 条 |
| `workers/ace_download_worker.py` | ACE DiT 单模型下载 | 调 `vendor/acestep/model_downloader.download_submodel`（`snapshot_download`，HF/ModelScope），**不报字节进度**，插件靠扫目录体积估算 |
| `vendor/acestep/model_downloader.py` | ACE 上游下载器 | `MAIN_MODEL_REPO = "ACE-Step/Ace-Step1.5"`，`MAIN_MODEL_COMPONENTS = [acestep-v15-turbo, vae, Qwen3-Embedding-0.6B]`，`SUBMODEL_REGISTRY`（xl-turbo/sft/base 等） |
| `workers/soulx_install_worker.py` | SoulX-Singer 安装 | ModelScope `snapshot_download("Soul-AILab/SoulX-Singer")` + `SoulX-Singer-Preprocess/rmvpe/*`，写 `install_status.json`（原子替换）——**这是现有唯一"状态落盘"的下载** |
| `runtime.py` | 路径解析 / worker 契约 | `resolve_paths()`(L432)、`save_config()`、`run_worker()`(L850)：`python -I script job.json result.json` |
| `static/index.html`（7254 行） | **面板真实产物**，plugin.toml 的 panel entry | `NAV`(L6941) 8 个页面：start/materials/train/models/sing/create/stage/settings；`Onboarding`(L5661) 5 步向导，第 4 步是安装清单 + `DeployOverview`(含「测下载速度」L5615)；App 壳 L7000–7111 |
| `ui/*.tsx` | 旧的前端源码 | **已落后于产物**：没有 `PageStage`、`DeployOverview`；`html/panel.html` 也没有。→ 本次前端改动**直接改 `static/index.html`**，不要从 `ui/` 重建（会把舞台页、测速页整体回退） |
| `tools/build_package.py` | 打包 `dist/music_synth.neko-plugin` | 排除 `tools/ host/ _trash/` 等 |
| `tests/test_smoke.py` | 唯一现有测试 | 只检查清单 |
| 生效安装目录 | `D:\ruanjian\N.E.K.O\.neko-plugin-installations\plugins\music_synth` | 当前与 dev 的 `__init__.py/setup.py/static/index.html` 字节一致；部署 = 拷到这里 |

### A.2 现有下载/安装链路

1. **向导**：第 4 步列出全部项（`check_dependencies` 的 items）→ 「一键安装」= `ms_setup_install({path})` → 插件进程内线程跑 `setup.install(paths, items=None)`，**一次性装全部缺失项**（环境 + 程序 + 模型，含 ACE 6GB），装完才能离开向导（`setupReady` 为假时 App 会强制打开向导到第 4 步，L7022）。
2. **安装顺序**（`setup.install` L2070）：base_python → ddsp_repo → venv（synth/sep 共用 `envs\ddsp`）→ torch → DDSP 依赖 → capture_native → ffmpeg(winget) → audio-separator/onnxruntime → `MODEL_ITEMS` 逐个 → `EXTRA_INSTALLERS`（`ace_deps`、`ace_model`）。
3. **文件下载引擎**：`_download_file`(L668) + `_download_one`(L753)
   - `.part` + HTTP Range 续传、416 视为已完成、Content-Length 对账、`os.replace` 原子落盘、`_write_marker` 写大小标记；
   - `_rank_sources` 对每个 (源 × 直连/代理) 并发 Range 1MB 测速（`PROBE_BYTES=1MB`、`PROBE_BUDGET_S=6`、缓存 `PROBE_TTL_S=600`）；
   - 中途换源：`SLOW_WINDOW_S=15`、`SLOW_RATIO=0.35`、`SLOW_FLOOR_BPS=200KB/s`；「20 秒不到 512KB」判死；不支持 Range 的 host 记入 `_NO_RANGE_HOSTS`；断网等待预算 `NET_WAIT_BUDGET_S=15min`；
   - **没有内容校验**（无 sha256），换源续传时**不验证新源字节与已下部分一致**；
   - 进度只以字符串 `"label：12/345 MB"` 回调，由 `_setup_progress` 用正则反解。
4. **源清单**（`setup.py` 顶部）：`GH_MIRRORS`（gh-proxy.com / ghfast.top / hk.gh-proxy.com / ghproxy.net）、`HF_MIRROR_HOSTS`（hf-mirror.com / hf-mirror.net）+ huggingface.co、`PYPI_MIRRORS`（清华 / 阿里云）、PyTorch 官方 cu128/cpu、`PYTHON_STANDALONE_MIRROR`（NJU）。
5. **持久性**：下载线程挂在插件进程里（`self._thread(...)`，daemon 线程）。**关面板不会断**（面板只是轮询），但**插件重启 / N.E.K.O 退出即中断**，且作业状态只在内存里，重启后进度条从 0 计、不会自动接着下；ACE / SoulX / 底模三条下载线路各有一套作业与进度逻辑。
6. **未安装功能的页面**：没有统一门控。各页自己查状态（如原创页查 `ms_ace_status`、`ms_soulx_status`），表现不一。

### A.3 真实下载物清单（本计划的源表以此为准）

| key（现有） | 内容 | 体积 | 现有来源 |
|---|---|---|---|
| `base_python` | python-build-standalone 3.10/3.11 便携版 | 0.03GB | NJU 镜像 → GitHub API/固定 URL |
| `ffmpeg` | Gyan.FFmpeg | ~0.08GB | winget |
| `synth_env`/`sep_env` | venv `envs\ddsp`（共用） | — | 本地创建 |
| `torch` | torch+torchaudio cu128（或 cpu） | 2.9GB / 0.25GB | download.pytorch.org |
| `ddsp_deps` | `DDSP_PACKAGES` | 0.6GB | PyPI 清华/阿里云 |
| `audio_separator` | `SEP_PACKAGES` + onnxruntime(-gpu) | 0.3GB | PyPI |
| `capture_native` | proc-tap、winrt | 0.02GB | PyPI |
| `ace_deps` | diffusers/transformers/peft… → `acestep\pylibs` | 0.3GB | PyPI |
| `ddsp_repo` | DDSP-SVC zip（commit b9d4e6a） | 0.03GB | codeload.github.com + GH 镜像 |
| `ddsp_contentvec` | `lengyue233/content-vec-best/pytorch_model.bin` | 0.40GB | hf-mirror.com/.net、huggingface.co |
| `ddsp_vocoder` | openvpi pc_nsf_hifigan zip | 0.05GB | GitHub Release + 镜像 |
| `ddsp_rmvpe` | yxlllc/RMVPE 230917 zip | 0.06GB | GitHub Release + 镜像 |
| `ddsp_reflow_base` | SVCFusion `ddsp6.3_6x512_6x1024.sf_dlc` → model_0.pt | 0.21GB | HF 三源 |
| `ddsp_base_low` / `ddsp_base_high`（可选） | 6x512 / 10x2048 `.sf_dlc` | 0.09 / 0.97GB | HF 三源 |
| `sep_karaoke` | Mel-Band RoFormer Karaoke ckpt + yaml | 1.75GB | nomadkaraoke / TRvlvr GitHub Release |
| `ace_model` | `ACE-Step/Ace-Step1.5` 的 turbo + vae + Qwen3-Embedding-0.6B | ~6GB | ModelScope → HF(hf-mirror.net) |
| `ace:<name>` | 其它 DiT（xl-turbo 18.6GB 等，`ACE_MODEL_CATALOG`） | 4.6–18.6GB | 同上 |
| SoulX | `Soul-AILab/SoulX-Singer`（svc 2.79GB + dit 2.82GB）+ Preprocess rmvpe 0.18GB；代码 GitHub zip | ~5.8GB | ModelScope（代码 GitHub 直连） |
| 舞台资源 | 字体 / pixi / rough / doodles | 0 | 随包发布，无下载 |
| 歌词识别 | faster-whisper | 0 | 借用「为她所听」，本插件不下载 |

---

## B. 目标架构

### B.1 功能 × 层级模型

层级定义：
- **T0 基础环境**：Python、venv、torch、pip 依赖、ffmpeg。必须前台装完才能进主页（装环境期间不能跑任何功能）。
- **T1 程序**：代码仓库 / 小工具（< 50MB）。和 T0 一起前台装（体积小，放后台反而让功能页多一个等待态）。
- **T2 模型**：大权重。**后台静默下载**，用户装完 T0+T1 即可进主页。

功能（向导第一步让用户勾选；括号内为功能 id）：

| 功能 | T0 | T1 | T2 | 对应页面（门控） |
|---|---|---|---|---|
| 翻唱 `cover`（默认勾选，其他功能的前置） | base_python, synth_env, torch, ddsp_deps, ffmpeg, sep_env/audio_separator | ddsp_repo | ddsp_contentvec, ddsp_vocoder, ddsp_rmvpe, sep_karaoke | `sing`（让她唱歌） |
| 音色训练 `train`（依赖 cover） | 同 cover | 同 cover | + ddsp_reflow_base；可选 ddsp_base_low / ddsp_base_high（默认不勾） | `materials`、`train` |
| 原创歌曲 `create`（依赖 cover 的 T0；需 NVIDIA） | + ace_deps | — | ace_model（turbo+vae+Qwen3）；可选 `ace:<其它模型>` | `create` |
| SoulX 唱法 `soulx`（可选，依赖 create） | （共用 envs\ddsp，worker 自检） | SoulX 代码 zip | SoulX svc/dit/rmvpe | `create` 页内"后处理引擎=SoulX"区块（局部门控） |
| 听歌采集 `capture`（默认勾选） | capture_native | — | — | `sing` 页"采集"区块（局部门控） |
| 唱歌舞台 `stage` | — | — | —（随包） | `stage` 不门控；宿主加载器未装时只显示提示条，不模糊 |

不门控页面：`start`、`models`、`settings`。
功能就绪判定（`feature_ready(f)`）= 该功能所有**必需** T0/T1/T2 项 `status == ready`；可选项不影响。
依赖关系：选 `train`/`create` 自动带上 `cover` 的 T0/T1；`soulx` 自动带 `create`。
没有 NVIDIA 显卡时：`create`/`soulx` 在选择页置灰并写明原因（沿用 `_ace_setup_items` 的 gpu_ok 判定），`train` 显示"不建议"但可选。

### B.2 后台下载服务（下载不随页面/插件中断）

```
面板(static/index.html)  ──轮询 ms_dl_status(只读state.json)──┐
       │ ms_dl_enqueue / ms_dl_control                         │
       ▼                                                       │
插件进程 __init__.py ── download_manager.py ──spawn/attach──► workers/download_daemon.py（独立进程, DETACHED）
                                │  写 cmd/*.json                 │ 读队列、下载、校验、安装后处理
                                └──────────── 读 ◄── <compute_root>\_downloads\state.json（原子替换）
```

新增模块（均为**纯标准库、无相对导入**，便于 pytest 直接 import；`download_manager.py` 例外可 `from . import runtime`）：

| 文件 | 职责 |
|---|---|
| `download_catalog.py` | 下载物目录：artifact id、feature、tier、required、files[{rel, size, sha256, sources[]}]、post 动作（`none`/`unzip:<mode>`/`dlc_convert`/`hf_layout`）、目标路径解析函数（输入 `paths` dict） |
| `download_engine.py` | 单文件下载引擎：测速排序、Range 续传、尾部重叠校验、换源、sha256、磁盘满、限速、进度回调（结构化 dict，不再是字符串） |
| `workers/download_daemon.py` | 后台守护进程：单实例锁、心跳、队列调度（并发 2）、命令处理、状态落盘、后处理、空队列 60 秒后自退 |
| `download_manager.py` | 插件侧门面：`ensure_daemon()`、`enqueue()`、`control()`、`read_state()`；插件启动时若 state 里有未完成任务且 `download_auto_resume=true` 则拉起守护进程 |

**守护进程启动**：`subprocess.Popen([sys.executable, "-I", "-X", "utf8", <plugin>/workers/download_daemon.py, <state_dir>, <plugin_dir>], creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW, close_fds=True, stdin/out/err=DEVNULL→log文件)`。用宿主 Python（`sys.executable`）——安装 T0 之前也一定存在；引擎只用标准库。需要 torch 的后处理（`.sf_dlc → model_0.pt`）由守护进程以子进程调用 `paths["synth_python"]` 执行现有 `_install_dlc_model` 的转换逻辑（抽成 `workers/dlc_convert_worker.py`）；synth 环境未就绪时该项停在 `pending_post`，T0 装好后自动续做。

**单实例 + 心跳**：`_downloads\daemon.lock` 记 `{pid, started_at, heartbeat_at}`，每 2 秒更新；`ensure_daemon()` 判定：lock 不存在 / pid 不存活 / heartbeat 超过 10 秒 → 视为失效，重新拉起。守护进程启动时用 `msvcrt.locking` 独占锁文件，抢锁失败立即退出（防双开抢同一个 `.part`）。

**退出策略**：配置 `download_keep_after_exit`（默认 `true`）——N.E.K.O 关闭后继续下载，下完自退；为 `false` 时插件 `shutdown` 生命周期向守护进程发 `pause_all` 并让其退出，下次启动自动续。

**状态存储** `<compute_root>\_downloads\`：
```
state.json            # 守护进程唯一写者；tmp + os.replace；每 1 秒最多写一次
cmd\<ts>-<rand>.json  # 插件写，守护进程读后删除（pause/resume/cancel/retry/prioritize/enqueue）
parts\<artifact>\<file>.part
parts\<artifact>\<file>.part.json   # {url, source_id, total, etag, last_modified, bytes, sha256_partial_state?}
daemon.lock / daemon.log（滚动 5MB×2）
```
`state.json` 结构：
```json
{"version":1,"daemon":{"pid":123,"heartbeat_at":1732170000.1,"running":true},
 "totals":{"bytes_done":0,"bytes_total":0,"speed_bps":0,"eta_s":0,"active":1,"queued":3,"failed":0},
 "items":{"ddsp_contentvec":{"feature":"cover","tier":"T2","label":"…","state":"downloading",
   "bytes_done":0,"bytes_total":0,"speed_bps":0,"source":"hf-mirror.net","route":"direct",
   "switches":0,"error":"","updated_at":0,"files":[{"rel":"pytorch_model.bin","state":"downloading","bytes_done":0,"bytes_total":0}]}}}
```
item `state` 枚举：`queued | probing | downloading | verifying | post | pending_post | done | paused | paused_disk_full | failed | canceled`。

**UI 只是观察者**：面板任何时候关闭/切页都不发任何"停止"指令；只有用户点「暂停/取消」才写 cmd。

### B.3 下载源管理与自动换源

**源表**（写进 `download_catalog.py`；"候选"项必须在步骤 S1 实测通过——返回真实字节且与参考 sha256 一致——才能入表，不通过就删掉并在 DEV_NOTES 记录）：

| 下载物 | 源（排序前的默认优先级） |
|---|---|
| HF 文件（contentvec、SVCFusion .sf_dlc） | hf-mirror.net、hf-mirror.com、huggingface.co；候选：ModelScope 同名仓库（S1 查有无） |
| GitHub Release / codeload（vocoder、rmvpe、sep_karaoke、DDSP zip、SoulX 代码 zip、便携 Python） | gh-proxy.com、ghfast.top、hk.gh-proxy.com、ghproxy.net、github.com 直连；便携 Python 额外 NJU 镜像 |
| ACE（Ace-Step1.5 组件与各 DiT） | modelscope.cn（`/models/{repo}/resolve/master/{path}`）、hf-mirror.net、hf-mirror.com、huggingface.co |
| SoulX 权重 | modelscope.cn、hf-mirror.net、huggingface.co（候选，S1 确认 HF 同名仓库存在） |
| PyPI（T0） | 清华、阿里云、候选：USTC `mirrors.ustc.edu.cn/pypi/simple`、腾讯云 `mirrors.cloud.tencent.com/pypi/simple`、pypi.org |
| PyTorch（T0） | download.pytorch.org/whl/cu128（或 cpu）、阿里云 `mirrors.aliyun.com/pytorch-wheels/cu128/`（`-f` find-links 方式）；候选：NJU/SJTU pytorch 镜像 |
| ffmpeg（T0） | winget Gyan.FFmpeg；失败兜底：gyan.dev release-essentials zip、GitHub BtbN 构建（走 GH 镜像）→ 解压到 `<compute_root>\tools\ffmpeg\bin`，分离 worker 启动时前插 PATH |

**每个文件都记 `size` + `sha256`**。来源：HF/ModelScope 的 LFS 元数据（HF `GET /api/models/{repo}/tree/main?recursive=1` 的 `lfs.oid` 即 sha256；hf-mirror 同样支持）；GitHub Release 资产 API 的 `digest`（`sha256:...`）；都拿不到时由 S1 脚本从最快源完整下载一次计算并固化。ACE / SoulX 的仓库文件很多，**不固化在代码里**：守护进程入队时调 HF tree API / ModelScope `GET /api/v1/models/{repo}/repo/files?Recursive=true` 列出文件（含 size/sha256），失败再用 catalog 内置的最小文件清单（至少保证 turbo+vae+Qwen3 的分片名与 size）。

**测速**（复用并统一 `setup._probe_speed` 与 `compat._probe_one`）：每个 (源 × 线路[直连/代理]) Range 拉 1MB、6 秒封顶、并发 ≤ 8，返回 `text/html` 记为不通（镜像失效页）；缓存 10 分钟。

**换源策略（默认值，均可在 config 覆盖）**：

| 参数（新增 config 键） | 默认 | 含义 |
|---|---|---|
| `download_slow_window_s` | 15 | 速度采样窗口 |
| `download_slow_bps` | 204800（200KB/s） | 绝对下限：**连续 2 个窗口**低于它 → 重测其他源，有更快的就换 |
| `download_slow_ratio` | 0.35 | 当前速度 < 最快备选源测速 × 0.35 且低于 `download_slow_bps` → 换（沿用现值） |
| `download_better_ratio` | 3.0 | 当前 < 1MB/s 且备选源测速 ≥ 当前 × 3 → 换（解决"200KB/s 以上但仍很慢"） |
| （常量）卡死判定 | 20 秒内 < 512KB | 直接判该源失败（沿用现值） |
| `download_max_switches` | 8 | 单文件换源上限，到达后固定用历史最快源直到完成 |
| （常量）降权冷却 | 600 秒 | 被换掉的源 10 分钟内排序时速度按 ×0.5 计 |
| `download_max_parallel` | 2 | 同时下载的文件数 |

**换源续传规则**：
1. 新源必须支持 Range（探测时 206）且报告的 `total` 与已记录 `total` 相等，否则从 0 开始（删 `.part`）。
2. **尾部重叠校验**：续传前从新源取 `[pos-64KB, pos)` 与本地 `.part` 末尾 64KB 比较，一致才从 `pos` 续；不一致 → 该文件从 0 重下，并把新源标记 `mismatch` 冷却。
3. 下载完成 → sha256 校验（流式，边下边算的哈希在续传时需重算已有部分；实现上续传时先对 `.part` 现有内容做一次增量哈希）。不一致 → 删 `.part`、该源标记 `corrupt`、换下一个源重下一次；第二次仍不一致 → item `failed`，错误"校验失败（文件已损坏），已从两个源重试"。
4. 通过后 `os.replace(.part, 目标)`，写现有 `_write_marker` 兼容标记，再做 post 动作。

**磁盘**：入队时预检 `free ≥ 剩余字节 × 1.1 + 512MB`，不足则 `paused_disk_full`；写入时捕获 `OSError`（`errno == 28` 或 `winerror == 112`）→ 保留 `.part`、`paused_disk_full`；守护进程每 30 秒复查剩余空间，够了自动恢复。

**下载源偏好**：`download_source` 保留原值语义并**扩展一个值 `auto`**：`auto`=纯测速；`mirror`=国内源优先（测速结果相近（±30%）时国内源排前）；`huggingface`=官方源优先（同理）。`plugin.toml` 默认值改为 `"auto"` 仅影响**全新安装**（`data\config.json` 已有显式值的老用户不变）；`ms_setup_install` 的 `enum` 扩为 `["auto","huggingface","mirror"]`。

### B.4 UI 组件（全部写在 `static/index.html` 的 shell 脚本里，跟随现有 `jsx/jsxs`、`Panel/Btn/Muted` 写法）

1. **向导第 1.5 步「选功能」`FeaturePicker`**：在现有第 4 步（选安装目录/安装）之前插入（向导总步数 5→6，`startAt` 兼容：旧值 4 映射到新安装步）。功能卡片多选：显示每个功能的 T0+T1 前台体积、T2 后台体积、硬件可用性。选择写入 config `download_features`。
2. **向导安装步**：只列所选功能的 T0/T1（T2 分组折叠显示"进入主页后自动后台下载"）。T0/T1 装完 → 按钮「进入主页」可点（不再要求 `report.ready`）；点击后自动 `ms_dl_enqueue({features})` 入队 T2，然后关闭向导。App L7022 的强制开向导条件改为"T0/T1 未就绪"，而非"全部就绪"。
3. **常驻下载条 `DownloadDock`**：挂在 `.ms-shell` 底部（`position: sticky; bottom: 0`，高 44px，z-index 高于页面、低于向导/对话框），所有页面可见，有任务未完成就一直显示：
   - 收起态："后台下载中 2/6 · 1.2/6.4 GB · 3.4 MB/s · hf-mirror.net · 约 25 分钟" + 总进度条 + [展开] [全部暂停/继续]
   - 展开态：抽屉列出每项（功能、状态、进度、速度、当前源、换源次数、错误 + [重试][暂停][取消][换源]）
   - 全部完成：显示"下载完成"8 秒后收起；有失败项时保持显示红色状态直到用户处理或关闭。
   - 轮询：有活动任务时 1.5 秒，空闲 10 秒；`ms_dl_status` 只读 `state.json`。
4. **未安装门控 `FeatureGate`**：包住门控页面内容。`feature_ready == false` 时：内容容器 `filter: blur(8px); pointer-events: none; user-select: none`（**不用 `backdrop-filter`**——侧栏曾因此耗显卡被去掉），上盖半透明层，居中卡片"「原创歌曲」还没安装 · 需要下载约 6.3GB（环境 0.3GB + 模型 6GB）· [下载]"。
   - 点[下载] → 同一个卡片原地切换为**下载卡片**（在模糊层之上，不跳页）：按 T0→T1→T2 分段进度、当前源、速度、剩余时间、[暂停][后台下载（关闭卡片，任务继续）]；
   - 功能 T0 尚缺时（例如只装了翻唱又来点原创）：T0/T1 走 `ms_setup_install({features:[f], tiers:["T0","T1"]})`（插件内线程，pip 有缓存可重入），完成后自动入队 T2；卡片统一显示两段进度。
   - 下载中切走页面再回来 → 卡片从 `state.json` 恢复显示；全部完成后模糊层淡出（300ms），页面可用。
   - 已在下载的功能页：门控卡片直接显示下载卡片（不显示"下载"按钮）。
5. **设置与环境页**：扩展现有 `DeployOverview` 的「测下载速度」——按"下载物"维度测（每个下载物的每个源），给出将选用的源；新增"下载源偏好（auto/国内优先/官方优先）"与"关闭 N.E.K.O 后继续下载"开关。

---

## C. 分步骤实施计划

> 通用规则（所有步骤）：
> - 每步开工前在 dev 仓库 `git status` 干净或已提交；每步一个提交，提交信息写明 S 编号。
> - 不改动的旧入口签名保持兼容（`ms_setup_install` 不传新参数时行为与现在一致——测试会验证）。
> - Python 新模块用 ruff check（不跑 format，见 CI 约定）。
> - 前端只改 `static/index.html`；改前备份为 `static/index.html.bak_dltier_<日期>`（`tools/build_package.py` L70 已排除文件名含 `.bak` 的文件，不会进包）。
> - 读写源码一律 UTF-8；PowerShell 下校验中文内容用 `python -c` 而不是 `Get-Content`。

### S1 下载物目录 + 源实测（EXEC-PRO）
- 范围：新建 `download_catalog.py`；新建 `tools/dl_verify_sources.py`（实测 B.3 源表每个 (artifact, source)：可达、206、Content-Length、首 1MB 与参考一致、取 sha256）；产出 `tools/dl_sources_report.json` 并把结论（size/sha256、剔除的候选源）固化进 catalog。
- catalog 内容：A.3 全部 T2 项 + `ddsp_repo`/SoulX 代码 zip/便携 Python（T1/T0 单文件类）；每项 `feature/tier/required/label/files/post/target(paths)->Path`；ACE/SoulX 为 `repo` 型（`repo_id`、`components`/`allow_patterns`、内置最小文件清单）。
- 提供函数：`artifacts_for(features, tiers) -> list[id]`、`feature_items(feature) -> {T0:[keys],T1:[...],T2:[...]}`、`FEATURES` 元数据（label、depends、页面映射、gpu_required）。
- 触及文件：`download_catalog.py`（新）、`tools/dl_verify_sources.py`（新）、`tools/dl_sources_report.json`（新）。
- 依赖：无。
- 完成定义：catalog 覆盖 A.3 所有下载物；每个非 repo 型文件都有 size+sha256；每个 artifact ≥ 2 个通过实测的源（GitHub 类 ≥ 3）；候选源去留有记录。

### S2 测试夹具：本地假镜像（EXEC-FLASH）
- 范围：`tests/dl_harness/fake_mirrors.py`——在 127.0.0.1 起 N 个端口的 HTTP 服务（`http.server` + 线程），每个端口可配置：限速（B/s）、支持/忽略 Range、第 K 字节后断连、返回错误字节（篡改）、返回 HTML 200、5xx、首字节延迟、动态调速（运行中改速度）；提供 pytest fixture `mirrors(n, **cfg)`，以及随机内容文件生成器（固定种子，给出 sha256）。
- 触及文件：`tests/dl_harness/__init__.py`、`tests/dl_harness/fake_mirrors.py`、`tests/conftest.py`（新）。
- 依赖：无（可与 S1 并行）。
- 完成定义：`tests/test_harness_selfcheck.py` 覆盖每种故障模式均可复现。

### S3 下载引擎（EXEC-PRO）
- 范围：新建 `download_engine.py`，从 `setup.py` 的 `_probe_speed/_rank_sources/_download_file/_download_one/_route_opener/_routes` 抽取并增强：结构化进度回调 `on_progress(dict)`、`.part.json` 元数据、尾部 64KB 重叠校验、sha256（含续传增量）、B.3 全部换源阈值（参数化，默认值同表）、源降权冷却、`max_switches`、磁盘满识别、取消/暂停标志（`threading.Event`，在每个 chunk 检查）、`Content-Type: text/html` 拒绝。
- `setup.py` 中 `_download_file` 改为调用 `download_engine.download(...)` 的薄包装，**签名与行为保持兼容**（旧调用方 `_install_zip/_download_http_model/_download_sep_weights/ms_base_download` 不改也能工作，进度回调仍输出旧格式字符串）。
- 触及文件：`download_engine.py`（新）、`setup.py`（`_download_file/_download_one` 处）、`tests/test_download_engine.py`（新）。
- 依赖：S2。
- 完成定义：D-S3 全部场景通过；`setup.py` 旧路径在假镜像上回归通过。

### S4 后台守护进程 + 插件门面（EXEC-PRO）
- 范围：`workers/download_daemon.py`（B.2 全部：单实例锁、心跳、队列、并发、cmd 处理、state 原子写、磁盘复查、空闲自退、日志滚动）；`download_manager.py`（`ensure_daemon/enqueue/control/read_state/feature_ready`）；后处理：`unzip`（复用 `setup._install_zip` 的放置逻辑，抽成纯函数 `setup.place_zip(spec, archive, dest_root)`）、`dlc_convert`（新 `workers/dlc_convert_worker.py`，调用现有 `_install_dlc_model` 的转换部分）、`hf_layout`（ACE/SoulX 文件放到 `checkpoints/<component>/…` 与 `snapshot_download` 相同的目录结构，并调用 `_sync_model_code_files`）。
- 触及文件：`workers/download_daemon.py`（新）、`workers/dlc_convert_worker.py`（新）、`download_manager.py`（新）、`setup.py`（抽 `place_zip`）、`tests/test_download_daemon.py`（新）。
- 依赖：S1、S3。
- 完成定义：D-S4 全部场景通过（尤其 kill 续传、双开、关插件后继续）。

### S5 分级安装后端（EXEC-PRO）
- 范围：
  - `setup.check_dependencies`：每个 item 增加 `feature`、`tier` 字段（来自 catalog 映射；ACE 项由 `_ace_setup_items` 填）；报告新增 `features: {cover:{ready, t0_ready, t1_ready, t2_ready, missing:[...], fg_gb, bg_gb, gpu_ok}, ...}`。
  - `setup.install(paths, items, progress, probe, *, features=None, tiers=None)`：`features/tiers` 给定时只装对应 key；T2 项**不在这里下**（返回 `deferred: [keys]`），除非 `tiers` 显式含 `T2` 且 `download_engine="legacy"`。
  - pip 换源：`_pip` 失败或 `Progress X of Y` 速率连续 30 秒 < `download_slow_bps` 时，换下一个 PyPI 镜像重跑（pip 缓存使重跑不重下已完成的 wheel）；torch 同理在官方 index 与阿里云 find-links 间切换。
  - ffmpeg：winget 失败 → catalog 中 ffmpeg zip 兜底，装到 `<compute_root>\tools\ffmpeg\bin`；`runtime.ffmpeg_status` 识别该目录；分离 worker 环境前插 PATH。
  - 新配置键（`plugin.toml [music_synth]` 追加，带注释）：`download_features = []`、`download_engine = "daemon"`、`download_keep_after_exit = true`、`download_auto_resume = true`、`download_max_parallel = 2`、`download_slow_window_s = 15`、`download_slow_bps = 204800`、`download_slow_ratio = 0.35`、`download_better_ratio = 3.0`、`download_max_switches = 8`；`download_source` 注释与枚举加 `auto`。
- 触及文件：`setup.py`、`runtime.py`（`ffmpeg_status`、配置默认值读取）、`plugin.toml`、`tests/test_tiering.py`（新）。
- 依赖：S1、S3。
- 完成定义：D-S5 通过；不传 `features/tiers` 时 `install()` 结果与改前一致（回归）。

### S6 插件入口（EXEC-FLASH）
- 范围（`__init__.py`）：
  - 新入口：`ms_dl_status`（只读，<200ms）、`ms_dl_enqueue({features?, artifacts?})`、`ms_dl_control({action: pause|resume|cancel|retry|prioritize|switch_source, target: id|"all"})`、`ms_dl_speedtest({artifact?})`（按下载物逐源测速，复用 engine 测速）、`ms_feature_status`（返回 `features` 段 + 下载中状态合并）。
  - `ms_setup_install` 新增可选参数 `features`、`tiers`；`download_source` enum 加 `auto`；成功后若 `download_auto_models` 语义（= `download_engine=="daemon"`）则自动 `enqueue(features)`。`setup_state.json` 新增 `base_completed`（T0+T1 就绪）与原 `completed` 并存（`completed` 语义不变 = 全部就绪）。
  - `startup` 生命周期：`download_auto_resume` 为真且 state 有未完成项 → `ensure_daemon()`；`shutdown`：`download_keep_after_exit=false` 时发 `pause_all`。
  - `ms_state` 快照里附 `downloads` 摘要（totals），减少面板请求。
- 触及文件：`__init__.py`。
- 依赖：S4、S5。
- 完成定义：D-S6 通过；旧入口回归通过。

### S7 ACE / SoulX / 底模下载并入守护进程（EXEC-PRO）
- 范围：`ms_ace_download`、`_ace_install_model_sync`（EXTRA_INSTALLERS `ace_model`）、`ms_soulx_install` 的权重部分、`ms_base_download` 改为 `enqueue` 到守护进程（repo 型 artifact：先列文件再逐文件下载，ModelScope/HF 多源）；保留原 `snapshot_download` 路径作为 `download_engine="legacy"` 回退。SoulX 的 venv/pip 部分仍由原 worker 做，worker 增加 `--skip-models` 参数。返回值保持含 `job_id`（映射到守护进程 item id），旧面板代码不改也能显示进度。
- 触及文件：`__init__.py`（上述入口）、`workers/soulx_install_worker.py`、`download_catalog.py`（repo 型定义）、`workers/download_daemon.py`（repo 列表逻辑）。
- 依赖：S4、S6。
- 完成定义：D-S7 通过；ACE 下载有真实字节进度（不再扫目录估算）。

### S8 向导：选功能 + 只装基础环境 + 直接进主页（EXEC-FLASH）
- 范围（`static/index.html`）：`Onboarding` 插入 `FeaturePicker` 步；安装步按功能过滤清单、T2 折叠为"后台下载"；"进入主页"在 `base_completed` 后可点；App 强制开向导条件改为 `!base_completed`；`wizardStartAt` 旧值兼容；已有安装（`completed=true`）升级后**不再弹向导**。
- 触及文件：`static/index.html`（`Onboarding` L5661 起、App L7000–7040）。
- 依赖：S6。
- 完成定义：D-S8 通过。

### S9 常驻下载条 DownloadDock（EXEC-FLASH）
- 范围：`static/index.html` 新组件 `DownloadDock`，挂在 App 的 `.ms-shell` 内；B.4-3 全部行为；轮询节流；面板不可见时（`document.hidden`）暂停轮询。
- 触及文件：`static/index.html`。
- 依赖：S6。
- 完成定义：D-S9 通过。

### S10 未安装门控 FeatureGate + 原地下载卡片（EXEC-FLASH）
- 范围：`static/index.html` 新组件 `FeatureGate({feature, children})`，包住 `PageMaterials/PageTrain/PageSing/PageCreate`（App L7100–7107 的渲染处包裹，不改各页内部）；`create` 页内 SoulX、`sing` 页内采集的局部门控用同组件的 `inline` 变体；B.4-4 全部行为。
- 触及文件：`static/index.html`。
- 依赖：S6、S9（共用状态 hook `useDownloads()`）。
- 完成定义：D-S10 通过。

### S11 测速与源设置界面 + 打包（EXEC-FLASH）
- 范围：`DeployOverview` 增加按下载物测速表（调 `ms_dl_speedtest`）、源偏好与"关闭后继续下载"开关（写 `ms_set_config`，其 schema 增加对应键）；README「安装」一节更新；`tools/build_package.py` 确认新文件进包、`tests/`/`tools/dl_*` 不进包、`.bak*` 不进包。
- 触及文件：`static/index.html`、`__init__.py`（`ms_set_config` schema）、`README.md`、`tools/build_package.py`（如需）。
- 依赖：S6。
- 完成定义：D-S11 通过。

### S12 部署 + 全流程跑通（EXEC-BOTH：PRO 先后端，FLASH 再界面）
- 范围：
  1. 备份生效安装目录的 `data\config.json`、`data\setup_state.json` 到 `_reports\dl_tiering_e2e\backup\`；
  2. 把 dev 改动拷到 `.neko-plugin-installations\plugins\music_synth`，重启 N.E.K.O（不要刷新桌宠页，见注意事项）；
  3. **全新安装演练**：向导里选一个新目录 `D:\NEKOMusicSynth_e2e`，勾选 翻唱+训练+原创，跑完 T0/T1 → 进主页 → 后台下 T2 → 期间执行 D-S12 全部扰动场景；
  4. **升级演练**：恢复备份的 config（指回老安装根目录），确认老安装零下载、零弹窗、所有页面不模糊；
  5. 证据（日志、state.json 快照、CDP 截图、测试输出）放 `_reports\dl_tiering_e2e\`，写 `_reports\dl_tiering_e2e\REPORT.md`。
  6. 结束后恢复用户原配置。
- 依赖：S1–S11 全部。
- 完成定义：D-S12 与 E 清单的执行方部分全部有证据。

### S13 总验收（@claude-opus-5-5）
- 按 E 清单逐项复核证据并抽测；不通过项退回对应步骤负责人。

依赖图：`S1 ∥ S2 → S3 → S4 → (S5 ∥) → S6 → S7 ∥ S8 ∥ S9 → S10 → S11 → S12 → S13`（S5 依赖 S1、S3，可与 S4 并行）。

---

## D. 每步测试与验收标准

> 公共命令（PowerShell，在 `D:\ruanjian\N.E.K.O\music_synth-dev` 下执行）：
> ```powershell
> python -m pytest tests -q
> python -m ruff check download_catalog.py download_engine.py download_manager.py workers\download_daemon.py workers\dlc_convert_worker.py setup.py __init__.py
> ```
> 插件入口调用（生效插件）：`curl.exe -s -X POST http://127.0.0.1:48915/plugin/execute -H "Content-Type: application/json" -d '{\"plugin_id\":\"music_synth\",\"entry_id\":\"ms_dl_status\",\"args\":{}}'`（参数格式以记忆中的宿主入口约定为准，S12 前执行方先用 `ms_state` 试通）。
> 读 UTF-8 输出：管道给 `python -c "import sys,json;print(json.dumps(json.load(sys.stdin),ensure_ascii=False,indent=1))"`。

### D-S1 目录与源实测
- 命令：`python -I tools\dl_verify_sources.py --out tools\dl_sources_report.json`；`python -m pytest tests\test_catalog.py -q`
- Given catalog，When 遍历全部 artifact，Then 每个非 repo 文件有 `size>0` 和 64 位十六进制 `sha256`，`sources` 数量满足 S1 完成定义。
- Given 某候选源返回 HTML / 308 跳到不可达域名，When 实测，Then 报告标 `rejected` 且不出现在 catalog。
- Given `artifacts_for(["create"], ["T2"])`，Then 只含 `ace_model`；`artifacts_for(["train"], ["T2"])` 含 cover 的 T2 + `ddsp_reflow_base`，不含 low/high。
- 网络相关测试标 `@pytest.mark.network`，离线时 `-m "not network"` 可跳过。

### D-S2 夹具自检
- 命令：`python -m pytest tests\test_harness_selfcheck.py -q`
- 每种故障模式各一条 Given/When/Then（限速误差 ±20%、断连字节点准确、篡改字节可被 sha 发现、HTML 200、忽略 Range 返回 200 全文）。

### D-S3 下载引擎
- 命令：`python -m pytest tests\test_download_engine.py -q`
- 正常：Given 3 个源（1/4/8 MB/s），When 下载 50MB，Then 选 8MB/s 源、sha 一致、目标文件存在且无 `.part`。
- **中途换源**：Given 源 A 前 10MB 快、之后降到 50KB/s，源 B 2MB/s，When 下载，Then 30 秒内（2 个窗口）换到 B，B 请求头 `Range: bytes=<pos>-` 中 pos>0（日志可证），最终 sha 一致，`switches==1`。
- **换源尾部不一致**：Given B 的内容与 A 不同（篡改），When 换到 B，Then 尾部重叠校验失败、`.part` 重置为 0、从其他源重下，最终 sha 一致。
- **不支持 Range 的源**：Given 已下 20MB，唯一备选源忽略 Range，Then 不把 `.part` 追加成坏文件；从 0 用该源重下或等待原源，最终 sha 一致。
- **校验失败**：Given 所有源都返回篡改字节，Then 两次重下后抛 `ChecksumError`，目标文件不存在，`.part` 已删。
- **磁盘满**：用 monkeypatch 让第 N 次 write 抛 `OSError(errno=28)`，Then 抛 `DiskFullError`，`.part` 保留且大小 = 已写字节。
- **卡死**：Given 源 20 秒内只给 100KB，Then 判失败换源。
- **HTML 冒充**：Given 源返回 `text/html` 200，Then 测速记 0、不下载。
- **暂停/取消**：设置 pause 事件后 ≤1 个 chunk 内返回 `Paused`，`.part` 保留；再次调用从断点续。
- **回归**：`setup._download_file` 在假镜像上下载成功、回调字符串匹配 `r"：\d+/\d+ MB"`（`_setup_progress` 依赖此格式）。

### D-S4 守护进程
- 命令：`python -m pytest tests\test_download_daemon.py -q`（测试里用临时目录做 state_dir、假镜像做源、子进程真实拉起守护进程）
- **kill 后续传**：Given 下载 200MB 到 ~40%，When `taskkill /F /PID <daemon_pid>`，Then 10 秒内 `ensure_daemon()` 识别心跳失效并重拉，新进程从 `.part` 大小续（第一条请求 Range 起点 ≥ kill 前记录字节数 − 1 个 chunk），最终 sha 一致。
- **双开**：同时调用两次 `ensure_daemon()` / 手工再起一个守护进程，Then 只有一个持锁进程存活，另一个 2 秒内退出，`.part` 未被并发写（最终 sha 一致）。
- **插件进程退出不影响**：Given 由一个"假插件"子进程拉起守护进程，When 杀掉假插件进程，Then 守护进程继续并完成（DETACHED 生效）。
- **命令**：pause/resume/cancel/retry/prioritize 各一条，state 在 2 秒内反映；cancel 后 `.part` 删除。
- **state 原子性**：读线程以 50ms 频率并发读 `state.json` 1 分钟，Then 无一次 JSON 解析失败。
- **磁盘满恢复**：模拟 `paused_disk_full`，When 让 free 检查返回足够，Then 30 秒内自动恢复。
- **空闲退出**：队列清空 60 秒后进程退出，`daemon.running=false`。
- **后处理**：zip 型（vocoder 结构的测试 zip）解压结果与 `setup._install_zip` 旧结果目录树一致；`dlc_convert` 在无 synth_python 时为 `pending_post`，提供后转为 `done`。

### D-S5 分级后端
- 命令：`python -m pytest tests\test_tiering.py -q`；`python -I -c "import runtime"` 类的导入检查不适用（包内相对导入），改用 `python -m pytest`。
- Given 全新 paths（临时根），When `check_dependencies`，Then 每个 item 有 `feature`/`tier`；`features.cover.ready=false`，`fg_gb`/`bg_gb` 与 catalog 体积求和一致。
- Given `install(features=["cover"], tiers=["T0","T1"])`（安装函数 monkeypatch 掉真实 pip/下载，只记录调用），Then 只调用 cover 的 T0/T1，返回 `deferred` 含 4 个 cover T2 key，不含 ACE。
- **回归**：`install(items=None)` 不传新参数时，调用序列与改前（录制的基线序列，S5 开工时先录）一致。
- pip 换源：模拟清华镜像 pip 退出码非 0，Then 自动用阿里云重跑一次且命令行 `--index-url` 已更换。
- 配置：`plugin.toml` 原有键全部存在且未改名（测试读 toml 比对基线键集合）。

### D-S6 入口
- 命令：`python -m pytest tests\test_entries_dl.py -q`（用 stub 宿主 SDK 实例化插件类；若实例化成本过高，则在 S12 真宿主上用 48915 调用验证，并在报告中注明）
- `ms_dl_status` 在 state 文件 5MB 时响应 < 200ms。
- `ms_setup_install({})` 老调用方式行为不变；`ms_setup_install({features:["cover"],tiers:["T0","T1"]})` 完成后 `setup_state.base_completed=true` 且守护进程自动入队 cover T2。
- 插件 `startup` 时 state 有未完成项 → 守护进程被拉起；`download_auto_resume=false` 时不拉起。

### D-S7 ACE/SoulX 并入
- 真机（S12 环境）：`ms_ace_download({model:"acestep-v15-turbo"})` 返回 job_id；`ms_dl_status` 中对应 item `bytes_total>0` 且 `bytes_done` 单调递增；完成后 `ms_ace_status` 认为模型就绪，原创页能生成一首 10 秒歌。
- 列文件失败（把 ModelScope API 域名临时指向假镜像返回 500）→ 回落 HF tree API → 再回落内置清单，三条路径各一条单测（`tests\test_repo_listing.py`）。
- `download_engine="legacy"` 时走原 `snapshot_download`，行为与改前一致。

### D-S8 向导
- CDP（连 48911 主窗口；按记忆先清"请她离开"的 `visibility:hidden` 状态）：
  - Given 全新安装根，When 打开向导，Then 出现功能选择步；无 NVIDIA 时原创卡片置灰且有原因文字。
  - Given 勾选 翻唱+原创，When 进入安装步，Then 清单只含 cover+create 的 T0/T1，T2 折叠显示"进入主页后后台下载 约 X GB"。
  - When T0/T1 完成，Then「进入主页」可点；点后向导关闭、主页出现 DownloadDock 且显示 cover+create 的 T2 项。
  - Given 已有完整安装（升级场景），When 打开面板，Then 不弹向导。
- 截图存 `_reports\dl_tiering_e2e\s8_*.png`。

### D-S9 常驻下载条
- Given 有 T2 在下，When 依次切换全部 8 个页面，Then 每页底部都有下载条，进度不回退（相邻两次读数 `bytes_done` 不减）。
- When 关闭插件面板 60 秒后重新打开，Then 下载条显示的 `bytes_done` ≥ 关闭前 + 60 秒 × 0.5 × 关闭前速度（证明期间在下）。
- 展开抽屉 → 暂停单项 → 状态变"已暂停"、速度归零；继续 → 恢复。
- 全部完成 → 显示"下载完成"并在 ~8 秒后收起；制造一项失败 → 条保持红色。
- 面板隐藏时（`document.hidden`）不发 `ms_dl_status` 请求（CDP 网络日志验证）。

### D-S10 门控与下载卡片
- Given 原创未安装，When 进入「原创歌曲」页，Then 页面内容模糊（截图可见）、内容区不可点击（CDP 点击按钮无响应）、居中卡片显示功能名+体积+[下载]。
- When 点[下载]，Then 卡片在模糊层上原地变为下载卡片，显示 T0/T1/T2 分段进度；URL/当前页不变。
- When 下载中切到「开始」再切回，Then 卡片仍在、进度 ≥ 离开前。
- When 下载完成，Then 模糊层消失、页面可操作。
- Given 只装了翻唱，Then 「让她唱歌」「准备素材」不模糊，「训练声音」仅在 reflow 底模未就绪时模糊。
- GPU：模糊层开启时 `chrome://gpu` 不要求；只要求 CDP `Performance.getMetrics` 中 1 分钟内无持续 > 30% 的额外主线程占用（与无模糊时对比）。

### D-S11 测速与设置
- 「测下载速度」按下载物显示每源速度与"将选用"，耗时 ≤ 90 秒；切换源偏好后再次入队，排序变化可在 `daemon.log` 看到。
- 打包：`python tools\build_package.py` 后 `python -c "import zipfile;n=zipfile.ZipFile(r'dist\music_synth.neko-plugin').namelist();print([x for x in n if 'download_' in x or 'tests/' in x or '.bak' in x])"` → 只含 4 个新模块，不含 tests/.bak。

### D-S12 全流程（真机）
执行方逐条执行并截图/存 state 快照：
1. 全新根目录完整走一遍（翻唱+训练+原创）：T0/T1 完成后进主页；T2 全部完成；翻唱出一首成品、训练能启动到第 1 个 checkpoint 前（可中止）、原创生成一首 10 秒歌。
2. **关页面不中断**：T2 进行中关闭插件面板 2 分钟，再开 → 进度推进。
3. **切页不中断**：T2 进行中在 8 个页面间来回切 20 次 → 无中断、无重复任务（state 中同一 artifact 只有一项）。
4. **关 N.E.K.O 不中断**：T2 进行中退出 N.E.K.O → `Get-Process python | ? { $_.Id -eq <daemon_pid> }` 仍在且 `state.json` 持续更新；重开 N.E.K.O → 面板自动接上。
5. **杀守护进程**：`taskkill /F /PID <pid>` → 插件 10 秒内重拉、从断点续。
6. **慢源换源**：用 Windows 防火墙规则或 hosts 把当前源（如 hf-mirror.net）限断（`New-NetFirewallRule -DisplayName dl_test_block -Direction Outbound -RemoteAddress <ip> -Action Block`，测完 `Remove-NetFirewallRule -DisplayName dl_test_block`）→ 30 秒内换源续传，`switches≥1`，最终 sha 通过。
7. **校验失败**：手工篡改一个已下 `.part` 的中间 1KB → 完成时 sha 失败 → 自动重下 → 成功。
8. **磁盘满**：在一个小容量 VHD（`New-VHD`/磁盘管理建 1GB 卷，或 U 盘）上做根目录跑 sep_karaoke → 出现 `paused_disk_full` 卡片提示；清空间后自动恢复。
9. **升级演练**：恢复原配置 → 老安装无下载、无弹窗、无模糊页；翻唱一首成功。
10. 恢复用户原始 `config.json`/`setup_state.json`，并在 REPORT 中写明已恢复。

---

## E. 整体验收清单（@claude-opus-5-5 最终验收用）

需求覆盖：
- [ ] R1 分级：向导先选功能；只装所选功能的 T0+T1；`FEATURES` 与 B.1 表一致；依赖自动带上。
- [ ] R2 静默后台：T0/T1 完成即可进主页；T2 自动入队；DownloadDock 在所有页面常驻直到完成。
- [ ] R3 门控：未安装功能页高斯模糊 + 居中"未安装"+[下载]；点后下载卡片在模糊层之上原地显示进度。
- [ ] R4 不中断：切页、关面板、关 N.E.K.O、杀守护进程四种情况都有证据显示继续/续传；UI 无任何隐式停止指令（代码审查 `ms_dl_control` 的调用点只在用户按钮上）。
- [ ] R5 多源换源：每个下载物 ≥2 个实测通过的源；慢于阈值 30 秒内换源；换源续传有尾部校验；sha256 全覆盖；测速按下载物维度。
- [ ] R6 全流程：S12 的 10 条全部有证据（截图、state 快照、日志片段）。

质量与兼容：
- [ ] `python -m pytest tests -q` 全绿（network 用例在有网环境下也全绿）。
- [ ] `ruff check` 无新告警。
- [ ] `plugin.toml` 原有键无改名/删除；新增键有中文注释；`download_source` 兼容旧值。
- [ ] 老安装升级：零下载、零弹窗、零模糊、功能正常。
- [ ] `ms_setup_install` / `ms_ace_download` / `ms_soulx_install` / `ms_base_download` 旧调用方式返回结构兼容。
- [ ] `download_engine="legacy"` 可整体回退到旧行为。
- [ ] 打包产物包含新模块、不含 tests/tools/备份。
- [ ] 全本地：新代码中无任何上传音频/用户数据的请求（grep `POST`/`urlopen` 仅指向下载源与本机端口）。
- [ ] `static/index.html` 未被 `ui/` 重建覆盖（舞台工作室页、测速页仍在）。
- [ ] 用户原始配置已恢复；DEV_NOTES.md 追加本轮记录（含剔除的候选源及原因）。

---

## 注意事项（执行方必读）

1. 前端改 `static/index.html`，**不要**跑 `_html_shell/build_dev.mjs`——`ui/` 落后于产物，重建会回退舞台页与测速页。
2. 生效插件目录是 `.neko-plugin-installations\plugins\music_synth`，根 `plugins\` 是旧副本。
3. 桌宠页面不要刷新（会崩），需要时重启 N.E.K.O；重启后先用 CDP 清"请她离开"状态。
4. 重启后 music_synth 若没 auto_start，用 48915 手动 start。
5. hf-mirror.com 会 302/308 到国内不通的 CDN，不能默认信任；一切以测速和 sha 为准。
6. E2E 会改插件配置里的 `compute_root`（会清掉派生路径），**必须先备份、结束后恢复**。
