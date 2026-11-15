# 爱奇艺本地 QSV 转换工具

将爱奇艺 Windows 客户端已下载、且本机授权有效的 QSV 批量转换为 MP4。下载由客户端完成；本工具读取本地文件和客户端下载记录，保留输入目录层级、历史成品及每次运行记录。

转换时保留音视频原编码和时间戳，使用 FFmpeg 无损封装；候选 MP4 必须通过完整视频及全部音轨解码、帧数检查和复制校验，才能发布为正式成品。源 QSV 和客户端数据库均不改写。

## 支持范围

- Windows x64、64 位 Python；项目默认使用 Python 3.12 环境。
- QSV 容器版本 2、授权版本 5，视频段连续，带独立 `amp4` 音轨。
- 单节目 TS 中唯一的 AVC/H.264 或 HEVC/H.265 视频轨；按分段标记处理加密或明文内容。
- 自动加载 [本机适配配置](configs/native_v5.yaml) 中客户端目录下最新版本的 DLL；函数位置需与 DLL 兼容，并具有可用证书和有效授权。客户端升级后若接口不兼容，需重新分析并调整配置。

其他容器或授权版本、非连续视频段、TS 包级加密及不支持的流结构会明确报错。研究目录中的其他格式资料不代表已接入正式转换。

## 安装与首次运行

以下命令使用 **Windows PowerShell**，相对路径均以本工具根目录为基准。

### 1. 准备 Python 依赖

先激活项目使用的 Python 环境，使下文 `python` 指向该环境。VS Code 工作区使用的解释器以父目录 [工作区文件](../002_获取爱奇艺视频.code-workspace) 的 `settings.python.defaultInterpreterPath` 为准。

```powershell
Set-Location -LiteralPath "D:\VSCode\personal_tools\002_获取爱奇艺视频"
python -m pip install -r ".\requirements.txt"
```

正式程序的 Python 第三方依赖见 [requirements.txt](requirements.txt)。本机爱奇艺客户端、DLL、证书和 FFmpeg 可执行文件需另行准备；研究依赖位于 `research/requirements.txt`，运行转换无需安装它们。

### 2. 检查配置

编辑 [configs/config.yaml](configs/config.yaml)，至少核对以下内容：

1. `input_root` 指向本机客户端下载目录；`input_path` 选择要扫描的子目录。
2. `download_guard.database_path` 指向该客户端实际使用的下载状态数据库，其记录名称、完成状态和 XML 编码与配置一致。
3. `output_path` 和 `work_dir` 指向可写目录，并留有成品、中间媒体所需空间。
4. `native_profile` 指向适用的本机适配 YAML；在该文件中核对客户端目录、DLL 文件名和证书路径。
5. `ffmpeg_path` 指向可运行的 FFmpeg；按设备能力设置 `decode_verification.cpu` 和 `decode_verification.gpu`。

所有路径、设备开关、缓冲大小、超时和日志参数均以实际 YAML 为准，本文不复制配置中的机器路径和参数值。

### 3. 启动批次

```powershell
python ".\decode_qsv.py" --config ".\configs\config.yaml"
```

主入口只接收必填的 `--config` 参数，输入范围和转换行为均在 YAML 中调整。查看命令行帮助：

```powershell
python ".\decode_qsv.py" --help
```

启动后终端打印本机实时日志地址和逐项进度；运行结束打印 JSON 汇总及离线 HTML 报告路径。按 `Ctrl+C` 可中断，已提交的成品与记录保留，未完成项保留现场；再次运行会重新检查并复用有效成品。

## 配置说明

### 路径与输入范围

| 配置项 | 含义与解析基准 |
| --- | --- |
| `input_root` | 已存在的输入根目录，递归发现 `.qsv`，扩展名大小写不敏感。 |
| `input_path` | 目录列表。空列表扫描整个输入根目录；非空时扫描所选目录及其子目录，重叠选择自动去重。相对路径以 `input_root` 为基准，解析后必须位于根目录内且已存在；不接受单文件或通配符。 |
| `output_path` | 成品根目录，保留源文件相对 `input_root` 的目录层级。 |
| `work_dir` | 批次工作根目录，按每次运行、每个视频分开保存中间媒体、日志及报告。 |
| `native_profile` | 本机适配 YAML 路径；其内部 `dll_root`、`cert_path` 相对该适配文件解析。程序按数字版本号选择 `dll_root` 下最新版本目录中的 `dll_name`，缺少 DLL 时不回退旧版本。 |
| `ffmpeg_path` | FFmpeg 可执行文件路径，用于封装、完整解码验证和媒体信息读取。 |

