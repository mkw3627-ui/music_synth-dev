# 开发说明（当前版本）

这个仓库是 N.E.K.O 插件「为她而唱」（`music_synth`）的源码。

## 目录

```
__init__.py            插件入口（全部 plugin_entry）
runtime.py             路径解析、硬件探测、worker 调用
pipeline.py            曲目库 + 分离→变声→混音流水线
song_listener.py       听歌录制 / 应用声道采集
model_package.py       声音包导入导出
ace_runtime.py         ACE-Step 本地运行时封装
workers/               独立子进程 worker
ui/                    面板源码（React 等价件，由宿主打包）
static/index.html      面板产物（单文件）
html/panel.html        同上（历史入口）
acestep/               vendored ACE-Step v1.5（Apache-2.0）
assets/                采集组件（proc-tap）与 SMTC vendor
plugin.toml            插件清单（同时声明 AI 入口 schema）
```

## 运行环境

插件本身跑在宿主 Python；重活由独立环境执行：

- **DDSP-SVC 6.3**：`<compute_root>/envs/ddsp`（torch 2.9 + CUDA 12.8）
- **ACE-Step**：复用 DDSP 环境的 torch；轻量依赖装在
  `<compute_root>/acestep/pylibs`（diffusers / loguru / accelerate /
  transformers 4.28 / tokenizers / huggingface_hub）
- **人声分离**：`<compute_root>/envs/sep`
- **模型权重**：不随仓库分发
  - DDSP：`<compute_root>/ddsp/DDSP-SVC/pretrain/reflow_base/model_0.pt`
  - ACE：`<compute_root>/acestep/checkpoints/`（约 6GB）

## 构建插件包

按 N.E.K.O 官方 `.neko-plugin` 格式打包：

```
manifest.toml + metadata.toml
payload/dependencies.toml
payload/profiles/default.toml
payload/plugins/music_synth/...
```

`tools/build_package.py` 会收集源码、生成 TOML、计算 payload hash 并做完整性校验。
用法：

```bash
python tools/build_package.py
# 输出 dist/music_synth.neko-plugin
```

## 不入库的内容

- `static/_preview/`（开发预览音频，几百 MB）
- `*.bak*` / `_trash/` / `claude-backups/`
- 模型权重、虚拟环境、训练数据、用户音频

## 中断续跑（2026-11-05）

插件重载会带走内存里的处理线程，所以流水线不能假设自己会一直活着：

- `Pipeline.process_track(..., resume=True)` 会复用上一轮已完成的分离、去混响/去噪、
  混响估计、配调、变声结果，只补没做完的步骤；`resume=False`（面板「重新唱一遍」）
  才从头重跑。复用变声结果要求模型、调性、步数和输入人声都没变。
- 启动时把残留的 `running/queued` 记为 `failed + step=interrupted`。如果是
  「生成并让她唱」产生的曲目（`auto_sing=true` 或源文件在 `work/ace/`），会自动
  续跑（最多两次），不需要用户再点一次「让她唱」。
- 面板按钮：成品在 → 「重新唱一遍」；没有成品但已有中间结果 → 「继续唱」；
  什么都没有 → 「让她唱」。错误提示的按钮名与之一致。

## ACE-Step 升级到 v0.1.8 + XL（2026-11-05）

- `acestep/` 整体替换为 ACE-Step v0.1.8；旧包备份在
  `acestep.bak_20261105_pre_v018/`。插件自己的 `ace_runtime.py` / `ace_worker.py` /
  `ace_train_worker.py` 调用签名保持兼容（`initialize_service` 多了可选
  `vae_checkpoint`，`generate_music` / `preprocess_audio_files` 未变）。
- 新增 XL（4B DiT）生成：`ms_ace_generate` 多了 `model` 参数，面板有「生成模型」
  下拉；turbo 默认 8 步，sft/base 默认 50 步（`inference_steps=0` 自动）。
  `ms_ace_download` 负责下载未安装的模型（XL 约 18.6GB，断点续传）。
- 依赖升级（`acestep/pylibs`）：transformers 4.57.1、tokenizers 0.22.1、
  huggingface_hub 0.36.0、peft 0.18.0；新增 vector-quantize-pytorch、einx、
  torch-einops-utils、frozendict；DCW 需要 pytorch_wavelets + PyWavelets +
  setuptools<81。`ms_ace_install` 的安装清单已同步。
- 本地补丁：`acestep/model_downloader.py` 的 `MAIN_MODEL_COMPONENTS` 去掉了
  `acestep-5Hz-lm-1.7B`。插件不加载 LM，若不改，每次首唱都会因为缺少 LM 触发
  整个主仓库重下。
