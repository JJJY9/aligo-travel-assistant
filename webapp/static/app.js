/* Aligo Web 前端（无构建、无外部依赖） */
"use strict";

const AGENT_NAMES = {
  event_collection: "事项收集",
  preference: "偏好管理",
  itinerary_planning: "行程规划",
  information_query: "信息查询",
  rag_knowledge: "知识库查询",
  memory_query: "记忆查询",
};

const INTENT_NAMES = {
  trip_planning: "行程规划",
  travel_planning: "行程规划",
  itinerary_planning: "行程规划",
  event_collection: "事项收集",
  preference: "偏好管理",
  preferences: "偏好管理",
  preference_collection: "偏好管理",
  memory_query: "记忆查询",
  knowledge_query: "知识问答",
  rag_knowledge: "知识问答",
  information_query: "信息查询",
  weather_query: "天气查询",
  greeting: "问候",
  chitchat: "闲聊",
};

const PREF_NAMES = {
  home_location: "常住地",
  transportation_preference: "交通偏好",
  hotel_brands: "酒店偏好",
  airlines: "航空公司偏好",
  seat_preference: "座位偏好",
  meal_preference: "餐食偏好",
  budget_level: "预算等级",
};

const state = {
  userId:
    sessionStorage.getItem("aligo.userId") ||
    localStorage.getItem("aligo.userId") ||
    "web_user",
  sessionId: sessionStorage.getItem("aligo.sessionId") || null,
  sessions: [],
  searchQuery: "",
  status: null,
  health: null,
  busy: false,
  controller: null,
  lastUserText: "",
  kbItems: [],
  kbTotal: 0,
  kbQuery: "",
  kbCategory: "",
  kbDeleting: new Set(),
  kbImporting: false,
  kbEditingId: null,
  kbEditingMetadata: {},
  kbSaving: false,
  kbSemanticMode: false,
  kbSemanticQuery: "",
  tripEditingId: null,
  tripsExpanded: false,
  tripsAll: [],
  tripsAllTotal: 0,
  tripsFilter: "",
  sessionTotal: 0,
  showArchived: false,
  archivedCount: 0,
  archivedMatches: 0,
  sessionsLoading: false,
};

function isValidId(value) {
  return /^[A-Za-z0-9_一-龥-]{1,64}$/.test(String(value || ""));
}

/* 深链接：?user=...&session=... 可直接打开某个会话 */
(function applyUrlState() {
  try {
    const params = new URLSearchParams(location.search);
    const urlUser = params.get("user");
    if (urlUser && isValidId(urlUser)) {
      state.userId = urlUser;
      // 分享链接只作为当前标签页的临时身份，不覆盖本地长期身份
      sessionStorage.setItem("aligo.userId", urlUser);
    }
    const urlSession = params.get("session");
    if (urlSession && isValidId(urlSession)) {
      state.sessionId = urlSession;
      sessionStorage.setItem("aligo.sessionId", urlSession);
    }
  } catch {
    /* 忽略非法的 URL 参数 */
  }
})();

const els = {
  app: byId("app"),
  sidebar: byId("sidebar"),
  history: byId("history"),
  historySearch: byId("historySearch"),
  archivedToggle: byId("archivedToggle"),
  archivedLabel: byId("archivedLabel"),
  upcoming: byId("upcoming"),
  upcomingLabel: byId("upcomingLabel"),
  upcomingRoute: byId("upcomingRoute"),
  upcomingDate: byId("upcomingDate"),
  messages: byId("messages"),
  scroll: byId("scroll"),
  input: byId("input"),
  sendBtn: byId("sendBtn"),
  stopBtn: byId("stopBtn"),
  composer: byId("composer"),
  userIdInput: byId("userId"),
  userLabel: byId("userLabel"),
  userAvatar: byId("userAvatar"),
  storageLabel: byId("storageLabel"),
  convTitle: byId("convTitle"),
  modelChip: byId("modelChip"),
  health: byId("health"),
  healthText: byId("healthText"),
  themeToggle: byId("themeToggle"),
  exportChat: byId("exportChat"),
  exportHtml: byId("exportHtml"),
  copyLink: byId("copyLink"),
  printChat: byId("printChat"),
  menuBtn: byId("menuBtn"),
  collapseSidebar: byId("collapseSidebar"),
  scrim: byId("scrim"),
  memorySheet: byId("memorySheet"),
  closeMemory: byId("closeMemory"),
  saveUser: byId("saveUser"),
  exportData: byId("exportData"),
  exportDataMd: byId("exportDataMd"),
  exportSessions: byId("exportSessions"),
  overview: byId("overview"),
  addPrefBtn: byId("addPrefBtn"),
  prefForm: byId("prefForm"),
  prefType: byId("prefType"),
  prefValue: byId("prefValue"),
  prefSubmit: byId("prefSubmit"),
  prefCancel: byId("prefCancel"),
  trips: byId("trips"),
  exportTrips: byId("exportTrips"),
  exportTripsCsv: byId("exportTripsCsv"),
  addTripBtn: byId("addTripBtn"),
  expandTrips: byId("expandTrips"),
  tripFilterRow: byId("tripFilterRow"),
  tripFilter: byId("tripFilter"),
  tripFilterCount: byId("tripFilterCount"),
  tripForm: byId("tripForm"),
  tripOrigin: byId("tripOrigin"),
  tripDestination: byId("tripDestination"),
  tripStart: byId("tripStart"),
  tripEnd: byId("tripEnd"),
  tripPurpose: byId("tripPurpose"),
  tripSubmit: byId("tripSubmit"),
  destinations: byId("destinations"),
  stats: byId("stats"),
  usageStats: byId("usageStats"),
  metricsStats: byId("metricsStats"),
  metricsNote: byId("metricsNote"),
  usageAgents: byId("usageAgents"),
  usageTrend: byId("usageTrend"),
  usageTrendTotal: byId("usageTrendTotal"),
  usageChart: byId("usageChart"),
  usageTable: byId("usageTable"),
  storageChips: byId("storageChips"),
  knowledgeChip: byId("knowledgeChip"),
  kbList: byId("kbList"),
  kbSearch: byId("kbSearch"),
  kbCategoryFilter: byId("kbCategoryFilter"),
  kbMore: byId("kbMore"),
  kbImport: byId("kbImport"),
  kbImportFile: byId("kbImportFile"),
  kbSemantic: byId("kbSemantic"),
  kbEval: byId("kbEval"),
  kbEvalResult: byId("kbEvalResult"),
  kbForm: byId("kbForm"),
  kbContent: byId("kbContent"),
  kbCategory: byId("kbCategory"),
  kbSubmit: byId("kbSubmit"),
  kbCancel: byId("kbCancel"),
  runDoctor: byId("runDoctor"),
  doctorNetwork: byId("doctorNetwork"),
  doctorList: byId("doctorList"),
  toast: byId("toast"),
  shortcutsBtn: byId("shortcutsBtn"),
  shortcutOverlay: byId("shortcutOverlay"),
  closeShortcuts: byId("closeShortcuts"),
};

let welcomeTemplate = null;
let sessionLoadToken = 0;
let sessionsRequestId = 0;
let statusRequestId = 0;
// 行程列表（展开全部 / 状态刷新时的二次请求）共用序号：只应用最新一次结果，
// 防止切换用户后旧用户的数据被渲染（行内删除按当前用户执行，会误删同名 trip_id）
let tripsRequestId = 0;
let searchTimer = null;
let memorySheetOpener = null;
let shortcutOpener = null;

/* ---------- 基础工具 ---------- */

function byId(id) {
  return document.getElementById(id);
}

function icon(name, className = "icon") {
  return `<svg class="${className}"><use href="#i-${name}"></use></svg>`;
}

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function highlightSnippet(text, query) {
  const source = String(text ?? "");
  const needle = String(query || "").trim();
  if (!needle) return escapeHtml(source);

  const lowerSource = source.toLowerCase();
  const lowerNeedle = needle.toLowerCase();
  let cursor = 0;
  let found = lowerSource.indexOf(lowerNeedle, cursor);
  if (found < 0) return escapeHtml(source);

  const parts = [];
  while (found >= 0) {
    parts.push(escapeHtml(source.slice(cursor, found)));
    parts.push(`<mark>${escapeHtml(source.slice(found, found + needle.length))}</mark>`);
    cursor = found + needle.length;
    found = lowerSource.indexOf(lowerNeedle, cursor);
  }
  parts.push(escapeHtml(source.slice(cursor)));
  return parts.join("");
}

function clampText(text, max) {
  const value = String(text || "").trim();
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

function clockNow() {
  return new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function clockTime(timestamp) {
  if (!timestamp) return "";
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

let toastTimer = null;
function toast(message) {
  els.toast.textContent = message;
  els.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    els.toast.hidden = true;
  }, 2000);
}

async function copyText(text, message = "已复制") {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        toast(message);
        return;
      } catch {
        /* 权限受限时回退到 execCommand */
      }
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const copied = document.execCommand("copy");
    area.remove();
    if (!copied) throw new Error("copy failed");
    toast(message);
  } catch {
    toast("复制失败");
  }
}

async function getJSON(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const error = new Error(await errorMessage(response));
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function errorMessage(response) {
  let detail = `请求失败（HTTP ${response.status}）`;
  try {
    const data = await response.json();
    if (typeof data.detail === "string") {
      detail = data.detail;
    } else if (Array.isArray(data.detail) && data.detail.length) {
      detail = data.detail[0].msg || detail;
    }
  } catch {
    /* 保留默认错误信息 */
  }
  return detail;
}

/* ---------- 安全的 Markdown 渲染 ----------
   先在原文上识别块级结构，再对文本内容转义并处理行内样式，
   因此不会执行任何 HTML / 脚本。 */

function renderInline(raw) {
  return escapeHtml(raw)
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
    )
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

function splitTableRow(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function renderMarkdown(text) {
  const lines = String(text ?? "").split("\n");
  const out = [];
  const paragraph = [];
  let index = 0;

  const flushParagraph = () => {
    if (paragraph.length) {
      out.push(`<p>${paragraph.join("<br>")}</p>`);
      paragraph.length = 0;
    }
  };

  while (index < lines.length) {
    const line = lines[index];

    const fence = line.match(/^```([\w+-]*)\s*$/);
    if (fence) {
      flushParagraph();
      const code = [];
      index += 1;
      while (index < lines.length && !/^```/.test(lines[index])) {
        code.push(lines[index]);
        index += 1;
      }
      index += 1;
      out.push(
        `<pre><button type="button" class="code-copy">复制</button><code>${escapeHtml(
          code.join("\n"),
        )}</code></pre>`,
      );
      continue;
    }

    if (
      /^\s*\|.*\|\s*$/.test(line) &&
      index + 1 < lines.length &&
      /^\s*\|[\s:|-]+\|\s*$/.test(lines[index + 1])
    ) {
      flushParagraph();
      const header = splitTableRow(line);
      index += 2;
      const rows = [];
      while (index < lines.length && /^\s*\|.*\|\s*$/.test(lines[index])) {
        rows.push(splitTableRow(lines[index]));
        index += 1;
      }
      out.push(
        `<div class="table-wrap"><table><thead><tr>${header
          .map((cell) => `<th>${renderInline(cell)}</th>`)
          .join("")}</tr></thead><tbody>${rows
          .map(
            (row) =>
              `<tr>${row.map((cell) => `<td>${renderInline(cell)}</td>`).join("")}</tr>`,
          )
          .join("")}</tbody></table></div>`,
      );
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      flushParagraph();
      const level = Math.min(Math.max(heading[1].length, 2), 4);
      out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    if (/^\s*---+\s*$/.test(line)) {
      flushParagraph();
      out.push("<hr>");
      index += 1;
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      flushParagraph();
      const items = [];
      while (index < lines.length && /^\s*[-*]\s+/.test(lines[index])) {
        items.push(`<li>${renderInline(lines[index].replace(/^\s*[-*]\s+/, ""))}</li>`);
        index += 1;
      }
      out.push(`<ul>${items.join("")}</ul>`);
      continue;
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      flushParagraph();
      const items = [];
      while (index < lines.length && /^\s*\d+[.)]\s+/.test(lines[index])) {
        items.push(`<li>${renderInline(lines[index].replace(/^\s*\d+[.)]\s+/, ""))}</li>`);
        index += 1;
      }
      out.push(`<ol>${items.join("")}</ol>`);
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      flushParagraph();
      const quoted = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quoted.push(renderInline(lines[index].replace(/^\s*>\s?/, "")));
        index += 1;
      }
      out.push(`<blockquote>${quoted.join("<br>")}</blockquote>`);
      continue;
    }

    if (!line.trim()) {
      flushParagraph();
      index += 1;
      continue;
    }

    paragraph.push(renderInline(line));
    index += 1;
  }

  flushParagraph();
  return out.join("\n");
}

/* ---------- 滚动 ---------- */

function atBottom() {
  const el = els.scroll;
  return el.scrollHeight - el.scrollTop - el.clientHeight < 140;
}

function scrollToBottom(force = false) {
  if (force) {
    els.scroll.scrollTop = els.scroll.scrollHeight;
    return;
  }
  if (atBottom()) {
    els.scroll.scrollTop = els.scroll.scrollHeight;
  }
}

function mutate(fn, force = false) {
  const pinned = force || atBottom();
  fn();
  if (pinned) {
    requestAnimationFrame(() => {
      els.scroll.scrollTop = els.scroll.scrollHeight;
    });
  }
}

/* ---------- 消息渲染 ---------- */

function messageHead(timestamp) {
  const head = document.createElement("div");
  head.className = "msg-head";
  head.innerHTML = `<span class="msg-avatar">${icon("route")}</span><span class="msg-name">Aligo</span><time class="msg-time"></time>`;
  head.querySelector("time").textContent = timestamp ? clockTime(timestamp) : clockNow();
  return head;
}

