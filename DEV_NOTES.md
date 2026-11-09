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



## 0.2.33：内置体验音色 + 新手引导重做（2026-11-08）

- **内置体验音色「沫沫」**：原 `voice_20261107_104445` 改名为沫沫（3 个 LoRA 变体、
  exp 目录、`ddsp_ckpt` 同步改，仍然是同一个 `model_2000.pt`，1,037,295,771 字节）。
  新增 `data/bundled_voices.json` 登记表（名字 → 试听样曲），`ms_ace_loras` /
  `ms_state` 给登记过的音色补 `bundled` / `demo` 标记，卡片显示「内置 · 体验」；
  `ms_ace_preview` / `ms_train_preview` 命中内置音色时直接返回自带 demo，不跑推理。
  试听是整首 mp3：2,029,100 字节（1.94 MiB）、253.6 秒、48 kHz 双声道，市场允许
  `.mp3` 资产不受 2 MiB 单文件限制，也不会触发二进制扫描。
- **新手引导重做（五页）**：体验（大播放器放沫沫 demo）→ 功能（7 项翻页）→
  用法（导入→选音色→生成→试听导出）→ 环境（17 项检测、单项/一键安装）→ 开始。
  旧引导约 40 条原文逐条保留（对照表 `_backup_onboarding/old-content-mapping.md`）。
  安装位置改为系统「选择文件夹」：宿主桥 `nekoHost.pickDirectory` → 新入口
  `ms_setup_browse`（ctypes `SHBrowseForFolderW`，tkinter 兜底）。
- **高低质量训练底模改为可选**：`optional=true`，缺了不拦跳过引导；概览把
  「必需 / 可选」体积分开显示。
- **底模检测修复**：`pretrain/bases` 下的官方 `.sf_dlc` 按结构认档位
  （`workers/base_ckpt.py` 读 `n_layers`/`n_chans`；6 层/512=低档，10 层/2048=高档），
  解析器不可用时退回官方文件名；`_train_base_ckpt` 共用同一判定。首次检测不再
  把已放好的底模报成「缺 1.1GB」。

## 口型轨 v5：行尾拖长音不再闭嘴（2026-11-08）

现象：「有些词没有口型」——唱到句尾长音/副歌延长/ラララ 时嘴提前闭上，声音还在唱。

原因：`build_timed_cues` 按 `cap = max(1.2, 字数*0.42)` 估每行时长，LRC 只给
「下一行什么时候开始」。录音里最后一个字常常拖得比这长（Remember 里
`あなたのハートに` 从 138.3 一直唱到 148.7，估出来 141.6 就结束了），
于是剩下的几秒没有任何 cue，嘴闭着。用干声包络量过：整首 14.4 秒
「有声无口型」，修复后 0.5 秒。

修复（`lipsync._vocal_span_end` + `build_timed_cues(env=…)`）：
- 每行铺完字后，从 `bounds[-1]` 往后扫干声包络（阈值 0.14、允许 0.55 秒
  的断续、行尾 0.8 秒内没有声音就当真间奏），找出这一行真正收声的时刻；
- 用最后一个字的元音补 0.45 秒一节的延续 cue（桥那边每节有 ±6% 起伏，
  长音不会变成定格表情），并且去掉这个字的句尾换气标记（这不是换气口）；
- `payload["v"] = 5`，`__init__._lips_payload` 的缓存门槛 `old_v < 5`
  会让旧的 v4 缓存下次播放时自动重算。

## 口型轨 v6 + 桥 g29：响度调制，嘴不再"木头"（2026-11-08）

反馈：「它只知道张的最大、张的最小，中间没有滑动值」。

原因：张度完全由 5 个元音常量决定（A=.88 / O=.76 / E=.66 / I=.52 / U=.42），
字内只有 ±6% 的弧线，字与字之间是 90ms 过渡——每个字都是一个定格张度。

修复：
- `lipsync.build_track_payload`：cue 模式也把干声包络（50ms/帧）一起写进
  `lips.json`（v6，Remember 从 12KB → 42KB）。
- `bridge.js`（g29）：`open = cue_open × (0.45 + 0.85 × amp)`，amp 是同一时刻
  干声包络的线性插值；A/O/E/I/U 只决定"这个音的口型上限"，真实响度决定
  "这一刻张多大"。渐强、渐弱、颤音都会穿过 30/110ms 平滑走到画面上。
  实测同一段（行尾长音 141.6–148.6s）：张口度标准差 0.020 → 0.101，
  帧间变化 >2% 的帧占比 1% → 18%。
- `lips_server.snapshot`：旧桥（`?v` < 29）不下发 env——g28 见到 env 会
  走"没歌词，按响度开合"分支而完全忽略 cue 轨，刷新到 g29 后才启用。
- 顺手修掉一个反方向的问题：包络为 0 的段落（比如这首 218s 后干声没唱）
  现在 `open=0`，嘴不会在没声音的地方自己动。

页面部署：`resources/bin/static/live2d/music-groove-bridge.js` 已替换成 g29，
`templates/index.html` 第 385 行 `lips4-g28` → `lips4-g29`。

## 口型轨 v7 + 桥 g33：重音/轻音，张口幅度与起手速度都分开（2026-11-08）

反馈：「有些词能识别到重音和轻音吗？重音张口幅度不一样，张幅速度也不一样。」

数据侧（`lipsync._apply_stress`，cue 从 5 元组变 6 元组，末位是 0..1 的重音）：
- 每个字取「字内较响那一半包络帧的平均」而不是峰值——包络按全曲 p95 归一化，
  响的峰值全被削到 1.0，只看 max 会让副歌整段同分（实测 29% 的字顶在 1.0）；
  取半边平均后顶格只剩 2%。
- 两路信号：全局 `g`（对整首 p20~p90 归一化）+ 局部（比前后各三字中位数响多少，
  权重 0.65/0.35）。算完再按本曲自己的 p15~p90 二次拉伸并做 `t^1.15`，
  轻音压到 0、重音顶到 1，避免一大片 0.5 的“平”字。
