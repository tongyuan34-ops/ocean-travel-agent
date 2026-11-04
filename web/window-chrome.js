/* 桌面壳：无边框窗口的窗口按钮与拖动。
 *
 * 这个文件只在 pywebview 里做事。浏览器里 window.pywebview 不存在，
 * 整段代码自动静默退出，不会留下一个点了没反应的按钮。
 */
(function () {
  'use strict';

  var body = document.body;
  var topbar = document.getElementById('topbar');
  var controls = document.getElementById('winControls');
  var minBtn = document.getElementById('winMin');
  var maxBtn = document.getElementById('winMax');
  var closeBtn = document.getElementById('winClose');
  var api = null;

  function bind() {
    api = window.pywebview && window.pywebview.api;
    if (!api) return;
    body.classList.add('is-desktop');
    if (controls) controls.hidden = false;
  }

  // pywebview 可能在我们执行之前就绪，也可能之后，两条路都要接
  if (window.pywebview && window.pywebview.api) bind();
  window.addEventListener('pywebviewready', bind);

  function call(name) {
    if (!api || typeof api[name] !== 'function') return null;
    try {
      return api[name]();
    } catch (err) {
      return null;
    }
  }

  function setMaximized(state) {
    if (!maxBtn) return;
    maxBtn.classList.toggle('is-maxed', !!state);
    var label = state ? '还原' : '最大化';
    maxBtn.setAttribute('aria-label', label);
    maxBtn.title = label;
  }

  if (minBtn) {
    minBtn.addEventListener('click', function () { call('minimize'); });
  }
  if (closeBtn) {
    closeBtn.addEventListener('click', function () { call('close'); });
  }
  if (maxBtn) {
    maxBtn.addEventListener('click', function () {
      var result = call('toggle_maximize');
      // 后端返回的是 Promise（跨进程调用），拿到真值再换图标
      if (result && typeof result.then === 'function') {
        result.then(setMaximized);
      }
    });
  }

  // 顶栏是拖动区域，但顶栏上的按钮不能拖窗口。
  // pywebview 的拖动监听挂在 document.body 上，这里在冒泡到 body 之前截住——
  // 不拦的话，点一下按钮会先开始拖窗口，手一抖窗口就跑了。
  if (topbar) {
    topbar.addEventListener('mousedown', function (ev) {
      var target = ev.target;
      if (target && target.closest &&
          target.closest('button,a,input,textarea,select')) {
        ev.stopPropagation();
      }
    });
  }
})();