function chipButton(iconName, label, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "chip";
  button.innerHTML = `${icon(iconName)}<span></span>`;
  button.querySelector("span").textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

function renderUserNode(text) {
  const node = document.createElement("div");
  node.className = "msg user";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  node.appendChild(bubble);
  return node;
}

function refreshEditButtons() {
  els.messages.querySelectorAll(".user-edit").forEach((node) => node.remove());
  if (state.busy || !state.sessionId) return;
  const users = els.messages.querySelectorAll(".msg.user");
  const last = users[users.length - 1];
  if (!last) return;
  const bubble = last.querySelector(".bubble");
  if (!bubble || bubble.querySelector(".user-edit-form")) return;
  const foot = document.createElement("div");
  foot.className = "msg-foot user-edit";
  foot.appendChild(
    chipButton("edit", "编辑", () => startEditUserNode(last, bubble.textContent)),
  );
  last.appendChild(foot);
}

function cancelPendingEdits() {
  const inputs = [
    ...els.messages.querySelectorAll(".msg.user.editing .user-edit-input"),
  ];
  if (!inputs.length) return true;
  // 正在提交的编辑不能取消（否则界面显示已取消，但请求仍会删除并重发）
  if (inputs.some((input) => input.dataset.saving === "1")) return false;
  inputs.forEach((input) => {
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
  return true;
}

function startEditUserNode(node, originalText) {
  if (state.busy) return;
  const bubble = node.querySelector(".bubble");
  if (!bubble || bubble.querySelector(".user-edit-form")) return;
  const foot = node.querySelector(".user-edit");
  if (foot) foot.remove();
  node.classList.add("editing");
  let saving = false;

  const form = document.createElement("div");
  form.className = "user-edit-form";
  const input = document.createElement("textarea");
  input.className = "user-edit-input";
  input.maxLength = 4000;
  input.value = originalText;
  input.dataset.saving = "0";
  input.setAttribute("aria-label", "编辑提问");
  const actions = document.createElement("div");
  actions.className = "user-edit-actions";
  const save = document.createElement("button");
  save.type = "button";
  save.className = "save";
  save.textContent = "保存并重新发送";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "取消";
  actions.append(save, cancel);
  form.append(input, actions);

  bubble.textContent = "";
  bubble.appendChild(form);
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);

  const restore = () => {
    node.classList.remove("editing");
    bubble.textContent = originalText;
    refreshEditButtons();
  };
  const close = () => {
    if (saving) return;
    input.removeEventListener("keydown", onKeydown);
    restore();
  };
  const onKeydown = (event) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      event.preventDefault();
      close();
    } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      submit();
    }
  };
  input.addEventListener("keydown", onKeydown);

  cancel.addEventListener("click", close);

  async function submit() {
    if (saving) return;
    const text = input.value.trim();
    if (!text) {
      toast("提问不能为空");
      input.focus();
      return;
    }
    if (text === originalText) {
      close();
      return;
    }
    saving = true;
    input.dataset.saving = "1";
    save.disabled = true;
    cancel.disabled = true;
    save.textContent = "发送中…";
    // 删除 + 重发期间锁住输入区与会话切换，避免交错的发送改动被当作本轮删除
    setBusy(true);
    let mismatch = false;
    try {
      mismatch = await deleteLastTurn(originalText);
    } catch (error) {
      saving = false;
      input.dataset.saving = "0";
      setBusy(false);
      toast(`编辑失败：${error.message}`);
      save.disabled = false;
      cancel.disabled = false;
      save.textContent = "保存并重新发送";
      return;
    }
    setBusy(false);
    input.removeEventListener("keydown", onKeydown);
    // 本轮的用户提问与回答一并移除后重新发送；内容不一致说明这条提问
    // 尚未写入历史，此时只清理界面并直接重发，不删除服务端上一轮
    let next = node.nextElementSibling;
    while (next) {
      const following = next.nextElementSibling;
      if (next.classList.contains("msg")) next.remove();
      next = following;
    }
    node.remove();
    if (mismatch) toast("这条提问还没有保存到历史，已直接重新发送");
    await sendMessage(text);
  }

  save.addEventListener("click", submit);
}

function renderHistoryTrace(meta) {
  const schedule = Array.isArray(meta.schedule) ? meta.schedule : [];
  const agents = Array.isArray(meta.agents) ? meta.agents : [];
  const agentStatuses = new Map();
  const agentDurations = new Map();
  agents.forEach((agent) => {
    if (!agent || !agent.name) return;
    agentStatuses.set(agent.name, agent.status);
    if (agent.duration !== null && agent.duration !== undefined) {
      agentDurations.set(agent.name, agent.duration);
    }
  });
  const steps = schedule.length
    ? schedule
    : agents.map((agent) => ({ agent_name: agent.name, reason: "" }));

  const root = document.createElement("div");
  root.className = "trace done collapsed";
  root.innerHTML = `
    <button type="button" class="trace-head" aria-expanded="false">
      <span class="trace-status" aria-hidden="true"></span>
      <span class="trace-title">执行路线</span>
      <span class="trace-meta"></span>
      <svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m6 9 6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </button>
    <ol class="route"></ol>`;

  const list = root.querySelector(".route");
  const addStop = (label, note, status, duration = null) => {
    const li = document.createElement("li");
    li.className = `stop ${status}`;
    li.innerHTML =
      '<span class="stop-dot" aria-hidden="true"></span><span class="stop-body"><span class="stop-label"></span><span class="stop-note"></span></span><span class="stop-time"></span>';
    li.querySelector(".stop-label").textContent = label;
    const noteEl = li.querySelector(".stop-note");
    noteEl.textContent = note;
    noteEl.hidden = !note;
    if (duration !== null && duration !== undefined) {
      li.querySelector(".stop-time").textContent = `${Number(duration).toFixed(1)}s`;
    }
    list.appendChild(li);
  };

  addStop("整理记忆与上下文", "已加载偏好与最近对话", "done");
  const intentText = summarizeIntents(meta.intents);
  addStop("理解你的需求", intentText ? `识别为：${intentText}` : "", "done");
  steps.forEach((item) => {
    const name = item.agent_name || "";
    const status = agentStatuses.get(name) === "error" ? "error" : "done";
    addStop(
      AGENT_NAMES[name] || name,
      item.reason ? clampText(item.reason, 48) : "",
      status,
      agentDurations.get(name),
    );
  });

  const elapsed = Number(meta.elapsed || 0);
  root.querySelector(".trace-meta").textContent = `${2 + steps.length} 站${
    elapsed ? ` · ${elapsed.toFixed(1)}s` : ""
  }`;

  const head = root.querySelector(".trace-head");
  head.addEventListener("click", () => {
    root.classList.toggle("collapsed");
    head.setAttribute("aria-expanded", String(!root.classList.contains("collapsed")));
  });
  return root;
}

function renderAssistantNode(markdown, options = {}) {
  const { timestamp = "", regenText = "", meta = null } = options;
  const node = document.createElement("div");
  node.className = "msg assistant";
  const bubble = document.createElement("div");
  bubble.className = "bubble answer";
  bubble.innerHTML = renderMarkdown(markdown || "（无内容）");
  const foot = document.createElement("div");
  foot.className = "msg-foot";

  if (meta) {
    (Array.isArray(meta.agents) ? meta.agents : []).forEach((agent) => {
      foot.appendChild(makeBadge(agent.name, agent.status));
    });
  }
  foot.appendChild(chipButton("copy", "复制", () => copyText(markdown || "")));
  if (meta) {
    const usageText = formatUsage(meta.usage, meta.elapsed);
    if (usageText) {
      const usage = document.createElement("span");
      usage.className = "usage";
      usage.textContent = usageText;
      foot.appendChild(usage);
    }
  }
  if (regenText) {
    const regen = chipButton("refresh", "重新生成", () => regenerate(node, regenText));
    regen.dataset.action = "regen";
    foot.appendChild(regen);
  }

  const parts = [messageHead(timestamp)];
  const hasTrace =
    meta &&
    ((Array.isArray(meta.schedule) && meta.schedule.length > 0) ||
      (Array.isArray(meta.agents) && meta.agents.length > 0));
  if (hasTrace) {
    parts.push(renderHistoryTrace(meta));
  }
  parts.push(bubble, foot);
  node.append(...parts);
  return node;
}

function makeBadge(name, status) {
  const badge = document.createElement("span");
  badge.className = status === "error" ? "badge err" : "badge";
  badge.textContent = `${status === "error" ? "✗" : "✓"} ${AGENT_NAMES[name] || name}`;
  return badge;
}

function formatUsage(usage, elapsed) {
  const parts = [];
  if (usage && usage.calls) parts.push(`${usage.calls} 次调用`);
  if (usage && usage.total_tokens) parts.push(`${usage.total_tokens.toLocaleString("zh-CN")} tokens`);
  if (typeof elapsed === "number") parts.push(`${elapsed}s`);
  return parts.join(" · ");
}

async function deleteLastTurn(expect) {
  const result = await getJSON(
    `/api/sessions/${encodeURIComponent(state.sessionId)}/last-turn?user_id=${encodeURIComponent(
      state.userId,
    )}&expect=${encodeURIComponent(expect)}`,
    { method: "DELETE" },
  );
  return Boolean(result && result.mismatch);
}

async function regenerate(node, text) {
  if (state.busy) return;
  trimRegenButtons();
  if (state.sessionId) {
    // 先删除服务端上一轮，避免重新生成在历史里叠加重复问答
    setBusy(true);
    let mismatched = false;
    try {
      mismatched = await deleteLastTurn(text);
    } catch (error) {
      setBusy(false);
      toast(`重新生成失败：${error.message}`);
      return;
    }
    setBusy(false);
    if (mismatched) {
      // 服务端最后一轮已不是这条回答对应的提问（例如历史在其它页面被修改）：
      // 不能删除当前节点后重发旧问题，否则会打乱顺序并在历史中留下重复问答
      toast("上一轮对话已变化，未重新生成；请刷新页面后重试");
      return;
    }
  }
  node.remove();
  await sendMessage(text, { echo: false });
}

function trimRegenButtons() {
  const buttons = [...els.messages.querySelectorAll('[data-action="regen"]')];
  buttons.slice(0, -1).forEach((button) => button.remove());
}

const FOLLOW_UP_SETS = {
  itinerary_planning: [
    { label: "导出这份行程", action: "export" },
    { label: "出发当天天气如何？", prompt: "帮我查一下出发当天的天气" },
    { label: "调整行程安排", prompt: "帮我调整一下行程安排" },
  ],
  information_query: [
    { label: "据此规划行程", prompt: "根据上面的信息帮我规划行程" },
    { label: "查差旅住宿标准", prompt: "一线城市住宿标准是多少？" },
  ],
  rag_knowledge: [
    { label: "帮我规划一次出差", prompt: "帮我规划下周一从北京去杭州出差 3 天" },
    { label: "查一下杭州天气", prompt: "杭州明天天气怎么样？" },
  ],
  memory_query: [
    { label: "规划新行程", prompt: "帮我规划一次出差" },
    { label: "查看我的偏好", prompt: "我有哪些出行偏好？" },
  ],
  preference: [
    { label: "按偏好规划行程", prompt: "根据我的偏好帮我规划一次出差" },
  ],
};

function buildFollowUps(agents) {
  const names = new Set((agents || []).map((agent) => agent && agent.name).filter(Boolean));
  for (const key of [
    "itinerary_planning",
    "information_query",
    "rag_knowledge",
    "memory_query",
    "preference",
  ]) {
    if (names.has(key)) return FOLLOW_UP_SETS[key];
  }
  return [];
}

function addFollowUps(node, agents) {
  const items = buildFollowUps(agents);
  if (!items.length) return;
  const row = document.createElement("div");
  row.className = "follow-ups";
  items.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "follow-up";
    button.textContent = item.label;
    button.addEventListener("click", () => {
      if (item.action === "export") {
        triggerExport();
        return;
      }
      sendMessage(item.prompt);
    });
    row.appendChild(button);
  });
  node.appendChild(row);
}

function triggerExport() {
  if (!state.sessionId) return;
  window.open(
    `/api/sessions/${encodeURIComponent(state.sessionId)}/export?user_id=${encodeURIComponent(
      state.userId,
    )}`,
    "_blank",
  );
}

async function toggleTripsExpanded() {
  if (state.tripsExpanded) {
    state.tripsExpanded = false;
    renderOverview();
    return;
  }
  const userId = state.userId;
  const requestId = (tripsRequestId += 1);
  els.expandTrips.disabled = true;
  try {
    // order=desc：取「最近 200 条」，而不是最早的 200 条
    const data = await getJSON(
      `/api/trips?user_id=${encodeURIComponent(userId)}&limit=200&order=desc`,
    );
    if (requestId !== tripsRequestId || userId !== state.userId) return;
    state.tripsAll = data.trips || [];
    state.tripsAllTotal = Number(data.total || state.tripsAll.length);
    state.tripsExpanded = true;
    renderOverview();
  } catch (error) {
    if (requestId !== tripsRequestId || userId !== state.userId) return;
    toast(error.message);
  } finally {
    els.expandTrips.disabled = false;
  }
}

function resetTripForm() {
  state.tripEditingId = null;
  els.tripForm.reset();
  els.tripSubmit.textContent = "保存行程";
}

function startEditTrip(trip) {
  state.tripEditingId = trip.trip_id;
  els.tripOrigin.value = trip.origin || "";
  els.tripDestination.value = trip.destination || "";
  els.tripStart.value = trip.start_date || "";
  els.tripEnd.value = trip.end_date || "";
  els.tripPurpose.value = trip.purpose || "";
  els.tripSubmit.textContent = "保存修改";
  els.tripForm.hidden = false;
  els.tripDestination.focus();
}

function conflictNote(conflicts) {
  if (!Array.isArray(conflicts) || !conflicts.length) return "";
  const first = conflicts[0];
  const route = `${first.origin || "—"} → ${first.destination || "—"}`;
  const suffix =
    conflicts.length > 1 ? ` 等 ${conflicts.length} 条行程的` : "的行程";
  return `，与 ${route}${suffix}日期重叠`;
}

