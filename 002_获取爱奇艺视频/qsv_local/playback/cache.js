// 只读访问当前客户端已打开的播放缓存索引；不读取密钥，不调用下载或播放控制函数。
rpc.exports.describe = function (name) {
  // name 为配置中的模块名；返回模块路径供宿主先核验摘要。
  if (Process.pointerSize !== 8) throw Error('requires_x64');
  return Process.getModuleByName(name).path;
};

rpc.exports.inspect = function (settings, tvids) {
  // settings 为已核验布局和边界，tvids 为公开链接解析出的正数字编号；返回白名单查询结果。
  const module = Process.getModuleByName(settings.module);
  const vtable = module.base.add(settings.manager_vtable_rva);
  const pattern = vtable.toString().slice(2).padStart(16, '0').match(/../g).reverse().join(' ');
  if (!Array.isArray(tvids) || tvids.length === 0 || tvids.some(value => !/^[1-9][0-9]*$/.test(value))) {
    throw Error('invalid_tvids');
  }
  const execute = new NativeFunction(module.base.add(settings.sqlite_exec_rva), 'int',
    ['pointer', 'pointer', 'pointer', 'pointer', 'pointer']);

  function select(database, sql) {
    // database 是客户端已打开的缓存句柄，sql 仅由下方固定 SELECT 构造；同步返回有界行列表。
    const rows = [];
    let invalid = false;
    const callback = new NativeCallback(function (context, count, values, names) {
      // context 为未使用上下文；count 为列数，values/names 为 SQLite 当前行字符串数组；返回 0 继续，1 停止。
      if (count < 0 || count > 32 || rows.length >= settings.max_rows) { invalid = true; return 1; }
      const row = {};
      for (let i = 0; i < count; i++) {
        const name = names.add(i * Process.pointerSize).readPointer().readUtf8String();
        const value = values.add(i * Process.pointerSize).readPointer();
        if (!/^[A-Z_]+$/i.test(name)) { invalid = true; return 1; }
        if (value.isNull()) row[name] = null;
        else {
          // SQLite 回调字符串以 NUL 结束；逐字节限长，避免把相邻内存作为字段读取。
          let size = 0;
          while (size <= settings.max_text_bytes && value.add(size).readU8() !== 0) size++;
          if (size > settings.max_text_bytes) { invalid = true; return 1; }
          row[name] = value.readUtf8String(size);
        }
      }
      rows.push(row);
      return 0;
    }, 'int', ['pointer', 'int', 'pointer', 'pointer']);
    const status = execute(database, Memory.allocUtf8String(sql), callback, ptr(0), ptr(0));
    if (invalid || status !== 0) throw Error('bounded_cache_select_failed');
    return rows;
  }

  let scanned = 0, limited = false, changedRegions = 0;
  const databases = [], seen = new Set();
  for (const region of Process.enumerateRanges({protection: 'rw-', coalesce: true})) {
    if (region.file || region.size > settings.scan_max_region_bytes) continue;
    if (scanned + region.size > settings.scan_max_total_bytes) { limited = true; break; }
    scanned += region.size;
    let matches;
    try { matches = Memory.scanSync(region.base, region.size, pattern); }
    catch (_) { changedRegions++; continue; }
    for (const hit of matches) {
      let database;
      try {
        if (!hit.address.readPointer().equals(vtable)) continue;
        database = hit.address.add(settings.database_pointer_offset).readPointer();
        if (database.isNull() || seen.has(database.toString())) continue;
        if (Process.getModuleByAddress(database.readPointer()).name !== module.name) continue;
      } catch (_) { continue; }
      seen.add(database.toString());
      const tables = select(database, "SELECT name FROM sqlite_master WHERE type='table'");
      if (!['T_FILE_INFO', 'T_BLOCK_INFO', 'T_PGF_PATH'].every(name => tables.some(row => row.name === name))) continue;
      // 编号仅含正数字，LIMIT 来自已校验正整数；不接受外部 SQL 或文件写操作。
      const ids = tvids.map(value => "'" + value + "'").join(',');
      const files = select(database, 'SELECT f.TVID,f.VID,f.FILE_TYPE,f.SEG_INDEX,f.SEG_TOTAL_COUNT,' +
        'f.FILE_SIZE,f.BLOCK_SIZE AS FILE_BLOCK_SIZE,COALESCE(b.BLOCKS,0) AS CACHED_BLOCKS,' +
        'COALESCE(b.BYTES,0) AS CACHED_BYTES,b.FIRST_BLOCK,b.LAST_BLOCK FROM T_FILE_INFO f LEFT JOIN ' +
        '(SELECT FID,COUNT(*) AS BLOCKS,SUM(BLOCK_SIZE) AS BYTES,MIN(BLOCK_NO) AS FIRST_BLOCK,' +
        'MAX(BLOCK_NO) AS LAST_BLOCK FROM T_BLOCK_INFO GROUP BY FID) b ON f.FID=b.FID ' +
        'WHERE f.TVID IN (' + ids + ') ORDER BY f.TVID,f.VID,f.SEG_INDEX LIMIT ' + settings.max_rows);
      const totals = select(database, 'SELECT COUNT(*) AS BLOCKS,SUM(BLOCK_SIZE) AS BYTES FROM T_BLOCK_INFO');
      const paths = select(database, 'SELECT PGF_ID,PGF_PATH,BLOCK_COUNT FROM T_PGF_PATH ORDER BY PGF_ID');
      databases.push({files, files_limited: files.length >= settings.max_rows, totals, paths});
    }
  }
  return {databases, scanned_bytes: scanned, scan_limited: limited, changed_regions: changedRegions};
};
