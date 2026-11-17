/**
 * 在优酷自带 Electron 的独立 Node 进程中检查原生签名接口。
 * 输入：标准输入 JSON，含 operation、模块路径及签名参数或加密接口元数据。
 * 输出：带固定前缀的 JSON，仅由 Python 父进程消费；不保存或打印到研究报告。
 * 生成请求签名、解包 UPS 元数据或在内存还原当前挑战；不写授权材料，不附加运行中客户端。
 */
'use strict';

const fs = require('fs');
let stage = 'read_input';

/**
 * 同步写完父进程协议后退出，避免 process.exit 丢弃 Windows 管道中尚未完成的异步 stdout 写入。
 * @param {object} result 仅供父进程在内存消费的结构化结果。
 * @param {number} exitCode 子进程退出码，0 表示成功。
 * @returns {never} 完整写入后结束隔离进程。
 */
function finish(result, exitCode) {
  stage = 'write_result';
  const output = Buffer.from('\nYOUKU_RESEARCH_RESULT=' + JSON.stringify(result) + '\n', 'utf8');
  let offset = 0;
  while (offset < output.length) {
    const written = fs.writeSync(1, output, offset, output.length - offset);
    if (written <= 0) throw new Error('ProtocolWriteFailed');
    offset += written;
  }
  process.exit(exitCode);
}

/**
 * 等待父进程完整发送输入并关闭管道；Windows Electron 的同步 fd 0 读取可能提前抛 EOF。
 * @returns {Promise<string>} 合并后的 UTF-8 输入；管道出错时拒绝，不生成签名。
 */
function readInput() {
  return new Promise((resolve, reject) => {
    let raw = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', chunk => { raw += chunk; });
    process.stdin.once('end', () => resolve(raw));
    process.stdin.once('error', reject);
  });
}

/**
 * 等待完整输入后执行单次原生操作，错误仅报告步骤和系统码。
 * @returns {Promise<void>} 完成时由 finish 写完结果并退出进程。
 */
async function main() {
  try {
    const raw = await readInput();
    stage = 'parse_input';
    const input = JSON.parse(raw);
    stage = 'load_module';
    const sdk = require(input.module_path);
    stage = 'create_player';
    const player = new sdk.KAliPlayer();
    if (input.operation === 'decode_challenge') {
      stage = 'decode_challenge';
      finish({
        challenge: player.getDecryptR1(input.encoded_challenge),
      }, 0);
    }
    if (input.operation === 'decode_metadata') {
      stage = 'decode_metadata';
      const decoded = player.getDecryptR1(input.encoded_metadata);
      finish({
        metadata: decoded ? JSON.parse(decoded) : null,
      }, 0);
    }
    const clientTs = String(Math.floor(Date.now() / 1000));
    stage = 'sign';
    const ckey = player.getUpsCKey(input.ccode, input.vid, input.client_ip, clientTs);
    stage = 'create_challenge';
    const encryptR1 = player.getEncryptR1();
    finish({
      runtime: {node: process.versions.node, electron: process.versions.electron, arch: process.arch},
      ckey, client_ts: clientTs, encryptR1,
    }, 0);
  } catch (error) {
    finish({error: error.name, stage,
      code: typeof error.code === 'string' && /^[A-Z_]{1,32}$/.test(error.code) ? error.code : null,
      errno: Number.isInteger(error.errno) ? error.errno : null}, 1);
  }
}

main();
