# QQ 音乐下载文件转换

将 `C:\Users\carri\Music\VipSongsDownload` 内的 `.mflac` 转为 FLAC、`.mgg` 转为 OGG。默认将结果直接放在当前项目根目录 `D:\LocalCode\吃瓜分析`，源文件保持原样。

## 运行

先启动 QQ 音乐并登录下载歌曲的账号，再从项目根目录运行：

```powershell
python -m pip install -r 001_QQ_music_download/requirements.txt
python 001_QQ_music_download/convert.py
```

如需处理其他目录或重新生成已有文件：

```powershell
python 001_QQ_music_download/convert.py --source "C:\音乐\下载" --overwrite
```

脚本首次运行会将固定版本的 [qmdec 源代码](https://github.com/jeff-0717/qq-music-decoder/tree/b09abe65ead438e43c2c6f124d27631e36f6f57b) 下载到项目的 `.tools/qq-music-decoder`（已被项目 `.gitignore` 忽略），以后复用。需要 Git、Python 3.10+、网络连接和当前账号对歌曲的播放权限。QQ 音乐的会话和歌曲密钥只在运行内存中使用，不写入项目文件；会话仅发往 QQ 音乐的密钥接口。脚本会完整解码校验输出，若原始 FLAC 尾包损坏，会无损重建并再次校验。无损重建可能丢弃末尾不足半秒的损坏音频数据。

默认不会覆盖同名输出；已有文件若通过校验会跳过。`--overwrite` 可重新生成。
