// 只观察目标导出的真实调用；settings/sections 由父进程的 PE 检查提供，票据及签名不发送。
const image = Process.getModuleByName(settings.module);
let reported = false;
let observedCount = 0;

function textAt(pointer, limit) {
  // pointer 为客户端字符串地址，limit 为最大字节数；返回有界 UTF-8 字符串，非法地址抛错。
  if (pointer.isNull()) throw new Error('null string');
  for (let size = 0; size < limit; size++) {
    if (pointer.add(size).readU8() === 0) return pointer.readUtf8String(size);
  }
  throw new Error('unterminated string');
}

function guidSlots(pointer, guid) {
  // pointer/guid 来自真实调用；按长度在 PE 数据区找 MSVC 字符串并核对完整内容，返回相符槽 RVA。
  // 调用者可能复制字符串，不能要求传入 h 的指针与静态对象的堆地址完全相同。
  const raw = Memory.alloc(8);
  raw.writeU64(guid.length);
  const pattern = Array.from(new Uint8Array(raw.readByteArray(8)))
    .map(value => value.toString(16).padStart(2, '0')).join(' ');
  const found = [];
  const direct = [];
  for (const section of sections) {
    const contextImage = Process.getModuleByName(section.module);
    const base = contextImage.base.add(section.rva);
    for (const match of Memory.scanSync(base, section.size, pattern)) {
      try {
        const slot = match.address.sub(16);
        if (slot.compare(base) < 0 || slot.add(32).compare(base.add(section.size)) > 0) continue;
        const size = slot.add(16).readU64().toNumber();
        const capacity = slot.add(24).readU64().toNumber();
        if (size === guid.length && capacity >= size && capacity >= 16 &&
            textAt(slot.readPointer(), size + 1) === guid) {
          const record = {module: section.module, rva: slot.sub(contextImage.base).toUInt32()};
          found.push(record);
          if (slot.readPointer().equals(pointer)) direct.push(record);
        }
      } catch (_) { /* 仅接受完整可读的 MSVC 字符串结构。 */ }
    }
  }
  return Array.from(new Map((direct.length ? direct : found).map(value => [value.module + ':' + value.rva, value])).values());
}

const importedFree = image.enumerateImports().filter(value => value.name === 'free');
if (importedFree.length !== 1 ||
    !image.base.add(settings.free_import_rva).readPointer().equals(importedFree[0].address)) {
  throw new Error('CRT free import mismatch');
}
Interceptor.attach(image.getExportByName(settings.export), {
  onEnter(args) {
    // args 为客户端真实参数；核对现有 11 参数 ABI 的直接签名或封装调用，不更改原始参数。
    this.accepted = false;
    this.enabled = false;
    this.reason = '输入字符串或参数地址结构不符';
    if (reported) return;
    if (++observedCount > observation.max_events) {
      if (observedCount === observation.max_events + 1) send({kind: 'limit'});
      return;
    }
    this.enabled = true;
    try {
      this.shape = [0, 1, 2, 3, 8, 9].map(index => {
        try {
          const value = textAt(args[index], settings.max_output_bytes);
          let fields = [];
          try { const parsed = JSON.parse(value); if (parsed && !Array.isArray(parsed) && typeof parsed === 'object') fields = Object.keys(parsed).sort(); } catch (_) {}
          return {index: index, length: value.length, json_fields: fields};
        } catch (_) { return {index: index, unreadable: true}; }
      });
      const vid = textAt(args[0], 64);
      this.framed = vid === '';
      if (!this.framed && !/^[A-Za-z0-9]+$/.test(vid)) return;
      const version = textAt(args[1], 64);
      const guid = textAt(args[2], settings.context.max_guid_bytes + 1);
      const source = textAt(args[3], 64);
      const stamp = args[5].toInt32();
      const count = args[7].toInt32();
      this.reason = '版本、设备、时间或扩展参数数量不符';
      if (!/^\d+(?:\.\d+)*$/.test(version) || !source || !guid ||
          guid.length > settings.context.max_guid_bytes ||
          Math.abs(Date.now() / 1000 - stamp) > 3600 || count !== settings.extra_values.length) return;
      for (let index = 0; index < count; index++) {
        this.reason = '扩展整数数组与配置不符';
        if (args[6].add(index * 4).readS32() !== settings.extra_values[index]) return;
      }
      const extraText = textAt(args[8], settings.max_output_bytes);
      const lastText = textAt(args[9], settings.max_output_bytes);
      this.reason = '扩展 JSON 或末尾字符串结构不符';
      if (this.framed) {
        // 新版通过最后一个 JSON 参数传入视频及设备；只观察其 ABI 和双长度帧，不复用控制调用输出。
        const control = JSON.parse(lastText);
        if (extraText !== '' || !control || Array.isArray(control) || typeof control !== 'object' ||
            !Object.prototype.hasOwnProperty.call(control, 'cmd')) return;
        this.shape.push({index: 9, checks: {guid_matches: control.guid === guid, version_matches: String(control.apv) === version,
          platform_matches: Number(control.platform) === args[4].toInt32(), timestamp_matches: Math.abs(Number(control.timestamp) - stamp) <= 1}});
        // 实测 GUID、版本和平台与外层参数一致，业务时间戳可以早于本次 SDK 调用时间。
        // 此处只证明真实 ABI；后续另行生成清单视频 cKey 并检查官方授权，不把控制输出当作 cKey。
        if (!validControl(control, version, guid, args[4].toInt32(), settings.context.max_guid_bytes)) return;
      } else {
        const extra = JSON.parse(extraText);
        if (!extra || Array.isArray(extra) || typeof extra !== 'object' || lastText !== '') return;
      }
      this.length = args[10];
      this.guidPointer = args[2];
      this.guid = guid;
      this.accepted = true;
    } catch (_) { /* 未识别的调用仅跳过，不更改客户端参数。 */ }
  },
  onLeave(result) {
    // result 为客户端原始返回值；只发送长度和唯一设备槽位置，客户端自行释放原始内存。
    if (reported || !this.enabled) return;
    if (!this.accepted) {
      send({kind: 'observed', reason: this.reason, shape: this.shape});
      return;
    }
    try {
      const length = this.length.readS32();
      if (length <= 0 || length > settings.max_output_bytes || result.isNull()) {
        send({kind: 'observed', reason: '签名输出指针或长度不符'});
        return;
      }
      const output = result.readUtf8String(length);
      const valid = validOutput(output, this.framed, observation.paired_frame_width);
      if (!valid) {
        send({kind: 'observed', reason: '签名输出字符结构不符'});
        return;
      }
      const slots = guidSlots(this.guidPointer, this.guid);
      if (slots.length === 0) {
        send({kind: 'diagnostic', reason: '设备字符串数据槽不唯一', matches: slots.length});
        return;
      }
      reported = true;
      send({kind: 'verified', slots: slots, signature_length: length,
            method: this.framed ? 'observed_framed_call_and_consistent_msvc_strings' : 'observed_playback_call_and_consistent_msvc_strings'});
    } catch (_) {
      send({kind: 'observed', reason: '设备字符串扫描或返回帧读取异常'});
    }
  }
});
send({kind: 'ready'});