- 实测分布（Remember 463 字 / 愛のシュプリーム! 1189 字）：中位 0.39/0.51，
  0 和 1 各占 19%/20% 与 11%/10%，不再是全曲一个张度。
- 没有包络时统一给 0.5（桥行为与 v6 一致），旧四/五元组 cue 也能读。
- `payload["v"] = 7`，缓存门槛 `old_v < 7` 自动重算旧轨。

桥侧（`bridge.js` g33）：
- 幅度：`open × (0.76 + 0.44 × stress)`——同一个元音，重字比轻字多张约 58%。
  包络系数从 0.85 微调到 0.80，给重音腾余量；两条系数合起来副歌最响处
  只有 5~8% 的帧会顶到 1.0，其余仍在响度里起伏。
- 速度：张口时间常数 `60ms → 18ms`、收口 `135ms → 80ms`（stress 0→1），
  元音间换形窗口 `120ms → 60ms`。轻音慢起慢收，重音是「咬」下去的。
- 顺手补了静音口型：响度 < 0.12 时增益线性收到 0（0.12 处两段相等，连续）。
  前奏/间奏（愛のシュプリーム! 前 15 秒、Remember 218s 后）不再半张着嘴。

页面部署：`music-groove-bridge.js` 已换成 g33，`index.html` 第 385 行
`lips4-g32` → `lips4-g33`。刷新页面（Ctrl+Shift+R）即可，不用重启游戏。

## 口型轨 v8 + 桥 g34：轻重再拉开一档（2026-11-08 晚）

反馈：「重音和轻音还是分得不够清楚，细节再细一点。」

先量了一下 v7 的问题：全曲口径上重音确实铺开了（0 和 1 都有），但**相邻字之间**
差得很小——Remember 相邻 |Δ| 中位 0.18、35% 相邻对差不到 0.1；愛のシュプリーム!
中位只有 0.11、47% 差不到 0.1。画面上看就是「一片差不多」，轻重听不出来。

数据侧（`lipsync._apply_stress`）：
- 仍以「字内较响一半帧的平均」为基础，全局 p20~p90 归一化 + 邻字中位数突出度；
- 新掺入**局部名次**：在前后各 4 个字里排第几（并列算半个），权重 0.4。
  副歌整段都很响时，只看全曲分位会压成同一个值，名次能保留「这一句里谁更重」；
  权重试过 0.5，会一重一轻来回抖（相邻跳变 >0.55 的占 12%→实测抖），0.4 是平衡点。
- 相邻 |Δ| 中位：0.18→0.23、0.11→0.16；差不到 0.1 的相邻对 35%→23%、47%→35%；
  跳动 >0.55 的仍只有 12%/5%，没有变成乱抖。
- `payload["v"] = 8`，缓存门槛 `old_v < 8` 会让 v7 缓存重算。

桥侧（`bridge.js` g34）：
- 幅度：包络项 `0.45+0.80×amp` → `0.40+0.85×amp`（0.12 以下仍线性收口），
  重音项 `0.76+0.44×s` → `0.60+0.58×s`。轻音的下限从 0.76 压到 0.60，
  重音上限 1.18：同一个元音，重字比轻字多张嘴近一倍（原来是 1.6 倍）。
  实测轻音帧平均张度 0.27→0.21、重音 0.76→0.74（Remember），
  轻重比 2.8→3.5；愛のシュプリーム! 5.6→6.9。削顶帧仍只有 7%/4%。
- 速度：张口 `80ms→16ms`、收口 `175ms→65ms`（stress 0→1），嘴形平滑
  `115ms→55ms`、换形窗口 `140ms→54ms`。轻音是「飘过去」，重音是「咬下去」。
- 咬字冲量：字头 60~80ms 内 `+10%×stress²` 的过冲再落回（`exp(-local×7)`），
  轻音没有这一下。字内起伏也从固定 ±6% 改成 `±(3%+5%×stress)`——
  轻音几乎不动，重音有颤音感。
- 实测（模拟桥端 60fps）：Remember 中位张度 0.49→0.42、p90 0.96→0.92；
  愛のシュプリーム! 0.43→0.37、p90 0.85→0.81。整体略收，轻重差更明显。

页面部署：`music-groove-bridge.js` 已换成 g34，`index.html` 的
`lips4-g33` → `lips4-g34`。两首已重算成 v8，刷新页面即可。

## 口型轨 v8 + 桥 g35：按段变速 + 面部微表情（2026-11-08）

反馈：「下一句和上一句连得很近很快，嘴巴的速度会变快一点——每一段都要分析；
另外要根据嘴型或歌词做脸上的微表情，现在只有嘴巴动，很奇怪。」

### 一、按段变速（纯桥端，不需要重算口型轨）

- `lipsRates(cues)`：每个字取**前后各 3 个字**的开始间隔（IOI）中位数，
  0.45s/字 → 0（慢），0.18s/字 → 1（快），中间线性。换歌（`st.track`）时算一次，
  存在 `lips.rate[]`，和 cue 一一对应。
- 平滑速度：`speed = 1.22 - 0.50 × rate`，乘在张口/收口/嘴形三个时间常数上。
  慢段 ×1.22 更从容，快段 ×0.72 更跟手（同一句话里的重音差异仍由 stress 决定）。
- 换形窗口：`blend = 0.09 × (1.55-0.95·stress) × (1.15-0.45·rate)`
  ——快段提前换口型（窗口 70ms→68ms 级别缩短），不吃字。
- 用中位数而不是平均：一串长音里夹一个短字不会被算成「这段很快」。

### 二、面部微表情（关键：写在哪一帧）

只动嘴最像木偶。加三条很轻的通道：眉毛 `ParamBrowLY/RY`、笑眼
`ParamEyeLSmile/RSmile`、脸颊 `ParamCheek`。