- ACE 音色 LoRA 训练：`ms_ace_train` 现在接受 `models` 列表，默认把所有**已下载**
  的底模依次排队训练（turbo、xl_turbo、base、sft…）。每个底模产出独立 LoRA，
  多个模型时名字自动加变体后缀（如 `梦梦max_turbo`、`梦梦max_xl_turbo`），
  互不覆盖；某个模型失败不会影响后面的模型。`ms_ace_generate` 仍会检查
  `adapter_config.json` 的 `base_model_name_or_path`，不会把 2B 的 LoRA 挂到 XL 上。
- 训练 worker 用 `model_alias`（turbo / xl_turbo …）调用 `training_v2`；
  本地补丁：`acestep/training_v2/cli/args.py` 的 `VARIANT_DIR_MAP` 增加了
  `xl_turbo / xl_base / xl_sft`。
- 本地补丁：`acestep/training/dataset_builder_modules/preprocess_audio.py`
  在 `torchaudio.load` 失败时回退到 `soundfile`。Windows + torch 2.9 下
  torchcodec 的 FFmpeg DLL 经常加载不了，不补这个预处理会全部失败。

- **XL + CPU offload 的坑**：v0.1.8 在 4B XL 上开 `offload_dit_to_cpu=True`
  会在第一个扩散步后原生段错误（关 DCW 也一样）。现在 XL 固定用
  `offload_to_cpu=True` + `offload_dit_to_cpu=False`：既能避开崩溃，也会让
  upstream 的 VRAM 预检跳过（`offload_to_cpu=True` 时预检直接 return）。
  本机 16GB 实测 10s、275s XL + LoRA 都能生成；275s 之前因为预检
  “need 2.8GB / free 2.5GB” 被拦住，现在走 offload 不再预检。
- 低于 12GB 显存的卡仍会拒绝 XL。
- 下载源：`download_source=mirror` 映射到 ModelScope；XL 从 ModelScope
  下载实测约 60-70MB/s（HF 直连约 0.5MB/s）。`ms_ace_download` 可断点续传。

## 训练页分类 + 翻唱→原创串联（2026-11-06）

- 「训练声音」第 2 步改成两张类型卡：**翻唱的声音**（DDSP）和
  **原创歌曲的声音**（ACE LoRA），可以单选或全选。只选原创时直接调用
  `ms_ace_train`；两个都选时先跑翻唱。
- `ms_train_start` 新增 `ace_models` / `ace_name`。传了 `ace_models` 会强制
  `auto=True`，让翻唱跑到目标步数并自动 `finalize_best`，然后在
  `train_runs/<run_id>/chain.json` 记录串联状态。插件线程
  `_watch_train_chain` 等翻唱 `finished` 后自动调用 `ms_ace_train`，
  并把 ACE 任务 id / 结果写回 chain.json；插件重载后
  `_resume_train_chains` 会恢复。
- 翻唱被用户停止或失败时，chain 记为 `aborted`，不会启动原创训练。
- `ms_train_status` / `ms_train_runs` 的视图里多了一个 `chain` 字段，
  面板第 3 步据此显示「接着训练原创歌曲声音」的进度。
- 「我的声音」页拆成 **翻唱的声音**（DDSP 模型）和
  **原创歌曲的声音**（ACE 音色 LoRA，来自 `ms_ace_status.loras`）两组。
- 原创歌曲页训练完成的按钮只保留一个（训练→我的声音，生成→去让她唱歌）。

## 原创音色按名字合并（2026-11-06）

- `_ace_loras()` 现在按音色名合并变体：`梦梦op_turbo` + `梦梦op_xl_turbo`
  只显示为一个「梦梦op」，`variants` 里记录每个底模的 LoRA。
- 生成时传音色名（不带变体后缀）即可；`ms_ace_generate` 会按当前
  `model` 自动挑对应变体，没有该模型版本就明确报错。
- `ms_set_config` / 面板「设为当前」通过新入口 `ms_ace_set_lora` 保存
  `ace_lora`；不带 `lora` 参数生成时默认用它。
- 「我的声音 → 原创歌曲的声音」卡片与翻唱卡片对齐：设为当前、试听、导出、
  在哪里、删除。试听走 `ms_ace_preview`（10 秒、按 adapter mtime 缓存）；
  导出走 `ms_ace_export_lora`（zip 到 exports/）；删除走
  `ms_ace_delete_lora`（移进回收站）。
