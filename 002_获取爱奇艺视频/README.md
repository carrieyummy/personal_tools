# 爱奇艺链接下载与 QSV 转换

本工具使用已登录的爱奇艺客户端，通过链接清单下载视频，也可将客户端已完成且授权有效的 QSV 转换为 MP4。

## 准备

以下命令使用 Windows PowerShell。先激活工作区 `settings.python.defaultInterpreterPath` 对应的 Python 环境，使 `python` 指向该解释器，然后进入工具目录并安装依赖：

```powershell
Set-Location -LiteralPath "D:\VSCode\personal_tools\002_获取爱奇艺视频"
python -m pip install -r ".\requirements.txt"
```

检查 [主配置](configs/config.yaml) 中的 `output_path`、`work_dir`、`native_profile` 和 `ffmpeg_path`，按本机情况设置 CPU/GPU 验证设备，至少启用一个设备。配置文件中的相对路径以对应配置文件所在目录为基准。

## 链接下载

1. 登录爱奇艺客户端，打开任意一集有权限的正片并暂停一次，运行期间保持客户端打开；客户端重启后重新初始化。
2. 编辑 [configs/playback.yaml](configs/playback.yaml) 指向的下载清单，填写官方短链接、分享链接或普通播放链接；可参考 [清单模板](download_list/template.yaml)。按需设置清单画质和整剧范围，以及主配置的音质。
3. 执行下载：

   ```powershell
   python ".\download_iqiyi.py" --config ".\configs\playback.yaml"
   ```

   只查看公开正片目录时，可使用：

   ```powershell
   python ".\download_iqiyi.py" --config ".\configs\playback.yaml" --plan
   ```

   `--plan` 不请求登录播放会话或下载媒体。正式入口可用 `--index` 选择清单下标、用 `--episode` 筛选集序。

## 本地 QSV 转换

1. 在客户端完成视频下载并确保本机授权有效。检查主配置中的 `input_root`、`input_path` 和 `download_guard`，确保客户端下载记录及状态与实际一致。
2. 启动转换：

   ```powershell
   python ".\decode_qsv.py" --config ".\configs\config.yaml"
   ```

## 查看结果

终端输出实时日志地址和批次报告；成品保存在配置的 `output_path`。失败或中断时保留工作现场；按 Ctrl+C 中断后可重新运行，程序会检查可复用的成品或断点。

链接下载复用成品时，先核对当前来源和 `video_info.json` 中的发布记录，再检查预期文件名和登记字节数。复用时无需读取 MP4 内容，因此无法发现同尺寸的损坏或替换。新下载的成品须通过完整解码和复制摘要校验后才发布。

爱奇艺链接任务处理完毕后，实时网页会继续运行。失败剧集仍可点击“清除断点并重试”，确认后清理本集相关来源的未发布缓存，再重新获取播放清单和下载。全部处理完后，在网页顶部点击“停止服务”，脚本会保存最终报告并退出。关掉浏览器仍可通过终端显示的本机地址重新打开页面。

链接下载的 `download.threads` 控制同时下载的剧集数，`max_in_flight` 控制解析和后处理的总并发，也限制本地转换的在途数。指定画质或授权不可用时会报错；客户端升级后若本机接口不兼容，需要重新验证适配。

也可打开父目录的 `002_获取爱奇艺视频.code-workspace`，运行“爱奇艺：链接下载”或“爱奇艺：批量转换 QSV”。模块和流程见 [设计与流程](docs/设计与流程.md)，详细操作、研究和验收命令见 [操作与验证参考](docs/操作与验证参考.md)。
