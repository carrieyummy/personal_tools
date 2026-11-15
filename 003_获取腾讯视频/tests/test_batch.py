"""隔离验收批量筛选、来源保护、发布复用及并发槽；运行参数从实际配置复制。"""

from copy import deepcopy
from pathlib import Path
import tempfile
import json
from threading import Event, Lock, Thread
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from local.batch import Batch, index_path, load_index, slot
from local.configuration import load_config
from local.hls import discover_all
from local.journal import Journal
from local.storage import atomic_json, fingerprint, publish


class MemoryJournal:
    """测试用内存日志；记录任务阶段和终态，不启动网络服务。"""

    def __init__(self):
        """创建独立批次标识和记录列表，无参数。"""
        self.run, self.entries = {"id": "test_run"}, []

    def add(self, task):
        """为 task 返回新条目并登记；task 至少含 video_id 和 title。"""
        entry = dict(task)
        self.entries.append(entry)
        return entry

    def update(self, entry, stage=None, **fields):
        """更新 entry 的 stage 与 fields，无返回值。"""
        if stage:
            entry["stage"] = stage
        entry.update(fields)

    def record_resource(self, entry, resource, wait_seconds, execution_seconds):
        """记录 entry 的 resource 等待及执行秒数，供隔离测试检查；无返回值。"""
        timing = entry.setdefault("resources", {}).setdefault(resource, {"wait_seconds": 0.0, "execution_seconds": 0.0})
        timing["wait_seconds"] += wait_seconds
        timing["execution_seconds"] += execution_seconds