async function addTrip(event) {
  event.preventDefault();
  const destination = els.tripDestination.value.trim();
  const startDate = els.tripStart.value;
  if (!destination || !startDate) {
    toast("请填写目的地与出发日期");
    return;
  }
  const editingId = state.tripEditingId;
  const payload = {
    user_id: state.userId,
    origin: els.tripOrigin.value.trim(),
    destination,
    start_date: startDate,
    end_date: els.tripEnd.value || null,
    purpose: els.tripPurpose.value.trim() || "出差",
  };
  els.tripSubmit.disabled = true;
  try {
    let message = "行程已更新";
    if (editingId) {
      const result = await getJSON(`/api/trips/${encodeURIComponent(editingId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      message += conflictNote(result.conflicts);
    } else {
      const result = await getJSON("/api/trips", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      message =
        result.created === false ? "已存在相同行程，已更新该记录" : "行程已添加";
      message += conflictNote(result.conflicts);
    }
    toast(message);
    els.tripForm.hidden = true;
    resetTripForm();
    await refreshStatus();
  } catch (error) {
    toast(error.message);
  } finally {
    els.tripSubmit.disabled = false;
  }
}

function exportSafeName(text) {
  return (
    String(text || "aligo")
      .replace(/[\\/:*?"<>|\s]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40) || "aligo"
  );
}

function buildExportHtml(title, bodyHtml) {
  const exportedAt = new Date().toLocaleString("zh-CN");
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>
  :root { --ink:#14262e; --ink-3:#7b8d93; --line:#e2e9e8; --accent:#d9552c;
    --bg:#f2f5f4; --surface:#ffffff; --code-bg:#0e1e24; --code-ink:#dce9e6; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.65 "PingFang SC", "HarmonyOS Sans SC", "Microsoft YaHei", system-ui, sans-serif; }
  .page { max-width: 820px; margin: 24px auto; padding: 32px 36px; background: var(--surface);
    border-radius: 16px; box-shadow: 0 12px 40px rgba(15, 36, 42, 0.08); }
  h1 { margin: 0 0 4px; font-size: 22px; }
  .meta { margin-bottom: 20px; color: var(--ink-3); font-size: 12.5px; }
  .msg { margin: 18px 0; }
  .msg.user { display: flex; justify-content: flex-end; }
  .msg.user .bubble { max-width: 85%; padding: 10px 14px; border-radius: 14px 14px 4px 14px;
    background: #162c34; color: #f3f7f6; white-space: pre-wrap; }
  .msg.assistant .bubble { padding: 12px 16px; border: 1px solid var(--line);
    border-radius: 14px 14px 14px 4px; background: var(--surface); }
  .msg-head, .trace, .msg-foot, .follow-ups, .code-copy { display: none !important; }
  .bubble p { margin: 8px 0; }
  .bubble h2, .bubble h3, .bubble h4 { margin: 14px 0 6px; }
  .bubble ul, .bubble ol { margin: 8px 0; padding-left: 22px; }
  .bubble pre { padding: 12px 14px; border-radius: 10px; overflow: auto;
    background: var(--code-bg); color: var(--code-ink); font-size: 13px; }
  .bubble code { font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; }
  .bubble table { width: 100%; margin: 10px 0; border-collapse: collapse; font-size: 13.5px; }
  .bubble th, .bubble td { padding: 6px 10px; border: 1px solid var(--line); text-align: left; }
  .bubble a { color: var(--accent); }
  .footer { margin-top: 24px; padding-top: 12px; border-top: 1px solid var(--line);
    color: var(--ink-3); font-size: 12px; }
  @media print {
    body { background: #fff; color: #111; }
    .page { margin: 0; padding: 0; box-shadow: none; }
    .msg.user .bubble { background: #f0f0f0 !important; color: #111 !important; }
    .msg.assistant .bubble { border-color: #ddd; }
    .bubble pre { background: #f5f5f5 !important; color: #111 !important; border: 1px solid #ddd; }
    .bubble a { color: #111; }
    .msg, .bubble { break-inside: avoid; }
  }
</style>
</head>
<body>
<div class="page">
<h1>${escapeHtml(title)}</h1>
<div class="meta">Aligo 智能旅行助手 · 导出于 ${escapeHtml(exportedAt)}</div>
${bodyHtml}
<div class="footer">由 Aligo 智能旅行助手生成 · 回答由 AI 生成，请核对关键信息</div>
</div>
</body>
</html>`;
}

function exportSessionHtml() {
  if (!state.sessionId) return;
  if (!cancelPendingEdits()) {
    toast("正在保存修改，请稍候");
    return;
  }
  const title = (els.convTitle.textContent || "").trim() || "Aligo 对话";
  const clone = els.messages.cloneNode(true);
  clone
    .querySelectorAll(".trace, .msg-foot, .follow-ups, .code-copy, .welcome")
    .forEach((node) => node.remove());
  const html = buildExportHtml(title, clone.innerHTML);
  const blob = new Blob([html], { type: "text/html;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  const now = new Date();
  const localDate = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(
    2,
    "0",
  )}-${String(now.getDate()).padStart(2, "0")}`;
  link.download = `${exportSafeName(title)}-${localDate}.html`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // 下载是异步读取 Blob 的，稍后再释放，避免 Firefox / Safari 取消下载
  setTimeout(() => URL.revokeObjectURL(url), 5000);
  toast("HTML 已导出");
}

/* ---------- 执行路线 ---------- */

function summarizeIntents(intents) {
  if (!Array.isArray(intents)) return "";
  const names = intents
    .map((item) => {
      if (typeof item === "string") return INTENT_NAMES[item] || item;
      if (item && typeof item === "object") {
        const key = item.type || item.intent || item.name || "";
        return INTENT_NAMES[key] || key;
      }
      return "";
    })
    .filter(Boolean);
  return names.slice(0, 3).join("、");
}

function createTrace() {
  const root = document.createElement("div");
  root.className = "trace running";
  root.innerHTML = `
    <button type="button" class="trace-head" aria-expanded="true">
      <span class="trace-status" aria-hidden="true"></span>
      <span class="trace-title">正在整理上下文</span>
      <span class="trace-meta"></span>
      <svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m6 9 6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </button>
    <ol class="route"></ol>`;

  const list = root.querySelector(".route");
  const titleEl = root.querySelector(".trace-title");
  const metaEl = root.querySelector(".trace-meta");
  const headEl = root.querySelector(".trace-head");
  const stops = new Map();

  headEl.addEventListener("click", () => {
    root.classList.toggle("collapsed");
    headEl.setAttribute("aria-expanded", String(!root.classList.contains("collapsed")));
  });

  function upsertStop(key, label, note = "", status = "running") {
    let stop = stops.get(key);
    if (!stop) {
      const li = document.createElement("li");
      li.className = "stop";
      li.innerHTML =
        '<span class="stop-dot" aria-hidden="true"></span><span class="stop-body"><span class="stop-label"></span><span class="stop-note"></span></span><span class="stop-time"></span>';
      list.appendChild(li);
      stop = {
        li,
        label: li.querySelector(".stop-label"),
        note: li.querySelector(".stop-note"),
        time: li.querySelector(".stop-time"),
      };
      stops.set(key, stop);
    }
    stop.li.className = `stop ${status}`;
    stop.label.textContent = label;
    stop.note.textContent = note;
    stop.note.hidden = !note;
    return stop;
  }

  function finishStop(key, label, note = "") {
    upsertStop(key, label, note, "done");
  }

  function setAgent(name, status, note = "", duration = null) {
    let stop = null;
    const entries = [...stops.entries()].reverse();
    for (const [key, candidate] of entries) {
      if (key.startsWith(`agent:${name}:`)) {
        stop = candidate;
        break;
      }
    }
    if (!stop) {
      stop = upsertStop(`agent:${name}:extra`, AGENT_NAMES[name] || name, note, status);
    }
    stop.li.className = `stop ${status}`;
    if (note) {
      stop.note.textContent = note;
      stop.note.hidden = false;
    }
    if (duration !== null && duration !== undefined) {
      stop.time.textContent = `${Number(duration).toFixed(1)}s`;
    }
  }

  return {
    root,
    start() {
      upsertStop("context", "整理记忆与上下文");
    },
    phase(phase, label) {
      if (phase === "intent") {
        finishStop("context", "整理记忆与上下文", "已加载偏好与最近对话");
        upsertStop("intent", label || "理解你的需求");
        titleEl.textContent = "正在理解你的需求";
      } else if (phase === "context") {
        upsertStop("context", label || "整理记忆与上下文");
      }
    },
    plan(event) {
      const intentText = summarizeIntents(event.intents);
      finishStop("intent", "理解你的需求", intentText ? `识别为：${intentText}` : "");
      const schedule = event.schedule || [];
      if (!schedule.length) {
        upsertStop("compose", "整理回答");
        titleEl.textContent = "正在整理回答";
        return;
      }
      schedule.forEach((item, index) => {
        const agentName = item.agent_name || "";
        const label = AGENT_NAMES[agentName] || agentName;
        const note = item.reason ? clampText(item.reason, 48) : "";
        upsertStop(`agent:${agentName}:${index}`, label, note, "pending");
      });
      titleEl.textContent = `正在执行 ${schedule.length} 个智能体`;
    },
    agentStart(name) {
      setAgent(name, "running");
    },
    agentDone(name, status, message, duration) {
      if (status === "error") {
        setAgent(name, "error", clampText(message || "执行失败", 80), duration);
      } else {
        setAgent(name, "done", "", duration);
      }
    },
    finish(elapsed) {
      for (const stop of stops.values()) {
        if (stop.li.classList.contains("running") || stop.li.classList.contains("pending")) {
          stop.li.className = "stop done";
        }
      }
      root.classList.remove("running");
      root.classList.add("done");
      root.classList.add("collapsed");
      headEl.setAttribute("aria-expanded", "false");
      titleEl.textContent = "执行路线";
      metaEl.textContent = `${stops.size} 站 · ${Number(elapsed || 0).toFixed(1)}s`;
    },
    fail(message) {
      stops.forEach((stop) => {
        if (stop.li.classList.contains("running")) {
          stop.li.className = "stop error";
          stop.note.textContent = message;
          stop.note.hidden = false;
        }
      });
      root.classList.remove("running");
      root.classList.add("error");
      root.classList.remove("collapsed");
      headEl.setAttribute("aria-expanded", "true");
      titleEl.textContent = "执行中断";
    },
  };
}

function createAssistantMessage(regenText = "") {
  const root = document.createElement("div");
  root.className = "msg assistant";
  const head = messageHead("");
  const trace = createTrace();
  const bubble = document.createElement("div");
  bubble.className = "bubble answer";
  bubble.hidden = true;
  const foot = document.createElement("div");
  foot.className = "msg-foot";
  foot.hidden = true;
  root.append(head, trace.root, bubble, foot);

  function setReply(markdown, data) {
    bubble.innerHTML = renderMarkdown(markdown || "（无内容）");
    bubble.hidden = false;
    foot.hidden = false;
    foot.innerHTML = "";

    (data.agents || []).forEach((agent) => {
      foot.appendChild(makeBadge(agent.name, agent.status));
    });
    foot.appendChild(chipButton("copy", "复制", () => copyText(markdown || "")));

    const usageText = formatUsage(data.usage, data.elapsed);
    if (usageText) {
      const usage = document.createElement("span");
      usage.className = "usage";
      usage.textContent = usageText;
      foot.appendChild(usage);
    }
    if (regenText) {
      const regen = chipButton("refresh", "重新生成", () => regenerate(root, regenText));
      regen.dataset.action = "regen";
      foot.appendChild(regen);
    }
    addFollowUps(root, data.agents || []);
  }

  function fail(message) {
    bubble.hidden = false;
    bubble.innerHTML = `<blockquote>${escapeHtml(message)}</blockquote>`;
    foot.hidden = false;
    foot.innerHTML = "";
    if (regenText) {
      const retry = chipButton("refresh", "重试", () => regenerate(root, regenText));
      retry.dataset.action = "regen";
      foot.appendChild(retry);
    }
  }

  return { root, trace, setReply, fail };
}

/* ---------- 会话管理 ---------- */

function hideWelcome() {
  const welcome = els.messages.querySelector("[data-welcome]");
  if (welcome) welcome.remove();
}

function showWelcome() {
  els.messages.innerHTML = "";
  if (welcomeTemplate) {
    els.messages.appendChild(welcomeTemplate.cloneNode(true));
  }
}

function setConversationTitle(title) {
  els.convTitle.textContent = title || "新对话";
  els.convTitle.title = title || "";
}

function shareUrl() {
  const params = new URLSearchParams();
  params.set("user", state.userId);
  if (state.sessionId) params.set("session", state.sessionId);
  return `${location.origin}${location.pathname}?${params.toString()}`;
}

function syncUrl() {
  try {
    history.replaceState(null, "", shareUrl());
  } catch {
    /* 某些环境下 history API 不可用 */
  }
}

function syncSessionControls() {
  els.exportChat.hidden = !state.sessionId;
  els.exportHtml.hidden = !state.sessionId;
  els.copyLink.hidden = !state.sessionId;
  els.printChat.hidden = !state.sessionId;
  syncUrl();
}

function newSession() {
  if (state.busy) {
    toast("请等待当前回复完成");
    return;
  }
  sessionLoadToken += 1;
  state.sessionId = null;
  sessionStorage.removeItem("aligo.sessionId");
  syncSessionControls();
  setConversationTitle("新对话");
  showWelcome();
  if (state.showArchived) {
    // 归档视图下新建对话：切回普通列表，否则新会话不会出现在侧栏
    state.showArchived = false;
    updateArchivedToggle();
    loadSessions(state.searchQuery, { reset: true });
  }
  markActiveHistory();
  closeMobileSidebar();
  els.input.focus();
}

function markActiveHistory() {
  els.history.querySelectorAll(".history-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.id === state.sessionId);
  });
}

function dateGroupLabel(timestamp) {
  if (!timestamp) return "更早";
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "更早";
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const day = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const diff = Math.round((today - day) / 86400000);
  if (diff <= 0) return "今天";
  if (diff === 1) return "昨天";
  return "更早";
}

function renderHistoryItem(session) {
  const item = document.createElement("div");
  item.className = "history-item";
  item.dataset.id = session.session_id;
  item.setAttribute("role", "button");
  item.setAttribute("tabindex", "0");
  if (session.session_id === state.sessionId) item.classList.add("active");
  if (session.pinned) item.classList.add("pinned");
  if (session.archived) item.classList.add("archived");

  const title = document.createElement("span");
  title.className = "hi-title";
  title.textContent = session.title || "新对话";
  item.appendChild(title);

  if (session.snippet) {
    const snippet = document.createElement("span");
    snippet.className = "hi-snippet";
    snippet.innerHTML = highlightSnippet(session.snippet, state.searchQuery);
    item.appendChild(snippet);
  }

  const actions = document.createElement("span");
  actions.className = "hi-actions";

  const remove = document.createElement("button");
  remove.type = "button";
  remove.title = "删除会话";
  remove.setAttribute("aria-label", "删除会话");
  remove.innerHTML = icon("trash");
  remove.addEventListener("click", (event) => {
    event.stopPropagation();
    deleteSession(session.session_id);
  });

  if (session.archived) {
    const restore = document.createElement("button");
    restore.type = "button";
    restore.title = "恢复会话";
    restore.setAttribute("aria-label", "恢复会话");
    restore.innerHTML = icon("refresh");
    restore.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleArchive(session);
    });
    actions.append(restore, remove);
  } else {
    const pin = document.createElement("button");
    pin.type = "button";
    pin.title = session.pinned ? "取消置顶" : "置顶会话";
    pin.setAttribute("aria-label", pin.title);
    pin.innerHTML = icon("pin");
    pin.addEventListener("click", (event) => {
      event.stopPropagation();
      togglePin(session);
    });

    const rename = document.createElement("button");
    rename.type = "button";
    rename.title = "重命名会话";
    rename.setAttribute("aria-label", "重命名会话");
    rename.innerHTML = icon("edit");
    rename.addEventListener("click", (event) => {
      event.stopPropagation();
      startRenameSession(item, session);
    });

    const archive = document.createElement("button");
    archive.type = "button";
    archive.title = "归档会话";
    archive.setAttribute("aria-label", "归档会话");
    archive.innerHTML = icon("archive");
    archive.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleArchive(session);
    });

    actions.append(pin, rename, archive, remove);
  }
  item.appendChild(actions);

  item.addEventListener("click", () =>
    openSession(session.session_id, { matchIndex: session.match_index }),
  );
  item.addEventListener("keydown", (event) => {
    if (event.target !== item) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openSession(session.session_id, { matchIndex: session.match_index });
    }
  });
  return item;
}

function renderHistory() {
  els.history.innerHTML = "";

  const appendArchivedHint = () => {
    if (state.showArchived || state.archivedMatches <= 0) return;
    const hint = document.createElement("button");
    hint.type = "button";
    hint.className = "history-more archived-hint";
    hint.textContent = `另有 ${state.archivedMatches} 条归档会话匹配`;
    hint.addEventListener("click", () => {
      state.showArchived = true;
      updateArchivedToggle();
      loadSessions(state.searchQuery, { reset: true });
    });
    els.history.appendChild(hint);
  };

  if (!state.sessions.length) {
    const empty = document.createElement("div");
    empty.className = "history-empty";
    if (state.showArchived) {
      empty.textContent = state.searchQuery ? "没有匹配的归档会话" : "还没有归档的会话";
    } else {
      empty.textContent = state.searchQuery ? "没有匹配的对话" : "还没有历史对话";
    }
    els.history.appendChild(empty);
    appendArchivedHint();
    return;
  }

  const addSection = (label, items) => {
    const section = document.createElement("div");
    section.className = "history-group";
    const heading = document.createElement("h3");
    heading.textContent = label;
    section.appendChild(heading);
    items.forEach((session) => section.appendChild(renderHistoryItem(session)));
    els.history.appendChild(section);
  };

  if (state.showArchived) {
    addSection("已归档", state.sessions);
  } else {
    const pinned = state.sessions.filter((session) => session.pinned);
    const regular = state.sessions.filter((session) => !session.pinned);
    if (pinned.length) addSection("置顶", pinned);

    const groups = new Map();
    regular.forEach((session) => {
      const label = dateGroupLabel(session.updated_at);
      if (!groups.has(label)) groups.set(label, []);
      groups.get(label).push(session);
    });
    groups.forEach((items, label) => addSection(label, items));
  }

  if (state.sessions.length < state.sessionTotal) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "history-more";
    more.textContent = `加载更多会话（已显示 ${state.sessions.length} / 共 ${state.sessionTotal}）`;
    more.addEventListener("click", loadMoreSessions);
    els.history.appendChild(more);
  }
  appendArchivedHint();
}

async function togglePin(session) {
  try {
    await getJSON(`/api/sessions/${encodeURIComponent(session.session_id)}/pin`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: state.userId, pinned: !session.pinned }),
    });
    toast(session.pinned ? "已取消置顶" : "已置顶会话");
    await loadSessions();
  } catch (error) {
    toast(`操作失败：${error.message}`);
  }
}

async function toggleArchive(session) {
  const archived = !session.archived;
  try {
    await getJSON(
      `/api/sessions/${encodeURIComponent(session.session_id)}/archive`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: state.userId, archived }),
      },
    );
    toast(archived ? "会话已归档" : "会话已恢复");
    await Promise.all([
      loadSessions(state.searchQuery, { reset: true }),
      refreshStatus(),
    ]);
  } catch (error) {
    toast(`操作失败：${error.message}`);
  }
}

function updateArchivedToggle(count) {
  if (!els.archivedToggle) return;
  if (Number.isFinite(count)) state.archivedCount = count;
  const total = state.archivedCount || 0;
  els.archivedToggle.hidden = total <= 0 && !state.showArchived;
  els.archivedToggle.setAttribute("aria-pressed", String(state.showArchived));
  els.archivedLabel.textContent = state.showArchived
    ? "返回全部会话"
    : `已归档（${total}）`;
}

async function loadSessions(query = state.searchQuery, options = {}) {
  state.searchQuery = String(query || "").trim();
  const requestId = (sessionsRequestId += 1);
  state.sessionsLoading = true;
  // 重置（搜索 / 切换用户）取第一页；其它刷新（重命名 / 置顶 / 删除）
  // 保留用户已经「加载更多」展开的数量
  const limit = options.reset
    ? 50
    : Math.min(200, Math.max(50, state.sessions.length));
  const params = new URLSearchParams({
    user_id: state.userId,
    limit: String(limit),
    offset: "0",
  });
  if (state.searchQuery) params.set("q", state.searchQuery);
  if (state.showArchived) params.set("archived", "1");
  let archivedMatches = 0;
  try {
    const data = await getJSON(`/api/sessions?${params}`);
    if (requestId !== sessionsRequestId) return;
    state.sessions = data.sessions || [];
    state.sessionTotal = Number(data.total || state.sessions.length);
    if (data.archived_count !== undefined) {
      updateArchivedToggle(Number(data.archived_count));
    }
    // 普通视图搜索时提示归档会话中的匹配数量（不把归档内容混进主列表）
    if (state.searchQuery && !state.showArchived) {
      const archivedParams = new URLSearchParams({
        user_id: state.userId,
        limit: "1",
        offset: "0",
        archived: "1",
        q: state.searchQuery,
      });
      try {
        const archivedData = await getJSON(`/api/sessions?${archivedParams}`);
        if (requestId !== sessionsRequestId) return;
        archivedMatches = Number(archivedData.total || 0);
      } catch {
        archivedMatches = 0;
      }
    }
  } catch {
    if (requestId !== sessionsRequestId) return;
    state.sessions = [];
    state.sessionTotal = 0;
  }
  if (requestId !== sessionsRequestId) return;
  state.sessionsLoading = false;
  state.archivedMatches = archivedMatches;
  renderHistory();
}

async function loadMoreSessions() {
  // 视图切换 / 新搜索的第一页仍在加载时，旧列表的 offset 不能用于新查询
  if (state.sessionsLoading) return;
  const viewQuery = state.searchQuery;
  const viewArchived = state.showArchived;
  const requestId = (sessionsRequestId += 1);
  const params = new URLSearchParams({
    user_id: state.userId,
    limit: "50",
    offset: String(state.sessions.length),
  });
  if (state.searchQuery) params.set("q", state.searchQuery);
  if (state.showArchived) params.set("archived", "1");
  try {
    const data = await getJSON(`/api/sessions?${params}`);
    if (requestId !== sessionsRequestId) return;
    if (viewQuery !== state.searchQuery || viewArchived !== state.showArchived) return;
    const seen = new Set(state.sessions.map((session) => session.session_id));
    for (const session of data.sessions || []) {
      if (!seen.has(session.session_id)) {
        seen.add(session.session_id);
        state.sessions.push(session);
      }
    }
    state.sessionTotal = Number(data.total || state.sessionTotal);
    renderHistory();
  } catch (error) {
    toast(error.message);
  }
}

async function openSession(sessionId, options = {}) {
  if (state.busy) {
    if (!options.silent) toast("请等待当前回复完成");
    return;
  }
  const token = ++sessionLoadToken;
  try {
    const data = await getJSON(
      `/api/sessions/${encodeURIComponent(sessionId)}?user_id=${encodeURIComponent(state.userId)}`,
    );
    // 加载期间用户可能已开始新对话或切换会话，避免覆盖当前界面
    if (state.busy || token !== sessionLoadToken) return;
    if (!data.messages || !data.messages.length) {
      if (!options.silent) toast("这个会话还没有消息");
      newSession();
      return;
    }
    state.sessionId = sessionId;
    sessionStorage.setItem("aligo.sessionId", sessionId);
    syncSessionControls();

    const session = state.sessions.find((item) => item.session_id === sessionId);
    setConversationTitle(session ? session.title : data.title || "对话");

    els.messages.innerHTML = "";
    let lastUserText = "";
    let lastAssistantNode = null;
    let lastAssistantMeta = null;
    data.messages.forEach((message) => {
      if (message.role === "user") {
        lastUserText = message.content;
        els.messages.appendChild(renderUserNode(message.content));
      } else {
        const node = renderAssistantNode(message.content, {
          timestamp: message.timestamp,
          regenText: lastUserText,
          meta: message.meta || null,
        });
        lastAssistantNode = node;
        lastAssistantMeta = message.meta || null;
        els.messages.appendChild(node);
      }
    });
    if (lastAssistantNode && lastAssistantMeta) {
      addFollowUps(lastAssistantNode, lastAssistantMeta.agents || []);
    }
    trimRegenButtons();
    refreshEditButtons();
    markActiveHistory();
    const matchIndex = Number.isInteger(options.matchIndex) ? options.matchIndex : -1;
    const matchNode =
      matchIndex >= 0 ? els.messages.querySelectorAll(".msg")[matchIndex] : null;
    if (matchNode) {
      matchNode.classList.add("msg-flash");
      matchNode.scrollIntoView({ block: "center" });
      setTimeout(() => matchNode.classList.remove("msg-flash"), 1800);
    } else {
      scrollToBottom(true);
    }
    closeMobileSidebar();
  } catch (error) {
    if (!options.silent) {
      toast(`加载会话失败：${error.message}`);
    } else if (token === sessionLoadToken) {
      // 启动恢复失败（服务重启 / 网络抖动）：丢弃旧会话 ID，避免下一条消息
      // 被写进界面从未展示过的隐藏会话
      state.sessionId = null;
      sessionStorage.removeItem("aligo.sessionId");
      syncSessionControls();
      showWelcome();
    }
  }
}

function startRenameSession(item, session) {
  if (item.querySelector(".hi-input")) return;
  const titleEl = item.querySelector(".hi-title");
  if (!titleEl) return;

  const input = document.createElement("input");
  input.className = "hi-input";
  input.maxLength = 60;
  input.value = session.title || "";
  titleEl.replaceWith(input);
  input.focus();
  input.select();
  let settled = false;

  const cancel = () => {
    if (settled) return;
    settled = true;
    if (input.isConnected) input.replaceWith(titleEl);
  };

  const save = async () => {
    if (settled) return;
    const value = input.value.trim();
    settled = true;
    try {
      await getJSON(`/api/sessions/${encodeURIComponent(session.session_id)}/title`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: state.userId, title: value }),
      });
      toast(value ? "会话已重命名" : "已恢复自动标题");
      await loadSessions();
      if (state.sessionId === session.session_id) {
        const updated = state.sessions.find((s) => s.session_id === session.session_id);
        setConversationTitle(updated ? updated.title : value);
      }
    } catch (error) {
      settled = false;
      toast(`重命名失败：${error.message}`);
      input.focus();
    }
  };

  input.addEventListener("click", (event) => event.stopPropagation());
  input.addEventListener("keydown", (event) => {
    event.stopPropagation();
    if (event.isComposing) return;
    if (event.key === "Enter") {
      event.preventDefault();
      save();
    } else if (event.key === "Escape") {
      event.preventDefault();
      cancel();
    }
  });
  input.addEventListener("blur", () => {
    if (!settled) cancel();
  });
}

async function deleteSession(sessionId) {
  if (state.busy) {
    toast("请等待当前回复完成");
    return;
  }
  if (!window.confirm("删除这个会话？聊天记录会从长期记忆中移除。")) return;
  // 作废进行中的会话加载：否则删除后晚到的 GET 会把已删除的会话重新渲染出来，
  // 后续消息还会以同一 session_id 复活它
  sessionLoadToken += 1;
  try {
    await getJSON(
      `/api/sessions/${encodeURIComponent(sessionId)}?user_id=${encodeURIComponent(state.userId)}`,
      { method: "DELETE" },
    );
    if (state.sessionId === sessionId) {
      state.sessionId = null;
      sessionStorage.removeItem("aligo.sessionId");
      syncSessionControls();
      setConversationTitle("新对话");
      showWelcome();
    }
    await loadSessions();
    toast("会话已删除");
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

/* ---------- 发送消息（SSE 流式） ---------- */

function setBusy(busy) {
  state.busy = busy;
  els.sendBtn.disabled = busy;
  els.stopBtn.hidden = !busy;
}

async function sendMessage(text, options = {}) {
  const message = String(text || "").trim();
  if (!message || state.busy) return;

  cancelPendingEdits();
  sessionLoadToken += 1;
  state.lastUserText = message;
  state.controller = new AbortController();
  setBusy(true);
  trimRegenButtons();
  document.querySelectorAll(".follow-ups").forEach((row) => row.remove());

  mutate(() => {
    hideWelcome();
    if (options.echo !== false) {
      els.messages.appendChild(renderUserNode(message));
    }
  }, true);
  // 生成期间不保留上一条消息的编辑入口（结束后由 refreshEditButtons 移动到新提问）
  els.messages.querySelectorAll(".user-edit").forEach((node) => node.remove());
  els.input.value = "";
  resizeInput();

  const assistant = createAssistantMessage(message);
  assistant.trace.start();
  mutate(() => els.messages.appendChild(assistant.root), true);

  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        user_id: state.userId,
        session_id: state.sessionId,
      }),
      signal: state.controller.signal,
    });
    if (!response.ok) {
      throw new Error(await errorMessage(response));
    }
    await readEventStream(response.body, (event) => handleStreamEvent(assistant, event));
    els.health.className = "health ok";
  } catch (error) {
    if (error.name === "AbortError") {
      mutate(() => {
        assistant.trace.fail("已停止");
        assistant.fail("已停止生成。");
      });
    } else {
      mutate(() => {
        assistant.trace.fail(error.message || "请求失败");
        assistant.fail(error.message || "请求失败，请稍后再试。");
      });
      els.health.className = "health err";
    }
  } finally {
    state.controller = null;
    setBusy(false);
    refreshEditButtons();
    // 只保留最新一条回答的「重新生成」：旧按钮对应的已不是最后一轮，
    // 点击只会触发 mismatch，甚至在外层历史中插入重复问答
    trimRegenButtons();
    const panelOpen = els.memorySheet && !els.memorySheet.hidden;
    if (!panelOpen) {
      // 用户正在侧栏重命名等输入框里操作时不抢焦点
      const active = document.activeElement;
      const editing =
        active &&
        active !== els.input &&
        (active.tagName === "INPUT" ||
          active.tagName === "TEXTAREA" ||
          active.tagName === "SELECT" ||
          active.isContentEditable);
      if (!editing) els.input.focus();
      if (!document.querySelector(".hi-input")) await loadSessions();
      refreshStatus();
    }
  }
}

async function readEventStream(body, onEvent) {
  if (!body) throw new Error("浏览器不支持流式响应");
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const chunk = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      chunk.split("\n").forEach((line) => {
        if (!line.startsWith("data:")) return;
        const payload = line.slice(5).trim();
        if (!payload) return;
        let event;
        try {
          event = JSON.parse(payload);
        } catch {
          return; // 忽略无法解析的事件
        }
        onEvent(event);
      });
      boundary = buffer.indexOf("\n\n");
    }
  }
}

function handleStreamEvent(assistant, event) {
  switch (event.type) {
    case "phase":
      mutate(() => assistant.trace.phase(event.phase, event.label));
      break;
    case "plan":
      mutate(() => assistant.trace.plan(event));
      break;
    case "agent_start":
      mutate(() => assistant.trace.agentStart(event.agent_name));
      break;
    case "agent_done":
      mutate(() =>
        assistant.trace.agentDone(
          event.agent_name,
          event.status,
          event.message,
          event.duration,
        ),
      );
      break;
    case "session": {
      if (event.session_id) {
        state.sessionId = event.session_id;
        sessionStorage.setItem("aligo.sessionId", event.session_id);
        syncSessionControls();
        if (els.convTitle.textContent === "新对话") {
          setConversationTitle(clampText(state.lastUserText, 40));
        }
      }
      break;
    }
    case "done": {
      if (event.session_id) {
        state.sessionId = event.session_id;
        sessionStorage.setItem("aligo.sessionId", event.session_id);
        syncSessionControls();
      }
      if (els.convTitle.textContent === "新对话") {
        setConversationTitle(clampText(state.lastUserText, 40));
      }
      mutate(() => {
        assistant.trace.finish(event.elapsed);
        assistant.setReply(event.reply, event);
      }, true);
      break;
    }
    case "error":
      mutate(() => {
        assistant.trace.fail(event.message || "处理失败");
        assistant.fail(event.message || "处理失败，请稍后再试。");
      });
      break;
    default:
      break;
  }
}

/* ---------- 状态与用户 ---------- */

function storageParts(storage) {
  if (!storage) return ["本地存储"];
  const parts = [];
  parts.push(storage.short_term === "redis" ? "Redis" : "本地内存");
  parts.push(storage.long_term === "postgres" ? "PostgreSQL" : "JSON");
  // 技能数量放在用户卡片上：安装后技能缺失（如 wheel 未打包）能直接看到
  const skills = state.health && state.health.skills;
  if (skills && typeof skills.count === "number") {
    parts.push(`技能 ${skills.count}`);
  }
  return parts;
}

function updateUserCard() {
  const userId = state.userId;
  els.userLabel.textContent = userId;
  els.userAvatar.textContent = (userId || "旅").slice(0, 1).toUpperCase();
  const storage = (state.status && state.status.storage) || (state.health && state.health.storage);
  els.storageLabel.textContent = storageParts(storage).join(" · ");
}

const BREAKER_LABELS = { closed: "正常", half_open: "半开", open: "熔断" };

function formatUptime(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  if (total < 60) return `${total} 秒`;
  if (total < 3600) return `${Math.floor(total / 60)} 分钟`;
  if (total < 86400) {
    return `${Math.floor(total / 3600)} 小时 ${Math.floor((total % 3600) / 60)} 分`;
  }
  return `${Math.floor(total / 86400)} 天 ${Math.floor((total % 86400) / 3600)} 小时`;
}

async function refreshMetrics() {
  if (!els.metricsStats) return;
  try {
    const data = await getJSON("/api/metrics");
    const requests = data.requests || {};
    const total = Number(requests.requests_total || 0);
    const tiles = [
      ["请求总数", total.toLocaleString("zh-CN")],
      [
        "平均耗时",
        total ? `${Math.round(Number(requests.avg_duration_ms || 0))} ms` : "—",
      ],
      [
        "最大耗时",
        total ? `${Math.round(Number(requests.max_duration_ms || 0))} ms` : "—",
      ],
      [
        "P95 耗时",
        total ? `${Math.round(Number(requests.p95_duration_ms || 0))} ms` : "—",
      ],
      [
        "熔断器",
        BREAKER_LABELS[String((data.circuit_breaker || {}).state || "")] || "—",
      ],
    ];
    els.metricsStats.innerHTML = "";
    tiles.forEach(([label, value]) => {
      const box = document.createElement("div");
      box.className = "stat";
      const number = document.createElement("b");
      number.textContent = String(value);
      const caption = document.createElement("span");
      caption.textContent = label;
      box.append(number, caption);
      els.metricsStats.appendChild(box);
    });
    if (els.metricsNote) {
      const cached = Number(data.sessions_cached || 0);
      const rate = data.rate_limit || {};
      let rateBackend = rate.backend === "redis" ? "Redis 共享" : "进程内";
      if (rate.degraded) {
        rateBackend =
          rate.backend === "redis" ? "Redis 共享（降级中）" : "进程内（Redis 不可用）";
      }
      els.metricsNote.hidden = false;
      els.metricsNote.textContent = `进程运行 ${formatUptime(
        requests.uptime_sec,
      )} · 缓存会话 ${cached} 个 · 限流 ${rateBackend} · 请求指标按进程统计`;
    }
  } catch {
    els.metricsStats.innerHTML = "";
    if (els.metricsNote) {
      els.metricsNote.hidden = false;
      els.metricsNote.textContent = "无法获取运行指标";
    }
  }
}

async function refreshStatus() {
  const requestId = (statusRequestId += 1);
  const userId = state.userId;
  try {
    const data = await getJSON(`/api/status?user_id=${encodeURIComponent(userId)}`);
    // 丢弃过期响应：归档 / 切换用户后，旧请求可能晚到并覆盖新的归档计数
    if (requestId !== statusRequestId || userId !== state.userId) return;
    state.status = data;
    if (state.tripsExpanded && Number(data.trip_count || 0) > 0) {
      try {
        const tripsData = await getJSON(
          `/api/trips?user_id=${encodeURIComponent(userId)}&limit=200&order=desc`,
        );
        if (requestId !== statusRequestId || userId !== state.userId) return;
        state.tripsAll = tripsData.trips || [];
        state.tripsAllTotal = Number(tripsData.total || state.tripsAll.length);
      } catch {
        if (requestId !== statusRequestId || userId !== state.userId) return;
        state.tripsAll = [];
        state.tripsAllTotal = 0;
      }
    } else if (!data.trip_count) {
      state.tripsExpanded = false;
      state.tripsAll = [];
      state.tripsAllTotal = 0;
    }
  } catch {
    if (requestId !== statusRequestId || userId !== state.userId) return;
    state.status = null;
  }
  updateUserCard();
  renderOverview();
}

function renderOverview() {
  const status = state.status;
  if (!status) {
    els.overview.innerHTML = '<div class="empty">无法获取数据概览</div>';
    els.trips.innerHTML = "";
    els.destinations.innerHTML = "";
    els.upcoming.hidden = true;
    els.exportTrips.hidden = true;
    els.exportTripsCsv.hidden = true;
    els.expandTrips.hidden = true;
    els.stats.innerHTML = "";
    els.usageStats.innerHTML = "";
    els.usageAgents.innerHTML = "";
    els.usageTrend.hidden = true;
    els.storageChips.innerHTML = "";
    return;
  }

  const preferences = Object.entries(status.preferences || {}).filter(([, value]) => {
    if (Array.isArray(value)) return value.length > 0;
    return value !== null && value !== undefined && value !== "";
  });
  if (!preferences.length) {
    els.overview.innerHTML =
      '<div class="empty">还没有偏好记录。聊天时告诉我「我喜欢住汉庭」，我会记住。</div>';
  } else {
    els.overview.innerHTML = "";
    preferences.forEach(([key, value]) => {
      const row = document.createElement("div");
      row.className = "pref-row";
      const label = document.createElement("span");
      label.className = "pref-name";
      label.textContent = PREF_NAMES[key] || key;
      const val = document.createElement("span");
      val.className = "pref-value";
      val.textContent = Array.isArray(value) ? value.join("、") : String(value);

      const actions = document.createElement("span");
      actions.className = "pref-actions";
      const edit = document.createElement("button");
      edit.type = "button";
      edit.title = "编辑偏好";
      edit.setAttribute("aria-label", "编辑偏好");
      edit.innerHTML = icon("edit");
      edit.addEventListener("click", () => startEditPreference(row, key, value));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.title = "删除偏好";
      remove.setAttribute("aria-label", "删除偏好");
      remove.innerHTML = icon("trash");
      remove.addEventListener("click", () => deletePreference(key, label.textContent));
      actions.append(edit, remove);

      row.append(label, val, actions);
      els.overview.appendChild(row);
    });
  }

  const allTrips = state.tripsExpanded
    ? state.tripsAll
    : Array.isArray(status.trips)
      ? status.trips
      : [];
  const filterText = state.tripsExpanded ? state.tripsFilter.trim().toLowerCase() : "";
  const trips = filterText
    ? allTrips.filter((trip) =>
        [trip.origin, trip.destination, trip.purpose, trip.start_date, trip.end_date]
          .map((value) => String(value || ""))
          .join(" ")
          .toLowerCase()
          .includes(filterText),
      )
    : allTrips;
  els.tripFilterRow.hidden = !state.tripsExpanded;
  if (state.tripsExpanded) {
    // 刷新状态时保留筛选框内容与光标位置
    if (els.tripFilter.value !== state.tripsFilter) {
      els.tripFilter.value = state.tripsFilter;
    }
    els.tripFilterCount.textContent = filterText
      ? `匹配 ${trips.length} / ${allTrips.length} 条`
      : "";
  }
  els.trips.innerHTML = "";
  if (!trips.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = filterText ? "没有匹配的行程" : "还没有行程记录";
    els.trips.appendChild(empty);
  } else {
    // 状态接口按记录顺序（旧→新）返回，界面新→旧展示；
    // 展开列表已按 order=desc 取回最近若干条，直接使用
    const ordered = state.tripsExpanded ? [...trips] : [...trips].reverse();
    ordered.forEach((trip) => {
      const row = document.createElement("div");
      row.className = "trip";
      const route = document.createElement("div");
      route.className = "trip-route";
      const path = document.createElement("span");
      path.textContent = `${trip.origin || "—"} → ${trip.destination || "—"}`;
      const purpose = document.createElement("span");
      purpose.textContent = trip.purpose || "出行";
      route.append(path, purpose);
      const meta = document.createElement("div");
      meta.className = "trip-meta";
      const status = tripStatus(trip);
      if (status) {
        const badge = document.createElement("span");
        badge.className = `trip-status ${status.cls}`;
        badge.textContent = status.label;
        meta.appendChild(badge);
      }
      const conflicts = Array.isArray(trip.conflicts) ? trip.conflicts : [];
      if (conflicts.length) {
        const badge = document.createElement("span");
        badge.className = "trip-status conflict";
        badge.textContent = "重叠";
        badge.title = conflicts
          .map((item) => {
            const dates = item.start_date
              ? `（${item.start_date}${item.end_date ? ` ~ ${item.end_date}` : ""}）`
              : "";
            return `${item.origin || "—"} → ${item.destination || "—"}${dates}`;
          })
          .join("\n");
        badge.setAttribute("aria-label", `与其它行程日期重叠：${badge.title}`);
        meta.appendChild(badge);
      }
      const start = trip.start_date || "";
      const end = trip.end_date || "";
      meta.appendChild(
        document.createTextNode(
          start && end ? `${start} ~ ${end}` : start || end || "日期未记录",
        ),
      );
      row.append(route, meta);
      if (trip.session_id) {
        row.classList.add("clickable");
        row.title = "查看对应对话";
        row.addEventListener("click", () => {
          closeMemorySheet();
          openSession(trip.session_id);
        });
      }

      const actions = document.createElement("span");
      actions.className = "trip-actions";

      const edit = document.createElement("button");
      edit.type = "button";
      edit.title = "编辑行程";
      edit.setAttribute("aria-label", `编辑行程 ${path.textContent}`);
      edit.innerHTML = icon("edit");
      edit.addEventListener("click", (event) => {
        event.stopPropagation();
        startEditTrip(trip);
      });

      const calendar = document.createElement("button");
      calendar.type = "button";
      calendar.title = "加入日历（.ics）";
      calendar.setAttribute("aria-label", "加入日历");
      calendar.innerHTML = icon("calendar");
      calendar.addEventListener("click", (event) => {
        event.stopPropagation();
        window.open(
          `/api/trips/${encodeURIComponent(trip.trip_id)}/calendar?user_id=${encodeURIComponent(
            state.userId,
          )}`,
          "_blank",
        );
      });

      const remove = document.createElement("button");
      remove.type = "button";
      remove.title = "删除行程";
      remove.setAttribute("aria-label", `删除行程 ${path.textContent}`);
      remove.innerHTML = icon("trash");
      remove.addEventListener("click", async (event) => {
        event.stopPropagation();
        if (!window.confirm(`删除行程「${path.textContent}」？`)) return;
        remove.disabled = true;
        try {
          await getJSON(
            `/api/trips/${encodeURIComponent(trip.trip_id)}?user_id=${encodeURIComponent(
              state.userId,
            )}`,
            { method: "DELETE" },
          );
          toast("行程已删除");
          await refreshStatus();
        } catch (error) {
          if (error.status === 404) {
            toast("行程不存在，已刷新列表");
            await refreshStatus();
          } else {
            toast(error.message);
            remove.disabled = false;
          }
        }
      });

      actions.append(edit, calendar, remove);
      row.appendChild(actions);

      els.trips.appendChild(row);
    });
  }

  els.exportTrips.hidden = !trips.length;
  els.exportTripsCsv.hidden = !trips.length;
  const tripTotal = Number(status.trip_count || trips.length);
  els.expandTrips.hidden = !state.tripsExpanded && tripTotal <= 5;
  els.expandTrips.textContent = state.tripsExpanded
    ? "收起全部"
    : `查看全部（${tripTotal}）`;
  if (state.tripsExpanded && !filterText && state.tripsAllTotal > allTrips.length) {
    const note = document.createElement("div");
    note.className = "empty";
    note.textContent = `仅显示最近 ${allTrips.length} 条（共 ${state.tripsAllTotal} 条）`;
    els.trips.appendChild(note);
  }

  const frequent = (status.statistics && status.statistics.frequent_destinations) || {};  const destinations = Object.entries(frequent)
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, 5);
  els.destinations.innerHTML = "";
  destinations.forEach(([name, count]) => {
    const chip = document.createElement("span");
    chip.className = "badge";
    chip.textContent = `${name} ×${count}`;
    els.destinations.appendChild(chip);
  });

  renderUpcoming(status.next_trip || pickUpcomingTrip(trips));
  updateArchivedToggle(Number(status.archived_count || 0));

  const stats = [
    ["历史行程", status.trip_count || 0],
    ["对话消息", status.chat_count || 0],
    ["偏好条目", preferences.length],
  ];
  els.stats.innerHTML = "";
  stats.forEach(([label, value]) => {
    const box = document.createElement("div");
    box.className = "stat";
    const number = document.createElement("b");
    number.textContent = String(value);
    const caption = document.createElement("span");
    caption.textContent = label;
    box.append(number, caption);
    els.stats.appendChild(box);
  });

  els.storageChips.innerHTML = "";
  const storage = status.storage || {};
  const chips = [
    storage.short_term === "redis" ? "短期记忆 · Redis" : "短期记忆 · 进程内",
    storage.long_term === "postgres" ? "长期记忆 · PostgreSQL" : "长期记忆 · JSON",
  ];
  chips.forEach((text) => {
    const chip = document.createElement("span");
    chip.className = "badge";
    chip.textContent = text;
    els.storageChips.appendChild(chip);
  });
  const fallback =
    (storage.short_term_configured && storage.short_term !== "redis") ||
    (storage.long_term_configured && storage.long_term !== "postgres");
  if (fallback) {
    const warn = document.createElement("span");
    warn.className = "badge err";
    warn.textContent = "已配置的后端暂不可用 · 已回退本地";
    els.storageChips.appendChild(warn);
  }

  const usage = status.usage || {};
  const usageTiles = [
    ["调用次数", usage.calls || 0],
    ["总 tokens", Number(usage.total_tokens || 0).toLocaleString("zh-CN")],
    ["平均耗时", usage.avg_elapsed ? `${usage.avg_elapsed}s` : "—"],
  ];
  els.usageStats.innerHTML = "";
  usageTiles.forEach(([label, value]) => {
    const box = document.createElement("div");
    box.className = "stat";
    const number = document.createElement("b");
    number.textContent = String(value);
    const caption = document.createElement("span");
    caption.textContent = label;
    box.append(number, caption);
    els.usageStats.appendChild(box);
  });

  els.usageAgents.innerHTML = "";
  const topAgents = Array.isArray(usage.top_agents) ? usage.top_agents : [];
  if (!topAgents.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "暂无智能体调用记录";
    els.usageAgents.appendChild(empty);
  } else {
    topAgents.forEach((agent) => {
      const chip = document.createElement("span");
      chip.className = "badge";
      chip.textContent = `${AGENT_NAMES[agent.name] || agent.name} ×${agent.count}`;
      els.usageAgents.appendChild(chip);
    });
  }

  renderUsageTrend(usage);
}

function renderUsageTrend(usage) {
  const days = Array.isArray(usage.by_day) ? usage.by_day : [];
  const total = days.reduce((sum, day) => sum + (Number(day.tokens) || 0), 0);
  if (!days.length || total <= 0) {
    els.usageTrend.hidden = true;
    return;
  }
  els.usageTrend.hidden = false;

  const maxTokens = Math.max(...days.map((day) => Number(day.tokens) || 0), 1);
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(
    now.getDate(),
  ).padStart(2, "0")}`;

  els.usageTrendTotal.textContent = `7 天合计 ${total.toLocaleString("zh-CN")} tokens`;
  els.usageChart.innerHTML = "";
  els.usageTable.innerHTML = "";

  const caption = document.createElement("caption");
  caption.textContent = "近 7 天模型 tokens 用量";
  const headRow = document.createElement("tr");
  ["日期", "调用次数", "tokens"].forEach((text) => {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = text;
    headRow.appendChild(th);
  });
  const thead = document.createElement("thead");
  thead.appendChild(headRow);
  const tbody = document.createElement("tbody");

  days.forEach((day) => {
    const tokens = Number(day.tokens) || 0;
    const calls = Number(day.calls) || 0;

    const column = document.createElement("div");
    column.className = day.date === today ? "usage-day today" : "usage-day";
    const wrap = document.createElement("div");
    wrap.className = "usage-bar-wrap";
    const bar = document.createElement("span");
    bar.className = tokens ? "usage-bar" : "usage-bar empty";
    bar.style.height = `${tokens ? Math.max(6, Math.round((tokens / maxTokens) * 100)) : 3}%`;
    bar.title = `${day.date}：${calls} 次调用 · ${tokens.toLocaleString("zh-CN")} tokens`;
    bar.setAttribute("aria-hidden", "true");
    wrap.appendChild(bar);
    const label = document.createElement("span");
    label.className = "usage-day-label";
    label.textContent = String(day.date).slice(5);
    column.append(wrap, label);
    els.usageChart.appendChild(column);

    const row = document.createElement("tr");
    [
      day.date,
      String(calls),
      tokens.toLocaleString("zh-CN"),
    ].forEach((text) => {
      const cell = document.createElement("td");
      cell.textContent = text;
      row.appendChild(cell);
    });
    tbody.appendChild(row);
  });

  els.usageTable.append(caption, thead, tbody);
}

function todayISO() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(
    now.getDate(),
  ).padStart(2, "0")}`;
}

function tripStatus(trip) {
  const start = String(trip.start_date || "");
  if (!start) return null;
  const end = String(trip.end_date || start);
  const today = todayISO();
  if (today < start) return { label: "即将出行", cls: "upcoming" };
  if (today > end) return { label: "已结束", cls: "past" };
  return { label: "进行中", cls: "active" };
}

function pickUpcomingTrip(trips) {
  const candidates = (trips || [])
    .filter((trip) => trip.start_date)
    .map((trip) => ({ trip, status: tripStatus(trip) }))
    .filter((item) => item.status);
  const ongoing = candidates
    .filter((item) => item.status.cls === "active")
    .sort((a, b) =>
      String(b.trip.end_date || b.trip.start_date).localeCompare(
        String(a.trip.end_date || a.trip.start_date),
      ),
    );
  if (ongoing.length) return ongoing[0].trip;
  const upcoming = candidates
    .filter((item) => item.status.cls === "upcoming")
    .sort((a, b) => {
      const byDate = String(a.trip.start_date).localeCompare(String(b.trip.start_date));
      if (byDate !== 0) return byDate;
      return (b.trip.session_id ? 1 : 0) - (a.trip.session_id ? 1 : 0);
    })[0];
  return upcoming ? upcoming.trip : null;
}

function renderUpcoming(trip) {
  if (!trip) {
    els.upcoming.hidden = true;
    return;
  }
  const status = tripStatus(trip);
  els.upcoming.hidden = false;
  els.upcoming.classList.toggle("ongoing", Boolean(status && status.cls === "active"));
  els.upcoming.dataset.session = trip.session_id || "";
  els.upcomingLabel.textContent = status ? status.label : "即将出行";
  els.upcomingRoute.textContent = `${trip.origin || "—"} → ${trip.destination || "—"}`;
  els.upcomingDate.textContent = `${trip.start_date}${
    trip.end_date ? ` ~ ${trip.end_date}` : ""
  } · ${trip.purpose || "出行"}`;
}

const LIST_PREF_TYPES = new Set(["hotel_brands", "airlines"]);

function newPreferenceValue(type, text) {
  if (LIST_PREF_TYPES.has(type)) {
    const items = text
      .split(/[、，,]/)
      .map((item) => item.trim())
      .filter(Boolean);
    return items.length ? items : text;
  }
  if (type === "budget_level") {
    const number = Number(text);
    return Number.isFinite(number) ? number : text;
  }
  return text;
}

function resetPreferenceForm() {
  if (!els.prefForm) return;
  els.prefForm.hidden = true;
  els.prefForm.reset();
  els.prefType.value = "home_location";
}

async function addPreference(event) {
  event.preventDefault();
  const type = els.prefType.value;
  const text = els.prefValue.value.trim();
  if (!text) {
    toast("请输入偏好值");
    return;
  }
  els.prefSubmit.disabled = true;
  try {
    await getJSON(`/api/preferences/${encodeURIComponent(type)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: state.userId,
        value: newPreferenceValue(type, text),
      }),
    });
    toast("偏好已保存");
    resetPreferenceForm();
    await refreshStatus();
  } catch (error) {
    toast(error.message);
  } finally {
    els.prefSubmit.disabled = false;
  }
}

