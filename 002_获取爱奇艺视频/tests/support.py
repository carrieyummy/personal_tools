"""创建与用户路径、剧集及参数值无关的临时批量转换测试环境。"""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import tempfile
import unittest
import sqlite3
from unittest.mock import patch
from xml.etree import ElementTree as ET

import yaml

from qsv_local.batch import run_batch
from qsv_local.configuration import read_config, resolve_path
from qsv_local.storage import atomic_json, fingerprint

PROJECT = Path(__file__).resolve().parents[1]
MEDIA = {"duration_seconds": 1.0, "width": 16, "height": 16, "fps": 1.0,
         "video_codec": "fixture", "audio_codecs": ["fixture"], "errors": []}


class ProjectCase(unittest.TestCase):
    """从实际配置复制参数，使用临时输入/输出与隔离媒体替身的基础用例。"""

    def setUp(self) -> None:
        """从 configs/config.yaml 读取参数并按其目录解析适配配置；创建隔离路径及随机服务端口，无返回值。"""
        temporary = tempfile.TemporaryDirectory(prefix="qsv-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.input = self.root / "input"
        self.output = self.root / "output"
        self.work = self.root / "work"
        self.input.mkdir()
        config_path = PROJECT / "configs" / "config.yaml"
        self.raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        profile = resolve_path(self.raw["native_profile"], config_path.parent)
        self.raw.update(input_root=str(self.input), input_path=[], output_path=str(self.output),
                        work_dir=str(self.work), native_profile=str(profile))
        # 测试服务始终使用随机端口，避免占用用户明确配置的固定端口。
        self.raw["logging"]["port"] = 0
        self.raw["download_guard"]["database_path"] = str(self.root / "downloads.db")
        self.downloaded, self.downloading = set(), set()
        self.config_path = self.root / "fixture.yaml"
        self.write_downloads()
        self.write_config()
        self.config = read_config(self.config_path)
        self.console = io.StringIO()
        for name, replacement in (("mux", self.fake_mux), ("finalize_verification", self.fake_finalize)):
            patcher = patch(f"qsv_local.batch.{name}", side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_config(self) -> None:
        """将当前 self.raw 测试配置写入临时 YAML；无参数和返回值。"""
        self.config_path.write_text(yaml.safe_dump(self.raw, allow_unicode=True), encoding="utf-8")

    def source(self, relative: str = "series/episode.qsv", content: bytes = b"source-one") -> Path:
        """创建相对输入路径 relative 的源内容 content；返回绝对测试文件路径。"""
        path = self.input / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.downloaded.add(path)
        self.write_downloads()
        return path

    def write_downloads(self) -> None:
        """将测试完成/进行中清单写入临时 SQLite，路径和状态值复用配置；不读取源文件内容，无返回值。"""
        settings = self.raw["download_guard"]
        connection = sqlite3.connect(settings["database_path"])
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS table_file (Name TEXT PRIMARY KEY, Data BLOB)")
            for name, sources in ((settings["completed_record"], self.downloaded),
                                  (settings["active_record"], self.downloading)):
                root = ET.Element("Root")
                channels = ET.SubElement(root, "Chs")
                for source in sorted(sources):
                    ET.SubElement(channels, "Ch", {
                        "SaveDir": str(source.parent), "SaveFileName": source.name,
                        "State": str(settings["completed_states"][0]) if sources is self.downloaded else "active",
                    })
                data = ET.tostring(root, encoding=settings["xml_encoding"], xml_declaration=True)
                connection.execute("INSERT OR REPLACE INTO table_file (Name, Data) VALUES (?, ?)", (name, data))
            connection.commit()
        finally:
            connection.close()

    def fake_prepare(self, config: dict, source: Path, work: Path, notify, cancel_event=None) -> dict:
        """用合成媒体隔离准备阶段；config/source/work/notify 为正式接口，cancel_event 为取消信号；返回待验证报告。"""
        work.mkdir(parents=True)
        candidate = work / "validated.partial.mp4"
        candidate.write_bytes(b"fixture-mp4:" + source.read_bytes())
        notify("extracting", progress=50.0, detail="测试提取")
        report = {"status": "extracted", "source_unchanged": True, "source": str(source),
                  "source_fingerprint": fingerprint(source, config["copy_buffer_bytes"]),
                  "candidate": str(candidate), "output_fingerprint": fingerprint(candidate, config["copy_buffer_bytes"]),
                  "verification": {"frames": 1, "decode_exit_code": 0}, "media": deepcopy(MEDIA),
                  "conversion": {"video_pes": 1}, "license_version": "5", "preparation_seconds": 0}
        atomic_json(work / "report.json", report)
        return report

    def fake_verify(self, config: dict, report: dict, work: Path, notify, device: str, cancel_event=None) -> dict:
        """隔离 config/work 对应的解码进程；report 提供候选，notify/device/cancel_event 为正式接口；返回成功证明。"""
        notify("verifying", device=device, progress=99.0, detail="测试验证")
        report.update(status="decoded")
        report["verification"]["device"] = device
        atomic_json(work / "report.json", report)
        return report

    def fake_mux(self, config: dict, report: dict, work: Path, notify, cancel_event=None) -> dict:
        """模拟封装 config/report 指定的候选；work、notify 和 cancel_event 为阶段接口，返回待验证报告。"""
        report["status"] = "prepared"
        return report

    def fake_finalize(self, config: dict, report: dict, work: Path, notify, cancel_event=None) -> dict:
        """模拟 config/report 的后置完整性检查；work、notify 和 cancel_event 为阶段接口，返回成功证明。"""
        report["status"] = "verified"
        return report

    def batch(self) -> tuple[dict, object]:
        """使用隔离准备、验证和 GPU 检查执行当前配置；返回批次结果与准备 mock。"""
        with (redirect_stdout(self.console), redirect_stderr(self.console),
              patch("qsv_local.batch.prepare", side_effect=self.fake_prepare) as converter,
              patch("qsv_local.batch.verify", side_effect=self.fake_verify),
              patch("qsv_local.batch.check_gpu"),
              patch("qsv_local.migration.probe_media", return_value=deepcopy(MEDIA))):
            result = run_batch(self.config)
        return result, converter

    def old_report(self, source: Path) -> tuple[Path, dict]:
        """为 source 创建旧平铺成品及真实指纹成功报告；返回旧 MP4 与报告字典。"""
        self.output.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        old = self.output / "legacy.mp4"
        old.write_bytes(b"verified-legacy-video")
        report = {"status": "verified", "source": str(source), "output": str(old),
                  "source_sha256": fingerprint(source, self.config["copy_buffer_bytes"])["sha256"],
                  "output_sha256": fingerprint(old, self.config["copy_buffer_bytes"])["sha256"],
                  "source_unchanged": True, "verification": {"frames": 1, "decode_exit_code": 0},
                  "conversion": {"video_pes": 1}, "license_version": "5"}
        atomic_json(self.work / "report.json", report)
        return old, report
