# 优酷视频下载工具

使用已登录的 Windows 优酷客户端，按链接清单下载账号有权观看的正片，完成还原、封装和完整解码验证。

## 操作步骤

以下 PowerShell 命令在本工具根目录执行。

1. 安装依赖：

   ```powershell
   & "C:\ProgramData\miniconda3\envs\test_regression_py3.12\python.exe" -m pip install -r "requirements.txt"
   ```

2. 检查 `configs/config.yaml`：设置输出目录 `output_path`、工作目录 `work_dir` 和可用的 `ffmpeg_path`，按需调整画质、并发数及 CPU/GPU 验证设置。路径和其他选项以配置文件及其注释为准；配置中的相对路径以该配置文件所在目录为基准。
3. 编辑 `download_list/download_list.yaml`，在 `download_list` 中填写优酷官方视频链接。可参考同目录的 `template.yaml`；清单中的 `quality` 和 `all-parts` 可覆盖主配置的画质和整剧选择。
4. 打开优酷客户端并登录，先播放一集账号已授权的正片，运行期间保持客户端打开。客户端路径见 `configs/native_playback.yaml`；原生接口与客户端版本经过校验，升级客户端后需要重新验证适配。
5. 启动下载：

   ```powershell
   & "C:\ProgramData\miniconda3\envs\test_regression_py3.12\python.exe" "decode.py"
   ```

   如需使用其他主配置，可追加 `--config "配置文件路径"`。也可打开父目录的 `004_获取优酷视频.code-workspace`，运行“优酷：按清单并发下载并完整验证”启动项。
6. 在浏览器打开终端输出的“实时日志”地址查看进度；结束后可查看终端给出的离线报告。成品保存在配置的 `output_path`，中间工作文件和断点保存在 `work_dir`。按 Ctrl+C 可取消批次，可靠断点会保留，重新运行可继续处理。

当前已验证帧享 4K HLS/TS 格式；选中的画质或格式不可用时会报错，不自动降档。`download.threads` 控制同时下载的剧集数，`max_in_flight` 控制解析和后处理等非下载任务的总并发数。

模块职责、下载与处理流程及验证边界见 [设计与流程](docs/设计与流程.md)。
