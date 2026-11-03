/* Aligo Web 前端（无构建、无外部依赖） */
const AGENT_NAMES = {
  event_collection: "事项收集",
  preference: "偏好管理",
  itinerary_planning: "行程规划",
  information_query: "信息查询",
  rag_knowledge: "知识库查询",
  memory_query: "记忆查询",
};

const state = {
  userId: localStorage.getItem("aligo.userId") || "web_user",
  sessionId: sessionStorage.getItem("aligo.sessionId") || null,
  busy: false,
};

const el = {
  messages: document.getElementById("messages"),
  input: document.getElementById("input"),
  send: document.getElementById("send"),
  userId: document.getElementById("userId"),
  overview: document.getElementById("overview"),
  sessionInfo: document.getElementById("sessionInfo"),
  conn: document.getElementById("connState"),
};

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function inline(text) {
  return text
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

/* 安全的极简 Markdown 渲染：先转义 HTML，再处理标题/列表/引用/加粗 */
function renderMarkdown(text) {
  const lines = escapeHtml(text).split("\n");
  const out = [];
  let paragraph = [];
  let inList = false;

  const flushParagraph = () => {
    if (paragraph.length) {
      out.push(`<p>${paragraph.join("<br>")}</p>`);
      paragraph = [];
    }
  };
  const closeList = () => {
    if (inList) {
      out.push("</ul>");
      inList = false;
    }
  };

  for (const line of lines) {
    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    if (heading) {
      flushParagraph();
      closeList();
      const level = heading[1].length + 1;
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }
    if (/^[-*]\s+/.test(line)) {
      flushParagraph();
      if (!inList) {
        out.push("<ul>");
        inList = true;
      }
      out.push(`<li>${inline(line.replace(/^[-*]\s+/, ""))}</li>`);
      continue;
    }
    if (/^>\s?/.test(line)) {
      flushParagraph();
      closeList();
      out.push(`<blockquote>${inline(line.replace(/^>\s?/, ""))}</blockquote>`);
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      closeList();
      continue;
    }
    closeList();
    paragraph.push(inline(line));
  }
  flushParagraph();
  closeList();
  return out.join("\n");
}

function makeBadge(text, isError) {
  const badge = document.createElement("span");
  badge.className = isError ? "badge err" : "badge";
  badge.textContent = (isError ? "✗ " : "✓ ") + text;
  return badge;
}

function appendMessage(role, html, metaNodes = []) {
  const wrapper = document.createElement("div");
  wrapper.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerHTML = html;
  wrapper.appendChild(bubble);
  if (metaNodes.length) {
    const meta = document.createElement("div");
    meta.className = "meta";
    metaNodes.forEach((node) => meta.appendChild(node));
    wrapper.appendChild(meta);
  }
  el.messages.appendChild(wrapper);
  el.messages.scrollTop = el.messages.scrollHeight;
  return wrapper;
}

function appendTyping() {
  const wrapper = document.createElement("div");
  wrapper.className = "msg assistant";
  wrapper.innerHTML =
    '<div class="bubble"><span class="typing"><i></i><i></i><i></i> 思考中…</span></div>';
  el.messages.appendChild(wrapper);
  el.messages.scrollTop = el.messages.scrollHeight;
  return wrapper;
}

function appendAssistant(data) {
  const meta = [];
  (data.agents || []).forEach((agent) => {
    meta.push(makeBadge(AGENT_NAMES[agent.name] || agent.name, agent.status === "error"));
  });
  if (data.usage && data.usage.calls) {
    const span = document.createElement("span");
    span.textContent = `${data.usage.calls} 次调用 · ${data.usage.total_tokens} tokens · ${data.elapsed}s`;
    meta.push(span);
  }
  appendMessage("assistant", renderMarkdown(data.reply || "（无内容）"), meta);
}

async function sendMessage(text) {
  const message = String(text || "").trim();
  if (!message || state.busy) return;

  state.busy = true;
  el.send.disabled = true;
  appendMessage("user", escapeHtml(message).replace(/\n/g, "<br>"));
  el.input.value = "";
  const typing = appendTyping();

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        user_id: state.userId,
        session_id: state.sessionId,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.detail || `请求失败（HTTP ${response.status}）`);
    }
    state.sessionId = data.session_id || state.sessionId;
    if (state.sessionId) sessionStorage.setItem("aligo.sessionId", state.sessionId);
    updateSessionInfo();
    typing.remove();
    appendAssistant(data);
    el.conn.className = "status-dot ok";
    refreshOverview();
  } catch (error) {
    typing.remove();
    appendMessage(
      "assistant",
      `<blockquote>⚠️ ${escapeHtml(error.message || String(error))}</blockquote>`,
    );
    el.conn.className = "status-dot err";
  } finally {
    state.busy = false;
    el.send.disabled = false;
    el.input.focus();
  }
}

async function refreshOverview() {
  try {
    const response = await fetch(`/api/status?user_id=${encodeURIComponent(state.userId)}`);
    if (!response.ok) throw new Error("status failed");
    const data = await response.json();
    const rows = [];
    const preferences = data.preferences || {};
    Object.keys(preferences).forEach((key) => {
      const value = Array.isArray(preferences[key])
        ? preferences[key].join(", ")
        : preferences[key];
      rows.push(
        `<div class="pref"><span>${escapeHtml(key)}</span><span>${escapeHtml(String(value))}</span></div>`,
      );
    });
    if (!rows.length) rows.push('<div class="empty">暂无偏好记录</div>');
    rows.push(`<div class="pref"><span>历史行程</span><span>${data.trip_count || 0} 次</span></div>`);
    el.overview.innerHTML = rows.join("");
  } catch {
    el.overview.innerHTML = '<div class="empty">无法获取数据概览</div>';
  }
}

function updateSessionInfo() {
  el.sessionInfo.textContent = state.sessionId ? `会话：${state.sessionId}` : "会话：新";
}

function newSession() {
  state.sessionId = null;
  sessionStorage.removeItem("aligo.sessionId");
  updateSessionInfo();
  el.messages.innerHTML =
    '<div class="welcome"><h2>新会话开始 👋</h2><p>试着描述一次出行需求，或从左侧选择一个示例。</p></div>';
}

el.userId.value = state.userId;
el.userId.addEventListener("change", () => {
  const value = el.userId.value.trim() || "web_user";
  state.userId = value;
  el.userId.value = value;
  localStorage.setItem("aligo.userId", value);
  newSession();
  refreshOverview();
});

document.getElementById("newSession").addEventListener("click", newSession);
document.getElementById("exportData").addEventListener("click", () => {
  window.open(`/api/export?user_id=${encodeURIComponent(state.userId)}`, "_blank");
});

document.querySelectorAll("[data-example]").forEach((button) => {
  button.addEventListener("click", () => sendMessage(button.dataset.example));
});

document.getElementById("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage(el.input.value);
});

el.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendMessage(el.input.value);
  }
});

updateSessionInfo();
refreshOverview();
el.input.focus();