- **时机**：Cubism 的 update 顺序是 motion → expression → eyeBlink → focus →
  naturalMovement → lipSync → physics → `beforeModelUpdate` → `model.update`。
  在 `beforeModelUpdate` 里用 `addParameterValueById` **叠加**，既不会被她的
  表情覆盖，也不会累积到下一帧（之后内部 `loadParam` 还原）。挂在
  `afterMotionUpdate` 上会被随后执行的表情/眨眼盖掉，写两遍又会叠两次。
- 目标值（都乘 `presence = clamp(2×amp)`，间奏/换气时自动收）：
  眉毛 `0.03 + 0.15×stress×presence`（重音抬眉）；
  笑眼 `0.04 + 0.13×bright×(0.5+0.5×stress)×presence`（亮元音 a/e 弯眼，
  o/u 收着——`LIPS_BRIGHT` 表）；脸颊 `0.12×stress×presence`（用劲）。
- 平滑 `FACE_TAU = 0.16s`；停唱、曲终、TTS 抢占都走同一支 `faceSmooth(dt,0,0,0)`，
  眉不会一直挑着。参数不存在（模型没做）就跳过，按 core 缓存一次检测结果。
- 没有歌词只有包络时也有一版弱表情（跟响度），保证「在唱」而不是死脸。

### 三、验证

写了个离线自测 `D:\ruanjian\N.E.K.O\_lips_harness_g36.js`（当时叫 g35）：用假 window/document/fetch
把 `bridge.js` 真跑起来（假时钟 + 假 coreModel + 假 LanLan1），喂一条
「前 10 字 0.6s/字 + 后 20 字 0.15s/字」的轨：
- `lips.rate` 慢段 0.00 / 快段 1.00，段边界按窗口整体翻转；
- 慢段嘴张 0.761、快段 0.821，`ParamMouthForm` 正常写入；
- 微表情三层都叠上了（brow 0.115 / smile 0.097 / cheek 0.068），
  停唱后归零、`endLipSync` 已释放；
- `RESULT: PASS`。

页面部署：`music-groove-bridge.js` 已换成 g35，`index.html` 第 385 行
`lips4-g34` → `lips4-g35`。刷新页面（Ctrl+Shift+R）即可，不用重启游戏，
也不用重算口型轨（数据侧没动）。

## 口型数据源收口：只喂干净人声 + 桥 g36 硬门限（2026-11-08）

反馈：「嘴巴是被背景音乐+人声一起驱动的，不是干净人声；背景音乐和成品声音放一起
会有误张。干净人声就在目录里，直接拿它来指挥嘴。播放时伴奏+人声一起放没问题。」

### 一、先查清楚：嘴到底跟着谁动

口型链路只有一条：`lips.json` 的 `env`（响度包络）→ 桥的 `applyLips`。
拿两首真实曲目做相关：

| 曲目 | env 与 `final/vocal_only.wav` | env 与 `final/final.wav` |
|---|---|---|
| Remember | **0.975** | 0.496 |
| 愛のシュプリーム! | **0.968** | 0.720 |

也就是说线上包络确实主要来自人声轨。**但审计出三处会漏进伴奏/混音的口子**，
全部堵掉：

1. `pipeline._build_lips` 以前在**变声之前**用「原曲分离人声」算包络。分离不干净
   时鼓/贝斯就混在 `_vocals.wav` 里——嘴巴跟着原曲伴奏动，正是「误张」。
   现在改成**变声完成后**再算，stem 用变声后的**干声**（`vocal_wav`，只有人声、
   没有伴奏、没有混响），和混音并行跑，不额外等。
2. `_lips_payload` 的兜底顺序是 `vocal_mixed → vocals → vocal_wav → final`，
   干净人声全缺时会落到 **final（成品混音）**。现在顺序改为
   `vocal_wav → vocal_mixed → vocal_clean → vocals`，**永不使用 final/source**；
   一个都没有就不算包络（宁可不张嘴，也不跟伴奏张嘴）。
3. `ms_lips_status` 以前直接拿 `final` 建侧车口型（`final/final.lips.json`），
   诊断结果和真正播放时不是同一份数据。现在走 `_lips_payload` 同一条路径，
   并返回 `stem` / `stem_kind` / `env` 供核对；旧的混音侧车已删除。

缓存版本 v9：payload 带 `stem_kind`，`v<9` 或 `stem_kind` 不在干净人声列表里的
旧缓存一律重算（Already 重算 Remember / 愛のシュプリーム!）。

### 二、误张：包络门限从「线性轻压」改成「硬斜坡」

以前 `loud<0.12` 只是线性乘 `loud/0.12`——`loud=0.1` 时嘴还能张开 ~30%，
间奏里人声轨的残留鼓点/混响尾巴就会让嘴跟着伴奏微微开合。
桥 g36 改成：

```js
gain *= smoothstep(0.06, 0.18, loud);   // 0.06 以下完全闭嘴，0.18 以上正常
```

微表情的 presence 用同一条斜坡（`0.08→0.26`），间奏不再「脸还在演」。
实测两首：cue 时刻包络 <0.08 的占 20% / 3%（愛のシュプリーム! 集中在
0–20s、80–100s、200–220s、250–270s 的间奏段），这些段现在完全闭嘴；
真实唱句的 cue 峰值中位 0.65 / 0.84，门限不误伤。

### 三、播放

播放仍然用 `final.wav`（伴奏 + 人声），没有改——要的就是成品一起放；
只有「指挥嘴」的信号换成干净人声。

### 四、验证

- 插件已热重载；两首曲目的 lips.json 已按 v9 重算（`stem_kind=vocal_mixed`）。
- `ms_lips_status(track_id=…)` 现在会返回 `stem`、`stem_kind`、`env`。
- 离线自测 `_lips_harness_g36.js`：假时钟 + 假模型跑真桥，喂一条
  6.0–7.0s 包络≈0 的间奏，断言此时 `mouth=0.016`（闭嘴），
  其余慢/快段/微表情/停唱回收全部通过，`RESULT: PASS`。
