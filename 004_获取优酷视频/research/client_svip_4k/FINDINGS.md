# 优酷客户端 SVIP 与最高画质获取研究

研究日期：2026-11-16（北京时间）。样本从 `configs/research.yaml` 指定的下载清单和标题读取。本次匹配《难哄》第 1 集；以下数字是本次观测结果，不是程序默认值或对其他剧集的保证。

## 当前结论

已复用本机客户端的现有登录态，成功查询会员、下载清晰度和播放流元数据，取得正片 `3840×2160` 的普通 4K 流信息。**尚未取得截图对应的帧享 4K 正片流，也没有生成可独立播放的视频文件。**

| 检查项 | 本次证据 | 能说明什么 |
| --- | --- | --- |
| 客户端 | Windows 优酷 `9.5.3.1001` | 本次研究的实际版本 |
| 账号权益 | 会员接口 `is_vip=1`、`member_id=100006`；UI 显示 SVIP | 现有账号已被识别为会员 |
| 播放鉴权 | UPS `user.vip=true`，存在用户标识；报告只保留存在性 | 播放接口也识别了登录状态 |
| 播放 UI | 《难哄 01》播放控制栏显示“帧享4K”；菜单包含对应 SVIP 档 | 原客户端能够选择这一档；UI 标签不等于本研究取得了同一媒体流 |
| 官方下载 | 下载窗口和 `downloadpanelattr` 均最高列出“高清SDR 1080P” | 此样本的官方下载入口没有列出 4K |
| 普通 4K | 正片 `stream_type=mp5hd4`，`3840×2160`，`size=676739088`，时长 `2732` 秒 | 已实际取得完整时长的 4K 流元数据；大小是接口声明，未下载验证 |
| 帧享开放标志 | `controller.hbr4k_open=true` | 服务端开放标志存在；不代表已经返回帧享流 |
| 媒体保护 | 正片 `drm_type=copyrightDRM`，含 `copyright_key` 字段；不保存字段值 | 独立文件生成还需要验证此保护路径，不能只拼接 URL 或修改后缀 |

普通 4K 的 `mp5hd4` 与播放器代码中的帧享类型不是同一类。源码中的帧享类型包含 `hls5hd4_hbr`、`cmfv5hd4_hbr` 等；清晰度映射将 `4k_frame` 映射到 `25`。当前参数对照中，即使选择该映射，返回的正片仍只有普通 4K。不能把它当作“最高清晰度获取成功”。

研究请求返回的 `clientAbility.h265=1`，而 `z1080`、`z4k`、`zreal4k` 仍为 `0`。这说明本研究的客户端能力协商尚未复现帧享能力，是下一步应核对的具体差异；尚不能认定它是唯一原因。

脱敏实测归档在 `reports/observed_20261116.json`；每次重新运行的结果在 YAML 指定的输出目录。归档保留当时事实，不随用户后续修改配置自动改写。

## 已打通的调用链

1. 从客户端的 `Cookies` 数据库只读加载当前优酷会话，Cookie 值只驻内存。
2. 查询 `mtop.youku.vip.xtop.member.profile.get` 验证权益。
3. 查询 `mtop.youku.pixiu.downloadpanelattr.get` 获取当前样本的实际下载档位，不从前端枚举猜测可下载能力。
4. 使用客户端自身的 Electron，以独立 Node 子进程加载同一安装目录中的 `youku-player.node`，调用 `KAliPlayer.getUpsCKey` 和 `getEncryptR1` 生成请求材料。
5. 按本机缓存播放器代码中的参数构造 `mtop.youku.play.ups.appinfo.get` 请求，记录正片清晰度、尺寸、时长和 DRM 类型。

本次签名运行时是 `Electron 14.2.0 / Node 14.17.0 / ia32`。不要直接用系统 64 位 Node 加载该原生模块。脚本记录模块 SHA-256；客户端升级后应重新核对接口和结果，不能只改路径就宣称适配成功。

当前网络研究走 H5 MTop，携带客户端会话与桌面参数，**不是对运行中客户端原生 MTop 请求的完整复刻**。设备标识取配置指定的公共域 Cookie；尚未证明与运行中播放器的原生 UTDID 一致。这是帧享流尚未复现时需要继续比较的差异之一，不是已确认的失败原因。

