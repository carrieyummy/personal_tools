# 爱奇艺链接下载与本地 QSV 转换工具

通过链接清单复用本机爱奇艺客户端已登录的播放会话，将完整视频下载为 MP4，保存到主配置指定目录。客户端登录后，打开任意一集正片并暂停一次，保持客户端打开；后续其他集、其他电视剧由脚本独立请求播放资源，不需要逐集或逐剧打开播放页，也不创建客户端离线下载列表任务。

也保留 `decode_qsv.py`，用于将客户端已经下载、且本机授权有效的 QSV 批量转换为 MP4。两种入口共用 TS、本机 SDK、FFmpeg 和发布校验模块；保留原音视频编码及时间戳，完整解码视频和全部所选音轨、核对帧数和复制摘要后才发布。现有 QSV 和客户端数据库不改写。

## 通过链接下载

以下命令使用 Windows PowerShell，相对路径以本工具根目录为基准；`python` 使用下文安装说明中的工作区解释器。

1. 在客户端登录，打开任意正片后暂停，保持客户端打开。每次客户端重启后重新初始化一次。
2. 编辑 [download_list/download_list.yaml](download_list/download_list.yaml)，填写官方短链接、分享链接或普通 `/v_*.html` 播放链接。
3. 在 [configs/config.yaml](configs/config.yaml) 设置 `output_path`、`work_dir`、FFmpeg、本机 SDK 及验证设备；在 [configs/playback.yaml](configs/playback.yaml) 核对其引用的主配置、链接清单及当前客户端适配。
4. 执行下载：

```powershell
python ".\download_iqiyi.py" --config ".\configs\playback.yaml"
```

只查看按当前配置展开的正片目录，不获取媒体、不请求登录播放会话：

```powershell
python ".\download_iqiyi.py" --config ".\configs\playback.yaml" --plan
```

正式入口支持以下可选参数；完整语法见 `python ".\download_iqiyi.py" --help`：

| 参数 | 含义 |
| --- | --- |
| `--index` | 当前清单下标，从 0 开始，可重复传入；省略时处理全部链接。仍遵循该任务的全剧展开配置。 |
| `--episode` | 展开后按正整数集序过滤，可重复传入；不能把单集任务扩展成其他集。 |
| `--plan` | 只解析公开正片目录，打印任务并保存计划报告。 |

### 清单和画质

- `url` 必填；`title` 只作备注，成品命名使用官方专辑名、专辑 ID、正片集序和节目名。
- `all-parts` 的读取顺序为逐任务设置、清单全局设置、主配置 `download.all_parts`。开启时查询官方分页目录，仅包含有效正片，排除预告及花絮；关闭时仅处理链接指定集。
- `quality` 为每集原生响应声明画质组的排名。**当前正式入口仅开放 `quality: 1`（最高组）**；次高等排名的精确选档尚未适配，配置为其他排名会明确失败。同一视频同一排名的重复任务会去重。
- 分组依次读取专项配置 `playback.selection.rank_fields`；当前按分辨率档、增强档、码率档、帧率排序。同档优先保留客户端原生选中编码，再按配置的 HDR 和编码偏好排序，不按文件体积判断高低。
- 主配置 `audio.quality` 独立指定音轨排名，`1` 表示同一匹配语言的最高原生声明档位；排序字段读取专项配置 `playback.audio_selection.rank_fields`。脚本另行请求准确 AID 的完整音轨，并核对实际编码、时长及最终成品轨道，不使用清单内的默认 AAC 代替选定音轨。
- 每集严格核对原生请求及响应的 TVID、选定 VID、授权状态、完整清单和公开正片时长。档位不存在、无完整授权或所选格式未适配时明确失败，不将试看或其他档位当成成功。

“帧绮映画 / 4K”是播放档位标签，不能证明编码、位深及音轨相同。相同分辨率可能返回 HEVC Main、Main 10 或 AV1；客户端历史离线 QSV 的杜比音轨也不代表当前播放接口一定提供相同音轨。本工具选择本次原生响应中的最高视频组及最高音轨，保留原编码，实际属性写入目录索引并显示在进度网页。

### 下载、复用和输出

程序使用当前客户端播放 SDK 的独立预读请求生成资源，不切换可见播放页，不调用 `DownloadEx`。已有播放缓存是否覆盖整片不构成前提；缺失媒体由脚本自行获取。当前集也能生成新请求。