function preferenceInputValue(value) {
  if (Array.isArray(value)) return value.join("、");
  if (value !== null && typeof value === "object") return JSON.stringify(value);
  return String(value ?? "");
}

function preferenceOutputValue(original, text) {
  if (Array.isArray(original)) {
    // 与「添加偏好」保持一致：ASCII 逗号同样视为分隔符
    return text
      .split(/[、，,]/)
      .map((item) => item.trim())
      .filter(Boolean);
  }
  const trimmed = text.trim();
  if (
    typeof original === "number" &&
    trimmed !== "" &&
    !Number.isNaN(Number(trimmed))
  ) {
    return Number(trimmed);
  }
  if (typeof original === "boolean" && /^(true|false)$/i.test(trimmed)) {
    return trimmed.toLowerCase() === "true";
  }
  return trimmed;
}

function startEditPreference(row, type, currentValue) {
  const valueEl = row.querySelector(".pref-value");
  if (!valueEl || row.querySelector(".pref-input")) return;

  const input = document.createElement("input");
  input.className = "pref-input";
  input.value = preferenceInputValue(currentValue);
  valueEl.replaceWith(input);
  input.focus();
  input.select();
  let settled = false;

  const cancel = () => {
    if (settled) return;
    settled = true;
    if (input.isConnected) input.replaceWith(valueEl);
  };

  const save = async () => {
    if (settled) return;
    const text = input.value.trim();
    if (!text) {
      toast("偏好内容不能为空");
      input.focus();
      return;
    }
    settled = true;
    const value = preferenceOutputValue(currentValue, text);
    if (Array.isArray(value) && !value.length) {
      settled = false;
      toast("偏好内容不能为空");
      input.focus();
      return;
    }
    try {
      await getJSON(`/api/preferences/${encodeURIComponent(type)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: state.userId, value }),
      });
      toast("偏好已更新");
      await refreshStatus();
    } catch (error) {
      settled = false;
      toast(`保存失败：${error.message}`);
      input.focus();
    }
  };

  input.addEventListener("keydown", (event) => {
    event.stopPropagation();
    if (event.isComposing) return;
    if (event.key === "Enter") {
      event.preventDefault();
      save();
    } else if (event.key === "Escape") {
      event.preventDefault();
      cancel();
    }
  });
  input.addEventListener("blur", () => {
    if (!settled) cancel();
  });
}

async function deletePreference(type, label) {
  if (!window.confirm(`删除偏好「${label}」？`)) return;
  try {
    await getJSON(
      `/api/preferences/${encodeURIComponent(type)}?user_id=${encodeURIComponent(
        state.userId,
      )}`,
      { method: "DELETE" },
    );
    toast("偏好已删除");
    await refreshStatus();
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

async function refreshHealth() {
  try {
    const data = await getJSON("/api/health");
    state.health = data;
    els.health.className = "health ok";
    els.healthText.textContent = "在线";
    els.health.title = `${data.model || "模型"} · ${
      data.has_api_key ? "密钥已配置" : "未配置密钥"
    } · v${data.version || ""}`;
    if (data.model) els.modelChip.textContent = data.model;
  } catch {
    state.health = null;
    els.health.className = "health err";
    els.healthText.textContent = "服务未连接";
    els.health.title = "无法连接后端服务";
  }
  updateUserCard();
}

/* ---------- 主题 / 侧栏 ---------- */

function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("aligo.theme", theme);
  } catch {
    /* 隐私模式下忽略 */
  }
  updateThemeButton();
}

function updateThemeButton() {
  const dark = currentTheme() === "dark";
  els.themeToggle.innerHTML = icon(dark ? "sun" : "moon");
  els.themeToggle.title = dark ? "切换到浅色" : "切换到深色";
}

function isMobile() {
  return window.matchMedia("(max-width: 900px)").matches;
}

function focusSearch() {
  if (isMobile()) {
    els.app.classList.add("side-open");
    els.scrim.hidden = false;
  }
  els.historySearch.focus();
  els.historySearch.select();
}

function toggleSidebar() {
  if (isMobile()) {
    const open = !els.app.classList.contains("side-open");
    els.app.classList.toggle("side-open", open);
    els.scrim.hidden = !open;
  } else {
    const collapsed = els.app.classList.toggle("side-collapsed");
    try {
      localStorage.setItem("aligo.sideCollapsed", collapsed ? "1" : "0");
    } catch {
      /* 忽略 */
    }
  }
}

function closeMobileSidebar() {
  els.app.classList.remove("side-open");
  els.scrim.hidden = true;
}

function restoreSidebar() {
  if (!isMobile() && localStorage.getItem("aligo.sideCollapsed") === "1") {
    els.app.classList.add("side-collapsed");
  }
}

/* ---------- 记忆与数据面板 ---------- */

async function refreshKnowledge() {
  try {
    const data = await getJSON("/api/knowledge");
    els.knowledgeChip.innerHTML = "";
    const chip = document.createElement("span");
    chip.className = data.available ? "badge" : "badge err";
    chip.textContent = data.available
      ? `知识库 · ${Number(data.documents || 0).toLocaleString("zh-CN")} 片段`
      : `知识库 · ${data.reason || "不可用"}`;
    els.knowledgeChip.appendChild(chip);
  } catch {
    els.knowledgeChip.innerHTML = "";
  }
}

const KB_PAGE_SIZE = 20;
let kbSearchTimer = null;
// 知识库列表 / 语义检索共用的请求序号：只渲染最后一次发起的请求结果，
// 避免先发出的子串搜索晚到后覆盖语义检索结果（或反之）
let kbListSeq = 0;

function exitKbSemanticMode() {
  state.kbSemanticMode = false;
  state.kbSemanticQuery = "";
}

function knowledgeQueryParams(offset) {
  const params = new URLSearchParams({
    limit: String(KB_PAGE_SIZE),
    offset: String(offset),
  });
  if (state.kbQuery) params.set("q", state.kbQuery);
  if (state.kbCategory) params.set("category", state.kbCategory);
  return params;
}

async function refreshKnowledgeCategories() {
  if (!els.kbCategoryFilter) return;
  try {
    const data = await getJSON("/api/knowledge/categories");
    const categories = Array.isArray(data.categories) ? data.categories : [];
    const current = state.kbCategory;
    els.kbCategoryFilter.innerHTML = "";
    const all = document.createElement("option");
    all.value = "";
    all.textContent = "全部分类";
    els.kbCategoryFilter.appendChild(all);
    let found = false;
    categories.forEach((item) => {
      const option = document.createElement("option");
      option.value = String(item.category);
      option.textContent = `${item.category}（${item.count}）`;
      if (option.value === current) found = true;
      els.kbCategoryFilter.appendChild(option);
    });
    if (current && !found) {
      // 分类在刷新间消失（刚被改名 / 删除）：保留选项避免筛选静默失效
      const option = document.createElement("option");
      option.value = current;
      option.textContent = `${current}（0）`;
      els.kbCategoryFilter.appendChild(option);
    }
    els.kbCategoryFilter.value = current;
  } catch {
    // 分类统计失败不影响片段列表
  }
}

async function refreshKnowledgeDocuments() {
  if (!els.kbList) return;
  if (state.kbSemanticMode) {
    // 保持在语义检索模式：删除 / 编辑 / 导入后刷新的是当前语义结果，
    // 而不是悄悄切回关键词列表
    await runSemanticSearch({ silent: true });
    return;
  }
  const seq = ++kbListSeq;
  els.kbList.textContent = "加载中…";
  state.kbItems = [];
  state.kbTotal = 0;
  refreshKnowledgeCategories();
  try {
    const data = await getJSON(`/api/knowledge/documents?${knowledgeQueryParams(0)}`);
    if (seq !== kbListSeq) return;
    state.kbTotal = Number(data.total || 0);
    renderKnowledgeDocuments(data.items || [], false);
  } catch (error) {
    if (seq !== kbListSeq) return;
    els.kbList.textContent = error.message;
  }
  updateKnowledgeMore();
}

async function loadMoreKnowledgeDocuments() {
  if (!els.kbList || state.kbSemanticMode) return;
  const seq = kbListSeq;
  els.kbMore.disabled = true;
  try {
    const data = await getJSON(
      `/api/knowledge/documents?${knowledgeQueryParams(state.kbItems.length)}`,
    );
    // 语义检索可能在请求期间开始：不能再把关键词行追加进语义结果
    if (seq !== kbListSeq || state.kbSemanticMode) return;
    state.kbTotal = Number(data.total || 0);
    renderKnowledgeDocuments(data.items || [], true);
    els.kbList.focus();
  } catch (error) {
    if (seq === kbListSeq && !state.kbSemanticMode) toast(error.message);
  } finally {
    els.kbMore.disabled = false;
    // 期间可能已开始语义检索：不要把属于关键词列表的分页按钮重新显示出来
    if (seq === kbListSeq && !state.kbSemanticMode) updateKnowledgeMore();
  }
}

function updateKnowledgeMore() {
  if (!els.kbMore) return;
  if (state.kbSemanticMode) {
    // 语义结果不参与关键词分页
    els.kbMore.hidden = true;
    return;
  }
  els.kbMore.hidden = state.kbItems.length >= state.kbTotal;
}

function knowledgePreview(content, query) {
  const text = String(content || "");
  const needle = String(query || "").trim();
  if (!needle) {
    return text.length > 140 ? `${text.slice(0, 140)}…` : text;
  }
  const index = text.toLowerCase().indexOf(needle.toLowerCase());
  if (index < 0) {
    return text.length > 140 ? `${text.slice(0, 140)}…` : text;
  }
  const start = Math.max(0, index - 48);
  const end = Math.min(text.length, start + 140);
  return `${start > 0 ? "…" : ""}${text.slice(start, end)}${end < text.length ? "…" : ""}`;
}

function renderKnowledgeDocuments(items, append) {
  if (!append) {
    els.kbList.innerHTML = "";
    state.kbItems = [];
  }
  if (!append && !items.length) {
    const empty = document.createElement("p");
    empty.className = "kb-empty";
    empty.textContent = state.kbQuery
      ? "没有匹配的知识片段。"
      : "还没有知识片段，可以在下方添加。";
    els.kbList.appendChild(empty);
    return;
  }
  for (const doc of items) {
    const row = document.createElement("div");
    row.className = "pref-row kb-row";
    row.dataset.id = String(doc.id);

    const main = document.createElement("div");
    main.className = "kb-main";
    const text = document.createElement("div");
    text.className = "kb-text";
    const content = String(doc.content || "");
    const preview = knowledgePreview(content, state.kbQuery);
    if (state.kbQuery) {
      text.innerHTML = highlightSnippet(preview, state.kbQuery);
    } else {
      text.textContent = preview;
    }
    const meta = document.createElement("div");
    meta.className = "kb-meta";
    const bits = [`#${doc.id}`];
    if (doc.metadata && doc.metadata.category) bits.push(String(doc.metadata.category));
    if (doc.metadata && doc.metadata.title) bits.push(String(doc.metadata.title));
    const metaText = bits.join(" · ");
    if (state.kbQuery) {
      meta.innerHTML = highlightSnippet(metaText, state.kbQuery);
    } else {
      meta.textContent = metaText;
    }
    main.append(text, meta);
    row.append(main, knowledgeRowActions(doc));
    els.kbList.appendChild(row);
    state.kbItems.push(doc.id);
  }

  let note = els.kbList.querySelector(".kb-note");
  if (state.kbTotal > state.kbItems.length) {
    if (!note) {
      note = document.createElement("p");
      note.className = "kb-meta kb-note";
      els.kbList.prepend(note);
    }
    note.textContent = state.kbQuery
      ? `匹配 ${state.kbTotal} 条，已显示 ${state.kbItems.length} 条`
      : `共 ${state.kbTotal} 条，已显示 ${state.kbItems.length} 条`;
  } else if (note) {
    note.remove();
  }
}

function knowledgeRowActions(doc) {
  const actions = document.createElement("span");
  actions.className = "pref-actions";
  const edit = document.createElement("button");
  edit.type = "button";
  edit.title = "编辑片段";
  edit.setAttribute("aria-label", `编辑知识片段 ${doc.id}`);
  edit.innerHTML = icon("edit");
  edit.addEventListener("click", () => startEditKnowledge(doc));
  const del = document.createElement("button");
  del.type = "button";
  del.title = "删除片段";
  del.setAttribute("aria-label", `删除知识片段 ${doc.id}`);
  del.innerHTML = icon("trash");
  del.addEventListener("click", () => removeKnowledgeDocument(doc.id));
  actions.append(edit, del);
  return actions;
}

async function runSemanticSearch({ silent = false } = {}) {
  const query = silent ? state.kbSemanticQuery : els.kbSearch.value.trim();
  if (!query) {
    if (!silent) toast("请输入搜索内容");
    return;
  }
  // 取消尚未触发的子串搜索刷新，并让已发出的列表请求作废
  clearTimeout(kbSearchTimer);
  const seq = ++kbListSeq;
  els.kbSemantic.disabled = true;
  els.kbSemantic.textContent = "检索中…";
  // 语义检索期间隐藏「加载更多」：它属于关键词列表，点击结果不应掺入语义视图
  els.kbMore.hidden = true;
  els.kbMore.disabled = true;
  if (!silent) els.kbList.textContent = "语义检索中…（首次使用需加载向量模型）";
  try {
    const data = await getJSON(
      `/api/knowledge/search?q=${encodeURIComponent(query)}&top_k=8`,
    );
    if (seq !== kbListSeq) return;
    state.kbSemanticMode = true;
    state.kbSemanticQuery = query;
    renderSemanticResults(query, data.results || []);
  } catch (error) {
    if (seq !== kbListSeq) return;
    if (silent) {
      // 刷新语义结果失败（如知识库暂时不可用）：退回关键词列表，避免面板卡在旧结果
      exitKbSemanticMode();
      state.kbQuery = els.kbSearch.value.trim();
      toast(`语义结果刷新失败：${error.message}`);
      await refreshKnowledgeDocuments();
    } else {
      els.kbList.textContent = error.message;
    }
  } finally {
    els.kbSemantic.disabled = false;
    els.kbSemantic.textContent = "语义搜索";
    els.kbMore.disabled = false;
  }
}

async function runKbEval() {
  if (!els.kbEval) return;
  els.kbEval.disabled = true;
  els.kbEval.textContent = "评测中…";
  if (els.kbEvalResult) {
    els.kbEvalResult.hidden = false;
    els.kbEvalResult.textContent = "检索评测中…（首次使用需加载向量模型）";
  }
  try {
    const data = await getJSON("/api/knowledge/eval");
    const pct = (value) => `${Math.round(Number(value || 0) * 100)}%`;
    if (els.kbEvalResult) {
      els.kbEvalResult.textContent =
        `检索评测：${data.total} 个问题 · hit@1 ${pct(data.hit1)}` +
        ` · hit@3 ${pct(data.hit3)} · hit@5 ${pct(data.hit5)}` +
        ` · MRR ${Number(data.mrr || 0).toFixed(2)}`;
    }
  } catch (error) {
    if (els.kbEvalResult) els.kbEvalResult.textContent = `评测失败：${error.message}`;
  } finally {
    els.kbEval.disabled = false;
    els.kbEval.textContent = "检索评测";
  }
}

function renderSemanticResults(query, results) {  els.kbList.innerHTML = "";
  els.kbMore.hidden = true;
  const note = document.createElement("p");
  note.className = "kb-meta";
  note.textContent = `语义检索「${query}」：${results.length} 条结果（按相似度排序）`;
  els.kbList.appendChild(note);
  if (!results.length) {
    const empty = document.createElement("p");
    empty.className = "kb-empty";
    empty.textContent = "没有相关内容。";
    els.kbList.appendChild(empty);
  }
  results.forEach((doc) => {
    const row = document.createElement("div");
    row.className = "pref-row kb-row";
    row.dataset.id = String(doc.id);
    const main = document.createElement("div");
    main.className = "kb-main";
    const text = document.createElement("div");
    text.className = "kb-text";
    const content = String(doc.content || "");
    const preview =
      content.length > 160 ? `${content.slice(0, 160)}…` : content;
    text.innerHTML = highlightSnippet(preview, query);
    const meta = document.createElement("div");
    meta.className = "kb-meta";
    const bits = [`#${doc.id}`];
    if (doc.metadata && doc.metadata.category) {
      bits.push(String(doc.metadata.category));
    }
    if (doc.metadata && doc.metadata.title) {
      bits.push(String(doc.metadata.title));
    }
    bits.push(`相似度 ${Number(doc.distance || 0).toFixed(3)}`);
    meta.textContent = bits.join(" · ");
    main.append(text, meta);
    row.append(main, knowledgeRowActions(doc));
    els.kbList.appendChild(row);
  });
  const back = document.createElement("button");
  back.type = "button";
  back.className = "kb-more ghost";
  back.textContent = "返回全部片段";
  back.addEventListener("click", () => {
    exitKbSemanticMode();
    els.kbSearch.value = "";
    state.kbQuery = "";
    refreshKnowledgeDocuments();
  });
  els.kbList.appendChild(back);
}

async function removeKnowledgeDocument(docId) {
  if (state.kbSaving) {
    toast("正在保存知识片段，请稍候");
    return;
  }
  if (!window.confirm(`删除知识片段 #${docId}？`)) return;
  if (state.kbDeleting.has(docId)) return;
  state.kbDeleting.add(docId);
  const row = els.kbList.querySelector(`.kb-row[data-id="${docId}"]`);
  const button = row?.querySelector('button[title="删除片段"]');
  if (button) button.disabled = true;
  let removed = false;
  try {
    await getJSON(`/api/knowledge/documents/${docId}`, { method: "DELETE" });
    removed = true;
    toast("知识片段已删除");
  } catch (error) {
    if (error.status === 404) {
      removed = true;
      toast("知识片段已不存在，已刷新列表");
    } else {
      toast(error.message);
    }
  } finally {
    state.kbDeleting.delete(docId);
  }
  if (removed) {
    if (state.kbEditingId === docId) resetKnowledgeForm();
    await Promise.all([refreshKnowledge(), refreshKnowledgeDocuments()]);
    els.kbList.focus();
  } else if (button) {
    button.disabled = false;
  }
}

function startEditKnowledge(doc) {
  if (state.kbSaving) {
    toast("正在保存知识片段，请稍候");
    return;
  }
  state.kbEditingId = doc.id;
  // 保留完整 metadata：PUT 会整体覆盖，只回填分类会丢掉 title / source 等字段
  state.kbEditingMetadata = { ...(doc.metadata || {}) };
  els.kbContent.value = doc.content || "";
  els.kbCategory.value = doc.metadata?.category ? String(doc.metadata.category) : "";
  els.kbSubmit.textContent = "保存修改";
  els.kbCancel.hidden = false;
  els.kbContent.focus();
  els.kbContent.setSelectionRange(
    els.kbContent.value.length,
    els.kbContent.value.length,
  );
}

function resetKnowledgeForm({ clear = true } = {}) {
  state.kbEditingId = null;
  state.kbEditingMetadata = {};
  els.kbSubmit.textContent = "添加片段";
  els.kbCancel.hidden = true;
  if (clear) {
    els.kbContent.value = "";
    els.kbCategory.value = "";
  }
}

async function addKnowledgeDocument(event) {
  event.preventDefault();
  if (state.kbSaving) return;
  const content = els.kbContent.value.trim();
  if (!content) {
    toast("请输入知识内容");
    return;
  }
  const editingId = state.kbEditingId;
  const metadata = editingId ? { ...state.kbEditingMetadata } : {};
  const category = els.kbCategory.value.trim();
  if (category) metadata.category = category;
  else delete metadata.category;
  state.kbSaving = true;
  els.kbSubmit.disabled = true;
  els.kbCancel.disabled = true;
  els.kbList
    .querySelectorAll(".pref-actions button")
    .forEach((button) => (button.disabled = true));
  els.kbSubmit.textContent = editingId ? "保存中…" : "添加中…";
  try {
    await getJSON(
      editingId
        ? `/api/knowledge/documents/${encodeURIComponent(editingId)}`
        : "/api/knowledge/documents",
      {
        method: editingId ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content, metadata }),
      },
    );
    state.kbSaving = false;
    toast(editingId ? "知识片段已更新" : "知识片段已添加");
    // 提交期间用户可能继续输入下一条，只清空仍是本次提交内容的输入框
    if (els.kbContent.value.trim() === content) els.kbContent.value = "";
    if (els.kbCategory.value.trim() === category) els.kbCategory.value = "";
    if (state.kbEditingId === editingId) resetKnowledgeForm({ clear: false });
    await Promise.all([refreshKnowledge(), refreshKnowledgeDocuments()]);
  } catch (error) {
    toast(error.message);
  } finally {
    state.kbSaving = false;
    els.kbSubmit.disabled = false;
    els.kbCancel.disabled = false;
    els.kbList
      .querySelectorAll(".pref-actions button")
      .forEach((button) => (button.disabled = false));
    els.kbSubmit.textContent = state.kbEditingId ? "保存修改" : "添加片段";
  }
}

