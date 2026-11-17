# 腾讯视频工具目录规则

## 适用范围

- 本文件仅适用于 `D:\VSCode\personal_tools\003_获取腾讯视频` 及其子目录，不规定父目录或其他工具目录的组织方式。
- 本目录未覆盖的通用约定继续继承上级规则；子目录有明确规则时，以子目录规则为准。

## 编码规则

- 项目目录层级要清晰、规范
- 共用通用的接口、文件，避免重复编码
- 以最新设计为准，不兜底、不硬编码，配置写在配置文件里，配置文件格式yaml优先。
- 用户会手动调整配置文件，测试代码、文档内容不能写死为特定值，必须以配置文件为准
- .gitignore 里要按类型放、加注释
- 禁止使用带空格的路径

## 项目结构

本项目正在开发中 实际文件夹可能有变化，以实际需求为准，同步更新此处的项目结构

- 当前目录作为腾讯视频工具的根目录，内部按功能和职责分层。
- 根目录的代码文件仅保留可直接运行的入口脚本，腾讯视频 URL 下载与缓存还原统一使用 `decode.py`；命令行参数定义、默认值、互斥关系和参数校验集中在入口脚本中，入口负责定义命令行参数、调用业务模块并处理退出码。
- 具体业务逻辑、公共接口和工具函数放入 `local/`，按职责拆分模块并通过模块导入复用。
- `local/templates/` 保存实时日志和离线报告共用的页面模板；页面展示本工具历次运行及任务详情。
- 当前批量还原流程针对客户端已完成下载的主 HLS/TS 资源，读取 `QLDownload.xml` 和本地清单，通过客户端 DLL 还原 HVN 缓存；其他缓存结构需独立验证后接入。任务调度、验证、发布、复用和报告位于 `local/`。
- `local/playback/scheduler.py` 统一调度增量目录发现、逐集解析、下载和后处理；目录逐清单项完整校验后交接，日志批量登记并持续补充分组。
- `local/playback/` 实现 URL 来源：只读原生登录态、腾讯 HTTP 播放与完整分页选集、独立音质选择、HLS 分片断点；复用公共调度、媒体探测、完整解码、日志和安全发布，不导入其他工具目录的业务模块。
- `local/playback/signing_compat.py` 和 `local/playback/templates/signing_observer.js` 负责候选签名验收；`research/verify_native_signing.py` 显式启动观察，验证当前清单的授权、续签和有界媒体读取后才更新签名配置。所有研究入口按链接选取样本，不按剧名备注筛选。
- 测试脚本统一放入当前目录的 `tests/`。
- 主配置放在 `configs/config.yaml`，正式流程的专项配置放在 `configs/`；研究专用配置放在 `research/configs/`，相对路径以配置文件所在目录为基准；代码、测试和文档均以实际配置为准，不重复硬编码配置值。
- 电视剧成品目录通过主配置的 `naming.series_template` 命名，模板包含整剧首次上映日期 `release_date`、源站剧名 `title` 和整剧编号 `series_id`（URL 来源为 cid，缓存来源为 vbarid），平台标记在模板中维护；日期来自腾讯整剧简介的 `broadcast_time`，缺失、无效或编号不匹配时拒绝该任务。缓存模式每剧查询一次公开简介；日期只用于目录，不用于视频文件名。
- `local/premiere.py` 共用整剧日期读取；`local/migration.py` 负责用户明确要求时迁移旧目录，持有输出锁、校验目录内容和目标、保持索引并同步运行历史；旧离线 HTML 快照保留原样。
- 本机客户端目录、DLL 文件名和接口位置集中在 `configs/native_cache.yaml`；自动使用最新版本目录的 DLL，接口不兼容时再重新分析和调整。
- 研究资料和第三方参考资源放在 `research/`，研究专用 YAML 放在 `research/configs/`；正式流程与研究共用的配置仍由 `configs/` 维护，通过研究配置引用，不复制维护。
- `configs/native_playback.yaml` 维护原生播放协议、选集接口和已验证的原生用户只读 ABI；`configs/native_signing.yaml` 共用运行中播放器的签名 ABI、DLL 摘要及内存释放接口，正式流程和研究入口不重复维护这些值。安装目录引用缓存专项配置，SDK 的编码版本号与客户端产品版本分别维护。读取登录态在隔离进程中进行，票据只通过内存管道传递，不从历史 Cookie 或播放日志提取票据。登录或签名 DLL 内容变化时拒绝调用，须重新核对接口。