`--config` 的相对路径以命令执行目录为基准。主配置中的上述路径及 `download_guard.database_path`，除 `input_path` 外，相对路径均以主配置文件所在目录为基准。路径支持 `~`，不会自动展开 `$env:...`、`%...%` 等环境变量；YAML 中建议使用单引号或正斜杠表示 Windows 路径。

### 下载状态保护

程序以 SQLite 只读方式查询 `download_guard.database_path`，按 `completed_record`、`active_record`、`xml_encoding`、`completed_states` 核对下载状态，锁等待时间由 `database_timeout_seconds` 控制。

- 只有客户端明确登记完成的文件才进入转换。
- 同一路径出现在进行中记录时，进行中记录优先，即使完成记录中也存在该文件。
- `temporary_suffixes` 指定的下载临时文件会被排除；存在同名临时文件时，正式 QSV 也会跳过。
- 未完成或状态不明的文件计入 `pending_downloads`，完成筛选前不打开或哈希其内容。
- 数据库缺失、记录损坏或无法解析时直接报错，不依据文件大小或修改时间推测下载完成。

下载状态还会在处理和发布阶段重新核对；若状态改变，停止处理该项，后续运行再检查。

### 验证、资源和清理

| 配置项 | 行为与约束 |
| --- | --- |
| `decode_verification.cpu` / `.gpu` | 必须同时提供两个布尔开关，至少开启一个。CPU 使用软件解码；GPU 使用 NVIDIA CUDA 硬解。 |
| `copy_buffer_bytes` | 提取、哈希及复制使用的缓冲大小，单位字节，正整数。 |
| `max_pes_bytes` | 单个视频 PES 累计 TS 包缓冲上限，单位字节，正整数；超限报错。 |
| `process_timeout_seconds` | 每次媒体子进程的超时秒数，正整数；各次封装、验证等调用分别计时，并非整个批次的总时限。 |
| `keep_intermediates` | 布尔值。关闭时仅在成品和索引提交成功后删除中间媒体；日志、报告保留，失败现场也保留。 |
| `runtime.cancel_poll_interval_seconds` | 取消和服务关闭的轮询间隔，必须是大于零的有限秒数。 |
| `runtime.process_terminate_timeout_seconds` | 终止子进程后的等待时间，超时强制结束，必须是大于零的有限秒数。 |

源指纹、准备与 SDK 处理、封装、解码后的完整性检查和发布各有独立的单任务槽，每种启用设备同时解码一个视频。CPU 和 GPU 都开启时处理不同视频，优先分配空闲 GPU；不同视频的各阶段可重叠执行，解码结束后设备可在完整性检查期间接收下一视频。成品只有在源文件和输出摘要等检查通过后才发布。在途视频数量受启用设备数加两个阶段槽的上限约束。

启用 GPU 时，批次先检查 CUDA 初始化，具体视频的硬解能力在验证时检查。失败会报错，不会自动改用 CPU；无可用 NVIDIA CUDA 设备时，应在主配置中关闭 GPU、开启 CPU。

所有验证均要求正常结束、解码帧数与视频 PES 数相等，且无重复帧或丢帧。该帧数关系限于当前适配的输入结构；CPU/GPU 验证通过不代表两种解码器对所有损坏码流的检测行为完全等价。

## 成品、版本与复用

首次输出保留相对目录，仅将 `.qsv` 扩展名改为 `.mp4`。例如，下列为结构示意，并非实际配置值：

```text
input_root/
└── 剧集目录/
    └── 视频.qsv

output_path/
└── 剧集目录/
    ├── 视频.mp4
    └── video_info.json
```

每个有成品记录的输出目录独立保存 `video_info.json`，记录来源及成品指纹、历史文件、媒体信息、验证证明和各次转换结果。批次日志单独保存，成品复用依据目录索引。

