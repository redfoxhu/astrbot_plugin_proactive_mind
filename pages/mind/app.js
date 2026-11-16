/* 主动心智面板前端：只依赖宿主注入的 window.AstrBotPluginView */
(function () {
  "use strict";

  var view = {
    data: null,
    loadedAt: 0,
    timer: null,
  };

  function getBridge() {
    return window.AstrBotPluginView || window.AstrBotPluginPage || null;
  }

  function sleep(ms) {
    return new Promise(function (resolve) {
      window.setTimeout(resolve, ms);
    });
  }

  /** 宿主在 </body> 前注入桥接脚本；这里等它就绪，避免脚本顺序导致的 "桥接不可用"。 */
  async function waitForBridge(timeoutMs) {
    var deadline = Date.now() + (timeoutMs || 8000);
    while (Date.now() < deadline) {
      var bridge = getBridge();
      if (bridge) return bridge;
      await sleep(80);
    }
    return null;
  }

  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function unwrap(response) {
    if (response && response.status === "ok") {
      return response.data || {};
    }
    if (response && response.status === "error") {
      throw new Error(response.message || "请求失败");
    }
    return response || {};
  }

  async function apiGet(path, params) {
    var bridge = getBridge();
    if (!bridge) throw new Error("插件面板桥接不可用（请从插件详情页的「视图」入口打开）");
    var result = await bridge.apiGet("page/" + path, params || {});
    return unwrap(result);
  }

  async function apiPost(path, body) {
    var bridge = getBridge();
    if (!bridge) throw new Error("插件面板桥接不可用（请从插件详情页的「视图」入口打开）");
    var result = await bridge.apiPost("page/" + path, body || {});
    return unwrap(result);
  }

  function toast(message, isError) {
    var host = document.getElementById("toast");
    if (!host) return;
    var item = document.createElement("div");
    item.className = "item" + (isError ? " err" : "");
    item.textContent = message;
    host.appendChild(item);
    window.setTimeout(function () {
      item.remove();
    }, isError ? 6000 : 3000);
  }

  function pad(value) {
    return String(value).padStart(2, "0");
  }

  function toLocalInput(iso) {
    if (!iso) return "";
    var date = new Date(iso);
    if (isNaN(date.getTime())) return "";
    return (
      date.getFullYear() +
      "-" +
      pad(date.getMonth() + 1) +
      "-" +
      pad(date.getDate()) +
      "T" +
      pad(date.getHours()) +
      ":" +
      pad(date.getMinutes())
    );
  }

  function fromLocalInput(value) {
    if (!value) return "";
    var date = new Date(value);
    if (isNaN(date.getTime())) return "";
    return date.toISOString();
  }

  function humanCountdown(seconds) {
    if (seconds === null || seconds === undefined) return "未安排";
    var total = Math.max(0, Number(seconds) || 0);
    var day = Math.floor(total / 86400);
    var hour = Math.floor((total % 86400) / 3600);
    var minute = Math.floor((total % 3600) / 60);
    var second = total % 60;
    var parts = [];
    if (day) parts.push(day + " 天");
    if (hour) parts.push(hour + " 小时");
    if (minute) parts.push(minute + " 分");
    if (!day && !hour) parts.push(second + " 秒");
    return parts.join(" ");
  }

  function formatTime(value) {
    if (!value) return "未安排";
    var date = new Date(value);
    if (isNaN(date.getTime())) return String(value);
    try {
      return date.toLocaleString("zh-CN", { hour12: false });
    } catch (error) {
      return date.toISOString();
    }
  }

  function capText(cap) {
    if (cap === -1) return "无限";
    return String(cap);
  }

  function renderHeader(data) {
    var plugin = data.plugin || {};
    var runtime = data.runtime || {};
    var settings = data.settings || {};
    document.getElementById("version").textContent = "v" + (plugin.version || "-");
    var enabledTag = document.getElementById("enabled-tag");
    enabledTag.textContent = plugin.enabled ? "已启用" : "已停用";
    enabledTag.style.color = plugin.enabled ? "var(--pm-ok)" : "var(--pm-warn)";
    var lmTag = document.getElementById("lm-tag");
    lmTag.textContent =
      "LivingMemory: " +
      (runtime.livingmemory ? "已连接" : "未连接") +
      " · 窗口 " +
      (runtime.window || "-") +
      " 条";
    lmTag.style.color = runtime.livingmemory ? "var(--pm-ok)" : "var(--pm-warn)";

    var parts = [
      "流水来源 " + (runtime.flow_source || "-") + "（" + (runtime.window || "-") + " 条 / " + (runtime.window_source || "config") + "）",
      "模型 " + (runtime.provider || "（未知）"),
      "工具 " + (runtime.tool_count || 0) + " 个",
      "作业 " + (runtime.pending_jobs || 0) + " 个",
      "服务器时间 " + (plugin.server_time_human || "-"),
    ];
    document.getElementById("runtime").textContent = parts.join(" · ");

    var delay = document.getElementById("cfg-delay");
    var cap = document.getElementById("cfg-cap");
    var mode = document.getElementById("cfg-mode");
    var minInput = document.getElementById("cfg-min");
    var maxInput = document.getElementById("cfg-max");
    if (document.activeElement !== delay) delay.value = settings.wake_delay_minutes;
    if (document.activeElement !== cap) cap.value = settings.max_consecutive_wakes;
    if (document.activeElement !== mode) mode.value = settings.user_message_mode;
    if (document.activeElement !== minInput) minInput.value = settings.min_interval_minutes;
    if (document.activeElement !== maxInput) maxInput.value = settings.max_interval_minutes;
  }

  function echoBadges(session) {
    var echo = session.last_echo || {};
    var names = {
      platform_history: "平台历史",
      livingmemory: "LongMem",
    };
    var out = [];
    Object.keys(names).forEach(function (key) {
      if (!(key in echo)) return;
      var ok = !!echo[key];
      out.push(
        '<span class="badge ' +
          (ok ? "ok" : "warn") +
          '">' +
          names[key] +
          (ok ? " ✓" : " ✗") +
          "</span>"
      );
    });
    return out.join(" ");
  }

  function renderSession(session, settings, index) {
    var cap = settings.max_consecutive_wakes;
    var count = session.silent_wake_count || 0;
    var limitHit = cap !== -1 && count > cap;
    var countBadge = limitHit
      ? '<span class="badge danger">已停止创建（' + count + " / " + capText(cap) + "）</span>"
      : '<span class="badge">连续 ' + count + " / " + capText(cap) + "</span>";
    var delayBadge =
      session.delay_override_minutes === null || session.delay_override_minutes === undefined
        ? ""
        : '<span class="badge">延后覆盖 ' + session.delay_override_minutes + " 分</span>";
    var errorBadge = session.last_error
      ? '<span class="badge warn">' + esc(session.last_error) + "</span>"
      : "";
    var nextWake = session.next_wake_human || "未安排";
    var lastMessage = session.last_sent_message
      ? '<div class="quote">' + esc(session.last_sent_message) + "</div>"
      : '<div class="empty">还没有主动说过话</div>';

    return (
      '<div class="card" data-umo="' +
      esc(session.umo) +
      '">' +
      '<div class="card-head">' +
      '<span class="umo">' +
      esc(session.umo) +
      "</span>" +
      '<span class="badge">' +
      esc(session.platform || "-") +
      "</span>" +
      countBadge +
      delayBadge +
      errorBadge +
      echoBadges(session) +
      "</div>" +
      '<div class="kv">' +
      "<div><b>下次唤醒</b><span data-role=\"countdown\" data-index=\"" +
      index +
      '">' +
      esc(nextWake) +
      "</span></div>" +
      "<div><b>上次主动</b><span>" +
      esc(session.last_sent_at ? formatTime(session.last_sent_at) : "（无）") +
      "</span></div>" +
      "<div><b>上次决定</b><span>" +
      esc(session.last_decision_at ? formatTime(session.last_decision_at) : "（无）") +
      "</span></div>" +
      "</div>" +
      lastMessage +
      '<div class="actions">' +
      '<input type="datetime-local" data-role="wake-input" value="' +
      esc(toLocalInput(session.next_wake_at)) +
      '" />' +
      '<button data-action="set-wake">保存唤醒时间</button>' +
      '<button class="danger" data-action="cancel-wake">取消唤醒</button>' +
      '<input type="number" min="0" max="1440" step="1" style="width: 90px" data-role="delay-input" placeholder="延后分钟" value="' +
      esc(session.delay_override_minutes === null || session.delay_override_minutes === undefined ? "" : session.delay_override_minutes) +
      '" />' +
      '<button data-action="set-delay">应用延后</button>' +
      '<button class="ghost" data-action="clear-delay">恢复默认延后</button>' +
      '<button class="primary" data-action="run-now">立即跑一次</button>' +
      '<button class="ghost" data-action="reset-count">清零计数</button>' +
      "</div>" +
      "</div>"
    );
  }

  function renderSessions(data) {
    var host = document.getElementById("sessions");
    var sessions = data.sessions || [];
    if (!sessions.length) {
      host.innerHTML =
        '<div class="empty">还没有会话记录。等它和你聊过一次、或执行 /mind run 之后就会出现。</div>';
      return;
    }
    var settings = data.settings || {};
    host.innerHTML = sessions
      .map(function (session, index) {
        return renderSession(session, settings, index);
      })
      .join("");
  }

  function renderTraces(data) {
    var host = document.getElementById("traces");
    var traces = data.traces || [];
    if (!traces.length) {
      host.innerHTML = '<div class="empty">还没有决策记录。</div>';
      return;
    }
    host.innerHTML = traces
      .map(function (trace) {
        var actBadge =
          trace.act === true
            ? '<span class="badge ok">行动</span>'
            : trace.act === false
            ? '<span class="badge">不打扰</span>'
            : '<span class="badge warn">未判断</span>';
        var sentBadge = trace.message_sent
          ? '<span class="badge ok">已发送</span>'
          : '<span class="badge">未发送</span>';
        var blocks = trace.steps || [];
        var stepsHtml = blocks.length
          ? blocks
              .map(function (step) {
                return (
                  '<div class="meta">· ' +
                  esc(step.tool) +
                  " " +
                  (step.ok ? "成功" : "失败（" + esc(step.error || "unknown") + "）") +
                  "</div>"
                );
              })
              .join("")
          : "";
        var warnings = (trace.warnings || []).concat(trace.errors || []);
        var detail = warnings.length
          ? "<details><summary>告警 / 错误（" + warnings.length + "）</summary><div class=\"meta\">" + warnings.map(esc).join("<br>") + "</div></details>"
          : "";
        return (
          '<div class="trace">' +
          '<div class="meta">' +
          esc(trace.started_at ? formatTime(trace.started_at) : "") +
          " · " +
          esc(trace.trigger || "-") +
          " · 流水 " +
          esc(trace.flow_count || 0) +
          " 条（" +
          esc(trace.flow_source || "-") +
          "）</div>" +
          "<div>" +
          actBadge +
          " " +
          sentBadge +
          (trace.schedule_blocked
            ? ' <span class="badge danger">' + esc(trace.schedule_blocked) + "</span>"
            : "") +
          "</div>" +
          (trace.reason ? '<div class="quote">判断：' + esc(trace.reason) + "</div>" : "") +
          (trace.thought ? '<div class="quote">想法：' + esc(trace.thought) + "</div>" : "") +
          (trace.message ? '<div class="quote">要说：' + esc(trace.message) + "</div>" : "") +
          stepsHtml +
          '<div class="meta">下次唤醒：' +
          esc(trace.next_wake_at ? formatTime(trace.next_wake_at) : "未安排") +
          "</div>" +
          detail +
          "</div>"
        );
      })
      .join("");
  }

  function tickCountdown() {
    var nodes = document.querySelectorAll('[data-role="countdown"]');
    if (!nodes.length) return;
    var elapsed = Math.floor((Date.now() - view.loadedAt) / 1000);
    nodes.forEach(function (node) {
      var index = Number(node.getAttribute("data-index"));
      var sessions = (view.data && view.data.sessions) || [];
      var session = sessions[index];
      if (!session || session.countdown_seconds === null) return;
      node.textContent =
        session.next_wake_human +
        "（还有 " +
        humanCountdown(session.countdown_seconds - elapsed) +
        "）";
    });
  }

  function isEditingSessions() {
    var active = document.activeElement;
    if (!active || typeof active.closest !== "function") return false;
    return !!active.closest("#sessions");
  }

  async function load() {
    try {
      var data = await apiGet("state");
      view.data = data;
      view.loadedAt = Date.now();
      renderHeader(data);
      // 用户正在会话卡片里编辑时间/延后时，不打断输入
      if (!isEditingSessions()) {
        renderSessions(data);
      }
      renderTraces(data);
      tickCountdown();
    } catch (error) {
      toast("读取状态失败：" + error.message, true);
      document.getElementById("runtime").textContent = "读取失败：" + error.message;
    }
  }

  async function handleAction(action, card, umo) {
    var wakeInput = card.querySelector('[data-role="wake-input"]');
    var delayInput = card.querySelector('[data-role="delay-input"]');
    if (action === "set-wake") {
      var iso = fromLocalInput(wakeInput.value);
      if (!iso) {
        toast("请选择有效时间", true);
        return;
      }
      await apiPost("wake/set", { umo: umo, wake_at: iso });
      toast("已更新唤醒时间");
    } else if (action === "cancel-wake") {
      await apiPost("wake/cancel", { umo: umo });
      toast("已取消唤醒");
    } else if (action === "set-delay") {
      await apiPost("wake/delay", { umo: umo, minutes: delayInput.value });
      toast("已应用本会话延后");
    } else if (action === "clear-delay") {
      await apiPost("wake/delay", { umo: umo, minutes: "default" });
      toast("已恢复默认延后");
    } else if (action === "run-now") {
      await apiPost("run-now", { umo: umo });
      toast("已开始一次主动决策，稍后刷新可见轨迹");
    } else if (action === "reset-count") {
      await apiPost("count/reset", { umo: umo });
      toast("连续触发计数已清零");
    }
    await load();
  }

  function bindEvents() {
    document.getElementById("sessions").addEventListener("click", function (event) {
      var button = event.target.closest("button[data-action]");
      if (!button) return;
      var card = button.closest(".card");
      if (!card) return;
      var umo = card.getAttribute("data-umo");
      handleAction(button.getAttribute("data-action"), card, umo).catch(function (error) {
        toast(error.message, true);
      });
    });

    document.getElementById("btn-refresh").addEventListener("click", function () {
      load();
    });

    document.getElementById("btn-save-cfg").addEventListener("click", function () {
      var patch = {
        "wake.wake_delay_minutes": document.getElementById("cfg-delay").value,
        "limit.max_consecutive_wakes": document.getElementById("cfg-cap").value,
        "basic.user_message_mode": document.getElementById("cfg-mode").value,
        "wake.min_interval_minutes": document.getElementById("cfg-min").value,
        "wake.max_interval_minutes": document.getElementById("cfg-max").value,
      };
      apiPost("config/patch", { patch: patch })
        .then(function () {
          toast("节奏设置已保存");
          return load();
        })
        .catch(function (error) {
          toast(error.message, true);
        });
    });
  }

  async function start() {
    bindEvents();
    var bridge = await waitForBridge(8000);
    if (!bridge) {
      document.getElementById("runtime").textContent =
        "无法连接 AstrBot 面板桥接。请从「插件详情 → 视图 → 主动心智」打开本页面；" +
        "独立标签页没有宿主通信，接口无法调用。";
      document.getElementById("sessions").innerHTML =
        '<div class="empty">桥接未就绪，无法读取状态。</div>';
      document.getElementById("traces").innerHTML =
        '<div class="empty">桥接未就绪，无法读取轨迹。</div>';
      toast("插件面板桥接不可用", true);
      return;
    }
    if (typeof bridge.ready === "function") {
      try {
        var context = await bridge.ready();
        // 宿主若只给了 isDark 而没写 data-theme，这里补一刀，避免"白底白字"
        if (context && context.isDark && document.documentElement.dataset.theme !== "dark") {
          document.documentElement.dataset.theme = "dark";
        }
      } catch (error) {
        /* 上下文拿不到也继续，接口仍可用 */
      }
    }
    await load();
    if (view.timer) window.clearInterval(view.timer);
    view.timer = window.setInterval(tickCountdown, 1000);
    window.setInterval(load, 20000);
  }

  start();
})();
