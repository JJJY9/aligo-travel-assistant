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
  userId: localStorage.getItem("aligo.userId") || "web_user",
  sessionId: sessionStorage.getItem("aligo.sessionId") || null,
  sessions: [],
  searchQuery: "",
  status: null,
  health: null,
  busy: false,
  controller: null,
  lastUserText: "",
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
      localStorage.setItem("aligo.userId", urlUser);
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
  copyLink: byId("copyLink"),
  menuBtn: byId("menuBtn"),
  collapseSidebar: byId("collapseSidebar"),
  scrim: byId("scrim"),
  memorySheet: byId("memorySheet"),
  closeMemory: byId("closeMemory"),
  saveUser: byId("saveUser"),
  exportData: byId("exportData"),
  overview: byId("overview"),
  trips: byId("trips"),
  stats: byId("stats"),
  usageStats: byId("usageStats"),
  usageAgents: byId("usageAgents"),
  storageChips: byId("storageChips"),
  toast: byId("toast"),
};

let welcomeTemplate = null;
let sessionLoadToken = 0;
let sessionsRequestId = 0;

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
    throw new Error(await errorMessage(response));
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

function regenerate(node, text) {
  if (state.busy) return;
  trimRegenButtons();
  node.remove();
  sendMessage(text, { echo: false });
}

function trimRegenButtons() {
  const buttons = [...els.messages.querySelectorAll('[data-action="regen"]')];
  buttons.slice(0, -1).forEach((button) => button.remove());
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
  els.copyLink.hidden = !state.sessionId;
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

function renderHistory() {
  els.history.innerHTML = "";
  if (!state.sessions.length) {
    const empty = document.createElement("div");
    empty.className = "history-empty";
    empty.textContent = state.searchQuery ? "没有匹配的对话" : "还没有历史对话";
    els.history.appendChild(empty);
    return;
  }

  const groups = new Map();
  state.sessions.forEach((session) => {
    const label = dateGroupLabel(session.updated_at);
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(session);
  });

  groups.forEach((items, label) => {
    const section = document.createElement("div");
    section.className = "history-group";
    const heading = document.createElement("h3");
    heading.textContent = label;
    section.appendChild(heading);

    items.forEach((session) => {
      const item = document.createElement("div");
      item.className = "history-item";
      item.dataset.id = session.session_id;
      item.setAttribute("role", "button");
      item.setAttribute("tabindex", "0");
      if (session.session_id === state.sessionId) item.classList.add("active");

      const title = document.createElement("span");
      title.className = "hi-title";
      title.textContent = session.title || "新对话";
      item.appendChild(title);

      if (session.snippet) {
        const snippet = document.createElement("span");
        snippet.className = "hi-snippet";
        snippet.textContent = session.snippet;
        item.appendChild(snippet);
      }

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "hi-del";
      remove.title = "删除会话";
      remove.setAttribute("aria-label", "删除会话");
      remove.innerHTML = icon("trash");
      remove.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteSession(session.session_id);
      });
      item.appendChild(remove);

      item.addEventListener("click", () => openSession(session.session_id));
      item.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openSession(session.session_id);
        }
      });
      section.appendChild(item);
    });

    els.history.appendChild(section);
  });
}

async function loadSessions(query = state.searchQuery) {
  state.searchQuery = String(query || "").trim();
  const requestId = (sessionsRequestId += 1);
  const queryParam = state.searchQuery
    ? `&q=${encodeURIComponent(state.searchQuery)}`
    : "";
  try {
    const data = await getJSON(
      `/api/sessions?user_id=${encodeURIComponent(state.userId)}${queryParam}`,
    );
    if (requestId !== sessionsRequestId) return;
    state.sessions = data.sessions || [];
  } catch {
    if (requestId !== sessionsRequestId) return;
    state.sessions = [];
  }
  renderHistory();
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
    setConversationTitle(session ? session.title : "对话");

    els.messages.innerHTML = "";
    let lastUserText = "";
    data.messages.forEach((message) => {
      if (message.role === "user") {
        lastUserText = message.content;
        els.messages.appendChild(renderUserNode(message.content));
      } else {
        els.messages.appendChild(
          renderAssistantNode(message.content, {
            timestamp: message.timestamp,
            regenText: lastUserText,
            meta: message.meta || null,
          }),
        );
      }
    });
    trimRegenButtons();
    markActiveHistory();
    scrollToBottom(true);
    closeMobileSidebar();
  } catch (error) {
    if (!options.silent) toast(`加载会话失败：${error.message}`);
  }
}

