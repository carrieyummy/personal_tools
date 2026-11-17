// 真实签名调用的无副作用结构校验；供 Frida 观察器及隔离测试共用。
function consistentGuid(values) {
  // values 为所有已验收设备位置的读取值；全部非空且一致时返回 GUID，否则抛错。
  if (!values.length || values.some(value => typeof value !== 'string' || !value) || new Set(values).size !== 1) {
    throw new Error('SDK 设备上下文副本不一致');
  }
  return values[0];
}

function validControl(control, version, guid, platform, maxGuidBytes) {
  // control 为已解析载荷，其余参数来自外层 SDK 调用及配置；返回业务载荷是否符合已实测的封装协议。
  if (!control || Array.isArray(control) || typeof control !== 'object') return false;
  return Object.prototype.hasOwnProperty.call(control, 'cmd') &&
    (typeof control.cmd === 'string' || typeof control.cmd === 'number' && Number.isInteger(control.cmd)) &&
    control.guid === guid && String(control.apv) === version && Number(control.platform) === platform &&
    Number.isInteger(Number(control.platform)) && Number(control.platform) >= 0 && Number(control.platform) <= 65535 &&
    (typeof control.timestamp === 'number' || typeof control.timestamp === 'string' && /^\d+$/.test(control.timestamp)) &&
    Number.isFinite(Number(control.timestamp)) && Number(control.timestamp) >= 0 &&
    /^[A-Za-z0-9_.-]+$/.test(String(control.cmd)) && String(control.cmd).length <= 64 &&
    typeof control.guid === 'string' && control.guid.length > 0 && control.guid.length <= maxGuidBytes &&
    /^\d+(?:\.\d+)*$/.test(String(control.apv)) &&
    Number.isInteger(Number(control.platform)) && Number(control.platform) >= 0 && Number(control.platform) <= 65535 &&
    Number.isFinite(Number(control.timestamp)) && Number(control.timestamp) >= 0;
}

function validOutput(output, framed, frameWidth) {
  // output 为内存返回文本；framed 指定封装模式，frameWidth 是配置中的十六进制长度宽度；返回结构有效性。
  if (!framed) return /^[A-Za-z0-9+/=_-]+$/.test(output) && !/^-?\d+$/.test(output);
  if (!Number.isInteger(frameWidth) || frameWidth <= 0) return false;
  const header = output.slice(0, frameWidth * 2);
  const first = parseInt(header.slice(0, frameWidth), 16);
  const second = parseInt(header.slice(frameWidth), 16);
  return header.length === frameWidth * 2 && /^[0-9a-fA-F]+$/.test(header) && first > 0 && second > 0 &&
    frameWidth * 2 + first + second === output.length;
}