- 原创歌曲页的「训练 ACE 音色（LoRA）」折叠已移除，训练统一到「训练声音」页；
  训练页新增训练轮数 / LoRA rank 和系统推荐。




## 分离保立体声（2026-11-06）

`workers/separate_worker.py` 原来把输入和分离结果都 `mean(axis=1)` 成单声道。
ACE 原曲是立体声（左右相关约 0.93），经过「让她唱」后成品变成双单声道
（左右完全相同），背景音乐明显变窄、变糊。现在：
- 切片、重采样、片段拼接都按 [T, C] 处理，保留原始声道；
- 单声道片段在多轨拼接时自动补齐到相同声道数。
实测 10s 立体声片段：instrumental 左右相关 0.87，vocals 0.99。
注意：SoulX-SVC 模型本身输出 24kHz 单声道，这是模型限制；要 48k 立体声
人声请用 DDSP 或 ACE LoRA 直接唱。

## SoulX → DDSP 二级变声（2026-11-06）

`post_engine` 新增 `soulx_ddsp`：干声先走 SoulX-Singer SVC，再走一遍 DDSP-SVC 补音色/气音。

- 动机：SoulX 是 24 kHz zero-shot 模型，英文气音会被抹平（实测 2–8 kHz 频谱平坦度 0.0215，
  而 DDSP 是 0.0992）；DDSP 的 44.1 kHz 模型会把气音/噪声成分重新加回来。
- 链路：`_soulx_convert`（输出在 `<track>/convert/converted.wav`）→ 可选电平对齐 →
  `_convert`（输出在 `<track>/ddsp/convert/converted.wav`）→ 混音。
- `soulx_svc_worker.py` 新增 `match_input_level`：把 SVC 输出对齐到输入干声的 RMS
  （SVC 输出电平基本跟着 prompt 走，提示样本偏轻时会低 5–8 dB），峰值软限到 0.95。
- 与纯 DDSP 的实测差异（同一干声）：1–4 k 占比 11.7% vs 11.6%，8–12 k 0.56% vs 0.79%；
  气音指标基本一致（平坦度 0.0995 vs 0.0992）。也就是说气音来自 DDSP，SoulX 负责唱法/咬字。
- 代价：多一次变声（约 30 s/首）+ 一次 SoulX（约 1 min/首）。
- 曲目记录的 `steps.convert.engine` 会写成 `soulx_ddsp`，面板下拉的值为 `soulx_ddsp`。

## 原创页交互与生成质量（2026-11-06 晚）

- **SoulX→DDSP 去中间处理**：`_soulx_convert` 不再传 `match_input_level`，SoulX 输出原样进 DDSP。
- **面板选项收敛**：删除「不用（ACE 直接出成品）」和「SoulX-Singer SVC（实验）」两个下拉项；
  只保留「用翻唱声音（DDSP）」和「SoulX → DDSP」。后端仍兼容旧的 `""` / `soulx` 值。
- **seed 真修好**：`ms_ace_generate` 以前从不传 `use_random_seed`，ACE 默认 True，面板 seed 完全失效。
  现在填 seed 即固定；同 seed 下 8 步和 24 步输出逐样本一致（turbo 不吃步数）。
- **多候选生成**：`ms_ace_generate(candidates=1..6)`，默认面板 4。ACE 批量出 N 条，
  `ace_worker.py` 按 1–4k / 4–8k / 8–12k 占比、低频轰头、峰均比、立体声相关打分，
  返回 `best`；插件把没选中的候选也入库（标题加「· 候选 N」），可在「让她唱歌」试听。
- **面板持久化**：原创页的表单/高级选项存 localStorage（`ms_create_ui_v2`），切页/重开插件后恢复。
- **任务恢复**：`ms_ace_status` 返回 `jobs`（queued/running 的 ace / ace-download）；
  原创页挂载时自动接回正在跑的任务，不再因为切页而「消失」。
- **原创/翻唱标签**：`ms_track_audio` 对 origin=ace 且没有 final 的曲目回退到 source；
  「唱好的歌」每条加「原创 / 翻唱」Pill，候选曲也能直接试听。
- **模型删除**：新增 `ms_ace_delete_model`（移进 `<root>/acestep/_trash_models`）；
  拒绝删除当前选中模型、拒绝在生成/下载中删除。面板生成模型下方有删除按钮。
- **下载进度**：`ms_ace_download` 的 job 线程每 2 秒统计目标目录体积，按 catalog 的
  download_gb 折算成 5–95% 写入 job；面板在模型选择下方和任务卡都显示进度条。
