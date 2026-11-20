# 腾讯视频下载与缓存还原

使用 Windows 腾讯视频客户端，通过链接清单下载账号有权观看的正片，或还原客户端已完成的下载缓存，封装并验证成品。

## 准备

以下 PowerShell 命令在本工具根目录、同一个 PowerShell 7 窗口中执行。读取工作区解释器并安装依赖：

```powershell
$tencentWorkspace = Get-Content -LiteralPath "../003_获取腾讯视频.code-workspace" -Raw | ConvertFrom-Json
$tencentPython = $tencentWorkspace.settings.'python.defaultInterpreterPath'
& "$tencentPython" -m pip --proxy "" install -r "requirements.txt"
```

新开终端时，先重新执行前两行。检查 `configs/config.yaml` 中的 `ffmpeg_path`、`output_path` 和 `work_dir`；按本机情况设置 CPU/GPU 完整解码验证，至少启用一个设备。相对路径以配置文件所在目录为基准。

## 链接下载

1. 在主配置中将 `source` 设置为 `url`。编辑 `default_list` 指向的清单，在 `download_list` 中填写腾讯视频官方链接；字段可参考 [清单模板](download_list/template.yaml)。
2. 按需设置画质和选集范围。主配置的 `download.all_related_parts` 控制系列下载，清单顶层的 `all-related-parts` 可覆盖该设置。开启后适用于清单中的所有链接，优先于 `all-parts`，按腾讯官方同组选集标签逐部展开已上线的编号正片。普通剧集会展开本剧，互动番外、预告和花絮不进入下载队列。只下载单集时，链接须包含该集的视频编号。音质由主配置的 `audio.quality` 控制。
3. 登录腾讯视频客户端，播放一集账号已授权的正片并保持唯一播放器运行，目标视频也需要相应权限。
4. 启动下载：

   ```powershell
   & "$tencentPython" -X utf8 "decode.py" --config "configs/config.yaml"
   ```

   可省略 `--config` 使用默认主配置，也可指定其他配置文件。

## 客户端缓存还原

1. 在腾讯视频客户端完成视频下载，在主配置中将 `source` 设置为 `download`。
2. 检查 `input_root`、`download_record`、`output_path` 和 `work_dir`；输出及工作目录须位于客户端下载根目录之外。按需使用 `selection` 筛选，两个筛选列表均为空时处理全部已完成任务。
3. 使用上面的启动命令。当前缓存流程处理已完成的主 HLS/TS 资源，通过客户端 DLL 还原 HVN 缓存；获取整剧上映日期需要联网。

## 查看结果

终端会输出实时日志地址及离线报告路径，成品和运行记录保存在配置的 `output_path`。按 Ctrl+C 可中断任务，失败或中断的工作现场保留在 `work_dir`，重新运行可检查并复用符合条件的成品和断点。

画质或音质档位不可用时会报错，不自动降档；加密 HLS 尚未支持。URL 下载数由 `download.threads` 限制，解析和后处理共用 `max_in_flight`；缓存还原的在途任务数也由 `max_in_flight` 限制。

链接来源使用腾讯虚拟 TS 分片时，工具会检查首个媒体分片。内容无法识别为 MPEG-TS 时，该集提前报错并保留现场。

也可打开父目录的 `003_获取腾讯视频.code-workspace`，运行“腾讯视频：按配置下载或还原”。更多设计及技术约束见 [设计与流程](docs/设计与流程.md)，原有的详细操作、验证和研究命令见 [操作与验证参考](docs/操作与验证参考.md)。
