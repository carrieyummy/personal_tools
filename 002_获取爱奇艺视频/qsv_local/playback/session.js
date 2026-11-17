/* 复用已初始化的播放 SDK 生成请求并观察响应；不切换播放界面，不调用离线下载。 */
'use strict';

let adapter = null, programModule = null, manager = null, movieInput = null, retained = [], ownsProgram = false, target = null;

function programStatus() {
    // 查询本版本播放预读列表；无参数，返回 JSON 状态，不含地址、账号或授权票据。
    if (manager === null) return [];
    const buffer = Memory.alloc(adapter.max_body_bytes);
    const get = new NativeFunction(programModule.base.add(adapter.programs.status_rva), 'int', ['pointer', 'pointer', 'int']);
    const length = get(manager, buffer, adapter.max_body_bytes);
    if (length < 0 || length >= adapter.max_body_bytes) throw new Error('播放预读状态超过上限');
    const text = buffer.readUtf8String(length);
    const result = JSON.parse(text);
    if (!Array.isArray(result)) throw new Error('播放预读状态格式变化');
    return result;
}

rpc.exports = {
    // settings 是已核验的 YAML 适配参数；返回加载模块路径供宿主再次核对。
    install(settings) {
        const qtp = Process.getModuleByName(settings.qtp.module);
        const player = Process.getModuleByName(settings.player.module);
        adapter = settings;
        programModule = player;
        for (const key of ['push_front_rva', 'delete_rva', 'status_rva'])
            if (!Number.isInteger(settings.programs[key]) || settings.programs[key] < 0 || settings.programs[key] >= player.size)
                throw new Error('播放预读接口偏移超出模块');
        const records = new Map();
        let serial = 0;
        function readText(pointer) {
            // pointer 指向原生零结尾 UTF-8 字符串；在配置上限及当前内存区内查找终止符，返回精确文本。
            const range = Process.findRangeByAddress(pointer);
            if (!range || !range.protection.includes('r')) throw new Error('文本内存不可读');
            const length = Math.min(settings.max_text_bytes, Number(range.base.add(range.size).sub(pointer)));
            const bytes = new Uint8Array(pointer.readByteArray(length));
            const end = bytes.indexOf(0);
            if (end < 0) throw new Error('文本缺少有界终止符');
            return end === 0 ? '' : pointer.readUtf8String(end);
        }
        function target(url) {
            // url 是原生请求地址；只接受配置中的精确域名，返回是否观察。
            const match = /^https?:\/\/([^/:?#]+)(?::\d+)?(?:\/|$)/i.exec(url);
            return match !== null && settings.hosts.includes(match[1].toLowerCase());
        }
        function byUser(pointer) {
            // pointer 是 SDK 回调上下文；返回本批次最后一个匹配的有效请求。
            return Array.from(records.values()).reverse().find(r => r.target && r.user === pointer.toString());
        }
        Interceptor.attach(qtp.getExportByName('qtp_set_opt'), {onEnter(args) {
            const key = args[0].toString();
            if (!records.has(key)) {
                if (records.size >= settings.max_requests) records.delete(records.keys().next().value);
                records.set(key, {id: ++serial, headers: [], bytes: 0});
            }
            const record = records.get(key), option = args[1].toInt32();
            try {
                if (option === settings.qtp.url_option) {
                    record.url = readText(args[2]);
                    record.target = target(record.url);
                } else if (option === settings.qtp.user_option) record.user = args[2].toString();
                else if (option === settings.qtp.header_option && !args[2].isNull() && record.headers.length < settings.max_headers)
                    record.headers.push(readText(args[2]));
            } catch (_) { /* 未完成设置的对象不进入响应处理。 */ }
        }});
        Interceptor.attach(qtp.getExportByName('qtp_async_get'), {onEnter(args) {
            const record = records.get(args[0].toString());
            if (record && record.target) send({event: 'request', id: record.id, url: record.url, headers: record.headers});
        }});
        Interceptor.attach(player.base.add(settings.player.body_rva), {onEnter(args) {
            const record = byUser(args[2]);
            if (!record) return;
            const size = args[1].toInt32();
            if (size <= 0 || record.bytes + size > settings.max_body_bytes) return;
            record.bytes += size;
            send({event: 'body', id: record.id}, args[0].readByteArray(size));
        }});
        Interceptor.attach(player.base.add(settings.player.complete_rva), {
            onEnter(args) { this.record = byUser(args[3]); },
            onLeave() { if (this.record) send({event: 'complete', id: this.record.id}); }
        });
        Interceptor.attach(qtp.getExportByName('qtp_destroy_req'), {onEnter(args) { records.delete(args[0].toString()); }});
        return {qtp: qtp.path, player: player.path};
    },
    preload(video) {
        // video 是经过官方资料校验的目标节目；返回是否已提交本次临时播放预读，界面保持原视频。
        if (manager !== null) throw new Error('本观察实例已有播放请求');
        const spec = adapter.programs, fields = spec.fields;
        for (const key of ['tvid', 'vid'])
            if (typeof video[key] !== 'string' || video[key].length >= spec.capacities[key] || !/^[a-zA-Z0-9]+$/.test(video[key]))
                throw new Error('节目编号超出原生输入约束');
        const create = new NativeFunction(programModule.getExportByName(spec.factory), 'pointer', []);
        manager = create();
        if (manager.isNull()) { manager = null; throw new Error('播放预读管理器创建失败'); }
        target = video.tvid;
        if (programStatus().some(row => String(row.tvid) === target))
            throw new Error('目标节目已有其他播放预读，本次不接管或删除原任务');
        movieInput = Memory.alloc(spec.input_size);
        movieInput.writeByteArray(new Uint8Array(spec.input_size));
        for (const key of ['bitstream', 'hdr_type', 'frame_rate', 'movie_type', 'audio_language', 'audio_type', 'audio_channel_type'])
            movieInput.add(fields[key]).writeS32(adapter.request[key]);
        for (const key of ['bitstream_extend_info', 'audio_extend_info']) {
            if (!Object.keys(adapter.request[key]).length) continue;
            const text = JSON.stringify(adapter.request[key]);
            const byteLength = Array.from(text).reduce((n, c) => {
                const value = c.codePointAt(0); return n + (value < 0x80 ? 1 : value < 0x800 ? 2 : value < 0x10000 ? 3 : 4);
            }, 0);
            if (byteLength >= spec.capacities[key]) throw new Error('媒体选择扩展参数超过原生容量');
            movieInput.add(fields[key]).writeUtf8String(text);
        }
        for (const key of ['tvid', 'vid']) movieInput.add(fields[key]).writeUtf8String(video[key]);
        movieInput.add(fields.charge).writeU8(video.ischarge ? 1 : 0);
        const params = Memory.allocUtf8String(adapter.request.vrs_param), extra = Memory.allocUtf8String(JSON.stringify(adapter.request.extra));
        retained = [movieInput, params, extra];
        movieInput.add(fields.vrs_param).writePointer(params);
        movieInput.add(fields.extra).writePointer(extra);
        const push = new NativeFunction(programModule.base.add(spec.push_front_rva), 'void', ['pointer', 'pointer']);
        ownsProgram = true;
        push(manager, movieInput);
        return true;
    },
    cancel() {
        // 仅撤销本实例创建的临时播放预读；无参数，返回是否发出了撤销命令。
        if (!ownsProgram || manager === null) return false;
        new NativeFunction(programModule.base.add(adapter.programs.delete_rva), 'void', ['pointer', 'pointer'])(manager, movieInput);
        ownsProgram = false;
        return true;
    },
    pending() {
        // 只读检查本次目标仍有多少播放预读条目；无参数，返回非负数量。
        return programStatus().filter(row => String(row.tvid) === target).length;
    },
    close() {
        // 释放本实例持有的管理器及输入缓冲；不销毁客户端全局 SDK 或其他任务，无返回值。
        if (manager !== null) new NativeFunction(programModule.getExportByName(adapter.programs.destroy), 'void', ['pointer'])(manager);
        manager = null; movieInput = null; retained = []; target = null;
    }
};
