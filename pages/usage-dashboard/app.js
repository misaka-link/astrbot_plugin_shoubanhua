(() => {
  "use strict";

  const state = {
    view: "overview",
    userPage: 1,
    groupPage: 1,
    eventsPage: 1,

    // Configuration
    configRevision: null,
    rawConfig: null,
    parameterFields: [],
    parameterModes: [],
    settingsMetadata: [],
    isConfigDirty: false,
    isSavingConfig: false,

    // Sensitive configuration
    sensitiveState: {
      generic_api_keys: { configured: false, count: 0 },
      proxy_url: { configured: false, write_only: false }
    },
    isUpdatingSensitive: false,

    // Presets (independent view & state)
    presetsRevision: null,
    rawPresets: [],
    isPresetsDirty: false,
    isSavingPresets: false,
    presetSearch: "",

    privacyMasks: {
      userAvatar: false,
      userName: false,
      userId: false,
      groupAvatar: false,
      groupName: false,
      groupId: false,
      balance: false
    }
  };

  const bridge = window.AstrBotPluginPage;
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const text = (value, fallback = "-") => (value === null || value === undefined || value === "") ? fallback : String(value);

  function formatRouteChannel(apiRoute, endpointType) {
    const route = String(apiRoute || "").trim();
    const endpoint = String(endpointType || "").trim();
    if (route === "generic" && endpoint) return endpoint;
    return [route, endpoint].filter(Boolean).join(" / ");
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "'": "&#39;",
      '"': "&quot;"
    })[char]);
  }

  function setStatus(message = "", type = "info") {
    const element = $("#status");
    if (!element) return;
    if (!message) {
      element.textContent = "";
      element.className = "status-bar";
      return;
    }
    element.textContent = message;
    element.className = `status-bar ${type}`;
  }

  function rangeParams(extra = {}) {
    const start = $("#start-date")?.value || "";
    const end = $("#end-date")?.value || "";
    return { ...(start ? { start } : {}), ...(end ? { end } : {}), ...extra };
  }

  function eventsParams(extra = {}) {
    // 事件列表独立筛选：选择了日期时只看当天，否则跟随顶部时间范围
    const day = $("#events-day")?.value || "";
    const base = day ? { start: day, end: day } : rangeParams();
    return { ...base, ...extra };
  }

  async function requestBridge(method, endpoint, payload) {
    if (!bridge) {
      throw new Error("AstrBot 插件页面通信桥接不可用，请在 AstrBot 管理面板中运行。");
    }
    const fn = method === "GET" ? bridge.apiGet : bridge.apiPost;
    if (typeof fn !== "function") {
      throw new Error(`桥接方法 ${method === "GET" ? "apiGet" : "apiPost"} 不存在`);
    }

    let response;
    try {
      response = await (method === "GET" ? fn.call(bridge, endpoint, payload) : fn.call(bridge, endpoint, payload));
    } catch (err) {
      throw new Error(err.message || "请求执行异常");
    }

    let data = response;
    // Support if response is a Fetch Response instance or response-like object
    if (response && typeof response.json === "function") {
      const status = response.status;
      let json = {};
      try {
        json = await response.json();
      } catch {
        json = {};
      }
      if (!response.ok || Number(status) >= 400) {
        const error = new Error(json?.message || json?.error || `请求失败 (HTTP ${status})`);
        error.status = status;
        error.data = json;
        throw error;
      }
      data = json;
    }

    if (data && (data.ok === false || (data.status && Number(data.status) >= 400))) {
      const error = new Error(data.message || data.error || `请求失败 (${data.status})`);
      error.status = Number(data.status);
      error.data = data;
      throw error;
    }

    return data;
  }

  function get(endpoint, params = {}) {
    return requestBridge("GET", endpoint, params);
  }

  function post(endpoint, body = {}) {
    return requestBridge("POST", endpoint, body);
  }

  function cell(value, className = "") {
    const td = document.createElement("td");
    td.textContent = text(value);
    if (className) td.className = className;
    return td;
  }

  function updatePrivacyMask(mask) {
    document.querySelectorAll(`[data-privacy-mask="${mask}"]`).forEach((element) => {
      element.classList.toggle("is-privacy-masked", state.privacyMasks[mask]);
      element.setAttribute("aria-pressed", String(state.privacyMasks[mask]));
    });
  }

  function makePrivacyToggle(element, mask, label) {
    element.dataset.privacyMask = mask;
    element.classList.add("privacy-toggle");
    element.tabIndex = 0;
    element.setAttribute("role", "button");
    element.title = `双击或按 Enter/空格可切换${label}模糊显示`;
    element.setAttribute("aria-label", `切换${label}模糊显示`);
    element.setAttribute("aria-pressed", String(state.privacyMasks[mask]));
    element.classList.toggle("is-privacy-masked", state.privacyMasks[mask]);

    const toggle = () => {
      state.privacyMasks[mask] = !state.privacyMasks[mask];
      updatePrivacyMask(mask);
    };

    element.addEventListener("dblclick", (event) => {
      event.preventDefault();
      toggle();
    });
    element.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      toggle();
    });
    return element;
  }

  function privacyValue(value, mask, label, className = "span") {
    const element = document.createElement(className);
    element.textContent = text(value);
    return makePrivacyToggle(element, mask, label);
  }

  function fmtYuan(milli) {
    // 后端金额以「厘」(1 厘 = 0.001 元) 整数存储，这里换算为元并去掉尾零
    const value = (Number(milli) || 0) / 1000;
    return String(Number(value.toFixed(3)));
  }

  function balanceCell(value, className = "text-right font-mono") {
    const td = document.createElement("td");
    td.className = className;
    td.append(privacyValue(fmtYuan(value), "balance", "余额", "span"));
    return td;
  }

  function emptyRow(target, colspan, message = "暂无数据") {
    if (!target) return;
    target.replaceChildren();
    const row = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = colspan;
    td.className = "table-empty";
    td.textContent = message;
    row.append(td);
    target.append(row);
  }

  function fallbackAvatar(className, content, mask, label) {
    const badge = document.createElement("div");
    badge.className = className;
    badge.textContent = content;
    return makePrivacyToggle(badge, mask, label);
  }

  function imageAvatar(url, fallback, mask, label) {
    const image = document.createElement("img");
    image.className = "entity-avatar";
    image.alt = "";
    image.loading = "lazy";
    image.src = url;
    makePrivacyToggle(image, mask, label);
    image.onerror = () => image.replaceWith(fallback());
    return image;
  }

  function avatar(user) {
    const wrapper = document.createElement("div");
    wrapper.className = "entity-cell";

    const userAvatarUrl = user.avatar_url || user.user_avatar_url || `https://q1.qlogo.cn/g?b=qq&nk=${encodeURIComponent(user.user_id)}&s=100`;
    const image = imageAvatar(
      userAvatarUrl,
      () => fallbackAvatar("entity-avatar font-mono", "QQ", "userAvatar", "用户头像"),
      "userAvatar",
      "用户头像"
    );

    const info = document.createElement("div");
    info.className = "entity-info";

    const name = document.createElement("span");
    name.className = "entity-name";
    name.textContent = user.nickname || user.user_nickname || `QQ ${user.user_id}`;
    makePrivacyToggle(name, "userName", "用户昵称");

    const id = document.createElement("span");
    id.className = "entity-id";
    id.textContent = user.user_id;
    makePrivacyToggle(id, "userId", "QQ 号");

    info.append(name, id);
    wrapper.append(image, info);
    return wrapper;
  }

  function groupAvatarUrl(group) {
    const providedUrl = group.group_avatar_url;
    if (providedUrl) return providedUrl;
    const groupId = String(group.group_id || "").trim();
    return /^\d+$/.test(groupId) ? `https://p.qlogo.cn/gh/${groupId}/${groupId}/100` : "";
  }

  function displayGroup(group) {
    const wrapper = document.createElement("div");
    wrapper.className = "entity-cell";

    const groupAvatar = groupAvatarUrl(group);
    const visual = groupAvatar
      ? imageAvatar(
        groupAvatar,
        () => fallbackAvatar("entity-group-badge", "群", "groupAvatar", "群头像"),
        "groupAvatar",
        "群头像"
      )
      : fallbackAvatar("entity-group-badge", "群", "groupAvatar", "群头像");

    const info = document.createElement("div");
    info.className = "entity-info";

    const name = document.createElement("span");
    name.className = "entity-name";
    name.textContent = group.name || group.group_name || `群 ${group.group_id}`;
    makePrivacyToggle(name, "groupName", "群昵称");

    const id = document.createElement("span");
    id.className = "entity-id";
    id.textContent = group.group_id;
    makePrivacyToggle(id, "groupId", "QQ 群号");

    info.append(name, id);
    wrapper.append(visual, info);
    return wrapper;
  }

  function metric(label, value, theme = "theme-teal", hint = "") {
    const card = document.createElement("article");
    card.className = `metric-card ${theme}`;

    const title = document.createElement("span");
    title.className = "metric-label";
    title.textContent = label;

    const number = document.createElement("strong");
    number.className = "metric-value";
    number.textContent = text(value, "0");

    card.append(title, number);
    if (hint) {
      const hintEl = document.createElement("span");
      hintEl.className = "metric-hint";
      hintEl.textContent = hint;
      card.append(hintEl);
    }
    return card;
  }

  // --- Overview ---
  async function loadOverview() {
    setStatus("正在获取概览与账本数据...");
    const overview = await get("usage/overview", rangeParams());

    const summary = overview.summary || {};
    $("#metrics").replaceChildren(
      metric("成功输出", summary.successful_outputs, "theme-blue", "生成完成数"),
      metric("本次消耗", fmtYuan(summary.charged_amount) + " 元", "theme-amber", "已消耗金额"),
      metric("失败扣费", fmtYuan(summary.failed_charged_amount) + " 元", "theme-rose", "失败且未返还"),
      metric("LLM 工具免计费", summary.unbilled_llm_outputs, "theme-teal", "插件免计费调用")
    );

    renderTrend(overview.trend || []);
    renderModels(overview.models || []);
    await loadEvents();
    setStatus("");
  }

  async function loadEvents() {
    const data = await get("usage/events", eventsParams({
      outcome: $("#events-outcome")?.value || "",
      page: state.eventsPage,
      page_size: Number($("#events-page-size")?.value) || 15,
    }));
    renderEvents(data.items || []);
    pagination($("#events-pagination"), data, (page) => {
      state.eventsPage = page;
      loadEvents().catch(handleError);
    });
  }

  function renderTrend(items) {
    const target = $("#trend");
    if (!target) return;
    target.replaceChildren();
    if (!items.length) {
      target.className = "trend-container is-empty";
      target.textContent = "所选时间范围内暂无趋势统计数据";
      return;
    }

    target.className = "trend-container";
    const value = (item, key) => Math.max(0, Number(item[key]) || 0);
    const peak = Math.max(...items.map((item) => Math.max(value(item, "outputs"), value(item, "charged_amount"))), 1);
    const chartHeight = 180;
    const padding = { top: 16, right: 16, bottom: 30, left: 30 };
    const chartWidth = Math.max(360, items.length * 56 + padding.left + padding.right);
    const plotWidth = chartWidth - padding.left - padding.right;
    const plotHeight = chartHeight - padding.top - padding.bottom;
    const xAt = (index) => items.length === 1
      ? padding.left + plotWidth / 2
      : padding.left + (plotWidth * index) / (items.length - 1);
    const yAt = (amount) => padding.top + plotHeight - (amount / peak) * plotHeight;
    const svgElement = (name) => document.createElementNS("http://www.w3.org/2000/svg", name);

    const chart = svgElement("svg");
    chart.classList.add("trend-chart");
    chart.setAttribute("viewBox", `0 0 ${chartWidth} ${chartHeight}`);
    chart.setAttribute("role", "img");
    chart.setAttribute("aria-labelledby", "trend-chart-title trend-chart-description");
    chart.style.minWidth = `${chartWidth}px`;

    const title = svgElement("title");
    title.id = "trend-chart-title";
    title.textContent = "每日成功输出与本次消耗走势";
    const description = svgElement("desc");
    description.id = "trend-chart-description";
    description.textContent = `统计 ${items.length} 个日期，从 ${items[0].date} 至 ${items[items.length - 1].date}。`;
    chart.append(title, description);

    for (let step = 0; step <= 4; step += 1) {
      const y = padding.top + (plotHeight * step) / 4;
      const grid = svgElement("line");
      grid.classList.add("trend-grid-line");
      grid.setAttribute("x1", String(padding.left));
      grid.setAttribute("x2", String(chartWidth - padding.right));
      grid.setAttribute("y1", String(y));
      grid.setAttribute("y2", String(y));
      chart.append(grid);
    }

    const buildPoints = (key) => items.map((item, index) => `${xAt(index)},${yAt(value(item, key))}`).join(" ");
    const outputLine = svgElement("polyline");
    outputLine.classList.add("trend-line", "is-output");
    outputLine.setAttribute("points", buildPoints("outputs"));
    const chargeLine = svgElement("polyline");
    chargeLine.classList.add("trend-line", "is-charge");
    chargeLine.setAttribute("points", buildPoints("charged_amount"));
    chart.append(outputLine, chargeLine);

    const labelInterval = Math.max(1, Math.ceil(items.length / 8));
    for (const [index, item] of items.entries()) {
      const x = xAt(index);
      const outputs = value(item, "outputs");
      const chargedAmount = value(item, "charged_amount");
      const point = svgElement("g");
      point.classList.add("trend-point");
      const pointTitle = svgElement("title");
      pointTitle.textContent = `${item.date}\n成功输出: ${outputs}\n本次消耗: ${fmtYuan(chargedAmount)} 元`;

      const outputMarker = svgElement("circle");
      outputMarker.classList.add("trend-marker", "is-output");
      outputMarker.setAttribute("cx", String(x));
      outputMarker.setAttribute("cy", String(yAt(outputs)));
      outputMarker.setAttribute("r", "3.5");

      const chargeMarker = svgElement("rect");
      chargeMarker.classList.add("trend-marker", "is-charge");
      chargeMarker.setAttribute("x", String(x - 3));
      chargeMarker.setAttribute("y", String(yAt(chargedAmount) - 3));
      chargeMarker.setAttribute("width", "6");
      chargeMarker.setAttribute("height", "6");
      chargeMarker.setAttribute("rx", "1");

      point.append(pointTitle, outputMarker, chargeMarker);
      chart.append(point);

      if (index % labelInterval === 0 || index === items.length - 1) {
        const dateLabel = svgElement("text");
        dateLabel.classList.add("trend-axis-label");
        dateLabel.setAttribute("x", String(x));
        dateLabel.setAttribute("y", String(chartHeight - 9));
        dateLabel.setAttribute("text-anchor", "middle");
        dateLabel.textContent = String(item.date || "").slice(5);
        chart.append(dateLabel);
      }
    }

    target.append(chart);
  }

  function renderModels(items) {
    const target = $("#model-breakdown");
    if (!target) return;
    if (!items.length) return emptyRow(target, 5, "暂无模型调用数据");
    target.replaceChildren();

    for (const item of items) {
      const row = document.createElement("tr");

      const modelTd = document.createElement("td");
      const modelBadge = document.createElement("span");
      modelBadge.className = "badge badge-code";
      modelBadge.textContent = text(item.actual_model);
      modelTd.append(modelBadge);

      const routeTd = document.createElement("td");
      const routeText = formatRouteChannel(item.api_route, item.endpoint_type);
      if (routeText) {
        const routeBadge = document.createElement("span");
        routeBadge.className = "badge badge-route";
        routeBadge.textContent = routeText;
        routeTd.append(routeBadge);
      } else {
        routeTd.textContent = "-";
      }

      row.append(
        modelTd,
        routeTd,
        cell(item.outputs, "text-right font-mono"),
        cell(fmtYuan(item.charged_amount), "text-right font-mono"),
        cell(item.attempts, "text-right font-mono")
      );
      target.append(row);
    }
  }

  function renderEvents(items) {
    const target = $("#recent-events");
    if (!target) return;
    if (!items.length) return emptyRow(target, 10, "暂无最近活动记录");
    target.replaceChildren();

    for (const item of items) {
      const row = document.createElement("tr");

      // 1. Time
      const timeStr = item.occurred_at ? item.occurred_at.replace("T", " ").slice(0, 19) : "-";
      const timeTd = cell(timeStr, "font-mono");

      // 2. User
      const userTd = document.createElement("td");
      if (item.user_id) {
        userTd.append(avatar(item));
      } else {
        userTd.textContent = "-";
      }

      // 3. Group
      const groupTd = document.createElement("td");
      if (item.group_id) {
        groupTd.append(displayGroup(item));
      } else {
        groupTd.textContent = "-";
      }

      // 4. Event / Source
      const eventTd = document.createElement("td");
      const sourceText = item.source ? `[${item.source}] ` : "";
      const kindText = item.event_kind || "generation";
      eventTd.textContent = `${sourceText}${kindText}`;
      eventTd.className = "font-mono";

      // 5. Model (Logical -> Actual)
      const modelTd = document.createElement("td");
      const logical = item.logical_model || "";
      const actual = item.actual_model || "";
      if (logical && actual && logical !== actual) {
        modelTd.innerHTML = `<span class="badge badge-code">${escapeHtml(logical)}</span> → <span class="badge badge-code">${escapeHtml(actual)}</span>`;
      } else if (actual || logical) {
        const badge = document.createElement("span");
        badge.className = "badge badge-code";
        badge.textContent = actual || logical;
        modelTd.append(badge);
      } else {
        modelTd.textContent = "-";
      }

      // 6. Route Channel
      const routeTd = document.createElement("td");
      const routeText = formatRouteChannel(item.api_route, item.endpoint_type);
      if (routeText) {
        const routeBadge = document.createElement("span");
        routeBadge.className = "badge badge-route";
        routeBadge.textContent = routeText;
        routeTd.append(routeBadge);
      } else {
        routeTd.textContent = "-";
      }

      // 7. Outcome & Status
      const outcomeTd = document.createElement("td");
      const outcomeVal = String(item.outcome || "-").toLowerCase();
      const outcomeBadge = document.createElement("span");
      const httpStatus = Number(item.http_status);

      if (outcomeVal === "success" || outcomeVal === "成功") {
        outcomeBadge.className = "badge badge-success";
        outcomeBadge.textContent = "成功";
      } else if (outcomeVal === "applied" || outcomeVal === "已应用") {
        outcomeBadge.className = "badge badge-applied";
        outcomeBadge.textContent = "已应用";
      } else if (outcomeVal === "imported" || outcomeVal === "历史导入") {
        outcomeBadge.className = "badge badge-info";
        outcomeBadge.textContent = "历史导入";
      } else if (outcomeVal === "adjustment" || outcomeVal === "额度调整") {
        outcomeBadge.className = "badge badge-applied";
        outcomeBadge.textContent = "额度调整";
      } else if (outcomeVal === "checkin" || outcomeVal === "签到") {
        outcomeBadge.className = "badge badge-info";
        outcomeBadge.textContent = "签到";
      } else if (outcomeVal === "skipped" || outcomeVal === "免扣费") {
        outcomeBadge.className = "badge badge-neutral";
        outcomeBadge.textContent = "免扣费";
      } else {
        outcomeBadge.className = "badge badge-error";
        outcomeBadge.textContent = item.outcome || "失败";
      }
      outcomeTd.append(outcomeBadge);
      if (httpStatus && httpStatus > 0) {
        const statusSpan = document.createElement("span");
        statusSpan.className = "badge badge-code font-mono";
        statusSpan.style.marginLeft = "4px";
        statusSpan.textContent = String(httpStatus);
        outcomeTd.append(statusSpan);
      }

      // 8. Output count
      const outputTd = cell(item.output_count !== undefined && item.output_count !== null ? item.output_count : "-", "text-right font-mono");

      // 9. Charged amount
      const chargeTd = cell(item.charged_amount !== undefined && item.charged_amount !== null ? fmtYuan(item.charged_amount) : "-", "text-right font-mono");

      // 10. Balance Delta
      const deltaTd = document.createElement("td");
      deltaTd.className = "text-right font-mono";
      const deltaVal = Number(item.balance_delta);
      let deltaText = "-";
      if (Number.isNaN(deltaVal) || item.balance_delta === null || item.balance_delta === undefined) {
        deltaText = "-";
      } else if (deltaVal > 0) {
        deltaTd.className += " delta-positive";
        deltaText = `+${fmtYuan(deltaVal)}`;
      } else if (deltaVal < 0) {
        deltaTd.className += " delta-negative";
        deltaText = fmtYuan(deltaVal);
      } else {
        deltaTd.className += " delta-zero";
        deltaText = "0";
      }
      deltaTd.append(privacyValue(deltaText, "balance", "余额", "span"));

      if (item.resulting_balance !== null && item.resulting_balance !== undefined) {
        const resSpan = document.createElement("span");
        resSpan.className = "metric-hint font-mono";
        resSpan.style.display = "block";
        resSpan.textContent = `余: ${fmtYuan(item.resulting_balance)}`;
        makePrivacyToggle(resSpan, "balance", "余额");
        deltaTd.append(resSpan);
      }

      row.append(
        timeTd,
        userTd,
        groupTd,
        eventTd,
        modelTd,
        routeTd,
        outcomeTd,
        outputTd,
        chargeTd,
        deltaTd
      );
      if (item.note) {
        row.title = `备注: ${item.note}`;
      }
      target.append(row);
    }
  }

  // --- Users & Groups ---
  function pagination(target, data, onPage) {
    if (!target) return;
    target.replaceChildren();
    const totalPages = Math.ceil(data.total / data.page_size) || 1;

    const summary = document.createElement("span");
    summary.className = "pagination-info";
    summary.textContent = `第 ${data.page} / ${totalPages} 页 · 共 ${data.total} 条`;

    const previous = document.createElement("button");
    previous.className = "button secondary button-sm";
    previous.textContent = "上一页";
    previous.disabled = data.page <= 1;
    previous.onclick = () => onPage(data.page - 1);

    const next = document.createElement("button");
    next.className = "button secondary button-sm";
    next.textContent = "下一页";
    next.disabled = data.page * data.page_size >= data.total;
    next.onclick = () => onPage(data.page + 1);

    target.append(summary, previous, next);
  }

  async function loadUsers() {
    setStatus("正在加载用户用量...");
    const data = await get("usage/users", rangeParams({
      search: $("#user-search")?.value.trim() || "",
      page: state.userPage,
      page_size: 30
    }));

    const target = $("#users-table");
    if (!data.items.length) {
      emptyRow(target, 5, "未找到符合条件的用户记录");
    } else {
      target.replaceChildren();
      for (const item of data.items) {
        const row = document.createElement("tr");

        const personTd = document.createElement("td");
        personTd.append(avatar(item));

        const actionTd = document.createElement("td");
        actionTd.className = "text-center";
        const actionBtn = document.createElement("button");
        actionBtn.className = "button secondary button-sm";
        actionBtn.type = "button";
        actionBtn.textContent = "调整余额";
        actionBtn.onclick = () => openAdjust("user", item.user_id, item.nickname ? `${item.nickname} (${item.user_id})` : `QQ ${item.user_id}`);
        actionTd.append(actionBtn);

        row.append(
          personTd,
          cell(item.outputs, "text-right font-mono"),
          cell(fmtYuan(item.charged_amount), "text-right font-mono"),
          balanceCell(item.balance),
          actionTd
        );
        target.append(row);
      }
    }

    pagination($("#users-pagination"), data, (page) => {
      state.userPage = page;
      loadUsers().catch(handleError);
    });
    setStatus("");
  }

  async function loadGroups() {
    setStatus("正在加载群组用量...");
    const data = await get("usage/groups", rangeParams({
      search: $("#group-search")?.value.trim() || "",
      page: state.groupPage,
      page_size: 30
    }));

    const target = $("#groups-table");
    if (!data.items.length) {
      emptyRow(target, 6, "未找到符合条件的群组记录");
    } else {
      target.replaceChildren();
      for (const item of data.items) {
        const row = document.createElement("tr");

        const groupTd = document.createElement("td");
        groupTd.append(displayGroup(item));

        const actionTd = document.createElement("td");
        actionTd.className = "text-center";
        const actionBtn = document.createElement("button");
        actionBtn.className = "button secondary button-sm";
        actionBtn.type = "button";
        actionBtn.textContent = "调整余额";
        actionBtn.onclick = () => openAdjust("group", item.group_id, item.name ? `${item.name} (${item.group_id})` : `群 ${item.group_id}`);
        actionTd.append(actionBtn);

        row.append(
          groupTd,
          cell(item.outputs, "text-right font-mono"),
          cell(fmtYuan(item.charged_amount), "text-right font-mono"),
          cell(item.active_users, "text-right font-mono"),
          balanceCell(item.balance),
          actionTd
        );
        target.append(row);
      }
    }

    pagination($("#groups-pagination"), data, (page) => {
      state.groupPage = page;
      loadGroups().catch(handleError);
    });
    setStatus("");
  }

  // --- Presets View Management (Independent Tab & Storage) ---
  function markPresetsDirty(dirty = true) {
    state.isPresetsDirty = dirty;
    const dot = $("#presets-dirty-icon");
    const label = $("#presets-dirty-text");
    const badge = $("#presets-dirty-badge");

    if (dirty) {
      if (dot) dot.className = "status-indicator-dot is-dirty";
      if (label) label.textContent = "有未保存的预设更改";
      if (badge) badge.hidden = false;
    } else {
      if (dot) dot.className = "status-indicator-dot is-synced";
      if (label) label.textContent = "预设已与服务器同步";
      if (badge) badge.hidden = true;
    }
  }

  function addPresetRow(preset = {}, prepend = false) {
    const table = $("#presets-table-body");
    if (!table) return;

    table.querySelector("tr.table-empty")?.remove();

    const row = document.createElement("tr");
    row.className = "preset-data-row";
    row.dataset.legacyAlias = preset.legacy_alias || "";
    row.innerHTML = `
      <td><input class="preset-command font-mono" value="${escapeHtml(preset.command || "")}" placeholder="指令名，如 粘土人" required aria-label="预设指令名"></td>
      <td><textarea class="preset-prompt font-mono" rows="2" placeholder="专属系统提示词..." required aria-label="专属系统提示词">${escapeHtml(preset.prompt || "")}</textarea></td>
      <td class="text-center"><button class="icon-button remove-row" type="button" title="移除预设" aria-label="移除预设">✕</button></td>
    `;

    row.querySelector(".remove-row").onclick = () => {
      row.remove();
      filterPresetsDisplay();
      markPresetsDirty(true);
    };

    row.querySelectorAll("input, textarea").forEach((el) => {
      el.addEventListener("input", () => {
        filterPresetsDisplay();
        markPresetsDirty(true);
      });
    });

    if (prepend && table.firstChild) {
      table.insertBefore(row, table.firstChild);
    } else {
      table.append(row);
    }
  }

  function filterPresetsDisplay() {
    const query = (state.presetSearch || "").toLowerCase().trim();
    const rows = $$("#presets-table-body tr.preset-data-row");
    let visibleCount = 0;

    for (const row of rows) {
      const cmd = (row.querySelector(".preset-command")?.value || "").toLowerCase();
      const prm = (row.querySelector(".preset-prompt")?.value || "").toLowerCase();
      const match = !query || cmd.includes(query) || prm.includes(query);
      row.hidden = !match;
      if (match) visibleCount++;
    }

    const emptyNotice = $("#presets-empty-notice");
    if (visibleCount === 0 && rows.length > 0) {
      if (!emptyNotice) {
        const tr = document.createElement("tr");
        tr.id = "presets-empty-notice";
        tr.innerHTML = `<td colspan="3" class="table-empty">未找到匹配 "${escapeHtml(state.presetSearch)}" 的预设条目</td>`;
        $("#presets-table-body")?.append(tr);
      }
    } else if (emptyNotice) {
      emptyNotice.remove();
    }
  }

  function collectPresets() {
    const rows = $$("#presets-table-body tr.preset-data-row");
    return rows.map((row) => {
      const preset = {
        command: row.querySelector(".preset-command")?.value.trim() || "",
        prompt: row.querySelector(".preset-prompt")?.value.trim() || "",
      };
      if (row.dataset.legacyAlias) {
        preset.legacy_alias = row.dataset.legacyAlias;
      }
      return preset;
    }).filter((p) => p.command || p.prompt);
  }

  async function loadPresets() {
    setStatus("正在获取预设提示词列表...");
    $("#presets-conflict-alert").hidden = true;
    const response = await get("presets");

    state.presetsRevision = response.revision;
    state.rawPresets = response.presets || [];

    const badge = $("#presets-revision-badge");
    if (badge) badge.textContent = `rev: ${state.presetsRevision || "init"}`;

    const table = $("#presets-table-body");
    if (table) {
      table.replaceChildren();
      if (!state.rawPresets.length) {
        emptyRow(table, 3, "暂无预设提示词，点击右上角【+ 添加预设】创建");
      } else {
        for (const p of state.rawPresets) {
          addPresetRow(p);
        }
      }
    }

    filterPresetsDisplay();
    markPresetsDirty(false);
    setStatus("");
  }

  async function savePresets() {
    if (state.isSavingPresets) return;
    state.isSavingPresets = true;
    const saveBtn = $("#presets-save-btn");
    if (saveBtn) {
      saveBtn.disabled = true;
      saveBtn.textContent = "保存中...";
    }

    try {
      setStatus("正在保存预设到服务器...");
      const presetsPayload = collectPresets();
      const response = await post("presets", {
        revision: state.presetsRevision,
        presets: presetsPayload,
      });

      state.presetsRevision = response.revision;
      if (response.presets) {
        state.rawPresets = response.presets;
      }
      const badge = $("#presets-revision-badge");
      if (badge) badge.textContent = `rev: ${state.presetsRevision || "ok"}`;
      markPresetsDirty(false);
      $("#presets-conflict-alert").hidden = true;
      setStatus(response.message || "预设已成功保存并即时生效。", "success");
    } catch (error) {
      if (error.status === 409 || (error.message && error.message.includes("409")) || (error.message && error.message.includes("其他页面修改"))) {
        $("#presets-conflict-alert").hidden = false;
        setStatus("预设保存冲突：服务器端预设已被其他页面修改，请重新加载。", "error");
      } else {
        handleError(error);
      }
    } finally {
      state.isSavingPresets = false;
      if (saveBtn) {
        saveBtn.disabled = false;
        saveBtn.innerHTML = `<span class="btn-symbol">💾</span> 保存预设`;
      }
    }
  }

  // --- Sensitive Configuration (Keys & Credential Proxy) ---
  function parseKeyList(rawText) {
    if (!rawText) return [];
    return rawText
      .split(/[\n,;\s]+/)
      .map((k) => k.trim())
      .filter(Boolean);
  }

  async function loadSensitiveConfig() {
    try {
      const response = await get("configuration/sensitive");
      if (response.revision) {
        state.configRevision = response.revision;
        const badge = $("#config-revision-badge");
        if (badge) badge.textContent = `rev: ${state.configRevision || "init"}`;
      }
      if (response.sensitive) {
        state.sensitiveState = response.sensitive;
      }
      renderSensitiveStatus();
    } catch (err) {
      console.warn("获取敏感配置状态失败:", err);
    }
  }

  function renderSensitiveStatus() {
    const sensitive = state.sensitiveState || {};

    // Keys Status
    const keysState = sensitive.generic_api_keys || {};
    const keysBadge = $("#sensitive-keys-badge");
    const keysStateText = $("#sensitive-keys-state");
    const keysCountText = $("#sensitive-keys-count");

    if (keysStateText) {
      keysStateText.textContent = keysState.configured ? "已配置" : "未配置";
    }
    if (keysCountText) {
      keysCountText.textContent = `${keysState.count || 0} 个 Key`;
    }
    keysBadge?.classList.toggle("is-configured", !!keysState.configured);

    const serviceUrlState = sensitive.generic_api_url || {};
    const serviceUrlCard = $("#sensitive-service-url-card");
    const serviceUrlBadge = $("#sensitive-service-url-badge");
    const serviceUrlStateText = $("#sensitive-service-url-state");
    const genericUrlGroup = $("#config-generic-url-group");
    if (serviceUrlCard) {
      serviceUrlCard.hidden = false;
    }
    if (genericUrlGroup) {
      genericUrlGroup.hidden = !!serviceUrlState.write_only;
    }
    const credentialedServiceUrlConfigured = !!serviceUrlState.write_only && !!serviceUrlState.configured;
    if (serviceUrlStateText) {
      serviceUrlStateText.textContent = credentialedServiceUrlConfigured ? "已配置" : "未配置";
    }
    serviceUrlBadge?.classList.toggle("is-configured", credentialedServiceUrlConfigured);

    // Proxy credentials are always write-only, so expose a replacement entry even
    // before a normal proxy URL is upgraded to an authenticated one.
    const proxyState = sensitive.proxy_url || {};
    const proxyCard = $("#sensitive-proxy-card");
    const proxyBadge = $("#sensitive-proxy-badge");
    const proxyStateText = $("#sensitive-proxy-state");

    if (proxyCard) {
      proxyCard.hidden = false;
    }
    if (proxyStateText) {
      proxyStateText.textContent = proxyState.write_only && proxyState.configured
        ? "已配置"
        : "未配置";
    }
    proxyBadge?.classList.toggle(
      "is-configured",
      !!proxyState.write_only && !!proxyState.configured,
    );
  }

  async function mutateSensitive(target, action, extraPayload = {}) {
    if (state.isUpdatingSensitive) return;
    state.isUpdatingSensitive = true;

    const sensitiveButtons = $$(".sensitive-btn-row .button");
    sensitiveButtons.forEach((b) => (b.disabled = true));

    try {
      setStatus("正在更新安全密钥凭据...");
      const payload = {
        revision: state.configRevision,
        target,
        action,
        ...extraPayload,
      };

      const response = await post("configuration/sensitive", payload);

      if (response.revision) {
        state.configRevision = response.revision;
        const badge = $("#config-revision-badge");
        if (badge) badge.textContent = `rev: ${state.configRevision || "ok"}`;
      }

      if (response.sensitive) {
        state.sensitiveState = response.sensitive;
      }

      // Immediately blank typed secret inputs in the DOM
      if (target === "generic_api_keys") {
        const input = $("#sensitive-keys-input");
        if (input) input.value = "";
      } else if (target === "generic_api_url") {
        const input = $("#sensitive-service-url-input");
        if (input) input.value = "";
      } else if (target === "proxy_url") {
        const input = $("#sensitive-proxy-input");
        if (input) input.value = "";
      }

      renderSensitiveStatus();
      $("#config-conflict-alert").hidden = true;
      setStatus(response.message || "密钥凭据已安全更新。", "success");
    } catch (error) {
      if (error.status === 409 || (error.message && error.message.includes("409")) || (error.message && error.message.includes("其他页面修改"))) {
        $("#config-conflict-alert").hidden = false;
        setStatus("敏感配置更新冲突：服务器配置已被修改，请重新加载。", "error");
      } else {
        handleError(error);
      }
    } finally {
      state.isUpdatingSensitive = false;
      sensitiveButtons.forEach((b) => (b.disabled = false));
    }
  }

  // --- Dynamic Settings Section Management ---
  function renderDynamicSettings(settingsMetadata = [], currentValues = {}) {
    const container = $("#config-dynamic-settings");
    if (!container) return;
    container.replaceChildren();

    // Filter out write_only settings (e.g. proxy_url when write_only)
    const validSettings = settingsMetadata.filter((s) => !s.write_only && s.key !== "generic_api_url");
    if (!validSettings.length) {
      container.innerHTML = `<p class="metric-hint">暂无其他可配置项</p>`;
      return;
    }

    // Group settings by setting.group
    const groupsMap = new Map();
    for (const setting of validSettings) {
      const gName = setting.group || "常规设置";
      if (!groupsMap.has(gName)) groupsMap.set(gName, []);
      groupsMap.get(gName).push(setting);
    }

    for (const [groupName, groupSettings] of groupsMap.entries()) {
      const panel = document.createElement("div");
      panel.className = "settings-group-panel";

      const header = document.createElement("div");
      header.className = "settings-group-header";
      const title = document.createElement("h3");
      title.className = "settings-group-title";
      title.textContent = groupName.replace(/^[【\[](.*?)[】\]]/, "$1");
      header.append(title);
      panel.append(header);

      const grid = document.createElement("div");
      grid.className = "settings-fields-grid";

      for (const setting of groupSettings) {
        const item = document.createElement("div");
        const val = currentValues[setting.key] !== undefined ? currentValues[setting.key] : setting.default;
        const type = (setting.type || "string").toLowerCase();

        const reloadBadgeHtml = setting.reload_required ? `<span class="badge badge-reload" title="修改此项需要重载插件后生效">需重载</span>` : "";

        if (type === "bool" || type === "boolean") {
          item.className = "setting-item is-bool";
          const inputId = `setting_${setting.key}`;
          item.innerHTML = `
            <input type="checkbox" id="${inputId}" data-setting-key="${setting.key}"${val ? " checked" : ""}>
            <label for="${inputId}" class="setting-label-block">
              <div class="field-label-row">
                <span class="field-label">${escapeHtml(setting.label || setting.key)}</span>
                ${reloadBadgeHtml}
              </div>
              ${setting.hint ? `<span class="setting-hint">${escapeHtml(setting.hint)}</span>` : ""}
            </label>
          `;
        } else if (type === "int" || type === "integer" || type === "number") {
          item.className = "setting-item";
          item.innerHTML = `
            <div class="field-label-row">
              <label class="field-label">${escapeHtml(setting.label || setting.key)}</label>
              ${reloadBadgeHtml}
            </div>
            <input class="field-input font-mono" type="number" data-setting-key="${setting.key}"
              value="${val ?? setting.default ?? 0}"
              ${setting.min !== undefined ? `min="${setting.min}"` : ""}
              ${setting.max !== undefined ? `max="${setting.max}"` : ""}>
            ${setting.hint ? `<span class="setting-hint">${escapeHtml(setting.hint)}</span>` : ""}
          `;
        } else if (setting.options && Array.isArray(setting.options) && setting.options.length > 0) {
          item.className = "setting-item";
          const optsHtml = setting.options.map((opt) =>
            `<option value="${escapeHtml(opt)}"${String(opt) === String(val) ? " selected" : ""}>${escapeHtml(opt)}</option>`
          ).join("");
          item.innerHTML = `
            <div class="field-label-row">
              <label class="field-label">${escapeHtml(setting.label || setting.key)}</label>
              ${reloadBadgeHtml}
            </div>
            <select class="field-select font-mono" data-setting-key="${setting.key}">
              ${optsHtml}
            </select>
            ${setting.hint ? `<span class="setting-hint">${escapeHtml(setting.hint)}</span>` : ""}
          `;
        } else if (type === "text") {
          item.className = "setting-item";
          item.innerHTML = `
            <div class="field-label-row">
              <label class="field-label">${escapeHtml(setting.label || setting.key)}</label>
              ${reloadBadgeHtml}
            </div>
            <textarea class="field-textarea font-mono" rows="3" data-setting-key="${setting.key}">${escapeHtml(val ?? setting.default ?? "")}</textarea>
            ${setting.hint ? `<span class="setting-hint">${escapeHtml(setting.hint)}</span>` : ""}
          `;
        } else if (type === "list") {
          item.className = "setting-item";
          const listTextVal = Array.isArray(val) ? val.join("\n") : (val || "");
          item.innerHTML = `
            <div class="field-label-row">
              <label class="field-label">${escapeHtml(setting.label || setting.key)} <span class="setting-hint">(每行一项)</span></label>
              ${reloadBadgeHtml}
            </div>
            <textarea class="field-textarea font-mono" rows="3" data-setting-key="${setting.key}" placeholder="每行一项...">${escapeHtml(listTextVal)}</textarea>
            ${setting.hint ? `<span class="setting-hint">${escapeHtml(setting.hint)}</span>` : ""}
          `;
        } else {
          // Default string
          item.className = "setting-item";
          item.innerHTML = `
            <div class="field-label-row">
              <label class="field-label">${escapeHtml(setting.label || setting.key)}</label>
              ${reloadBadgeHtml}
            </div>
            <input class="field-input font-mono" type="text" data-setting-key="${setting.key}"
              value="${escapeHtml(val ?? setting.default ?? "")}">
            ${setting.hint ? `<span class="setting-hint">${escapeHtml(setting.hint)}</span>` : ""}
          `;
        }

        grid.append(item);
      }

      panel.append(grid);
      container.append(panel);
    }
  }

  function collectDynamicSettings() {
    const settingsObj = {};
    const settingsMap = new Map();
    for (const s of state.settingsMetadata || []) {
      settingsMap.set(s.key, s);
    }

    const els = $$("#config-dynamic-settings [data-setting-key]");
    for (const el of els) {
      const key = el.dataset.settingKey;
      const meta = settingsMap.get(key);
      const type = (meta?.type || "string").toLowerCase();

      if (el.type === "checkbox") {
        settingsObj[key] = el.checked;
      } else if (type === "int" || type === "integer" || type === "number") {
        settingsObj[key] = Number(el.value);
      } else if (type === "list") {
        settingsObj[key] = el.value
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean);
      } else {
        settingsObj[key] = el.value;
      }
    }
    return settingsObj;
  }

  // --- Configuration Management & Routing Sync ---
  function markConfigDirty(dirty = true) {
    state.isConfigDirty = dirty;
    const dot = $("#config-dirty-icon");
    const label = $("#config-dirty-text");
    const badge = $("#config-dirty-badge");

    if (dirty) {
      if (dot) dot.className = "status-indicator-dot is-dirty";
      if (label) label.textContent = "有未保存的配置更改";
      if (badge) badge.hidden = false;
    } else {
      if (dot) dot.className = "status-indicator-dot is-synced";
      if (label) label.textContent = "配置已与服务器同步";
      if (badge) badge.hidden = true;
    }
  }

  function getModelCatalogue() {
    const list = $$("#config-models-table .route-model")
      .map((input) => input.value.trim())
      .filter(Boolean);
    if (!list.length && state.rawConfig?.model_list) {
      return state.rawConfig.model_list;
    }
    return [...new Set(list)];
  }

  function modelOptions(models, currentValue, includeAll = false) {
    const choices = includeAll ? ["ALL", ...models] : [...models];
    if (currentValue && !choices.includes(currentValue)) {
      choices.push(currentValue);
    }
    return choices.map((model) =>
      `<option value="${escapeHtml(model)}"${model === currentValue ? " selected" : ""}>${escapeHtml(model)}${model === currentValue && !models.includes(model) && model !== "ALL" ? " (已移除)" : ""}</option>`
    ).join("");
  }

  function updateModelReferences(previousName, nextName) {
    if (!previousName || !nextName || previousName === nextName) return;
    const defaultSelect = $("#config-default-model");
    if (defaultSelect?.value === previousName) {
      defaultSelect.dataset.pendingModelReference = nextName;
    }
    $$(".binding-model, .mapping-source, .mapping-target, .template-model, .param-model-select").forEach((select) => {
      if (select.value === previousName) {
        select.dataset.pendingModelReference = nextName;
      }
    });
    $$(".param-card").forEach((card) => {
      if (card._entryData?.model === previousName) {
        card._entryData = { ...card._entryData, model: nextName };
      }
    });
  }

  function syncAllModelDropdowns() {
    const models = getModelCatalogue();
    const defaultSelect = $("#config-default-model");
    const curDefault = defaultSelect ? defaultSelect.value : "";

    if (defaultSelect) {
      const reference = defaultSelect.dataset.pendingModelReference || curDefault;
      defaultSelect.innerHTML = modelOptions(models, reference);
      delete defaultSelect.dataset.pendingModelReference;
    }

    // Keep unavailable references visible. The backend can then reject them
    // explicitly instead of silently replacing them with the first model.
    $$("#config-bindings-table .binding-model").forEach((select) => {
      const reference = select.dataset.pendingModelReference || select.value;
      select.innerHTML = modelOptions(models, reference);
      delete select.dataset.pendingModelReference;
    });
    $$("#config-mappings-table .mapping-source, #config-mappings-table .mapping-target").forEach((select) => {
      const reference = select.dataset.pendingModelReference || select.value;
      select.innerHTML = modelOptions(models, reference);
      delete select.dataset.pendingModelReference;
    });
    $$("#config-templates-table .template-model").forEach((select) => {
      const reference = select.dataset.pendingModelReference || select.value;
      select.innerHTML = modelOptions(models, reference, true);
      delete select.dataset.pendingModelReference;
    });
    $$(".param-model-select").forEach((select) => {
      const reference = select.dataset.pendingModelReference || select.value;
      select.innerHTML = modelOptions(models, reference);
      delete select.dataset.pendingModelReference;
    });
  }

  function getModelRouteCapabilities(modelName) {
    const targetName = (modelName || "").trim();
    const rows = $$("#config-models-table tr");
    let modelRow = null;

    for (const r of rows) {
      const nameInput = r.querySelector(".route-model");
      if (nameInput && nameInput.value.trim() === targetName) {
        modelRow = r;
        break;
      }
    }

    let isGemini = false;
    let isChat = false;
    let isGenerations = false;
    let isEdits = false;

    if (modelRow) {
      isGemini = !!modelRow.querySelector(".route-gemini")?.checked;
      isChat = !!modelRow.querySelector(".route-chat")?.checked;
      isGenerations = !!modelRow.querySelector(".route-generations")?.checked;
      isEdits = !!modelRow.querySelector(".route-edits")?.checked;
    } else if (state.rawConfig) {
      isGemini = !!state.rawConfig.gemini_model_list?.includes(targetName);
      isChat = !!state.rawConfig.chat_completions_model_list?.includes(targetName);
      isGenerations = !!state.rawConfig.images_generations_model_list?.includes(targetName);
      isEdits = !!state.rawConfig.images_edits_model_list?.includes(targetName);
    }

    const isGeneric = isChat || isGenerations || isEdits || (!isGemini);

    const endpoints = new Set();
    if (isGemini) {
      endpoints.add("gemini_generate_content");
    }
    if (isChat) {
      endpoints.add("chat_completions");
    }
    if (isGenerations) {
      endpoints.add("images_generations");
    }
    if (isEdits) {
      endpoints.add("images_edits");
    }

    return {
      isGemini,
      isGeneric,
      isChat,
      isGenerations,
      isEdits,
      endpoints,
    };
  }

  function isFieldApplicable(field, routeCaps) {
    const route = (field.route || "any").toLowerCase();

    // 1. Route check
    if (route === "gemini" && !routeCaps.isGemini) {
      return false;
    }
    if (route === "generic" && !routeCaps.isGeneric) {
      return false;
    }

    // 2. Endpoint types check
    const endpointTypes = Array.isArray(field.endpoint_types) ? field.endpoint_types : [];
    if (!endpointTypes.length) {
      return true;
    }

    return endpointTypes.some((ep) => routeCaps.endpoints.has(ep));
  }

  function addModelRow(name = "", flags = {}) {
    const table = $("#config-models-table");
    if (!table) return;

    const row = document.createElement("tr");
    row.innerHTML = `
      <td><input class="route-model font-mono" value="${escapeHtml(name)}" placeholder="如 gemini-2.5-flash" aria-label="模型名称" required></td>
      <td class="text-center"><input class="route-gemini" type="checkbox" aria-label="使用 Gemini 路由"${flags.gemini ? " checked" : ""}></td>
      <td class="text-center"><input class="route-chat" type="checkbox" aria-label="使用 Chat Completions 路由"${flags.chat ? " checked" : ""}></td>
      <td class="text-center"><input class="route-generations" type="checkbox" aria-label="使用 Images Generations 路由"${flags.generations ? " checked" : ""}></td>
      <td class="text-center"><input class="route-edits" type="checkbox" aria-label="使用 Images Edits 路由"${flags.edits ? " checked" : ""}></td>
      <td class="text-center"><button class="icon-button remove-row" type="button" title="移除模型" aria-label="移除模型">✕</button></td>
    `;

    // Mutual exclusivity between Gemini and Generic checkboxes
    const geminiCb = row.querySelector(".route-gemini");
    const genericCbs = [row.querySelector(".route-chat"), row.querySelector(".route-generations"), row.querySelector(".route-edits")];

    geminiCb.addEventListener("change", () => {
      if (geminiCb.checked) {
        genericCbs.forEach((cb) => (cb.checked = false));
      }
      reRenderAllParameterCards();
      markConfigDirty();
    });

    genericCbs.forEach((cb) => {
      cb.addEventListener("change", () => {
        if (cb.checked) {
          geminiCb.checked = false;
        }
        reRenderAllParameterCards();
        markConfigDirty();
      });
    });

    const modelInput = row.querySelector(".route-model");
    let previousModelName = modelInput.value.trim();
    modelInput.addEventListener("input", () => {
      const nextModelName = modelInput.value.trim();
      updateModelReferences(previousModelName, nextModelName);
      previousModelName = nextModelName;
      syncAllModelDropdowns();
      reRenderAllParameterCards();
      markConfigDirty();
    });

    row.querySelector(".remove-row").onclick = () => {
      row.remove();
      syncAllModelDropdowns();
      reRenderAllParameterCards();
      markConfigDirty();
    };

    table.append(row);
  }

  function addPrefixTag(prefixText) {
    const list = $("#config-prefixes-list");
    if (!list) return;
    const tag = document.createElement("div");
    tag.className = "prefix-tag";
    tag.dataset.prefix = prefixText;
    tag.innerHTML = `
      <span>${escapeHtml(prefixText)}</span>
      <button class="prefix-tag-del" type="button" title="删除触发词" aria-label="删除触发词 ${escapeHtml(prefixText)}">✕</button>
    `;
    tag.querySelector(".prefix-tag-del").onclick = () => {
      tag.remove();
      markConfigDirty();
    };
    list.append(tag);
  }

  function addBindingRow(binding = {}) {
    const models = getModelCatalogue();
    const row = document.createElement("tr");
    const modelOpts = models.map((m) =>
      `<option value="${escapeHtml(m)}"${m === binding.model ? " selected" : ""}>${escapeHtml(m)}</option>`
    ).join("");

    row.innerHTML = `
      <td><input class="binding-command font-mono" value="${escapeHtml(binding.command || "")}" placeholder="如 手办化 / anime" required aria-label="触发指令"></td>
      <td><select class="binding-model font-mono" aria-label="生效模型">${modelOpts}</select></td>
      <td class="text-center"><button class="icon-button remove-row" type="button" title="移除绑定" aria-label="移除绑定">✕</button></td>
    `;
    row.querySelector(".remove-row").onclick = () => {
      row.remove();
      markConfigDirty();
    };
    row.querySelectorAll("input, select").forEach((el) => el.addEventListener("change", () => markConfigDirty()));
    $("#config-bindings-table")?.append(row);
  }

  function addMappingRow(mapping = {}) {
    const models = getModelCatalogue();
    const sourceOpts = models.map((m) =>
      `<option value="${escapeHtml(m)}"${m === mapping.model ? " selected" : ""}>${escapeHtml(m)}</option>`
    ).join("");
    const targetOpts = models.map((m) =>
      `<option value="${escapeHtml(m)}"${m === mapping.mapped_model ? " selected" : ""}>${escapeHtml(m)}</option>`
    ).join("");

    const row = document.createElement("tr");
    row.innerHTML = `
      <td><select class="mapping-source font-mono" aria-label="源模型">${sourceOpts}</select></td>
      <td><select class="mapping-target font-mono" aria-label="降级目标模型">${targetOpts}</select></td>
      <td><input class="mapping-priority font-mono" type="number" min="-1" max="10000" value="${Number(mapping.priority ?? 0)}" aria-label="优先权重"></td>
      <td class="text-center"><button class="icon-button remove-row" type="button" title="移除映射" aria-label="移除映射">✕</button></td>
    `;
    row.querySelector(".remove-row").onclick = () => {
      row.remove();
      markConfigDirty();
    };
    row.querySelectorAll("input, select").forEach((el) => el.addEventListener("change", () => markConfigDirty()));
    $("#config-mappings-table")?.append(row);
  }

  function addTemplateRow(template = {}) {
    const models = getModelCatalogue();
    const opts = ["ALL", ...models];
    const modelOpts = opts.map((m) =>
      `<option value="${escapeHtml(m)}"${m === template.model ? " selected" : ""}>${escapeHtml(m)}</option>`
    ).join("");

    const row = document.createElement("tr");
    row.innerHTML = `
      <td><select class="template-model font-mono" aria-label="适用模型">${modelOpts}</select></td>
      <td><textarea class="template-prompt font-mono" rows="2" placeholder="提示词模板，使用 {prompt} 占位符..." required aria-label="模板内容">${escapeHtml(template.prompt_template || "")}</textarea></td>
      <td class="text-center"><button class="icon-button remove-row" type="button" title="移除模板" aria-label="移除模板">✕</button></td>
    `;
    row.querySelector(".remove-row").onclick = () => {
      row.remove();
      markConfigDirty();
    };
    row.querySelectorAll("input, select, textarea").forEach((el) => el.addEventListener("input", () => markConfigDirty()));
    $("#config-templates-table")?.append(row);
  }

  // --- Parameter Cards with Filtering & depends_on Support ---
  function getParameterModes() {
    const modes = state.parameterModes || [];
    if (modes.length) return modes;
    return [
      { value: "none", label: "无厂商参数" },
      { value: "gpt", label: "GPT" },
      { value: "gemini", label: "Gemini" },
      { value: "grok", label: "Grok" },
      { value: "seedream", label: "Seedream" },
    ];
  }

  function normalizeParameterMode(value) {
    const mode = String(value || "none");
    return getParameterModes().some((item) => item.value === mode) ? mode : "none";
  }

  function getCardCurrentValues(card) {
    const raw = { ...(card._entryData || {}) };
    const modelSelect = card.querySelector(".param-model-select");
    const modeSelect = card.querySelector(".param-mode-select");
    if (modelSelect) {
      raw.model = modelSelect.value;
    }
    if (modeSelect) {
      raw.parameter_mode = normalizeParameterMode(modeSelect.value);
    }
    card.querySelectorAll("[data-field]").forEach((el) => {
      const fieldName = el.dataset.field;
      if (el.type === "checkbox") {
        raw[fieldName] = el.checked;
      } else if (el.type === "number") {
        raw[fieldName] = Number(el.value);
      } else {
        raw[fieldName] = el.value;
      }
    });
    return raw;
  }

  function updateCardBody(card) {
    const currentData = getCardCurrentValues(card);
    currentData.parameter_mode = normalizeParameterMode(currentData.parameter_mode);
    card._entryData = currentData;

    const modelSelected = currentData.model || "";
    const selectedMode = currentData.parameter_mode;
    const routeCaps = getModelRouteCapabilities(modelSelected);
    const allFields = state.parameterFields || [];

    // Base/quota fields stay visible; vendor fields follow the selected mode
    // and retain the existing endpoint capability checks.
    const applicableFields = allFields.filter((field) => {
      const fieldMode = field.parameter_mode || "base";
      if (fieldMode === "mode_switch") return false;
      if (fieldMode === "base") return true;
      return fieldMode === selectedMode && isFieldApplicable(field, routeCaps);
    });

    // Group fields by field.group
    const groupsMap = new Map();
    for (const field of applicableFields) {
      const gName = field.group || "基础参数";
      if (!groupsMap.has(gName)) groupsMap.set(gName, []);
      groupsMap.get(gName).push(field);
    }

    const body = card.querySelector(".param-card-body") || document.createElement("div");
    body.className = "param-card-body";
    body.replaceChildren();

    if (!applicableFields.length) {
      body.innerHTML = `<p class="metric-hint">该模型所属通道暂无可配置参数</p>`;
      return;
    }

    const fieldElementsMap = new Map();

    for (const [groupName, groupFields] of groupsMap.entries()) {
      const section = document.createElement("div");
      section.className = "param-group-section";

      const gTitle = document.createElement("h4");
      gTitle.className = "param-group-title";
      gTitle.textContent = groupName;
      section.append(gTitle);

      const grid = document.createElement("div");
      grid.className = "param-fields-grid";

      for (const field of groupFields) {
        const item = document.createElement("div");
        const val = currentData[field.name] !== undefined ? currentData[field.name] : field.default;
        const fieldType = (field.type || "string").toLowerCase();

        let inputEl;

        if (fieldType === "bool" || fieldType === "boolean") {
          item.className = "param-field-item is-boolean";
          const inputId = `param_${Math.random().toString(36).slice(2, 8)}_${field.name}`;
          inputEl = document.createElement("input");
          inputEl.type = "checkbox";
          inputEl.id = inputId;
          inputEl.dataset.field = field.name;
          inputEl.checked = !!val;

          const labelEl = document.createElement("label");
          labelEl.htmlFor = inputId;
          labelEl.className = "field-label";
          labelEl.textContent = field.label || field.name;
          if (field.hint) labelEl.title = field.hint;

          item.append(inputEl, labelEl);
        } else if (fieldType === "int" || fieldType === "integer" || fieldType === "number") {
          item.className = "param-field-item";
          const labelEl = document.createElement("label");
          labelEl.className = "field-label";
          labelEl.textContent = field.label || field.name;
          if (field.hint) labelEl.title = field.hint;

          inputEl = document.createElement("input");
          inputEl.className = "field-input font-mono";
          inputEl.type = "number";
          inputEl.dataset.field = field.name;
          inputEl.value = val ?? field.default ?? 0;
          if (field.min !== undefined) inputEl.min = String(field.min);
          if (field.max !== undefined) inputEl.max = String(field.max);

          item.append(labelEl, inputEl);
        } else if (fieldType === "select" || (field.options && Array.isArray(field.options) && field.options.length > 0)) {
          item.className = "param-field-item";
          const labelEl = document.createElement("label");
          labelEl.className = "field-label";
          labelEl.textContent = field.label || field.name;
          if (field.hint) labelEl.title = field.hint;

          inputEl = document.createElement("select");
          inputEl.className = "field-select font-mono";
          inputEl.dataset.field = field.name;
          inputEl.innerHTML = (field.options || []).map((opt) =>
            `<option value="${escapeHtml(opt)}"${String(opt) === String(val) ? " selected" : ""}>${escapeHtml(opt)}</option>`
          ).join("");

          item.append(labelEl, inputEl);
        } else {
          item.className = "param-field-item";
          const labelEl = document.createElement("label");
          labelEl.className = "field-label";
          labelEl.textContent = field.label || field.name;
          if (field.hint) labelEl.title = field.hint;

          inputEl = document.createElement("input");
          inputEl.className = "field-input font-mono";
          inputEl.type = "text";
          inputEl.dataset.field = field.name;
          inputEl.value = String(val ?? field.default ?? "");
          if (field.max_length) inputEl.maxLength = field.max_length;

          item.append(labelEl, inputEl);
        }

        fieldElementsMap.set(field.name, { field, inputEl, container: item });
        grid.append(item);
      }

      section.append(grid);
      body.append(section);
    }

    // Apply depends_on logic: disable/enable dependent controls based on parent checkbox
    for (const { field, inputEl } of fieldElementsMap.values()) {
      if (field.depends_on) {
        const dependency = typeof field.depends_on === "string"
          ? { field: field.depends_on, equals: true }
          : field.depends_on;
        const parentEntry = fieldElementsMap.get(dependency.field);
        const updateDisabled = () => {
          const parentValue = parentEntry
            ? (parentEntry.inputEl.type === "checkbox" ? parentEntry.inputEl.checked : parentEntry.inputEl.value)
            : dependency.equals;
          inputEl.disabled = parentValue !== dependency.equals;
        };

        updateDisabled();
        if (parentEntry?.inputEl) {
          parentEntry.inputEl.addEventListener("change", updateDisabled);
        }
      }
    }

    card.querySelectorAll("input, select").forEach((el) => {
      el.addEventListener("change", () => markConfigDirty());
      el.addEventListener("input", () => markConfigDirty());
    });
  }

  function renderModelParameterCard(paramEntry = {}) {
    const models = getModelCatalogue();
    const modelSelected = paramEntry.model || (models[0] || "");

    const card = document.createElement("div");
    card.className = "param-card";
    card._entryData = { ...paramEntry, model: modelSelected };

    // Header with model selector & delete button
    const header = document.createElement("div");
    header.className = "param-card-header";

    const titleGroup = document.createElement("div");
    titleGroup.className = "param-card-title-group";
    titleGroup.innerHTML = `
      <span class="field-label">配置模型:</span>
      <select class="param-model-select font-mono" aria-label="选择要配置的模型">
        ${models.map((m) => `<option value="${escapeHtml(m)}"${m === modelSelected ? " selected" : ""}>${escapeHtml(m)}</option>`).join("")}
      </select>
    `;

    const modeGroup = document.createElement("label");
    modeGroup.className = "param-mode-control";
    modeGroup.innerHTML = `<span class="field-label">参数模式</span>`;
    const modeSelect = document.createElement("select");
    modeSelect.className = "param-mode-select field-select font-mono";
    modeSelect.setAttribute("aria-label", "选择厂商参数模式");
    const selectedMode = normalizeParameterMode(paramEntry.parameter_mode);
    modeSelect.innerHTML = getParameterModes().map((mode) =>
      `<option value="${escapeHtml(mode.value)}"${mode.value === selectedMode ? " selected" : ""}>${escapeHtml(mode.label || mode.value)}</option>`
    ).join("");
    modeGroup.append(modeSelect);

    const removeBtn = document.createElement("button");
    removeBtn.className = "icon-button";
    removeBtn.type = "button";
    removeBtn.title = "移除该模型参数";
    removeBtn.setAttribute("aria-label", "移除该模型参数");
    removeBtn.textContent = "✕";
    removeBtn.onclick = () => {
      card.remove();
      markConfigDirty();
    };

    header.append(titleGroup, modeGroup, removeBtn);
    card.append(header);

    const body = document.createElement("div");
    body.className = "param-card-body";
    card.append(body);

    const modelSelect = titleGroup.querySelector(".param-model-select");
    modelSelect.addEventListener("change", () => {
      const curData = getCardCurrentValues(card);
      curData.model = modelSelect.value;
      card._entryData = curData;
      updateCardBody(card);
      markConfigDirty();
    });
    modeSelect.addEventListener("change", () => {
      const curData = getCardCurrentValues(card);
      curData.parameter_mode = normalizeParameterMode(modeSelect.value);
      card._entryData = curData;
      updateCardBody(card);
      markConfigDirty();
    });

    updateCardBody(card);
    $("#config-parameters-list")?.append(card);
  }

  function reRenderAllParameterCards() {
    $$(".param-card").forEach((card) => {
      updateCardBody(card);
    });
  }

  async function loadConfiguration() {
    setStatus("正在获取配置数据...");
    $("#config-conflict-alert").hidden = true;
    const response = await get("configuration");

    state.configRevision = response.revision;
    state.rawConfig = response.config || {};
    state.parameterFields = response.metadata?.model_parameter_fields || [];
    state.parameterModes = response.metadata?.parameter_modes || [];
    state.settingsMetadata = response.metadata?.settings || [];

    const config = state.rawConfig;

    // Revision Badge
    const badge = $("#config-revision-badge");
    if (badge) badge.textContent = `rev: ${state.configRevision || "init"}`;

    // Base endpoints and default model
    const genericUrlInput = $("#config-generic-url");
    if (genericUrlInput) genericUrlInput.value = config.generic_api_url || "";

    // Models Table
    $("#config-models-table")?.replaceChildren();
    const modelList = config.model_list || [];
    for (const name of modelList) {
      addModelRow(name, {
        gemini: config.gemini_model_list?.includes(name),
        chat: config.chat_completions_model_list?.includes(name),
        generations: config.images_generations_model_list?.includes(name),
        edits: config.images_edits_model_list?.includes(name),
      });
    }

    syncAllModelDropdowns();
    if (config.model && $("#config-default-model")) {
      $("#config-default-model").value = config.model;
    }

    // Prefixes
    $("#config-prefixes-list")?.replaceChildren();
    const prefixes = config.extra_prefix || [];
    for (const p of prefixes) {
      const pText = typeof p === "string" ? p : p.prefix || "";
      if (pText) addPrefixTag(pText);
    }

    // Bindings
    $("#config-bindings-table")?.replaceChildren();
    for (const b of config.command_model_list || []) {
      addBindingRow(b);
    }

    // Mappings
    $("#config-mappings-table")?.replaceChildren();
    for (const m of config.model_mapping_list || []) {
      addMappingRow(m);
    }

    // Templates
    $("#config-templates-table")?.replaceChildren();
    for (const t of config.model_prompt_template_list || []) {
      addTemplateRow(t);
    }

    // Parameter Cards
    $("#config-parameters-list")?.replaceChildren();
    for (const paramEntry of config.model_parameter_list || []) {
      renderModelParameterCard(paramEntry);
    }

    // Dynamic Full Plugin Settings (serialized under config.settings)
    renderDynamicSettings(state.settingsMetadata, config.settings || {});

    // Sensitive status accompanies the ordinary configuration snapshot so its
    // revision always refers to the configuration currently displayed.
    state.sensitiveState = response.sensitive || state.sensitiveState;
    renderSensitiveStatus();

    markConfigDirty(false);
    setStatus("");
  }

  function collectConfiguration() {
    const modelRows = $$("#config-models-table tr");
    const model_list = modelRows
      .map((row) => row.querySelector(".route-model")?.value.trim())
      .filter(Boolean);

    const flagList = (selector) => modelRows
      .filter((row) => row.querySelector(selector)?.checked)
      .map((row) => row.querySelector(".route-model")?.value.trim())
      .filter(Boolean);

    const extra_prefix = $$("#config-prefixes-list .prefix-tag")
      .map((tag) => tag.dataset.prefix?.trim())
      .filter(Boolean);

    const command_model_list = $$("#config-bindings-table tr").map((row) => ({
      command: row.querySelector(".binding-command")?.value.trim() || "",
      model: row.querySelector(".binding-model")?.value || "",
    })).filter((b) => b.command);

    const model_mapping_list = $$("#config-mappings-table tr").map((row) => ({
      model: row.querySelector(".mapping-source")?.value || "",
      mapped_model: row.querySelector(".mapping-target")?.value || "",
      priority: Number(row.querySelector(".mapping-priority")?.value || 0),
    })).filter((m) => m.model && m.mapped_model);

    const model_prompt_template_list = $$("#config-templates-table tr").map((row) => ({
      model: row.querySelector(".template-model")?.value || "",
      prompt_template: row.querySelector(".template-prompt")?.value.trim() || "",
    })).filter((t) => t.model && t.prompt_template);

    // Parameter cards: retrieve merged data object preserving non-displayed/hidden fields
    const model_parameter_list = $$(".param-card").map((card) => {
      return getCardCurrentValues(card);
    }).filter((p) => p.model);

    // Dynamic settings values serialized under config.settings
    const dynamicSettings = collectDynamicSettings();

    // Preserve existing other fields from rawConfig, but deliberately omit prompt_list or any keys
    const configPayload = { ...(state.rawConfig || {}) };
    delete configPayload.prompt_list;
    delete configPayload.generic_api_keys;
    delete configPayload.generic_api_keys_configured;
    delete configPayload.gemini_api_keys;
    delete configPayload.gemini_api_keys_configured;
    delete configPayload.gemini_api_url;

    configPayload.model = $("#config-default-model")?.value.trim() || "";
    configPayload.model_list = model_list;
    const serviceUrlState = state.sensitiveState?.generic_api_url || {};
    configPayload.generic_api_url = serviceUrlState.write_only
      ? ""
      : $("#config-generic-url")?.value.trim() || "";
    configPayload.gemini_model_list = flagList(".route-gemini");
    configPayload.chat_completions_model_list = flagList(".route-chat");
    configPayload.images_generations_model_list = flagList(".route-generations");
    configPayload.images_edits_model_list = flagList(".route-edits");
    configPayload.extra_prefix = extra_prefix;
    configPayload.command_model_list = command_model_list;
    configPayload.model_mapping_list = model_mapping_list;
    configPayload.model_prompt_template_list = model_prompt_template_list;
    configPayload.model_parameter_list = model_parameter_list;
    configPayload.settings = dynamicSettings;

    return configPayload;
  }

  async function saveConfiguration() {
    if (state.isSavingConfig) return;
    state.isSavingConfig = true;
    const saveBtns = [$("#config-save-btn"), $("#config-save-btn-bottom")];
    saveBtns.forEach((b) => {
      if (b) {
        b.disabled = true;
        b.textContent = "保存中...";
      }
    });

    try {
      setStatus("正在保存配置到服务器...");
      const payload = {
        revision: state.configRevision,
        config: collectConfiguration(),
      };

      const response = await post("configuration", payload);
      state.configRevision = response.revision;
      if (response.config) {
        state.rawConfig = response.config;
      }
      await loadConfiguration();
      $("#config-conflict-alert").hidden = true;
      setStatus(response.message || "配置已成功保存并即时生效。", "success");
    } catch (error) {
      if (error.status === 409 || (error.message && error.message.includes("409")) || (error.message && error.message.includes("其他页面修改"))) {
        $("#config-conflict-alert").hidden = false;
        setStatus("配置保存冲突：服务器端配置已被修改，请重新加载。", "error");
      } else {
        handleError(error);
      }
    } finally {
      state.isSavingConfig = false;
      saveBtns.forEach((b) => {
        if (b) {
          b.disabled = false;
          b.innerHTML = `<span class="btn-symbol">💾</span> 保存配置`;
        }
      });
    }
  }

  // --- Adjust Dialog ---
  function openAdjust(type, id, label) {
    $("#adjust-subject-type").value = type;
    $("#adjust-subject-id").value = id;
    $("#adjust-title").textContent = `调整${type === "user" ? "用户" : "群组"}余额`;
    $("#adjust-subject").textContent = label;
    $("#adjust-amount").value = "";
    $("#adjust-note").value = "";
    $("#adjust-dialog")?.showModal();
  }

  function closeAdjust() {
    $("#adjust-dialog")?.close();
  }

  async function loadCurrent() {
    if (state.view === "overview") return loadOverview();
    if (state.view === "users") return loadUsers();
    if (state.view === "groups") return loadGroups();
    if (state.view === "presets") return loadPresets();
    if (state.view === "config") return loadConfiguration();
  }

  function handleError(error) {
    console.error(error);
    setStatus(error.message || "请求失败，请检查网络或后端日志", "error");
  }

  function setupEvents() {
    // Navigation Tabs with Independent Dirty Checks
    $$(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        const nextView = tab.dataset.view;
        if (nextView === state.view) return;

        // Independent dirty confirmation check
        if (state.view === "config" && state.isConfigDirty) {
          const leave = confirm("配置管理有未保存的更改，确定要离开吗？未保存的更改将丢失。");
          if (!leave) return;
        } else if (state.view === "presets" && state.isPresetsDirty) {
          const leave = confirm("预设管理有未保存的更改，确定要离开吗？未保存的更改将丢失。");
          if (!leave) return;
        }

        state.view = nextView;
        $$(".tab").forEach((item) => item.classList.toggle("is-active", item === tab));
        $$(".view").forEach((view) => view.classList.toggle("is-active", view.id === `${state.view}-view`));
        loadCurrent().catch(handleError);
      });
    });

    // Date Refresh Button
    const refreshBtn = $("#refresh-button");
    if (refreshBtn) refreshBtn.onclick = () => loadCurrent().catch(handleError);

    // Users View Search
    $("#user-search-button")?.addEventListener("click", () => {
      state.userPage = 1;
      loadUsers().catch(handleError);
    });
    $("#user-search")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        state.userPage = 1;
        loadUsers().catch(handleError);
      }
    });

    // Groups View Search
    $("#group-search-button")?.addEventListener("click", () => {
      state.groupPage = 1;
      loadGroups().catch(handleError);
    });
    $("#group-search")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        state.groupPage = 1;
        loadGroups().catch(handleError);
      }
    });

    // Overview Events Filters (day / outcome / page size)
    for (const control of ["#events-day", "#events-outcome", "#events-page-size"]) {
      $(control)?.addEventListener("change", () => {
        state.eventsPage = 1;
        loadEvents().catch(handleError);
      });
    }

    // Presets View Actions
    $("#preset-search-input")?.addEventListener("input", (e) => {
      state.presetSearch = e.target.value;
      filterPresetsDisplay();
    });

    $("#presets-refresh-btn")?.addEventListener("click", () => {
      if (state.isPresetsDirty) {
        const confirmed = confirm("当前有未保存的预设更改，重新加载将丢失这些更改，是否继续？");
        if (!confirmed) return;
      }
      loadPresets().catch(handleError);
    });

    $("#presets-conflict-reload-btn")?.addEventListener("click", () => loadPresets().catch(handleError));

    $("#add-preset-btn")?.addEventListener("click", () => {
      addPresetRow({}, true);
      filterPresetsDisplay();
      markPresetsDirty(true);
    });

    $("#presets-save-btn")?.addEventListener("click", () => savePresets());

    // Sensitive Key Actions
    $("#key-action-append-btn")?.addEventListener("click", () => {
      const input = $("#sensitive-keys-input");
      const keys = parseKeyList(input?.value);
      if (!keys.length) {
        setStatus("请输入要追加的 API Key", "error");
        return;
      }
      mutateSensitive("generic_api_keys", "append", { values: keys });
    });

    $("#key-action-replace-btn")?.addEventListener("click", () => {
      const input = $("#sensitive-keys-input");
      const keys = parseKeyList(input?.value);
      if (!keys.length) {
        setStatus("请输入要替换的 API Key", "error");
        return;
      }
      mutateSensitive("generic_api_keys", "replace", { values: keys });
    });

    $("#key-action-clear-btn")?.addEventListener("click", () => {
      const confirmed = confirm("确定要清空全部 API Key 吗？清空后生图功能可能无法使用。");
      if (!confirmed) return;
      mutateSensitive("generic_api_keys", "clear", { values: [] });
    });

    // Sensitive service URL actions are available only for credential-bearing URLs.
    $("#service-url-action-replace-btn")?.addEventListener("click", () => {
      const input = $("#sensitive-service-url-input");
      const val = input?.value.trim();
      if (!val) {
        setStatus("请输入带认证信息的共享服务地址", "error");
        return;
      }
      mutateSensitive("generic_api_url", "replace", { value: val });
    });

    $("#service-url-action-clear-btn")?.addEventListener("click", () => {
      const confirmed = confirm("确定要清除共享服务地址凭据吗？");
      if (!confirmed) return;
      mutateSensitive("generic_api_url", "clear");
    });

    // Sensitive Proxy Actions
    $("#proxy-action-replace-btn")?.addEventListener("click", () => {
      const input = $("#sensitive-proxy-input");
      const val = input?.value.trim();
      if (!val) {
        setStatus("请输入代理地址凭据", "error");
        return;
      }
      mutateSensitive("proxy_url", "replace", { value: val });
    });

    $("#proxy-action-clear-btn")?.addEventListener("click", () => {
      const confirmed = confirm("确定要清除代理凭据吗？");
      if (!confirmed) return;
      mutateSensitive("proxy_url", "clear");
    });

    // Configuration Actions
    $("#config-refresh-btn")?.addEventListener("click", () => {
      if (state.isConfigDirty) {
        const confirmed = confirm("当前有未保存的配置更改，重新加载将丢失这些更改，是否继续？");
        if (!confirmed) return;
      }
      loadConfiguration().catch(handleError);
    });

    $("#conflict-reload-btn")?.addEventListener("click", () => loadConfiguration().catch(handleError));
    $("#config-save-btn")?.addEventListener("click", () => saveConfiguration());

    $("#config-form")?.addEventListener("submit", (e) => {
      e.preventDefault();
      saveConfiguration();
    });

    $("#config-form")?.addEventListener("input", (e) => {
      // Exclude sensitive inputs from marking normal config form dirty
      if (["sensitive-keys-input", "sensitive-service-url-input", "sensitive-proxy-input"].includes(e.target.id)) return;
      markConfigDirty(true);
    });

    $("#config-form")?.addEventListener("change", (e) => {
      if (["sensitive-keys-input", "sensitive-service-url-input", "sensitive-proxy-input"].includes(e.target.id)) return;
      markConfigDirty(true);
    });

    $("#add-model-btn")?.addEventListener("click", () => {
      addModelRow();
      syncAllModelDropdowns();
      reRenderAllParameterCards();
      markConfigDirty(true);
    });

    $("#add-prefix-btn")?.addEventListener("click", () => {
      const input = $("#new-prefix-input");
      const val = input.value.trim();
      if (!val) return;
      addPrefixTag(val);
      input.value = "";
      markConfigDirty(true);
    });

    $("#new-prefix-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        $("#add-prefix-btn")?.click();
      }
    });

    $("#add-binding-btn")?.addEventListener("click", () => {
      addBindingRow();
      markConfigDirty(true);
    });

    $("#add-mapping-btn")?.addEventListener("click", () => {
      addMappingRow();
      markConfigDirty(true);
    });

    $("#add-template-btn")?.addEventListener("click", () => {
      addTemplateRow();
      markConfigDirty(true);
    });

    $("#add-parameter-btn")?.addEventListener("click", () => {
      renderModelParameterCard();
      markConfigDirty(true);
    });

    // Adjust balance dialog form submission
    $("#adjust-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const amount = Number((Number($("#adjust-amount")?.value) || 0).toFixed(3));
        if (!amount) {
          throw new Error("变更金额不能为 0");
        }
        setStatus("正在提交余额调整...");
        await post("usage/adjust", {
          subject_type: $("#adjust-subject-type")?.value,
          subject_id: $("#adjust-subject-id")?.value,
          amount,
          note: $("#adjust-note")?.value.trim() || "",
        });
        closeAdjust();
        setStatus("余额调整成功。", "success");
        await loadCurrent();
      } catch (error) {
        handleError(error);
      }
    });

    // Adjust Dialog controls
    const dialog = $("#adjust-dialog");
    $("#adjust-cancel-btn")?.addEventListener("click", closeAdjust);
    dialog?.querySelector(".close-dialog-btn")?.addEventListener("click", closeAdjust);
    dialog?.addEventListener("click", (e) => {
      if (e.target === dialog) closeAdjust();
    });
  }

  async function start() {
    if (!bridge) {
      setStatus("AstrBot 页面通信桥接不可用，请在 AstrBot 管理端内打开。", "error");
      return;
    }
    try {
      if (typeof bridge.ready === "function") {
        await bridge.ready();
      }
      setupEvents();
      await loadOverview();
    } catch (error) {
      handleError(error);
    }
  }

  start();
})();