- **下载实测**：`acestep-v15-sft`（ModelScope）4.5GB 下载成功，10s/50 步生成成功。
  注意：sft 是 2B 模型，现有 `梦梦op` LoRA 只有 turbo / xl_turbo 变体；要用 sft 唱她的声音，
  需要再训练一个 sft 版 LoRA（`ms_ace_train` 默认会训练所有已下载底模）。

## 底模兼容层（2026-11-06 夜）

- 新增 `workers/base_ckpt.py`：解析底模并从张量形状推断结构。
  - 支持 DDSP-SVC 6.x `{global_step, model}`、SVCFusion `.sf_dlc` 容器、
    plain state_dict / `{state_dict|weight|model}`、`module.` 前缀。
  - 识别 RVC / so-vits-svc 并明确拒绝（架构不兼容，不能静默转换）。
  - 推断 sampling_rate / block_size / win_length / n_layers / n_chans /
    n_aux_layers / n_aux_chans / use_pitch_aug / encoder。
- 训练启动时自动把底模转成 `<expdir>/model_0.pt`，把结构写进 `state["arch"]`；
  `train_session_worker._build_config` 用 arch 覆盖 `configs/reflow.yaml` 的默认结构。
- 新增入口 `ms_base_inspect`（path / tier / convert），面板「翻唱训练强度」旁有
  「检查底模」按钮；训练失败时也会明确写出不兼容原因。
- 设置可用 `ddsp_base_low / ddsp_base_mid / ddsp_base_high` 指定三档底模路径；
  没配置时统一回退到内置 `pretrain/reflow_base/model_0.pt`。

## 三档底模映射（2026-11-07）

官方 `SVCFusion/DDSP6.3-Pretrain` 实际有 7 个变体，命名是 `<aux>_<main>`：

- 低 = `ddsp6.3_6x512_6x512.sf_dlc`（6 层 / 512 主通道，0.09GB）
- 中 = `ddsp6.3_6x512_6x1024.sf_dlc`（6 层 / 1024，0.20GB，原内置）
- 高 = `ddsp6.3_6x512_10x2048.sf_dlc`（10 层 / 2048，0.97GB）
- 其它：`6x512_6x768`(0.14) / `6x1024_6x1024`(0.31) / `10x1024_6x512`(0.27) / `10x1024_6x1024`(0.39)

- 文件放 `<ddsp_root>/pretrain/bases/`，`_train_base_ckpt` 会自动找；也可用
  `ddsp_base_low/mid/high` 显式指定。
- 训练时自动把 `.sf_dlc` 转成 `model_0.pt`，并按推断的 arch 覆盖训练配置。
- 自动 batch 估算会按底模层数/通道放大每 batch 显存，避免高档底模直接 OOM。

## 首次检测认可 bases 下的 .sf_dlc（2026-11-07）

背景：用户机器上早已放好官方 `ddsp6.3_6x512_10x2048.sf_dlc` /
`ddsp6.3_6x512_6x512.sf_dlc`，但 `ms_setup_check` 只找转好的 `high.pt` /
`low.pt`，把两项报成「可选缺失 · 1.1GB」，误导用户以为要下载。

- `setup.py` 里 `ddsp_base_low/high` 增加 `tier` 字段；`_missing_files` 判定缺失后，
  `_find_tier_base` 会扫 `<ddsp_root>/pretrain/bases/*.sf_dlc`。
- 档位优先按 `workers/base_ckpt.py`（DDSP 环境自己的解析器）读出的
  `n_layers/n_chans` 判定：6 层/512 → 低，10 层/2048 → 高；结构读出来但不符
  档位的不会被文件名放行。解析器不可用（DDSP 环境没装好）时才退回官方文件名
  `ddsp6.3_6x512_<层>x<通道>.sf_dlc`。
- 解析结果按「路径+大小+mtime」缓存在进程内，冷启动一次约 1.5 秒/文件，
  之后重复检测不再起子进程（实测第二次 `check_dependencies` 0.11 秒）。
- 报告条目新增 `resolved_path` / `detected_by`（`arch` 或 `filename`），`detail`
  写明「已就绪：结构 10 层 / 2048 通道」。两项仍是 `optional=true`，可选组、
  跳过引导的语义不变；判定就绪后 `install(items=None)` 自然不会再排这两项下载。
- `setup.resolve_tier_base` 与 `_train_base_ckpt` 共用同一判定（官方文件名之后的
  兜底），避免「检测说已就绪、训练却回退中档」。

## 分享/冷启动链路（2026-11-07）

- 低/高训练底模纳入 `setup.MODEL_ITEMS`（kind=dlc，hf-mirror + HuggingFace 回退），
  首次安装向导会下载到 `<ddsp_root>/pretrain/bases/low.pt|high.pt`。