- 源内容和已登记成品的校验结果一致时直接复用，无需再次转换和完整解码。
- 源内容变化时保留旧成品，新文件追加本地时间戳 `yymmdd_hhmmss`；名称冲突时再追加序号。
- 已登记成品缺失或校验不符时重新转换。首次目标位置存在未登记的同名文件时会报冲突，不覆盖该文件。
- SHA-256 以文件大小和纳秒修改时间作为缓存条件；两者均未变化的外部内容改写无法通过缓存检查发现。
- 转换期间重新计算源内容摘要，确认未改变；成品发布时先复制到目标盘临时文件并校验摘要，再以不覆盖方式发布和提交索引。
- 同一输出根目录使用 `.conversion.lock` 独占锁，避免两个批次同时写入。

旧单文件模式若在 `work_dir` 根层保留了能够证明成品有效的 `report.json`，首次访问相应源文件时会尝试迁移到当前目录布局，并在汇总中记录 `migrated`。

## 日志与运行结果

### 实时页面和离线报告

`logging.port` 控制仅监听 `127.0.0.1` 的实时服务；填写 `0` 自动分配端口，否则使用配置的固定端口。打开终端打印的地址，可按目录查看转换和验证状态、搜索文件及错误、筛选结果，并导出 HTML。运行日志由独立线程合并写盘请求，批次结束时等待最终状态写盘；写盘失败仍保留内存状态并提示。

`logging.refresh_interval_seconds` 控制页面刷新间隔；`progress_interval_seconds` 控制同阶段进度的最小写盘和采样间隔，阶段切换立即请求保存，结束时等待最后一次写盘。日志原子替换失败时按 `write_retries` 和 `write_retry_interval_seconds` 重试。

| 位置 | 内容 |
| --- | --- |
| `output_path / logging.state_file` | 历次运行的 JSON 日志。 |
| `output_path / logging.html_file` 的文件名基准 | 导出时追加本地时间戳及必要序号，生成独立 HTML，不覆盖历史报告。 |
| 各成品目录的 `video_info.json` | 成品版本索引与转换结果。 |
| `work_dir / 运行标识 / videos / 源相对路径 /` | 每个视频的 `report.json`、封装日志、解码日志、进度及媒体信息。 |
| 批次工作目录的 `gpu_probe.log` | 启用 GPU 时的 CUDA 初始化检查日志。 |

`logging.state_file` 必须是 `.json` 文件名，`logging.html_file` 必须是 `.html` 文件名，均不带目录；运行日志不能命名为保留的 `video_info.json`。实时服务随批次结束关闭，之后可打开导出的离线 HTML；报告里的媒体及现场链接仍依赖原文件位置。

### 汇总与退出码

终端 JSON 汇总包含 `total`、`completed`、`reused`、`failed`、`migrated`、`pending_downloads`、逐项 `results` 和离线报告路径 `report`。`total` 是发现时可处理的任务数，等待下载的文件另计；`migrated` 可以同时计入 `reused`，不能将所有字段直接相加。

| 退出码 | 含义 |
| --- | --- |
| `0` | 批次没有转换失败；也可能全部复用、没有匹配文件或只有等待下载的文件。 |
| `1` | 存在转换失败，或配置校验、运行过程发生异常。 |
| `2` | 命令行参数错误，例如缺少必填的 `--config`。 |
| `130` | 用户中断。 |

单个视频的转换失败会记录现场并继续处理其他任务；配置、下载数据库、输出锁或 GPU 初始化等批次级异常会终止批次。确认处理是否完整时，应同时查看汇总中的 `failed` 和 `pending_downloads`。

## 测试与辅助检查

### 单元测试

```powershell
python -m unittest discover -s "tests" -t "." -v
```

测试从实际配置复制运行参数，使用临时目录、临时下载数据库及隔离的媒体/SDK 替身，覆盖下载保护、目录映射、版本复用、流水线、取消、日志和验证规则；不代表真实客户端授权或设备解码能力已经验收。

### 检查输入格式

```powershell
python ".\tests\inspect_formats.py" --config ".\configs\config.yaml"
```

按主配置的输入范围和下载保护检查已完成文件，输出容器/授权版本分组、等待下载条目及识别错误。

### 比较 CPU/GPU 完整解码性能

```powershell
python ".\tests\benchmark_decode.py" --config ".\configs\config.yaml" --benchmark-config ".\configs\benchmark_decode.yaml"
```