class BatchTests(unittest.TestCase):
    """围绕实际 YAML 构造隔离环境，验证批量行为而非真实 SDK。"""

    def setUp(self):
        """创建临时 XML、资源及配置；无参数或返回值。"""
        self.temporary = tempfile.TemporaryDirectory(prefix="tencent_batch_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = deepcopy(load_config(Path("configs/config.yaml"), source="download"))
        self.config["input_root"] = self.root / "input"
        self.config["output_path"] = self.root / "output"
        self.config["work_dir"] = self.root / "work"
        self.config["download_record"] = self.root / "download.xml"
        self.config["selection"] = {"video_ids": [], "series_names": []}
        self.config["logging"]["port"] = 0  # 隔离测试监听随机端口，避免与用户服务冲突。
        self.config["input_root"].mkdir()
        self.root_xml = ET.Element("qqlive")
        self.make_record("id_a", "covera", "剧甲", "第一集")
        self.make_record("id_b", "coverb", "剧乙", "第二集")
        self.write_xml()

    def make_record(self, video_id, series_id, series, title, completed=True):
        """添加单集 video_id、字母数字整剧编号 series_id、剧名 series 和标题 title；completed 指定完成状态，无返回值。"""
        fid = video_id + ".hls"
        folder = self.config["input_root"] / "account" / fid
        folder.mkdir(parents=True)
        (folder / "0.ts").write_bytes(b"H" * (18 + 188))
        (folder / ".m3u8").write_text("#EXTM3U\n#EXTINF:1,\na.ts?index=0&brs=0&bre=187\n#EXT-X-ENDLIST\n", encoding="utf-8")
        ET.SubElement(self.root_xml, "l", {"id": video_id, "vbarid": series_id, "vbarname": series, "fn": title,
                      "fid": fid, "cfid": "format", "cache_complete": str(completed).lower(),
                      "bHls": "1", "download_size": "188", "fs": "188"})

    def write_xml(self):
        """将当前内存 XML 保存至隔离的下载记录，无返回值。"""
        ET.ElementTree(self.root_xml).write(self.config["download_record"], encoding="utf-8", xml_declaration=True)

    def test_selection_union_and_incomplete(self):
        """验证 ID 和剧名取并集，未完成项单独记录。"""
        self.root_xml[1].set("cache_complete", "false")
        self.write_xml()
        self.config["selection"] = {"video_ids": ["id_a"], "series_names": ["剧乙"]}
        tasks, skipped = discover_all(self.config)
        self.assertEqual([t["video_id"] for t in tasks], ["id_a"])
        self.assertEqual([t["video_id"] for t in skipped], ["id_b"])

    def test_missing_selection_reported(self):
        """指定未登记的视频和剧名时分别报告跳过，不误处理其他记录。"""
        self.config["selection"] = {"video_ids": ["missing_id"], "series_names": ["未下载剧集"]}
        tasks, skipped = discover_all(self.config)
        self.assertEqual(tasks, [])
        self.assertEqual({item["video_id"] for item in skipped}, {"missing_id", "未下载剧集"})

    def test_invalid_series_id_is_skipped(self):
        """整剧编号缺失或含非法字符时跳过该集，其余有效剧集仍可发现。"""
        for series_id in (None, "", "../cover", "cover/other", "cover other"):
            with self.subTest(series_id=series_id):
                if series_id is None:
                    self.root_xml[0].attrib.pop("vbarid", None)
                else:
                    self.root_xml[0].set("vbarid", series_id)
                self.write_xml()
                tasks, skipped = discover_all(self.config)
                self.assertEqual([task["video_id"] for task in tasks], ["id_b"])
                self.assertEqual([item["video_id"] for item in skipped], ["id_a"])
                self.assertIn("vbarid", skipped[0]["detail"])

    def test_series_template_rejects_invalid_paths_and_fields(self):
        """模板不得漏掉剧名或编号，也不得利用字段访问或分隔符生成多层路径。"""
        task = discover_all(self.config)[0][0]
        for template in ("{title}", "{series_id}", "{title}_{unknown}", "{title}_{series_id.real}",
                         "{title}/{series_id}", "{title} {series_id}", "{title}_{series_id}.",
                         "{title}_{series_id!r}", "{title}_{series_id:.3}", "{title}_{series_id"):
            with self.subTest(template=template):
                self.config["naming"]["series_template"] = template
                with self.assertRaises(ValueError):
                    index_path(self.config, task)

    def test_versioned_publish_and_reuse(self):
        """同名不同编号的剧分开发布复用；未登记文件不覆盖，源变更不复用旧版。"""
        self.root_xml[1].set("vbarname", self.root_xml[0].get("vbarname"))
        self.root_xml[1].set("fn", self.root_xml[0].get("fn"))
        self.write_xml()
        task, other = discover_all(self.config)[0]
        journal = MemoryJournal()
        batch = Batch(self.config, journal)
        entry = journal.add(task)
        folder = index_path(self.config, task).parent
        expected = self.config["naming"]["series_template"].format(title=task["series"], series_id=task["series_id"])
        self.assertEqual(folder, self.config["output_path"] / expected)
        self.assertNotEqual(folder, index_path(self.config, other).parent)
        folder.mkdir(parents=True)
        original = folder / (task["title"] + ".mp4")
        original.write_bytes(b"external")

        def fake_extract(config, segments, folder, target, **kwargs):
            """生成隔离媒体并返回 segments 的真实摘要，供流程验证。"""
            target.write_bytes(b"media")
            result = []
            for segment in segments:
                fp = fingerprint(segment["path"], config["copy_buffer_bytes"], force=True)
                result.append({"path": str(segment["path"]), "size": fp["size_bytes"], "sha256": fp["sha256"]})
            return result

        def fake_process(command, log_path, config, *args, **kwargs):
            """模拟封装输出；command 最后一项为新候选文件，无返回值。"""
            Path(command[-1]).write_bytes(b"candidate")

        media = {"video_codec": "test", "audio_codecs": ["test"], "duration_seconds": 1}
        proof = {"device": "cpu", "frames": 1, "duration_seconds": 1, "exit_code": 0}
        with patch("local.batch.extract", fake_extract), patch("local.batch.run_process", fake_process), \
             patch.object(Batch, "media", return_value=media), patch.object(Batch, "verify", return_value=proof):
            result = batch.job(task, entry)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(original.read_bytes(), b"external")
            self.assertNotEqual(Path(result["output"]), original)
            self.assertEqual(len(load_index(index_path(self.config, task))["files"]), 1)
            self.assertEqual(batch.job(task, entry)["status"], "reused")
            other_result = batch.job(other, journal.add(other))
            self.assertEqual(other_result["status"], "completed")
            self.assertEqual(Path(other_result["output"]).parent, index_path(self.config, other).parent)
            self.assertEqual(batch.job(other, journal.add(other))["status"], "reused")
            self.assertEqual(batch.job(task, entry)["output"], result["output"])
            segment = next(task["folder"].rglob("0.ts"))
            segment.write_bytes(b"I" * segment.stat().st_size)
            changed = batch.job(task, entry)
            self.assertEqual(changed["status"], "completed")
            self.assertNotEqual(changed["output"], result["output"])

    def test_bounded_scheduler_and_failure_isolation(self):
        """检查在途上限及单项失败隔离；并发任务用事件同步，不依赖真实 SDK。"""
        tasks, _ = discover_all(self.config)
        self.config["max_in_flight"] = 2
        journal = MemoryJournal()
        batch = Batch(self.config, journal)
        entered, release, lock = Event(), Event(), Lock()
        active = {"count": 0, "maximum": 0}

        def fake_job(task, entry):
            """记录活跃数，模拟首项失败、次项成功；返回结果或抛错。"""
            with lock:
                active["count"] += 1
                active["maximum"] = max(active["maximum"], active["count"])
                if active["count"] == 2:
                    entered.set()
            release.wait(5)
            with lock:
                active["count"] -= 1
            if task["video_id"] == "id_a":
                raise ValueError("测试失败")
            return {"status": "completed", "output": "result"}

        def unlock():
            """待两项同时进入后释放任务，无返回值。"""
            entered.wait(5)
            release.set()

        helper = Thread(target=unlock)
        helper.start()
        with patch.object(batch, "job", side_effect=fake_job):
            summary = batch.run(tasks)
        helper.join()
        self.assertTrue(entered.is_set())
        self.assertEqual(active["maximum"], 2)
        self.assertEqual((summary["completed"], summary["failed"]), (1, 1))

    def test_locked_slot_responds_to_cancel(self):
        """验证等待资源槽的任务能响应取消信号。"""
        from threading import Semaphore
        from local.runtime import TaskCancelled
        sem = Semaphore(0)
        cancel = Event()
        cancel.set()
        with self.assertRaises(TaskCancelled):
            with slot(sem, cancel, 0.01):
                pass

    def test_resource_records_wait_and_execution_separately(self):
        """占用资源槽后让任务等待，再释放并核对等待与持有耗时分别记录。"""
        batch = Batch(self.config, MemoryJournal())
        entry, waiting, entered = {}, Event(), Event()
        batch.prepare.acquire()

        def worker():
            """等待并短暂持有还原槽；无参数或返回值。"""
            waiting.set()
            with batch.resource(entry, "extract", batch.prepare):
                entered.set()
                time.sleep(0.05)

        thread = Thread(target=worker)
        thread.start()
        self.assertTrue(waiting.wait(5))
        time.sleep(0.05)
        self.assertFalse(entered.is_set())
        batch.prepare.release()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertGreater(entry["resources"]["extract"]["wait_seconds"], 0)
        self.assertGreater(entry["resources"]["extract"]["execution_seconds"], 0)

    def test_distinct_stages_overlap_and_prepare_is_exclusive(self):
        """确认还原槽独占且封装槽可同时运行，使用事件同步验证重叠。"""
        batch = Batch(self.config, MemoryJournal())
        prepared, muxed, release, second = Event(), Event(), Event(), Event()

        def first_prepare():
            """持有还原槽直到 release；无参数或返回值。"""
            with slot(batch.prepare, batch.cancel, batch.interval):
                prepared.set()
                release.wait(5)

        def second_prepare():
            """等待同一还原槽并通知 second；无参数或返回值。"""
            prepared.wait(5)
            with slot(batch.prepare, batch.cancel, batch.interval):
                second.set()

        def mux():
            """在还原槽占用时获取封装槽；无参数或返回值。"""
            prepared.wait(5)
            with slot(batch.mux, batch.cancel, batch.interval):
                muxed.set()

        workers = [Thread(target=f) for f in (first_prepare, second_prepare, mux)]
        for worker in workers:
            worker.start()
        self.assertTrue(muxed.wait(5))
        self.assertFalse(second.is_set())
        release.set()
        for worker in workers:
            worker.join(5)
        self.assertTrue(second.is_set())

    def test_failed_verification_preserves_external_file(self):
        """验证完整解码失败时不发布、不覆盖已有成品且保存失败现场。"""
        task = discover_all(self.config)[0][0]
        journal = MemoryJournal()
        batch = Batch(self.config, journal)
        folder = index_path(self.config, task).parent
        folder.mkdir(parents=True)
        target = folder / (task["title"] + ".mp4")
        target.write_bytes(b"existing")

        def fake_extract(config, segments, folder, target, **kwargs):
            """写入测试媒体并按 segments 返回来源摘要。"""
            target.write_bytes(b"media")
            result = []
            for segment in segments:
                fp = fingerprint(segment["path"], config["copy_buffer_bytes"], force=True)
                result.append({"path": str(segment["path"]), "size": fp["size_bytes"], "sha256": fp["sha256"]})
            return result

        def fake_process(command, log_path, config, *args, **kwargs):
            """写入测试候选文件；command 最后一项是输出路径，无返回值。"""
            Path(command[-1]).write_bytes(b"candidate")

        media = {"video_codec": "test", "audio_codecs": ["test"], "duration_seconds": 1}
        with patch("local.batch.extract", fake_extract), patch("local.batch.run_process", fake_process), \
             patch.object(Batch, "media", return_value=media), patch.object(Batch, "verify", side_effect=ValueError("损坏")):
            with self.assertRaisesRegex(ValueError, "损坏"):
                batch.job(task, journal.add(task))
        self.assertEqual(target.read_bytes(), b"existing")
        self.assertFalse(index_path(self.config, task).exists())
        self.assertEqual(len(list((self.config["work_dir"] / journal.run["id"]).glob("*/report.json"))), 1)

    def test_changed_source_blocks_final_publish(self):
        """模拟目标盘复制后来源复核失败，确保没有最终成品且临时文件被清理。"""
        source, target = self.root / "candidate.mp4", self.root / "final.mp4"
        source.write_bytes(b"test media")
        digest = fingerprint(source, self.config["copy_buffer_bytes"], force=True)["sha256"]

        def reject():
            """模拟来源在复制期间变化，无参数；通过异常阻止发布。"""
            raise ValueError("来源变化")

        with self.assertRaisesRegex(ValueError, "来源变化"):
            publish(source, target, digest, self.config["copy_buffer_bytes"], before_publish=reject)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob("*.partial")), [])

    def test_copy_does_not_hold_source_or_index_lock(self):
        """复制进行时来源槽和索引锁可被其他任务获取，最终仍完成登记。"""
        task = discover_all(self.config)[0][0]
        journal = MemoryJournal()
        batch = Batch(self.config, journal)
        copying, release = Event(), Event()
        outcome = []

        def fake_extract(config, segments, folder, target, **kwargs):
            """生成候选媒体并返回 segments 的真实摘要。"""
            target.write_bytes(b"media")
            return [{"path": str(s["path"]), "size": s["size"],
                     "sha256": fingerprint(s["path"], config["copy_buffer_bytes"], force=True)["sha256"]} for s in segments]

        def fake_process(command, log_path, config, *args, **kwargs):
            """向 command 指定的输出写入隔离候选文件。"""
            Path(command[-1]).write_bytes(b"candidate")

        def slow_publish(*args, **kwargs):
            """暂停复制，等待 release 后调用真实发布函数并返回成品指纹。"""
            copying.set()
            if not release.wait(5):
                raise TimeoutError("测试未释放复制")
            return publish(*args, **kwargs)

        def run_job():
            """运行测试任务并记录结果或异常，无参数或返回值。"""
            try:
                outcome.append(batch.job(task, journal.add(task)))
            except Exception as error:
                outcome.append(error)

        media = {"video_codec": "test", "audio_codecs": ["test"], "duration_seconds": 1}
        proof = {"device": "cpu", "frames": 1, "duration_seconds": 1, "exit_code": 0}
        with patch("local.batch.extract", fake_extract), patch("local.batch.run_process", fake_process), \
             patch("local.batch.publish", slow_publish), patch.object(Batch, "media", return_value=media), \
             patch.object(Batch, "verify", return_value=proof):
            worker = Thread(target=run_job)
            worker.start()
            try:
                self.assertTrue(copying.wait(5))
                self.assertTrue(batch.check.acquire(timeout=1))
                batch.check.release()
                self.assertTrue(batch.index_lock.acquire(timeout=1))
                batch.index_lock.release()
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome[0]["status"], "completed")
        self.assertEqual(len(load_index(index_path(self.config, task))["files"]), 1)

    def test_source_change_during_copy_blocks_job_publication(self):
        """复制期间来源变化时，完整任务不得发布或写入索引。"""
        task = discover_all(self.config)[0][0]
        journal = MemoryJournal()
        batch = Batch(self.config, journal)

        def fake_extract(config, segments, folder, target, **kwargs):
            """写入测试媒体并返回分片的实际摘要。"""
            target.write_bytes(b"media")
            return [{"path": str(s["path"]), "size": s["size"],
                     "sha256": fingerprint(s["path"], config["copy_buffer_bytes"], force=True)["sha256"]} for s in segments]

        def fake_process(command, log_path, config, *args, **kwargs):
            """将测试候选文件写入 command 的输出位置。"""
            Path(command[-1]).write_bytes(b"candidate")

        def changed_publish(*args, **kwargs):
            """在真实发布的最终复核前修改来源；返回真实发布结果或异常。"""
            original = kwargs["before_publish"]
            def changed_check():
                """保持分片长度并修改内容，然后运行原有复核；无返回值。"""
                segment = next(task["folder"].rglob("0.ts"))
                segment.write_bytes(b"I" * segment.stat().st_size)
                original()
            kwargs["before_publish"] = changed_check
            return publish(*args, **kwargs)

        media = {"video_codec": "test", "audio_codecs": ["test"], "duration_seconds": 1}
        proof = {"device": "cpu", "frames": 1, "duration_seconds": 1, "exit_code": 0}
        with patch("local.batch.extract", fake_extract), patch("local.batch.run_process", fake_process), \
             patch("local.batch.publish", changed_publish), patch.object(Batch, "media", return_value=media), \
             patch.object(Batch, "verify", return_value=proof):
            with self.assertRaisesRegex(ValueError, "复制成品期间"):
                batch.job(task, journal.add(task))
        self.assertFalse(index_path(self.config, task).exists())
        self.assertFalse(list(self.config["output_path"].rglob("*.mp4")))
        self.assertFalse(list(self.config["output_path"].rglob("*.partial")))

    def test_concurrent_same_video_publishes_once(self):
        """两个同来源任务并发时只登记一个成品，另一任务复用已登记成品。"""
        task = discover_all(self.config)[0][0]
        self.config["max_in_flight"] = 2
        journal = MemoryJournal()
        batch = Batch(self.config, journal)
        second_extract = Event()
        extract_count = 0

        def fake_extract(config, segments, folder, target, **kwargs):
            """写入候选媒体并返回分片摘要。"""
            nonlocal extract_count
            extract_count += 1
            if extract_count == 2:
                second_extract.set()
            target.write_bytes(b"media")
            return [{"path": str(s["path"]), "size": s["size"],
                     "sha256": fingerprint(s["path"], config["copy_buffer_bytes"], force=True)["sha256"]} for s in segments]

        def fake_process(command, log_path, config, *args, **kwargs):
            """向 command 指定的输出写入测试候选文件。"""
            Path(command[-1]).write_bytes(b"candidate")

        def synchronized_publish(*args, **kwargs):
            """等待第二项开始还原后发布，确保两项确实同时在途。"""
            if not second_extract.wait(5):
                raise TimeoutError("第二项未进入还原")
            return publish(*args, **kwargs)

        media = {"video_codec": "test", "audio_codecs": ["test"], "duration_seconds": 1}
        proof = {"device": "cpu", "frames": 1, "duration_seconds": 1, "exit_code": 0}
        with patch("local.batch.extract", fake_extract), patch("local.batch.run_process", fake_process), \
             patch("local.batch.publish", synchronized_publish), patch.object(Batch, "media", return_value=media), \
             patch.object(Batch, "verify", return_value=proof):
            summary = batch.run([task, task])
        self.assertTrue(second_extract.is_set())
        self.assertEqual((summary["completed"], summary["reused"], summary["failed"]), (1, 1, 0))
        self.assertEqual(len(load_index(index_path(self.config, task))["files"]), 1)

    def test_index_failure_rolls_back_published_file(self):
        """索引登记失败时删除本次新发布的文件，保留失败报告。"""
        task = discover_all(self.config)[0][0]
        journal = MemoryJournal()
        batch = Batch(self.config, journal)

        def fake_extract(config, segments, folder, target, **kwargs):
            """写入测试媒体并返回来源分片摘要。"""
            target.write_bytes(b"media")
            return [{"path": str(s["path"]), "size": s["size"],
                     "sha256": fingerprint(s["path"], config["copy_buffer_bytes"], force=True)["sha256"]} for s in segments]

        def fake_process(command, log_path, config, *args, **kwargs):
            """将测试候选内容写入 command 指定的输出。"""
            Path(command[-1]).write_bytes(b"candidate")

        def reject_index(path, data):
            """拒绝当前任务的索引写入，其余记录交由真实实现保存。"""
            if path == index_path(self.config, task):
                raise OSError("索引写入失败")
            atomic_json(path, data)

        media = {"video_codec": "test", "audio_codecs": ["test"], "duration_seconds": 1}
        proof = {"device": "cpu", "frames": 1, "duration_seconds": 1, "exit_code": 0}
        with patch("local.batch.extract", fake_extract), patch("local.batch.run_process", fake_process), \
             patch("local.batch.atomic_json", reject_index), patch.object(Batch, "media", return_value=media), \
             patch.object(Batch, "verify", return_value=proof):
            with self.assertRaisesRegex(OSError, "索引写入失败"):
                batch.job(task, journal.add(task))
        self.assertFalse(index_path(self.config, task).exists())
        self.assertFalse(list(self.config["output_path"].rglob("*.mp4")))
        reports = list((self.config["work_dir"] / journal.run["id"]).glob("*/report.json"))
        self.assertEqual(json.loads(reports[0].read_text(encoding="utf-8"))["status"], "failed")

    def test_finished_stage_time_stops_before_batch_close(self):
        """条目终态立即结束阶段计时，批次关闭等待不计入该条目。"""
        journal = Journal(self.config)
        entry = journal.add({"video_id": "timing", "title": "timing"})
        journal.update(entry, "publishing", status="running")
        journal.update(entry, status="completed")
        elapsed = entry["stages"]["publishing"]
        time.sleep(0.1)
        journal.close("completed", {"completed": 1})
        self.assertEqual(entry["stages"]["publishing"], elapsed)
        self.assertGreaterEqual(journal.run["elapsed_seconds"], 0.1)

    def test_interrupted_history_is_reconciled(self):
        """启动日志时将上次未结束的批次和条目标记为中断，并保留离线报告。"""
        output = self.config["output_path"]
        output.mkdir(parents=True)
        path = output / self.config["logging"]["state_file"]
        history = {"version": 1, "runs": [{"id": "previous", "status": "running", "entries": [
            {"video_id": "previous_video", "status": "running", "_start": 1}]}]}
        path.write_text(json.dumps(history), encoding="utf-8")
        journal = Journal(self.config)
        report = journal.close("completed", {})
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["runs"][0]["status"], "interrupted")
        self.assertEqual(saved["runs"][0]["entries"][0]["status"], "interrupted")
        self.assertTrue(report.exists())


if __name__ == "__main__":
    unittest.main()