## 下载行为与用法

- 主配置 `source` 选择 `url` 或 `download`，仓库配置默认 `url`；代码不设置另一个模式默认值。URL 模式自动读取 `default_list`，缓存模式使用原有下载记录与 `selection`，专项缓存验收入口显式选择缓存分支。
- `quality` 和 `all-parts` 按清单任务项、清单全局、主配置 `download` 的顺序继承；只处理已上线正片，排除预告和速通等扩展版本。分组与分页必须完整读取，不能把首分组的 `has_next=false` 当成整剧结束。重复视频参数冲突时停止。
- 每集按接口实际尺寸、帧率及专项配置的指标排名，档位不存在、响应降档或实际媒体规格不符时失败；请求能力与本机显示器能力分开记录，不将营销 HDR 名称作为文件色彩属性。当前已完成《花开锦绣》单集 MAX 的完整实网下载、CPU/GPU 全片解码、发布及再次复用验收；研究证据见 `research/playback_findings.md`。接口返回不能代替完整文件验收。fMP4、范围分片及外置 HLS 音轨有严格解析与本地封装支持；新的独立音轨选择协议仍须实测适配。加密 HLS 明确失败，不改选低档。
- 音质通过主配置 `audio.quality` 按每集实际可授权下载的原始音源排名选择，正整数 `1` 表示最高，`2` 表示次高；档位不足或多个音源无法比较等级时明确失败。会员标签不代替音质等级，客户端实时滤镜及无法下载的项不进入音质列表，不计档、不触发终端选择。接口只返回视频内标准原轨时，该轨就是唯一可下载音源。新独立音质协议未验证时明确停止，不能把语言音轨数当作音质等级。
- URL 成品文件名使用 `naming.episode_template`，包括集序、源站标题、画质及音质，视频编号可按需加入模板。缓存旧版与未知同名文件均保留，发生同名冲突时新成品会添加区分后缀。共用剧目录索引中区分 `source_type` 和具体音视频源身份，按配置的复用校验方式检查同源成品。
- URL 断点按整剧、视频和来源指纹隔离；签名地址不落盘，腾讯 TS 的分片序号与字节范围参与身份计算。CDN 路径也可能含临时签名，只对与原生文件名精确匹配的虚拟 TS 段规范化身份。地址失效仅续签同源；来源变化保留旧现场。完整解码、来源复核及目标盘复制摘要均通过后才发布，索引失败回滚本次发布；失败和取消保留断点。
- URL 下载数由 `download.threads` 独立限制，解析、复用检查、续签及后处理共用顶层 `max_in_flight` 总并发上限；`download.queue_capacity_gb` 按 1024³ 字节限制已下载且等待处理的分片，空队列允许单集超限。队列不包含下载中、等待入队、处理中及历史失败断点的磁盘占用。
- URL 登录快照按主配置 `authentication` 在请求边界定期重读；明确认证拒绝时串行重读并重试原画质/音轨请求，旧代次的迟到拒绝复用已恢复快照。Cookie 与 logintoken 必须来自同一个不可变快照；不能静默切换账号。媒体地址过期与接口认证拒绝分别处理，恢复耗尽后停止派发并将未执行任务记为跳过；票据及账号标识只驻内存，报告仅记录安全状态和计数。
- 以下 PowerShell 命令均以本工具目录为当前目录，解释器以当前工作区设置为准：`& "C:/ProgramData/miniconda3/envs/test_regression_py3.12/python.exe" "decode.py" --config "configs/config.yaml"`。切换来源只修改 YAML，不需更换入口。
- URL 模式启动前，在腾讯客户端播放一集已授权正片并保持唯一播放器运行。正式流程自动匹配安装目录、进程名及签名 DLL 摘要，通过 `frida` 调用已初始化 SDK 生成签名，复制结果后使用同一 CRT 释放内存并自动分离；不会修改客户端配置或原播放请求。独立加载 DLL 返回的错误码不能作为签名使用，也不切回网页签名或低画质。
- 依赖安装：`& "C:/ProgramData/miniconda3/envs/test_regression_py3.12/python.exe" -m pip --proxy "" install -r "requirements.txt"`。`frida` 用于运行中 SDK 的原生签名及短时协议研究；不再使用网页 AES 签名依赖。
- 研究入口 `research/playback_probe.py --config research/configs/playback_research.yaml` 对照能力声明与真实码流；`research/inspect_playback_runtime.py` 使用同一专项配置检查官方页面引用的公开播放器代码和指纹，不读取账号。脱敏报告与公开参考代码写入已忽略的研究结果目录，禁止保存票据或带签名媒体地址。
- `research/probe_delivery_variants.py --config research/configs/playback_research.yaml` 比较同档交付方式、SDK 版本、请求头和显式代理；可用重复的 `--variant <配置名称>` 限定研究范围，不改变正式配置。`research/inspect_source_identity.py` 使用同一专项配置连续查询并诊断续签身份。`research/probe_transfer.py` 比较 urllib 与本机 curl；`research/probe_file_key.py` 仅研究精确文件的授权响应，不作为正式下载的隐式替代路径。
- `research/inspect_native_requests.py` 使用同一专项配置只读近期客户端日志，输出白名单协议参数。`research/observe_native_playback.py --config research/configs/playback_research.yaml --pid <已核对的客户端进程编号>` 短时观察 libcurl/WinHTTP、下载代理和签名调用，不改原调用参数或客户端文件；边界读取 `native_observation`，结束后自动分离。主进程与播放器子进程都可能加载 SDK，须实际核对调用位置，不能仅凭 DLL 已加载认定通道。报告只保留协议结构与白名单参数，签名、票据、会话种子和带签名媒体地址不落盘。
- `research/probe_native_key.py --config research/configs/playback_research.yaml` 对照原生签名、同档播放响应及有界分片传输；`--pid <播放器编号>` 可显式指定已核对的运行上下文。`--local-only` 只检查签名输出结构：带 `--pid` 时使用运行中 SDK，不带时在隔离进程加载 DLL，以诊断初始化依赖。不要直接运行内部 `native_key_worker.py`，其标准输入输出是仅供父进程使用的内存签名管道。
- `tests/verify_playback.py --config configs/config.yaml --item <当前清单零基序号> --report-dir research/results` 显式检查链接集的画质、音质及完整清单；追加 `--full-download` 才执行单集全片下载、完整解码、发布及再次复用。普通 unittest 只使用回环服务、临时目录和合成媒体，不访问真实账号。
- `tests/verify_url_output.py --config configs/config.yaml --report <已成功发布单集的全片报告> --report-dir research/results` 先核对成品摘要，再补验当前配置启用但首次流水线尚未使用的解码设备，并确认补验期间成品未变化；不重复下载、不覆盖媒体。正式流水线的 CPU/GPU 是可用验证资源池，每集使用一个设备；需要同一文件验证所有启用设备时使用该专项入口。

## 工作区配置

- 本工具使用父目录中的 `003_获取腾讯视频.code-workspace`；该文件不属于本文件的目录管辖范围，本文件不规定其他工具的工作区配置。
- 涉及本工具工作区配置的任务中，Python 解释器路径统一配置在 `settings.python.defaultInterpreterPath`，启动、任务等其他配置通过 `${config:python.defaultInterpreterPath}` 引用，不重复硬编码解释器路径。

## 并发配置约定

- 继承仓库根目录 `AGENTS.md` 的统一规则：`download.threads` 仅控制媒体下载；解析、选档、复用检查、续签及后处理共用顶层 `max_in_flight` 总并发上限。等待队列和设备独占槽按各自配置限制，不计为活动下载；本地转换使用处理上限。