从 `output_path` 中选取最新登记成功的 MP4，校验复制到工作目录后运行。任务数、重复次数、预热、并发、后端和报告位置均读取 [性能专项配置](configs/benchmark_decode.yaml)；可重复传入 `--case`，值必须是该配置 `cases` 中的 `name`，省略时运行全部组。

比较范围为完整视频及全部音轨的解码，不含 QSV 提取、SDK 处理或成品发布。包含 GPU 的组需要可用 CUDA 环境；系统可找到 `nvidia-smi` 时额外采样 GPU 使用情况。报告保留，结束后删除本次输入副本。

### 验收真实 CPU/GPU 验证

```powershell
python ".\tests\accept_verification.py" --config ".\configs\config.yaml" --benchmark-config ".\configs\benchmark_decode.yaml"
```

需要已有登记成功的 MP4、对应源 QSV、客户端下载完成记录及工作目录中的原转换报告。该脚本分别用 CPU 和 GPU 验证正常副本及截断副本，始终检查两种设备；报告写入专项配置指定的报告根目录，不改写正式 MP4 或源 QSV。

## 常见问题

| 现象 | 检查方式 |
| --- | --- |
| 没有匹配的 QSV，或出现等待下载条目 | 检查 `input_root`、`input_path`、客户端下载记录及同名临时文件；磁盘上有文件并不代表客户端确认完成。 |
| 下载数据库缺失、XML 解码失败或记录无效 | 核对 `download_guard` 的实际路径、记录名称、完成状态及编码；程序不猜测编码或修复客户端数据库。 |
| 最新客户端版本目录缺少 DLL，或升级后 SDK 调用异常 | 核对 `native_profile` 中客户端目录和 DLL 文件名；接口不兼容时需重新分析函数签名和位置。 |
| `SDK certificate/license failed` | 核对适配配置中的证书及客户端本机授权是否有效。 |
| CUDA 初始化或视频硬解失败 | 检查 `gpu_probe.log`、视频现场的 `decode.log`、FFmpeg、NVIDIA 驱动及对应格式硬解能力，或显式改用 CPU 配置。 |
| 封装、完整解码、帧数或超时失败 | 查看该视频工作目录的 `report.json`、`mux.log`、`decode.log` 和 `decode_progress.log`；结合实际媒体检查输入结构及主配置中的超时和缓冲限制。 |
| 输出目录无法独占或同名文件冲突 | 确认没有批次仍在运行，检查报错目标及目录索引；锁文件的存在本身不代表锁仍被持有。 |
| 索引或历史日志损坏 | 按报错路径检查并从可靠备份恢复；程序保留损坏记录并报错，不自动重建或覆盖。 |

## 项目结构

```text
002_获取爱奇艺视频/
├── decode_qsv.py              # 命令行入口与退出码
├── configs/                   # 主配置、本机适配与性能专项配置
├── qsv_local/
│   ├── configuration.py       # 配置校验与路径解析
│   ├── discovery.py           # 输入发现和输出映射
│   ├── download_state.py      # 客户端下载状态保护
│   ├── batch.py               # 有界流水线与结果提交
│   ├── workflow.py            # 提取、封装、验证及成功清理
│   ├── container.py           # QSV 容器解析
│   ├── native.py              # 本机 v5 SDK 接口
│   ├── transport.py           # TS 分段与视频负载处理
│   ├── verification.py        # CPU/GPU 解码命令与帧数校验
│   ├── records.py             # 目录成品索引
│   ├── migration.py           # 旧单文件成品迁移
│   ├── storage.py             # 指纹、原子写入、发布及输出锁
│   ├── process.py             # 媒体子进程和进度读取
│   ├── runtime.py             # 取消与子进程回收
│   ├── activity_log.py        # 历史日志、实时服务及离线报告
│   └── templates/             # 转换记录网页模板
├── tests/                     # 单元测试、诊断、验收与性能比较脚本
├── research/                  # 调研资料及第三方参考，非正式运行依赖
├── requirements.txt           # 正式 Python 依赖
└── README.md
```

格式研究和适配边界见 [research/formats.yaml](research/formats.yaml)，资料索引见 [research/catalog.yaml](research/catalog.yaml)。本工具工作区文件位于父目录，启动和任务中的解释器统一引用其 `settings.python.defaultInterpreterPath`。