- 部署：`music-groove-bridge.js` → g36，`index.html` 的 `lips4-g35` → `lips4-g36`。

## 唱歌表情升级：桥 g37（2026-11-08）

用户反馈「脸是僵的，只有嘴在动」。排查发现两件事：

1. **线上跑的根本不是新桥**：`ms_lips_status` 里 `client_version` 一直是 33，
   说明主窗口从 g33 之后就没刷新过，g35/g36 的表情代码一次都没执行。
   部署方式不变：拷 `music-groove-bridge.js` + 改 `index.html` 的 `lips4-gNN`，
   然后 **Ctrl+Shift+R**。
2. 即使加载了，旧表情幅度也太小（眉 0.03~0.18、笑眼 0.04~0.17），
   在 ±1 的表情参数上几乎看不出来。

### 一、表情改成「乐句级」的四条通道

桥仍在 `beforeModelUpdate` 用 `addParameterValueById` 叠加上去（不覆盖她
自己的表情/眨眼/视线），幅度按 `presence`（干净人声包络 0.06→0.20 斜坡）
开关，间奏/停唱自然落回 0：

| 通道 | 参数 | 目标值 |
|---|---|---|
| 眉毛 | `ParamBrowLY/RY` | `presence*(0.10+0.45*stress+0.10*bright) + 0.18*onset + 0.08*dyn`，上限 0.85 |
| 笑眼 | `ParamEyeLSmile/RSmile` | `presence*(0.16+0.50*bright)*(0.35+0.65*presence) + 0.26*dyn*presence`，上限 0.90 |
| 眯眼 | `ParamEyeLOpen/ROpen` | `-0.18 * smile`（眨眼写 0 时钳在 0，不会睁不开） |
| 脸颊 | `ParamCheek` | `presence*(0.10+0.38*stress)`，上限 0.60 |
| 眉角 | `ParamBrowLAngle/RAngle` | `presence*(0.08+0.26*stress) + 0.10*onset`，上限 0.60 |

- `dyn` = 包络 0.35s 平滑后的「乐句响度」；`onset` = 从「没在唱」进入
  「在唱」那一刻的脉冲（换气后开口挑一下眉）。
- 四条通道时间常数不同（眉 0.14s / 笑眼 0.34s / 脸颊 0.28s / 眉角 0.20s），
  唱完一句笑眼会比眉毛晚一点落下来。
- 参数不存在就跳过（按 core 缓存支持表）；`face` 掩码通过 ping 上报。

### 二、头/身体的轻摆动（律动没接管时）

插件自己放歌时「为她所听」的律动不一定在跑（`blend≈0`），头完全不动，
远看就是「只有嘴动」。桥现在用人声包络 + 唱速给一点很轻的摆动：

- `ParamAngleX`：±(3.8+2.6*rate)° 慢摆；`ParamAngleZ`：±(1.8+1.4*rate)°；
- `ParamAngleY`：句首 -3° 点头 + 响乐句 -1.1°；
- `ParamBodyAngleX/Z`：±1.2° / ±2.0°。
- 律动在跑（`blend>0.15`）时整段跳过，避免两套动作拽同一个头。

### 三、监听顺序与兜底

- 主程序也会在 `beforeModelUpdate` 挂常驻表情监听；EventEmitter 按注册顺序
  调用，所以桥每 3 秒把自己的监听挪到队尾（有 `off` 才挪，避免重复挂）。
- 有的运行时 `.on` 存在但从不 `emit`：桥记录事件最后一次触发时间，
  超过 1 秒没触发就在 rAF 里补写（`faceStats.eventAt`）。
- ping 现在上报 `face`（9 位支持掩码）、`fw`（表情写入帧数）、`fh`（事件是否
  在触发）、`fv`（眉,笑眼,脸颊,眉角）。`ms_lips_status` / `/hello` 会带回来，
  以后排查「表情没生效」先看 `client_version` 是不是 37、`face` 掩码是否为
  `111111111`、`fw` 有没有增长。

### 四、验证

- 离线自测 `_lips_harness_g37.js`：慢段表情
  `brow=0.47 smile=0.59 cheek=0.34 angle=0.24`、头/身体
  `AngleX=0.46 BodyZ=-1.62`；间奏嘴 `0.016`、表情归零；停唱全部回收。
  `RESULT: PASS`。
- 部署：`music-groove-bridge.js` → g37，`index.html` 的 `lips4-g36` → `lips4-g37`。
- 旧参考：g36 的误张口型修复见上一节。

## 修 g37 的「打架」：桥 g38（2026-11-08）

用户反馈：刷新后脸和整个身体都在跟默认待机动画打架，一卡一卡、一抽一抽。

### 一、根因

g37 新加的两类写入正好踩在主程序自己的参数所有权上：

1. **头/身体角度**（`ParamAngleX/Y/Z`、`ParamBodyAngleX/Z`）。这组参数每帧被
   好几方写：待机 motion（绝对）、主程序的随机视线（绝对）、Cubism 的 focus
   （加性，最多 ±30°）、呼吸（加性）、以及物理（把头角读进去算头发/身体）。
   桥再在 `beforeModelUpdate` 加一层，就变成「待机 + 视线 + 呼吸 + 桥」四方抢
   同一根骨头；而且叠加是在 `physics.evaluate()` 之后，物理看不到这次角度变化，
   身体/头发不跟——远看就是脸和身体各动各的。
2. **`ParamEyeLOpen/ROpen`**。主程序在 `coreModel.update` 里每帧按眨眼状态
   **绝对写**这两个参数（`_updateEyeBlink`），时机在 `beforeModelUpdate` 之后，
   桥的叠加要么被整段盖掉（白写），要么在 motion 接管眼睛的帧里和待机眨眼抢。