const KB_IMPORT_MAX_BYTES = 2 * 1024 * 1024;

async function importKnowledgeFile() {
  if (state.kbImporting) return;
  state.kbImporting = true;
  els.kbImport.disabled = true;
  let imported = 0;
  let skipped = 0;
  try {
    const file = els.kbImportFile.files && els.kbImportFile.files[0];
    els.kbImportFile.value = "";
    if (!file) return;
    if (file.size > KB_IMPORT_MAX_BYTES) {
      toast("JSON 文件不能超过 2 MB");
      return;
    }
    let data;
    try {
      data = JSON.parse(await file.text());
    } catch {
      toast("无法解析 JSON 文件");
      return;
    }
    const documents = Array.isArray(data)
      ? data
      : data && Array.isArray(data.documents)
        ? data.documents
        : null;
    if (!documents || !documents.length) {
      toast("文件中没有知识片段（需要 documents 数组）");
      return;
    }
    if (documents.length > 16384) {
      toast("单次最多导入 16384 条知识片段");
      return;
    }
    if (!window.confirm(`导入 ${documents.length} 条知识片段？`)) return;

    els.kbImport.textContent = "导入中…";
    // 接口单次最多 200 条；导出文件可能更大，按批上传保证备份可完整恢复
    for (let start = 0; start < documents.length; start += 200) {
      const batch = documents.slice(start, start + 200);
      const result = await getJSON("/api/knowledge/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ documents: batch }),
      });
      imported += result.imported || 0;
      skipped += result.skipped || 0;
      els.kbImport.textContent = `导入中… ${Math.min(
        start + batch.length,
        documents.length,
      )}/${documents.length}`;
    }
    toast(`已导入 ${imported} 条知识片段${skipped ? `，跳过 ${skipped} 条` : ""}`);
    await Promise.all([refreshKnowledge(), refreshKnowledgeDocuments()]);
  } catch (error) {
    if (imported > 0) {
      toast(`已导入 ${imported} 条后失败：${error.message}（重试会跳过已导入内容）`);
      await Promise.all([refreshKnowledge(), refreshKnowledgeDocuments()]);
    } else {
      toast(error.message);
    }
  } finally {
    state.kbImporting = false;
    els.kbImport.disabled = false;
    els.kbImport.textContent = "导入 JSON";
  }
}