每集按实际清单内容识别 HLS 或 DASH，字段名为 `m3u8` 的内嵌内容也可能是 MPD XML。HLS 核对物理文件的 `[start,end)` 范围连续、不重复，合计与源声明大小一致；DASH 按每轨初始化和 `SegmentList` 范围获取，经本机 SDK 处理 CENC 样本；独立 AMP4 音轨按原生完整调度前缀获取字节块，再重建整轨。未提供分段时长的字节块不估算时长，最终核对完整媒体的实测时长。

完整下载按主配置 `copy_buffer_bytes` 流式写盘，内存不随整个长分段增大；MP4 单片结构处理受专项配置 `max_fragment_bytes` 限制。单集分片并发、重试、进度间隔和时长容差读取 `playback.media_download`。`media_probe.max_segment_bytes` 仅限制研究抽样。

链接模式的 `download.threads` 仅限制同时传输媒体的剧集数，来源解析及复用检查在共享处理池执行，单集仍使用专项配置的 `workers` 并发获取分片，理论分片请求上限为两者的乘积。下载完成后进入由 `download.queue_capacity_gb` 限制的等待队列，单位为 1024³ 字节；空队列允许单集超限。下载完成即释放下载名额；等待队列积压时暂停新下载，预解析缓存有界。该容量不包含下载中、等待入队、处理中及历史失败断点的磁盘占用。顶层 `max_in_flight` 统一限制解析、复用检查及后处理的总并发数，也控制本地 QSV 转换的在途数；授权处理、封装、完整性检查和发布各一个槽，CPU 和 GPU 各一个解码槽，空闲时优先使用 GPU。

完成分段按长度及 SHA-256 保存断点。重新运行会重新取得临时资源，核对稳定来源标识、分段范围及实际内容后复用完成段；临时票据变化不影响断点，来源变化使用独立工作目录。未完成文件不登记为有效段，失败现场保留。

全片解码及候选摘要均通过后先保存 `validated.json` 检查点，再取得输出目录写入权。若其他 QSV 批次正在使用同一输出根目录，发布会明确失败并保留候选；待该批次结束后重新运行，重新核对候选内容摘要即可继续发布，不重复下载和完整解码。检查点缺失时不沿用旧验证标记，候选被改写时停止复用。

成品目录及文件名复用主配置 `naming`；链接模式在电视剧文件夹前添加整剧上映日期，视频文件名继续附带实际分辨率。日期前缀由 `naming.playback_date_prefix` 配置，支持 `{release_date:%Y-%m-%d}` 等 Python 日期格式，只作用于文件夹，`series_template` 和 `episode_template` 原有值保持独立。本地 QSV 转换继续使用原目录模板。

上映日期从专项配置 `network.album_info_url` 对应的官方专辑资料中读取 `publishTime`，按 `album_date_utc_offset_minutes` 展示偏移转换为年月日；不使用单集日期、`period`、`issueTime` 或当前时间替代。专辑编号须严格匹配，日期缺失或无效时明确报错。每个新运行批次按专辑缓存一次日期，并固定本次命名设置，同一批次内不会因后续配置编辑而改变日期前缀。重启脚本会重新读取配置和专辑资料。

正在运行的旧下载批次继续使用原命名。新增日期功能不改原有模板值，不重启或停止现有进程，不迁移或重命名下载目录。新启动脚本的新成品使用日期目录；已登记的无日期旧成品仍会按来源、摘要和媒体属性校验后在原路径复用，视频保持原位，避免仅因目录前缀变化而重复下载。媒体断点身份不包含目录日期，因此既有完整分段及已验证候选可继续校验复用。

每部剧只保存一个 `video_info.json`，与 QSV 转换共用目录索引文件；链接记录按 MP4 文件名保存在 `playback_files` 分区，完整保留上映日期、来源、视频与音轨选择、全片解码及复制证明，QSV 原有分区继续保留。多集并发发布时，在同一输出锁内重新加载索引并原子提交，避免互相覆盖；索引提交失败时回收本次新副本并保留已验证候选。只复用来源一致、重新校验内容摘要并重新探测实际媒体通过的成品；已有文件不覆盖。成功后的中间媒体清理由主配置 `keep_intermediates` 控制。