3. **表情监听顺序**。主程序换表情时会重新注册自己的 `beforeModelUpdate`
   监听（绝对写入），落到桥后面，那一帧桥的眉/笑眼/脸颊被整段抹掉；旧桥每 3 秒
   才挪一次队尾，于是脸上出现「有表情→没表情」的周期跳变。

### 二、验证（真 Cubism 运行时）

用 vendor 里的真 SDK + 真模型（`model/mkw/小侦探`）跑了离线探针：
模拟待机每帧绝对写 `ParamAngleX`，桥在 `beforeModelUpdate` 加 6。结果
`maxLeftoverMinusPrevMotion = 0` —— 叠加量在帧尾 `loadParameters()` 里被还原，
**不会累积**。也就是说 `addParameterValueById` 这个写点本身是安全的；
打架只来自「写的是别人也在写的参数」。

### 三、g38 的改法

- **删掉头/身体的全部写入**（`applyMotionLite`、`sing` 状态）。头身交还给
  待机动作/视线/物理；「为她所听」的律动仍在（`MusicGroove`），不受影响。
  唱歌时的生动感只靠表情，不再抢骨头。
- **删掉 `ParamEyeLOpen/ROpen` 的叠加**，眼睛完全交给主程序眨眼/视线。
- **表情监听每帧挪到队尾**：在 `afterMotionUpdate` 里 `off + on` 一次
  （只做数组操作）。同一个 `InternalModel.update` 里 `afterMotionUpdate` 和
  `beforeModelUpdate` 之间不会插入别的 JS 任务，所以这一帧的
  `beforeModelUpdate` 里桥一定最后写，不会被主程序的绝对表情盖掉。
- 表情仍是 `addParameterValueById` 叠加（眉/笑眼/脸颊/眉角），间奏/停唱归零；
  `face` 诊断掩码从 9 位变 7 位（去掉了眼开合）。

### 四、验证与部署

- `_lips_harness_g38.js`：慢段表情 `brow=0.47 smile=0.59 cheek=0.34 angle=0.24`、
  快段 `0.49/0.59/0.32`、间奏嘴 `0.016` 且表情归零、停唱无写入；
  另外断言「不写 ParamAngle*/ParamBodyAngle*/ParamEyeLOpen」以及
  「主程序后注册监听时桥仍最后写」。`RESULT: PASS`。
- 部署：`music-groove-bridge.js` → v38，`index.html` 的 `lips4-g37` →
  `lips4-g38`；主窗口 **Ctrl+Shift+R** 后 `ms_lips_status` 的
  `client_version` 应为 38。

## 把全身接回来：桥 g39（2026-11-08）

g38 把头和身体交回待机之后，用户的反馈很直接：「模型好像只有嘴巴能动，
眼睛微微动一下，整个身体……把整个身体全部控制起来，让他有模有样感觉像唱歌」。
问题是 g37 的「打架」也必须不能复现。所以 g39 不是把 g37 的叠加写回来，
而是换写点和写法。

### 一、为什么 g37 会打架、g39 为什么不会

拆开 Cubism 4 `InternalModel.update` 的顺序看得很清楚：

```
motionManager.update   ← 待机动作、游戏自己算的视线（绝对写 ParamAngleX/Y）
emit afterMotionUpdate
saveParameters()
expression → eyeBlink → updateFocus（往角度上「加」视线偏移）
        → updateNaturalMovements（往呼吸/角度上「加」呼吸）
        → 物理 evaluate（读角度，算头发/衣摆）
        → emit beforeModelUpdate
coreModel.update() → loadParameters()
```

g37 在 `beforeModelUpdate` 用 `addParameterValueById` 往头上加量：物理已经
算完了（头发不跟），而待机动作、视线、呼吸每一帧都在写同一组参数，两边
各自加各自的 → 一卡一抽。

g39 改成在 **`afterMotionUpdate`** 用 `setParameterValueById` **绝对写**：
- 待机动作和视线已经写完，我们整个替换掉，不是叠加；
- 写完之后 `saveParameters`、物理、眨眼都在这个姿态上继续算，
  头发/衣摆会跟着头走；
- 每帧都是「按目标重新赋值」，不会跨帧累积。

这正是主程序「为她所听」（`MusicGroove.applyToModel`）用的写法和写点。

### 二、驱动信号

全部来自干净人声的口型轨（`lips.json`：`env` 干声包络 + `cues` 的
`stress` 重音 / `rate` 唱速 / 元音亮度），和嘴、表情共用同一份数据，
所以身体和嘴一定是同一拍：

- `want = presence`（包络 0.06→0.20 的软斜坡，间奏=0）；
- `mix` 0.35s 起势 / 0.30s 收势，`mix < 0.004 && want == 0` 时**一次都不写**，
  待机动画全权接管；
- 摇摆相位 `phase += dt·(0.225 + 0.105·rate)·2π`（慢歌约 4.4s 一个来回，
  快歌约 3.0s），副摇摆半速，避免机械左右；
- 每个新字（`lipsCueIndex` 变化）给一下点头脉冲，0.17s 衰减，
  深度 `0.30 + 0.55·stress`；乐句开头（`faceDyn.onset`）换歪头方向。

写入（按参数自身半量程换算，换模型幅度观感一致）：

| 参数 | 幅度 | 含义 |
| --- | --- | --- |
| `ParamAngleX` | ±6.8° × (0.6+0.4·loud) | 跟着乐句左右摆头 |
| `ParamAngleY` | −(2.0+1.5·loud+4.8·hit)° | 一直微微低头，重音点头 |
| `ParamAngleZ` | ±(2.4+1.4·loud) + 0.9·drift | 歪头，逐乐句换边 |
| `ParamBodyAngleX` | ±3.2°（相位落后 0.55rad） | 上身横摆 |
| `ParamBodyAngleY` | −nod×0.18 | 胸口跟着点头起伏 |
| `ParamBodyAngleZ` | ±1.4°（慢副摆） | 侧倾 |
| `ParamBreath` | 0.47+0.06·loud | 呼吸基线（自然呼吸在其上加） |

