# B 站视频下载工具

下载 B 站投稿分 P、番剧或 YAML 清单，按配置保存原始音视频编码及字幕，也可拼接已下载的分 P。

## 操作步骤

1. 使用 VS Code 打开父目录的 [001_获取B站视频.code-workspace](../001_获取B站视频.code-workspace)，在“终端 → 运行任务”中执行“B站：准备环境”。
2. 登录 Chrome 中的 B 站，运行工作区任务“B站：打开 Cookie 导出工具”。在 Chrome 打开 `chrome://extensions`，启用开发者模式并加载任务打开的 `chrome_cookie_export` 文件夹；点击扩展“B站登录态本机导出”，将文件保存到工具目录的 `cookies/`。运行“B站：验证 Chrome 登录态”检查登录。
3. 检查 [config/config.yaml](config/config.yaml) 中的 `auth`、`paths.output_dir`、`download` 和字幕设置。Cookie 保存到其他位置时调整 `auth.cookie_file`；路径以配置文件所在目录为基准。
4. 在“运行和调试”中选择“B站：查看画质与字幕”“B站：下载当前分 P”或“B站：下载全部分 P”，输入视频链接、BV/AV、番剧 ep/ss 号或 YAML 清单路径，并根据提示选择画质。清单可参考 [download_list/template.yaml](download_list/template.yaml)，在 `download_list` 中填写任务链接。

也可在**仓库根目录**的 PowerShell 中执行现有清单：

```powershell
& "C:/ProgramData/miniconda3/envs/test_regression_py3.12/python.exe" ".\001_获取B站视频\download_bilibili.py" --url ".\001_获取B站视频\download_list\download_list.yaml"
```

清单中的 `all-parts`、`merge-parts` 和 `quality` 控制下载范围、拼接和画质；番剧全部关联季由主配置 `download.all_seasons` 控制。离线拼接可使用工作区的“B站：拼接已下载分 P”。

终端会输出本机实时日志地址，成品及离线报告保存在配置的 `paths.output_dir`。按 Ctrl+C 可中断，已完成文件和断点保留；重新运行时会核对并复用符合条件的结果。Cookie 过期后重新导出即可。

下载与处理流程见 [设计与流程](docs/设计与流程.md)；完整参数、拼接规则及原有命令见 [操作与命令参考](docs/操作与命令参考.md)。