正式下载启动时会在主配置 `output_path` 下合并已有的逐集 `.playback.json`：先检查全部旧记录的路径、成功证明及视频长度，原子写入各剧的 `video_info.json`，回读一致后才删除冗余逐集 JSON。记录冲突或检查失败时保留旧文件并报错；视频不复制、不移动、不重新下载。整理只处理元数据，成品复用仍执行内容摘要和实际媒体校验。`--plan` 不整理输出目录。已运行的旧批次保持原状态，完成后由下次正式运行整理其逐集记录。

可在当前下载继续运行时只读查看清单中各剧的日期和新目录名；该命令不请求播放 SDK、不启动下载、不写输出目录：

```powershell
python ".\tests\probe_release_dates.py" --config ".\configs\playback.yaml"
```

正常下载启动后自动打开本机进度网页；`--plan` 不启动网页。网页按电视剧分组，每集一行，分别展示下载百分比、字节数、速度及剩余时间，以及当前解析与保存阶段：授权还原、音轨重建、无损封装、全片验证、校验和发布。全片验证开始后显示实际使用的 CPU 或 GPU，后续阶段和离线报告保留该信息；历史记录没有设备信息时不显示。有真实进度时显示本阶段百分比，无法量化时显示阶段状态，不将内部段数当成剧集进度，也不拼出虚假的总进度。详情可打开成品及单集报告。

浏览器、刷新、端口及日志文件名读取主配置 `logging`；`open_browser` 控制自动打开，浏览器打开失败时可访问终端打印的本机地址。链接模式使用 `playback_state_file` 独立记录历史，结束后导出 `playback_html_file` 对应的带时间戳离线报告；`final_state_timeout_seconds` 限制等待网页确认终态的时间。

终端输出逐集百分比、处理状态、汇总及批次报告路径；报告位于主配置 `work_dir` 下的 `playback_downloads/`。单项失败记录后继续其余任务；失败退出码为 1，命令行错误为 2，中断为 130。按 `Ctrl+C` 停止后可重新运行复用断点。

当前链接路线适配已验证客户端的 TS AVC/HEVC、DASH SegmentList AV1/CENC 和独立 AMP4 音轨；音频按本次原生候选选择，独立 AAC 已完成真实全轨解码，杜比等编码仍须以实际返回及验证为准。验证使用主配置启用的设备空闲槽，失败时不自动切换设备。客户端模块摘要和 ABI 必须匹配专项配置，升级后需要重新验证，不能只替换摘要继续调用。

临时媒体地址、原生账号请求头及授权票据只在内存使用，不写报告。正式来源直接使用已捕获的完整原生响应，不重复重放账号请求；媒体 CDN 只使用公开媒体请求头。网络代理由 `network.use_environment_proxy` 控制。

## 播放会话与缓存研究入口

具体 ABI、样本及验证证据见 [研究记录](research/link_download_findings.md)。研究入口与正式下载共用播放模块；客户端准备条件相同，不需要打开目标集。

以下只读入口查询当前链接对应的主 PGF 缓存索引：

```powershell
python -m pip --proxy "" install -r ".\research\requirements.txt"
python ".\research\inspect_playback_cache.py" --config ".\configs\playback.yaml"
```

缓存检查程序只读访问已打开的主 PGF 索引，不读取数据库密钥。报告写入专项配置 `report_dir`，只描述索引覆盖，不证明最高画质、缓存内容校验或全片完整性。`data-qsv.db`、`data-nor.db` 尚未完整适配，不会将主索引缺少记录解释为所有缓存中均无媒体。

以下研究入口请求清单中指定链接的原生播放资源；`$linkIndex` 是当前清单下标，从 0 开始，按实际清单调整：

```powershell
$linkIndex = 0
python ".\research\probe_playback.py" --config ".\configs\playback.yaml" --index $linkIndex
```

可在该命令末尾选择添加一个媒体验证参数：

- `--sample-media`：当前用于 HLS/TS，按专项配置的 `playback.media_probe.positions` 获取前、中、后等短段，用本机 SDK 处理授权并完整解码每个样本。DASH 的结构抽样见 `tests/probe_dash_media.py`，正式入口的 DASH 整集下载不受此研究入口限制。
- `--full-media`：按任务 `quality` 和主配置 `audio.quality` 获取链接指定集的所选视频与独立音轨，核对范围和时长；无损封装、完整解码，核对视频 PES/MP4 样本数、分辨率及复制摘要后发布到主配置 `output_path`。命名复用主配置 `naming`，元数据取自官方公开资料；已有同名文件不覆盖。播放来源与验证证明保存在本剧 `video_info.json` 的 `playback_files` 分区，发布时也会先合并本剧的旧逐集记录。中间文件清理由主配置 `keep_intermediates` 控制。