眼睛开合仍然一次都不写（主程序 `_updateEyeBlink` 每帧绝对写，眨眼照常）；
眼球也不写（视线归 `updateFocus`）。

### 三、和「为她所听」的律动不抢

`applyNow` 里先看 `blend`：频谱律动在跑（`blend > 0.15`）时全身交给
`MusicGroove.applyToModel`，唱歌姿态的 `mix` 直接归零；律动不在时
（插件没开、48765 没数据）由 g39 的唱歌姿态驱动。两者不会同时写同一组参数。

### 四、验证与部署

- `_lips_harness_g39.js`（g38 的用例全部保留，改动见下）：
  慢段 `mix=1.00`、`AngleX=4.09 / AngleY=−4.16 / AngleZ=−4.24 /
  BodyX=0.57 / BodyY=−0.75 / BodyZ=−1.34 / Breath=0.51`；
  快段 `AngleY=−5.31`；间奏 0.5s 时 `mix=0.17`、点头只剩 0.38°（收势），
  1.9s 后**零写入**；停唱零写入；并断言头/身体参数全部走
  `setParameterValueById`、`added` 里没有它们（不叠加），眼睛开合不碰，
  以及表情监听仍在队尾。`RESULT: PASS`。
- 部署：`music-groove-bridge.js` → v39，`index.html` 的 `lips4-g38` →
  `lips4-g39`；主窗口 **Ctrl+Shift+R** 后 `ms_lips_status` 的
  `client_version` 应为 39，另加了 `sm`（唱歌姿态权重）与 `sw`（参数写入计数）
  两个诊断字段。

## 照「为她所听」的写法修抽动：桥 g40（2026-11-08）

g39 上线后用户看到的是「人物一直一抽一抽」。先去看「为她所听」
（`music_perception/live2d/`）真正怎么驱动模型，和 g39 比有两点不同：

### 一、参考实现（stage.html + music_groove.js）

```js
model.internalModel.motionManager.stopAllMotions();      // 先停掉待机动作
...
function apply() { MusicGroove.applyToModel(core, frame, { gain: 1, breathBase: 0.45 }); }
model.internalModel.on('afterMotionUpdate', apply);
model.internalModel.on('beforeModelUpdate', apply);       // 渲染前再写一遍
```

而 `MusicGroove.update` 里所有目标值都先过一遍低通：

```js
var SMOOTH_TAU = 0.06;
var alpha = 1 - Math.exp(-dt / SMOOTH_TAU);
for (var k in target) s[k] += (target[k] - s[k]) * alpha;
```

也就是说：**（1）目标值先滤波再写；（2）afterMotionUpdate 和
beforeModelUpdate 各写一次。**

### 二、g39 错在哪

1. **目标值是阶跃的**。每换一个字 `sing.hit` 直接跳到 `0.30+0.55·stress`，
   `ParamAngleY` 一帧里从 −3.5° 掉到 −8.3°，再按 0.17s 衰减；快段每 0.15s
   一个字，就是每秒 6~7 下「一下一下」的抽。乐句换边时 `tiltDir` 符号硬翻，
   `ParamAngleZ` 也是一帧甩 7.6°。
2. **只在 afterMotionUpdate 写**。SDK 在它和渲染之间还会跑
   `updateFocus`（视线加法）、`updateNaturalMovements`（呼吸加法），
   任何一帧被加一点，下一帧又被我们写成原值，就是「偏一下、弹回来」。

### 三、g40 的改法

- 头/身体/呼吸的目标值全部先过低通（0.09s，参考值 0.06s；歪头换边 0.22s
  更慢，像重心移过去），再按 `sing.mix` 从当前值过渡写入。
  单帧位移从「一帧 4°」降到 0.3° 以内。
- `beforeModelUpdate` 里把这一帧算好的值**原样**再写一遍（不再 blend），
  渲染前最后一刻的姿态就是我们的值。这和 stage.html 的双写一致。
- 收势：`mix` 0.30s 衰减，<0.01 直接归零、`sing.out` 清空，一次都不写。
- 诊断加 `sj`：2 秒内头部单帧最大位移（度）。正常应该 <1°，
  `sj` 明显大于 1 就说明还有别的东西在拽她。

### 四、验证与部署

- `_lips_harness_g40.js`：g39 用例全保留，新增
  「beforeModelUpdate 原样再写一遍（值一致到 1e-6）」、
  「快段逐帧 1 秒：头 ΔY≤0.27°、ΔZ≤0.01°」、「内部诊断 sj=0.19°」、
  间奏/停唱零写入。`RESULT: PASS`。
- 部署：`music-groove-bridge.js` → v40，`index.html` 的 `lips4-g39` →
  `lips4-g40`；主窗口 **Ctrl+Shift+R** 后 `client_version` 应为 40。

## 大节奏与所有衔接的过渡：桥 g41（2026-11-09）

再读一遍 `music_groove.js` 里「大节奏」的处理方式，核心只有一句：
**任何换段、换档、换方向，都不是直接赋值或指数追赶，而是「从当前值出发、
固定时长 smoothstep 走到新值」，两端速度都是 0。** 具体：

| 变化 | 参考实现的处理 |
| --- | --- |
| 歌曲段落 intro/build/verse/chorus/outro | `SECT_FADE = 3.0` 秒 smoothstep，改 amp/rate/body/move/accent |
| 舞段（摇头/点头/身体） | `MOVE_FADE = 1.5` 秒 smoothstep，只在整拍换 |
| 节奏档 head/sway/groove | `PROF_FADE = 1.2` 秒 smoothstep + 0.6s 防抖 + 1.5s 保持 |
| 风格 / 左右方向 | `STYLE_FADE = 1.6` 秒 smoothstep（方向从 `-1` 拧到 `+1`） |
| 拍点重音 | `swell(t, 0.035, 0.19)`：先 35ms 起手再收，峰值归一 |
| 突发重音 | `swell(t, 0.05, 0.26)`，且两次之间 ≥0.25s |
| 大甩头点缀 | 三段 smoothstep 包络（惯性小推 → 反向大甩 → 收回），只在小节头起手 |
| 摆幅 | 单独 0.65s 慢平滑，不跟单拍起跳 |
| 限幅 | `softLim`（tanh）而不是硬 clamp——硬 clamp 会切出平顶，离开时像被推一把 |