- `runtime.paths` 增加 `ddsp_base_low/mid/high`；`_train_base_ckpt` 按
  配置路径 → 安装路径 → bases 下的 .sf_dlc → 内置中档 的顺序解析。
- 新增 `ms_base_status` / `ms_base_download`；训练页在质量档位下显示
  「底模已就绪」或「下载底模」按钮，并带真实下载进度。
- 没有低/高档底模时训练会明确提示回退到中档，不再静默。

## 产物校验 + 采集排障（2026-11-07，v0.2.27）

两处改动都来自本机实测，不是推测。

### 1. 生成产物有效性校验（`ace_runtime.py`）

**问题**：GTX 1650（sm_75，pre-Ampere）上 fp16 采样溢出，DiT 仍返回 `success=True`，
写出的 WAV 每个采样点都是 `-1.0`（唯一值数量 1、过零率 0）。调用方
`workers/ace_worker.py` 只看 `success`，于是把死文件当成品交给用户，用户只听到「没声音」。

**改动**：新增 `_probe_audio_file()`（纯标准库），在生成返回前抽首/中/尾三段共约 2.4 万采样点，
检查唯一值数量与峰值：
- 唯一值 ≤ 1 → 判定「常量信号」
- 峰值 < 0.001 → 判定「近乎静音」
- 命中即把 `success` 置 `False`，消息进 `error` 与 `status_message`（调用方 `error` 优先，两处必须一致）
- 检测结果写进 `extra_outputs["quality"]["probes"]`，供面板展示

**附带**：设备为 cuda/auto 时，失败消息追加 pre-Ampere 提示（改 `device=cpu` 重试）。

### 2. 采集子进程 stderr 不再丢弃（`workers/app_song_worker.py`）

**问题**：`Capture.start()` 用 `stderr=subprocess.DEVNULL` 丢弃子进程 stderr。缺
`proctap/_native.*.pyd` 这类真因（`ImportError: No module named 'proctap._native'`）
全在 stderr 里，被吞掉后用户只看到「录不到声音」，无从排障。

**改动**：
- stderr 重定向到 `<out_dir>/capture_stderr.log`（超过 2MB 自动轮转）
- 新增 `Capture.stderr_tail()`；`packet()` 抛错时附带 stderr 末尾 12 行
- `stop()` 用 `try/finally` 关闭日志句柄

### 验证

| 项 | 结果 |
|---|---|
| `_probe_audio_file` 对有效音频（端到端成品 / MusicGen） | `ok=True`（uniq 15628 / 9559） |
| 对人为构造的常量 −1.0 | `ok=False` →「常量信号（唯一值 1，峰值 1.000）」 |
| 对全静音 WAV | `ok=False` →「常量信号（唯一值 1，峰值 0.000）」 |
| `pytest plugin/plugins/music_synth/tests/` | 1 passed |
| 沙箱实测（N.E.K.O standalone 服务器） | `/health` 200，`music_synth` = running |

## 口型 / 显存规划 / 联动（2026-11-07，v0.2.28）

- 逐字口型：`lipsync.py` + `lips_server.py` + `lyrics_fetch.py`，`data/lips_pinyin.json`
  提供离线「汉字 → 拼音 → 口型」表；新增入口 `ms_lips_status`（检查/重建某曲目的
  口型轨，`rebuild=true` 强制重算）和 `ms_lips_preview`（不出声，只让角色按歌词对口型）。
- 显存规划：`vram_policy.py` + `ms_vram_plan`；训练与唱歌互斥，唱歌前等训练空闲，
  唱完复原「为她所听」的采集优先级。
- 与她所听联动：`song_listener.py` 增加联动状态、优先采集与歌词抓取；面板显示
  「已联动为她所听」，联动期间本页不提供手动开始/停止。
- 训练：
  - 修复全自动步数上限的 `TypeError`：素材 ≥10 分钟时三档上限都没覆盖到，
    `cap` 为 `None`，`while step < cap` 直接崩；补 32000 档并加兜底，
    里程碑里带 `null` 也不会再整次训练失败。
  - 翻唱 + 原创全部练完后，训练页自动跳到「我的声音」，同一训练只跳一次；
    避免用户停在训练页看不出已经练完。
- ACE：`candidates`（1–6 个候选，按频谱/动态打分挑最佳）与 `quantization`
  （auto / off / int8_weight_only / fp8_weight_only）参数透传。
- 采集：新增 `workers/song_titles.py`，录制标题清洗与曲名处理增强。

