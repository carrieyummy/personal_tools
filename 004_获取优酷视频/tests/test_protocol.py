"""隔离验证逐集签名绑定、共享认证恢复、媒体身份和敏感信息边界。"""

from copy import deepcopy
import json
import shutil
import subprocess
from pathlib import Path
from threading import Event, RLock
import time
import unittest
from unittest.mock import patch

from local.configuration import load_config
from local.youku.client import Client, AuthenticationError, PlaybackError, SourceChanged, captured_groups
from local.youku.source import resolve
from local.youku.native import native_challenge, run_native
from local.errors import ProtocolError, safe_message

ROOT=Path(__file__).resolve().parents[1]


class ProtocolTests(unittest.TestCase):
    """复用当前配置构造纯内存接口替身；不启动客户端、原生库或网络。"""

    def setUp(self):
        """加载用户配置并创建不执行构造副作用的客户端；无参数或返回值。"""
        self.config=load_config(ROOT / "configs/config.yaml")
        self.client=Client.__new__(Client)
        self.client.config,self.client.profile,self.client.cancel=self.config,self.config["playback"],Event()
        self.client.lock,self.client.stopped=RLock(),Event()
        self.client.loaded=time.monotonic()
        self.client.jar=object()
        self.client.generation,self.client.recoveries,self.client.account=0,0,None
        self.client.context={"ua":"fixture","utid":"secret-device"}

    def playback_snapshot(self):
        """返回不含历史网络请求的已初始化播放器替身；无参数，适配模式与平台码均读当前配置。"""
        return {"vid":"current", "response_vid":"current", "initialized":True,
            "adapter":deepcopy(self.client.profile["request"]["context"]["adapter"]),
            "groups":{
                "steal_params":{"ccode":self.client.profile["native"]["ccode"],"utid":"device","version":"sdk"},
                "biz_params":{"h265":1,"brand":"board-vendor","appArch":"x64","pcdnVer":"pcdn",
                    "btype":"board-model","chipset":"gpu","preferClarity":1,"app_ver":"sdk",
                    "play_ability":1,"close_ability":0,"drm_type":3,"key_index":"fixture","enc_ver":1,
                    "native_ver":"client"},
                "ad_params":{k:"platform" for k in self.client.profile["request"]["ad_fields"]}}}

    def test_capture_works_without_historical_requests(self):
        """历史请求已被性能缓冲淘汰时仍读取当前快照；模板保留真实能力与配置参数，不携带旧集材料。"""
        snapshot=self.playback_snapshot()
        self.client.pid=1
        with patch("local.youku.client.LiveInspector") as inspector, \
                patch("local.youku.client.evaluate_player",return_value=snapshot) as evaluate, \
                patch.object(Path,"read_text",return_value=json.dumps({"version":"fixture","buildnumber":"build"})):
            inspector.return_value.__enter__.return_value.evaluate.return_value=self.client.context
            self.client.capture()
        self.assertNotIn("performance",evaluate.call_args.args[2])
        self.assertEqual(self.client.initial_vid,snapshot["vid"])
        self.assertEqual(self.client.groups["biz_params"]["close_ability"],0)
        self.assertEqual(self.client.groups["steal_params"]["client_ip"],self.client.profile["native"]["client_ip"])
        for key,value in self.client.profile["request"]["context"]["biz_params"].items():
            if key not in snapshot["groups"]["biz_params"]:
                self.assertEqual(self.client.groups["biz_params"][key],value)
        self.assertNotIn("encryptR_client",self.client.groups["biz_params"])
        self.assertNotIn("ckey",self.client.groups["steal_params"])

    def test_capture_rejects_uninitialized_or_switching_player(self):
        """空播放器、缺少媒体响应以及切集中的身份错配均明确失败，不借用其他视频上下文。"""
        current=self.playback_snapshot()
        for snapshot in (None,{},dict(current,initialized=False),dict(current,response_vid="previous")):
            with self.subTest(snapshot=snapshot), self.assertRaisesRegex(PlaybackError,"初始化"):
                captured_groups(snapshot,self.client.profile)

    def test_capture_rejects_missing_device_and_changed_adapter(self):
        """缺少必需设备字段、原生平台错配或播放模式变化时拒绝猜测参数。"""
        current=self.playback_snapshot()
        missing=deepcopy(current);del missing["groups"]["biz_params"]["chipset"]
        wrong_platform=deepcopy(current);wrong_platform["groups"]["steal_params"]["ccode"]="wrong-platform"
        wrong_mode=deepcopy(current);wrong_mode["adapter"]["support_type"]="unsupported"
        for snapshot in (missing,wrong_platform,wrong_mode):
            with self.subTest(snapshot=snapshot), self.assertRaises(PlaybackError):
                captured_groups(snapshot,self.client.profile)

    def test_expired_token_retries_same_memory_jar(self):
        """token 拒绝后保留响应更新的 Jar，重新签名一次，成功不触发全局停止。"""
        rejected=self.client.profile["authentication"]["rejected_codes"][0]
        values=[{"ret":[rejected+"::expired"]},{"ret":["SUCCESS::ok"],"data":{}}]
        with patch("local.youku.client.request_mtop",side_effect=values) as request:
            self.client.request({"api":"fixture"},{})
        self.assertEqual(request.call_count,2)
        self.assertIs(request.call_args_list[0].args[1],request.call_args_list[1].args[1])
        self.assertEqual(self.client.recoveries,1)
        self.assertFalse(self.client.stopped.is_set())

    @unittest.skipUnless(shutil.which("node"), "桥接协议隔离检查需要 Node.js")
    def test_native_result_is_fully_written_before_exit(self):
        """模拟管道短写，要求退出前同步写完协议；异步 stdout 不允许参与结果输出。"""
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8').split('\nasync function main')[0];
const pieces=[];let exited=false;
const result={fixture:'x'.repeat(1000)};
const fake={writeSync(fd,buffer,offset,length){assert.equal(fd,1);const n=Math.min(7,length);pieces.push(Buffer.from(buffer.subarray(offset,offset+n)));return n;}};
const context={Buffer,require(name){assert.equal(name,'fs');return fake;},process:{
  stdout:{write(){throw Error('AsyncProtocolWrite');}},
  exit(code){assert.equal(code,0);assert.deepEqual(JSON.parse(Buffer.concat(pieces).toString().trim().split('YOUKU_RESEARCH_RESULT=')[1]),result);exited=true;}
},fixture:result};
vm.runInNewContext(source+'\nfinish(fixture,0);',context);
assert(exited);assert(pieces.length>1);
"""
        result=subprocess.run([shutil.which("node"),"-e",script,str(ROOT / "local/youku/native_bridge.cjs")],
                              capture_output=True,text=True,timeout=self.client.profile["native"]["timeout_seconds"])
        self.assertEqual(result.returncode,0,result.stderr)

    def test_native_invalid_protocol_and_failure_diagnostics_are_safe(self):
        """截断的协议行明确标识错误；挑战解码失败包含退出码，不泄漏原生消息或授权字段。"""
        from types import SimpleNamespace
        with patch("local.youku.native.subprocess.run",return_value=SimpleNamespace(returncode=0,
                stdout='YOUKU_RESEARCH_RESULT={"private":"truncated')):
            result=run_native(self.client.profile,{"operation":"sign"})
        self.assertEqual(result,{"error":"InvalidStructuredResult","returncode":0})
        with patch("local.youku.native.run_native",return_value={"returncode":0,"error":"NoStructuredResult","message":"private-value"}):
            with self.assertRaises(ProtocolError) as raised:
                native_challenge(self.client.profile,"private-challenge")
        message=safe_message(raised.exception)
        self.assertIn("NoStructuredResult",message)
        self.assertIn("returncode=0",message)
        self.assertNotIn("private",message)

    @unittest.skipUnless(shutil.which("node"), "桥接协议隔离检查需要 Node.js")
    def test_native_waits_for_complete_chunked_input(self):
        """参数分块晚到时必须等待 end 再加载原生模块，不能同步读取 fd 0 或提前解析半份 JSON。"""
        script=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),{EventEmitter}=require('events');
const source=fs.readFileSync(process.argv[1],'utf8'),stdin=new EventEmitter();
stdin.setEncoding=x=>assert.equal(x,'utf8');let loads=0,exits=0;const output=[];
const input=JSON.stringify({operation:'sign',module_path:'fixture-module',ccode:'fixture',vid:'fixture',client_ip:'fixture'});
const fakeFs={readFileSync(){throw Error('SyncInputForbidden');},writeSync(fd,b,o,n){output.push(Buffer.from(b.subarray(o,o+n)));return n;}};
class Player{getUpsCKey(){return 'fixture-signature';}getEncryptR1(){return 'fixture-challenge';}}
vm.runInNewContext(source,{Buffer,require(name){if(name==='fs')return fakeFs;assert.equal(name,'fixture-module');loads++;return {KAliPlayer:Player};},
  process:{stdin,versions:{node:'fixture',electron:'fixture'},arch:'fixture',exit(code){assert.equal(code,0);exits++;}}});
assert.equal(loads,0);assert.equal(exits,0);
stdin.emit('data',input.slice(0,7));assert.equal(loads,0);
stdin.emit('data',input.slice(7));assert.equal(loads,0);stdin.emit('end');
setImmediate(()=>{assert.equal(loads,1);assert.equal(exits,1);const payload=JSON.parse(Buffer.concat(output).toString().trim().split('YOUKU_RESEARCH_RESULT=')[1]);assert.equal(payload.ckey,'fixture-signature');});
"""
        result=subprocess.run([shutil.which("node"),"-e",script,str(ROOT / "local/youku/native_bridge.cjs")],
                              capture_output=True,text=True,timeout=self.client.profile["native"]["timeout_seconds"])
        self.assertEqual(result.returncode,0,result.stderr)

    def test_authentication_exhaustion_blocks_new_requests(self):
        """认证恢复耗尽后拒绝后续调用，不让每个下载线程重复尝试失效会话。"""
        rejected=self.client.profile["authentication"]["rejected_codes"][0]
        with patch("local.youku.client.request_mtop",return_value={"ret":[rejected]}) as request:
            with self.assertRaises(AuthenticationError):self.client.request({},{})
            count=request.call_count
            with self.assertRaises(AuthenticationError):self.client.request({},{})
            self.assertEqual(request.call_count,count)
        self.assertEqual(count,self.config["authentication"]["recovery_attempts"]+1)

    def test_target_request_never_reuses_current_video_context(self):
        """新签名与三个请求组绑定目标集，移除上一集相关字段，并拒绝响应视频错配。"""
        self.client.groups={"steal_params":{"vid":"old","utid":"private"},
            "biz_params":dict(vid="old",**{k:"old-value" for k in self.client.profile["request"]["remove_biz_fields"]}),
            "ad_params":{k:"fixture" for k in self.client.profile["request"]["ad_fields"]}}
        signature={"returncode":0,"ckey":"private-signature","client_ts":"123","encryptR1":"private-challenge",
                   "module_sha256":self.client.profile["restoration"]["challenge_module_sha256"]}
        response={"data":{"data":{"video":{"encodeid":"new"},"stream":[{}],"user":{"uid":"fixture-user"}}}}
        with patch("local.youku.client.native_signature",return_value=signature) as sign, patch.object(self.client,"request",return_value=response) as request:
            body,challenge=self.client.ups("new")
            self.assertIn("vid=new",sign.call_args.args[0]["sample"]["url"])
            payload=request.call_args.args[1]
            biz=json.loads(payload["biz_params"])
            self.assertEqual(biz["vid"],"new")
            self.assertFalse(set(biz)&set(self.client.profile["request"]["remove_biz_fields"]))
            self.assertEqual(challenge,signature["encryptR1"])
            response["data"]["data"]["video"]["encodeid"]="old"
            with self.assertRaises(PlaybackError):self.client.ups("new")

    def test_account_change_stops_batch(self):
        """同一批次出现不同账号标识时停止后续请求；不输出两次账号值。"""
        self.client.groups={"steal_params":{},"biz_params":{},"ad_params":{}}
        self.client.account="first-account"
        signature={"returncode":0,"ckey":"private","client_ts":"123","encryptR1":"private",
                   "module_sha256":self.client.profile["restoration"]["challenge_module_sha256"]}
        response={"data":{"data":{"video":{"encodeid":"new"},"stream":[{}],"user":{"uid":"second-account"}}}}
        with patch("local.youku.client.native_signature",return_value=signature), patch.object(self.client,"request",return_value=response):
            with self.assertRaises(AuthenticationError):self.client.ups("new")
        self.assertTrue(self.client.stopped.is_set())

    def test_signed_url_rotation_preserves_identity_but_key_change_does_not(self):
        """URL 查询签名变化可续传；媒体密钥改变必须改变不透明来源身份并拒绝旧源。"""
        name=next(iter(self.client.profile["supported_streams"]))
        task={"video_id":"fixture","series_id":"series","index":1,"quality":1,"duration":1}
        stream={"stream_type":name,"media_type":"standard","audio_lang":self.client.profile["audio_language"],
                "width":3840,"height":2160,"size":188,"drm_type":"copyrightDRM","milliseconds_video":1000,
                "m3u8_url":"https://cdn.invalid/list?token=private","encryptR_server":"private-server",
                "stream_ext":{"copyright_key":"private-license"}}
        body={"video":{"seconds":1},"show":{"encodeid":"series"},"dvd":{"show_videotype":"正片","show_video_ep":1},"stream":[stream]}
        self.client.config=deepcopy(self.config);self.client.config["audio"]["quality"]=1
        template="#EXTM3U\n#EXTINF:1,\n#EXT-X-PRIVINF:FILESIZE=188\nhttps://cdn.invalid/media.ts?token={}\n#EXT-X-ENDLIST\n"
        raw=[(template.format(token).encode(),"https://cdn.invalid/list",{}) for token in ("first","second","third","preview")]
        with patch.object(self.client,"ups",return_value=(body,"private-client")), patch("local.youku.source.fetch_media",side_effect=raw), \
                patch("local.youku.source.session_key",side_effect=[b"a"*16,b"a"*16,b"b"*16]):
            first=resolve(self.client,task)
            second=resolve(self.client,task,expected=first)
            self.assertEqual(first.identity,second.identity)
            with self.assertRaises(SourceChanged):resolve(self.client,task,expected=first)
            with self.assertRaisesRegex(PlaybackError,"时长"):
                resolve(self.client,{**task,"duration":1+self.config["duration_tolerance_seconds"]*2})
        self.assertNotIn("private",json.dumps(first.description))
        self.assertNotIn("private",repr(first))


if __name__=="__main__":
    unittest.main()
