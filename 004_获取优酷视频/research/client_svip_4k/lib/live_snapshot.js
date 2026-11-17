/**
 * 读取当前样本的实际播放上下文，不导航、不暂停或启动播放。
 * @param {Object} options 包含 api 名称、目标 vid 和公开流字段数组 fields。
 * @returns {Object} 公开流摘要及仅供 Python 内存使用的请求地址；地址不得写入报告。
 */
(options) => {
  const context = window.videoPlayer && window.videoPlayer.context;
  if (!context || context.config.vid !== options.vid) throw Error('SampleMismatch');
  const resource = context.originResource;
  if (!resource || !resource.video || resource.video.encodeid !== options.vid) throw Error('OriginMismatch');
  // 播放器可能预取下一集；不能把最后一次 UPS 请求直接当作当前正片。
  const entries = performance.getEntriesByType('resource').filter(entry => {
    try {
      const url = new URL(entry.name);
      if (!url.pathname.includes('/' + options.api + '/')) return false;
      const payload = JSON.parse(url.searchParams.get('data'));
      const steal = JSON.parse(payload.steal_params);
      return JSON.parse(payload.biz_params).vid === options.vid &&
        (!Object.hasOwnProperty.call(steal, 'vid') || steal.vid === options.vid);
    } catch (_) {
      return false;
    }
  });
  if (!entries.length) throw Error('RequestNotAvailable');
  return {
    request_url: entries[entries.length - 1].name,
    player_version: context.version,
    video: {encodeid: resource.video.encodeid, title: resource.video.title, seconds: resource.video.seconds},
    client_capabilities: Object.fromEntries(Object.entries(resource.clientAbility || {}).filter(([, v]) => typeof v === 'number')),
    vip: Object.fromEntries(Object.entries(resource.vip || {}).filter(([, v]) => typeof v === 'boolean')),
    streams: (resource.stream || []).map(stream => ({
      ...Object.fromEntries(options.fields.filter(key => key in stream && ['string','number','boolean'].includes(typeof stream[key])).map(key => [key, stream[key]])),
      json_path: '$.stream',
      has_m3u8_url: !!stream.m3u8_url,
      has_mpd_url: !!stream.mpd_url,
      has_copyright_key: !!(stream.stream_ext && stream.stream_ext.copyright_key),
    })),
  };
}