## 市场发布：包内不再带二进制（2026-11-07，v0.2.29）

市场对插件包的硬规则：**包内不允许出现二进制文件**
（`check --release` 报 `the Market rejects binary files in the package`）。
v0.2.28 的发布任务就是死在这一条上，被点名的有：

- `vendor/proc-tap-.../bin/proctap.exe`、`vendor/proc-tap-.../proctap/_native.cp311-win_amd64.pyd`
- `vendor/smtc_vendor/winrt/_winrt*.pyd`（4 个）、`vendor/smtc_vendor/winrt/msvcp140.dll`
- `vendor/acestep/genres_vocab.txt.gz`

改法：

- 新增 `pyproject.toml`，用 `[tool.neko.build] exclude = ["*.pyd","*.exe","*.dll","*.so","*.dylib","*.gz"]`
  让官方打包 CLI 不收二进制；`tools/build_package.py` 同步加了同样的后缀过滤。
- proc-tap 与 winrt 改为安装时 pip 装进合成环境：
  `CAPTURE_NATIVE_PACKAGES = ["proc-tap==1.1.1", "winrt-Windows.Media.Control"]`，
  装不上只影响「逐应用采集 / 系统媒体信息」，不阻塞环境安装。
- `runtime.py` 找不到包内组件时回退到合成环境 venv 的 site-packages：
  `_capture_paths_ok` 接受 `site-packages/proctap/_native*.pyd`，
  `locate_app_capture_runtime` 的 addon 回退为 vendor 目录，
  `locate_smtc_runtime` 增加 `site-packages/winrt/windows/media/control` 判定。
- ACE 词表改为随包发纯文本 `vendor/acestep/genres_vocab.txt`（4.98MB），
  `constrained_logits_processor` 本来就支持无 .gz。

实测：在 `E:/NekoMusicSynth/envs/ddsp` 里 `pip install proc-tap==1.1.1 winrt-Windows.Media.Control`
后，用插件同样的隔离方式 `python -I -S` 可以正常 `import proctap` 和
`from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager`。

### 0.2.30：过 2 MiB 单文件上限

v0.2.29 的发布检查已经不再报二进制，但换成两条新问题：

- `[ERROR] the Market rejects package files over 2 MiB: vendor/acestep/genres_vocab.txt`
  —— 市场对**单个文件**还有 2 MiB 上限。这个词表只被 ACE 的
  `MetadataConstrainedLogitsProcessor`（LLM 约束解码）用到，而本插件
  `ace_runtime.py` 固定 `use_constrained_decoding=False`、也不加载 LM，
  所以直接删掉，不再随包发。
- `[WARN] could not derive plugin metadata (staged tree contains directories that
  cannot survive packaging (['vendor/proc-tap-1.1.1-4a0fbb7d05/bin']))`
  —— 排除 .exe 之后留下一个空目录，导致插件元数据推断失败、入口参数 schema 丢失。
  处理：`vendor/proc-tap-1.1.1-4a0fbb7d05/` 与 `vendor/smtc_vendor/` 整个从仓库移除，
  原生组件统一走安装时 pip（`CAPTURE_NATIVE_PACKAGES`）。备份留在
  `D:\ruanjian\N.E.K.O\_vendor_backup_20261107\`（也在 git 历史里）。

## 0.2.31：入口重复注册 + plugin.toml 字段合规

发布检查里还剩两条警告，这次清掉：

- `duplicate @plugin_entry id 'ms_set_active_model' in __init__.py:4960; first seen at __init__.py:3498`
  —— 同一个 id 声明了两次：3494 行的包装方法 `ms_set_active_model_action`
  和 4957 行的真正实现 `ms_set_active_model`。后注册覆盖前者，等于白写一个入口。
  处理：删掉 3494 行的 `@plugin_entry`，**保留 `@ui.action`**（面板「设为当前模型」
  按钮还绑着它，方法体也只是转调真正的实现）。两个方法都留在类里，功能没有任何删减。
- `[plugin].entries is not a recognized plugin.toml field`
  —— 当前官方校验器的 `[plugin]` 允许字段里已经没有 `entries`；入口由
  `@plugin_entry` 装饰器在加载时注册，打包 CLI 会把它们写进 `plugin.meta.json`。
  处理：删掉 plugin.toml 里 `# >>> STATIC ENTRIES` 之后的全部 `[[plugin.entries]]`
  块（64 个）。宿主 `plugin/core/registry.py` 是从装饰器收集入口的，
  `plugin.meta.json` 负责给面板提供参数 schema，所以功能不受影响。

