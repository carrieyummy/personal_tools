"""复用已初始化的播放 SDK 研究清单目标，不切换当前播放窗口，不创建离线下载任务。"""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsv_local.playback.session import probe_playback


def main() -> int:
    """解析研究配置及清单下标；成功返回 0，失败返回 1，中断返回 130。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="专项 YAML 路径")
    parser.add_argument("--index", type=int, required=True, help="当前链接清单下标，从 0 开始")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--sample-media", action="store_true", help="获取并验证专项配置所选的有界短分段，不发布整集成品")
    mode.add_argument("--full-media", action="store_true", help="按该任务配置的 quality 获取链接指定集，完整验证后发布到主配置 output_path")
    args = parser.parse_args()
    try:
        report, output = probe_playback(args.config, args.index, args.sample_media, args.full_media)
        print(f"目标集校验通过：{report['video']['title']} 第 {report['video']['episode']} 集")
        print(f"原生视频候选 {len(report['native']['video'])} 个；资源来源：{report['resource_origin']}")
        print(f"会话研究报告：{output}")
        if args.sample_media:
            print(f"媒体样本全部通过：{report['media_samples']['all_samples_verified']}")
            return 0 if report["media_samples"]["all_samples_verified"] else 1
        return 0
    except KeyboardInterrupt:
        print("已停止播放会话研究。", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"播放会话研究失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