g40 虽然给目标值加了 0.09s 低通，但**目标本身还是阶跃的**，只是被滤软：
每换一个字 `hit` 抬到峰值、快段 6.7Hz 重复，歪头换边 0.22s 就翻过去。
g41 按参考逐条改：

1. **重音改成包络**：`swell(t, 0.045, 0.20)`，不再阶跃+指数衰减；
   并且限制最小间隔 `0.42 + 0.22·rate` 秒（快段约 0.64s 才点一下头），
   快段重音幅度再收 40%。快段 0.15s 一个字不可能每个字都点。
2. **连续点头**：新增 `nodPh`（0.85~1.35Hz 余弦），基础律动是连续的，
   字的重音只是加深一点——和 MusicGroove 的 `bobRaw` 同一思路。
3. **歪头换边**：`tiltDir` 只改目标，`tiltS` 用 1.4 秒 smoothstep 从
   当前值走到新值；至少 3 秒才换一次（以前 0.9s，慢歌会一直倒头）。
4. **大节奏**：`sing.pow` 是乐句响度的 2 秒慢包络，
   `powerK = 0.70 + 0.55·clamp((pow-0.25)/0.55)` 缩放整体幅度；
   身体参与度 `bodyMix` 起 1.2s / 落 0.9s。副歌进来是渐渐变猛。
5. **起唱/收势**：起 0.8s、落 0.5s；换气 0.5s 内保持最后一句的姿态
   （句与句之间不会一收一放）；收势到 8% 直接归零交回待机。
6. 诊断新增 `sp`（大节奏力度包络）与 `sb`（身体参与度）。

### 验证（`_lips_harness_g41.js`）

逐帧量「头单帧走了多少度」，四段全部连续：

- 慢段：`ΔX≤0.06° ΔY≤0.14° ΔZ≤0.00°`
- 重新开口 + 歪头换边（8.0–9.7s）：`ΔX≤0.16° ΔY≤0.14° ΔZ≤0.08°`
- 快段（0.15s/字，1 秒）：`ΔX≤0.20° ΔY≤0.19° ΔZ≤0.02°`
- `sj`（内部 2 秒滑窗）`0.17°`
- 间奏：6.5s 仍保持（mix 0.94）→ 7.4s 已收到 0.16 → 7.9s 一次都不写
- 停唱 2 秒后 mix=0、零写入、表情归零、已 release 嘴

### 部署

`music-groove-bridge.js` → v41，`index.html` 的 `lips4-g40` → `lips4-g41`；
主窗口 **Ctrl+Shift+R** 后 `client_version` 应为 41。

## 身体上下抽动：把「为她所听」的律动整个搬过来：桥 g42（2026-11-09）

g39~g41 一直在修「人声驱动的全身」，但用户看到的**身体上下抽动**一直没消失。
根因不是过渡不够软，而是**架构上有两个驱动源**：

1. `applyNow()` 里 `if (engine && blend > 0.15) 用 MusicGroove`，否则
   `applySing()`——blend 在阈值上下抖一下（能量门槛、`isActive()` 翻转），
   两个驱动源就交替接手，`ParamBodyAngleY` 一上一下。
2. 就算只有 `applySing`，它自己也在 1.35Hz 写
   `ParamBodyAngleY = -(rawNod*0.18)`，而 `rawNod` 跟着每个字的重音走——
   人声包络本来就一顿一顿，它驱动身体必然「抽」。
3. `beforeModelUpdate` 只重写了「人声全身」的那 7 个参数（`singReassert`），
   律动接管时没有对应的重写，晚到的 `updateFocus`/`updateNaturalMovements`
   加法每帧把姿态拽偏一点。

### g42 的分法（就是「为她所听」的分法）

| 通道 | 信号源 | 写入者 |
| --- | --- | --- |
| 嘴（开合/嘴形） | 干净人声口型轨（cues + env） | `applyLips`（主程序口型通道） |
| 表情（眉/笑眼/脸颊/眉角） | 同一份干净人声 | `applyFace`（叠加） |
| 头/上身/呼吸 | **成品混音**（主窗口自己播的 `<audio>`，带伴奏） | `MusicGroove.applyToModel`（绝对写入） |

- `lips.audioEl` 在创建时就设 `crossOrigin = 'anonymous'`（必须在 `src` 之前；
  服务端 `/song.wav` 已对 200/206 都发 `Access-Control-Allow-Origin: *`），
  否则 `AnalyserNode` 拿到被污染的静音流，歌会哑。
- 新增 `mixAudio`：`AudioContext` → `AnalyserNode(fftSize 2048, smoothing 0.72)`
  → `createMediaElementSource(audioEl)` → `analyser → destination`（分析之外仍然出声）。
  取特征的方法与 `stage.html` 的本地音频模式逐行一致：三段能量、总能量 ×2.6、
  谱重心、`flux > 0.06` 且间隔 >220ms 的起音包络；BPM 用 4 秒慢 EMA 估（60~180）。
- **AudioContext 不是 running 就不建图**：挂在 suspended 的 context 上等于把声音
  送进一个不输出的节点。没起来就 1 秒后再试，这期间歌照常播。
- `tick()` 里：本地成品在放 → 用 `mixFeatures(dt)`，`blend` 目标固定 1
  （静音段由引擎自己收势，不来回切）；否则退回 48765 系统回环的 `features(dt)`。