验证（在本机用 stub 的 `plugin.sdk` 真实 import 一次插件包）：
- 修复前：75 处 `@plugin_entry` 声明、74 个唯一 id，`ms_set_active_model` 重复 2 次（3498 / 4960）。
- 修复后：74 个唯一 id、0 重复，`ms_set_active_model` 只出现 1 次且绑定到实现方法；
  3 个 `@llm_tool` 正常注册；所有 `@ui.action` 指向的入口 id 都存在。

未动：`[music_synth]` 段里的旧运行配置（`sep_backend` / `precision` / `max_parallel` /
`download_source` / `auto_setup`）。校验器对它的提示是「仍兼容，建议移到
config.example.toml」，但移动会改变默认值来源，这次只记录不动。

## 0.2.32：合并本地已装版本的功能（2026-11-07）

本地安装目录（`.neko-plugin-installations/plugins/music_synth`，0.2.25 + 后续本地改动）
和仓库（0.2.31）是两条分叉。这次把两边合到一起，功能不删减。

从本地合入：

- **口型轨**：`lipsync.build_track_payload` / `track_lyrics`（payload v3）、
  `_carry_visemes`（长音符/促音/表外字沿用邻字口型）、`vocal_syllable_onsets` +
  `_snap_bounds`（行内字边界吸附到干声起音）。流水线处理时预生成 `lips.json`，
  播放只读盘；播放侧 `_lips_payload` 优先用缓存，歌词变了会自动重算。
- **翻唱可用性判断**：`workers/song_check_worker.py` + `Pipeline._check_song`
  （纯音乐 / 录音不完整 / 空录音）。`ms_sing_anchor(force=true)` 可强制照唱；
  面板显示 `instrumental` / `incomplete` / `silent` 与 `check_reason`。
- **录音完整性**：`app_song_worker` 记录播放器报的整首时长与起止进度
  （`expected_s` / `start_position_s` / `end_position_s`），`song_listener` 同步给
  「为她所听」；播放器不报时用 `lyrics_fetch.fetch_song_duration` 兜底。
- **训练前释放显存**：`vram_release.py`。先请求「为她所听」只停 GPU 模型，
  失败再整插件停掉；训练进入等待/结束后空闲 45 秒，由后台线程恢复。
- **语料流程**：`_slice_bounds` 两级切口（长静音 + 句间换气）、`_snap_cut` 切到
  附近最安静的一帧；`dry_sing` 改为 bg_probe 粗筛 + 伴奏轨核对
  （`workers/stem_check_worker.py`）+ 纯音乐跳过（`song_check_worker`）；
  `ms_corpus_reprocess` 按新流程重跑历史素材。**取消去混响**：anvuew 会把
  6~11 kHz（齿音、气声、咬字）压掉 20~37 dB，人声发闷，训练素材同样不再走这一步。

保留仓库侧（不回退）：

- 0.2.27 采集 stderr 排障、0.2.28 CPU 降级、0.2.29 原生组件改 pip、
  0.2.30 过 2 MiB、0.2.31 入口去重与 plugin.toml 字段合规。
- `_run_attempts`：DDSP / SoulX 变声 OOM 自动改走 CPU（本地版还没迁到这两处）。
- **`runtime.is_oom`**：0.2.31 的 runtime.py 里漏了这个函数，而 `pipeline.py` 与
  `__init__.py` 都在调用它——任何 worker 失败都会 `AttributeError`。这次补回。

验证（本机用 stub 的 `plugin.sdk` 真实 import 插件包）：

- 合并后 75 个唯一 `@plugin_entry`、0 重复、3 个 `@llm_tool`，
  所有 `@ui.action` 指向的入口都存在。
- 本地安装版是 76 个声明（含重复的 `ms_set_active_model`），仓库 0.2.31 是 74 个；
  合并后 75 = 74 + `ms_corpus_reprocess`，入口 id 集合与本地版完全一致
  （`ms_set_active_model` 只注册 1 次，保留 0.2.31 的修复）。

## 首次使用引导重做（2026-11-07）

旧的四步「欢迎 / 安装位置 / 安装 / 完成」向导换成五页新手引导
（`ui/onboarding.tsx`，`ui/setupWizard.tsx` 只保留同名转导出）：

1. 体验：直接播放内置音色「沫沫」的试听样曲（`ms_ace_preview`，失败回退
   `ms_train_preview`；都是 `data/demo/momo_demo.mp3`，不占显存）。
2. 功能一览：听歌识曲 / 人声分离 / 翻唱变声 / 训练自己的音色 / 原创歌曲 /
   混音出曲 / AI 可调用演唱，横向翻页。