两个参数互斥。研究入口应用该任务的 `quality` 排名，只研究链接指定集；不进行 `all-parts` 全剧展开，批量下载使用 `download_iqiyi.py`。

链接下载不依赖客户端离线下载目录、下载数据库或已有 QSV。下文的输入目录、下载状态保护、流水线日志和旧成品迁移说明主要用于本地 QSV 转换入口。

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
| `output_path` | 成品根目录，目录及文件名由 `naming` 模板和客户端记录决定。 |
| `work_dir` | 批次工作根目录，按每次运行、每个视频分开保存中间媒体、日志及报告。 |
| `native_profile` | 本机适配 YAML 路径；其内部 `dll_root`、`cert_path` 相对该适配文件解析。程序按数字版本号选择 `dll_root` 下最新版本目录中的 `dll_name`，缺少 DLL 时不回退旧版本。 |
| `ffmpeg_path` | FFmpeg 可执行文件路径，用于封装、完整解码验证和媒体信息读取。 |

`--config` 的相对路径以命令执行目录为基准。主配置中的上述路径及 `download_guard.database_path`，除 `input_path` 外，相对路径均以主配置文件所在目录为基准。路径支持 `~`，不会自动展开 `$env:...`、`%...%` 等环境变量；YAML 中建议使用单引号或正斜杠表示 Windows 路径。

`naming.series_template` 必须包含 `title` 和 `series_id`，分别来自客户端下载记录的 `AlbumName` 和 `AlbumID`；`naming.episode_template` 必须包含 `index` 和 `title`，分别来自记录的 `vedioorder` 和不含扩展名的源文件名。集序须为正整数、专辑 ID 须为正数字；缺失或无效时停止映射。名称中的空白和 Windows 非法字符替换为下划线，模板本身不能生成非法路径。重新核对下载状态时若命名元数据已改变，该项会跳过。

`naming.playback_date_prefix` 是链接下载专用的文件夹前缀，必须包含且仅包含 `release_date` 日期字段，可用格式说明控制年月日及分隔符；生成结果不能包含空白、路径分隔符或其他 Windows 非法字符。它不改变 QSV 的 `series_template` 或视频的 `episode_template`。

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

源指纹、准备与 SDK 处理、封装、解码后的完整性检查和发布各有独立的单任务槽，每种启用设备同时解码一个视频。CPU 和 GPU 都开启时处理不同视频，优先分配空闲 GPU；不同视频的各阶段可重叠执行，解码结束后设备可在完整性检查期间接收下一视频。成品只有在源文件和输出摘要等检查通过后才发布。在途视频数量由 `max_in_flight` 限制。

启用 GPU 时，批次先检查 CUDA 初始化，具体视频的硬解能力在验证时检查。失败会报错，不会自动改用 CPU；无可用 NVIDIA CUDA 设备时，应在主配置中关闭 GPU、开启 CPU。

所有验证均要求正常结束、解码帧数与视频 PES 数相等，且无重复帧或丢帧。该帧数关系限于当前适配的输入结构；CPU/GPU 验证通过不代表两种解码器对所有损坏码流的检测行为完全等价。

## 成品、版本与复用

首次输出按配置的专辑目录和单集文件模板生成，文件扩展名为 `.mp4`。结构如下；实际名称以主配置为准：

```text
input_root/
└── 剧集目录/
    └── 视频.qsv

output_path/
└── 根据专辑名和专辑ID生成的目录/
    ├── 根据集序和源文件名生成的视频.mp4
    └── video_info.json
```

每个有成品记录的输出目录独立保存 `video_info.json`，记录来源及成品指纹、历史文件、媒体信息、验证证明和各次转换结果。批次日志单独保存，成品复用依据目录索引。

命名调整前按输入目录镜像输出的成品及索引保留在原位置，不自动迁移到新专辑目录；重新处理相同来源时，新目录中的索引决定复用或转换。旧单文件模式的迁移行为仍适用。