- `applyNow()`：`grooveActive()` 才 `grooveApply(core)`，**没有律动数据就一次都不写**，
  整段交回待机。`applySing`/`sing`/`swell` 全部删除（不再有第二个驱动源）。
- `beforeModelUpdate` 改成 `grooveReassertListener`：把同一帧的律动姿态原样重写
  （和 `stage.html` 双写一致）。眼球参数 `ParamEyeBallX/Y` 先存后还——
  律动会写它们，但在这个位置写会盖掉主程序的视线跟随。
- 诊断改为 `mix`（1=本地混音分析 / 0=回退回环 / -1=建图失败）、`me`（混音能量）、
  `mb`（估的 BPM）、`mf`（分析帧数）、`bl`（律动权重）。

### 验证（`_lips_harness_g42.js`，接真引擎 music_groove.js）

- 唱歌但没有混音可分析：身体参数**零写入**（这正是以前抽动的场景）。
- 嘴/表情不受影响：`mouth≈0.76`、`brow 0.47`、`smile 0.59`。
- 成品开始播：`crossOrigin` 正确、分析器出帧、`blend→0.99`；
  头/身/呼吸全是绝对写入，无叠加；没有碰眼开合。
- `beforeModelUpdate` 重写值与 `afterMotionUpdate` 完全一致；眼球保持原值。
- 逐帧连续性：头 `ΔY≤0.20°`、身体 `ΔY≤0.00°`；跨段 6 秒 `ΔY≤0.23°`。
- 停唱 2.5 秒后：零写入、表情归零、已 release 嘴。

### 部署

`music-groove-bridge.js` → v42，`index.html` 的 `lips4-g41` → `lips4-g42`；
主窗口 **Ctrl+Shift+R** 后 `client_version` 应为 42。

## 2026-11-09 06:05：口型/表情从 g43 回退到 g42

- **g43 是 05:05 中断会话留下的未完成版本**：改了长音张口（hold 判定 + 压平弧线）、
  用 `/stem.wav` 实时分析（vox）重写了表情公式（energyMix / brightMix / sect /
  beatPulse / vib 进眉、笑眼、脸颊、眉角四条通道），`__init__.py` 多传 `stem_path`
  并把 lipsync 缓存提到 v10。没有 `_lips_harness_g43.js`，也没有本节记录。
- 用户反馈脸部不对 → 整套回退到 04:54 的 g42 快照
  （`_backup_singface_20261109/`：`__init__.py` / `bridge.js` / `lips_server.py` /
  `lipsync.py`）：插件四件套 + 游戏侧 `music-groove-bridge.js`，
  模板 `lips4-g43` → `lips4-g42`，两个插件重载。
- **g43 完整留档**：`_backup_v43_20261109/`（含当时的 `templates/index.html`）。
- 回退后 `/hello` 不再有 `vox` / `stem` / `sect` 字段（v42 服务端）；
  主窗口 **Ctrl+Shift+R** 后 `client_version` 应为 42。
- 注意：仓库 `music_synth-dev` 里从来没有 g42/g43 这几件（`lips_server.py` 还是
  11-07 旧版、`__init__.py` 缺 `_start_singing` 等），仓库 DEV_NOTES 也没有 g37–g42 记录。

## 0.2.34：把丢失的面板 UI 补回源码（2026-11-09）

`ede806c` 用 `ui/*.tsx` 重新打包面板时，把只存在于已编译 `html/panel.html` 里的
UI 一并覆盖掉了（源码里从来没有这些片段），于是两处界面退回旧版：

- **翻唱声音训练 · 训练底模选择（低 / 中 / 高）** restored：`pageTrain.tsx` 补回
  `quality` 状态机、`ms_base_status` / `ms_base_inspect` / `ms_base_download` 调用、
  三档按钮、检查底模按钮、底模就绪状态与下载进度条，`ms_train_start` 重新带
  `quality`（low / mid / high → `ddsp6.3_6x512_6x512` / `..._6x1024` / `..._10x2048`）。
- **原创歌曲页布局重做** restored：`pageCreate.tsx` 换回双栏版式
  （左「歌曲内容」，右「生成模型 / 声音与后处理 / 开始」），模型卡片支持
  `ms_ace_delete_model` 删除已下载模型（移进回收站）和下载进度条，高级选项补回
  `候选数（1-6）` 并随 `ms_ace_generate` 传 `candidates`；表单与高级选项写入
  `localStorage["ms_create_ui_v2"]`，切页/重开插件后仍在。
- 同时修正 `postEngine` 默认值为 `ddsp`、候选数默认 4。

教训：面板只有 `ui/*.tsx` 是源码，`html/panel.html` / `static/index.html` 是产物；
任何直接改产物的补丁都会在下一次 build 时丢失。

## 0.2.34 发布：修复下载依赖（2026-11-09）

市场更新说明：修复下载依赖。

下载/依赖链路（已实测：本地 HTTP 模拟截断下载、ms_setup_check ready=True、ms_dl_diag 跑通）：
- 截断下载不再误判成功（按已下字节数校验完整性，抛 TruncatedDownloadError）
- ddsp_deps 依赖校验上线：缺依赖先装、装不上明确报错而非死等编译
- disk/cuda 空间语义修正；死代理自动重探，未开代理时强制直连（runtime.proxy_alive；
  lyrics_fetch 同一套代理判定，避免 10061）
- 下载并发闸门 _setup_lock、慢速守卫、失败 detail 带 missing_after
- 无法自动安装时报错给出路径；新增 ms_setup_redownload 重新下载入口
- 未复跑：_install_dlc_model 900s 超时、_pip 代理重试、坏 venv 迁移全流程

面板 UI 补回源码（ui/pageTrain.tsx 低/中/高底模档位、ui/pageCreate.tsx 双栏原创页），
产物与源码一致重建（panel.html 481,091 字节）。口型链路已在 b87b9cc 回填源码
（口型轨 v5–v9 + 桥 g29–g42）。
