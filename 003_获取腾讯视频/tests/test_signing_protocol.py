"""用 Node 执行与 Frida 共用的纯结构校验，覆盖封装载荷时间及返回帧边界。"""

import json
from pathlib import Path
import shutil
import subprocess
import unittest

from research.settings import load_research


@unittest.skipUnless(shutil.which("node"), "JavaScript 结构测试需要 Node.js")
class SigningProtocolTests(unittest.TestCase):
    """不附加客户端，仅运行无副作用的 JavaScript 校验函数。"""

    def check_script(self, body: str):
        """以当前配置生成合成输入，执行 body 中的 Node 断言；失败时显示断言结果，无返回值。"""
        root = Path(__file__).resolve().parents[1]
        research, config = load_research(root / "research/configs/playback_research.yaml")
        settings = config["playback"]["playback"]
        data = {"version": settings["app_version"], "platform": int(settings["query"]["platform"]),
                "max_guid": settings["ckey"]["context"]["max_guid_bytes"],
                "width": research["native_observation"]["signing"]["paired_frame_width"]}
        source = root.joinpath("local/playback/templates/signing_protocol.js").read_text(encoding="utf-8")
        source += "\nconst assert = require('node:assert/strict');\nconst data=" + json.dumps(data) + ";\n" + body
        result = subprocess.run([shutil.which("node"), "-"], input=source, text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_control_business_timestamp_and_invalid_fields(self):
        """业务载荷允许旧时间戳；设备、版本、平台不符及时间字段非法必须拒绝。"""
        self.check_script("""
const guid = 'g'.repeat(Math.min(data.max_guid, 32));
const control = {cmd: 1, guid, apv: data.version, platform: data.platform, timestamp: 0};
const accepts = value => validControl(value, data.version, guid, data.platform, data.max_guid);
assert.equal(accepts(control), true);
for (const value of [{...control, guid: guid + 'x'}, {...control, apv: 'invalid'},
  {...control, platform: data.platform + 1}, {...control, timestamp: null},
  {...control, timestamp: -1}, {...control, timestamp: 'invalid'}, {...control, cmd: null}]) {
  assert.equal(accepts(value), false);
}
""")

    def test_direct_and_framed_outputs_are_distinct(self):
        """直接签名拒绝数值错误码；封装输出须具有完整的两个长度字段及载荷。"""
        self.check_script("""
assert.equal(validOutput('Abcd+=', false, data.width), true);
assert.equal(validOutput('-1', false, data.width), false);
const frame = '4'.padStart(data.width, '0') + '3'.padStart(data.width, '0') + 'abcd123';
assert.equal(validOutput(frame, true, data.width), true);
assert.equal(validOutput(frame.slice(0, -1), true, data.width), false);
assert.equal(validOutput(frame + 'x', true, data.width), false);
assert.equal(validOutput('x' + frame.slice(1), true, data.width), false);
""")

    def test_device_copies_must_all_agree(self):
        """多个设备副本必须同时可读且一致，空值和分歧均拒绝，不能只取首个值。"""
        self.check_script("""
assert.equal(consistentGuid(['same', 'same']), 'same');
for (const values of [[], [''], ['same', 'different'], ['same', '']]) {
  assert.throws(() => consistentGuid(values));
}
""")


if __name__ == "__main__":
    unittest.main()
