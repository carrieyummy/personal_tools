"""Convert locally downloaded QQ Music musicex files to playable audio.

The pinned qmdec source handles QQ Music's changing auth/key protocol and QMC2
cipher. This script keeps the login cookie and ekey in memory, validates the
result, and publishes it in the project root only after successful validation.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = Path.home() / "Music" / "VipSongsDownload"
DEPENDENCY = PROJECT_ROOT / ".tools" / "qq-music-decoder"
DEPENDENCY_URL = "https://github.com/jeff-0717/qq-music-decoder.git"
DEPENDENCY_COMMIT = "b09abe65ead438e43c2c6f124d27631e36f6f57b"
EXTENSIONS = {".mflac", ".mgg"}


def run(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=check)


def load_qmdec():
    """Load the reviewed, pinned dependency; fetch it once if missing."""
    if not DEPENDENCY.exists():
        DEPENDENCY.parent.mkdir(parents=True, exist_ok=True)
        clone = run("git", "clone", "--quiet", DEPENDENCY_URL, str(DEPENDENCY))
        if clone.returncode:
            raise RuntimeError(f"无法获取解密依赖：{clone.stderr.strip()}")
        checkout = run("git", "-C", str(DEPENDENCY), "checkout", "--quiet", DEPENDENCY_COMMIT)
        if checkout.returncode:
            raise RuntimeError(f"无法固定解密依赖版本：{checkout.stderr.strip()}")
    revision = run("git", "-C", str(DEPENDENCY), "rev-parse", "HEAD")
    if revision.returncode or revision.stdout.strip() != DEPENDENCY_COMMIT:
        raise RuntimeError("解密依赖版本与脚本审核版本不符，请检查 .tools/qq-music-decoder")
    sys.path.insert(0, str(DEPENDENCY / "src"))
    from qmdec.auth import extract_cookie_from_process
    from qmdec.cli import _decrypt_with_ekey
    from qmdec.musicex import fetch_ekey, parse_file_tail

    return extract_cookie_from_process, _decrypt_with_ekey, fetch_ekey, parse_file_tail


def ffmpeg_executable() -> str:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise RuntimeError("缺少 FFmpeg；请先运行 python -m pip install -r 001_QQ_music_download/requirements.txt") from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


def validate_audio(ffmpeg: str, path: Path) -> tuple[bool, str]:
    result = run(ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-xerror",
                 "-i", str(path), "-map", "0:a:0", "-f", "null", "NUL" if sys.platform == "win32" else "/dev/null")
    return result.returncode == 0, result.stderr.strip()


def flac_samples(path: Path) -> tuple[int, int]:
    """Read sample rate and total sample count from the FLAC STREAMINFO block."""
    with path.open("rb") as stream:
        header = stream.read(42)
    if len(header) < 42 or header[:4] != b"fLaC" or header[4] & 0x7F or int.from_bytes(header[5:8], "big") != 34:
        raise RuntimeError(f"无效的 FLAC STREAMINFO：{path.name}")
    packed = int.from_bytes(header[18:26], "big")
    return packed >> 44, packed & ((1 << 36) - 1)


def repair_flac(ffmpeg: str, raw_path: Path, repaired_path: Path) -> None:
    result = run(ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y",
                 "-i", str(raw_path), "-map", "0:a:0", "-c:a", "flac",
                 "-compression_level", "5", str(repaired_path))
    if result.returncode or not repaired_path.exists():
        raise RuntimeError(f"FLAC 无损重建失败：{result.stderr.strip()}")
    valid, details = validate_audio(ffmpeg, repaired_path)
    if not valid:
        raise RuntimeError(f"重建后仍有解码错误：{details}")
    old_rate, old_samples = flac_samples(raw_path)
    new_rate, new_samples = flac_samples(repaired_path)
    if old_rate != new_rate or old_rate == 0 or abs(old_samples - new_samples) / old_rate > 0.5:
        raise RuntimeError("修复前后的音频时长差超过 0.5 秒，已停止输出")


def convert_file(source: Path, output: Path, overwrite: bool, ffmpeg: str,
                 decrypt_with_ekey, fetch_ekey, parse_file_tail, auth) -> str:
    meta = parse_file_tail(source)
    if not meta or meta["audio_size"] <= 0:
        raise RuntimeError("不支持或不完整的 QQ 音乐文件")

    expected_ext = ".flac" if source.suffix.lower() == ".mflac" else ".ogg"
    destination = output / f"{source.stem}{expected_ext}"
    if destination.exists() and not overwrite:
        valid, _ = validate_audio(ffmpeg, destination)
        if valid:
            return f"跳过（已存在并通过解码校验）：{destination}"
        raise RuntimeError(f"目标文件已存在但解码失败；修复时请加 --overwrite：{destination}")

    ekey = meta.get("ekey")
    if not ekey:
        if not auth.get("ok"):
            raise RuntimeError("QQ 音乐未登录或无法读取会话，请启动 QQ 音乐并登录有权播放该歌曲的账号")
        file_mid = Path(meta["filename"]).stem
        ekey = fetch_ekey(meta["song_mid"], file_mid, auth["cookie"], auth["uin"])
    if not ekey:
        raise RuntimeError("QQ 音乐没有返回这首歌的密钥；请检查账号权限或登录状态")

    with tempfile.TemporaryDirectory(prefix=".qqmusic-", dir=output) as temporary:
        temp_dir = Path(temporary)
        result = decrypt_with_ekey(source, temp_dir, ekey, meta["audio_size"], "", True)
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "解密失败"))
        raw_path = Path(result["output"])
        if raw_path.suffix.lower() != expected_ext:
            raise RuntimeError(f"解密后的格式不是预期的 {expected_ext}，已停止输出")
        valid, details = validate_audio(ffmpeg, raw_path)
        publish = raw_path
        repaired = False
        if not valid:
            if expected_ext != ".flac":
                raise RuntimeError(f"音频解码校验失败：{details}")
            publish = temp_dir / "repaired.flac"
            repair_flac(ffmpeg, raw_path, publish)
            repaired = True
        if destination.exists() and not overwrite:
            raise RuntimeError(f"目标文件已存在：{destination}")
        publish.replace(destination)
    return f"完成：{source.name} → {destination.name}" + ("（已无损修复 FLAC 末尾）" if repaired else "")


def main() -> int:
    parser = argparse.ArgumentParser(description="将 QQ 音乐 .mflac/.mgg 下载文件转换为 FLAC/OGG")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="下载目录或单个文件")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT, help="输出目录，默认当前项目根目录")
    parser.add_argument("--overwrite", action="store_true", help="覆盖同名输出")
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not source.exists():
        parser.error(f"输入不存在：{source}")
    if source.is_file() and source.suffix.lower() not in EXTENSIONS:
        parser.error(f"不支持的输入类型：{source.suffix}")
    files = sorted((p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONS)
                   if source.is_dir() else [source])
    if not files:
        parser.error("没有找到 .mflac 或 .mgg 文件")
    output.mkdir(parents=True, exist_ok=True)
    try:
        extract_auth, decrypt, fetch_key, parse_tail = load_qmdec()
        ffmpeg = ffmpeg_executable()
        auth = extract_auth()
    except (OSError, RuntimeError) as exc:
        print(f"准备失败：{exc}", file=sys.stderr)
        return 1
    failures = 0
    for path in files:
        try:
            print(convert_file(path, output, args.overwrite, ffmpeg, decrypt, fetch_key, parse_tail, auth))
        except (OSError, RuntimeError, ValueError) as exc:
            failures += 1
            print(f"失败：{path.name}：{exc}", file=sys.stderr)
    print(f"共 {len(files)} 首，成功 {len(files) - failures} 首，失败 {failures} 首")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