const DOCTOR_MARKS = { ok: "✓", warn: "⚠", fail: "✗", skip: "—" };

async function runDoctorCheck() {
  els.runDoctor.disabled = true;
  els.runDoctor.textContent = "检查中…";
  els.doctorList.textContent = "正在检查…";
  try {
    const query = els.doctorNetwork.checked ? "?network=true" : "";
    const data = await getJSON(`/api/doctor${query}`);
    renderDoctorChecks(data.checks || []);
  } catch (error) {
    els.doctorList.textContent = `自检失败：${error.message}`;
  } finally {
    els.runDoctor.disabled = false;
    els.runDoctor.textContent = "运行自检";
  }
}

function renderDoctorChecks(checks) {
  els.doctorList.innerHTML = "";
  for (const check of checks) {
    const row = document.createElement("div");
    row.className = `doctor-row ${check.status}`;
    const mark = document.createElement("span");
    mark.className = "doctor-status";
    mark.textContent = DOCTOR_MARKS[check.status] || "?";
    const name = document.createElement("span");
    name.className = "doctor-name";
    name.textContent = check.name;
    const detail = document.createElement("span");
    detail.className = "doctor-detail";
    detail.textContent = check.detail;
    row.append(mark, name, detail);
    els.doctorList.appendChild(row);
  }
}