async function deleteSession(sessionId) {
  if (!window.confirm("删除这个会话？聊天记录会从长期记忆中移除。")) return;
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

  sessionLoadToken += 1;
  state.lastUserText = message;
  state.controller = new AbortController();
  setBusy(true);
  trimRegenButtons();

  mutate(() => {
    hideWelcome();
    if (options.echo !== false) {
      els.messages.appendChild(renderUserNode(message));
    }
  }, true);
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
    els.input.focus();
    await loadSessions();
    refreshStatus();
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
  return parts;
}

function updateUserCard() {
  const userId = state.userId;
  els.userLabel.textContent = userId;
  els.userAvatar.textContent = (userId || "旅").slice(0, 1).toUpperCase();
  const storage = (state.status && state.status.storage) || (state.health && state.health.storage);
  els.storageLabel.textContent = storageParts(storage).join(" · ");
}

async function refreshStatus() {
  try {
    state.status = await getJSON(`/api/status?user_id=${encodeURIComponent(state.userId)}`);
  } catch {
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
    els.stats.innerHTML = "";
    els.usageStats.innerHTML = "";
    els.usageAgents.innerHTML = "";
    els.storageChips.innerHTML = "";
    return;
  }

  const preferences = Object.entries(status.preferences || {}).filter(([, value]) => {
    return Array.isArray(value) ? value.length : Boolean(value);
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

  const trips = Array.isArray(status.trips) ? status.trips : [];
  els.trips.innerHTML = "";
  if (!trips.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "还没有行程记录";
    els.trips.appendChild(empty);
  } else {
    [...trips].reverse().forEach((trip) => {
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
      const start = trip.start_date || "";
      const end = trip.end_date || "";
      meta.textContent =
        start && end ? `${start} ~ ${end}` : start || end || "日期未记录";
      row.append(route, meta);
      els.trips.appendChild(row);
    });
  }

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
}

function startEditPreference(row, type, currentValue) {
  const valueEl = row.querySelector(".pref-value");
  if (!valueEl || row.querySelector(".pref-input")) return;

  const input = document.createElement("input");
  input.className = "pref-input";
  input.value = Array.isArray(currentValue)
    ? currentValue.join("、")
    : String(currentValue ?? "");
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
    const value = Array.isArray(currentValue)
      ? text
          .split(/[、,，]/)
          .map((item) => item.trim())
          .filter(Boolean)
      : text;
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

function openMemorySheet() {
  els.userIdInput.value = state.userId;
  els.memorySheet.hidden = false;
  refreshStatus();
}

function closeMemorySheet() {
  els.memorySheet.hidden = true;
}

async function saveUser() {
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
    try {
      localStorage.setItem("aligo.userId", value);
    } catch {
      /* 忽略 */
    }
    state.sessionId = null;
    sessionStorage.removeItem("aligo.sessionId");
    syncSessionControls();
    setConversationTitle("新对话");
    showWelcome();
    updateUserCard();
    await loadSessions();
    refreshStatus();
  }
  closeMemorySheet();
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
  byId("brandHome").addEventListener("click", (event) => {
    event.preventDefault();
    newSession();
  });

  let searchTimer = null;
  els.historySearch.addEventListener("input", () => {
    clearTimeout(searchTimer);
    const value = els.historySearch.value;
    searchTimer = setTimeout(() => loadSessions(value), 250);
  });
  els.historySearch.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      clearTimeout(searchTimer);
      loadSessions(els.historySearch.value);
    }
  });

  byId("openMemory").addEventListener("click", openMemorySheet);
  els.closeMemory.addEventListener("click", closeMemorySheet);
  els.memorySheet.addEventListener("click", (event) => {
    if (event.target === els.memorySheet) closeMemorySheet();
  });
  els.saveUser.addEventListener("click", saveUser);
  els.exportData.addEventListener("click", () => {
    window.open(`/api/export?user_id=${encodeURIComponent(state.userId)}`, "_blank");
  });

  els.themeToggle.addEventListener("click", () => {
    applyTheme(currentTheme() === "dark" ? "light" : "dark");
  });

  els.copyLink.addEventListener("click", () => {
    if (!state.sessionId) return;
    copyText(shareUrl(), "链接已复制");
  });

  els.exportChat.addEventListener("click", () => {
    if (!state.sessionId) return;
    const url = `/api/sessions/${encodeURIComponent(
      state.sessionId,
    )}/export?user_id=${encodeURIComponent(state.userId)}`;
    window.open(url, "_blank");
  });

  els.menuBtn.addEventListener("click", toggleSidebar);
  els.collapseSidebar.addEventListener("click", toggleSidebar);
  els.scrim.addEventListener("click", closeMobileSidebar);

  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
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