- 源内容和已登记成品的校验结果一致时直接复用，无需再次转换和完整解码。
- 源内容变化时保留旧成品，新文件追加本地时间戳 `yymmdd_hhmmss`；名称冲突时再追加序号。
- 已登记成品缺失或校验不符时重新转换。首次目标位置存在未登记的同名文件时会报冲突，不覆盖该文件。
- SHA-256 以文件大小和纳秒修改时间作为缓存条件；两者均未变化的外部内容改写无法通过缓存检查发现。
- 转换期间重新计算源内容摘要，确认未改变；成品发布时先复制到目标盘临时文件并校验摘要，再以不覆盖方式发布和提交索引。
- 输出锁和链接工作目录锁的文件名读取主配置 `logging.lock_file`，仅限制本平台写入；本地 QSV 批次全程持锁，链接下载在整理旧记录和发布时持有输出锁。同平台冲突仍立即报错，已验证候选保留用于续发。
- 可以与腾讯、优酷、芒果同时写入同一个 `output_path`，每个项目只运行一个进程。各工具的锁名、工作目录、日志文件名及带平台标识的剧集目录须保持独立；不同平台不要配置为同一个实际剧集目录，各平台的 `video_info.json` 格式并不通用。并发上限分别作用于各项目，不是跨项目的总限额。
- 此规则在新启动的进程中生效；正在运行的旧批次保持原状，先让它结束后再使用新版共享目录并行运行。锁文件保留不代表被占用，不需要删除旧锁文件。

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
python ".\tests\benchmark_decode.py" --config ".\configs\config.yaml" --benchmark-config ".\research\configs\benchmark_decode.yaml"
```

从 `output_path` 中选取最新登记成功的 MP4，校验复制到工作目录后运行。任务数、重复次数、预热、并发、后端和报告位置均读取 [性能专项配置](research/configs/benchmark_decode.yaml)；可重复传入 `--case`，值必须是该配置 `cases` 中的 `name`，省略时运行全部组。

比较范围为完整视频及全部音轨的解码，不含 QSV 提取、SDK 处理或成品发布。包含 GPU 的组需要可用 CUDA 环境；系统可找到 `nvidia-smi` 时额外采样 GPU 使用情况。报告保留，结束后删除本次输入副本。

### 验收真实 CPU/GPU 验证

```powershell
python ".\tests\accept_verification.py" --config ".\configs\config.yaml" --benchmark-config ".\research\configs\benchmark_decode.yaml"
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
| 本平台目录锁被占用或同名文件冲突 | 确认没有本平台批次仍在运行，检查报错目标及目录索引；锁文件的存在本身不代表锁仍被持有。 |
| 索引或历史日志损坏 | 按报错路径检查并从可靠备份恢复；程序保留损坏记录并报错，不自动重建或覆盖。 |
| 链接任务报“未提供媒体清单地址” | 某些最高档实际提供内嵌 MPD；当前版本按清单内容识别 DASH，无须切换该集播放器或降低画质。仍失败时查看批次中的安全片源诊断。 |
| 链接断点文件短暂“拒绝访问” | 原子替换按 `logging.write_retries` 和 `write_retry_interval_seconds` 有界重试；持续失败保留现场，再次运行核对已登记段的长度和摘要。 |

## 项目结构

```text
002_获取爱奇艺视频/
├── decode_qsv.py              # 本地 QSV 转换入口
├── download_iqiyi.py          # 链接下载入口
├── download_list/             # 用户链接清单及模板
├── configs/                   # 正式下载和 QSV 转换的主配置、链接下载及本机适配配置
├── qsv_local/
│   ├── configuration.py       # 配置校验与路径解析
│   ├── discovery.py           # 输入发现和输出映射
│   ├── download_state.py      # 客户端下载状态保护
│   ├── batch.py               # 有界流水线与结果提交
│   ├── workflow.py            # 提取、封装、验证及成功清理
│   ├── container.py           # QSV 容器解析
│   ├── native.py              # 本机 v5 SDK 接口
│   ├── playback/              # 链接、正片目录、原生会话、选档、流式下载及缓存研究
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
├── research/                  # 调研入口、资料及第三方参考，非正式转换依赖
│   └── configs/               # 研究专用配置，如 CPU/GPU 性能比较配置
├── requirements.txt           # 正式 Python 依赖
└── README.md
```

研究专用配置统一放在 `research/configs/`；研究脚本直接复用正式工具配置中的公共参数，不复制维护。配置文件中的相对路径以各自文件所在目录为基准；性能配置迁移后，报告仍写入其配置指定的位置。

格式研究和适配边界见 [research/formats.yaml](research/formats.yaml)，资料索引见 [research/catalog.yaml](research/catalog.yaml)。本工具工作区文件位于父目录，启动和任务中的解释器统一引用其 `settings.python.defaultInterpreterPath`。