function memorySheetFocusable() {
  const card = els.memorySheet.querySelector(".sheet-card");
  if (!card) return [];
  return [
    ...card.querySelectorAll(
      'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), ' +
        'select:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
    ),
  ].filter((el) => el.offsetParent !== null || el === document.activeElement);
}

function trapMemorySheetFocus(event) {
  if (
    event.key !== "Tab" ||
    els.memorySheet.hidden ||
    !els.shortcutOverlay.hidden
  ) {
    // 快捷键面板是叠在记忆面板之上的独立模态，Tab 由它自己处理
    return;
  }
  const focusable = memorySheetFocusable();
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  const inside = els.memorySheet.contains(active);
  if (event.shiftKey && (active === first || !inside)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (active === last || !inside)) {
    event.preventDefault();
    first.focus();
  }
}

function openMemorySheet() {
  memorySheetOpener = document.activeElement;
  els.userIdInput.value = state.userId;
  els.memorySheet.hidden = false;
  els.closeMemory.focus();
  refreshStatus();
  refreshKnowledge();
  refreshKnowledgeDocuments();
  refreshMetrics();
}

function closeMemorySheet() {
  if (els.memorySheet.hidden) return;
  els.memorySheet.hidden = true;
  const opener = memorySheetOpener;
  memorySheetOpener = null;
  if (opener && opener.isConnected && !opener.hidden) {
    opener.focus();
    return;
  }
  // 触发按钮已被移除或隐藏时回到输入框，避免焦点落到 body
  els.input?.focus();
}

