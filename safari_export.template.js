/* Run in the console of an authenticated https://www.bilibili.com page in Safari.
 * Does not inspect browser cookies; exports only video and stream metadata.
 */
(async () => {
  "use strict";
  const bvids = __BVIDS_JSON__;
  const allPages = __ALL_PAGES_JSON__;
  if (location.protocol !== "https:" || location.hostname !== "www.bilibili.com") {
    throw new Error("请在已登录的 https://www.bilibili.com 视频页面中运行此脚本。");
  }
  const manifest = {
    schema: "bili-dolby-manifest-v1",
    created_at: new Date().toISOString(),
    user_agent: navigator.userAgent,
    entries: [],
    errors: []
  };
  const delay = () => new Promise(resolve => setTimeout(resolve, 500));
  const streamFields = ["id", "baseUrl", "base_url", "backupUrl", "backup_url", "bandwidth",
    "mimeType", "mime_type", "codecs", "width", "height", "frameRate", "frame_rate", "codecid",
    "size", "SegmentBase", "segment_base", "is_drm", "drm_type", "drm_tech_type", "widevine_pssh", "bilidrm_uri"];
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const scalar = value => value === null || ["string", "boolean"].includes(typeof value) ||
    (typeof value === "number" && Number.isFinite(value));
  const pick = (source, fields) => Object.fromEntries(fields.filter(k => object(source) &&
    k in source && scalar(source[k])).map(k => [k, source[k]]));
  const streams = value => Array.isArray(value) ? value.filter(object) : [];
  const integer = (value, fallback = 0) => ["string", "number"].includes(typeof value) && Number.isInteger(Number(value)) &&
    Number.isSafeInteger(Number(value)) ? Number(value) : fallback;
  const flag = value => typeof value === "string" ? !["", "0", "false", "none", "null"].includes(value.trim().toLowerCase()) : Boolean(value);
  const protectedStream = s => ["is_drm", "drm_type", "drm_tech_type", "widevine_pssh", "bilidrm_uri"].some(k => flag(s[k]));
  function usableUrl(value, now = Date.now() / 1000) {
    if (typeof value !== "string" || /[\x00-\x20\x7f]/.test(value)) return false;
    try {
      const url = new URL(value);
      return ["https:", "http:"].includes(url.protocol) && Boolean(url.hostname) &&
        !url.username && !url.password && url.searchParams.getAll("deadline").every(deadline =>
          /^\d+$/.test(deadline) && Number(deadline) > now);
    } catch { return false; }
  }
  const urls = s => [s.baseUrl, s.base_url, ...[s.backupUrl, s.backup_url].flatMap(u => Array.isArray(u) ? u : [])];
  const hasUrl = s => urls(s).some(u => usableUrl(u));
  const ec3 = s => ["ec-3", "eac3", "e-ac-3"].includes(String(s.codecs || "").trim().toLowerCase());
  function cleanStream(stream) {
    const result = pick(stream, streamFields);
    for (const key of ["baseUrl", "base_url"]) {
      if (key in result && !usableUrl(result[key], 0)) delete result[key];
    }
    for (const key of ["backupUrl", "backup_url"]) {
      if (Array.isArray(stream[key])) result[key] = stream[key].filter(url => usableUrl(url, 0));
    }
    for (const key of ["SegmentBase", "segment_base"]) {
      if (object(stream[key])) result[key] = pick(stream[key], ["Initialization", "indexRange", "initialization", "index_range"]);
    }
    for (const key of ["widevine_pssh", "bilidrm_uri", "is_drm"]) {
      if (key in stream) result[key] = flag(stream[key]);
    }
    if (protectedStream(stream)) result.is_drm = true;
    for (const key of ["drm_type", "drm_tech_type"]) {
      if (key in result && !flag(result[key])) result[key] = 0;
    }
    return result;
  }
  function sanitize(data) {
    const result = pick(data, ["quality", "format", "timelength", "fnval", "fnver", "video_codecid", "drm_type", "drm_tech_type"]);
    if (Array.isArray(data.accept_quality)) result.accept_quality = data.accept_quality.filter(q =>
      ["number", "string"].includes(typeof q)).map(q => integer(q));
    if (Array.isArray(data.accept_description)) result.accept_description = data.accept_description.filter(s => typeof s === "string");
    if (Array.isArray(data.support_formats)) result.support_formats = streams(data.support_formats).map(s =>
      pick(s, ["quality", "format", "description", "new_description", "display_desc", "superscript", "codecs", "need_login", "need_vip"]));
    for (const key of ["is_preview", "is_drm", "widevine_pssh", "bilidrm_uri"]) {
      if (key in data) result[key] = flag(data[key]);
    }
    if (protectedStream(data)) result.is_drm = true;
    for (const key of ["drm_type", "drm_tech_type"]) {
      if (key in result && !flag(result[key])) result[key] = 0;
    }
    const raw = object(data.dash) ? data.dash : {};
    const dolby = object(raw.dolby) ? raw.dolby : {};
    result.dash = {
      ...pick(raw, ["duration", "minBufferTime", "min_buffer_time"]),
      video: streams(raw.video).map(cleanStream),
      audio: streams(raw.audio).map(cleanStream),
      dolby: { type: integer(dolby.type), audio: streams(dolby.audio).map(cleanStream) }
    };
    return result;
  }
  function classify(data) {
    const dash = data.dash || {};
    const dolby = dash.dolby || {};
    const type = integer(dolby.type);
    const video = streams(dash.video);
    const audio = streams(dolby.audio);
    const restricted = protectedStream(data) || flag(data.is_preview);
    const hasDv = !restricted && video.some(s => integer(s.id) === 126 && hasUrl(s) && !protectedStream(s));
    const hasEc3 = !restricted && audio.some(s => ec3(s) && hasUrl(s) && !protectedStream(s));
    const candidate = type === 2 && hasEc3;
    const status = restricted ? "unavailable" : (candidate ? (hasDv ? "dual_candidate" : "atmos_only_candidate") :
      (type === 2 || ![...video, ...streams(dash.audio), ...audio].some(hasUrl) ? "unavailable" : "non_atmos"));
    const reason = restricted ? (flag(data.is_preview) ? "接口仅返回预览，不能作为完整视频下载。" :
      "接口返回受保护的流；此工具仅处理直接可下载的原始流。") :
      (candidate ? (hasDv ? "可获取双杜比候选流；下载后须验证码流。" : "可获取 Atmos 候选音轨，但未提供 DV 视频。") :
        (status === "unavailable" ? "未提供可用原始流；地址可能已过期，请重新导出并检查登录、会员及权限。" : "API 未标为全景声。"));
    return {
      type, dolby_type: type, api_advertised_atmos: type === 2,
      api_atmos_candidate: candidate, has_dv: hasDv, has_ec3: hasEc3,
      dual_candidate: candidate && hasDv,
      advertised_dv: (Array.isArray(data.accept_quality) ? data.accept_quality : []).some(q => integer(q) === 126) ||
        streams(data.support_formats).some(f => integer(f.quality) === 126),
      status, reason
    };
  }
  function safeError(error) {
    if (error.localMessage) return {status: "unavailable", error: error.localMessage};
    if (error.apiCode !== undefined) {
      const code = error.apiCode;
      const kind = [-101, -403, -10403, -10404].includes(code) ? "鉴权或权限限制" :
        ([-352, -412, -401].includes(code) ? "风控拦截" : "接口错误");
      const status = [-101, -403, -10403, -10404].includes(code) ? "auth_error" :
        ([-352, -412, -401].includes(code) ? "risk_error" : "api_error");
      return {status, error: `${kind} (code=${code})`, error_code: code};
    }
    const name = ["AbortError", "TypeError", "SyntaxError", "Error"].includes(error.name) ? error.name : "Error";
    return {status: "api_error", error: name === "AbortError" ? "请求超时；请稍后重试。" :
      `请求失败 (${name})；检查网络、登录状态或稍后重试。`};
  }
  function localError(message) {
    const error = new Error("Unavailable video metadata");
    error.localMessage = message;
    return error;
  }
  async function api(path, params) {
    const url = new URL(path, "https://api.bilibili.com");
    for (const [key, value] of Object.entries(params)) url.searchParams.set(key, String(value));
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    let response;
    let body;
    try {
      response = await fetch(url, {credentials: "include", redirect: "error", cache: "no-store", signal: controller.signal});
      if (response.ok) body = await response.json();
    } finally { clearTimeout(timeout); }
    if (!response.ok) {
      const error = new Error("HTTP API failure");
      error.apiCode = -response.status;
      throw error;
    }
    if (!object(body) || !Number.isInteger(body.code)) throw new Error("Invalid API envelope");
    if (body.code !== 0) {
      const error = new Error("Bilibili API failure");
      error.apiCode = body.code;
      throw error;
    }
    if (!object(body.data)) throw new Error("Invalid API data");
    return body.data;
  }
  for (const bvid of bvids) {
    try {
      const meta = await api("/x/web-interface/view", {bvid});
      let pages = streams(meta.pages);
      if (!pages.length && meta.cid) pages = [{cid: meta.cid, page: 1, part: meta.title}];
      if (!pages.length) throw localError("视频信息没有有效分 P CID；请检查视频是否仍然可访问。");
      if (!allPages) pages = pages.slice(0, 1);
      for (const page of pages) {
        const title = typeof meta.title === "string" ? meta.title : bvid;
        const entry = {bvid, title, cid: integer(page.cid), page: integer(page.page, 1),
          part: typeof page.part === "string" ? page.part : title};
        try {
          if (entry.cid <= 0 || entry.page <= 0) throw localError("分 P CID 或页码无效；此分 P 未请求播放接口。");
          const data = await api("/x/player/playurl", {
            bvid, cid: entry.cid, qn: 126, fnval: 4048, fnver: 0, fourk: 1
          });
          entry.playinfo = sanitize(data);
          entry.classification = classify(entry.playinfo);
          entry.status = entry.classification.status;
          if (entry.classification.api_atmos_candidate) {
            console.log(`${bvid} P${entry.page}: ${entry.classification.dual_candidate ? "双杜比候选" : "Atmos 候选，未提供 DV"}；须下载后验证。`);
          }
        } catch (error) {
          Object.assign(entry, {status: "api_error"}, safeError(error));
          manifest.errors.push({bvid, cid: entry.cid, page: entry.page, ...safeError(error)});
        }
        manifest.entries.push(entry);
        await delay();
      }
    } catch (error) {
      const failure = {bvid, status: "api_error", ...safeError(error)};
      manifest.entries.push(failure);
      manifest.errors.push(failure);
      await delay();
    }
  }
  const blob = new Blob([JSON.stringify(manifest, null, 2) + "\n"], {type: "application/json"});
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = "bilibili-dolby-manifest.json";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
  console.log(`已导出 ${manifest.entries.length} 条；双杜比候选 ${manifest.entries.filter(e => e.classification?.dual_candidate).length} 条；错误 ${manifest.errors.length} 条。`);
  return {entries: manifest.entries.length, candidates: manifest.entries.filter(e => e.classification?.dual_candidate).length,
    errors: manifest.errors.length};
})();
