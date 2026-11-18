# 芒果视频下载工具

读取芒果视频客户端已有登录态，按官方链接下载账号可获取的正片，封装为 MP4 并进行完整解码验证。

## 操作步骤

以下 PowerShell 命令在本工具根目录执行。

1. 安装依赖：

   ```powershell
   & "C:/ProgramData/miniconda3/envs/test_regression_py3.12/python.exe" -m pip --proxy "" install -r "requirements.txt"
   ```

2. 在芒果视频客户端登录有观看权限的账号。工具读取客户端保存的登录态；过期时需重新登录。
3. 检查 `configs/config.yaml`：设置输出目录 `output_path`、工作目录 `work_dir`、可用的 `ffmpeg_path` 和登录态路径 `auth.user_data`，按需调整画质、并发数及 CPU/GPU 验证设置。配置中的相对路径以配置文件所在目录为基准，具体选项以配置及其注释为准。
4. 编辑 `download_list/download_list.yaml`，在 `download_list` 中填写官方视频链接，可参考同目录的 `template.yaml`。清单中的 `quality` 和 `all-parts` 可覆盖主配置的画质和整剧选择。
5. 按清单启动下载：

   ```powershell
   & "C:/ProgramData/miniconda3/envs/test_regression_py3.12/python.exe" "download_mgtv.py" --url "download_list/download_list.yaml"
   ```

   `--url` 也可指定官方视频链接，并可重复使用；不传 `--url` 时程序会提示输入链接或清单路径。可用 `--all-parts` 下载整剧，或用 `--no-all-parts` 只下载链接对应集，两者会覆盖清单和主配置的范围设置。使用其他主配置时可追加 `--config "配置文件路径"`。

   也可打开父目录的 `005_获取芒果视频.code-workspace`，运行“芒果下载：YAML批量最高画质”启动项。
6. 在浏览器打开终端输出的“实时记录”地址查看进度；结束后可查看终端给出的离线报告。成品保存在配置的 `output_path`，工作现场保存在 `work_dir`。按 Ctrl+C 可取消，断点会保留，重新运行可继续处理。

检查登录态可使用 `--login-only`；只查看画质可在下载命令中追加 `--list-formats`，如只查看链接对应集，再加 `--no-all-parts`。指定画质不存在时不会自动降档；加密 HLS 尚未支持。`download.threads` 控制同时下载的剧集数，`max_in_flight` 控制解析和后处理等非下载任务的总并发数。

模块职责及处理流程见 [设计与流程](docs/设计与流程.md)。