function openShortcuts() {
  if (!els.shortcutOverlay.hidden) return;
  const active = document.activeElement;
  shortcutOpener =
    active instanceof HTMLElement && active !== document.body ? active : null;
  els.shortcutOverlay.hidden = false;
  els.shortcutsBtn.setAttribute("aria-expanded", "true");
  els.closeShortcuts.focus();
}

function closeShortcuts() {
  if (els.shortcutOverlay.hidden) return;
  els.shortcutOverlay.hidden = true;
  els.shortcutsBtn.setAttribute("aria-expanded", "false");
  const opener = shortcutOpener;
  shortcutOpener = null;
  if (opener && opener.isConnected && !opener.hidden) {
    opener.focus();
    // 不可聚焦的打开者（如 html / body）focus 后仍留在 body，回退到按钮
    if (document.activeElement === opener) return;
  }
  els.shortcutsBtn.focus();
}

async function saveUser() {
  if (state.busy) {
    toast("请等待当前回复完成");
    return;
  }
  const value = els.userIdInput.value.trim() || "web_user";
  if (!isValidId(value)) {
    toast("用户 ID 只能包含中文、字母、数字、下划线和短横线");
    return;
  }
  if (value !== state.userId) {
    sessionLoadToken += 1;
    state.userId = value;
    els.historySearch.value = "";
    state.searchQuery = "";
    clearTimeout(searchTimer);
    try {
      localStorage.setItem("aligo.userId", value);
      sessionStorage.setItem("aligo.userId", value);
    } catch {
      /* 忽略 */
    }
    state.sessionId = null;
    sessionStorage.removeItem("aligo.sessionId");
    syncSessionControls();
    setConversationTitle("新对话");
    showWelcome();
    updateUserCard();
    state.showArchived = false;
    state.archivedCount = 0;
    updateArchivedToggle(0);
    // 清掉上一个用户遗留的行程 / 知识编辑状态，避免提交到新用户
    resetTripForm();
    els.tripForm.hidden = true;
    resetKnowledgeForm();
    resetPreferenceForm();
    state.tripsExpanded = false;
    state.tripsAll = [];
    state.tripsAllTotal = 0;
    state.tripsFilter = "";
    // 立即清空上一个用户的列表与面板：新用户的 /api/sessions、/api/status
    // 返回前旧行仍可点击，而删除 / 编辑按当前 user_id 执行，会误伤新用户的同名记录
    closeMemorySheet();
    state.status = null;
    state.sessions = [];
    state.sessionTotal = 0;
    state.archivedMatches = 0;
    els.history.innerHTML = "";
    renderOverview();
    await loadSessions(state.searchQuery, { reset: true });
    refreshStatus();
  }
  toast("已切换用户");
}

/* ---------- 输入框 ---------- */

function resizeInput() {
  els.input.style.height = "auto";
  els.input.style.height = `${Math.min(els.input.scrollHeight, 180)}px`;
}

/* ---------- 事件绑定 ---------- */

function bindEvents() {
  els.composer.addEventListener("submit", (event) => {
    event.preventDefault();
    sendMessage(els.input.value);
  });

  els.input.addEventListener("input", resizeInput);
  els.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendMessage(els.input.value);
    }
  });

  els.stopBtn.addEventListener("click", () => {
    if (state.controller) state.controller.abort();
  });

  els.kbForm.addEventListener("submit", addKnowledgeDocument);
  els.kbCancel.addEventListener("click", () => resetKnowledgeForm());
  els.kbSemantic.addEventListener("click", () => runSemanticSearch());
  if (els.kbEval) els.kbEval.addEventListener("click", () => runKbEval());
  els.kbImport.addEventListener("click", () => els.kbImportFile.click());
  els.kbImportFile.addEventListener("change", importKnowledgeFile);
  els.kbSearch.addEventListener("input", () => {
    clearTimeout(kbSearchTimer);
    kbSearchTimer = setTimeout(() => {
      exitKbSemanticMode();
      state.kbQuery = els.kbSearch.value.trim();
      refreshKnowledgeDocuments();
    }, 250);
  });
  els.kbSearch.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing) {
      event.preventDefault();
      clearTimeout(kbSearchTimer);
      exitKbSemanticMode();
      state.kbQuery = els.kbSearch.value.trim();
      refreshKnowledgeDocuments();
    }
  });
  els.kbCategoryFilter.addEventListener("change", () => {
    exitKbSemanticMode();
    state.kbCategory = els.kbCategoryFilter.value;
    refreshKnowledgeDocuments();
  });
  els.kbMore.addEventListener("click", loadMoreKnowledgeDocuments);
  els.runDoctor.addEventListener("click", runDoctorCheck);

  document.addEventListener("click", (event) => {
    const example = event.target.closest("[data-example]");
    if (example) {
      sendMessage(example.dataset.example);
      return;
    }
    const copyButton = event.target.closest(".code-copy");
    if (copyButton) {
      const code = copyButton.closest("pre")?.querySelector("code");
      copyText(code ? code.textContent : "");
    }
  });

  byId("newSession").addEventListener("click", newSession);
  els.archivedToggle.addEventListener("click", () => {
    state.showArchived = !state.showArchived;
    updateArchivedToggle();
    loadSessions(state.searchQuery, { reset: true });
  });
  byId("brandHome").addEventListener("click", (event) => {
    event.preventDefault();
    newSession();
  });

  els.historySearch.addEventListener("input", () => {
    clearTimeout(searchTimer);
    const value = els.historySearch.value;
    searchTimer = setTimeout(() => loadSessions(value, { reset: true }), 250);
  });
  els.historySearch.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing) {
      event.preventDefault();
      clearTimeout(searchTimer);
      loadSessions(els.historySearch.value, { reset: true });
    }
  });

  els.upcoming.addEventListener("click", () => {
    if (els.upcoming.dataset.session) {
      openSession(els.upcoming.dataset.session);
      closeMobileSidebar();
    } else {
      openMemorySheet();
    }
  });

  byId("openMemory").addEventListener("click", openMemorySheet);
  els.closeMemory.addEventListener("click", closeMemorySheet);
  // Tab 监听绑在 document：面板内元素被重渲染 / 禁用导致焦点回到 body 时，
  // 下一次 Tab 仍会被拉回面板（绑在面板上会收不到事件）
  document.addEventListener("keydown", trapMemorySheetFocus);
  els.memorySheet.addEventListener("click", (event) => {
    if (event.target === els.memorySheet) closeMemorySheet();
  });
  els.saveUser.addEventListener("click", saveUser);
  els.exportData.addEventListener("click", () => {
    window.open(`/api/export?user_id=${encodeURIComponent(state.userId)}`, "_blank");
  });
  els.exportDataMd.addEventListener("click", () => {
    window.open(
      `/api/export?user_id=${encodeURIComponent(state.userId)}&format=md`,
      "_blank",
    );
  });
  els.exportSessions.addEventListener("click", () => {
    window.open(
      `/api/sessions/export?user_id=${encodeURIComponent(state.userId)}`,
      "_blank",
    );
  });

  els.themeToggle.addEventListener("click", () => {
    applyTheme(currentTheme() === "dark" ? "light" : "dark");
  });

  els.shortcutsBtn.addEventListener("click", openShortcuts);
  els.closeShortcuts.addEventListener("click", closeShortcuts);
  els.shortcutOverlay.addEventListener("click", (event) => {
    if (event.target === els.shortcutOverlay) closeShortcuts();
  });
  els.shortcutOverlay.addEventListener("keydown", (event) => {
    if (event.key === "Tab") {
      event.preventDefault();
      els.closeShortcuts.focus();
    }
  });

  els.printChat.addEventListener("click", () => {
    if (!cancelPendingEdits()) {
      toast("正在保存修改，请稍候");
      return;
    }
    window.print();
  });

  els.copyLink.addEventListener("click", () => {
    if (!state.sessionId) return;
    copyText(shareUrl(), "链接已复制");
  });

  els.exportChat.addEventListener("click", triggerExport);
  els.exportHtml.addEventListener("click", exportSessionHtml);
  els.exportTrips.addEventListener("click", () => {
    window.open(
      `/api/trips/calendar?user_id=${encodeURIComponent(state.userId)}`,
      "_blank",
    );
  });
  els.exportTripsCsv.addEventListener("click", () => {
    window.open(
      `/api/trips/export?user_id=${encodeURIComponent(state.userId)}`,
      "_blank",
    );
  });
  els.addPrefBtn.addEventListener("click", () => {
    els.prefForm.hidden = !els.prefForm.hidden;
    if (!els.prefForm.hidden) els.prefValue.focus();
  });
  els.prefForm.addEventListener("submit", addPreference);
  els.prefCancel.addEventListener("click", resetPreferenceForm);

  els.expandTrips.addEventListener("click", toggleTripsExpanded);
  els.tripFilter.addEventListener("input", () => {
    state.tripsFilter = els.tripFilter.value;
    renderOverview();
  });
  els.addTripBtn.addEventListener("click", () => {    if (els.tripForm.hidden) {
      resetTripForm();
      els.tripForm.hidden = false;
      els.tripDestination.focus();
    } else {
      els.tripForm.hidden = true;
      resetTripForm();
    }
  });
  els.tripForm.addEventListener("submit", addTrip);

  els.menuBtn.addEventListener("click", toggleSidebar);
  els.collapseSidebar.addEventListener("click", toggleSidebar);
  els.scrim.addEventListener("click", closeMobileSidebar);

  window.addEventListener("keydown", (event) => {
    const target = event.target;
    const typing =
      target instanceof HTMLElement &&
      (target.tagName === "INPUT" ||
        target.tagName === "TEXTAREA" ||
        target.tagName === "SELECT" ||
        target.isContentEditable);

    if (event.key === "Escape" && !els.shortcutOverlay.hidden) {
      event.preventDefault();
      closeShortcuts();
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      focusSearch();
      return;
    }
    if (event.key === "/" && !typing) {
      event.preventDefault();
      focusSearch();
      return;
    }
    if (
      event.key === "?" &&
      !typing &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.altKey
    ) {
      event.preventDefault();
      openShortcuts();
      return;
    }
    if (event.key === "Escape") {
      if (document.activeElement === els.historySearch && els.historySearch.value) {
        clearTimeout(searchTimer);
        els.historySearch.value = "";
        loadSessions("", { reset: true });
        return;
      }
      closeMemorySheet();
      closeMobileSidebar();
    }
  });

  window.addEventListener("resize", () => {
    if (!isMobile()) {
      els.app.classList.remove("side-open");
      els.scrim.hidden = true;
    }
    resizeInput();
  });
}

/* ---------- 启动 ---------- */

async function init() {
  welcomeTemplate = els.messages.querySelector("[data-welcome]").cloneNode(true);
  els.userIdInput.value = state.userId;
  bindEvents();
  updateThemeButton();
  restoreSidebar();
  updateUserCard();
  resizeInput();

  await Promise.all([refreshHealth(), loadSessions(), refreshStatus()]);

  if (state.sessionId) {
    await openSession(state.sessionId, { silent: true });
  } else {
    showWelcome();
  }
  syncSessionControls();
  els.input.focus();
  requestAnimationFrame(resizeInput);
}

init();
