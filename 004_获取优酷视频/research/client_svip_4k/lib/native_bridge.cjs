/**
 * 在优酷自带 Electron 的独立 Node 进程中检查原生签名接口。
 * 输入：标准输入 JSON，含 operation、模块路径及签名参数或加密接口元数据。
 * 输出：带固定前缀的 JSON，仅由 Python 父进程消费；不保存或打印到研究报告。
 * 仅生成请求签名、随机挑战或解包 UPS 接口元数据；不解密视频、不导出授权或媒体密钥、不附加运行中客户端。
 */
'use strict';

const fs = require('fs');

try {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const sdk = require(input.module_path);
  const player = new sdk.KAliPlayer();
  if (input.operation === 'decode_metadata') {
    const decoded = player.getDecryptR1(input.encoded_metadata);
    process.stdout.write('\nYOUKU_RESEARCH_RESULT=' + JSON.stringify({
      metadata: decoded ? JSON.parse(decoded) : null,
    }) + '\n');
    process.exit(0);
  }
  const clientTs = String(Math.floor(Date.now() / 1000));
  const ckey = player.getUpsCKey(input.ccode, input.vid, input.client_ip, clientTs);
  const encryptR1 = player.getEncryptR1();
  process.stdout.write('\nYOUKU_RESEARCH_RESULT=' + JSON.stringify({
    runtime: {node: process.versions.node, electron: process.versions.electron, arch: process.arch},
    ckey, client_ts: clientTs, encryptR1,
  }) + '\n');
  process.exit(0);
} catch (error) {
  process.stdout.write('\nYOUKU_RESEARCH_RESULT=' + JSON.stringify({error: error.name, message: error.message}) + '\n');
  process.exit(1);
}
