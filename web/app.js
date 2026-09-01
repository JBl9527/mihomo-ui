/* Mihomo 控制台前端：零依赖，原生 DOM。
   分块顺序：工具函数 → 状态与请求 → 视图渲染 → 弹层 → 实时流量 → 启动。 */
(function () {
  "use strict";

  // ------------------------------------------------------------ 工具
  var app = document.getElementById("app");
  var toasts = document.getElementById("toasts");
  var scrim = document.getElementById("scrim");

  function $(selector, root) { return (root || document).querySelector(selector); }
  function $$(selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); }
  function bind(name) { return document.querySelector('[data-bind="' + name + '"]'); }
  function put(name, text) { var node = bind(name); if (node) { node.textContent = text; } }

  function esc(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function bytes(value) {
    var n = Number(value) || 0;
    if (n < 1024) { return n.toFixed(0) + " B"; }
    var units = ["KB", "MB", "GB", "TB", "PB"];
    var index = -1;
    do { n /= 1024; index += 1; } while (n >= 1024 && index < units.length - 1);
    return (n >= 100 ? n.toFixed(0) : n.toFixed(n >= 10 ? 1 : 2)) + " " + units[index];
  }

  function rate(value) { return bytes(value) + "/s"; }

  function stamp(value) {
    var seconds = Number(value) || 0;
    if (!seconds) { return "—"; }
    var d = new Date(seconds * 1000);
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate())
      + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function ago(value) {
    var seconds = Number(value) || 0;
    if (!seconds) { return "—"; }
    var diff = Math.max(0, Math.floor(Date.now() / 1000) - seconds);
    if (diff < 60) { return "刚刚"; }
    if (diff < 3600) { return Math.floor(diff / 60) + " 分钟前"; }
    if (diff < 86400) { return Math.floor(diff / 3600) + " 小时前"; }
    if (diff < 86400 * 30) { return Math.floor(diff / 86400) + " 天前"; }
    return stamp(seconds);
  }

  function daysLeft(value) {
    var seconds = Number(value) || 0;
    if (!seconds) { return null; }
    return Math.ceil((seconds - Date.now() / 1000) / 86400);
  }
  // ------------------------------------------------------------ 提示条
  function toast(kind, title, detail) {
    var node = document.createElement("div");
    node.className = "toast " + (kind || "");
    node.innerHTML = "<b>" + esc(title) + "</b>" + (detail ? "<small>" + esc(detail) + "</small>" : "");
    toasts.appendChild(node);
    var timer = setTimeout(close, kind === "bad" ? 7000 : 4200);
    node.addEventListener("click", close);
    function close() {
      clearTimeout(timer);
      node.classList.add("out");
      setTimeout(function () { if (node.parentNode) { node.parentNode.removeChild(node); } }, 200);
    }
  }

  function report(result, fallback) {
    var notes = (result && result.notes) || [];
    toast("ok", (result && result.msg) || fallback || "已完成", notes.join("；"));
  }

  function fail(exc) { toast("bad", "操作没有完成", exc && exc.message ? exc.message : String(exc)); }

  function busy(button, running) {
    if (!button) { return; }
    button.classList.toggle("busy", !!running);
    button.disabled = !!running;
  }

  // ------------------------------------------------------------ 请求
  function api(path, options) {
    options = options || {};
    var init = { method: options.method || "GET", headers: {}, cache: "no-store" };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    return fetch(path, init).then(function (response) {
      if (response.status === 401) {
        location.replace("/login");
        throw new Error("会话已失效，请重新登录");
      }
      return response.text().then(function (text) {
        var data = {};
        if (text) { try { data = JSON.parse(text); } catch (exc) { data = {}; } }
        if (!response.ok) {
          throw new Error(data.detail || "请求失败（HTTP " + response.status + "）");
        }
        return data;
      });
    });
  }

  var state = {
    status: {},
    controller: {},
    profiles: [],
    activeId: null,
    groups: [],
    filter: "",
    view: "dash",
    logTimer: null
  };
  // ------------------------------------------------------------ 视图切换
  var TITLES = { dash: "概览", profiles: "配置", nodes: "节点", logs: "日志", settings: "设置" };

  function goto(view) {
    if (!TITLES[view]) { view = "dash"; }
    state.view = view;
    $$(".view").forEach(function (node) { node.classList.toggle("is-on", node.dataset.view === view); });
    $$(".nav-item").forEach(function (node) { node.classList.toggle("is-on", node.dataset.goto === view); });
    put("view-title", TITLES[view]);
    app.classList.remove("menu-open");
    if (location.hash.slice(1) !== view) { history.replaceState(null, "", "#" + view); }
    if (view === "nodes") { loadGroups(); }
    if (view === "logs") { loadLogs(); }
    scheduleLogs();
  }

  document.addEventListener("click", function (event) {
    var target = event.target.closest("[data-goto]");
    if (target) { goto(target.dataset.goto); }
  });

  $("#menu").addEventListener("click", function () { app.classList.toggle("menu-open"); });

  // ------------------------------------------------------------ 状态
  function loadStatus() {
    return api("/api/status").then(function (data) {
      state.status = data;
      renderStatus();
      return data;
    }).catch(function (exc) {
      setLink("off", exc.message);
    });
  }

  function setLink(kind, text) {
    var pill = bind("link-state");
    pill.className = "link-state " + kind;
    put("link-text", text);
  }

  function renderStatus() {
    var s = state.status;
    put("panel_version", "控制台 v" + (s.panel_version || "—"));
    put("s-version", s.panel_version || "—");
    put("s-kernel", s.kernel_version || "未获取到");
    put("s-supervisor", s.supervisor_label || "—");
    put("s-dir", s.mihomo_dir || "—");
    put("s-service", s.service_active ? "运行中" : "已停止");

    var service = bind("t-service");
    service.textContent = s.service_active ? "运行中" : (s.kernel_installed ? "已停止" : "未安装");
    service.className = "tile-v " + (s.service_active ? "ok" : "bad");
    put("t-supervisor", (s.supervisor_label || "未接管") + " 接管");

    var apiTile = bind("t-api");
    apiTile.textContent = s.api_ready ? "正常" : "无响应";
    apiTile.className = "tile-v " + (s.api_ready ? "ok" : "warn");
    put("t-port", "端口 " + (s.controller_port || "—"));

    put("t-nodes", (s.nodes || 0) + " / " + (s.groups || 0));
    put("t-version", "内核 " + (s.kernel_version || "—"));
    put("t-port-mixed", s.mixed_port ? String(s.mixed_port) : "—");
    put("t-mode", "模式 " + (s.mode || "—") + (s.tun ? " · TUN 开启" : ""));

    if (s.service_active && s.api_ready) { setLink("on", "内核运行中"); }
    else if (s.service_active) { setLink("off", "控制器无响应"); }
    else { setLink("off", s.kernel_installed ? "内核已停止" : "内核未安装"); }

    var notices = [];
    if (!s.kernel_installed) { notices.push("没有找到内核可执行文件，请用安装脚本先装 mihomo。"); }
    if (s.service_active && !s.api_ready && s.error) { notices.push("控制器 API 读取失败：" + s.error); }
    if (!s.ui_ready) { notices.push("没检测到 Zashboard 静态文件，安装脚本里的「安装/更新 Zashboard」可以补上。"); }
    if (!s.auth_enabled) { notices.push("面板当前没有开启登录密码，任何能访问这个端口的人都能改配置。"); }
    var panel = bind("notice-panel");
    var list = bind("notice-list");
    list.innerHTML = notices.map(function (item) { return "<li>" + esc(item) + "</li>"; }).join("");
    panel.hidden = notices.length === 0;

    put("ui-tip", s.ui_ready
      ? "已就绪，会带上控制器地址和密钥直接打开。"
      : "未检测到 ui/index.html，Zashboard 还没装。");
  }
  // ------------------------------------------------------------ 配置列表
  function usage(info) {
    var used = (Number(info.upload) || 0) + (Number(info.download) || 0);
    var total = Number(info.total) || 0;
    return { used: used, total: total, ratio: total > 0 ? Math.min(1, used / total) : 0 };
  }

  function metaRows(item) {
    var info = item.sub_info || {};
    var use = usage(info);
    var left = daysLeft(info.expire);
    var rows = [];
    if (info.nodes) { rows.push(["节点", info.nodes + " 个"]); }
    if (use.total) { rows.push(["流量", bytes(use.used) + " / " + bytes(use.total)]); }
    else if (use.used) { rows.push(["已用流量", bytes(use.used)]); }
    if (info.expire) {
      rows.push(["到期", stamp(info.expire) + (left === null ? "" : left >= 0 ? "（剩 " + left + " 天）" : "（已过期）")]);
    }
    rows.push(["更新", ago(item.updated_at)]);
    rows.push(["体积", bytes(item.size)]);
    if (item.sub_url) { rows.push(["订阅", item.sub_url.replace(/(:\/\/[^/]+).*/, "$1/…")]); }
    var html = "";
    if (use.total) {
      html += '<div class="meter' + (use.ratio > 0.85 ? " hot" : "") + '"><i style="width:'
        + (use.ratio * 100).toFixed(1) + '%"></i></div>';
    }
    html += '<div class="kv">' + rows.map(function (row) {
      return "<div><span>" + esc(row[0]) + "</span><b>" + esc(row[1]) + "</b></div>";
    }).join("") + "</div>";
    return html;
  }

  function profileCard(item) {
    var tags = [];
    if (item.active) { tags.push('<span class="tag mint">运行中</span>'); }
    tags.push('<span class="tag ' + (item.source === "subscription" ? "indigo" : "") + '">'
      + (item.source === "subscription" ? "订阅" : "YAML") + "</span>");
    var left = daysLeft((item.sub_info || {}).expire);
    if (left !== null && left <= 3) {
      tags.push('<span class="tag ' + (left < 0 ? "red" : "amber") + '">'
        + (left < 0 ? "已过期" : "剩 " + left + " 天") + "</span>");
    }
    return '<article class="card' + (item.active ? " on" : "") + '" data-id="' + esc(item.id) + '">'
      + '<div class="card-top"><span class="card-name">' + esc(item.name) + "</span>" + tags.join("")
      + '<div class="card-acts">'
      + (item.active ? "" : '<button class="solid xs" data-act="activate">启用</button>')
      + '<button class="ghost xs" data-act="edit">编辑</button>'
      + (item.source === "subscription" || item.sub_url ? '<button class="ghost xs" data-act="refresh">刷新订阅</button>' : "")
      + '<button class="ghost xs" data-act="dup">复制</button>'
      + '<button class="ghost xs" data-act="down">下载</button>'
      + (item.active ? "" : '<button class="danger xs" data-act="del">删除</button>')
      + "</div></div>" + metaRows(item) + "</article>";
  }
  function renderProfiles() {
    var box = bind("profile-list");
    var keyword = state.filter.trim().toLowerCase();
    var items = state.profiles.filter(function (item) {
      return !keyword || String(item.name).toLowerCase().indexOf(keyword) >= 0;
    });
    if (!items.length) {
      box.innerHTML = '<div class="panel"><div class="rows"><p class="empty">'
        + (state.profiles.length ? "没有匹配的配置。" : "还没有配置。点「导入订阅」填机场链接，或者「粘贴 YAML」贴一份现成配置。")
        + "</p></div></div>";
    } else {
      box.innerHTML = items.map(profileCard).join("");
    }

    var current = null;
    state.profiles.forEach(function (item) { if (item.active) { current = item; } });
    var card = bind("active-card");
    if (!current) {
      card.innerHTML = '<p class="empty">还没有启用任何配置，先去「配置」导入一份订阅。</p>';
    } else {
      card.innerHTML = '<div class="active-name">' + esc(current.name)
        + '<span class="tag mint">运行中</span></div>' + metaRows(current)
        + '<div class="row acts"><button class="ghost sm" data-act="refresh" data-id="' + esc(current.id)
        + '">刷新订阅</button><button class="ghost sm" data-act="edit" data-id="' + esc(current.id)
        + '">编辑 YAML</button></div>';
    }
  }

  function loadProfiles() {
    return api("/api/profiles").then(function (data) {
      state.profiles = data.profiles || [];
      state.activeId = data.active_id || null;
      renderProfiles();
    }).catch(fail);
  }

  $("#filter").addEventListener("input", function (event) {
    state.filter = event.target.value || "";
    renderProfiles();
  });

  // ------------------------------------------------------------ 策略组
  function delayClass(value) {
    if (!value) { return ""; }
    if (value < 200) { return "fast"; }
    if (value < 500) { return "mid"; }
    return "slow";
  }

  function groupCard(group) {
    var selectable = String(group.type).toLowerCase() === "selector";
    var nodes = group.options.map(function (option) {
      return '<button class="node' + (option.name === group.now ? " on" : "") + '" data-node="'
        + esc(option.name) + '"><span class="node-name">' + esc(option.name) + '</span>'
        + '<span class="node-delay ' + delayClass(option.delay) + '">'
        + (option.delay ? option.delay + " ms" : "—") + "</span></button>";
    }).join("");
    return '<section class="group' + (selectable ? "" : " locked") + '" data-group="' + esc(group.name) + '">'
      + '<header class="group-head"><span class="group-name">' + esc(group.name) + "</span>"
      + '<span class="tag">' + esc(group.type) + "</span>"
      + (selectable ? "" : '<span class="tag amber">自动</span>')
      + '<span class="group-now">' + esc(group.now || "—") + "</span></header>"
      + '<div class="nodes">' + nodes + "</div></section>";
  }
  function loadGroups() {
    var box = bind("group-list");
    return api("/api/proxies").then(function (data) {
      state.groups = data.groups || [];
      if (!state.groups.length) {
        box.innerHTML = '<div class="panel"><div class="rows"><p class="empty">当前配置里没有策略组。</p></div></div>';
        return;
      }
      box.innerHTML = state.groups.map(groupCard).join("");
    }).catch(function (exc) {
      box.innerHTML = '<div class="panel"><div class="rows"><p class="empty">' + esc(exc.message)
        + "</p></div></div>";
    });
  }

  bind("group-list").addEventListener("click", function (event) {
    var button = event.target.closest(".node");
    if (!button) { return; }
    var group = button.closest(".group");
    if (!group || group.classList.contains("locked")) {
      toast("warn", "这个策略组不支持手动选择", "url-test / fallback 由内核按延迟自动决定");
      return;
    }
    busy(button, true);
    api("/api/proxies/" + encodeURIComponent(group.dataset.group), {
      method: "PUT",
      body: { name: button.dataset.node }
    }).then(function (result) {
      report(result, "已切换节点");
      loadGroups();
    }).catch(fail).then(function () { busy(button, false); });
  });

  $("#reload-proxies").addEventListener("click", loadGroups);

  // ------------------------------------------------------------ 日志
  function loadLogs() {
    var lines = $("#log-lines").value || "200";
    return api("/api/logs?lines=" + encodeURIComponent(lines)).then(function (data) {
      var box = bind("log-text");
      var atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
      box.textContent = data.text || "还没有日志输出。";
      put("log-source", "来源：" + (data.source || "—"));
      if (atBottom) { box.scrollTop = box.scrollHeight; }
    }).catch(fail);
  }

  function scheduleLogs() {
    if (state.logTimer) { clearInterval(state.logTimer); state.logTimer = null; }
    if (state.view === "logs" && $("#log-follow").checked) {
      state.logTimer = setInterval(loadLogs, 4000);
    }
  }

  $("#reload-logs").addEventListener("click", loadLogs);
  $("#log-lines").addEventListener("change", loadLogs);
  $("#log-follow").addEventListener("change", scheduleLogs);
  // ------------------------------------------------------------ 弹层
  var openSheets = [];

  function closeTop() {
    var sheet = openSheets.pop();
    if (sheet && sheet.parentNode) { sheet.parentNode.removeChild(sheet); }
    if (!openSheets.length) { scrim.hidden = true; }
  }

  function sheet(html, options) {
    options = options || {};
    var node = document.createElement("div");
    node.className = "sheet" + (options.mid ? " mid" : "");
    node.setAttribute("role", "dialog");
    node.setAttribute("aria-modal", "true");
    node.innerHTML = html;
    document.body.appendChild(node);
    scrim.hidden = false;
    openSheets.push(node);
    node.addEventListener("click", function (event) {
      if (event.target.closest("[data-close]")) { closeTop(); }
    });
    var first = node.querySelector("input, textarea, button");
    if (first) { setTimeout(function () { first.focus(); }, 30); }
    return node;
  }

  scrim.addEventListener("click", closeTop);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && openSheets.length) { closeTop(); }
  });

  function ask(title, text, label, danger) {
    return new Promise(function (resolve) {
      var node = sheet('<div class="sheet-head"><h2>' + esc(title) + "</h2></div>"
        + '<div class="sheet-body"><p class="tip">' + esc(text) + "</p></div>"
        + '<div class="sheet-foot"><span class="spacer"></span>'
        + '<button class="ghost sm" data-close>取消</button>'
        + '<button class="' + (danger ? "danger" : "solid") + ' sm" data-yes>' + esc(label || "确定")
        + "</button></div>", { mid: true });
      node.querySelector("[data-yes]").addEventListener("click", function () {
        closeTop();
        resolve(true);
      });
      node.addEventListener("click", function (event) {
        if (event.target.closest("[data-close]")) { resolve(false); }
      });
    });
  }
  // ------------------------------------------------------------ YAML 编辑器
  var TOKENS = /(#[^\n]*)|("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|\b(true|false|null)\b|\b(\d+(?:\.\d+)?)\b|(^[ \t]*-[ \t])|(^[ \t]*[^\s:#-][^:#\n]*?)(?=:)/gm;

  function highlight(source) {
    var out = "";
    var last = 0;
    var found;
    TOKENS.lastIndex = 0;
    while ((found = TOKENS.exec(source)) !== null) {
      out += esc(source.slice(last, found.index));
      if (found[1]) { out += '<span class="tk-com">' + esc(found[1]) + "</span>"; }
      else if (found[2]) { out += '<span class="tk-str">' + esc(found[2]) + "</span>"; }
      else if (found[3]) { out += '<span class="tk-bool">' + esc(found[3]) + "</span>"; }
      else if (found[4]) { out += '<span class="tk-num">' + esc(found[4]) + "</span>"; }
      else if (found[5]) { out += '<span class="tk-dash">' + esc(found[5]) + "</span>"; }
      else { out += '<span class="tk-key">' + esc(found[6]) + "</span>"; }
      last = found.index + found[0].length;
      if (TOKENS.lastIndex === found.index) { TOKENS.lastIndex += 1; }
    }
    return out + esc(source.slice(last));
  }

  function codeBlock() {
    return '<div class="code"><div class="code-gutter"><div class="code-nums"></div></div>'
      + '<pre class="code-hl" aria-hidden="true"></pre>'
      + '<textarea class="code-input" spellcheck="false" wrap="off" aria-label="YAML 配置"></textarea></div>';
  }

  function wireCode(root, value) {
    var input = $(".code-input", root);
    var layer = $(".code-hl", root);
    var nums = $(".code-nums", root);
    var count = 0;
    input.value = value || "";

    function paint() {
      var text = input.value;
      // 超大配置就不上色了，避免每次输入都重绘几十万字符
      layer.innerHTML = text.length > 160000 ? esc(text) : highlight(text);
      var lines = text.split("\n").length;
      if (lines !== count) {
        count = lines;
        var buffer = [];
        for (var i = 1; i <= lines; i += 1) { buffer.push(i); }
        nums.textContent = buffer.join("\n");
      }
    }

    function sync() {
      layer.scrollTop = input.scrollTop;
      layer.scrollLeft = input.scrollLeft;
      nums.style.transform = "translateY(" + -input.scrollTop + "px)";
    }

    input.addEventListener("input", function () { paint(); sync(); });
    input.addEventListener("scroll", sync);
    input.addEventListener("keydown", function (event) {
      if (event.key !== "Tab") { return; }
      event.preventDefault();
      var at = input.selectionStart;
      input.value = input.value.slice(0, at) + "  " + input.value.slice(input.selectionEnd);
      input.selectionStart = input.selectionEnd = at + 2;
      paint();
    });
    paint();
    return input;
  }
  // ------------------------------------------------------------ 导入订阅
  function subscriptionSheet() {
    var node = sheet('<div class="sheet-head"><h2>导入订阅</h2>'
      + '<button class="ghost sm" data-close>关闭</button></div>'
      + '<div class="sheet-body">'
      + '<label class="field"><span>订阅链接</span>'
      + '<input type="url" name="sub_url" placeholder="https://机场给的订阅地址" required></label>'
      + '<label class="field"><span>配置名称</span>'
      + '<input type="text" name="name" placeholder="例如：A 机场" required></label>'
      + '<div class="grid-2"><label class="field"><span>机场分组名</span>'
      + '<input type="text" name="provider" value="机场节点"></label>'
      + '<label class="field"><span>本地代理端口</span>'
      + '<input type="text" name="port" value="7890" inputmode="numeric"></label></div>'
      + '<div class="grid-2"><label class="field"><span>订阅自动更新（小时）</span>'
      + '<input type="text" name="interval" value="24" inputmode="numeric"></label>'
      + '<div class="field"><span>可选项</span>'
      + '<label class="pick check"><input type="checkbox" name="tun" checked> 开启 TUN 全局接管</label>'
      + '<label class="pick check"><input type="checkbox" name="activate" checked> 导入后立即启用</label>'
      + '<label class="pick check"><input type="checkbox" name="skip"> 跳过内核校验</label></div></div>'
      + '<p class="tip">面板会生成一份保守的可跑配置：分流走 GEOSITE/GEOIP，TUN 用 gvisor 栈，'
      + '订阅缓存按配置隔离存放，切换配置立刻生效。导入后可以随时在编辑器里改。</p>'
      + "</div>"
      + '<div class="sheet-foot"><span class="spacer"></span>'
      + '<button class="ghost sm" data-close>取消</button>'
      + '<button class="solid sm" data-go>导入</button></div>');

    var go = node.querySelector("[data-go]");
    go.addEventListener("click", function () {
      var read = function (name) { return node.querySelector('[name="' + name + '"]'); };
      var url = read("sub_url").value.trim();
      var name = read("name").value.trim();
      if (!/^https?:\/\//i.test(url)) { toast("warn", "订阅链接要以 http:// 或 https:// 开头"); return; }
      if (!name) { toast("warn", "给这份配置起个名字"); return; }
      busy(go, true);
      api("/api/profiles/subscription", {
        method: "POST",
        body: {
          sub_url: url,
          name: name,
          activate: read("activate").checked,
          skip_check: read("skip").checked,
          options: {
            provider_name: read("provider").value.trim() || "机场节点",
            port: parseInt(read("port").value, 10) || 7890,
            interval: Math.max(300, (parseInt(read("interval").value, 10) || 24) * 3600),
            tun: read("tun").checked
          }
        }
      }).then(function (result) {
        closeTop();
        report(result, "订阅已导入");
        refreshAll();
      }).catch(fail).then(function () { busy(go, false); });
    });
  }
  // ------------------------------------------------------------ 粘贴 / 编辑 YAML
  function yamlSheet(profile) {
    var editing = !!profile;
    var node = sheet('<div class="sheet-head"><h2>' + (editing ? "编辑配置" : "粘贴 YAML")
      + '</h2><span class="tag">' + (editing ? esc(profile.id) : "新配置") + "</span>"
      + '<span class="spacer" style="margin-left:auto"></span>'
      + '<button class="ghost sm" data-close>关闭</button></div>'
      + '<div class="sheet-body">'
      + '<div class="grid-2"><label class="field"><span>配置名称</span>'
      + '<input type="text" name="name" value="' + esc(editing ? profile.name : "") + '" required></label>'
      + '<label class="field"><span>订阅链接（改这里会写回 proxy-providers）</span>'
      + '<input type="url" name="sub_url" value="' + esc(editing ? profile.sub_url : "") + '"></label></div>'
      + '<div class="field"><span>YAML 内容</span>' + codeBlock() + "</div>"
      + '<div class="bar"><label class="pick check"><input type="checkbox" name="skip"> 跳过内核校验（订阅或规则下载慢时勾选）</label>'
      + '<label class="pick"><input type="file" name="file" accept=".yaml,.yml,.txt" style="width:auto"></label></div>'
      + "</div>"
      + '<div class="sheet-foot">'
      + (editing ? '<button class="ghost sm" data-download>下载</button>' : "")
      + '<span class="spacer"></span><button class="ghost sm" data-close>取消</button>'
      + '<button class="ghost sm" data-save>保存</button>'
      + '<button class="solid sm" data-save-run>保存并启用</button></div>');

    var input = wireCode(node, editing ? profile.config : DEFAULT_YAML);
    var picker = node.querySelector('[name="file"]');
    picker.addEventListener("change", function () {
      var file = picker.files && picker.files[0];
      if (!file) { return; }
      var reader = new FileReader();
      reader.onload = function () {
        input.value = String(reader.result || "");
        input.dispatchEvent(new Event("input"));
        var nameField = node.querySelector('[name="name"]');
        if (!nameField.value.trim()) { nameField.value = file.name.replace(/\.(ya?ml|txt)$/i, ""); }
      };
      reader.readAsText(file);
    });

    var down = node.querySelector("[data-download]");
    if (down) {
      down.addEventListener("click", function () { location.href = "/api/profiles/" + profile.id + "/download"; });
    }

    function submit(button, activate) {
      var name = node.querySelector('[name="name"]').value.trim();
      var subUrl = node.querySelector('[name="sub_url"]').value.trim();
      var skip = node.querySelector('[name="skip"]').checked;
      if (!name) { toast("warn", "给这份配置起个名字"); return; }
      if (!input.value.trim()) { toast("warn", "YAML 内容是空的"); return; }
      busy(button, true);
      var request = editing
        ? api("/api/profiles/" + profile.id, {
          method: "PUT",
          body: { name: name, raw_yaml: input.value, sub_url: subUrl || null, skip_check: skip, activate: activate }
        })
        : api("/api/profiles/yaml", {
          method: "POST",
          body: { name: name, raw_yaml: input.value, skip_check: skip, activate: activate }
        });
      request.then(function (result) {
        closeTop();
        report(result, "配置已保存");
        refreshAll();
      }).catch(fail).then(function () { busy(button, false); });
    }

    node.querySelector("[data-save]").addEventListener("click", function (event) { submit(event.target, false); });
    node.querySelector("[data-save-run]").addEventListener("click", function (event) { submit(event.target, true); });
  }
  var DEFAULT_YAML = [
    "mixed-port: 7890",
    "allow-lan: true",
    "mode: rule",
    "log-level: info",
    "external-controller: 0.0.0.0:9090",
    "",
    "proxies:",
    "  # 在这里粘贴节点，或者改用「导入订阅」",
    "",
    "proxy-groups:",
    "  - name: 节点选择",
    "    type: select",
    "    proxies: [DIRECT]",
    "",
    "rules:",
    "  - GEOSITE,cn,DIRECT",
    "  - MATCH,节点选择",
    ""
  ].join("\n");

  // ------------------------------------------------------------ 卡片动作
  var ACTIONS = {
    activate: function (id, button) {
      toast("", "正在切换配置", "会先用内核校验，再尽量热重载，失败会自动回滚");
      busy(button, true);
      return api("/api/profiles/" + id + "/activate", { method: "POST" })
        .then(function (result) { report(result, "已切换配置"); })
        .catch(fail).then(function () { busy(button, false); refreshAll(); });
    },
    refresh: function (id, button) {
      busy(button, true);
      return api("/api/profiles/" + id + "/refresh", { method: "POST" })
        .then(function (result) { report(result, "订阅已刷新"); loadProfiles(); })
        .catch(fail).then(function () { busy(button, false); });
    },
    dup: function (id, button) {
      busy(button, true);
      return api("/api/profiles/" + id + "/duplicate", { method: "POST" })
        .then(function (result) { report(result, "已复制"); loadProfiles(); })
        .catch(fail).then(function () { busy(button, false); });
    },
    down: function (id) { location.href = "/api/profiles/" + id + "/download"; },
    edit: function (id, button) {
      busy(button, true);
      return api("/api/profiles/" + id).then(yamlSheet).catch(fail)
        .then(function () { busy(button, false); });
    },
    del: function (id, button) {
      var item = null;
      state.profiles.forEach(function (row) { if (row.id === id) { item = row; } });
      return ask("删除配置", "「" + ((item && item.name) || id) + "」会连同它的机场缓存一起删掉，无法恢复。",
        "删除", true).then(function (yes) {
        if (!yes) { return null; }
        busy(button, true);
        return api("/api/profiles/" + id, { method: "DELETE" })
          .then(function (result) { report(result, "已删除"); loadProfiles(); })
          .catch(fail).then(function () { busy(button, false); });
      });
    }
  };

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-act]");
    if (!button) { return; }
    var holder = button.closest("[data-id]");
    var id = button.dataset.id || (holder ? holder.dataset.id : "");
    var action = ACTIONS[button.dataset.act];
    if (id && action) { action(id, button); }
  });
  // ------------------------------------------------------------ 全局动作
  function refreshAll() {
    loadStatus();
    loadProfiles();
    if (state.view === "nodes") { loadGroups(); }
  }

  $("#add-sub").addEventListener("click", subscriptionSheet);
  $("#add-yaml").addEventListener("click", function () { yamlSheet(null); });
  $("#reload-all").addEventListener("click", function (event) {
    busy(event.target, true);
    Promise.all([loadStatus(), loadProfiles()]).then(function () { busy(event.target, false); });
  });

  $("#restart-kernel").addEventListener("click", function (event) {
    var button = event.target;
    ask("重启内核", "重启期间代理会中断几秒，正在下载的连接会断开。", "重启内核").then(function (yes) {
      if (!yes) { return; }
      busy(button, true);
      api("/api/restart", { method: "POST" })
        .then(function (result) { report(result, "内核已重启"); })
        .catch(fail).then(function () { busy(button, false); refreshAll(); });
    });
  });

  $$("[data-service]").forEach(function (button) {
    button.addEventListener("click", function () {
      busy(button, true);
      api("/api/service", { method: "POST", body: { action: button.dataset.service } })
        .then(function (result) { report(result, "已执行"); })
        .catch(fail).then(function () { busy(button, false); refreshAll(); });
    });
  });

  $("#pw-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var form = event.target;
    var button = form.querySelector("button[type=submit]");
    busy(button, true);
    api("/api/password", {
      method: "POST",
      body: { old: form.old.value, new: form.new.value }
    }).then(function (result) {
      report(result, "密码已更新");
      form.reset();
    }).catch(fail).then(function () { busy(button, false); });
  });

  $("#logout").addEventListener("click", function () {
    api("/api/logout", { method: "POST" }).then(function () { location.replace("/login"); }).catch(fail);
  });

  $("#open-ui").addEventListener("click", function () {
    var info = state.controller || {};
    if (!info.port) { toast("warn", "还没读到控制器地址", "内核没有运行或控制器 API 无响应"); return; }
    if (!info.ui_ready) { toast("warn", "Zashboard 还没安装", "用安装脚本的「安装/更新 Zashboard」补上即可"); return; }
    var base = location.protocol + "//" + location.hostname + ":" + info.port + "/ui/";
    window.open(base + "?hostname=" + encodeURIComponent(location.hostname)
      + "&port=" + info.port + "&secret=" + encodeURIComponent(info.secret || "") + "#/", "_blank");
  });
  // ------------------------------------------------------------ 实时流量
  var samples = [];
  var peak = 0;
  var socket = null;
  var retry = null;

  function prepare(canvas) {
    var ratio = window.devicePixelRatio || 1;
    var w = canvas.clientWidth || canvas.width;
    var h = canvas.clientHeight || canvas.height;
    if (canvas.width !== Math.floor(w * ratio) || canvas.height !== Math.floor(h * ratio)) {
      canvas.width = Math.floor(w * ratio);
      canvas.height = Math.floor(h * ratio);
    }
    var ctx = canvas.getContext && canvas.getContext("2d");
    if (!ctx) { return null; }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx: ctx, w: w, h: h };
  }

  function series(view, index, max, color, alpha) {
    var ctx = view.ctx;
    var count = samples.length;
    var step = count > 1 ? view.w / (count - 1) : view.w;
    ctx.beginPath();
    for (var i = 0; i < count; i += 1) {
      var x = i * step;
      var y = view.h - 2 - (samples[i][index] / max) * (view.h - 8);
      if (i === 0) { ctx.moveTo(x, y); } else { ctx.lineTo(x, y); }
    }
    if (count > 1) {
      ctx.lineTo(view.w, view.h);
      ctx.lineTo(0, view.h);
      ctx.closePath();
      var fill = ctx.createLinearGradient(0, 0, 0, view.h);
      fill.addColorStop(0, color + alpha);
      fill.addColorStop(1, color + "00");
      ctx.fillStyle = fill;
      ctx.fill();
    }
    ctx.beginPath();
    for (var j = 0; j < count; j += 1) {
      var px = j * step;
      var py = view.h - 2 - (samples[j][index] / max) * (view.h - 8);
      if (j === 0) { ctx.moveTo(px, py); } else { ctx.lineTo(px, py); }
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.6;
    ctx.lineJoin = "round";
    ctx.stroke();
  }
  function ceiling() {
    var top = 64 * 1024;
    samples.forEach(function (row) { top = Math.max(top, row[0], row[1]); });
    return top * 1.15;
  }

  function paint(canvas, grid) {
    if (!canvas || !canvas.clientWidth) { return; }
    var view = prepare(canvas);
    if (!view) { return; }
    var max = ceiling();
    if (grid) {
      view.ctx.strokeStyle = "rgba(255,255,255,.05)";
      view.ctx.lineWidth = 1;
      for (var i = 1; i <= 3; i += 1) {
        var y = Math.round(view.h / 4 * i) + 0.5;
        view.ctx.beginPath();
        view.ctx.moveTo(0, y);
        view.ctx.lineTo(view.w, y);
        view.ctx.stroke();
      }
      view.ctx.fillStyle = "#5A6875";
      view.ctx.font = '11px ui-monospace, Menlo, Consolas, monospace';
      view.ctx.fillText(rate(max), 6, 14);
    }
    if (!samples.length) { return; }
    series(view, 1, max, "#6E8BFF", "38");
    series(view, 0, max, "#37E5B5", "3A");
  }

  function paintAll() {
    paint($("#spark"), false);
    if (state.view === "dash") { paint($("#flow"), true); }
  }

  function pushSample(up, down) {
    samples.push([Number(up) || 0, Number(down) || 0]);
    if (samples.length > 150) { samples.shift(); }
    peak = Math.max(peak, Number(up) || 0, Number(down) || 0);
    put("rate-up", rate(up));
    put("rate-down", rate(down));
    put("rate-up-2", rate(up));
    put("rate-down-2", rate(down));
    put("rate-peak", rate(peak));
    paintAll();
  }

  window.addEventListener("resize", paintAll);
  function stopSocket() {
    if (retry) { clearTimeout(retry); retry = null; }
    if (socket) {
      socket.onclose = null;
      try { socket.close(); } catch (exc) { /* 已经断了 */ }
      socket = null;
    }
  }

  function later() {
    if (retry) { return; }
    retry = setTimeout(function () { retry = null; openSocket(); }, 6000);
  }

  function openSocket() {
    stopSocket();
    api("/api/controller").then(function (info) {
      state.controller = info;
      if (!info.port) { put("flow-note", "控制器端口未知"); later(); return; }
      var proto = location.protocol === "https:" ? "wss:" : "ws:";
      var url = proto + "//" + location.hostname + ":" + info.port + "/traffic";
      if (info.secret) { url += "?token=" + encodeURIComponent(info.secret); }
      var ws = new WebSocket(url);
      socket = ws;
      ws.onopen = function () { put("flow-note", "已连接控制器 :" + info.port); };
      ws.onmessage = function (event) {
        try {
          var data = JSON.parse(event.data);
          pushSample(data.up, data.down);
        } catch (exc) { /* 忽略非 JSON 帧 */ }
      };
      ws.onclose = function () {
        if (socket === ws) { socket = null; }
        put("flow-note", "控制器连接已断开，正在重连");
        later();
      };
      ws.onerror = function () { put("flow-note", "无法连接控制器 :" + info.port); };
    }).catch(function () { later(); });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { stopSocket(); } else { openSocket(); refreshAll(); }
  });
  // ------------------------------------------------------------ 启动
  function boot() {
    api("/api/session").then(function (session) {
      if (session.auth_enabled && !session.logged_in) {
        location.replace("/login");
        return;
      }
      $("#logout").hidden = !session.auth_enabled;
      app.hidden = false;
      goto((location.hash || "").slice(1) || "dash");
      refreshAll();
      openSocket();
      setInterval(function () { if (!document.hidden) { loadStatus(); } }, 7000);
      setInterval(function () { if (!document.hidden && state.view === "profiles") { loadProfiles(); } }, 30000);
    }).catch(function (exc) {
      app.hidden = false;
      fail(exc);
    });
  }

  window.addEventListener("hashchange", function () {
    var view = (location.hash || "").slice(1);
    if (view && view !== state.view) { goto(view); }
  });

  boot();














})();