3. 使用流程：导入歌曲 → 选择音色 → 生成 → 试听导出。
4. 依赖与环境：原向导的全部文案、安装位置选择、17 个检测项、单项安装 /
   一键安装 / 进度轮询 / 失败重试都在这一页；操作栏固定在卡片底部。
5. 完成：全部就绪 + 开始使用。

- 旧向导的说明文字、依赖项标签、安装/失败/完成提示都保留，只是排版和分页重做
  （对照表见 `D:\ruanjian\N.E.K.O\_backup_onboarding\old-content-mapping.md`）。
- 「已经引导过」记在 localStorage `ms_onboarding_v2_done`（完成或跳过都会写）。
  首页「重新查看引导」和设置页入口都能重新打开，从第 1 页开始。
- 必需项还能在本页安装时，跳过会被拦回第 4 页；检测失败 / 没有可安装项时不拦。
- **`ms_setup_check` 在「环境未就绪」时会返回 `ok=false`**，而 `ui/api.ts` 的
  `unpack` 把 `ok=false` 当业务失败。新加了 `callEntryLoose`（只剥
  `success/data` 外壳，不看 `ok`），向导检测走它；其它入口仍走原来的
  `callEntry`，避免把真正的失败吞掉。

### 安装目录改成系统目录选择框（2026-11-07）

用户反馈：「安装到哪里」不该让人手打绝对路径。第 4 页现在有「选择文件夹」按钮，
手填输入框只作兜底：

1. 优先 `window.nekoHost.pickDirectory({startPath,title})`（Electron 预加载桥，
   `src/preload/shared/common.js` 里的 `setupHostCapabilityBridge`）。返回
   `{cancelled:true}` 或 `{cancelled:false, selected_root}`，直接回填。
2. 没有宿主桥（普通浏览器 / 旧宿主）时走新入口 `ms_setup_browse`：
   `setup.py` 在后台线程里弹原生目录框，入口本身只等 `wait` 秒（上限 30，
   因为 `plugin_runtime.timeout = 45`）。UI 用 `restart` 只在第一次传 true，
   之后重复轮询拿结果，避免取消后又被轮询重新弹窗；返回
   `state=picked|cancelled|open|error`，`error` 时给警告 + 盘符快捷填入按钮。
3. 原生实现顺序：Windows 先走纯 `ctypes` 的 `shell32!SHBrowseForFolderW`
   （`BIF_RETURNONLYFSDIRS|BIF_EDITBOX|BIF_NEWDIALOGSTYLE`，起始目录用
   `BFFM_SETSELECTIONW` 设好）；失败再试 `tkinter.filedialog.askdirectory`。
   **宿主内 tkinter 不可用**：PyInstaller 打进了 `_tkinter.pyd` / `tk86t.dll`，
   但没打 `tkinter.filedialog` 子模块，`import` 直接 ImportError。
- 原输入框保留，`suggested_root` 预填、剩余空间判断、「模型和程序只装到这个
  目录」的说明都在；那句「必须自己填一个绝对路径……不会自动挑盘」的吓人文案
  已换成「点「选择文件夹」在系统窗口里挑一个位置；模型和程序只装到这个目录，
  不会改动系统设置。」
- 入口靠 `@plugin_entry` 注册，`plugin.toml` 自 0.2.31 起不再写
  `[[plugin.entries]]`（字段本身不被识别），所以这次只加装饰器、没改 toml。

### 训练质量底模改可选（2026-11-07）

`ddsp_base_low`（6x512，约 0.09 GB）/ `ddsp_base_high`（10x2048，约 0.97 GB）
在 `MODEL_ITEMS` 里加了 `"optional": True`：

- 报告里 `required=false`，缺了不参与 `ready` 判定；`required_missing_gb` 只算
  必需项，可选单独给 `optional_missing_gb`（`total_missing_gb` 保留旧名，语义
  已收窄为必需项）。
- 第 4 页排序：必需缺失 → 可选缺失 → 必需就绪 → 可选就绪；可选组上方一条
  「可选 · 只在训练音色时用；不装也能训练，会自动用中档底模」，状态用中性的
  「可选」而不是红色「缺少」。
- 「一键安装全部」在后端 `install(items=None)` 里就是「所有可安装且未就绪」，
  所以包含可选项；必需项全齐时底部按钮 label 变成「安装可选项」。
- 只有可选项缺失时 `ready=true`，跳过引导 / 下一步都放行；必需项缺失时仍然
  拦（`knownMissing` 只看 `required && !ok && installable`）。


