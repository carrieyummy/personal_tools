"""配置、断点、PES 边界及并发资源的隔离测试；不访问真实客户端或网络。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from threading import Barrier, Event, Lock, current_thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from Crypto.Cipher import AES
import yaml

from local.batch import Batch
from local.application import UrlBatch, verification_valid, cleanup_media
from local.configuration import load_config
from local.download import download, SegmentLimitError
from local.scheduler import Scheduler
from local.runtime import TaskCancelled, check_cancel
from local.storage import atomic_json, fingerprint, publish, read_json, RecordWriteError
from local.errors import safe_message
from local.tasks import requests, video_id
from local.youku.transport import decrypt_transport, restore_files, normalize_continuity
from local.youku.manifest import parse_manifest

ROOT = Path(__file__).resolve().parents[1]


def fixture(key: bytes) -> tuple[bytes, bytes]:
    """以 key 构造跨 TS 包且尾部不对齐的音视频 PES；返回密文与预期明文。"""
    encrypted, clear = bytearray(), bytearray()
    cipher = AES.new(key,AES.MODE_ECB)
    for pid, stream in ((256,0xE0),(257,0xC0)):
        payload = bytes(range(251))*2+b"tail"
        aligned = len(payload)//16*16
        encoded = cipher.encrypt(payload[:aligned])+payload[aligned:]
        header = b"\0\0\1"+bytes([stream])+(len(payload)+3).to_bytes(2,"big")+b"\x80\0\0"
        for target, body in ((encrypted,header+encoded),(clear,header+payload)):
            for number,offset in enumerate(range(0,len(body),160)):
                chunk = body[offset:offset+160]
                adaptation = 183-len(chunk)
                target.extend(bytes([0x47,(pid>>8)|(0x40 if number==0 else 0),pid&255,0x30|number]))
                target.extend(bytes([adaptation,0])+b"\xff"*(adaptation-1)+chunk)
    return bytes(encrypted),bytes(clear)


class JournalStub:
    """线程安全的测试状态收集器；不写盘、不运行 HTTP 服务。"""

    def __init__(self):
        """初始化独立条目列表及锁；无参数。"""
        self.lock, self.entries, self.run = Lock(), [], {}

    def add(self, task):
        """登记 task 的副本并返回条目。"""
        return self.add_many([task])[0]

    def add_many(self, tasks, discovery=None, memberships=None):
        """批量收集 tasks 及 discovery 进度；memberships 补充分组，返回新条目列表。"""
        entries = [deepcopy(task) for task in tasks]
        with self.lock:
            self.entries.extend(entries)
            for entry, group in memberships or []:
                if group not in entry.setdefault("groups", []):
                    entry["groups"].append(deepcopy(group))
            if discovery is not None:
                self.run["discovery"] = deepcopy(discovery)
        return entries

    def update(self, entry, stage=None, **values):
        """在线程锁下更新 entry 的阶段及 values；无返回值。"""
        with self.lock:
            entry.update(values)
            if stage:
                entry["stage"] = stage

    def record_resource(self, entry, name, waited, held):
        """登记 entry 的 name 资源等待和持有时间；无返回值。"""
        with self.lock:
            entry.setdefault("resources",{})[name] = (waited,held)


class PipelineTests(unittest.TestCase):
    """从用户当前配置派生测试上下文，验证格式与资源行为而非固定默认值。"""

    def setUp(self):
        """读取当前配置，创建独立临时现场；无参数及返回值。"""
        self.config = load_config(ROOT / "configs/config.yaml")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_config_and_list_override(self):
        """简单链接列表继承清单全局设置，且不改变用户原清单。"""
        self.assertTrue(self.config["work_dir"].is_absolute())
        path = self.root / "list.yaml"
        quality = self.config["download"]["quality"] + 1
        whole = not self.config["download"]["all_parts"]
        path.write_text(yaml.safe_dump({"quality": quality, "all-parts": whole,
                                       "download_list": ["https://v.youku.com/video?vid=Xabc=="]}), encoding="utf-8")
        config = dict(self.config,default_list=path)
        item = requests(config)[0]
        self.assertEqual(item["quality"], quality)
        self.assertEqual(item["all_parts"], whole)
        self.assertEqual(video_id("https://v.youku.com/v_show/id_Xabc==.html"),"Xabc==")
        with self.assertRaises(ValueError):
            video_id("https://evil.invalid/video?vid=Xabc==")
        for invalid in ({"url": "https://v.youku.com/video?vid=Xabc=="}, "", " https://v.youku.com/video?vid=Xabc=="):
            path.write_text(yaml.safe_dump({"download_list": [invalid]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                requests(config)

    def test_atomic_record_retries_transient_windows_denial(self):
        """共享盘首次拒绝原子替换时等待后重试，成功前保持旧记录完整，不重写临时内容。"""
        path=self.root / "record.json"
        path.write_text('{"old":true}',encoding="utf-8")
        options={**self.config["storage"],"replace_attempts":max(2,self.config["storage"]["replace_attempts"])}
        denied=PermissionError(13,"private-response");denied.winerror=5
        replace=Path.replace
        calls=[]
        def transient(source,target):
            """首次替换 source 到 target 时模拟系统拒绝；后续执行真实原子替换并返回目标 Path。"""
            calls.append(source)
            if len(calls)==1:
                self.assertEqual(read_json(target),{"old":True})
                raise denied
            return replace(source,target)
        with patch.object(Path,"replace",transient),patch("local.storage.time.sleep") as sleep:
            atomic_json(path,{"new":True},options)
        self.assertEqual(read_json(path),{"new":True})
        self.assertEqual(calls[0],calls[1])
        sleep.assert_called_once_with(options["retry_interval_seconds"])
        self.assertEqual(list(self.root.glob("*.tmp")),[])

    def test_atomic_record_exhaustion_preserves_old_file_and_diagnostics(self):
        """持续拒绝访问时按配置次数停止，旧文件不变，诊断包含路径系统码而不含原始异常文本。"""
        path=self.root / "record.json"
        path.write_text('{"old":true}',encoding="utf-8")
        denied=PermissionError(13,"private-response");denied.winerror=5
        with patch.object(Path,"replace",side_effect=denied) as replace,patch("local.storage.time.sleep") as sleep:
            with self.assertRaises(RecordWriteError) as raised:
                atomic_json(path,{"new":True},self.config["storage"])
        self.assertEqual(replace.call_count,self.config["storage"]["replace_attempts"])
        self.assertEqual(sleep.call_count,self.config["storage"]["replace_attempts"]-1)
        self.assertEqual(read_json(path),{"old":True})
        self.assertEqual(list(self.root.glob("*.tmp")),[])
        message=safe_message(raised.exception)
        self.assertIn(str(path),message)
        self.assertIn("winerror=5",message)
        self.assertNotIn("private-response",message)

    def test_atomic_record_does_not_retry_unrelated_io_failure(self):
        """磁盘满等非共享冲突立即失败，不把所有 I/O 错误当成瞬时占用。"""
        with patch.object(Path,"replace",side_effect=OSError(28,"private-response")) as replace, \
                patch("local.storage.time.sleep") as sleep:
            with self.assertRaises(RecordWriteError):
                atomic_json(self.root / "record.json",{},self.config["storage"])
        self.assertEqual(replace.call_count,1)
        sleep.assert_not_called()

    def test_cross_segment_pes_and_clear_segments(self):
        """PES 横跨文件边界仍精确还原，明文段保持所有字节不变。"""
        key = bytes(range(16))
        encrypted, clear = fixture(key)
        self.assertEqual(decrypt_transport(encrypted,key)[0],clear)
        parts = [encrypted[:188*2],encrypted[188*2:],clear]
        paths, records, segments = [], {}, []
        for index, part in enumerate(parts):
            path = self.root / f"{index}.ts"
            path.write_bytes(part)
            paths.append(path)
            records[str(index)]={"bytes":len(part),"sha256":hashlib.sha256(part).hexdigest()}
            segments.append({"private":{"DRM_NOTENCRYPT":int(index==2)},"discontinuity":int(index==2)})
        target = self.root / "restored.ts"
        stats = restore_files(paths,segments,records,key,target,self.config["copy_buffer_bytes"],self.config["playback"]["media"]["max_pes_bytes"],Event())
        self.assertEqual(target.read_bytes(),clear+clear)
        self.assertEqual(stats["video_pes"],2)

    def test_changed_source_and_pes_limit_rejected(self):
        """摘要不符或 PES 超过缓冲上限时拒绝产出成功。"""
        key=bytes(range(16)); encrypted,_=fixture(key)
        path=self.root / "source.ts"; path.write_bytes(encrypted)
        records={"0":{"bytes":len(encrypted),"sha256":"0"*64}}
        segments=[{"private":{},"discontinuity":0}]
        with self.assertRaisesRegex(ValueError,"Changed"):
            restore_files([path],segments,records,key,self.root / "bad.ts",self.config["copy_buffer_bytes"],1024,Event())
        records["0"]["sha256"]=hashlib.sha256(encrypted).hexdigest()
        with self.assertRaisesRegex(ValueError,"Limit"):
            restore_files([path],segments,records,key,self.root / "limit.ts",self.config["copy_buffer_bytes"],32,Event())

    def test_bare_clear_flag_is_not_encrypted(self):
        """实测无等号 DRM_NOTENCRYPT 必须识别为明文；重复或非法保护值须拒绝。"""
        raw=b"#EXTM3U\n#EXTINF:4,\n#EXT-X-PRIVINF:FILESIZE=188,DRM_NOTENCRYPT\na.ts\n#EXT-X-ENDLIST\n"
        manifest=parse_manifest(raw,"https://cdn.invalid/list")
        self.assertEqual(manifest["segments"][0]["private"]["DRM_NOTENCRYPT"],1)
        with self.assertRaises(ValueError):
            parse_manifest(raw.replace(b"DRM_NOTENCRYPT",b"DRM_NOTENCRYPT=1,DRM_NOTENCRYPT"),"https://cdn.invalid/list")

    def test_continuity_normalization_preserves_every_payload(self):
        """跨段连续计数只修改 TS 头低四位，所有 PES、时间戳和音视频数据保持原样。"""
        _,clear=fixture(bytes(range(16)))
        original=clear+clear
        path=self.root / "joined.ts";path.write_bytes(original)
        normalize_continuity(path,Event())
        restored=path.read_bytes()
        counters={}
        for offset in range(0,len(original),188):
            self.assertEqual(original[offset:offset+3],restored[offset:offset+3])
            self.assertEqual(original[offset+4:offset+188],restored[offset+4:offset+188])
            self.assertEqual(original[offset+3]&0xF0,restored[offset+3]&0xF0)
            pid=((restored[offset+1]&31)<<8)|restored[offset+2]
            cc=restored[offset+3]&15
            if pid in counters:self.assertEqual(cc,(counters[pid]+1)%16)
            counters[pid]=cc

    def test_download_resume_rehashes_and_never_saves_urls(self):
        """可靠断点可复用，改写片段重新下载，签名不进入记录。"""
        data=b"fixture-media"
        source=SimpleNamespace(identity="id",segments=[{"url":"https://cdn.invalid/secret?token=private","private":{"FILESIZE":len(data)}}])
        def transfer_stub(url,path,config,expected,cancel):
            """替代传输，将已知字节写入 path 并返回摘要；其余参数遵循生产签名。"""
            path.write_bytes(data)
            return {"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}
        with patch("local.download.transfer",side_effect=transfer_stub) as transfer:
            first=download(source,self.root,self.config,Event(),lambda:source,lambda **values:None)
            second=download(source,self.root,self.config,Event(),lambda:source,lambda **values:None)
            self.assertEqual(transfer.call_count,1)
            self.assertEqual(second[1]["reused_segments"],1)
            first[0][0].write_bytes(b"changed")
            download(source,self.root,self.config,Event(),lambda:source,lambda **values:None)
            self.assertEqual(transfer.call_count,2)
        ledger=(self.root / "segments.json").read_text()
        self.assertNotIn("token",ledger)
        self.assertNotIn("secret",ledger)

    def test_download_rejects_oversize_manifest_before_transfer(self):
        """声明大小超限时提前明确指出段号与配置上限，不下载也不消耗重试。"""
        size=self.config["playback"]["media"]["max_segment_bytes"]+1
        source=SimpleNamespace(identity="id",segments=[{"url":"https://cdn.invalid/private", "private":{"FILESIZE":size}}])
        with patch("local.download.transfer") as transfer:
            with self.assertRaises(SegmentLimitError) as raised:
                download(source,self.root,self.config,Event(),lambda:source,lambda **values:None)
        transfer.assert_not_called()
        self.assertIn(str(size),str(raised.exception))
        self.assertIn("max_segment_bytes",str(raised.exception))

    def test_download_preserves_safe_reason_and_does_not_retry_limits(self):
        """无清单大小时仍拒绝对响应超限重试；其他传输失败保留具体脱敏原因和段号。"""
        source=SimpleNamespace(identity="id",segments=[{"url":"https://cdn.invalid/private", "private":{}}])
        with patch("local.download.transfer",side_effect=SegmentLimitError("分段响应超限")) as transfer:
            with self.assertRaises(SegmentLimitError):
                download(source,self.root,self.config,Event(),lambda:source,lambda **values:None)
        self.assertEqual(transfer.call_count,1)
        from local.youku.client import PlaybackError
        with patch("local.download.transfer",side_effect=PlaybackError("分段长度不符：实收=1，HTTP声明=2")) as transfer:
            with self.assertRaisesRegex(PlaybackError,"1/1.*实收=1"):
                download(source,self.root,self.config,Event(),lambda:source,lambda **values:None)
        self.assertEqual(transfer.call_count,self.config["download"]["retries"]+1)

    def test_publish_does_not_overwrite(self):
        """成品发布失败不能覆盖原文件，复制摘要错误也不产生成品。"""
        source=self.root / "candidate.mp4"; source.write_bytes(b"new")
        target=self.root / "existing.mp4"; target.write_bytes(b"old")
        digest=hashlib.sha256(b"new").hexdigest()
        with self.assertRaises(OSError):
            publish(source,target,digest,self.config["copy_buffer_bytes"])
        self.assertEqual(target.read_bytes(),b"old")
        with self.assertRaises(RuntimeError):
            publish(source,self.root / "wrong.mp4","0"*64,self.config["copy_buffer_bytes"])
        self.assertFalse((self.root / "wrong.mp4").exists())

    def test_success_flag_without_audio_frames_is_not_proof(self):
        """零退出码不能代替完整验证，缺音轨帧或音轨时长不符必须拒绝复用。"""
        proof={"exit_code":0,"device":"gpu","frames":25,"duration_seconds":1,"audio_frames":{"0":1},"audio_duration_seconds":{"0":1}}
        tolerance=self.config["duration_tolerance_seconds"]
        self.assertTrue(verification_valid(proof,1,tolerance))
        self.assertFalse(verification_valid({**proof,"audio_frames":{"0":0}},1,tolerance))
        self.assertFalse(verification_valid({**proof,"audio_duration_seconds":{"0":1+tolerance*2}},1,tolerance))

    def test_cleanup_removes_success_work(self):
        """成功清理删除媒体、日志及证明所在的完整工作目录。"""
        processing=self.root / "processing/run";processing.mkdir(parents=True)
        segments=self.root / "segments";segments.mkdir()
        (segments / "000.ts").write_bytes(b"media")
        for name in ("candidate.mp4","restored.ts","group.m3u8","groups.ffconcat","decode.log","audio.framehash","restoration.json"):
            (processing / name).write_bytes(b"fixture")
        atomic_json(self.root / "candidate.json",{"proof":"fixture"},self.config["storage"])
        self.assertIsNone(cleanup_media(self.root))
        self.assertFalse(self.root.exists())

    def test_cleanup_keeps_only_requested_media(self):
        """配置保留中间媒体时，仍清理工作日志、帧哈希和证明文件。"""
        processing=self.root / "processing/run";processing.mkdir(parents=True)
        segments=self.root / "segments";segments.mkdir()
        for path in (segments / "000.ts", processing / "candidate.mp4", processing / "decode.log",
                     processing / "audio.framehash", self.root / "candidate.json"):
            path.write_bytes(b"fixture")
        self.assertIsNone(cleanup_media(self.root,True))
        self.assertTrue((segments / "000.ts").exists())
        self.assertTrue((processing / "candidate.mp4").exists())
        self.assertFalse((processing / "decode.log").exists())
        self.assertFalse((processing / "audio.framehash").exists())
        self.assertFalse((self.root / "candidate.json").exists())

    def test_expired_url_refresh_is_independent_of_transport_retries(self):
        """普通传输重试为零时仍允许配置中的地址续签，刷新后必须实际重试该分段。"""
        config=deepcopy(self.config);config["download"]["retries"]=0
        data=b"media"
        source=SimpleNamespace(identity="id",segments=[{"url":"https://cdn.invalid/expired","private":{"FILESIZE":len(data)}}])
        fresh=SimpleNamespace(identity="id",segments=[{"url":"https://cdn.invalid/fresh","private":{"FILESIZE":len(data)}}])
        def transfer_stub(url,path,config,expected,cancel):
            """过期 url 返回 HTTP 403，新 url 写入 path；其他参数与正式传输签名一致。"""
            if url.endswith("expired"):
                raise HTTPError(url,403,"expired",None,None)
            path.write_bytes(data)
            return {"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}
        with patch("local.download.transfer",side_effect=transfer_stub), patch("local.download.time.monotonic",side_effect=[0,1]):
            _,proof=download(source,self.root,config,Event(),lambda:fresh,lambda **values:None)
        self.assertEqual(proof["refreshes"],1)

    def test_url_batch_routes_resolution_and_refresh_outside_download_pool(self):
        """正式单集生成器将初始解析、续签、后处理交给处理池，两次媒体传输都只在下载池运行。"""
        config=deepcopy(self.config)
        config["work_dir"]=self.root / "work"
        config["output_path"]=self.root / "output"
        journal=JournalStub();journal.run={"id":"fixture"}
        client=SimpleNamespace(stopped=Event(),diagnostics=lambda:{})
        batch=UrlBatch(config,journal,client)
        source=SimpleNamespace(identity="fixture",description={})
        calls=[]

        def resolve_stub(client,task,expected=None):
            """按 client/task 返回模拟来源；expected 标识续签，解析必须位于共享处理池。"""
            self.assertTrue(current_thread().name.startswith("youku-process"))
            calls.append("refresh" if expected is not None else "resolve")
            return source

        def download_stub(source,work,config,cancel,notify):
            """模拟 source 首次传输过期及续签后成功；其余参数沿用正式签名，返回路径和字节证据。"""
            self.assertTrue(current_thread().name.startswith("youku-download"))
            calls.append("download")
            refreshed=yield "refresh"
            self.assertIs(refreshed,source)
            self.assertTrue(current_thread().name.startswith("youku-download"))
            calls.append("download")
            return [],{"bytes":1}

        def restore_stub(*args):
            """确认后处理已进入共享池后主动终止；args 是正式还原参数，不访问真实媒体。"""
            self.assertTrue(current_thread().name.startswith("youku-process"))
            calls.append("processing")
            raise ValueError("fixture stops before native restoration")

        task={"video_id":"fixture","series_id":"fixture","title":"fixture"}
        with patch("local.application.resolve",side_effect=resolve_stub), \
                patch("local.application.download_steps",side_effect=download_stub), \
                patch.object(batch,"reuse",return_value=None), \
                patch.object(batch,"verified_candidate",return_value=None), \
                patch.object(batch,"restore",side_effect=restore_stub):
            self.assertEqual(batch.run([task])["failed"],1)
        self.assertEqual(calls,["resolve","download","refresh","download","processing"])

    def test_cancel_stops_dispatch_and_preserves_terminal_counts(self):
        """取消只中断已开始任务，未派发任务不执行生成器内容，所有条目有终态。"""
        config=deepcopy(self.config);config["download"]["threads"]=1;config["max_in_flight"]=1
        started, seen = Event(), []
        class Demo(Scheduler):
            """可取消的下载阶段替身；不访问磁盘与网络。"""
            def __init__(self):
                """初始化独立取消信号、日志和配置；无参数。"""
                self.config,self.cancel,self.journal,self.interval=config,Event(),JournalStub(),config["runtime"]["cancel_poll_interval_seconds"]
            def dispatch_stop_reason(self):
                """本测试不模拟认证拒绝；返回 None。"""
                return None
            def notify(self,entry,stage,**values):
                """记录 entry 的阶段和字段 values；无返回值。"""
                self.journal.update(entry,stage,**values)
            def _job_steps(self,task,entry):
                """登记 task 开始并等待取消；entry 为日志条目，取消时抛异常而不进入后处理。"""
                yield "download",0
                check_cancel(self.cancel)
                seen.append(task["index"]);started.set()
                self.cancel.wait(timeout=10)
                check_cancel(self.cancel)
                yield "ready",1
                return {"status":"completed"}
        batch=Demo()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(batch.run,[{"index":i,"video_id":str(i)} for i in range(3)])
            self.assertTrue(started.wait(timeout=5));batch.cancel.set()
            with self.assertRaises(TaskCancelled):future.result(timeout=10)
        self.assertEqual(seen,[0])
        self.assertEqual(batch.summary["interrupted"],3)

    def test_cpu_gpu_devices_overlap_and_release(self):
        """事件屏障证明 CPU/GPU 同时验证不同任务，完成后两个设备均释放。"""
        config=deepcopy(self.config); config["decode_verification"]={"cpu":True,"gpu":True}
        batch=Batch(config,JournalStub()); barrier=Barrier(2)
        def process_stub(command,log_path,config,progress=None,progress_path=None,**kwargs):
            """模拟完整解码；command/config 为生产参数，进度和音频帧写入指定测试文件。"""
            barrier.wait(timeout=10)
            progress_path.write_text("frame=25\nout_time_us=1000000\nprogress=end\n",encoding="utf-8")
            Path(command[-1]).write_text("#tb 0: 1/48000\n0, 0, 0, 48000, 4, abcd\n",encoding="utf-8")
        jobs=[]
        for index in range(2):
            work=self.root / str(index);work.mkdir();jobs.append((work,{}))
        with patch("local.batch.run_process",side_effect=process_stub), ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(batch.verify,work / "candidate.mp4",work,1,entry) for work,entry in jobs]
            results=[f.result(timeout=15) for f in futures]
        self.assertEqual({r["device"] for r in results},{"cpu","gpu"})
        self.assertEqual(batch.devices.qsize(),2)
        self.assertEqual(batch.devices.get()[1],"gpu")

    def test_queue_backpressure_and_oversize_progress(self):
        """有界调度在后处理阻塞时仍下载其他集，空队列允许超限单集推进。"""
        config=deepcopy(self.config)
        config["download"]["threads"]=2;config["max_in_flight"]=1
        config["download"]["queue_capacity_gb"]=1/1024**3
        processing, downloaded, release, prepared = Event(),Event(),Event(),Event()
        class Demo(Scheduler):
            """以同步事件替代网络和后处理阶段的调度测试批次。"""
            def __init__(self):
                """初始化测试配置及调度依赖；无参数。"""
                self.config,self.cancel,self.journal,self.interval=config,Event(),JournalStub(),config["runtime"]["cancel_poll_interval_seconds"]
            def dispatch_stop_reason(self):
                """测试不产生认证故障，返回 None。"""
                return None
            def notify(self,entry,stage,**values):
                """将测试 entry 阶段写入内存日志；无返回值。"""
                self.journal.update(entry,stage,**values)
            def _job_steps(self,task,entry):
                """解析 task 后进入下载，让出超限字节数再等事件；entry 为日志，返回成功状态。"""
                if task["index"] == 1:
                    prepared.set()
                yield "download",0
                if task["index"] == 0 and not prepared.wait(timeout=10):
                    raise TimeoutError("prepare gate")
                if task["index"]>0:
                    downloaded.set()
                yield "ready",2
                processing.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("test gate")
                return {"status":"completed"}
        batch=Demo()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(batch.run,[{"index":i,"video_id":str(i)} for i in range(4)])
            try:
                self.assertTrue(processing.wait(timeout=5))
                self.assertTrue(downloaded.wait(timeout=5))
            finally:
                release.set()
            self.assertEqual(future.result(timeout=15)["completed"],4)


if __name__ == "__main__":
    unittest.main()
