/* 主动心智面板（v2：三类作业） */
(function () {
  "use strict";

  var view = { data: null, loadedAt: 0, timer: null, editingIntent: null };

  function getBridge() {
    return window.AstrBotPluginView || window.AstrBotPluginPage || null;
  }

  function sleep(ms) {
    return new Promise(function (resolve) {
      window.setTimeout(resolve, ms);
    });
  }

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
    if (response && response.status === "ok") return response.data || {};
    if (response && response.status === "error") throw new Error(response.message || "请求失败");
    return response || {};
  }

  async function apiGet(path, params) {
    var bridge = getBridge();
    if (!bridge) throw new Error("插件面板桥接不可用（请从插件详情页的「视图」入口打开）");
    return unwrap(await bridge.apiGet("page/" + path, params || {}));
  }

  async function apiPost(path, body) {
    var bridge = getBridge();
    if (!bridge) throw new Error("插件面板桥接不可用（请从插件详情页的「视图」入口打开）");
    return unwrap(await bridge.apiPost("page/" + path, body || {}));
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
      date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()) +
      "T" + pad(date.getHours()) + ":" + pad(date.getMinutes())
    );
  }

  function fromLocalInput(value) {
    if (!value) return "";
    var date = new Date(value);
    return isNaN(date.getTime()) ? "" : date.toISOString();
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

  function jobBadge(job, label) {
    if (!job) return "";
    if (job.cancelled) return '<span class="badge warn">' + label + "：已取消</span>";
    if (!job.next_at) return '<span class="badge">' + label + "：未安排</span>";
    return (
      '<span class="badge ok">' + label + "：" + esc(job.human) +
      "（还有 " + humanCountdown(job.countdown) + "）</span>"
    );
  }

  function renderHeader(data) {
    var plugin = data.plugin || {};
    var runtime = data.runtime || {};
    document.getElementById("version").textContent = "v" + (plugin.version || "-");
    var enabled = document.getElementById("enabled-tag");
    enabled.textContent = plugin.enabled ? "已启用" : "已停用";
    enabled.style.color = plugin.enabled ? "var(--pm-ok)" : "var(--pm-warn)";
    var lm = document.getElementById("lm-tag");
    lm.textContent = "LivingMemory: " + (runtime.livingmemory ? "已连接" : "未连接");
    lm.style.color = runtime.livingmemory ? "var(--pm-ok)" : "var(--pm-warn)";
    document.getElementById("flow-tag").textContent =
      "流水 " + (runtime.window || "-") + " 条（" + (runtime.window_source || "config") + "）";
    document.getElementById("runtime").textContent = [
      "来源 " + (runtime.flow_source || "-"),
      "模型 " + (runtime.provider || "-"),
      "工具 " + (runtime.tool_count || 0),
      "用户消息：" + ((data.settings || {}).user_message_mode === "record_only" ? "记录模式（只回一条）" : ((data.settings || {}).user_message_mode || "-")),
      "服务器时间 " + (plugin.server_time_human || "-"),
    ].join(" · ");
  }

  function setValue(id, value) {
    var el = document.getElementById(id);
    if (el && document.activeElement !== el && value !== undefined && value !== null) {
      el.value = value;
    }
  }

  function renderGlobal(data) {
    var s = data.settings || {};
    setValue("cfg-random-min", s.random_min_minutes);
    setValue("cfg-random-max", s.random_max_minutes);
    setValue("cfg-hobby-min", s.hobby_min_interval_minutes);
    setValue("cfg-hobby-max", s.hobby_max_interval_minutes);
    setValue("cfg-hobby-mem", s.hobby_memory_count);
    setValue("cfg-hobby-flow", s.hobby_flow_count);
    setValue("cfg-cap", s.max_consecutive_wakes);
    setValue("cfg-flow-age", s.flow_max_age_hours);
    setValue("cfg-temp-max", s.temp_max_jobs);
    setValue("cfg-single", String(!!s.single_request));
    setValue("cfg-mem-mode", s.memory_mode || "tool");
  }

  function renderDialogueTopic(session) {
    var dialogue = session.dialogue || {};
    var topic = dialogue.topic ? String(dialogue.topic) : "";
    var provisional = dialogue.provisional ? "（保底时间，等她回复后覆盖）" : "";
    var line = topic
      ? '<div class="meta">下次话题：' + esc(topic) + "（用户一说话即作废）</div>"
      : '<div class="meta">下次话题：未留（下一轮自己发挥）</div>';
    if (provisional) line += '<div class="meta warn-text">' + esc(provisional) + "</div>";
    return line;
  }

  function renderJobBlock(session, kind, title, extraHtml) {
    var job = session[kind] || {};
    var inputId = "in-" + kind + "-" + esc(session.umo);
    return (
      '<div class="jobrow">' +
      '<div class="jobtitle">' + title + "</div>" +
      '<div class="jobbody">' +
      jobBadge(job, title) +
      extraHtml +
      "</div>" +
      '<div class="actions">' +
      '<input type="datetime-local" data-role="wake-input" data-kind="' + kind + '" value="' +
      esc(toLocalInput(job.next_at)) + '" />' +
      '<button data-action="set-' + kind + '">保存时间</button>' +
      '<button class="danger" data-action="cancel-' + kind + '">取消</button>' +
      '<button class="ghost" data-action="run-' + kind + '">立即跑一次</button>' +
      "</div></div>"
    );
  }

  function renderTemp(session) {
    var items = session.temp || [];
    // 每条临时提醒各有一个自己的时间输入框；有几条就有几个具体时间
    var list = items.length
      ? items
          .map(function (item) {
            return (
              '<div class="temp-item" data-temp="' + esc(item.id) + '">' +
              '<div class="meta"><b>' + esc(item.human || item.next_at || "") + "</b>（" +
              esc(item.created_by || "-") + "）" + esc(item.note || "") + "</div>" +
              '<div class="actions">' +
              '<input type="datetime-local" data-role="temp-input" data-temp="' + esc(item.id) +
              '" value="' + esc(toLocalInput(item.next_at)) + '" />' +
              '<button data-action="set-temp" data-temp="' + esc(item.id) + '">保存时间</button>' +
              '<button class="danger" data-action="cancel-temp" data-temp="' + esc(item.id) + '">取消</button>' +
              "</div></div>"
            );
          })
          .join("")
      : '<div class="empty">当前没有临时提醒</div>';
    return (
      '<div class="jobrow"><div class="jobtitle">临时提醒（' + items.length + " 条）</div>" +
      '<div class="jobbody">' + list + "</div>" +
      '<div class="jobtitle" style="margin-top:8px">新增一条</div>' +
      '<div class="actions">' +
      '<input type="datetime-local" data-role="temp-when" />' +
      '<input type="text" data-role="temp-note" placeholder="到点要做什么" style="min-width:220px" />' +
      '<button data-action="add-temp">新增</button>' +
      "</div></div>"
    );
  }

  function renderHobbyIntent(session) {
    // 注意：视图 iframe 没有 allow-modals，window.prompt 会被静默拦掉，
    // 所以这里用页内 textarea 编辑。
    var hobby = session.hobby || {};
    var editing = view.editingIntent === session.umo;
    if (!editing) {
      var text = hobby.intent
        ? '<div class="meta">下轮计划：' + esc(hobby.intent) + "（已执行 " + (hobby.runs || 0) + " 轮）</div>"
        : '<div class="meta">下轮计划：未写（已执行 ' + (hobby.runs || 0) + " 轮）</div>";
      return (
        text +
        '<div class="actions"><button class="ghost" data-action="edit-hobby-intent">改写爱好计划</button></div>'
      );
    }
    return (
      '<div class="field"><span>下轮计划（下一轮爱好周期会照着它做）</span>' +
      '<textarea data-role="intent-input" rows="4" style="width:100%">' +
      esc(hobby.intent || "") + "</textarea></div>" +
      '<div class="actions">' +
      '<button class="primary" data-action="save-hobby-intent">保存计划</button>' +
      '<button class="ghost" data-action="cancel-hobby-intent">取消</button>' +
      "</div>"
    );
  }

  function renderSession(session, settings) {
    var stale = session.last_sent || {};
    var lastSent = stale.message
      ? '<div class="quote">' + esc(stale.message) + "</div>"
      : '<div class="empty">还没有主动说过话</div>';
    var staleNote = stale.stale
      ? '<div class="meta warn-text">很久没主动说过话了（' + esc(stale.human) + "）</div>"
      : '<div class="meta">上次主动：' + esc(stale.human || "无") + "</div>";
    var lastDialogue = session.last_dialogue
      ? '<div class="quote">' + esc((session.last_dialogue.sender || session.last_dialogue.role) + "：" +
          session.last_dialogue.text) + "</div>"
      : '<div class="empty">读不到最近对话</div>';

    return (
      '<div class="card" data-umo="' + esc(session.umo) + '">' +
      '<div class="card-head"><span class="umo">' + esc(session.umo) + "</span>" +
      '<span class="badge">' + esc(session.platform || "-") + "</span>" +
      '<span class="badge' + (settings.max_consecutive_wakes !== -1 && session.silent_wake_count > settings.max_consecutive_wakes ? " danger" : "") + '">连续 ' +
      session.silent_wake_count + " / " + (settings.max_consecutive_wakes === -1 ? "∞" : settings.max_consecutive_wakes) +
      "</span></div>" +
      renderJobBlock(session, "dialogue", "对话节奏", renderDialogueTopic(session)) +
      renderJobBlock(session, "hobby", "爱好节奏", renderHobbyIntent(session)) +
      renderTemp(session) +
      '<div class="jobrow"><div class="jobtitle">上下文</div><div class="jobbody">' +
      staleNote + lastSent +
      '<div class="meta">最近一条对话</div>' + lastDialogue +
      "</div></div>" +
      '<div class="actions">' +
      '<button class="ghost" data-action="reset-count">清零计数</button>' +
      '<button class="ghost" data-action="reset-hobby-runs">轮数归零</button>' +
      "</div></div>"
    );
  }

  function renderSessions(data) {
    var host = document.getElementById("sessions");
    var sessions = data.sessions || [];
    if (!sessions.length) {
      host.innerHTML = '<div class="empty">还没有会话记录。</div>';
      return;
    }
    host.innerHTML = sessions
      .map(function (session) {
        return renderSession(session, data.settings || {});
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
        var kindLabel = { dialogue: "对话", hobby: "爱好", temp: "临时" }[trace.kind] || trace.kind || "-";
        var actBadge = trace.act === true
          ? '<span class="badge ok">行动</span>'
          : trace.act === false
          ? '<span class="badge">不打扰</span>'
          : '<span class="badge warn">未判断</span>';
        var sentBadge = trace.message_sent
          ? '<span class="badge ok">' + (trace.message_source === "main_agent" ? "主 Agent 已回复" : "已发送") + "</span>"
          : '<span class="badge">未发送</span>';
        var stats = trace.context_stats || {};
        var statsBadge = stats.requests
          ? '<span class="badge">请求 ' + stats.requests + " 次 · 人格 " + (stats.persona_units || 0) +
            " · 流水 " + (stats.flow_units || 0) + "</span>"
          : "";
        var repeatBadge = trace.looks_repeated
          ? '<span class="badge warn" title="相似度 ' + (trace.reason_similarity || "") + '">疑似复读</span>'
          : "";
        var steps = (trace.steps || []).map(function (step) {
          return '<div class="meta">· ' + esc(step.tool) + " " + (step.ok ? "成功" : "失败（" + esc(step.error || "") + "）") + "</div>";
        }).join("");
        var notes = (trace.warnings || []).concat(trace.errors || []);
        var detail = notes.length
          ? "<details><summary>告警 / 错误（" + notes.length + "）</summary><div class=\"meta\">" +
            notes.map(esc).join("<br>") + "</div></details>"
          : "";
        return (
          '<div class="trace"><div class="meta">' +
          esc(formatTime(trace.started_at)) + " · " + esc(kindLabel) + " · " + esc(trace.trigger || "-") +
          " · 流水 " + (trace.flow_count || 0) + " 条" +
          (trace.memory_count ? " · 记忆 " + trace.memory_count + " 条" : "") +
          "</div><div>" + actBadge + " " + sentBadge + " " + statsBadge + " " + repeatBadge +
          (trace.schedule_fallback ? ' <span class="badge warn">默认间隔</span>' : "") +
          (trace.schedule_blocked ? ' <span class="badge danger">' + esc(trace.schedule_blocked) + "</span>" : "") +
          "</div>" +
          (trace.reason ? '<div class="quote">判断：' + esc(trace.reason) + "</div>" : "") +
          (trace.thought ? '<div class="quote">想法：' + esc(trace.thought) + "</div>" : "") +
          (trace.message ? '<div class="quote">' + (trace.message_source === "main_agent" ? "回复（主 Agent）：" : "要说：") + esc(trace.message) + "</div>" : "") +
          steps +
          '<div class="meta">下次：' + esc(trace.next_wake_at ? formatTime(trace.next_wake_at) : "未安排") + "</div>" +
          detail + "</div>"
        );
      })
      .join("");
  }

  function isEditing() {
    var active = document.activeElement;
    return !!(active && typeof active.closest === "function" && active.closest("#sessions"));
  }

  function tickCountdown() {
    // 倒计时以服务端返回的 countdown 为基准，页面每秒推算
    var nodes = document.querySelectorAll("[data-role=countdown]");
    if (!nodes.length) return;
  }

  async function load() {
    try {
      var data = await apiGet("state");
      view.data = data;
      view.loadedAt = Date.now();
      renderHeader(data);
      renderGlobal(data);
      // 正在编辑爱好计划时也不重绘会话卡，避免把用户正在输入的 textarea 冲掉
      if (!isEditing() && !view.editingIntent) renderSessions(data);
      renderTraces(data);
      renderHints(data);
      renderOutbound(data);
    } catch (error) {
      toast("读取状态失败：" + error.message, true);
      document.getElementById("runtime").textContent = "读取失败：" + error.message;
    }
  }

  async function handleAction(action, card) {
    var umo = card.getAttribute("data-umo");
    var kind = null;
    if (action.indexOf("set-dialogue") === 0 || action === "cancel-dialogue" || action === "run-dialogue") kind = "dialogue";
    if (action.indexOf("set-hobby") === 0 || action === "cancel-hobby" || action === "run-hobby") kind = "hobby";

    if (action === "set-dialogue" || action === "set-hobby") {
      var input = card.querySelector('[data-role="wake-input"][data-kind="' + kind + '"]');
      var iso = fromLocalInput(input.value);
      if (!iso) { toast("请选择有效时间", true); return; }
      await apiPost(kind + "/set", { umo: umo, wake_at: iso });
      toast(kind === "hobby" ? "爱好节奏已更新" : "对话节奏已更新");
    } else if (action === "cancel-dialogue" || action === "cancel-hobby") {
      await apiPost("cancel", { umo: umo, kind: kind });
      toast("已取消" + (kind === "hobby" ? "爱好" : "对话") + "节奏");
    } else if (action === "run-dialogue" || action === "run-hobby") {
      await apiPost("run-now", { umo: umo, kind: kind });
      toast("已开始一次" + (kind === "hobby" ? "爱好" : "对话") + "周期");
    } else if (action === "set-temp") {
      var tempId = card.getAttribute("data-temp") || button.dataset.temp;
      var box = tempId
        ? card.querySelector('[data-role="temp-input"][data-temp="' + tempId + '"]')
        : null;
      var iso3 = fromLocalInput(box ? box.value : "");
      if (!iso3) { toast("请选择有效时间", true); return; }
      await apiPost("temp/set", { umo: umo, temp_id: tempId, wake_at: iso3 });
      toast("临时提醒时间已更新");
    } else if (action === "cancel-temp") {
      var cancelId = button.getAttribute("data-temp");
      await apiPost("temp/cancel", { umo: umo, temp_id: cancelId || null });
      toast("已取消临时提醒");
    } else if (action === "add-temp") {
      var when = card.querySelector('[data-role="temp-when"]').value;
      var note = card.querySelector('[data-role="temp-note"]').value;
      var iso2 = fromLocalInput(when);
      if (!iso2) { toast("请选择时间", true); return; }
      await apiPost("temp/add", { umo: umo, wake_at: iso2, note: note || "临时提醒" });
      toast("已加临时提醒");
    } else if (action === "edit-hobby-intent") {
      view.editingIntent = umo;
      if (view.data) renderSessions(view.data);
      return;
    } else if (action === "cancel-hobby-intent") {
      view.editingIntent = null;
      if (view.data) renderSessions(view.data);
      return;
    } else if (action === "save-hobby-intent") {
      var box = card.querySelector('[data-role="intent-input"]');
      var value = box ? box.value : "";
      await apiPost("hobby/intent", { umo: umo, intent: value });
      view.editingIntent = null;
      toast("已更新爱好计划");
    } else if (action === "reset-count") {
      await apiPost("count/reset", { umo: umo });
      toast("计数已清零");
    }
    if (action === "reset-hobby-runs") {
      await apiPost("count/reset", { umo: umo, what: "hobby_runs" });
      toast("爱好轮数已归零");
    }
    await load();
  }

  function bindEvents() {
    document.getElementById("sessions").addEventListener("click", function (event) {
      var button = event.target.closest("button[data-action]");
      if (!button) return;
      var card = button.closest(".card");
      if (!card) return;
      handleAction(button.getAttribute("data-action"), card).catch(function (error) {
        toast(error.message, true);
      });
    });

    document.getElementById("btn-refresh").addEventListener("click", function () {
      load();
    });

    document.getElementById("btn-save-cfg").addEventListener("click", function () {
      var patch = {
        "wake.min_interval_minutes": document.getElementById("cfg-random-min").value,
        "wake.max_interval_minutes": document.getElementById("cfg-random-max").value,
        "hobby.min_interval_minutes": document.getElementById("cfg-hobby-min").value,
        "hobby.max_interval_minutes": document.getElementById("cfg-hobby-max").value,
        "hobby.memory_count": document.getElementById("cfg-hobby-mem").value,
        "hobby.flow_count": document.getElementById("cfg-hobby-flow").value,
        "limit.max_consecutive_wakes": document.getElementById("cfg-cap").value,
        "flow.flow_max_age_hours": document.getElementById("cfg-flow-age").value,
        "temp.max_jobs": document.getElementById("cfg-temp-max").value,
        "mind.single_request": document.getElementById("cfg-single").value,
        "memory.mode": document.getElementById("cfg-mem-mode").value,
      };
      apiPost("config/patch", { patch: patch })
        .then(function () {
          toast("全局设置已保存");
          return load();
        })
        .catch(function (error) {
          toast(error.message, true);
        });
    });
  }

  function rulesToText(rules) {
    if (!rules) return "";
    if (Array.isArray(rules)) {
      return rules
        .map(function (rule) {
          if (rule && typeof rule === "object") {
            return (rule.pattern || rule.regex || "") + "=>" + (rule.replacement || rule.replace || "");
          }
          return "";
        })
        .filter(Boolean)
        .join("\n");
    }
    return String(rules);
  }

  function textToRules(text) {
    return String(text || "")
      .split(/\r?\n/)
      .map(function (line) {
        var index = line.indexOf("=>");
        if (index < 0) return null;
        return {
          pattern: line.slice(0, index).trim(),
          replacement: line.slice(index + 2),
        };
      })
      .filter(function (rule) {
        return rule && rule.pattern;
      });
  }

  function renderHints(data) {
    var s = (data && data.settings) || {};
    var defaults = (data && data.hint_defaults) || {};
    setValue("cfg-output-hint", s.output_hint || "");
    setValue("cfg-wake-hint", s.wake_hint || "");
    view.hintDefaults = defaults;
  }

  function bindHints() {
    var saveOutput = document.getElementById("btn-save-output-hint");
    var saveWake = document.getElementById("btn-save-wake-hint");
    var resetOutput = document.getElementById("btn-reset-output-hint");
    var resetWake = document.getElementById("btn-reset-wake-hint");
    if (!saveOutput || !saveWake) return;

    saveOutput.addEventListener("click", function () {
      apiPost("config/patch", {
        patch: { "mind.output_hint": document.getElementById("cfg-output-hint").value },
      })
        .then(function () {
          toast("output_hint 已保存");
          return load();
        })
        .catch(function (error) {
          toast(error.message, true);
        });
    });

    saveWake.addEventListener("click", function () {
      apiPost("config/patch", {
        patch: { "mind.wake_hint": document.getElementById("cfg-wake-hint").value },
      })
        .then(function () {
          toast("wake_hint 已保存");
          return load();
        })
        .catch(function (error) {
          toast(error.message, true);
        });
    });

    if (resetOutput) {
      resetOutput.addEventListener("click", function () {
        var value = (view.hintDefaults || {}).output_hint || "";
        document.getElementById("cfg-output-hint").value = value;
        apiPost("config/patch", { patch: { "mind.output_hint": value } })
          .then(function () {
            toast("已恢复默认 output_hint");
            return load();
          })
          .catch(function (error) {
            toast(error.message, true);
          });
      });
    }

    if (resetWake) {
      resetWake.addEventListener("click", function () {
        var value = (view.hintDefaults || {}).wake_hint || "";
        document.getElementById("cfg-wake-hint").value = value;
        apiPost("config/patch", { patch: { "mind.wake_hint": value } })
          .then(function () {
            toast("已恢复默认 wake_hint");
            return load();
          })
          .catch(function (error) {
            toast(error.message, true);
          });
      });
    }
  }

  function renderOutbound(data) {
    var s = (data && data.outbound) || {};
    setValue("cfg-out-replace", String(s.replace_enabled !== false));
    setValue("cfg-out-rules", rulesToText(s.replace_rules));
    setValue("cfg-out-segment", String(!!s.segment_enabled));
    setValue("cfg-out-seg-pattern", s.segment_pattern || "");
    setValue("cfg-out-seg-max", s.segment_max);
    setValue("cfg-out-seg-interval", s.segment_interval);
    setValue("cfg-out-cleanup", String(!!s.cleanup_enabled));
    setValue("cfg-out-cleanup-pattern", s.cleanup_pattern || "");
  }

  function bindOutbound() {
    var save = document.getElementById("btn-save-outbound");
    if (!save) return;
    save.addEventListener("click", function () {
      var patch = {
        "outbound.replace_enabled": document.getElementById("cfg-out-replace").value,
        "outbound.replace_rules": textToRules(document.getElementById("cfg-out-rules").value),
        "outbound.segment_enabled": document.getElementById("cfg-out-segment").value,
        "outbound.segment_pattern": document.getElementById("cfg-out-seg-pattern").value,
        "outbound.segment_max": document.getElementById("cfg-out-seg-max").value,
        "outbound.segment_interval": document.getElementById("cfg-out-seg-interval").value,
        "outbound.cleanup_enabled": document.getElementById("cfg-out-cleanup").value,
        "outbound.cleanup_pattern": document.getElementById("cfg-out-cleanup-pattern").value,
      };
      apiPost("config/patch", { patch: patch })
        .then(function () {
          toast("出站设置已保存");
          return load();
        })
        .catch(function (error) {
          toast(error.message, true);
        });
    });

    var preview = document.getElementById("btn-preview-outbound");
    if (preview) {
      preview.addEventListener("click", function () {
        var text = document.getElementById("cfg-out-preview-text").value;
        if (!text.trim()) {
          toast("先输入一段话", true);
          return;
        }
        apiPost("outbound/preview", { text: text })
          .then(function (data) {
            var host = document.getElementById("outbound-preview");
            var segments = data.segments || [];
            host.innerHTML =
              "共 " + segments.length + " 段：" +
              segments
                .map(function (seg, index) {
                  return '<div class="quote">#' + (index + 1) + " " + esc(seg) + "</div>";
                })
                .join("");
          })
          .catch(function (error) {
            toast(error.message, true);
          });
      });
    }
  }

  async function start() {
    bindEvents();
    bindHints();
    bindOutbound();
    var bridge = await waitForBridge(8000);
    if (!bridge) {
      document.getElementById("runtime").textContent =
        "无法连接 AstrBot 面板桥接。请从「插件详情 → 视图 → 主动心智」打开本页面。";
      toast("插件面板桥接不可用", true);
      return;
    }
    if (typeof bridge.ready === "function") {
      try {
        var context = await bridge.ready();
        if (context && context.isDark && document.documentElement.dataset.theme !== "dark") {
          document.documentElement.dataset.theme = "dark";
        }
      } catch (error) {
        /* 上下文拿不到也继续 */
      }
    }
    await load();
    if (view.timer) window.clearInterval(view.timer);
    view.timer = window.setInterval(tickCountdown, 1000);
    window.setInterval(load, 20000);
  }

  start();
})();