参数对照全部写在 YAML 的 `network.ups.cases`，包含下载取流参考、桌面播放能力及选择帧享档位三组。主机、版本、能力位和搜索词均读配置，代码没有另设一组运行默认值。

代码也保留播放器源码中的 `enc_ver` 接口元数据处理分支。本次服务器均返回未加密元数据，原生 `getDecryptR1` 分支**未触发，尚未完成真实响应验证**。该分支用于接口元数据，不能当作媒体 DRM 解密实现。

## 距离独立视频文件还差什么

需要先取得与原客户端“帧享4K”选择相同的正片流，再验证当前授权下的媒体处理和完整文件生成。重点比较实际播放器请求与研究请求中的设备信息、能力协商、返回的 `clientAbility`、权益标志及受保护媒体处理链。

本次没有导出媒体密钥，没有实现 `copyrightDRM` 视频还原，没有下载正片，没有做全片解码验收。报告中的 `independent_file_verified` 因而保持 `false`。仅有会员 Cookie、4K 标签、M3U8 地址或元数据中的文件大小，都不足以将任务判定为完成。

后续实现必须区分正片、广告、预览以及不同音轨。研究报告给每条流标注 `json_path`；本次正片来自 `$.data.data.stream`，其他广告路径不能参与正片最高画质排名。

## 复现

从优酷工具根目录运行，Windows PowerShell：

```powershell
Set-Location ".\research\client_svip_4k"
$researchPython = "C:\ProgramData\miniconda3\envs\test_regression_py3.12\python.exe"
$env:PYTHONUTF8 = "1"

& $researchPython ".\research.py" --config ".\configs\research.yaml" --action snapshot
& $researchPython ".\research.py" --config ".\configs\research.yaml" --action source
& $researchPython ".\research.py" --config ".\configs\research.yaml" --action native
& $researchPython ".\research.py" --config ".\configs\research.yaml" --action online
```

先检查 YAML 的客户端路径和样本选择，再运行。相对路径以 YAML 所在目录为基准；环境变量会显式展开。账号必须已在客户端登录。脚本不会自动登录、关闭客户端、修改客户端配置或降低请求画质来报告成功。

其他研究入口：

```powershell
# 搜索优酷自己的 HTTP 缓存；独占文件会记录为无法读取，不解锁、不停客户端。
& $researchPython ".\research.py" --config ".\configs\research.yaml" --action cache

# 可选：对另行取得的原始 UPS JSON 做离线脱敏分析；原始文件放在忽略目录。
& $researchPython ".\research.py" --config ".\configs\research.yaml" --action ups --input ".\private\ups.json"

# 在研究目录发现隔离测试；不会使用真实账号发送测试请求。
& $researchPython -m unittest discover -v -s tests -t .
```

JSON 报告的生成成功只表示采样过程完成。请检查接口返回码、业务错误、正片流及 `independent_file_verified`，不能仅凭进程退出码判断已获取视频。

依赖为 `requirements.txt` 中的 `PyYAML`，当前指定环境已安装，本次没有安装新包。换环境时可在该目录安装：

```powershell
& $researchPython -m pip --isolated install --proxy="" -r ".\requirements.txt"
```

## 文件与工作区

- `research.py`：统一命令行入口。
- `configs/research.yaml`：本机路径、样本、接口和研究参数。
- `lib/`：只读采样、会话复用、原生桥接和响应脱敏模块。
- `tests/`：配置、响应来源、会话隔离和脱敏测试。
- `reports/`：脱敏实测归档；`reports/latest/`、原始响应和媒体样本不进 Git。

父目录 `004_获取优酷视频.code-workspace` 已同步研究入口和测试工作目录，移除了指向本工具不存在文件的爱奇艺 QSV 启动项。所有启动项继续引用 `settings.python.defaultInterpreterPath`。未新增工具根目录 README，未修改已有下载清单。

本次验证：11 项隔离测试全部通过；当前会话查询、源码采样和工作区 JSONC / 解释器引用检查通过。测试过程中发现的 SQLite 连接未及时关闭问题已修复，并通过临时数据库清理测试。
