/* Ocean · 旅行助手 前端逻辑
 * 目标：1:1 还原参考视频的版式与半透明玻璃质感，同时完整接后端能力。
 * 兼容：ES5 语法（不带箭头函数/模板字符串/async），XHR 流式解析，
 *       不使用 EventSource，不使用任何前端框架与构建工具。
 */
(function () {
  'use strict';

  /* ================= 常量 ================= */

  var STORE = {
    api: 'ocean.api.base',
    sessions: 'ocean.sessions.v1',
    active: 'ocean.active.v1',
    wallpaper: 'ocean.wallpaper.v1',
    device: 'ocean.device.v1'
  };

  /* 固定壁纸：统一 2560×1340（1.91:1），和全屏 cover 的裁切比例一致，
   * 换哪一张首屏构图都不会跳。加新图照着这个尺寸出一份，别直接塞原图——
   * 手机上传的大图会让首屏多等几百毫秒。 */
  var WALLPAPERS = [
    { key: 'sea', label: '海上日落', url: './assets/wallpaper-sea.jpg' },
    { key: 'bay', label: '晨雾海湾', url: './assets/wallpaper-bay.jpg' },
    { key: 'night', label: '星空海面', url: './assets/wallpaper-night.jpg' },
    { key: 'fuji', label: '富士暮色', url: './assets/wallpaper-fuji.jpg' },
    { key: 'peak', label: '雪峰倒影', url: './assets/wallpaper-peak.jpg' }
  ];

  var ERROR_TEXT = {
    MODEL_UNAVAILABLE: '模型服务不可用，请检查网络或稍后重试',
    MODEL_TIMEOUT: '模型响应超时，可重试',
    MODEL_EMPTY_RESPONSE: '模型未返回内容，可重试',
    PERMISSION_DENIED: '该操作未获授权，已取消',
    PATH_NOT_ALLOWED: '该路径不在允许范围内',
    TOOL_NOT_ENABLED: '该功能未启用或未配置',
    MEDIA_BACKEND_MISSING: '未配置该功能的生成后端',
    MEDIA_UPSTREAM_ERROR: '生成服务返回异常，请稍后重试',
    MEDIA_UPSTREAM_TIMEOUT: '生成服务超时，请稍后重试',
    TASK_NOT_FOUND: '任务不存在或已过期',
    RATE_LIMITED: '操作过于频繁，请稍后重试',
    SESSION_NOT_FOUND: '会话不存在或已过期',
    SESSION_EXPIRED: '会话已过期，请新建会话',
    VALIDATION_ERROR: '输入不合法',
    INVALID_REQUEST: '请求无效',
    UPLOAD_TOO_LARGE: '图片太大，换一张小一点的',
    UPLOAD_TYPE_NOT_ALLOWED: '这个文件不是能识别的图片',
    UPLOAD_NOT_FOUND: '图片已失效，请重新选择',
    VISION_NOT_ENABLED: '当前未启用图片输入',
    NETWORK: '无法连接后端服务，请确认后端已启动',
    NETWORK_TIMEOUT: '请求超时，请稍后重试',
    INTERNAL_ERROR: '处理失败，请稍后重试'
  };

  var CAT_TEXT = {
    info: '信息',
    search: '检索',
    plan: '规划',
    media: '生图',
    system: '系统',
    plugin: '插件',
    other: '其他'
  };

  var ACTION_TEXT = {
    write_file: '写入文件',
    open_file: '用默认程序打开文件',
    create_dir: '创建目录',
    delete_path: '删除文件 / 目录',
    set_wallpaper: '切换桌面壁纸',
    read_file: '读取文件'
  };

  var PLATFORM_TEXT = {
    amap: '高德地图',
    weather: '天气',
    bing_cn: '必应检索',
    web_fetch: '网页抓取',
    douyin: '抖音',
    bilibili: 'B站',
    xiaohongshu: '小红书',
    weibo: '微博',
    kuaishou: '快手',
    zhihu: '知乎'
  };

  var CAP_TEXT = [
    { key: 'image_gen', label: '旅行图片生成' },
    { key: 'filesystem', label: '沙箱文件读写' },
    { key: 'wallpaper', label: '桌面壁纸切换' },
    { key: 'link_parse', label: '分享链接解析' },
    { key: 'search_web', label: '网页检索' },
    { key: 'session_fetch', label: '登录态采集' },
    { key: 'search_social', label: '社媒内容检索' }
  ];

  var SUGGESTIONS = [
    '帮我规划 5 天三亚行程，预算 8000，住大东海',
    '三亚最近天气怎么样？',
    '杭州两日周末行程，喜欢人文和咖啡',
    '生成一张三亚海边旅行打卡照'
  ];

  /* 每种风格的空状态与输入框文案。
     换了风格却还满屏「告诉我目的地、天数与预算」，切换就像只换了个皮肤；
     文案跟 key 走，后端新增风格时这里没有对应项就回落到默认那套。 */
  var PERSONA_EMPTY = {
    concierge: {
      title: 'Where to<br>next?',
      sub: '告诉我目的地、天数、预算或想住的区域。天气、地点、路线、住宿与图片都会来自真实工具调用。',
      placeholder: '告诉我目的地、天数与预算…',
      sugg: SUGGESTIONS
    },
    companion: {
      title: '边聊<br>边定',
      sub: '还没想好去哪儿也没关系。说说有几天、大概什么时候、想清静还是想热闹，我先把我的倾向给你。',
      placeholder: '先说说你大概想怎么玩…',
      sugg: ['三天，不想太赶，你更推荐哪儿', '带爸妈出门，五天左右', '一个人去海边待几天']
    },
    guide: {
      title: '这地方<br>为什么这样',
      sub: '想听哪座城市、哪段历史的来龙去脉？季节、地形、本地人的习惯都可以问。',
      placeholder: '想了解哪个地方…',
      sugg: ['泉州为什么被叫做世界宗教博物馆', '平潭的蓝眼泪什么时候能看到', '杭州的梅雨季具体是几月']
    },
    confidant: {
      title: '今天<br>怎么样',
      sub: '想说什么都行，不用组织语言。我不给方案，也不劝你出门。',
      placeholder: '慢慢说…',
      sugg: ['今天有点闷', '有件事我一直没想通', '就想随便聊聊']
    }
  };

  /* ================= 状态 ================= */

  var state = {
    caps: null,
    tools: {},
    toolList: [],
    sessionId: '',
    sessions: [],
    /* 待发送的附件。每个元素代表用户选中的一个文件：
       视频会展开成多张帧，所以 ids 是数组，预览只显示一张 */
    attachments: [],
    streaming: false,
    stream: null,
    perm: null,
    permQueue: [],
    permTimer: null,
    media: null,
    sheet: '',
    stick: true,
    capReady: false,
    // 对话风格：personas 是后端给的选项，persona 是当前选中项。
    // 存在会话上而不是浏览器里——换设备、清缓存后不该丢
    personas: [],
    persona: '',
    personaDefault: ''
  };

  var STATUS = { timer: null };
  var DOM = {};

  /* ================= 基础工具 ================= */

  function $(sel, root) {
    return (root || document).querySelector(sel);
  }

  function esc(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function trim(value) {
    return String(value === null || value === undefined ? '' : value).replace(/^\s+|\s+$/g, '');
  }

  function lsGet(key, fallback) {
    try {
      var raw = window.localStorage.getItem(key);
      return raw === null ? fallback : raw;
    } catch (e) {
      return fallback;
    }
  }

  function lsSet(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (e) {
      /* 隐私模式或容量不足时静默失败，不阻断主流程 */
    }
  }

  function lsDel(key) {
    try {
      window.localStorage.removeItem(key);
    } catch (e) { /* 同上 */ }
  }

  /* 设备 ID：只用于让后端按设备隔离长期偏好，不含任何用户身份信息 */
  function deviceId() {
    var id = lsGet(STORE.device, '');
    if (id && /^[A-Za-z0-9_-]{8,64}$/.test(id)) return id;
    id = '';
    try {
      if (window.crypto && window.crypto.randomUUID) id = window.crypto.randomUUID();
    } catch (e) { id = ''; }
    if (!id) {
      id = 'dev-' + String(Date.now()) + '-' + Math.random().toString(16).slice(2, 10);
    }
    lsSet(STORE.device, id);
    return id;
  }

  function toast(text, ms) {
    var node = DOM.toast;
    if (!node) return;
    node.innerHTML = esc(text);
    node.hidden = false;
    if (STATUS.timer) clearTimeout(STATUS.timer);
    STATUS.timer = setTimeout(function () {
      node.hidden = true;
    }, ms || 2600);
  }

  function pad2(n) {
    return n < 10 ? '0' + n : String(n);
  }

  function timeText(ts) {
    if (!ts) return '';
    var diff = Date.now() - ts;
    if (diff < 60000) return '刚刚';
    if (diff < 3600000) return Math.floor(diff / 60000) + ' 分钟前';
    var d = new Date(ts);
    return pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  /* ================= 接口层 ================= */

  function autoBase() {
    if (location.protocol === 'file:') return 'http://localhost:8000/api/v1';
    /* 手工填过接口地址就以它为准——这是唯一的显式配置，不该被自动推断覆盖 */
    var override = lsGet(STORE.api, '');
    if (override) return String(override).replace(/\/+$/, '');
    /* 页面由 http/https 打开时一律走同源相对路径：
       - serve.py 在**任意端口**都把 /api/** 反代到后端，换端口不用改前端；
       - 直连 http://host:8000 在 https 页面里会被当成混合内容拦掉；
       - 跨端口还要后端把来源加进 CORS_ORIGINS，漏一个就显示「未连接」。
       早先这里写死了「3001 端口才走相对路径」，换端口验证时整个页面都连不上，
       而且报错长得像后端没启动。只有用别的静态服务器打开（同源没有 /api）时，
       才需要在「设置 → 后端接口地址」里手填。 */
    return '/api/v1';
  }

  var API_BASE = autoBase();

  function normalizeError(data, status) {
    var code = data && data.code ? String(data.code) : (status ? 'HTTP_' + status : 'NETWORK');
    var message = ERROR_TEXT[code];
    if (!message) {
      if (data && data.message) message = String(data.message);
      else if (code.indexOf('HTTP_') === 0) message = '请求失败（' + status + '）';
      else message = '请求失败，请稍后重试';
    }
    return { code: code, message: message, detail: data && data.message ? String(data.message) : '' };
  }

  function request(method, path, body, cb) {
    var xhr = new XMLHttpRequest();
    var finished = false;
    function done(err, data) {
      if (finished) return;
      finished = true;
      cb(err, data);
    }
    try {
      xhr.open(method, API_BASE + path, true);
    } catch (e) {
      done(normalizeError(null, 0), null);
      return null;
    }
    xhr.timeout = 25000;
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.setRequestHeader('X-Device-ID', deviceId());
    if (body) xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onload = function () {
      var data = null;
      if (xhr.responseText) {
        try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      }
      if (xhr.status >= 200 && xhr.status < 300) done(null, data);
      else done(normalizeError(data, xhr.status), null);
    };
    xhr.onerror = function () { done(normalizeError(null, 0), null); };
    xhr.ontimeout = function () { done({ code: 'NETWORK_TIMEOUT', message: ERROR_TEXT.NETWORK_TIMEOUT, detail: '' }, null); };
    try {
      xhr.send(body ? JSON.stringify(body) : null);
    } catch (e) {
      done(normalizeError(null, 0), null);
    }
    return xhr;
  }

  /* ================= 连接与能力 ================= */

  function setConn(kind, text) {
    if (!DOM.conn) return;
    DOM.conn.className = 'conn' + (kind === 'on' ? ' is-on' : kind === 'off' ? ' is-off' : '');
    DOM.connText.innerHTML = esc(text);
  }

  function loadCapabilities(cb) {
    request('GET', '/system/capabilities', null, function (err, data) {
      if (err) {
        state.capReady = false;
        setConn('off', '未连接 · 点此重试');
        applyCapabilities();
        if (cb) cb(err);
        return;
      }
      state.caps = data;
      state.capReady = true;
      setConn('on', '已连接后端');
      applyCapabilities();
      renderChips();
      if (cb) cb(null, data);
    });
  }

  function loadTools(cb) {
    request('GET', '/tools', null, function (err, data) {
      if (!err && data && data.items) {
        state.toolList = data.items;
        state.tools = {};
        for (var i = 0; i < data.items.length; i++) {
          state.tools[data.items[i].name] = data.items[i];
        }
      }
      if (cb) cb(err);
    });
  }

  function applyCapabilities() {
    var caps = state.caps || {};
    var showImage = !!caps.image_gen;
    if (DOM.mediaBtn) DOM.mediaBtn.hidden = !showImage;
    if (DOM.composerMedia) DOM.composerMedia.hidden = !showImage;
    /* 图片输入：上传链路就绪才放按钮。放一个点了必然失败的入口，
       比没有入口更糟——用户会以为是自己的图有问题 */
    if (DOM.composerAttach) DOM.composerAttach.hidden = !caps.vision;
    /* 生视频不在产品范围内，界面永远不展示入口 */
  }

  function platformLabel(key) {
    return PLATFORM_TEXT[key] || key;
  }

  function renderChips() {
    if (!DOM.sourceChips) return;
    var caps = state.caps || {};
    var chips = [];
    chips.push('<span class="chip ' + (state.capReady ? 'is-ok' : 'is-off') + '">' + (state.capReady ? '已连接' : '未连接') + '</span>');
    var platforms = caps.platforms || [];
    var max = 4;
    for (var i = 0; i < platforms.length && i < max; i++) {
      chips.push('<span class="chip">' + esc(platformLabel(platforms[i])) + '</span>');
    }
    if (platforms.length > max) {
      chips.push('<span class="chip">+' + (platforms.length - max) + '</span>');
    }
    if (!state.capReady) {
      chips.push('<span class="chip">检查后端后重试</span>');
    }
    DOM.sourceChips.innerHTML = chips.join('');
  }

  /* ================= 附件：图片与视频 ================= */

  var MAX_ATTACHMENTS = 6;
  var MAX_UPLOAD_BYTES = 12 * 1024 * 1024;
  /* 视频抽几帧。4 帧足够回答「这是哪儿」「大概什么样」，
     再多只是在替同一段画面重复付 token */
  var VIDEO_FRAMES = 4;
  var attachSeq = 0;

  function visionReady() {
    return !!(state.caps && state.caps.vision);
  }

  function uploadUrl(id) {
    return API_BASE + '/media/uploads/' + encodeURIComponent(id);
  }

  function pendingIds() {
    var ids = [];
    for (var i = 0; i < state.attachments.length; i++) {
      var list = state.attachments[i].ids || [];
      for (var j = 0; j < list.length; j++) ids.push(list[j]);
    }
    return ids;
  }

  function attachmentsBusy() {
    for (var i = 0; i < state.attachments.length; i++) {
      if (state.attachments[i].busy) return true;
    }
    return false;
  }

  function slotCount() {
    var used = 0;
    for (var i = 0; i < state.attachments.length; i++) {
      /* 还在上传的按 1 张算，否则一边传一边加会突破上限 */
      used += Math.max(1, (state.attachments[i].ids || []).length);
    }
    return used;
  }

  function pickAttachments() {
    if (!DOM.attachFile) return;
    /* 清空 value：否则连续两次选同一个文件不会触发 change */
    DOM.attachFile.value = '';
    DOM.attachFile.click();
  }

  function onFilesPicked() {
    var files = DOM.attachFile && DOM.attachFile.files;
    if (!files || !files.length) return;
    var room = MAX_ATTACHMENTS - slotCount();
    if (room <= 0) {
      toast('一条消息最多 ' + MAX_ATTACHMENTS + ' 张图');
      return;
    }
    if (files.length > room) {
      toast('只取了前 ' + room + ' 个，一条消息最多 ' + MAX_ATTACHMENTS + ' 张图');
    }
    var batch = [];
    for (var i = 0; i < files.length && batch.length < room; i++) batch.push(files[i]);
    for (var j = 0; j < batch.length; j++) addAttachment(batch[j]);
  }

  function addAttachment(file) {
    var name = file.name || '';
    var isVideo = /^video\//.test(file.type || '') || /\.(mp4|mov|m4v|webm|3gp)$/i.test(name);
    if (file.size > MAX_UPLOAD_BYTES) {
      toast('「' + name + '」超过 12MB，换小一点的');
      return;
    }
    if (!isVideo && !/^image\//.test(file.type || '')) {
      toast('只支持图片和视频');
      return;
    }
    attachSeq += 1;
    var entry = { key: 'a' + attachSeq, kind: isVideo ? 'video' : 'image', ids: [], preview: '', busy: true };
    state.attachments.push(entry);
    renderAttachStrip();
    if (isVideo) uploadVideo(entry, file);
    else uploadImage(entry, file);
  }

  function uploadImage(entry, file) {
    /* 先本地预览再上传：手机上传几百 KB 也要一秒，等完再显示像卡住了 */
    entry.preview = URL.createObjectURL(file);
    renderAttachStrip();
    uploadBlob(file, file.name, function (err, data) {
      finishAttachment(entry, err, data ? [data] : []);
    });
  }

  /* 视频不往服务端传：浏览器本来就会解码，服务端再装一个 ffmpeg 只为抽 4 帧不划算。
     抽帧在本地做，传上去的是 4 张几十 KB 的 JPEG，而不是一段几十 MB 的 MOV——
     手机流量下这个差别就是「发得出去」和「发不出去」。 */
  function uploadVideo(entry, file) {
    toast('正在从视频里抽帧…');
    extractVideoFrames(file, function (err, frames) {
      if (err) { failAttachment(entry, err.message); return; }
      if (!frames.length) { failAttachment(entry, '这段视频抽不出画面'); return; }
      entry.preview = URL.createObjectURL(frames[0]);
      renderAttachStrip();
      uploadFrames(entry, frames, 0, []);
    });
  }

  function uploadFrames(entry, frames, index, done) {
    if (index >= frames.length) { finishAttachment(entry, null, done); return; }
    uploadBlob(frames[index], 'frame-' + (index + 1) + '.jpg', function (err, data) {
      if (err) { failAttachment(entry, err.message); return; }
      done.push(data);
      uploadFrames(entry, frames, index + 1, done);
    });
  }

  function finishAttachment(entry, err, items) {
    if (err) { failAttachment(entry, err.message); return; }
    var ids = [];
    for (var i = 0; i < items.length; i++) ids.push(items[i].upload_id);
    entry.ids = ids;
    entry.busy = false;
    renderAttachStrip();
    checkSendable();
  }

  function failAttachment(entry, message) {
    toast(message || '上传失败');
    removeAttachment(entry.key, true);
  }

  function removeAttachment(key, keepQuiet) {
    for (var i = 0; i < state.attachments.length; i++) {
      if (state.attachments[i].key !== key) continue;
      var entry = state.attachments[i];
      state.attachments.splice(i, 1);
      if (entry.preview) URL.revokeObjectURL(entry.preview);
      /* 已经传上去的顺手删掉，别在服务器上堆没人用的图 */
      for (var j = 0; j < entry.ids.length; j++) {
        request('DELETE', '/media/uploads/' + encodeURIComponent(entry.ids[j]), null, function () {});
      }
      renderAttachStrip();
      checkSendable();
      if (!keepQuiet) return;
      return;
    }
  }

  function clearAttachments() {
    for (var i = 0; i < state.attachments.length; i++) {
      if (state.attachments[i].preview) URL.revokeObjectURL(state.attachments[i].preview);
    }
    state.attachments = [];
    renderAttachStrip();
    checkSendable();
  }

  function renderAttachStrip() {
    var strip = DOM.attachStrip;
    if (!strip) return;
    if (!state.attachments.length) {
      strip.hidden = true;
      strip.innerHTML = '';
      if (DOM.composer) DOM.composer.className = 'composer';
      return;
    }
    strip.hidden = false;
    if (DOM.composer) DOM.composer.className = 'composer has-attach';
    var html = [];
    for (var i = 0; i < state.attachments.length; i++) {
      var a = state.attachments[i];
      html.push(
        '<div class="attach-chip' + (a.busy ? ' is-busy' : '') + '" data-key="' + a.key + '">' +
          (a.preview ? '<img src="' + esc(a.preview) + '" alt="">' : '') +
          (a.kind === 'video' ? '<span class="attach-tag">' + (a.ids.length || VIDEO_FRAMES) + ' 帧</span>' : '') +
          '<button type="button" class="attach-del" data-key="' + a.key + '" aria-label="移除">' +
            '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 3l6 6M9 3l-6 6"/></svg>' +
          '</button>' +
        '</div>'
      );
    }
    strip.innerHTML = html.join('');
  }

  function onAttachStripClick(event) {
    var node = event.target;
    while (node && node !== DOM.attachStrip) {
      if (node.getAttribute && node.getAttribute('data-key')) {
        removeAttachment(node.getAttribute('data-key'));
        return;
      }
      node = node.parentNode;
    }
  }

  function uploadBlob(blob, name, cb) {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', API_BASE + '/media/uploads', true);
    xhr.timeout = 60000;
    xhr.setRequestHeader('Content-Type', blob.type || 'application/octet-stream');
    /* HTTP 头只能放 latin-1：中文文件名不 encodeURIComponent 的话，
       setRequestHeader 会直接抛异常，用户看到的是「上传失败」但控制台没线索 */
    xhr.setRequestHeader('X-File-Name', encodeURIComponent(name || ''));
    xhr.setRequestHeader('X-Device-ID', deviceId());
    xhr.onload = function () {
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      if (xhr.status >= 200 && xhr.status < 300 && data && data.upload_id) cb(null, data);
      else cb(normalizeError(data, xhr.status), null);
    };
    xhr.onerror = function () { cb(normalizeError(null, 0), null); };
    xhr.ontimeout = function () { cb(normalizeError({ code: 'NETWORK_TIMEOUT' }, 0), null); };
    try { xhr.send(blob); } catch (e) { cb(normalizeError(null, 0), null); }
  }

  function toJpegBlob(canvas, cb) {
    if (canvas.toBlob) { canvas.toBlob(cb, 'image/jpeg', 0.82); return; }
    /* 老内核没有 toBlob：退回 dataURL 再转，慢一点但能用 */
    var dataUrl = canvas.toDataURL('image/jpeg', 0.82);
    var binary = atob(dataUrl.split(',')[1] || '');
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    cb(new Blob([bytes], { type: 'image/jpeg' }));
  }

  function extractVideoFrames(file, cb) {
    var url = URL.createObjectURL(file);
    var video = document.createElement('video');
    var canvas = document.createElement('canvas');
    var shots = [];
    var index = 0;
    var duration = 0;
    var finished = false;

    function finish(err, frames) {
      if (finished) return;
      finished = true;
      URL.revokeObjectURL(url);
      cb(err, frames || []);
    }

    video.preload = 'auto';
    video.muted = true;
    video.setAttribute('playsinline', '');
    video.onerror = function () { finish(new Error('浏览器打不开这段视频，换一段试试')); };

    video.onloadedmetadata = function () {
      duration = isFinite(video.duration) ? video.duration : 0;
      if (!duration) { finish(new Error('读不到视频时长')); return; }
      grab();
    };

    video.onseeked = function () {
      var width = video.videoWidth || 0;
      var height = video.videoHeight || 0;
      if (!width || !height) { finish(new Error('读不到视频画面')); return; }
      /* 长边压到 960：模型看的是构图和内容，不是像素 */
      var scale = Math.min(1, 960 / Math.max(width, height));
      canvas.width = Math.round(width * scale);
      canvas.height = Math.round(height * scale);
      canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
      toJpegBlob(canvas, function (blob) {
        if (blob) shots.push(blob);
        index += 1;
        grab();
      });
    };

    function grab() {
      if (index >= VIDEO_FRAMES) { finish(null, shots); return; }
      /* 均匀取帧，避开首尾各 5%：开头常是黑场，结尾常是水印 */
      var ratio = VIDEO_FRAMES === 1 ? 0.5 : 0.05 + 0.9 * (index / (VIDEO_FRAMES - 1));
      var target = duration * ratio;
      if (target > duration - 0.05) target = Math.max(0, duration - 0.05);
      video.currentTime = target;
    }

    video.src = url;
  }

  /* ================= 视图切换 ================= */

  function isAssistant() {
    return DOM.assistant.className.indexOf('is-on') >= 0;
  }

  function openAssistant() {
    var wasOpen = isAssistant();
    DOM.hero.hidden = true;
    DOM.assistant.className = 'assistant is-on';
    DOM.app.className = DOM.app.className.replace(' is-immersive', '');
    if (DOM.focusExit) DOM.focusExit.hidden = true;
    closeRail();
    if (!wasOpen) {
      if (state.sessionId) loadHistory(state.sessionId);
      else renderEmpty();
    }
    refreshComposer();
    stickNow();
  }

  function backHome() {
    DOM.assistant.className = 'assistant';
    DOM.hero.hidden = false;
    closeRail();
    closeSheet();
  }

  function setChatTitle(text) {
    if (!DOM.chatTitle) return;
    DOM.chatTitle.innerHTML = esc(text || '新的旅程');
    document.title = (text ? text + ' · ' : '') + 'Ocean 旅行助手';
  }

  function toggleFocus() {
    var on = DOM.app.className.indexOf('is-immersive') >= 0;
    if (on) {
      DOM.app.className = DOM.app.className.replace(' is-immersive', '');
      DOM.focusExit.hidden = true;
    } else {
      DOM.app.className += ' is-immersive';
      DOM.focusExit.hidden = false;
    }
  }

  /* ================= 桌面全屏 ================= */

  function isFullscreen() {
    return !!(document.fullscreenElement || document.webkitFullscreenElement);
  }

  function syncFullBtn() {
    if (!DOM.fullBtn) return;
    var on = isFullscreen();
    DOM.fullBtn.className = on ? 'icon-orb full-btn is-full' : 'icon-orb full-btn';
    DOM.fullBtn.setAttribute('aria-label', on ? '退出全屏' : '全屏');
    DOM.fullBtn.setAttribute('title', on ? '退出全屏（Esc）' : '全屏（F11）');
  }

  function toggleFullscreen() {
    var root = document.documentElement;
    if (isFullscreen()) {
      var exit = document.exitFullscreen || document.webkitExitFullscreen;
      if (exit) {
        try { exit.call(document); } catch (e) { /* 浏览器拒绝时保持原状 */ }
      }
      return;
    }
    var request = root.requestFullscreen || root.webkitRequestFullscreen;
    if (!request) {
      toast('当前浏览器不支持全屏，可按 F11');
      return;
    }
    try {
      var pending = request.call(root);
      if (pending && pending.catch) {
        pending.catch(function () { toast('全屏被浏览器拒绝，可按 F11'); });
      }
    } catch (e) {
      toast('全屏被浏览器拒绝，可按 F11');
    }
  }

  function onFullscreenChange() {
    syncFullBtn();
    /* 进入/退出全屏会改变可视高度，补一次重排，避免上下留白 */
    setTimeout(function () { applyViewport(); }, 120);
  }

  /* ================= 会话 ================= */

  function readSessions() {
    try {
      var list = JSON.parse(lsGet(STORE.sessions, '[]'));
      return Object.prototype.toString.call(list) === '[object Array]' ? list : [];
    } catch (e) {
      return [];
    }
  }

  function saveSessions() {
    lsSet(STORE.sessions, JSON.stringify(state.sessions));
  }

  function renderSessions() {
    var box = DOM.sessionList;
    if (!box) return;
    if (!state.sessions.length) {
      box.innerHTML = '<p class="rail-empty">还没有会话。点击右上角 + 开始一段新的旅程。</p>';
      return;
    }
    var html = [];
    for (var i = 0; i < state.sessions.length; i++) {
      var item = state.sessions[i];
      html.push(
        '<button type="button" class="session' + (item.id === state.sessionId ? ' is-active' : '') + '" data-id="' + esc(item.id) + '">' +
          '<span class="session-row">' +
            '<span class="session-name">' + esc(item.title || '新的旅程') + '</span>' +
            '<span class="session-del" data-del="' + esc(item.id) + '" title="删除会话">' +
              '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2.5 2.5l7 7M9.5 2.5l-7 7"/></svg>' +
            '</span>' +
          '</span>' +
          '<span class="session-time">' + esc(timeText(item.updatedAt)) + '</span>' +
        '</button>'
      );
    }
    box.innerHTML = html.join('');
  }

  function upsertSession(id, title, createdAt) {
    var found = -1;
    for (var i = 0; i < state.sessions.length; i++) {
      if (state.sessions[i].id === id) { found = i; break; }
    }
    if (found < 0) {
      state.sessions.unshift({ id: id, title: title || '新的旅程', createdAt: createdAt || Date.now(), updatedAt: Date.now() });
    } else {
      var item = state.sessions[found];
      if (title) item.title = title;
      item.updatedAt = Date.now();
      state.sessions.splice(found, 1);
      state.sessions.unshift(item);
    }
    saveSessions();
    renderSessions();
  }

  function dropSessionLocal(id) {
    var rest = [];
    for (var i = 0; i < state.sessions.length; i++) {
      if (state.sessions[i].id !== id) rest.push(state.sessions[i]);
    }
    state.sessions = rest;
    saveSessions();
    renderSessions();
  }

  /* ================= 对话风格 ================= */

  function personaOf(key) {
    for (var i = 0; i < state.personas.length; i++) {
      if (state.personas[i].key === key) return state.personas[i];
    }
    return null;
  }

  function personaEmpty() {
    return PERSONA_EMPTY[state.persona] || PERSONA_EMPTY.concierge;
  }

  function loadPersonas(cb) {
    request('GET', '/chat/personas', null, function (err, data) {
      if (err || !data || !data.items || !data.items.length) {
        // 取不到就把入口藏起来。宁可不显示，也不要给一个点了没反应的按钮
        if (DOM.personaBtn) DOM.personaBtn.hidden = true;
        if (cb) cb();
        return;
      }
      state.personas = data.items;
      state.personaDefault = data.default || data.items[0].key;
      applyPersona(state.persona || state.personaDefault);
      if (cb) cb();
    });
  }

  function applyPersona(key) {
    var item = personaOf(key);
    var resolved = item ? item.key : (state.personaDefault || 'concierge');
    state.persona = resolved;
    if (DOM.personaName) DOM.personaName.textContent = (item || {}).name || '礼宾';
    // kicker 跟着风格走：同一条「AI TRAVEL CONCIERGE」在陪聊模式下是不准确的
    if (DOM.chatKicker) DOM.chatKicker.textContent = (item || {}).latin || 'AI TRAVEL CONCIERGE';
    if (DOM.personaBtn) DOM.personaBtn.hidden = !state.personas.length;
    // 只换占位文案，不整体走 refreshComposer：那会把发送键状态和图片按钮一起重算，
    // 生成中切换风格时会把「停止」按钮改回「发送」
    if (DOM.input && !state.streaming) {
      DOM.input.setAttribute('placeholder', personaEmpty().placeholder);
    }
    // 空状态正显示着就跟着换一套文案；已经有对话则保持原样
    if (DOM.messages && DOM.messages.querySelector('.empty')) renderEmpty();
  }

  function switchPersona(key) {
    if (key === state.persona) return;
    applyPersona(key);
    if (!state.sessionId) {
      // 还没有会话：先记住选择，等 ensureSession 建完会话再写进后端
      toast('风格已选好，发出第一条消息后生效');
      return;
    }
    syncPersona(key, function () {
      var item = personaOf(key);
      toast('已切换到「' + ((item || {}).name || key) + '」');
    });
  }

  function syncPersona(key, done) {
    // 一律写后端，不拿「是不是默认风格」当短路条件：
    // 从知心切回礼宾时，后端还停在知心，跳过这一步就永远切不回去了
    if (!state.sessionId || !key) {
      if (done) done();
      return;
    }
    request(
      'PATCH',
      '/chat/sessions/' + encodeURIComponent(state.sessionId) + '/persona',
      { persona: key },
      function (err, data) {
        if (err) {
          toast(err.message || '切换风格失败');
          return;
        }
        applyPersona((data && data.persona) || key);
        if (done) done();
      }
    );
  }

  function useSession(id) {
    if (!id) return;
    state.sessionId = id;
    lsSet(STORE.active, id);
    var meta = null;
    for (var i = 0; i < state.sessions.length; i++) {
      if (state.sessions[i].id === id) { meta = state.sessions[i]; break; }
    }
    setChatTitle(meta ? meta.title : '新的旅程');
    renderSessions();
    loadHistory(id);
    closeRail();
    closeSheet();
  }

  function newChat() {
    state.sessionId = '';
    lsDel(STORE.active);
    setChatTitle('新的旅程');
    // 新会话回到默认风格：风格是会话属性，不是全局设置
    if (state.personaDefault) applyPersona(state.personaDefault);
    clearMessages();
    renderEmpty();
    renderSessions();
    closeRail();
    closeSheet();
    if (!isAssistant()) openAssistant();
    try { DOM.input.focus(); } catch (e) { /* 忽略 */ }
  }

  function ensureSession(cb) {
    if (state.sessionId) { cb(null, state.sessionId); return; }
    request('POST', '/chat/sessions', {}, function (err, data) {
      if (err) { cb(err, null); return; }
      state.sessionId = data.session_id;
      lsSet(STORE.active, data.session_id);
      upsertSession(data.session_id, '新的旅程', new Date(data.created_at || Date.now()).getTime());
      // 用户可能在建会话之前就选好了风格，这时候才写进后端；
      // 不这么做，第一条消息会用默认风格回答，选了个寂寞
      if (state.persona && state.persona !== state.personaDefault) {
        syncPersona(state.persona, function () { cb(null, state.sessionId); });
        return;
      }
      cb(null, data.session_id);
    });
  }

  function deleteSession(id, evt) {
    if (evt && evt.stopPropagation) evt.stopPropagation();
    request('DELETE', '/chat/sessions/' + encodeURIComponent(id), null, function (err) {
      if (err && err.code !== 'SESSION_NOT_FOUND') {
        toast(err.message);
        return;
      }
      dropSessionLocal(id);
      if (state.sessionId === id) newChat();
      toast('会话已删除');
    });
  }

  /* ================= 消息渲染 ================= */

  function clearMessages() {
    if (DOM.messages) DOM.messages.innerHTML = '';
    state.stick = true;
  }

  function setScrollable() {
    DOM.messages.scrollTop = DOM.messages.scrollHeight;
  }

  function stickNow() {
    state.stick = true;
    setScrollable();
    if (DOM.toBottom) DOM.toBottom.hidden = true;
  }

  function stickIfNeeded() {
    if (state.stick) setScrollable();
  }

  function checkScroll() {
    var box = DOM.messages;
    if (!box) return;
    var gap = box.scrollHeight - box.scrollTop - box.clientHeight;
    state.stick = gap < 90;
    if (DOM.toBottom) DOM.toBottom.hidden = state.stick;
  }

  function renderEmpty() {
    var box = DOM.messages;
    if (!box) return;
    var copy = personaEmpty();
    var sugg = [];
    for (var i = 0; i < copy.sugg.length; i++) {
      sugg.push('<button type="button" data-prompt="' + esc(copy.sugg[i]) + '">' + esc(copy.sugg[i]) + '</button>');
    }
    box.innerHTML =
      '<div class="empty">' +
        '<p class="empty-title">' + copy.title + '</p>' +
        '<p class="empty-sub">' + esc(copy.sub) + '</p>' +
        '<div class="sugg">' + sugg.join('') + '</div>' +
      '</div>';
  }

  function appendUser(text, attachments) {
    var node = document.createElement('div');
    var hasMedia = !!(attachments && attachments.length);
    node.className = hasMedia ? 'msg msg-user has-media' : 'msg msg-user';
    var html = '';
    if (attachments && attachments.length) {
      html += '<div class="msg-thumbs">';
      for (var i = 0; i < attachments.length; i++) {
        /* 用服务端地址而不是本地 objectURL：刷新后还能显示，也不用管生命周期 */
        html += '<img src="' + esc(uploadUrl(attachments[i])) + '" alt="" loading="lazy">';
      }
      html += '</div>';
    }
    if (text) html += '<div class="msg-text">' + esc(text) + '</div>';
    node.innerHTML = html;
    DOM.messages.appendChild(node);
    stickIfNeeded();
  }

  function createAssistantShell() {
    var root = document.createElement('div');
    root.className = 'msg msg-ai';
    var tools = document.createElement('div');
    var md = document.createElement('div');
    md.className = 'md';
    md.innerHTML = '<span class="thinking"><i></i><i></i><i></i><em>正在思考…</em></span>';
    root.appendChild(tools);
    root.appendChild(md);
    DOM.messages.appendChild(root);
    stickIfNeeded();
    return { root: root, tools: tools, md: md, text: '' };
  }

  /* 千分位，避免 12345 看成 1234 或 123456 */
  function formatTokens(n) {
    var v = Number(n);
    if (!isFinite(v) || v <= 0) return '';
    return v.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  /* 耗时：1 秒以下给毫秒，以上给一位小数秒 */
  function formatLatency(ms) {
    var v = Number(ms);
    if (!isFinite(v) || v <= 0) return '';
    if (v < 1000) return Math.round(v) + 'ms';
    if (v < 60000) return (v / 1000).toFixed(1) + 's';
    var m = Math.floor(v / 60000);
    var s = Math.round((v % 60000) / 1000);
    return m + 'm' + (s ? s + 's' : '');
  }

  /* 回答下方的一行小字：耗时 · token · 成本。
     三个都有才好看，缺哪个就跳过哪个；全缺就不渲染，不留空行。 */
  function appendUsageMeta(root, latencyMs, usage) {
    if (!root || !usage) return;
    var parts = [];

    var took = formatLatency(latencyMs);
    if (took) parts.push(took);

    var total = formatTokens(usage.total_tokens);
    if (total) {
      var detail = [];
      var pin = formatTokens(usage.input_tokens);
      var pout = formatTokens(usage.output_tokens);
      if (pin) detail.push('入 ' + pin);
      if (pout) detail.push('出 ' + pout);
      parts.push(total + ' tokens' + (detail.length ? '（' + detail.join(' / ') + '）' : ''));
    }

    /* cost 为 null 表示价目表里没有这个模型，不是免费——这种情况不显示，别误导 */
    if (typeof usage.cost === 'number' && usage.cost > 0) {
      parts.push('¥' + usage.cost.toFixed(4));
    }

    if (!parts.length) return;

    var meta = document.createElement('div');
    meta.className = 'msg-meta';
    var text = parts.join(' · ');
    /* 估算值必须标出来：本地模型不返回 usage 时是猜的，不能冒充精确数字 */
    if (usage.estimated) text += ' · 估算';
    meta.textContent = text;
    if (usage.estimated) meta.title = '该模型未返回精确用量，此处为按字数估算';
    root.appendChild(meta);
  }

  function appendAssistantText(text, taskIds, usage, latencyMs) {
    var root = document.createElement('div');
    root.className = 'msg msg-ai';
    var md = document.createElement('div');
    md.className = 'md';
    md.innerHTML = markdown(text);
    root.appendChild(md);
    DOM.messages.appendChild(root);
    /* 从历史里恢复图片：优先用后端持久化的 task 列表，正文里的 task_id 兜底 */
    var ids = [];
    var tm = extractTaskId(text);
    if (tm) ids.push(tm);
    var saved = taskIds || [];
    for (var i = 0; i < saved.length; i++) {
      if (ids.indexOf(saved[i]) < 0) ids.push(saved[i]);
    }
    for (var k = 0; k < ids.length; k++) {
      var holder = document.createElement('div');
      holder.className = 'media-inline';
      root.appendChild(holder);
      startInlineTask(holder, ids[k]);
    }
    /* 刷新后用量也得在，否则「只有刚聊完那几条有数字」会让人以为是 bug */
    appendUsageMeta(root, latencyMs, usage);
    stickIfNeeded();
  }

  function appendNotice(text) {
    var node = document.createElement('p');
    node.className = 'notice';
    node.innerHTML = esc(text);
    DOM.messages.appendChild(node);
  }

  function appendErrorCard(box, err, retryText) {
    var node = document.createElement('div');
    node.className = 'error-card';
    node.innerHTML = '<b>' + esc(err.message || '请求失败') + '</b>' +
      (err.detail && err.detail !== err.message ? ' · ' + esc(err.detail) : '') +
      (retryText ? '<div class="err-actions"><button type="button" data-retry="1">重试</button></div>' : '');
    var btn = node.querySelector('[data-retry]');
    if (btn && retryText) {
      btn.onclick = function () {
        if (state.streaming) return;
        sendMessage(retryText);
      };
    }
    box.appendChild(node);
    stickIfNeeded();
  }

  function loadHistory(id) {
    var box = DOM.messages;
    if (!box) return;
    box.innerHTML = '<p class="notice">正在载入会话…</p>';
    request('GET', '/chat/sessions/' + encodeURIComponent(id) + '/messages', null, function (err, data) {
      if (state.sessionId !== id) return;
      if (err) {
        if (err.code === 'SESSION_NOT_FOUND') {
          dropSessionLocal(id);
          state.sessionId = '';
          lsDel(STORE.active);
          setChatTitle('新的旅程');
          renderEmpty();
          renderSessions();
          toast('会话已失效，已从列表移除');
        } else {
          box.innerHTML = '';
          appendErrorCard(box, err, '');
        }
        return;
      }
      box.innerHTML = '';
      // 风格跟着会话走：刷新页面后靠它恢复，不用重选
      applyPersona((data && data.persona) || state.personaDefault);
      var items = (data && data.items) || [];
      if (!items.length) { renderEmpty(); return; }
      for (var i = 0; i < items.length; i++) {
        if (items[i].role === 'user') appendUser(items[i].content || '', items[i].attachments || []);
        else
          appendAssistantText(
            items[i].content || '',
            items[i].media_task_ids || [],
            items[i].usage || null,
            items[i].latency_ms || 0
          );
      }
      stickNow();
    });
  }

  /* ================= Markdown ================= */

  function safeUrl(url) {
    return /^https?:\/\//i.test(url) ? url : '#';
  }

  function inline(raw) {
    var s = esc(raw);
    s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, function (m, label, url) {
      return '<a href="' + safeUrl(url) + '" target="_blank" rel="noopener">' + label + '</a>';
    });
    s = s.replace(/(^|[\s（(>])(https?:\/\/[^\s<）)]+)/g, function (m, pre, url) {
      return pre + '<a href="' + safeUrl(url) + '" target="_blank" rel="noopener">' + url + '</a>';
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    return s;
  }

  function splitRow(line) {
    var s = trim(line).replace(/^\|/, '').replace(/\|$/, '');
    return s.split('|');
  }

  function markdown(src) {
    var text = String(src === null || src === undefined ? '' : src).replace(/\r\n/g, '\n');
    var blocks = [];
    text = text.replace(/```[a-zA-Z0-9_+-]*\n?([\s\S]*?)```/g, function (m, code) {
      blocks.push('<pre><code>' + esc(code.replace(/\n+$/, '')) + '</code></pre>');
      return '\u0000B' + (blocks.length - 1) + '\u0000';
    });

    var lines = text.split('\n');
    var out = [];
    var para = [];
    var quote = [];
    var list = '';

    function flushPara() {
      if (para.length) { out.push('<p>' + inline(para.join(' ')) + '</p>'); para = []; }
    }
    function flushList() {
      if (list) { out.push('</' + list + '>'); list = ''; }
    }
    function flushQuote() {
      if (quote.length) { out.push('<blockquote>' + inline(quote.join(' ')) + '</blockquote>'); quote = []; }
    }
    function flushAll() { flushPara(); flushList(); flushQuote(); }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].replace(/\s+$/, '');
      var t = trim(line);

      if (!t) { flushAll(); continue; }

      if (/^\u0000B\d+\u0000$/.test(t)) { flushAll(); out.push(t); continue; }

      var h = t.match(/^(#{1,4})\s+(.+)$/);
      if (h) {
        flushAll();
        var level = h[1].length;
        out.push('<h' + level + '>' + inline(h[2]) + '</h' + level + '>');
        continue;
      }

      if (/^([-*_])\s*\1\s*\1[\s\-*_]*$/.test(t)) { flushAll(); out.push('<hr>'); continue; }

      if (/^>\s?/.test(t)) {
        flushPara(); flushList();
        quote.push(t.replace(/^>\s?/, ''));
        continue;
      }
      flushQuote();

      if (/^\|/.test(t) && i + 1 < lines.length && /^\|[\s:|-]+\|?$/.test(trim(lines[i + 1]))) {
        flushAll();
        var head = splitRow(t);
        var th = [];
        for (var c = 0; c < head.length; c++) th.push('<th>' + inline(trim(head[c])) + '</th>');
        out.push('<table><thead><tr>' + th.join('') + '</tr></thead><tbody>');
        i += 2;
        while (i < lines.length && /^\|/.test(trim(lines[i]))) {
          var cells = splitRow(lines[i]);
          var tds = [];
          for (var k = 0; k < cells.length; k++) tds.push('<td>' + inline(trim(cells[k])) + '</td>');
          out.push('<tr>' + tds.join('') + '</tr>');
          i++;
        }
        i--;
        out.push('</tbody></table>');
        continue;
      }

      var li = t.match(/^[-*+]\s+(.+)$/);
      if (li && !/^([-*_])\s*\1\s*\1/.test(t)) {
        flushPara();
        if (list !== 'ul') { flushList(); out.push('<ul>'); list = 'ul'; }
        out.push('<li>' + inline(li[1]) + '</li>');
        continue;
      }
      var oli = t.match(/^\d+[.、)]\s*(.+)$/);
      if (oli) {
        flushPara();
        if (list !== 'ol') { flushList(); out.push('<ol>'); list = 'ol'; }
        out.push('<li>' + inline(oli[1]) + '</li>');
        continue;
      }
      flushList();

      if (/^(第\s*[0-9一二三四五六七八九十]+\s*天|Day\s*\d+)/i.test(t)) {
        flushPara();
        var title = t.replace(/^#+\s*/, '').replace(/\*\*/g, '');
        out.push('<div class="day"><b>' + inline(title) + '</b></div>');
        continue;
      }

      para.push(t);
    }
    flushAll();

    var html = out.join('');
    html = html.replace(/\u0000B(\d+)\u0000/g, function (m, n) { return blocks[Number(n)]; });
    return html;
  }

  /* ================= 工具卡片 ================= */

  function toolLabel(name) {
    var info = state.tools[name];
    if (!info || !info.description) return name;
    var text = String(info.description).split('。')[0];
    text = text.replace(/\*\*/g, '').replace(/\s+/g, ' ');
    return text;
  }

  /* 从工具摘要或正文里取出 task_id（兼容 task_id / 任务 ID 两种写法） */
  function extractTaskId(text) {
    var match = String(text || '').match(
      /(?:task[_\s-]?id|任务\s*ID)[：:\s`*]*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/i
    );
    return match ? match[1] : '';
  }

  function argsText(args) {
    if (!args) return '（无参数）';
    if (typeof args === 'string') return args;
    var keys = [];
    for (var key in args) {
      if (!Object.prototype.hasOwnProperty.call(args, key)) continue;
      keys.push(key);
    }
    if (!keys.length) return '（无参数）';
    var parts = [];
    for (var i = 0; i < keys.length && i < 6; i++) {
      var v = args[keys[i]];
      var s = typeof v === 'string' ? v : JSON.stringify(v);
      if (s && s.length > 80) s = s.slice(0, 80) + '…';
      parts.push(keys[i] + ': ' + s);
    }
    return parts.join('\n');
  }

  function createToolCard(call) {
    var card = document.createElement('div');
    card.className = 'tool is-run';
    card.setAttribute('data-call', call.call_id || '');
    /* 保留参数：行程类工具执行完后要用它生成地图 */
    card.__args = call.args || {};
    card.innerHTML =
      '<button type="button" class="tool-head">' +
        '<span class="tool-dot"></span>' +
        '<span class="tool-name">' + esc(toolLabel(call.tool)) + '</span>' +
        '<span class="tool-cat">' + esc(CAT_TEXT[call.category] || '工具') + '</span>' +
        '<span class="tool-state">执行中…</span>' +
        '<span class="tool-chev">▾</span>' +
      '</button>' +
      '<div class="tool-body">' +
        '<div class="tool-args">' + esc(argsText(call.args)) + '</div>' +
        '<div class="tool-sum"></div>' +
        '<div class="tool-more"></div>' +
      '</div>';
    var head = card.querySelector('.tool-head');
    head.onclick = function () {
      card.className = card.className.indexOf('is-open') >= 0
        ? card.className.replace(' is-open', '')
        : card.className + ' is-open';
    };
    return card;
  }

  function updateToolCard(card, ev) {
    if (!card) return;
    var stateText = ev.status === 'error' ? '失败' : '已完成';
    card.className = card.className.replace(' is-run', '').replace(' is-err', '').replace(' is-ok', '');
    card.className += ev.status === 'error' ? ' is-err' : ' is-ok';
    var label = card.querySelector('.tool-state');
    if (label) label.innerHTML = esc(stateText);
    var sum = card.querySelector('.tool-sum');
    if (sum) sum.innerHTML = esc(ev.summary || '（无摘要）');
    /* 行程类工具：直接用它自己的参数生成路线图，不解析助手正文。
       按钮挂在卡片外面，不展开工具详情也能看到。 */
    var tripTool = card.getAttribute('data-tool');
    if (tripTool === 'plan_trip' || tripTool === 'optimize_day_route') {
      var tripArgs = card.__args || {};
      var tripStops = tripArgs.stops || tripArgs.places || [];
      if (tripStops.length && card.getAttribute('data-map') !== '1' && card.parentNode) {
        card.setAttribute('data-map', '1');
        var mapHolder = document.createElement('div');
        mapHolder.className = 'map-inline';
        mapHolder.innerHTML =
          '<button type="button" class="js-map">查看路线地图</button>' +
          '<button type="button" class="js-pdf">导出</button>';
        card.parentNode.insertBefore(mapHolder, card.nextSibling);
        mapHolder.querySelector('.js-map').onclick = function () {
          openTripMap(tripArgs);
        };
        mapHolder.querySelector('.js-pdf').onclick = function () {
          openTripExport(tripArgs);
        };
      }
    }
    /* 生图任务：从摘要里取出 task_id，直接在对话里出图并轮询 */
    if (card.getAttribute('data-tool') === 'generate_image') {
      var taskId = extractTaskId(ev.summary);
      if (taskId) {
        var more = card.querySelector('.tool-more');
        more.innerHTML = '<button type="button">在生图面板查看</button>';
        more.querySelector('button').onclick = function () { openSheet('media', { taskId: taskId }); };
        if (card.getAttribute('data-inline') !== '1' && card.parentNode) {
          card.setAttribute('data-inline', '1');
          var holder = document.createElement('div');
          holder.className = 'media-inline';
          card.parentNode.insertBefore(holder, card.nextSibling);
          startInlineTask(holder, taskId);
        }
      }
    }
  }

  /* ================= 对话内的生图任务卡片 ================= */

  function taskCardHtml(taskId) {
    return '<div class="media-task" data-task="' + esc(taskId) + '">' +
      '<div class="mt-head"><span class="tool-dot"></span><span class="mt-state">已提交，等待生成…</span>' +
        '<span class="mt-gap"></span><span class="mt-id">' + esc(taskId.slice(0, 8)) + '</span></div>' +
      '<div class="mt-body">' +
        '<div class="mt-frame"></div>' +
        '<div class="mt-actions"></div>' +
      '</div>' +
    '</div>';
  }

  function startInlineTask(container, taskId) {
    container.innerHTML = taskCardHtml(taskId);
    pollInline(container, taskId, 2000);
  }

  /* 图片走服务端代理：不受上游 24 小时临时链接与跨域限制，页面内可直接显示 */
  function mediaImageUrl(taskId, download) {
    return API_BASE + '/media/tasks/' + encodeURIComponent(taskId) + '/image' +
      (download ? '?download=1' : '');
  }

  /* 行程路线图：参数取自行程工具本身，服务端做地理编码与静态图缓存 */
  function tripMapUrl(args) {
    var stops = args.stops || args.places || [];
    var parts = [];
    for (var i = 0; i < stops.length; i++) {
      parts.push('stops=' + encodeURIComponent(stops[i]));
    }
    if (args.city) parts.push('city=' + encodeURIComponent(args.city));
    var origin = args.origin || args.start || '';
    if (origin) parts.push('origin=' + encodeURIComponent(origin));
    return API_BASE + '/trips/map?' + parts.join('&');
  }

  function openTripMap(args) {
    var url = tripMapUrl(args);
    toast('正在生成路线地图…');
    var probe = new Image();
    probe.onload = function () { openLightbox(url); };
    probe.onerror = function () { toast('地图生成失败，请稍后重试'); };
    probe.src = url;
  }

  /* 把行程工具的原始参数整理成后端需要的行程结构，按天数平均切分 */
  function isoDate(offset) {
    var d = new Date();
    d.setDate(d.getDate() + (offset || 0));
    var m = d.getMonth() + 1;
    var day = d.getDate();
    return d.getFullYear() + '-' + (m < 10 ? '0' + m : m) + '-' + (day < 10 ? '0' + day : day);
  }

  function addDays(iso, offset) {
    var parts = String(iso || '').split('-');
    if (parts.length !== 3) return '';
    var d = new Date(Date.UTC(+parts[0], +parts[1] - 1, +parts[2]));
    d.setUTCDate(d.getUTCDate() + offset);
    var m = d.getUTCMonth() + 1;
    var day = d.getUTCDate();
    return d.getUTCFullYear() + '-' + (m < 10 ? '0' + m : m) + '-' + (day < 10 ? '0' + day : day);
  }

  function buildTripPlan(args, startDate) {
    var raw = args.stops || args.places || [];
    var stops = [];
    for (var i = 0; i < raw.length; i++) {
      var name = String(raw[i] || '').trim();
      if (name) stops.push({ name: name, time: '', note: '' });
    }
    var count = parseInt(args.days, 10);
    if (!count || count < 1) count = 1;
    if (count > 10) count = 10;
    if (stops.length && count > stops.length) count = stops.length;
    var days = [];
    var per = stops.length ? Math.floor(stops.length / count) : 0;
    var extra = stops.length ? stops.length % count : 0;
    var cursor = 0;
    for (var d = 0; d < count; d++) {
      var take = per + (d < extra ? 1 : 0);
      days.push({
        date: startDate ? addDays(startDate, d) : '',
        title: '',
        stops: stops.slice(cursor, cursor + take)
      });
      cursor += take;
    }
    return {
      title: (args.city ? args.city : '我的') + '行程',
      city: args.city || '',
      origin: args.origin || args.start || '',
      mode: args.mode || 'driving',
      start_date: startDate || '',
      days: days
    };
  }

  function postDownload(path, payload, filename, okText) {
    toast('正在生成…');
    var xhr = new XMLHttpRequest();
    xhr.open('POST', API_BASE + path, true);
    xhr.responseType = 'blob';
    xhr.timeout = 120000;
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.setRequestHeader('X-Device-ID', deviceId());
    xhr.onload = function () {
      if (xhr.status >= 200 && xhr.status < 300) {
        var url = URL.createObjectURL(xhr.response);
        var link = document.createElement('a');
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        setTimeout(function () { URL.revokeObjectURL(url); }, 5000);
        toast(okText);
      } else {
        toast('导出失败（HTTP ' + xhr.status + '）');
      }
    };
    xhr.onerror = function () { toast('导出失败，请检查网络'); };
    xhr.ontimeout = function () { toast('导出超时，请稍后重试'); };
    xhr.send(JSON.stringify(payload));
  }

  function exportTripPdf(payload) {
    postDownload('/trips/export', payload, payload.title + '.pdf', 'PDF 已开始下载');
  }

  function exportTripIcs(payload) {
    postDownload('/trips/export/ics', payload, payload.title + '.ics', '日历已开始下载');
  }

  function closeTripExport() {
    var box = $('#tripExport');
    if (box && box.parentNode) box.parentNode.removeChild(box);
  }

  function openTripExport(args) {
    closeTripExport();
    var box = document.createElement('div');
    box.className = 'trip-export';
    box.id = 'tripExport';
    box.innerHTML =
      '<div class="te-panel">' +
        '<button type="button" class="te-close" aria-label="关闭">&times;</button>' +
        '<p class="te-kicker">EXPORT</p>' +
        '<h3>导出行程</h3>' +
        '<label class="te-label">出发日期<input type="date" id="teDate" value="' + isoDate(1) + '"></label>' +
        '<div class="te-actions">' +
          '<button type="button" class="te-primary" id="tePdf">下载 PDF（含每日地图）</button>' +
          '<button type="button" id="teIcs">下载日历 .ics</button>' +
          '<button type="button" id="teShare">生成只读分享链接</button>' +
        '</div>' +
        '<div class="te-result" id="teResult"></div>' +
      '</div>';
    document.body.appendChild(box);

    box.onclick = function (e) {
      if (e.target === box || e.target.className === 'te-close') closeTripExport();
    };

    function currentPlan() {
      var input = $('#teDate', box);
      return buildTripPlan(args, input ? input.value : '');
    }

    $('#tePdf', box).onclick = function () { exportTripPdf(currentPlan()); };
    $('#teIcs', box).onclick = function () { exportTripIcs(currentPlan()); };
    $('#teShare', box).onclick = function () {
      var payload = currentPlan();
      var result = $('#teResult', box);
      result.innerHTML = '<p class="te-hint">正在生成地图与分享页…</p>';
      request('POST', '/trips/share', payload, function (err, data) {
        if (err) {
          result.innerHTML = '<p class="te-hint">分享生成失败，请稍后重试</p>';
          return;
        }
        var url = location.origin + (data && data.path ? data.path : '');
        result.innerHTML =
          '<input type="text" readonly id="teUrl" value="' + esc(url) + '">' +
          '<button type="button" id="teCopy">复制链接</button>' +
          '<p class="te-hint">有效期至 ' + esc((data && data.expires_at) || '') + '，拿到链接即可查看</p>';
        var input = $('#teUrl', result);
        if (input) {
          input.onclick = function () { input.select(); };
        }
        var copy = $('#teCopy', result);
        if (copy) {
          copy.onclick = function () {
            try {
              if (input) { input.select(); document.execCommand('copy'); }
              toast('链接已复制');
            } catch (e) {
              toast('请手动复制链接');
            }
          };
        }
      });
    };
  }

  /* 页面内大图查看层，不跳转、不打开本地文件 */
  function openLightbox(src) {
    closeLightbox();
    var box = document.createElement('div');
    box.className = 'lightbox';
    box.id = 'lightbox';
    box.innerHTML =
      '<button type="button" class="lb-close" aria-label="关闭">&times;</button>' +
      '<img alt="生成的旅行图片" src="' + esc(src) + '">';
    document.body.appendChild(box);
    box.onclick = function (e) {
      if (e.target === box || e.target.className === 'lb-close') closeLightbox();
    };
    document.addEventListener('keydown', lightboxEsc, false);
  }

  function closeLightbox() {
    var box = $('#lightbox');
    if (box && box.parentNode) box.parentNode.removeChild(box);
    document.removeEventListener('keydown', lightboxEsc, false);
  }

  function lightboxEsc(e) {
    if (e.keyCode === 27 || e.key === 'Escape') closeLightbox();
  }

  /* 对话内卡片的轮询：容器离开 DOM（切会话）时自动停止 */
  function pollInline(container, taskId, gap) {
    if (!document.body.contains(container)) return;
    request('GET', '/media/tasks/' + encodeURIComponent(taskId), null, function (err, data) {
      if (!document.body.contains(container)) return;
      var stateEl = container.querySelector('.mt-state');
      var frame = container.querySelector('.mt-frame');
      var actions = container.querySelector('.mt-actions');

      if (err) {
        if (err.code === 'TASK_NOT_FOUND') {
          /* 任务内存已过期，但 media/ 里可能还有本地成品，仍尝试在页面内显示 */
          renderInlineMedia(container, taskId, '任务已过期，尝试读取本地成品');
        } else {
          if (stateEl) stateEl.innerHTML = '查询失败，稍后重试';
          setTimeout(function () { pollInline(container, taskId, 5000); }, 5000);
        }
        return;
      }

      if (data.status === 'succeeded') {
        renderInlineMedia(container, taskId, '已完成');
        return;
      }

      if (data.status === 'failed') {
        if (stateEl) stateEl.innerHTML = '生成失败';
        if (frame) frame.className = 'mt-frame is-done';
        if (actions) actions.innerHTML = '<div class="mt-error">' + esc(data.error || '生成失败') + '</div>';
        return;
      }

      if (stateEl) stateEl.innerHTML = data.status === 'running' ? '正在生成…' : '排队中…';
      setTimeout(function () { pollInline(container, taskId, Math.min(gap + 800, 5000)); }, gap);
    });
  }

  /* 统一渲染对话内成品图：图片、页面内放大、直接下载 */
  function renderInlineMedia(container, taskId, label) {
    var stateEl = container.querySelector('.mt-state');
    var frame = container.querySelector('.mt-frame');
    var actions = container.querySelector('.mt-actions');
    var src = mediaImageUrl(taskId, false);
    if (stateEl && label) stateEl.innerHTML = esc(label);
    if (!frame) return;
    frame.className = 'mt-frame is-done';
    frame.innerHTML = '<img alt="生成的旅行图片" src="' + esc(src) + '">';
    var img = frame.querySelector('img');
    if (img) {
      img.onclick = function () { openLightbox(src); };
      img.onload = function () {
        if (stateEl && stateEl.innerHTML.indexOf('任务已过期') === 0) {
          stateEl.innerHTML = '已完成（本地成品）';
        }
      };
      img.onerror = function () {
        if (stateEl) stateEl.innerHTML = '任务不存在或已过期';
        frame.innerHTML = '<div style="padding:22px;font-size:12px;color:rgba(20,36,60,.55)">图片不可用，链接可能已过期</div>';
        if (actions) actions.innerHTML = '';
      };
    }
    if (!actions) return;
    actions.innerHTML =
      '<a class="primary" href="' + esc(mediaImageUrl(taskId, true)) + '" download="travel-' + esc(taskId.slice(0, 8)) + '.jpg">保存图片</a>' +
      '<button type="button" class="js-view">页面内查看大图</button>';
    var view = actions.querySelector('.js-view');
    if (view) view.onclick = function () { openLightbox(src); };
  }

  /* ================= SSE 发送 ================= */

  function setStreaming(on) {
    state.streaming = on;
    refreshComposer();
  }

  function refreshComposer() {
    var btn = DOM.sendBtn;
    var input = DOM.input;
    if (!btn || !input) return;
    if (state.streaming) {
      btn.className = 'send is-stop';
      btn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2"/></svg>';
      btn.setAttribute('aria-label', '停止生成');
      input.setAttribute('readonly', 'readonly');
      input.setAttribute('placeholder', '正在思考…');
    } else {
      btn.className = 'send';
      btn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12h15M13 6l6 6-6 6"/></svg>';
      btn.setAttribute('aria-label', '发送');
      input.removeAttribute('readonly');
      input.setAttribute('placeholder', personaEmpty().placeholder);
      var has = !!(state.caps && state.caps.image_gen);
      if (DOM.composerMedia) DOM.composerMedia.hidden = !has;
      if (DOM.composerAttach) DOM.composerAttach.hidden = !visionReady();
    }
    checkSendable();
  }

  function sendMessage(text) {
    text = trim(text);
    var ids = pendingIds();
    if ((!text && !ids.length) || state.streaming) return;
    if (attachmentsBusy()) { toast('图片还在上传，稍等一下'); return; }
    ensureSession(function (err, id) {
      if (err) { toast(err.message); return; }
      if (DOM.messages && DOM.messages.querySelector('.empty')) DOM.messages.innerHTML = '';
      appendUser(text, ids);
      stickNow();
      var meta = null;
      for (var i = 0; i < state.sessions.length; i++) {
        if (state.sessions[i].id === id) { meta = state.sessions[i]; break; }
      }
      var label = text || (ids.length > 1 ? '[' + ids.length + ' 张图]' : '[图片]');
      if (meta && (meta.title === '新的旅程' || !meta.title)) {
        var title = label.length > 20 ? label.slice(0, 20) + '…' : label;
        meta.title = title;
        setChatTitle(title);
      } else if (!meta) {
        setChatTitle(label.length > 20 ? label.slice(0, 20) + '…' : label);
      }
      upsertSession(id, meta ? meta.title : (label.length > 20 ? label.slice(0, 20) + '…' : label), '');
      startStream(id, text, ids);
      DOM.input.value = '';
      clearAttachments();
      resizeInput();
    });
  }

  function startStream(sessionId, text, attachments) {
    var shell = createAssistantShell();
    var buffer = '';
    var consumed = 0;
    var gotText = '';
    var renderTimer = null;
    var done = false;
    var errored = null;
    var finishReason = '';
    var cards = {};
    var watch = null;
    /* 本轮用量与耗时：end 事件带回，回答结束后显示在正文下方 */
    var turnUsage = null;
    var latencyMs = 0;
    /* 断线续传状态：stream id、最后收到的事件序号、重试次数 */
    var streamId = '';
    var lastEventId = 0;
    var retries = 0;
    var userStopped = false;
    var activeXhr = null;

    setStreaming(true);

    var xhr = new XMLHttpRequest();
    state.stream = {
      abort: function () {
        userStopped = true;
        if (streamId) {
          request('POST', '/chat/streams/' + encodeURIComponent(streamId) + '/cancel', {}, function () {});
        }
        try { if (activeXhr) activeXhr.abort(); } catch (e) { /* 忽略 */ }
      }
    };

    function paint() {
      if (renderTimer) return;
      renderTimer = setTimeout(function () {
        renderTimer = null;
        var thinking = shell.md.querySelector('.thinking');
        if (thinking) shell.md.innerHTML = '';
        if (gotText) shell.md.innerHTML = markdown(gotText);
        stickIfNeeded();
      }, 80);
    }

    function dispatchFrame(frame) {
      var lines = frame.split('\n');
      var data = [];
      for (var i = 0; i < lines.length; i++) {
        if (lines[i].indexOf('id:') === 0) {
          var seq = parseInt(lines[i].slice(3).trim(), 10);
          if (!isNaN(seq)) lastEventId = seq;
        } else if (lines[i].indexOf('data:') === 0) {
          data.push(lines[i].slice(5).replace(/^\s/, ''));
        }
      }
      if (!data.length) return;
      var payload = null;
      try { payload = JSON.parse(data.join('\n')); } catch (e) { payload = null; }
      if (!payload) return; /* 单个事件解析失败不中断整条流 */
      handleEvent(payload);
    }

    function handleEvent(ev) {
      if (!ev || !ev.type) return;
      if (ev.type === 'token') {
        gotText += (ev.content || '');
        paint();
      } else if (ev.type === 'tool_call') {
        var card = createToolCard(ev);
        card.setAttribute('data-tool', ev.tool || '');
        cards[ev.call_id] = card;
        shell.tools.appendChild(card);
        stickIfNeeded();
      } else if (ev.type === 'tool_result') {
        updateToolCard(cards[ev.call_id], ev);
      } else if (ev.type === 'permission_request') {
        showPermission(ev);
      } else if (ev.type === 'end') {
        finishReason = ev.finish_reason || 'stop';
        /* 用量与耗时只在 end 事件里给一次，先存着，等 finish() 统一渲染 */
        turnUsage = (ev.usage && ev.usage.turn) || null;
        latencyMs = typeof ev.latency_ms === 'number' ? ev.latency_ms : 0;
      } else if (ev.type === 'error') {
        errored = normalizeError(ev, 0);
      }
    }

    function pump() {
      var all = (activeXhr && activeXhr.responseText) || '';
      if (all.length <= consumed) return;
      var chunk = all.slice(consumed).replace(/\r\n/g, '\n');
      consumed = all.length;
      buffer += chunk;
      var idx;
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        dispatchFrame(buffer.slice(0, idx));
        buffer = buffer.slice(idx + 2);
      }
    }

    function finish(reason) {
      if (done) return;
      done = true;
      if (watch) { clearInterval(watch); watch = null; }
      if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
      setStreaming(false);
      state.stream = null;
      var thinking = shell.md.querySelector('.thinking');
      if (thinking) shell.md.innerHTML = '';
      if (gotText) shell.md.innerHTML = markdown(gotText);

      /* 兜底：若工具摘要被截断，正文里出现 task_id 时同样在对话内出图 */
      if (!errored && gotText && !shell.root.querySelector('.media-inline')) {
        var hasImageCall = false;
        for (var cid in cards) {
          if (Object.prototype.hasOwnProperty.call(cards, cid) && cards[cid].getAttribute('data-tool') === 'generate_image') {
            hasImageCall = true;
            break;
          }
        }
        if (hasImageCall) {
          var tm = extractTaskId(gotText);
          if (tm) {
            var holder = document.createElement('div');
            holder.className = 'media-inline';
            shell.root.insertBefore(holder, shell.md);
            startInlineTask(holder, tm);
          }
        }
      }

      if (errored) {
        appendErrorCard(shell.root, errored, text);
      } else if (reason === 'abort') {
        appendNotice('已停止生成');
      } else if (!gotText) {
        appendErrorCard(shell.root, { code: 'MODEL_EMPTY_RESPONSE', message: ERROR_TEXT.MODEL_EMPTY_RESPONSE, detail: '' }, text);
      }

      if (!errored && finishReason === 'length') {
        appendNotice('回答已达长度上限，可能被截断');
      }
      /* 中止和截断也照样花钱，只要吐过字就显示用量 */
      if (!errored && gotText) {
        appendUsageMeta(shell.root, latencyMs, turnUsage);
      }
      if (!errored && gotText && finishReason !== 'length') {
        var meta = null;
        for (var i = 0; i < state.sessions.length; i++) {
          if (state.sessions[i].id === sessionId) { meta = state.sessions[i]; break; }
        }
        if (meta) { meta.updatedAt = Date.now(); saveSessions(); renderSessions(); }
      }
      stickIfNeeded();

      /* 回答结束后轮询一次偏好候选：命中才会弹确认框，没命中就静默结束 */
      if (!errored && gotText && reason === 'done') {
        window.setTimeout(function () { pollMemorySuggestions(0); }, 1500);
      }
    }

    function bindStream() {
      var target = activeXhr;
      if (!target) return;
      target.onreadystatechange = function () {
        if (target.readyState >= 2) {
          var sid = target.getResponseHeader('X-Stream-ID');
          if (sid) streamId = sid;
        }
      };
      if ('onprogress' in target) {
        target.onprogress = pump;
      } else {
        if (watch) clearInterval(watch);
        watch = setInterval(pump, 250);
      }
      target.onload = function () {
        pump();
        if (watch) { clearInterval(watch); watch = null; }
        if (target.status < 200 || target.status >= 300) {
          var err = null;
          try {
            err = normalizeError(JSON.parse(target.responseText), target.status);
          } catch (e) {
            err = normalizeError(null, target.status);
          }
          errored = err;
          finish('error');
          return;
        }
        if (buffer) dispatchFrame(buffer);
        finish('done');
      };
      target.onerror = function () {
        scheduleReconnect();
      };
      target.ontimeout = function () {
        scheduleReconnect();
      };
      target.onabort = function () {
        if (userStopped) finish('abort');
      };
    }

    /* 断线后按 0.8s / 1.6s / 2.4s 退避重连，短暂断网能自动接上 */
    function scheduleReconnect() {
      if (done || userStopped) return;
      if (!streamId || retries >= 3) {
        errored = normalizeError(null, 0);
        finish('error');
        return;
      }
      retries += 1;
      toast('连接中断，正在恢复…');
      setTimeout(openResume, 800 * retries);
    }

    /* 从 lastEventId 之后重放，再继续接收新事件 */
    function openResume() {
      if (done || userStopped) return;
      if (watch) { clearInterval(watch); watch = null; }
      consumed = 0;
      buffer = '';
      activeXhr = new XMLHttpRequest();
      try {
        activeXhr.open(
          'GET',
          API_BASE + '/chat/streams/' + encodeURIComponent(streamId) +
            '?last_event_id=' + lastEventId,
          true
        );
      } catch (e) {
        errored = normalizeError(null, 0);
        finish('error');
        return;
      }
      activeXhr.setRequestHeader('Accept', 'text/event-stream');
      activeXhr.setRequestHeader('X-Device-ID', deviceId());
      if (lastEventId) {
        activeXhr.setRequestHeader('Last-Event-ID', String(lastEventId));
      }
      activeXhr.timeout = 330000;
      bindStream();
      activeXhr.send();
    }

    activeXhr = xhr;
    try {
      xhr.open('POST', API_BASE + '/chat/sessions/' + encodeURIComponent(sessionId) + '/messages', true);
    } catch (e) {
      finish('error');
      return;
    }
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.setRequestHeader('Accept', 'text/event-stream');
    xhr.setRequestHeader('X-Device-ID', deviceId());
    xhr.timeout = 330000;
    bindStream();

    try {
      xhr.send(JSON.stringify({ message: text, attachments: attachments || [] }));
    } catch (e) {
      errored = normalizeError(null, 0);
      finish('error');
    }
  }

  /* ================= 权限确认 ================= */

  function actionLabel(action) {
    return ACTION_TEXT[action] || action || '系统操作';
  }

  function showPermission(ev) {
    if (state.perm) {
      state.permQueue.push(ev);
      return;
    }
    state.perm = ev;
    DOM.permAction.innerHTML = esc(actionLabel(ev.action));
    var detail = ev.detail || {};
    DOM.permPath.innerHTML = detail.path ? esc(detail.path) : '';
    DOM.permPath.hidden = !detail.path;
    DOM.permPreview.innerHTML = detail.preview ? esc(detail.preview) : '（无预览）';
    DOM.permOverlay.hidden = false;
    startPermissionTimer();
  }

  function startPermissionTimer() {
    var left = 120;
    if (state.permTimer) clearInterval(state.permTimer);
    DOM.permCountdown.innerHTML = String(left);
    state.permTimer = setInterval(function () {
      left -= 1;
      if (left <= 0) {
        clearInterval(state.permTimer);
        state.permTimer = null;
        closePermission();
        toast('授权请求已超时，按拒绝处理');
        nextPermission();
        return;
      }
      DOM.permCountdown.innerHTML = String(left);
    }, 1000);
  }

  function closePermission() {
    DOM.permOverlay.hidden = true;
    if (state.permTimer) clearInterval(state.permTimer);
    state.permTimer = null;
    state.perm = null;
  }

  function nextPermission() {
    if (state.permQueue.length) {
      var next = state.permQueue.shift();
      window.setTimeout(function () { showPermission(next); }, 260);
    }
  }

  function decidePermission(granted, remember) {
    var ev = state.perm;
    if (!ev) return;
    closePermission();
    request('POST', '/permissions/' + encodeURIComponent(ev.request_id) + '/decision', { granted: granted, remember: !!remember }, function (err) {
      if (err && err.code !== 'INVALID_REQUEST') toast(err.message);
    });
    if (granted) toast(remember ? '已允许，本会话内同类操作不再询问' : '已允许本次操作');
    else toast('已拒绝该操作');
    nextPermission();
  }

  /* ================= 弹层 ================= */

  var SHEETS = {};

  function openSheet(type, opts) {
    if (!SHEETS[type]) return;
    state.sheet = type;
    var def = SHEETS[type];
    DOM.sheetKicker.innerHTML = esc(def.kicker || 'OCEAN');
    DOM.sheetTitle.innerHTML = esc(def.title);
    DOM.sheetBody.innerHTML = '';
    DOM.sheetOverlay.hidden = false;
    if (DOM.rail) DOM.rail.className = 'rail panel';
    if (DOM.railScrim) DOM.railScrim.className = 'rail-scrim';
    def.render(DOM.sheetBody, opts || {});
  }

  function closeSheet() {
    if (state.media && state.media.timer) {
      clearTimeout(state.media.timer);
    }
    if (state.sheet === 'media') state.media = null;
    DOM.sheetOverlay.hidden = true;
    state.sheet = '';
  }

  SHEETS.discover = {
    kicker: 'TRAVEL INSPIRATION',
    title: '旅行灵感',
    render: function (body) {
      var items = [
        { title: '三亚 · 五日', meta: 'ISLAND / 预算 8000', text: '大东海落脚，亚龙湾与蜈支洲岛各留一天，留半天空白。', prompt: '帮我规划 5 天三亚行程，预算 8000，住大东海' },
        { title: '杭州 · 周末', meta: 'CITY / 人文散步', text: '西湖以西的茶园与老巷，慢节奏的两天。', prompt: '杭州两日周末行程，喜欢人文和咖啡，预算 2000' },
        { title: '成都 · 美食', meta: 'FOOD / 三日', text: '按片区串起小吃与苍蝇馆子，少走回头路。', prompt: '成都 3 天美食路线，预算 3000，帮我排顺序' },
        { title: '海边打卡照', meta: 'IMAGE / 旅行海报', text: '用真实生图后端生成一张海边打卡照或旅行海报。', prompt: '生成一张三亚海边旅行打卡照，模板 checkin' }
      ];
      var html = ['<div class="insp-grid">'];
      for (var i = 0; i < items.length; i++) {
        html.push(
          '<button type="button" class="insp-card" data-prompt="' + esc(items[i].prompt) + '">' +
            '<span class="insp-title">' + esc(items[i].title) +
              '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 11l6-6M6 5h5v5"/></svg>' +
            '</span>' +
            '<span class="insp-meta">' + esc(items[i].meta) + '</span>' +
            '<span class="insp-text">' + esc(items[i].text) + '</span>' +
          '</button>'
        );
      }
      html.push('</div><p class="sheet-note">点击任意卡片，需求会填入输入框，可先修改再发送。</p>');
      body.innerHTML = html.join('');
      body.onclick = function (e) {
        var target = e.target;
        while (target && target !== body && !target.getAttribute('data-prompt')) target = target.parentNode;
        if (!target || target === body) return;
        var prompt = target.getAttribute('data-prompt');
        closeSheet();
        if (!isAssistant()) openAssistant();
        DOM.input.value = prompt;
        resizeInput();
        try { DOM.input.focus(); } catch (err) { /* 忽略 */ }
      };
    }
  };

  SHEETS.story = {
    kicker: 'OUR STORY',
    title: '关于 Ocean',
    render: function (body) {
      body.innerHTML =
        '<div class="story">' +
          '<p>Ocean 是一个会真正动手的旅行助手：它不只给建议，还会按你的问题去查天气、搜地点、算路线、比住宿、估算预算，并把每一步过程摊开给你看。</p>' +
          '<h4>它真的在调用工具</h4>' +
          '<p>对话里出现的半透明卡片就是一次真实工具调用：查天气、检索地点、规划路线、网页检索、解析分享链接、读写沙箱文件、生成旅行图片。数据全部来自工具返回，不来自模型的记忆。</p>' +
          '<h4>它不会编造</h4>' +
          '<ul>' +
            '<li>拿不到数据时会直接说暂无数据，不给"大概""约"这类数字。</li>' +
            '<li>不提供机票火车票班次、余票与票价，只给官方查询入口。</li>' +
            '<li>住宿只给真实地点与地图链接，不展示实时房价与库存。</li>' +
            '<li>生图链接 24 小时内有效，需要长期保存请及时另存。</li>' +
          '</ul>' +
          '<h4>每一步都可以被拦住</h4>' +
          '<p>写文件、建目录、删除路径、切换桌面壁纸这类有副作用的操作，一定会先弹授权框；120 秒不处理会按拒绝执行。</p>' +
          '<p class="sheet-note">生视频不在产品范围内，界面不会提供入口。</p>' +
        '</div>';
    }
  };

  SHEETS.journal = {
    kicker: 'JOURNAL',
    title: '会话记录',
    render: function (body) {
      var html = ['<div class="row" style="margin-bottom:14px"><button type="button" class="btn-primary small" id="journalNew">开始新的旅程</button></div>'];
      if (!state.sessions.length) {
        html.push('<p class="sheet-note">还没有会话。点上面的按钮开始第一段旅程。</p>');
      } else {
        html.push('<div class="insp-grid">');
        for (var i = 0; i < state.sessions.length; i++) {
          var s = state.sessions[i];
          html.push(
            '<button type="button" class="insp-card" data-use="' + esc(s.id) + '">' +
              '<span class="insp-title">' + esc(s.title || '新的旅程') +
                '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 11l6-6M6 5h5v5"/></svg>' +
              '</span>' +
              '<span class="insp-meta">' + esc(timeText(s.updatedAt)) + '</span>' +
              '<span class="insp-text">继续这段对话</span>' +
            '</button>'
          );
        }
        html.push('</div>');
      }
      body.innerHTML = html.join('');
      var newBtn = $('#journalNew', body);
      if (newBtn) newBtn.onclick = function () { closeSheet(); newChat(); };
      body.onclick = function (e) {
        var target = e.target;
        while (target && target !== body && !target.getAttribute('data-use')) target = target.parentNode;
        if (!target || target === body) return;
        var id = target.getAttribute('data-use');
        closeSheet();
        if (!isAssistant()) openAssistant();
        useSession(id);
      };
    }
  };

  SHEETS.capabilities = {
    kicker: 'CAPABILITIES',
    title: '能力与数据来源',
    render: function (body) {
      var caps = state.caps;
      var html = [];
      if (!caps) {
        html.push('<p class="sheet-note">尚未连接后端，无法读取能力清单。</p>');
        html.push('<div class="row"><button type="button" class="btn-primary small" id="capRetry">重新检测</button></div>');
        body.innerHTML = html.join('');
        $('#capRetry', body).onclick = function () {
          loadCapabilities(function () { loadTools(); closeSheet(); });
        };
        return;
      }

      html.push('<div class="cap-group"><h4>数据来源</h4><div class="row">');
      var platforms = caps.platforms || [];
      if (!platforms.length) html.push('<span class="chip">暂无可用数据源</span>');
      for (var i = 0; i < platforms.length; i++) {
        html.push('<span class="chip">' + esc(platformLabel(platforms[i])) + '</span>');
      }
      html.push('</div><p class="sheet-note">来源清单由后端运行时返回，前端不硬编码。</p></div>');

      html.push('<div class="cap-group"><h4>能力开关</h4>');
      for (var j = 0; j < CAP_TEXT.length; j++) {
        var cap = CAP_TEXT[j];
        var on = !!caps[cap.key];
        html.push(
          '<div class="cap-row"><span class="cap-name">' + esc(cap.label) + '</span>' +
            '<span class="badge ' + (on ? 'ok' : 'off') + '">' + (on ? '可用' : '未配置') + '</span></div>'
        );
      }
      html.push('</div>');

      html.push('<div class="cap-group"><h4>工具清单</h4>');
      if (!state.toolList.length) {
        html.push('<p class="sheet-note">工具清单未加载，可点下方重新检测。</p>');
      } else {
        for (var k = 0; k < state.toolList.length; k++) {
          var tool = state.toolList[k];
          var badge = '';
          if (!tool.enabled) badge = '<span class="badge off">未启用</span>';
          else if (tool.requires_permission) badge = '<span class="badge warn">需授权</span>';
          else badge = '<span class="badge ok">可用</span>';
          html.push(
            '<div class="cap-row"><span class="cap-name">' + esc(tool.description.split('。')[0].replace(/\*\*/g, '')) +
              '<span class="cap-desc">' + esc(CAT_TEXT[tool.category] || '') + '</span></span>' + badge + '</div>'
          );
        }
      }
      html.push('</div>');
      html.push('<div class="row"><button type="button" class="btn-quiet" id="capRetry2">重新检测</button></div>');
      body.innerHTML = html.join('');
      $('#capRetry2', body).onclick = function () {
        loadCapabilities(function () { loadTools(function () { openSheet('capabilities'); }); });
      };
    }
  };

  SHEETS.menu = {
    kicker: 'OCEAN',
    title: '菜单',
    render: function (body) {
      body.innerHTML =
        '<div class="insp-grid">' +
          '<button type="button" class="insp-card" data-go="discover"><span class="insp-title">Discover · 旅行灵感</span><span class="insp-text">挑一个目的地开场</span></button>' +
          '<button type="button" class="insp-card" data-go="story"><span class="insp-title">Our story · 关于</span><span class="insp-text">它为什么不会编造</span></button>' +
          '<button type="button" class="insp-card" data-go="journal"><span class="insp-title">Journal · 会话记录</span><span class="insp-text">继续之前的旅程</span></button>' +
          '<button type="button" class="insp-card" data-go="settings"><span class="insp-title">设置 · 壁纸与接口</span><span class="insp-text">换壁纸、改接口地址</span></button>' +
        '</div>';
      body.onclick = function (e) {
        var target = e.target;
        while (target && target !== body && !target.getAttribute('data-go')) target = target.parentNode;
        if (!target || target === body) return;
        openSheet(target.getAttribute('data-go'));
      };
    }
  };

  /* ================= 生图 ================= */

  function renderMedia(body, opts) {
    body.innerHTML =
      '<p class="sheet-note warn">图片链接为上游临时地址，24 小时内失效，需要长期保存请及时另存。</p>' +
      '<div class="field"><span class="field-label">风格模板</span><div class="tpl-grid" id="tplGrid"><span class="sheet-note">正在加载模板…</span></div></div>' +
      '<div class="field"><span class="field-label">画面描述</span>' +
        '<textarea id="mediaPrompt" placeholder="例如：三亚亚龙湾海边栈道打卡照，远处海面泛光，旅行博主视角"></textarea>' +
      '</div>' +
      '<div class="field"><span class="field-label">已有任务 ID</span>' +
        '<div class="inline-field">' +
          '<input type="text" id="taskIdInput" placeholder="粘贴 task_id，直接取回已提交的图片">' +
          '<button type="button" class="btn-quiet" id="taskLookup">查询</button>' +
        '</div>' +
      '</div>' +
      '<div class="row end"><button type="button" class="btn-primary small" id="mediaGo">生成图片</button></div>' +
      '<div id="mediaTaskArea"></div>';

    state.media = { style: '', prompt: '', taskId: opts.taskId || '', timer: null, gap: 2000 };

    var grid = $('#tplGrid', body);
    request('GET', '/media/templates', null, function (err, data) {
      if (err) {
        grid.innerHTML = '<span class="sheet-note">模板加载失败：' + esc(err.message) + '</span>';
        return;
      }
      var items = (data && data.items) || [];
      var html = [];
      for (var i = 0; i < items.length; i++) {
        html.push(
          '<button type="button" class="tpl" data-style="' + esc(items[i].key) + '">' +
            '<b>' + esc(items[i].label) + '</b>' +
            '<span>' + esc(items[i].scene) + '</span>' +
          '</button>'
        );
      }
      grid.innerHTML = html.join('');
      grid.onclick = function (e) {
        var target = e.target;
        while (target && target !== grid && !target.getAttribute('data-style')) target = target.parentNode;
        if (!target || target === grid) return;
        var cls = target.className;
        var isActive = cls.indexOf('is-active') >= 0;
        var all = grid.querySelectorAll('.tpl');
        for (var k = 0; k < all.length; k++) all[k].className = 'tpl';
        if (isActive) { state.media.style = ''; return; }
        target.className = 'tpl is-active';
        state.media.style = target.getAttribute('data-style');
      };
    });

    var go = $('#mediaGo', body);
    go.onclick = submitMedia;

    var taskInput = $('#taskIdInput', body);
    if (opts.taskId) taskInput.value = opts.taskId;
    function lookupTask() {
      var id = trim(taskInput.value);
      if (!/^[0-9a-fA-F\-]{36}$/.test(id)) { toast('请输入 36 位 task_id'); return; }
      state.media.taskId = id;
      renderTaskCard(id);
      pollTask(id);
    }
    $('#taskLookup', body).onclick = lookupTask;
    taskInput.onkeydown = function (e) {
      if (e.keyCode === 13) {
        if (e.preventDefault) e.preventDefault();
        lookupTask();
      }
    };

    if (opts.taskId) {
      renderTaskCard(opts.taskId);
      pollTask(opts.taskId);
    }
  }

  function submitMedia() {
    var promptEl = $('#mediaPrompt');
    var prompt = trim(promptEl ? promptEl.value : '');
    if (!prompt) { toast('请先填写画面描述'); return; }
    if (!state.caps || !state.caps.image_gen) { toast(ERROR_TEXT.MEDIA_BACKEND_MISSING); return; }
    state.media.prompt = prompt;
    var go = $('#mediaGo');
    if (go) { go.disabled = true; go.innerHTML = '提交中…'; }
    request('POST', '/media/images', { prompt: prompt, style: state.media.style || null, ref_image: null }, function (err, data) {
      if (go) { go.disabled = false; go.innerHTML = '生成图片'; }
      if (err) {
        var area = $('#mediaTaskArea');
        area.innerHTML = '<div class="media-task"><div class="mt-head"><span class="tool-dot"></span><span>' + esc(err.message) + '</span></div></div>';
        return;
      }
      state.media.taskId = data.task_id;
      var taskInput = $('#taskIdInput');
      if (taskInput) taskInput.value = data.task_id;
      renderTaskCard(data.task_id);
      pollTask(data.task_id);
    });
  }

  function renderTaskCard(taskId) {
    var area = $('#mediaTaskArea');
    if (!area) return;
    area.innerHTML =
      '<div class="media-task" id="mtCard">' +
        '<div class="mt-head"><span class="tool-dot"></span><span class="mt-state">生成中…</span><span class="mt-gap"></span>' +
          '<span class="mt-id">' + esc(taskId.slice(0, 8)) + '</span></div>' +
        '<div class="mt-body">' +
          '<div class="mt-frame" id="mtFrame"></div>' +
          '<div class="mt-actions" id="mtActions"></div>' +
        '</div>' +
      '</div>';
  }

  function setTaskState(text) {
    var el = $('#mtCard .mt-state');
    if (el) el.innerHTML = esc(text);
  }

  function pollTask(taskId) {
    if (!state.media) return;
    if (state.media.timer) clearTimeout(state.media.timer);
    request('GET', '/media/tasks/' + encodeURIComponent(taskId), null, function (err, data) {
      if (!state.media) return;
      if (err) {
        if (err.code === 'TASK_NOT_FOUND') {
          setTaskState('任务不存在或已过期');
          var frame = $('#mtFrame');
          if (frame) frame.className = 'mt-frame is-done';
        } else {
          setTaskState('查询失败，稍后重试');
          state.media.timer = setTimeout(function () { pollTask(taskId); }, 5000);
        }
        return;
      }
      if (data.status === 'succeeded') {
        setTaskState('已完成');
        showTaskImage(taskId);
      } else if (data.status === 'failed') {
        setTaskState('生成失败');
        var box = $('#mtActions');
        if (box) {
          box.innerHTML = '<div class="mt-error">' + esc(data.error || '生成失败') + '</div>' +
            '<button type="button" id="mtRetry">重新生成</button>';
          $('#mtRetry', box).onclick = submitMedia;
        }
        var f2 = $('#mtFrame');
        if (f2) f2.className = 'mt-frame is-done';
      } else {
        setTaskState(data.status === 'running' ? '正在生成…' : '排队中…');
        state.media.gap = Math.min((state.media.gap || 2000) + 800, 5000);
        state.media.timer = setTimeout(function () { pollTask(taskId); }, state.media.gap);
      }
    });
  }

  function showTaskImage(taskId) {
    var frame = $('#mtFrame');
    var actions = $('#mtActions');
    if (!frame || !actions) return;
    var src = mediaImageUrl(taskId, false);
    frame.className = 'mt-frame is-done';
    frame.innerHTML = '<img alt="生成的旅行图片" src="' + esc(src) + '">';
    var img = frame.querySelector('img');
    if (img) {
      img.onclick = function () { openLightbox(src); };
      img.onerror = function () {
        frame.innerHTML = '<div style="padding:22px;font-size:12px;color:rgba(20,36,60,.55)">图片加载失败，可稍后重试</div>';
      };
    }
    actions.innerHTML =
      '<a class="primary" href="' + esc(mediaImageUrl(taskId, true)) + '" download="travel-' + esc(taskId.slice(0, 8)) + '.jpg">保存图片</a>' +
      '<button type="button" id="mtView">页面内查看大图</button>' +
      '<button type="button" id="mtAgain">重新生成</button>';
    var view = $('#mtView', actions);
    if (view) view.onclick = function () { openLightbox(src); };
    var again = $('#mtAgain', actions);
    if (again) again.onclick = submitMedia;
  }

  /* ================= 用户偏好（长期记忆） ================= */

  /* 抽取结果只作为候选，用户确认后才写入，符合"有副作用先确认"的约定 */
  function showMemorySuggest(items) {
    if (!items || !items.length) return;
    if (document.getElementById('memOverlay')) return;
    var host = DOM.app || document.body;
    var overlay = document.createElement('div');
    overlay.className = 'overlay';
    overlay.id = 'memOverlay';
    overlay.innerHTML =
      '<div class="scrim" data-mem-close="1"></div>' +
      '<section class="modal" role="dialog" aria-modal="true">' +
        '<p class="modal-kicker">MEMORY</p>' +
        '<h3 class="modal-title">要记住这些偏好吗？</h3>' +
        '<p class="sheet-note">确认后用于以后的行程建议，可随时在「设置 · 我的偏好」里删除。</p>' +
        '<div class="mem-list" id="memSuggestList"></div>' +
        '<div class="perm-actions">' +
          '<button class="btn-quiet" type="button" id="memDismiss">忽略</button>' +
          '<button class="btn-primary" type="button" id="memSave">保存所选</button>' +
        '</div>' +
      '</section>';
    host.appendChild(overlay);

    var list = overlay.querySelector('#memSuggestList');
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      var label = document.createElement('label');
      label.className = 'mem-item';
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = true;
      box.setAttribute('data-cat', item.category || '其他');
      box.setAttribute('data-text', item.text || '');
      var text = document.createElement('span');
      text.innerHTML = '<b>' + esc(item.category || '其他') + '</b>' + esc(item.text || '');
      label.appendChild(box);
      label.appendChild(text);
      list.appendChild(label);
    }

    function close() {
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    }

    overlay.querySelector('#memDismiss').onclick = function () {
      request('DELETE', '/memory/suggestions', null, function () {});
      close();
      toast('已忽略这些偏好');
    };

    overlay.querySelector('#memSave').onclick = function () {
      var boxes = list.querySelectorAll('input[type=checkbox]');
      var chosen = [];
      for (var k = 0; k < boxes.length; k++) {
        if (!boxes[k].checked) continue;
        chosen.push({
          category: boxes[k].getAttribute('data-cat'),
          text: boxes[k].getAttribute('data-text')
        });
      }
      close();
      if (!chosen.length) return;
      request('POST', '/memory/items', { items: chosen }, function (err) {
        if (err) { toast(err.message); return; }
        toast('已记住 ' + chosen.length + ' 条偏好');
      });
    };

    overlay.querySelector('[data-mem-close]').onclick = function () { close(); };
  }

  /* 抽取在回答落库后异步跑，最多轮询 4 次（约 10 秒），避免空等 */
  function pollMemorySuggestions(attempt) {
    if (attempt > 3) return;
    request('GET', '/memory/suggestions', null, function (err, data) {
      if (err) return;
      var items = (data && data.items) || [];
      if (items.length) { showMemorySuggest(items); return; }
      window.setTimeout(function () { pollMemorySuggestions(attempt + 1); }, 3000);
    });
  }

  function loadMemoryList(body) {
    var box = $('#memList', body);
    if (!box) return;
    request('GET', '/memory', null, function (err, data) {
      if (!box.parentNode) return;
      if (err) {
        box.innerHTML = '<p class="sheet-note" style="margin:0">' + esc(err.message) + '</p>';
        return;
      }
      var items = (data && data.items) || [];
      if (!items.length) {
        box.innerHTML = '<p class="sheet-note" style="margin:0">还没有记录任何偏好。</p>';
        return;
      }
      var html = [];
      for (var i = 0; i < items.length; i++) {
        html.push(
          '<div class="mem-item"><span><b>' + esc(items[i].category || '其他') + '</b>' +
            esc(items[i].text || '') + '</span>' +
            '<button type="button" class="btn-quiet danger" data-mem-del="' +
              esc(items[i].id) + '">删除</button></div>'
        );
      }
      box.innerHTML = html.join('');
      box.onclick = function (e) {
        var target = e.target;
        while (target && target !== box && !target.getAttribute('data-mem-del')) {
          target = target.parentNode;
        }
        if (!target || target === box) return;
        var id = target.getAttribute('data-mem-del');
        request('DELETE', '/memory/items/' + encodeURIComponent(id), null, function (err2) {
          if (err2) { toast(err2.message); return; }
          toast('已删除');
          loadMemoryList(body);
        });
      };
    });
  }

  /* ================= 设置与壁纸 ================= */

  function wallpaperValue() {
    return lsGet(STORE.wallpaper, 'preset:sea');
  }

  function setTone(dark) {
    var cls = DOM.app.className;
    var has = cls.indexOf('is-dark') >= 0;
    if (dark && !has) DOM.app.className = cls + ' is-dark';
    else if (!dark && has) DOM.app.className = cls.replace(' is-dark', '');
  }

  /* 读取壁纸左中部的平均亮度，决定首屏文字用深色还是浅色 */
  function detectTone(src) {
    var probe = new Image();
    probe.onload = function () {
      try {
        var canvas = document.createElement('canvas');
        canvas.width = 36;
        canvas.height = 20;
        var ctx = canvas.getContext('2d');
        if (!ctx) { setTone(false); return; }
        ctx.drawImage(probe, 0, 0, 36, 20);
        var data = ctx.getImageData(0, 0, 36, 20).data;
        var sum = 0;
        var count = 0;
        for (var i = 0; i < data.length; i += 4) {
          var px = (i / 4) % 36;
          var py = Math.floor((i / 4) / 36);
          if (px > 22 || py < 5 || py > 15) continue;
          sum += 0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2];
          count += 1;
        }
        setTone(count ? (sum / count) < 112 : false);
      } catch (e) {
        setTone(false);
      }
    };
    probe.onerror = function () { setTone(false); };
    probe.src = src;
  }

  function applyWallpaper(value, instant) {
    var img = DOM.wallpaperImg;
    if (!img) return;
    var url = '';
    if (value.indexOf('preset:') === 0) {
      var key = value.slice(7);
      for (var i = 0; i < WALLPAPERS.length; i++) {
        if (WALLPAPERS[i].key === key) { url = WALLPAPERS[i].url; break; }
      }
    } else {
      url = value;
    }
    if (!url) return;
    img.className = instant ? '' : 'is-out';
    var loader = new Image();
    loader.onload = function () {
      img.src = url;
      img.className = '';
      detectTone(url);
    };
    loader.onerror = function () {
      img.className = 'is-out'; /* 加载失败保留 CSS 渐变兜底 */
    };
    loader.src = url;
  }

  function renderSettings(body) {
    var current = wallpaperValue();
    var html = [];
    html.push('<div class="field"><span class="field-label">壁纸</span><div class="wall-grid" id="wallGrid">');
    for (var i = 0; i < WALLPAPERS.length; i++) {
      var wp = WALLPAPERS[i];
      html.push(
        '<button type="button" class="wall-thumb' + (current === 'preset:' + wp.key ? ' is-active' : '') + '" data-wall="preset:' + wp.key + '">' +
          '<img alt="' + esc(wp.label) + '" src="' + esc(wp.url) + '">' +
          '<span>' + esc(wp.label) + '</span>' +
        '</button>'
      );
    }
    html.push('</div>');
    html.push('<div class="upload-row">' +
      '<button type="button" class="btn-quiet" id="wallUploadBtn">选择本地图片</button>' +
      '<input type="file" id="wallFile" accept="image/*" hidden>' +
      '<span class="sheet-note" style="margin:0">建议横图，2MB 以内；图片只保存在本机浏览器。</span>' +
    '</div></div>');

    html.push('<div class="field"><span class="field-label">自定义图片链接</span>' +
      '<input type="url" id="wallUrl" placeholder="https://…（填入横版图片地址）">' +
      '<div class="row" style="margin-top:10px"><button type="button" class="btn-quiet" id="wallUrlApply">应用</button></div>' +
    '</div>');

    html.push('<div class="field"><span class="field-label">后端接口地址</span>' +
      '<input type="url" id="apiBase" value="' + esc(lsGet(STORE.api, '')) + '" placeholder="' + esc(API_BASE) + '">' +
      '<div class="row" style="margin-top:10px">' +
        '<button type="button" class="btn-quiet" id="apiSave">保存并重连</button>' +
        '<button type="button" class="btn-quiet" id="apiReset">恢复默认</button>' +
      '</div>' +
      '<p class="sheet-note" style="margin-top:10px">当前生效：' + esc(API_BASE) + '。用 serve.py 打开页面时走同源代理，换端口也不用改这里；只有用别的静态服务器打开才需要手填后端地址。</p>' +
    '</div>');

    html.push('<div class="field"><span class="field-label">我的偏好</span>' +
      '<div class="mem-list" id="memList"><p class="sheet-note" style="margin:0">加载中…</p></div>' +
      '<div class="row" style="margin-top:10px">' +
        '<button type="button" class="btn-quiet danger" id="memClear">清空全部偏好</button>' +
      '</div>' +
      '<p class="sheet-note" style="margin-top:10px">助手会根据你说过的喜好给出保存建议，只有你确认后才会记录；删除后立即失效。</p>' +
    '</div>');

    html.push('<div class="field"><span class="field-label">本地数据</span>' +
      '<div class="row"><button type="button" class="btn-quiet danger" id="clearLocal">清空本机会话列表</button></div>' +
      '<p class="sheet-note" style="margin-top:10px">只清理浏览器里的会话目录，不影响后端已保存的会话。</p>' +
    '</div>');

    body.innerHTML = html.join('');

    var grid = $('#wallGrid', body);
    grid.onclick = function (e) {
      var target = e.target;
      while (target && target !== grid && !target.getAttribute('data-wall')) target = target.parentNode;
      if (!target || target === grid) return;
      var value = target.getAttribute('data-wall');
      lsSet(STORE.wallpaper, value);
      applyWallpaper(value);
      var all = grid.querySelectorAll('.wall-thumb');
      for (var k = 0; k < all.length; k++) all[k].className = 'wall-thumb';
      target.className = 'wall-thumb is-active';
      toast('壁纸已更换');
    };

    $('#wallUploadBtn', body).onclick = function () { $('#wallFile', body).click(); };
    $('#wallFile', body).onchange = function (e) {
      var file = e.target.files && e.target.files[0];
      if (!file) return;
      if (file.size > 3 * 1024 * 1024) { toast('图片过大，请选择 3MB 以内的图片'); return; }
      var reader = new FileReader();
      reader.onload = function () {
        var dataUrl = String(reader.result || '');
        applyWallpaper(dataUrl);
        if (dataUrl.length < 1600000) {
          lsSet(STORE.wallpaper, dataUrl);
          toast('壁纸已更换并保存');
        } else {
          toast('壁纸已临时应用，图片过大未保存');
        }
      };
      reader.readAsDataURL(file);
    };

    $('#wallUrlApply', body).onclick = function () {
      var value = trim($('#wallUrl', body).value);
      if (!/^https?:\/\//i.test(value)) { toast('请填写 http(s) 图片链接'); return; }
      lsSet(STORE.wallpaper, value);
      applyWallpaper(value);
      toast('壁纸已更换');
    };

    $('#apiSave', body).onclick = function () {
      var value = trim($('#apiBase', body).value);
      if (value) lsSet(STORE.api, value); else lsDel(STORE.api);
      toast('接口地址已保存，正在重连');
      window.setTimeout(function () { location.reload(); }, 600);
    };
    $('#apiReset', body).onclick = function () {
      lsDel(STORE.api);
      toast('已恢复默认，正在重连');
      window.setTimeout(function () { location.reload(); }, 600);
    };
    $('#clearLocal', body).onclick = function () {
      lsDel(STORE.sessions);
      lsDel(STORE.active);
      state.sessions = [];
      state.sessionId = '';
      renderSessions();
      setChatTitle('新的旅程');
      toast('本机会话列表已清空');
    };

    $('#memClear', body).onclick = function () {
      request('DELETE', '/memory', null, function (err) {
        if (err) { toast(err.message); return; }
        toast('偏好已清空');
        loadMemoryList(body);
      });
    };
    loadMemoryList(body);
  }

  SHEETS.settings = {
    kicker: 'SETTINGS',
    title: '设置',
    render: renderSettings
  };

  SHEETS.persona = {
    kicker: 'CONVERSATION STYLE',
    title: '对话风格',
    render: function (body) {
      if (!state.personas.length) {
        body.innerHTML = '<p class="sheet-note">暂时取不到风格列表，请检查后端连接。</p>';
        return;
      }
      var html = [
        '<p class="sheet-note">风格决定助手怎么跟你说话。切换只影响之后的回答，已经聊过的内容不会变。</p>',
        '<div class="persona-list">'
      ];
      for (var i = 0; i < state.personas.length; i++) {
        var item = state.personas[i];
        html.push(
          '<button type="button" class="persona-item' + (item.key === state.persona ? ' is-active' : '') +
            '" data-persona="' + esc(item.key) + '">' +
            '<span class="persona-item-main">' +
              '<span class="persona-item-name">' + esc(item.name) + '</span>' +
              '<span class="persona-item-latin">' + esc(item.latin) + '</span>' +
              '<span class="persona-item-text">' + esc(item.tagline) + '</span>' +
            '</span>' +
            '<span class="persona-item-mark"><svg viewBox="0 0 12 12" aria-hidden="true">' +
              '<path d="M2 6.4l2.6 2.6L10 3.4"/></svg></span>' +
          '</button>'
        );
      }
      html.push('</div>');
      body.innerHTML = html.join('');
      body.onclick = function (e) {
        var target = e.target;
        while (target && target !== body && !target.getAttribute('data-persona')) {
          target = target.parentNode;
        }
        if (!target || target === body) return;
        closeSheet();
        switchPersona(target.getAttribute('data-persona'));
      };
    }
  };

  SHEETS.media = {
    kicker: 'IMAGE STUDIO',
    title: '生成旅行图片',
    render: renderMedia
  };

  /* ================= 交互绑定 ================= */

  function resizeInput() {
    var el = DOM.input;
    if (!el) return;
    el.style.height = 'auto';
    var h = el.scrollHeight;
    el.style.height = Math.min(h, 126) + 'px';
  }

  function applyViewport() {
    var h = window.innerHeight;
    var vv = window.visualViewport;
    if (vv) h = vv.height;
    document.documentElement.style.setProperty('--app-h', h + 'px');
    if (vv && vv.offsetTop) {
      DOM.app.style.top = vv.offsetTop + 'px';
    } else if (DOM.app) {
      DOM.app.style.top = '0px';
    }
  }

  function openRail() {
    DOM.rail.className = 'rail panel is-open';
    DOM.railScrim.className = 'rail-scrim is-on';
  }

  function closeRail() {
    if (!DOM.rail) return;
    DOM.rail.className = 'rail panel';
    if (DOM.railScrim) DOM.railScrim.className = 'rail-scrim';
  }

  function bind() {
    DOM.closeBtn.onclick = function () {
      if (isAssistant()) backHome();
      else toggleFocus();
    };
    DOM.brand.onclick = function () {
      if (isAssistant()) backHome();
    };
    DOM.startBtn.onclick = openAssistant;
    DOM.exploreNow.onclick = function () {
      if (isAssistant()) newChat();
      else openAssistant();
    };
    DOM.scrollCue.onclick = openAssistant;
    DOM.filmBtn.onclick = function () { openSheet('discover'); };
    DOM.focusBtn.onclick = toggleFocus;
    DOM.focusExit.onclick = toggleFocus;
    if (DOM.fullBtn) {
      /* iOS Safari 不支持元素级全屏；已经装成独立 App（standalone）时本来就没有地址栏，
         这两种情况都不显示按钮，避免出现点了没反应的入口 */
      var canFullscreen = !!(document.documentElement.requestFullscreen ||
        document.documentElement.webkitRequestFullscreen);
      var standalone = window.matchMedia &&
        window.matchMedia('(display-mode: standalone)').matches;
      if (!canFullscreen || standalone) {
        DOM.fullBtn.style.display = 'none';
      } else {
        DOM.fullBtn.onclick = toggleFullscreen;
      }
    }
    document.addEventListener('fullscreenchange', onFullscreenChange, false);
    document.addEventListener('webkitfullscreenchange', onFullscreenChange, false);

    var navs = DOM.mainNav.querySelectorAll('button');
    for (var i = 0; i < navs.length; i++) {
      navs[i].onclick = function () { openSheet(this.getAttribute('data-sheet')); };
    }
    DOM.menuBtn.onclick = function () { openSheet('menu'); };

    DOM.newSessionBtn.onclick = newChat;
    DOM.railSettings.onclick = function () { openSheet('settings'); };
    DOM.settingsBtn.onclick = function () { openSheet('settings'); };
    DOM.capBtn.onclick = function () { openSheet('capabilities'); };
    DOM.mediaBtn.onclick = function () { openSheet('media'); };
    DOM.composerMedia.onclick = function () { openSheet('media'); };
    if (DOM.composerAttach) DOM.composerAttach.onclick = pickAttachments;
    if (DOM.attachFile) DOM.attachFile.onchange = onFilesPicked;
    if (DOM.attachStrip) DOM.attachStrip.onclick = onAttachStripClick;
    DOM.personaBtn.onclick = function () { openSheet('persona'); };
    DOM.railToggle.onclick = openRail;
    DOM.railScrim.onclick = closeRail;
    DOM.conn.onclick = function () {
      loadCapabilities(function () { loadTools(); });
    };

    DOM.sheetClose.onclick = closeSheet;
    DOM.sheetScrim.onclick = closeSheet;

    DOM.permDeny.onclick = function () { decidePermission(false, false); };
    DOM.permAllow.onclick = function () { decidePermission(true, false); };
    DOM.permRemember.onclick = function () { decidePermission(true, true); };

    DOM.sessionList.onclick = function (e) {
      var target = e.target;
      var del = null;
      while (target && target !== DOM.sessionList) {
        if (target.getAttribute && target.getAttribute('data-del')) { del = target.getAttribute('data-del'); break; }
        target = target.parentNode;
      }
      if (del) { deleteSession(del, e); return; }
      var session = null;
      target = e.target;
      while (target && target !== DOM.sessionList) {
        if (target.getAttribute && target.getAttribute('data-id')) { session = target.getAttribute('data-id'); break; }
        target = target.parentNode;
      }
      if (session) useSession(session);
    };

    DOM.messages.onclick = function (e) {
      var target = e.target;
      while (target && target !== DOM.messages) {
        if (target.getAttribute && target.getAttribute('data-prompt') && target.className.indexOf('sugg') < 0) {
          DOM.input.value = target.getAttribute('data-prompt');
          resizeInput();
          try { DOM.input.focus(); } catch (err) { /* 忽略 */ }
          return;
        }
        target = target.parentNode;
      }
    };

    DOM.messages.onscroll = checkScroll;
    DOM.toBottom.onclick = stickNow;

    DOM.composer.onsubmit = function (e) {
      if (e.preventDefault) e.preventDefault();
      if (state.streaming) {
        if (state.stream) state.stream.abort();
        return;
      }
      sendMessage(DOM.input.value);
      DOM.input.value = '';
      resizeInput();
    };

    DOM.input.oninput = function () { resizeInput(); checkSendable(); };
    DOM.input.onkeydown = function (e) {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.keyCode === 13 && !e.shiftKey) {
        if (e.preventDefault) e.preventDefault();
        DOM.composer.onsubmit(e);
      }
    };

    document.onkeydown = function (e) {
      if (e.keyCode === 27) {
        if (!DOM.permOverlay.hidden) { decidePermission(false, false); return; }
        if (!DOM.sheetOverlay.hidden) { closeSheet(); return; }
        if (DOM.rail.className.indexOf('is-open') >= 0) { closeRail(); return; }
      }
    };

    window.onresize = function () { applyViewport(); };
    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', applyViewport);
      window.visualViewport.addEventListener('scroll', applyViewport);
    }
    window.onorientationchange = function () { window.setTimeout(applyViewport, 250); };
  }

  function checkSendable() {
    if (!DOM.sendBtn) return;
    if (state.streaming) { DOM.sendBtn.disabled = false; return; }
    /* 只发图不写字是合法的：「这张图里是什么」本来就不用打字 */
    DOM.sendBtn.disabled = !trim(DOM.input.value) && !pendingIds().length;
  }

  /* ================= 启动 ================= */

  function init() {
    DOM.app = $('#app');
    DOM.hero = $('#hero');
    DOM.assistant = $('#assistant');
    DOM.topbar = $('#topbar');
    DOM.brand = $('#brand');
    DOM.closeBtn = $('#closeBtn');
    DOM.mainNav = $('#mainNav');
    DOM.exploreNow = $('#exploreNow');
    DOM.fullBtn = $('#fullBtn');
    DOM.menuBtn = $('#menuBtn');
    DOM.startBtn = $('#startBtn');
    DOM.filmBtn = $('#filmBtn');
    DOM.scrollCue = $('#scrollCue');
    DOM.focusBtn = $('#focusBtn');
    DOM.focusExit = $('#focusExit');
    DOM.hud = $('#hud');
    DOM.rail = $('#rail');
    DOM.railScrim = $('#railScrim');
    DOM.sessionList = $('#sessionList');
    DOM.newSessionBtn = $('#newSessionBtn');
    DOM.railToggle = $('#railToggle');
    DOM.railSettings = $('#railSettings');
    DOM.conn = $('#conn');
    DOM.connText = $('#connText');
    DOM.chatTitle = $('#chatTitle');
    DOM.chatKicker = $('#chatKicker');
    DOM.personaBtn = $('#personaBtn');
    DOM.personaName = $('#personaName');
    DOM.sourceChips = $('#sourceChips');
    DOM.mediaBtn = $('#mediaBtn');
    DOM.capBtn = $('#capBtn');
    DOM.settingsBtn = $('#settingsBtn');
    DOM.messages = $('#messages');
    DOM.toBottom = $('#toBottom');
    DOM.composer = $('#composer');
    DOM.composerMedia = $('#composerMedia');
    DOM.composerAttach = $('#composerAttach');
    DOM.attachFile = $('#attachFile');
    DOM.attachStrip = $('#attachStrip');
    DOM.input = $('#input');
    DOM.sendBtn = $('#sendBtn');
    DOM.sheetOverlay = $('#sheetOverlay');
    DOM.sheet = $('#sheet');
    DOM.sheetKicker = $('#sheetKicker');
    DOM.sheetTitle = $('#sheetTitle');
    DOM.sheetBody = $('#sheetBody');
    DOM.sheetClose = $('#sheetClose');
    DOM.sheetScrim = $('#sheetScrim');
    DOM.permOverlay = $('#permOverlay');
    DOM.permAction = $('#permAction');
    DOM.permPath = $('#permPath');
    DOM.permPreview = $('#permPreview');
    DOM.permCountdown = $('#permCountdown');
    DOM.permDeny = $('#permDeny');
    DOM.permAllow = $('#permAllow');
    DOM.permRemember = $('#permRemember');
    DOM.toast = $('#toast');
    DOM.wallpaperImg = $('#wallpaperImg');

    applyViewport();

    /* 壁纸：本地保存的优先，否则用默认海上日落 */
    applyWallpaper(wallpaperValue(), true);
    DOM.wallpaperImg.onerror = function () {
      DOM.wallpaperImg.className = 'is-out';
    };

    state.sessions = readSessions();
    renderSessions();
    bind();

    loadCapabilities(function () { applyCapabilities(); });
    loadTools();
    // 风格列表由后端提供：新增一种风格时只改后端，前端不用发版
    loadPersonas();

    var active = lsGet(STORE.active, '');
    if (active) {
      state.sessionId = active;
      var meta = null;
      for (var i = 0; i < state.sessions.length; i++) {
        if (state.sessions[i].id === active) { meta = state.sessions[i]; break; }
      }
      if (meta) setChatTitle(meta.title);
    }
    renderChips();
    refreshComposer();
    checkSendable();

    /* 首屏：不自动进入会话，保持与参考视频一致的 landing 观感 */
    DOM.hero.hidden = false;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
