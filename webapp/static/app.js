/* Aligo Web 前端（无构建、无外部依赖） */
"use strict";

const AGENT_NAMES = {
  event_collection: "事项收集",
  preference: "偏好管理",
  itinerary_planning: "行程规划",
  information_query: "信息查询",
  rag_knowledge: "知识库查询",
  memory_query: "记忆查询",
  "trip-budget": "预算建议",
  "packing-list": "行李清单",
  "travel-checklist": "行前检查清单",
  "local-transport": "市内交通建议",
  "safety-tips": "安全提醒",
  "trip-notes": "差旅贴士",
};

// 大小写不敏感：历史消息的 _meta 可能保存了 Trip-Budget / Packing-List 等
// 模型输出的混合大小写变体，精确键查找会退化为显示英文目录名。
// 必须按自有属性查找：constructor / __proto__ 等原型链成员会被当成真值，
// 界面显示成函数源码或 [object Object]
function agentDisplayName(name) {
  const key = String(name || "").trim().toLowerCase();
  return Object.prototype.hasOwnProperty.call(AGENT_NAMES, key)
    ? AGENT_NAMES[key]
    : name;
}

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

// 偏好类型 → 显示名。必须按自有属性查找：`PREF_NAMES[type] || type` 会把
// constructor / toString / __proto__ 等原型链成员当成真值，行标签与删除
// 确认框显示函数源码，用户无法辨认该行
function prefDisplayName(type) {
  const key = String(type);
  return Object.prototype.hasOwnProperty.call(PREF_NAMES, key)
    ? PREF_NAMES[key]
    : key;
}

// 偏好类型 → 最近一次已知值。渲染和保存成功后都更新：编辑按钮若只用
// 渲染时闭包里的旧值，刷新失败后再点编辑会回填旧值、再次保存把刚成功
// 的新值静默覆盖回去
const preferenceLatest = new Map();

// 存储访问一律按「种类」取对象：隐私设置 / 沙箱 iframe 下访问
// window.sessionStorage 这个属性本身就会抛 SecurityError——把对象作为
// 实参传入时，异常发生在调用之前，helper 里的 try 根本接不住
function _storage(kind) {
  return kind === "local" ? window.localStorage : window.sessionStorage;
}

function safeStorageGet(kind, key) {
  try {
    return _storage(kind).getItem(key);
  } catch {
    return null;
  }
}

function safeStorageSet(kind, key, value) {
  try {
    _storage(kind).setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

function safeStorageRemove(kind, key) {
  try {
    _storage(kind).removeItem(key);
  } catch {
    /* 隐私模式 / 禁用存储：忽略 */
  }
}

const state = {
  userId:
    safeStorageGet("session", "aligo.userId") ||
    safeStorageGet("local", "aligo.userId") ||
    "web_user",
  sessionId: safeStorageGet("session", "aligo.sessionId") || null,
  // 深链接里的会话 ID 被拒绝：init 时统一提示（此处 els 尚未定义）
  urlSessionRejected: false,
  // 存储的用户 ID 被当前规则拒绝（规则收紧 / 历史非法值）：回退 web_user
  // 但不静默，init 时提示用户修正（此处 els 尚未定义）
  rejectedUserId: null,
  // 初始化请求完成前不允许发送：此时页面还没展示已保存会话的历史，
  // 发送会把消息写进一个界面从未展示过的旧会话
  ready: false,
  sessions: [],
  searchQuery: "",
  status: null,
  health: null,
  busy: false,
  controller: null,
  // 停止 / 连接中断的回合登记：对账被新回合的 busy / token 推进跳过时
  // 保留登记，由新回合结束后的 flushPendingStoppedTurns 补跑
  pendingStoppedTurns: [],
  lastUserText: "",
  kbItems: [],
  // 列表区当前渲染的是语义检索结果（kbItems 只属于关键词列表）：
  // 语义视图下的 429 / 失败分支据此决定保留 DOM 还是显示空态文本
  kbShowingSemantic: false,
  // 服务端分页游标：以「已向服务端取回的条数」为准，而不是去重后的已显示
  // 条数——并发插入会让按显示数计算的 offset 永远追不上，加载更多失效
  kbOffset: 0,
  kbTotal: 0,
  kbQuery: "",
  kbCategory: "",
  kbDeleting: new Set(),
  kbImporting: false,
  kbEditingId: null,
  // 回填后的 textarea 基线：再次点同一行「编辑」时据此判断草稿是否被修改
  kbEditBaseline: "",
  kbEditingMetadata: {},
  kbEditingGroupParts: 0,
  kbEditSeq: 0,
  kbSaving: false,
  kbSemanticMode: false,
  // 关键词列表的渲染状态：列表内容对应的查询 / 分类快照。刷新失败（429
  // 保留旧结果）或在途时，"加载更多"不能以新查询 + 旧游标取页
  kbListLoading: false,
  kbLoadedQuery: null,
  kbLoadedCategory: null,
  kbListFailed: false,
  kbComposing: false,
  historyComposing: false,
  tripsComposing: false,
  kbSemanticQuery: "",
  tripEditingId: null,
  // 进入编辑时回填到表单的字段快照：跨行切换 / 只提交改动字段 / 判断
  // 「未修改」都按它比较，避免把刷新后的服务端值误当成用户改动
  tripEditBaseline: null,
  tripsExpanded: false,
  tripsAll: [],
  tripsAllTotal: 0,
  tripsFilter: "",
  // 行程状态筛选（active / upcoming / past）：与面板状态徽标同一口径
  tripsStatusFilter: "",
  // 展开列表内容对应的筛选快照：刷新失败保留旧列表时，计数文案不能
  // 假装旧结果就是新条件的匹配
  tripsLoadedFilter: "",
  tripsLoadedStatus: "",
  sessionTotal: 0,
  // 会话分页游标：与 kbOffset 同一口径，按「已向服务端取回的条数」前进；
  // 用「已显示条数」在头部并发插入整页时会永远卡在同一个窗口
  sessionsOffset: 0,
  showArchived: false,
  archivedCount: 0,
  archivedMatches: 0,
  sessionsLoading: false,
  // 会话列表当前渲染内容对应的查询 / 归档快照：搜索失败回滚与「加载更多」
  // 都以它为准（对齐知识库的 kbLoadedQuery / kbLoadedCategory）
  sessionsLoadedQuery: null,
  sessionsLoadedArchived: null,
  sessionLoading: false,
  // 正在进行加载的会话 id：删除其它会话时不能作废这次加载
  sessionLoadingId: null,
  // 已渲染窗口的起点 {sessionId, start}：静默重载（停止对账）按同一起点
  // 重建，用户已点开的更早消息不会从 DOM 消失、旧滚动位置仍然有效
  renderedStart: null,
  // 当前 DOM 只渲染到搜索命中窗口、会话更新部分尚未渲染：此期间会话末尾
  // 不在 DOM 中，编辑 / 重新生成会指向错误的「最后一轮」，一律隐藏
  partialHistory: false,
  // 上一轮回答因会话被删除而未落库、仍显示在消息区：下一条消息属于新
  // 会话，发送前清空该线程（否则两个服务端会话的历史混在一起）
  retiredThreadVisible: false,
  // 正在删除的会话 id：双击确认不能重复发 DELETE（计数会被减两次）
  deletingSessions: new Set(),
  // 偏好编辑被取消时置位：刷新被有意跳过，面板关闭时补一次
  statusStale: false,
  // 侧栏重命名输入框打断会话列表刷新时置位：重命名收尾时补一次刷新
  historyRefreshPending: false,
  // 行程最近一次保存的服务端值（同 preferenceLatest）：刷新在途 / 失败时
  // 再点「编辑」必须回填最新值，否则第二次保存会把刚保存的字段写回旧值
  tripLatest: new Map(),
};

// Windows 设备保留名（与服务端 is_valid_web_id 同口径）：ID 会拼成
// 记忆库文件名（NUL.json 指向设备），服务端为 JSON 后端统一 422
const RESERVED_DEVICE_NAMES = new Set([
  "con",
  "prn",
  "aux",
  "nul",
  ...Array.from({ length: 9 }, (_, i) => `com${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `lpt${i + 1}`),
]);

// 启动后校验：历史遗留的非法 / 设备保留名 ID 会让所有请求 422、界面不可用
function sanitizeStoredUserId() {
  if (isValidId(state.userId)) return;
  // 保留回退（避免整页 422），但记下被拒值，init 时提示用户修正，
  // 否则会静默切进共享默认账号且原账号数据从面板消失
  state.rejectedUserId = state.userId;
  state.userId = "web_user";
  safeStorageRemove("session", "aligo.userId");
  safeStorageRemove("local", "aligo.userId");
}

function utf8ByteLength(text) {
  try {
    if (typeof TextEncoder !== "undefined") {
      return new TextEncoder().encode(text).length;
    }
  } catch {
    /* 回退手工计算 */
  }
  let total = 0;
  for (const char of Array.from(text)) {
    const code = char.codePointAt(0);
    total += code <= 0x7f ? 1 : code <= 0x7ff ? 2 : code <= 0xffff ? 3 : 4;
  }
  return total;
}

function hasValidIdShape(value) {
  // 与服务端 _ID_PATTERN（pydantic-core 的 Rust \w：Alphabetic + Mark +
  // Nd + Pc + Join_Control）同一集合：CLI / API 建出的含组合符 / 零宽
  // 连接符的 ID 在面板里也要能切换，上标数字这类 No 则两端都拒绝
  const text = String(value || "");
  // 服务端按码点计数：增补平面字符在 JS 里占两个 UTF-16 单元，按
  // length 判断会把 33 个星形字母这类合法 ID 误拒
  if (!text || Array.from(text).length > 64) return false;
  // 与服务端 210 字节上限同口径（utils/identifiers.py）：ID 会拼成
  // .json.lock / .corrupt-* / .migrate-backup / .import.lock 等派生文件名，
  // 按最长的损坏备份名（id + 42 字节）给 NAME_MAX=255 留余量；
  // 53 个增补平面字符 = 212 字节会被所有接口 422
  if (utf8ByteLength(text) > 210) return false;
  try {
    return /^[\p{Alphabetic}\p{M}\p{Nd}\p{Pc}\u200c\u200d-]+$/u.test(text);
  } catch {
    return /^[A-Za-z0-9_一-龥-]{1,64}$/.test(text);
  }
}

function isValidId(value) {
  const text = String(value || "");
  if (!hasValidIdShape(text)) return false;
  return !RESERVED_DEVICE_NAMES.has(text.toLowerCase());
}

// 与服务端 RESERVED_SESSION_IDS 同口径（精确匹配）：export 会被静态导出
// 路由截获；设备名会话（con 等）是合法的会话 ID，不能用用户 ID 规则拒绝
const RESERVED_SESSION_IDS = new Set(["export"]);

function isValidSessionId(value) {
  const text = String(value || "");
  return hasValidIdShape(text) && !RESERVED_SESSION_IDS.has(text);
}

function sanitizeStoredSessionId() {
  if (state.sessionId === null) return;
  if (isValidSessionId(state.sessionId)) return;
  state.sessionId = null;
  safeStorageRemove("session", "aligo.sessionId");
}

// state 在常量定义之前创建：存储里的非法 ID 在这里统一回退
sanitizeStoredUserId();
sanitizeStoredSessionId();

/* 深链接：?user=...&session=... 可直接打开某个会话 */
(function applyUrlState() {
  try {
    const params = new URLSearchParams(location.search);
    const urlUser = params.get("user");
    if (urlUser && isValidId(urlUser)) {
      state.userId = urlUser;
      // 分享链接只作为当前标签页的临时身份，不覆盖本地长期身份
      safeStorageSet("session", "aligo.userId", urlUser);
    }
    const urlSession = params.get("session");
    if (urlSession && isValidSessionId(urlSession)) {
      state.sessionId = urlSession;
      safeStorageSet("session", "aligo.sessionId", urlSession);
    } else if (urlSession) {
      // 等 init 里 els/toast 可用后再提示（此处 IIFE 早于 els 定义）
      state.urlSessionRejected = true;
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
  upcomingWeather: byId("upcomingWeather"),
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
  memoryBtn: byId("openMemory"),
  closeMemory: byId("closeMemory"),
  saveUser: byId("saveUser"),
  kbExport: byId("kbExport"),
  exportData: byId("exportData"),
  exportDataMd: byId("exportDataMd"),
  exportDataKnowledge: byId("exportDataKnowledge"),
  exportSessions: byId("exportSessions"),
  exportSessionsHtml: byId("exportSessionsHtml"),
  importData: byId("importData"),
  importDataFile: byId("importDataFile"),
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
  exportTripsHtml: byId("exportTripsHtml"),
  tripsDedupe: byId("tripsDedupe"),
  tripImport: byId("tripImport"),
  tripImportFile: byId("tripImportFile"),
  tripStatusFilter: byId("tripStatusFilter"),
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
  tripCancel: byId("tripCancel"),
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
  kbListStatus: byId("kbListStatus"),
  kbSearch: byId("kbSearch"),
  kbCategoryFilter: byId("kbCategoryFilter"),
  kbMore: byId("kbMore"),
  kbImport: byId("kbImport"),
  kbImportFile: byId("kbImportFile"),
  kbSemantic: byId("kbSemantic"),
  kbEval: byId("kbEval"),
  kbDuplicates: byId("kbDuplicates"),
  kbDedupe: byId("kbDedupe"),
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
// 正在对账的停止回合（sessionId + turnId）：600ms 定时器与回合结束后的
// flushPendingStoppedTurns 会同时轮询同一回合，无互斥时一次停止放大成
// 两套 10 次详情 GET 与重复 toast
const reconcilingStoppedTurns = new Set();
// 每次会话加载的 AbortController：新加载开始即中止上一次在途请求。
// 旧请求的结果本就会按 token 丢弃，主动中止还能让等待它 settle 的调用方
// （启动恢复）立即返回，不会把 state.ready 卡到旧请求超时
let sessionLoadController = null;
let sessionsRequestId = 0;
// 会话列表刷新单页上限（与服务端 /api/sessions 的 limit 上限一致）：
// 刷新按已加载条数一次取回整窗，避免为展开过的列表逐页串行重扫全量历史
const SESSION_REFRESH_LIMIT_MAX = 1000;
let statusRequestId = 0;
// 行程列表（展开全部 / 状态刷新时的二次请求）共用序号：只应用最新一次结果，
// 防止切换用户后旧用户的数据被渲染（行内删除按当前用户执行，会误删同名 trip_id）
let tripsRequestId = 0;
let tripFilterTimer = null;
let searchTimer = null;
// 知识库工具栏动作（评测 / 查重复 / 清理重复）共用同一结果条：序号仲裁，
// 只有最新发起的动作可以写入结果，迟到的旧响应不能覆盖新结果
let kbActionResultSeq = 0;
// 共享结果条上的「进行中」文案归属：被清理等动作作废后，取消路径必须
// 把它清掉，否则面板永久停在「检索评测中…」
let kbResultPending = null;
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
    .replace(/'/g, "&#39;")
    // 行内代码占位符使用私用区 U+E000/U+E001：用户内容里出现同样字符时
    // 会被占位恢复步骤替换成代码内容（注入），必须在这里替换掉
    .replace(/[\uE000\uE001]/g, "\uFFFD");
}

function highlightSnippet(text, query) {
  const source = String(text ?? "");
  const needle = String(query || "").trim();
  if (!needle) return escapeHtml(source);

  // 大小写不敏感匹配直接在原文上做：toLowerCase 可能改变字符串长度
  // （如 İ → i̇ 变成两个码点），用它算出的下标去切原文会错位
  const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pattern = new RegExp(escaped, "gi");
  const parts = [];
  let cursor = 0;
  let match = pattern.exec(source);
  if (!match) return escapeHtml(source);
  while (match) {
    const found = match.index;
    parts.push(escapeHtml(source.slice(cursor, found)));
    parts.push(
      `<mark>${escapeHtml(source.slice(found, found + match[0].length))}</mark>`,
    );
    cursor = found + match[0].length;
    match = pattern.exec(source);
  }
  parts.push(escapeHtml(source.slice(cursor)));
  return parts.join("");
}

function clampText(text, max) {
  const value = String(text || "").trim();
  const chars = Array.from(value);
  return chars.length > max ? `${chars.slice(0, max).join("")}…` : value;
}

function sliceCodePoints(text, max) {
  const chars = Array.from(String(text || ""));
  return chars.length > max ? `${chars.slice(0, max).join("")}…` : String(text || "");
}

// 按 UTF-16 码元上限截断，但截断点落在代理对中间时少留一个高位代理，
// 避免产生孤立代理（浏览器按 U+FFFD 显示）。O(1)：这里处理的正是接近
// 50MB 的超长消息，不能整段转成码点数组
function capAtCodePointBoundary(text, max) {
  const source = String(text ?? "");
  if (source.length <= max) return source;
  let end = max;
  const last = source.charCodeAt(end - 1);
  const next = source.charCodeAt(end);
  if (last >= 0xd800 && last <= 0xdbff && next >= 0xdc00 && next <= 0xdfff) {
    end -= 1;
  }
  return source.slice(0, end);
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
  // 先取消 hidden 再写文本：隐藏期间的内容变更不会被 role=status 播报
  els.toast.hidden = false;
  els.toast.textContent = message;
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
    const previousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    document.body.appendChild(area);
    let copied = false;
    try {
      area.select();
      copied = document.execCommand("copy");
    } finally {
      // 清理必须放 finally：execCommand 抛错（文档非 active / 被策略禁用）
      // 时隐藏 textarea 会留在 DOM 并继续吞键盘输入，每失败一次再叠加一个
      area.remove();
      if (previousFocus) {
        try {
          previousFocus.focus({ preventScroll: true });
        } catch {
          /* 焦点恢复尽力而为 */
        }
      }
    }
    if (!copied) throw new Error("copy failed");
    toast(message);
  } catch {
    toast("复制失败");
  }
}

async function getJSON(url, options, timeoutMs = 120000) {
  // 普通 JSON 请求也要有兜底超时：服务端挂起 / 连接黑洞时 fetch 永不
  // settle，「检索中 / 加载中」的文案与 disabled 状态就永远不复位
  const controller = new AbortController();
  const callerSignal = options && options.signal;
  if (callerSignal) {
    callerSignal.addEventListener("abort", () => controller.abort());
  }
  let timedOut = false;
  const timer =
    timeoutMs > 0
      ? setTimeout(() => {
          timedOut = true;
          controller.abort();
        }, timeoutMs)
      : null;
  try {
    const headers = {
      // 面板请求的客户端标记：高开销伪 GET 接口据此拒绝跨站简单请求
      // （HTTP 内网 / 旧 Safari 不发送 Sec-Fetch-Site，不能只看该头）
      "X-Aligo-Client": "web",
      ...((options && options.headers) || {}),
    };
    const response = await fetch(url, {
      ...options,
      headers,
      signal: controller.signal,
    });
    if (!response.ok) {
      const error = new Error(await errorMessage(response));
      error.status = response.status;
      throw error;
    }
    // 2xx 也可能是非 JSON：session_id=export 会被静态导出路由截获并 200
    // 返回 Markdown，直接 response.json() 抛 SyntaxError，被上层当成
    // 「加载会话失败」；给出可理解的错误文本
    try {
      return await response.json();
    } catch {
      throw new Error("响应不是 JSON（接口可能被其它路由截获，请更换 ID 后重试）");
    }
  } catch (error) {
    if (timedOut) throw new Error("请求超时，请稍后重试");
    throw error;
  } finally {
    if (timer) clearTimeout(timer);
  }
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

// 响应头已到、错误体未到时的兜底：链路半开（移动网络切换 / 代理转发
// 5xx 头后上游挂掉）会让 response.json() 永不 settle，sendMessage 的
// finally 不执行、界面永久停在生成中
const ERROR_BODY_TIMEOUT_MS = 10000;

function withTimeout(promise, ms, message) {
  let timer = null;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

// 服务端已明确响应的错误（429 / 503 / 422 等）：与「连接中断」区分开，
// 便于调用方决定是否执行停止对账 / 回填输入 / 标记健康状态
class HttpError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "HttpError";
    this.status = status;
  }
}

/* ---------- 安全的 Markdown 渲染 ----------
   先在原文上识别块级结构，再对文本内容转义并处理行内样式，
   因此不会执行任何 HTML / 脚本。 */

function renderInline(raw) {
  // 私用区占位符由本函数内部生成：输入里同码点先剥掉，防止消息文本伪造
  // 占位符劫持代码 / 链接的还原过程
  raw = String(raw ?? "").replace(/[-]/g, "");
  // 行内代码先抽出为占位符：代码里的链接 / ** 不能被当作 Markdown 处理
  const codeSpans = [];
  let text = escapeHtml(raw).replace(/`([^`]+)`/g, (_, code) => {
    codeSpans.push(code);
    return `${codeSpans.length - 1}`;
  });
  const links = [];
  text = text
    // 链接先换成占位符：URL 里的 **（如 .../wiki/Foo**bar**）不能被后续
    // 加粗规则改写 href（此前 <strong> 被插进属性值，链接目标被破坏）
    .replace(
      /\[([^\[\]\n]{1,500})\]\((https?:\/\/(?:[^()\s]|\([^()\s]*\))+)\)/g,
      (_, label, href) => {
        links.push({ label, href });
        return `${links.length - 1}`;
      },
    )
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  const restoreCode = (value) =>
    value.replace(/(\d+)/g, (_, index) => {
      return `<code>${codeSpans[Number(index)] ?? ""}</code>`;
    });
  text = restoreCode(text);
  return text.replace(/(\d+)/g, (_, index) => {
    const link = links[Number(index)];
    if (!link) return "";
    // 链接文字里的强调仍然生效（GFM 允许 ** 出现在链接文本中）；
    // 代码占位符也要还原：[`patch`](url) 的链接文字在抽取链接时已随
    // label 离开外层 text，外层还原覆盖不到，此前会显示成不可见占位符
    const label = restoreCode(
      link.label.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>"),
    );
    // URL 里出现行内代码占位符时还原为原文（不带 <code> 标签）：
    // 否则 href 里残留私用区占位符，链接地址不可用
    const href = String(link.href).replace(
      /(\d+)/g,
      (_, index) => codeSpans[Number(index)] ?? "",
    );
    return `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });
}

function splitTableRow(line) {
  // GFM 表格：\| 是字面竖线，行内代码里的 | 不拆列；此前直接 split("|")
  // 会让 `a|b` 被拆成两个残片、\| 错位整张表
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  const cells = [];
  let current = "";
  let escaped = false;
  let inCode = false;
  for (const ch of trimmed) {
    if (escaped) {
      // GFM 反斜杠转义：下一个字符是 ASCII 标点（含 | * _ 等）时按字面
      // 输出该字符；普通字符（如 C:\temp 的 t）不构成转义，反斜杠本身
      // 保留。此前模板字符串 `` `\${ch}` `` 求值成字面量 "${ch}"（$ 被
      // 转义），把单元格改写成了乱码
      current += /[\u0021-\u002F\u003A-\u0040\u005B-\u0060\u007B-\u007E]/.test(ch)
        ? ch
        : "\\" + ch;
      escaped = false;
      continue;
    }
    if (ch === "\\") {
      escaped = true;
      continue;
    }
    if (ch === "`") {
      inCode = !inCode;
      current += ch;
      continue;
    }
    if (ch === "|" && !inCode) {
      cells.push(current.trim());
      current = "";
      continue;
    }
    current += ch;
  }
  if (escaped) current += "\\";
  cells.push(current.trim());
  return cells;
}

const MAX_MARKDOWN_CHARS = 200000;

function renderMarkdown(text) {
  let source = String(text ?? "");
  // 先归一化行尾：块级正则的 . / $ 不匹配 \r，CRLF 的标题与代码围栏
  // 会渲染成字面量（模型答案与恢复的历史消息都可能带 CRLF）
  source = source.replace(/\r\n?/g, "\n");
  let truncated = false;
  if (source.length > MAX_MARKDOWN_CHARS) {
    // 备份恢复允许单条消息接近 50MB：整段同步转义 + 正则扫描会在打开
    // 会话时把主线程冻结数秒。截断显示，完整内容仍保留在存储与导出里
    source = capAtCodePointBoundary(source, MAX_MARKDOWN_CHARS);
    truncated = true;
  }
  const lines = source.split("\n");
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

    // 开围栏允许任意 info string（```c# / ``` python / ```js title="a" 都是
    // 合法写法）；闭围栏仍只认「纯反引号 + 空白」，否则会先把正文吞进代码块、
    // 直到下一个 ``` 才「闭合」——开围栏识别过窄时闭合行会被误当成新的开围栏
    const fence = line.match(/^(`{3,})(.*)$/);
    if (fence) {
      flushParagraph();
      const fenceLength = fence[1].length;
      // 闭围栏必须至少同样长：四反引号围栏里可以安全包含三反引号代码块
      const closing = new RegExp("^`{" + fenceLength + ",}" + "\\s*$");
      const code = [];
      index += 1;
      while (index < lines.length && !closing.test(lines[index])) {
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
  if (truncated) {
    out.push(
      '<p class="md-truncated">（消息过长，已截断显示；完整内容见导出文件）</p>',
    );
  }
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

function capRenderText(text) {
  // 用户消息也要有渲染上限：备份恢复允许单条消息接近 50MB，原样塞进
  // pre-wrap 气泡会让浏览器布局阶段冻结主线程（助手消息已由
  // renderMarkdown 的 MAX_MARKDOWN_CHARS 截断，这里此前遗漏）
  const source = String(text == null ? "" : text);
  return source.length > MAX_MARKDOWN_CHARS
    ? capAtCodePointBoundary(source, MAX_MARKDOWN_CHARS) + "…（已截断显示）"
    : source;
}

function renderUserNode(text) {
  const node = document.createElement("div");
  node.className = "msg user";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = capRenderText(text);
  // 超过渲染上限的提问只显示截断串：编辑 / 重新生成的 expect 无法与
  // 服务端保存的完整原文一致（完整原文又超过协议上限），不提供必然
  // 失败的入口
  if (String(text == null ? "" : text).length > MAX_MARKDOWN_CHARS) {
    node.dataset.truncated = "1";
  }
  node.appendChild(bubble);
  return node;
}

function refreshEditButtons() {
  els.messages.querySelectorAll(".user-edit").forEach((node) => node.remove());
  if (state.busy || state.sessionLoading || !state.sessionId) return;
  // 会话末尾未渲染（搜索命中窗口）：「编辑最后一条」会删除并重发服务端
  // 真正的最后一轮，而不是 DOM 末尾这一条——在补到会话结尾前不提供入口
  if (state.partialHistory) return;
  const users = els.messages.querySelectorAll(".msg.user");
  const last = users[users.length - 1];
  if (!last) return;
  const bubble = last.querySelector(".bubble");
  if (!bubble || bubble.querySelector(".user-edit-form")) return;
  // 该轮提问没有成功写入服务端历史（停止 / 失败在成功落库之前）：
  // 编辑会对服务端「上一轮已保存问答」发起删除，同文重复提问时误删旧问答
  if (last.dataset.transient === "1") return;
  // 气泡文本是截断串（超长消息只渲染前 20 万字）：无法构造与服务端保存
  // 原文一致的 expect，编辑 / 重新生成必然 422 或 mismatch
  if (last.dataset.truncated === "1") return;
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
  if (state.sessionLoading) {
    toast("会话正在加载，请稍候");
    return;
  }
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
    // 输入框被替换掉后焦点会落到 body：还给该条目的「编辑」按钮，
    // 键盘用户（Esc 取消 / 空提交）不丢位置
    const chip = node.querySelector(".user-edit .chip");
    if (chip) chip.focus();
  };
  const close = () => {
    if (saving) return;
    input.removeEventListener("keydown", onKeydown);
    restore();
  };
  const onKeydown = (event) => {
    if (event.isComposing) return;
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
    if (state.busy) {
      // 流式回合进行中删除 / 重发会与在途回合交错（旧流事件写回新视图）
      toast("请等待当前回复完成");
      return;
    }
    if (!state.ready) {
      // 初始化未完成：此时 sendMessage 会直接拒绝，而本轮删除已经发生，
      // 这一轮会从历史里消失。删除前先拦住
      toast("页面正在载入，请稍候…");
      return;
    }
    if (state.sessionLoading) {
      // 会话还在加载就删上一轮，会删掉 / 重发到错误的会话
      toast("会话正在加载，请稍候");
      return;
    }
    if (currentSessionDeleting()) {
      // 删除在途时先删后会与 DELETE 交错：本轮被一并删除或拿到 409
      toast("会话正在删除，请稍候");
      return;
    }
    const text = input.value.trim();
    if (!text) {
      toast("提问不能为空");
      input.focus();
      return;
    }
    if ([...text].length > CHAT_MESSAGE_MAX) {
      // 先删上一轮再重发时超限必 422：删除成功、发送失败会让这一轮消失
      toast(`提问超过 ${CHAT_MESSAGE_MAX} 字，无法重新发送；请先精简后再试`);
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
      const assistants = els.messages.querySelectorAll(".msg.assistant");
      const lastAssistant = assistants[assistants.length - 1];
      const turnId =
        (lastAssistant && lastAssistant.dataset && lastAssistant.dataset.turnId) ||
        "";
      mismatch = await deleteLastTurn(originalText, turnId);
    } catch (error) {
      saving = false;
      input.dataset.saving = "0";
      setBusy(false);
      // 无状态码（超时 / 断网）或 5xx（网关 502/504、响应前上游挂掉）：
      // 删除上一轮的请求可能已在服务端执行，不能当确定失败让用户在旧视图上重试
      const status = Number((error && error.status) || 0);
      if (!status || status >= 500) {
        toast("编辑请求结果未知：上一轮可能已被删除，请刷新页面确认后再试");
      } else {
        toast(`编辑失败：${error.message}`);
      }
      save.disabled = false;
      cancel.disabled = false;
      save.textContent = "保存并重新发送";
      return;
    }
    setBusy(false);
    if (mismatch) {
      // 服务端最后一轮与这条提问不一致：可能是历史被其它标签页 / CLI 修改，
      // 也可能是本轮从未写入（服务端对两者返回相同结果）。与「重新生成」
      // 保持同一保守语义：不删本地节点、不重发，避免历史出现重复问答
      saving = false;
      input.dataset.saving = "0";
      save.disabled = false;
      cancel.disabled = false;
      save.textContent = "保存并重新发送";
      toast("上一轮对话已变化，未重新发送；请刷新页面后重试");
      return;
    }
    input.removeEventListener("keydown", onKeydown);
    // 本轮的用户提问与回答一并移除后重新发送
    let next = node.nextElementSibling;
    while (next) {
      const following = next.nextElementSibling;
      if (next.classList.contains("msg")) next.remove();
      next = following;
    }
    node.remove();
    await sendMessage(text);
  }

  save.addEventListener("click", submit);
}

function renderHistoryTrace(meta) {
  // 历史数据可能带非对象条目（旧导入的 null / 数字）：直接取 agent_name
  // 会 TypeError，让该会话永久打不开
  const schedule = (Array.isArray(meta.schedule) ? meta.schedule : []).filter(
    (item) => item && typeof item === "object",
  );
  const agents = (Array.isArray(meta.agents) ? meta.agents : []).filter(
    (agent) => agent && typeof agent === "object",
  );
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
      agentDisplayName(name),
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
  // 历史回合同样记录 client_turn_id：从历史打开会话后点「编辑 / 重新生成」
  // 要用它精确确认「服务端最后一轮确实是本轮」；缺了它服务端退化为按提问
  // 文本匹配，会误删其它标签页 / CLI 并发写入的同文问答
  if (meta && typeof meta.client_turn_id === "string" && meta.client_turn_id) {
    node.dataset.turnId = meta.client_turn_id;
  }
  const bubble = document.createElement("div");
  bubble.className = "bubble answer";
  bubble.innerHTML = renderMarkdown(markdown || "（无内容）");
  const foot = document.createElement("div");
  foot.className = "msg-foot";

  if (meta) {
    (Array.isArray(meta.agents) ? meta.agents : [])
      .filter((agent) => agent && typeof agent === "object")
      .forEach((agent) => {
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
  badge.textContent = `${status === "error" ? "✗" : "✓"} ${agentDisplayName(name)}`;
  return badge;
}

function formatUsage(usage, elapsed) {
  const parts = [];
  if (usage && usage.calls) parts.push(`${usage.calls} 次调用`);
  if (usage && usage.total_tokens) parts.push(`${usage.total_tokens.toLocaleString("zh-CN")} tokens`);
  if (typeof elapsed === "number") parts.push(`${elapsed}s`);
  return parts.join(" · ");
}

async function deleteLastTurn(expect, turnId = "") {
  // expect 放在 JSON body：CLI / 备份导入产生的长提问没有 4000 字上限，
  // 放进查询串会撞 FastAPI max_length（且 percent-encoding 会超过 h11
  // 请求行上限）；DELETE + 查询串仍保留给外部 API 兼容。
  // turn_id：服务端确认「最后一轮确实是本轮」才删除——另一标签页 / CLI
  // 并发追加同文提问时，只按提问原文匹配会误删对方刚写入的问答
  const body = { user_id: state.userId, expect };
  if (turnId) body.turn_id = turnId;
  const result = await getJSON(
    `/api/sessions/${encodeURIComponent(state.sessionId)}/last-turn`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  return Boolean(result && result.mismatch);
}

async function retryFailed(node, text) {
  if (state.busy) {
    toast("请等待当前回复完成");
    return;
  }
  if (!state.ready) {
    toast("页面正在载入，请稍候…");
    return;
  }
  if (state.sessionLoading) {
    toast("会话正在加载，请稍候");
    return;
  }
  if (currentSessionDeleting()) {
    toast("会话正在删除，请稍候");
    return;
  }
  // 停止 / 断网后服务端可能已把这一轮落库（对账被重试点击抢先跳过）：
  // 重发前按 client_turn_id 精确确认，已保存则刷新历史而不是再发一条
  const turnId = (node && node.dataset && node.dataset.turnId) || "";
  const idempotencyKey =
    (node && node.dataset && node.dataset.idempotencyKey) || "";
  if (turnId && state.sessionId) {
    setBusy(true);
    let persisted = false;
    try {
      persisted = await turnPersistedTurn(state.sessionId, turnId);
    } finally {
      setBusy(false);
    }
    if (persisted) {
      node.remove();
      const refreshed = await openSession(state.sessionId, {
        silent: true,
        keepOnError: true,
        preserveOnEmpty: true,
        toastOnError: true,
      });
      if (refreshed) {
        toast("这一轮已保存，已按服务端历史刷新");
      }
      return;
    }
  }
  trimRegenButtons();
  // 失败可能发生在提问写入历史之前：只重发，不删服务端上一轮，
  // 避免误删已保存的问答。重发复用原 turn id 与幂等键：同一逻辑回合
  // 若其实已被服务端执行 / 落库，回放响应而不是再执行一遍
  node.remove();
  await sendMessage(text, {
    echo: false,
    turnId,
    idempotencyKey,
  });
}

async function turnPersistedTurn(sessionId, turnId) {
  if (!sessionId || !turnId) return false;
  try {
    const data = await getJSON(
      `/api/sessions/${encodeURIComponent(sessionId)}?user_id=${encodeURIComponent(state.userId)}`,
      {},
      5000,
    );
    return Boolean(
      data &&
        Array.isArray(data.messages) &&
        data.messages.some(
          (item) => item && item.meta && item.meta.client_turn_id === turnId,
        ),
    );
  } catch {
    return false;
  }
}

function randomId() {
  // 浏览器提供 randomUUID 时用它；旧环境退回时间 + 随机的可读 id
  return typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function confirmDiscardPendingEdit(action) {
  if (!document.querySelector(".user-edit-input")) return true;
  return window.confirm(
    `正在编辑的修改尚未保存，${action}将丢弃修改。继续？`,
  );
}

async function regenerate(node, text) {
  if (state.busy) {
    toast("请等待当前回复完成");
    return;
  }
  if (!state.ready) {
    // 初始化未完成时 sendMessage 会拒绝，先删后弃会让这一轮消失
    toast("页面正在载入，请稍候…");
    return;
  }
  if (state.sessionLoading) {
    // 会话还没加载完就删上一轮，可能删掉 / 重发到错误的会话
    toast("会话正在加载，请稍候");
    return;
  }
  if (currentSessionDeleting()) {
    toast("会话正在删除，请稍候");
    return;
  }
  // 未保存的编辑确认必须在删除服务端上一轮之前：确认可取消，之后的
  // deleteLastTurn + node.remove 是破坏性的
  if (!confirmDiscardPendingEdit("重新生成")) return;
  trimRegenButtons();
  if ([...text].length > CHAT_MESSAGE_MAX) {
    toast(`提问超过 ${CHAT_MESSAGE_MAX} 字，无法重新发送；请先精简后再试`);
    return;
  }
  if (state.sessionId) {
    // 先删除服务端上一轮，避免重新生成在历史里叠加重复问答
    setBusy(true);
    // 删除阶段最长 120s 且此时没有可中止的流（stopBtn 隐藏）：被点的
    // 按钮必须有可见反馈，否则用户以为点击无效
    const labelNode =
      node && node.querySelector ? node.querySelector("span") : null;
    const previousLabel = labelNode ? labelNode.textContent : "";
    const restoreChip = () => {
      if (labelNode) labelNode.textContent = previousLabel;
      if (node && "disabled" in node) {
        node.disabled = false;
        if (node.removeAttribute) node.removeAttribute("aria-busy");
      }
    };
    if (node && "disabled" in node) {
      node.disabled = true;
      if (node.setAttribute) node.setAttribute("aria-busy", "true");
    }
    if (labelNode) labelNode.textContent = "准备中…";
    let mismatched = false;
    try {
      const turnId =
        (node && node.dataset && node.dataset.turnId) || "";
      mismatched = await deleteLastTurn(text, turnId);
    } catch (error) {
      setBusy(false);
      restoreChip();
      // 无状态码（超时 / 断网）或 5xx：DELETE 可能已在服务端执行，不能
      // 谎称「失败」让用户当没发生过
      const status = Number((error && error.status) || 0);
      if (!status || status >= 500) {
        toast("重新生成请求结果未知；请刷新页面确认后再重试");
      } else {
        toast(`重新生成失败：${error.message}`);
      }
      return;
    }
    setBusy(false);
    if (mismatched) {
      // 服务端最后一轮已不是这条回答对应的提问（例如历史在其它页面被修改）：
      // 不能删除当前节点后重发旧问题，否则会打乱顺序并在历史中留下重复问答
      restoreChip();
      toast("上一轮对话已变化，未重新生成；请刷新页面后重试");
      return;
    }
  }
  node.remove();
  await sendMessage(text, { echo: false, skipEditConfirm: true });
}

function trimRegenButtons() {
  const buttons = [...els.messages.querySelectorAll('[data-action="regen"]')];
  if (state.partialHistory) {
    // 会话末尾未渲染（搜索命中窗口）：DOM 末尾不是真正的最后一轮，
    // 「重新生成」会按提问文本匹配删除服务端最新问答，全部隐藏
    buttons.forEach((button) => button.remove());
    return;
  }
  buttons.slice(0, -1).forEach((button) => button.remove());
  // 会话以「用户提问」结尾（提问已保存但没有回答：被停止 / 编排中断）时，
  // 上一条回答的「重新生成」对应的已不是最后一轮：保留点击只会 mismatch，
  // 甚至在两次提问文本相同时误删末尾提问
  const messages = els.messages.querySelectorAll(".msg");
  const last = messages[messages.length - 1];
  if (last && last.classList.contains("user")) {
    els.messages
      .querySelectorAll('[data-action="regen"]')
      .forEach((button) => button.remove());
  }
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
  // 历史数据的 _meta.agents 可能是对象 / 字符串等非数组真值（旧版本或
  // 手工编辑的备份）：直接 .map 会抛 TypeError，被 openSession 的 catch
  // 当成「加载会话失败」、静默恢复时还会清掉已保存的会话 ID
  const list = Array.isArray(agents) ? agents : [];
  const names = new Set(list.map((agent) => agent && agent.name).filter(Boolean));
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

const downloadInFlight = new Set();

async function downloadFromApi(url, fallbackName) {
  if (downloadInFlight.has(url)) {
    // 重复点击不能叠加发起多次全量导出（导出槽位被占满后只会 503）
    toast("下载仍在进行中，请稍候");
    return false;
  }
  downloadInFlight.add(url);
  const controller =
    typeof AbortController !== "undefined" ? new AbortController() : null;
  // 端到端预算要覆盖服务端渲染 / 知识库分片规划 + 传输 + blob 缓冲：
  // 50MB 备份在慢链路上 2 分钟可能不够，放宽到 5 分钟
  const timer = controller
    ? setTimeout(() => controller.abort(), 300000)
    : null;
  try {
    const response = await fetch(url, {
      headers: { "X-Aligo-Client": "web" },
      ...(controller ? { signal: controller.signal } : {}),
    });
    if (!response.ok) {
      throw new Error(await errorMessage(response));
    }
    // 同一响应的质量告警合并成一次 toast：逐个 toast 会在同一任务内
    // 互相覆盖，用户只能看到最后一条（例如「缺出发日期」被「返程日期
    // 无法解析」盖掉）
    const notices = [];
    // ICS 导出会跳过缺少出发日期的行程：响应头带跳过条数，不能静默
    // 给出少行程的日历文件
    const skippedTrips = Number(
      response.headers.get("x-aligo-export-skipped") || 0,
    );
    if (skippedTrips > 0) {
      notices.push(`有 ${skippedTrips} 条行程缺少出发日期，未包含在日历中`);
    }
    // 返程日期无法解析的行程按单日导出（日历里会少几天）：CLI 有同等
    // 提示，面板不能静默截断
    const truncatedEnd = Number(
      response.headers.get("x-aligo-export-truncated-end") || 0,
    );
    if (truncatedEnd > 0) {
      notices.push(`有 ${truncatedEnd} 条行程的返程日期无法解析，日历按单日导出`);
    }
    // 回导自检：文件里存在导入端会整行 / 整事件拒绝的数据（日期非法 /
    // 倒置、字段超长），别等迁移 / 恢复时才发现行程变少
    const unimportable = Number(
      response.headers.get("x-aligo-export-unimportable") || 0,
    );
    if (unimportable > 0) {
      notices.push(
        `导出文件有 ${unimportable} 项在重新导入时会被跳过（日期非法 / 倒置、字段超长等），请先修正后再迁移`,
      );
    }
    // 导出文件整体超过导入上限：回导会整份被拒（不是部分跳过），
    // 必须和「回导会被跳过」分开告知
    const overImportLimit =
      response.headers.get("x-aligo-export-over-limit") || "";
    if (overImportLimit === "cli") {
      notices.push(
        "导出文件超过 CLI 单次导入上限（500 万字符），回导会被整份拒绝，请拆分或用 JSON 备份恢复",
      );
    } else if (overImportLimit) {
      notices.push(
        "导出文件超过面板导入上限（50 万字符 / 2MB），面板回导会被拒收，请用 CLI 导入或 JSON 备份恢复",
      );
    }
    // 含知识库的备份在恢复时可能跳过无法恢复的旧版 / 残缺片段：
    // 服务端用响应头给出计数，不能把「不含完整知识」的备份当完整成功
    const skippedKnowledge = Number(
      response.headers.get("x-knowledge-skipped") || 0,
    );
    if (skippedKnowledge > 0) {
      notices.push(
        `备份中有 ${skippedKnowledge} 条知识片段无法恢复，恢复时会跳过`,
      );
    }
    // 知识库整体超限或暂无法核对时服务端省略知识段（用户数据仍可恢复）：
    // 不能把「不含知识」的备份当完整成功静默下载；头值携带原因，
    // 两种处置不同（清理知识库 vs 稍后重试）
    const omittedKnowledge = response.headers.get("x-knowledge-omitted") || "";
    if (omittedKnowledge) {
      notices.push(
        omittedKnowledge === "planning-failed"
          ? "知识库暂时无法核对分片体积，本次备份不含知识片段（用户数据仍可恢复；稍后重试可得到含知识的备份）"
          : "知识库整体超出导出上限，本次备份不含知识片段（用户数据仍可恢复）",
      );
    }
    if (notices.length) toast(notices.join("；"));
    const blob = await response.blob();
    const disposition = response.headers.get("content-disposition") || "";
    const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
    let filename = fallbackName;
    if (match) {
      try {
        filename = decodeURIComponent(match[1]);
      } catch {
        filename = match[1];
      }
    }
    const link = document.createElement("a");
    const objectUrl = URL.createObjectURL(blob);
    link.href = objectUrl;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    return true;
  } catch (error) {
    if (error && error.name === "AbortError") {
      // 服务端挂起 / 连接黑洞：不能让按钮永远没有反馈
      toast("下载超时：服务端未在 5 分钟内完成，请稍后重试");
    } else {
      // 接口可能返回 400/404/409/503 JSON：不能把错误页当成文件下载
      toast(`下载失败：${error.message}`);
    }
    return false;
  } finally {
    if (timer) clearTimeout(timer);
    downloadInFlight.delete(url);
  }
}

function triggerExport() {
  if (!state.sessionId) return;
  downloadFromApi(
    `/api/sessions/${encodeURIComponent(state.sessionId)}/export?user_id=${encodeURIComponent(
      state.userId,
    )}`,
    `aligo_${state.sessionId}.md`,
  );
}

async function toggleTripsExpanded() {
  if (state.tripsExpanded) {
    // 收起后不能再让防抖定时器补发一次请求：它会把刚收起的视图重新
    // 展开，并覆盖收起时本该更新的概览
    clearTimeout(tripFilterTimer);
    state.tripsExpanded = false;
    renderOverviewUnlessEditing();
    return;
  }
  const userId = state.userId;
  const requestId = (tripsRequestId += 1);
  els.expandTrips.disabled = true;
  try {
    // order=desc：取「最近 200 条」，而不是最早的 200 条；保留筛选词
    // （服务端按 q 过滤完整历史，避免重开面板后旧行程又"消失"）
    const query = state.tripsFilter.trim();
    const status = state.tripsStatusFilter;
    // status 过滤带上浏览器本地日期：服务端（容器常为 UTC）与徽标
    // （todayISO）时区不一致时，筛选结果不能与徽标互相矛盾
    const suffix =
      (query ? `&q=${encodeURIComponent(query)}` : "") +
      (status
        ? `&status=${encodeURIComponent(status)}&today=${todayISO()}`
        : "");
    const data = await getJSON(
      `/api/trips?user_id=${encodeURIComponent(userId)}&limit=200&order=desc${suffix}`,
    );
    if (requestId !== tripsRequestId || userId !== state.userId) return;
    state.tripsAll = data.trips || [];
    state.tripsAllTotal = Number(data.total || state.tripsAll.length);
    state.tripsLoadedFilter = query;
    state.tripsLoadedStatus = status;
    state.tripsExpanded = true;
    renderOverviewUnlessEditing();
  } catch (error) {
    if (requestId !== tripsRequestId || userId !== state.userId) return;
    toast(error.message);
  } finally {
    els.expandTrips.disabled = false;
  }
}

async function decodeImportFile(file) {
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  // UTF-16（Excel「Unicode 文本」）：BOM 或 ASCII 区规律出现的 NUL 字节。
  // UTF-16 的 NUL 在 UTF-8 里是合法字符，不先识别会把整份文件解成乱码。
  if (bytes.length >= 2) {
    if (bytes[0] === 0xff && bytes[1] === 0xfe) {
      return new TextDecoder("utf-16le").decode(buffer);
    }
    if (bytes[0] === 0xfe && bytes[1] === 0xff) {
      return new TextDecoder("utf-16be").decode(buffer);
    }
  }
  const sample = bytes.subarray(0, Math.min(bytes.length, 512));
  let evenZeros = 0;
  let oddZeros = 0;
  for (let index = 0; index < sample.length; index += 1) {
    if (sample[index] !== 0) continue;
    if (index % 2 === 0) evenZeros += 1;
    else oddZeros += 1;
  }
  const threshold = Math.max(8, Math.floor(sample.length / 8));
  if (oddZeros >= threshold && oddZeros > evenZeros * 4) {
    return new TextDecoder("utf-16le").decode(buffer);
  }
  if (evenZeros >= threshold && evenZeros > oddZeros * 4) {
    return new TextDecoder("utf-16be").decode(buffer);
  }
  try {
    // fatal：非法 UTF-8 直接抛错，避免把 GBK 中文静默解成乱码
    return new TextDecoder("utf-8", { fatal: true }).decode(buffer);
  } catch {
    // Excel 导出的中文 CSV 常见 GBK / GB18030：UTF-8 解不了时按超集回退
    return new TextDecoder("gb18030").decode(buffer);
  }
}

async function importTripsFromFile(file) {
  if (!file) return;
  const button = els.tripImport;
  if (button) {
    if (button.disabled) return; // 导入进行中：忽略重复选择
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "导入中…";
  }
  // 解码 / 网络等待期间用户可能切换用户：导入必须落在选择文件时所属的用户
  const userId = state.userId;
  let imported = false;
  // 结果不确定但服务端可能已写入（503 部分失败 / 客户端超时）：按钮复位后
  // 也要刷新面板，否则已落库的行程在列表里不可见
  let reconcile = false;
  // 真正写库的请求是否已发出：预览（dry_run）超时零写入，不能提示
  // 「服务端可能仍在写入」，也不需要刷新对账
  let applySent = false;
  try {
    if (file.size > 2 * 1024 * 1024) {
      toast("文件过大（上限 2 MB），请拆分后再导入");
      return;
    }
    let text;
    try {
      text = await decodeImportFile(file);
    } catch (error) {
      toast(`读取文件失败：${error.message}`);
      return;
    }
    if (!String(text || "").trim()) {
      toast("文件为空，没有可导入的行程");
      return;
    }
    if (text.length > 500_000) {
      // 与 POST /api/trips/import 的 content 上限一致（50 万字符：
      // JSON 序列化后中文约 1.5 MB，仍在 2 MB 请求体上限内）
      toast("文件过大（超过 50 万字符），请拆分后再导入");
      return;
    }
    const postImport = (dryRun) =>
      getJSON(
        `/api/trips/import?user_id=${encodeURIComponent(userId)}` +
          (dryRun ? "&dry_run=1" : ""),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          // 定时事件按浏览器时区取日期：服务端在容器里常是 UTC，不带上
          // 时区会与面板 / CLI 记成不同的天
          body: JSON.stringify({
            content: text,
            timezone:
              typeof Intl !== "undefined" && Intl.DateTimeFormat
                ? Intl.DateTimeFormat().resolvedOptions().timeZone
                : undefined,
          }),
        },
      );
    try {
      // 先预览：把将新增 / 更新 / 跳过的数量交给用户确认，避免选错文件直接写库
      const preview = await postImport(true);
      // 服务端 skipped 列表最多 50 条：确认框里的「跳过」数量要用总数
      const previewSkipped = Number(
        preview.skipped_total ??
          (Array.isArray(preview.skipped) ? preview.skipped.length : 0),
      );
      // 扫描期间关闭面板：不再弹确认，更不能在看不见的界面里写入
      // 只看记忆面板本身：快捷键浮层打开时 isModalOpen() 也为真，
      // 面板已关仍会弹确认并在看不见的界面里写库
      if (els.memorySheet.hidden) return;
      // 确认框里带上前几条跳过原因：只报数量时用户无法判断是不是选错格式。
      // CSV 的 line 是物理行号、ICS 是事件序号（预览响应带 format）
      const isIcsPreview = preview.format === "ics";
      const skipLabel = (line) => {
        const value = Number(line);
        if (isIcsPreview) return value > 0 ? `事件 ${value}` : "";
        return `第 ${value} 行`;
      };
      const skipPreview = Array.isArray(preview.skipped)
        ? preview.skipped.slice(0, 3)
        : [];
      const skipReasons = skipPreview
        .map((item) => {
          const label = skipLabel(item.line);
          return label ? `${label}：${item.reason}` : String(item.reason);
        })
        .join("\n");
      // 合法但可疑的跨行记录（疑似错位引号把下一条行程吞进本行）：只提示
      const warningPreview = Array.isArray(preview.warnings)
        ? preview.warnings.slice(0, 3)
        : [];
      const previewWarnings = Number(
        preview.warnings_total ?? warningPreview.length,
      );
      const warningReasons = warningPreview
        .map((item) => {
          const label = skipLabel(item.line);
          return label ? `${label}：${item.reason}` : String(item.reason);
        })
        .join("\n");
      const ok = window.confirm(
        `将向用户 ${userId} 新增 ${Number(preview.created || 0)} 条、更新 ${Number(
          preview.updated || 0,
        )} 条行程` +
          (previewSkipped ? `，跳过 ${previewSkipped} 条` : "") +
          (previewWarnings ? `，${previewWarnings} 条记录请核对` : "") +
          "。继续？" +
          (skipReasons
            ? `\n\n跳过原因（前 ${skipPreview.length} 条）：\n${skipReasons}`
            : "") +
          (warningReasons
            ? `\n\n请核对（前 ${warningPreview.length} 条）：\n${warningReasons}`
            : ""),
      );
      if (!ok) {
        toast("已取消导入");
        return;
      }
      applySent = true;
      const result = await postImport(false);
      const created = Number(result.created || 0);
      const updated = Number(result.updated || 0);
      const skipped = Number(
        result.skipped_total ??
          (Array.isArray(result.skipped) ? result.skipped.length : 0),
      );
      const warnings = Number(
        result.warnings_total ??
          (Array.isArray(result.warnings) ? result.warnings.length : 0),
      );
      toast(
        `已导入 ${created} 条行程` +
          (updated ? `，更新 ${updated} 条` : "") +
          (skipped ? `，跳过 ${skipped} 条` : "") +
          (warnings ? `，${warnings} 条记录请核对` : ""),
      );
      imported = true;
    } catch (error) {
      const status = Number((error && error.status) || 0);
      const message = String((error && error.message) || error);
      if (status === 503 && message.includes("导入部分失败")) {
        // 服务端逐行提交后中途失败：已提交部分必须对账，否则面板停在
        // 旧列表。服务端文案已含已提交条数与「可安全重跑」，不再加前缀
        toast(message);
        reconcile = true;
      } else if (!status && message.includes("超时")) {
        if (!applySent) {
          // 预览请求 dry_run：超时只说明预览没返回，服务端没有写入任何
          // 行程，可以直接重试
          toast(
            "导入预览超时：预览不写库，未导入任何行程，可直接重试（服务端可能仍在处理该预览，重试可能提示「已有导入任务在处理」）",
          );
        } else {
          // 客户端放弃等待不代表服务端没在写（单 worker 执行器不可取消）
          toast(
            "导入请求超时：服务端可能仍在写入，请稍后重新打开面板确认结果（首次任务结束前重试可能提示「已有导入任务在处理」）",
          );
          reconcile = true;
        }
      } else {
        toast(`导入行程失败：${message}`);
      }
    }
  } finally {
    if (button) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = "导入行程";
    }
  }
  // 状态刷新可能等待慢接口：放在按钮复位之后，成功提示不会被「导入中…」卡住
  if (imported || reconcile) {
    if (state.userId !== userId) {
      // 导入落在选择文件时的用户：刷新只对当前用户的界面有意义
      toast(`已切换用户：导入结果属于用户 ${userId}，切回后重新打开面板即可看到`);
    } else {
      // 刷新失败由这里静默处理：否则「刷新数据概览失败」会覆盖导入结果提示
      await refreshStatus({ silentFailure: true });
    }
  }
}

function resetTripForm() {
  state.tripEditingId = null;
  state.tripEditBaseline = null;
  els.tripForm.reset();
  els.tripEnd.dataset.hadInvalid = "";
  els.tripSubmit.textContent = "保存行程";
}

function tripFormDirty() {
  // 行程表单有未保存草稿：编辑态或任一字段非空。切换用户会 resetTripForm
  // 清空表单，与知识表单 / 聊天行内编辑同口径先确认。
  if (!els.tripForm || els.tripForm.hidden) return false;
  return Boolean(
    state.tripEditingId !== null ||
      els.tripOrigin.value.trim() ||
      els.tripDestination.value.trim() ||
      els.tripStart.value ||
      els.tripEnd.value ||
      els.tripPurpose.value.trim(),
  );
}

function startEditTrip(trip) {
  // 刷新在途 / 失败时列表里的 trip 可能落后于刚保存的值：优先用最近一次
  // 服务端返回的行，避免第二次保存把已保存的字段写回旧值
  trip = state.tripLatest.get(trip.trip_id) || trip;
  // 历史数据可能有未补零 / 带时间的日期：<input type=date> 会静默清空非法值，
  // 若原样写入，用户保存其它字段时返程日期会被悄悄删掉
  const startDate = normalizeISODate(trip.start_date);
  const endDate = normalizeISODate(trip.end_date);
  // 表单里已有未提交的新增 / 编辑草稿时先确认：直接覆盖会让用户以为
  // 刚才填写的内容被保存了。正在编辑同一行时按「表单 vs 将回填值」逐字段
  // 比较：有改动才确认，未修改的重复点击保持静默
  if (state.tripEditingId === trip.trip_id) {
    const changed =
      els.tripOrigin.value.trim() !== String(trip.origin || "").trim() ||
      els.tripDestination.value.trim() !==
        String(trip.destination || "").trim() ||
      els.tripStart.value !== (startDate || "") ||
      els.tripEnd.value !== (endDate || "") ||
      els.tripPurpose.value.trim() !== String(trip.purpose || "").trim();
    if (
      changed &&
      !window.confirm("表单里有未提交的内容，编辑将覆盖，继续？")
    ) {
      return;
    }
  } else {
    // 正在编辑另一条行程时按「表单 vs 回填基线」逐字段比较：未改动只是
    // 点了另一行的编辑按钮，不应弹「未提交内容」确认。基线按写进表单的
    // 原值保存，刷新后服务端值变化也不会误判成用户改动
    const base = state.tripEditBaseline;
    const draft = base
      ? els.tripOrigin.value !== base.origin ||
        els.tripDestination.value !== base.destination ||
        els.tripStart.value !== base.start ||
        els.tripEnd.value !== base.end ||
        els.tripPurpose.value !== base.purpose
      : Boolean(
          els.tripDestination.value.trim() ||
            els.tripOrigin.value.trim() ||
            els.tripStart.value ||
            els.tripEnd.value ||
            els.tripPurpose.value.trim(),
        );
    if (draft && !window.confirm("表单里有未提交的内容，编辑将覆盖，继续？")) {
      return;
    }
  }
  state.tripEditingId = trip.trip_id;
  els.tripOrigin.value = trip.origin || "";
  els.tripDestination.value = trip.destination || "";
  els.tripStart.value = startDate || "";
  els.tripEnd.value = endDate || "";
  els.tripPurpose.value = trip.purpose || "";
  // 回填后的快照：保存时只提交改动字段、跨行切换时判断是否真有草稿
  state.tripEditBaseline = {
    origin: els.tripOrigin.value,
    destination: els.tripDestination.value,
    start: els.tripStart.value,
    end: els.tripEnd.value,
    purpose: els.tripPurpose.value,
  };
  els.tripEnd.dataset.hadInvalid = trip.end_date && !endDate ? "1" : "";
  if (!startDate || (trip.end_date && !endDate)) {
    toast("存在格式异常的日期，请核对后重新填写出发日期并确认返程日期");
  }
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
  if (
    editingId &&
    !els.tripEnd.value &&
    els.tripEnd.dataset.hadInvalid === "1" &&
    !window.confirm("原返程日期格式异常，保存后将清除该返程日期。继续保存？")
  ) {
    return;
  }
  const payload = { user_id: state.userId };
  const baseline = state.tripEditBaseline;
  if (editingId && baseline) {
    // 只提交改动字段：整表单 PUT 会把刷新前表单里的旧值一起写回，静默
    // 回滚 CLI / 另一标签页刚保存的字段（服务端按 exclude_unset 保留
    // 未提交字段）。清空的返程日期 / 目的显式发 null；原日期格式异常、
    // 用户确认清除时也要显式发 null（此时表单值与基线都一样是空串）
    const originValue = els.tripOrigin.value.trim();
    const endValue = els.tripEnd.value || null;
    const purposeValue = els.tripPurpose.value.trim() || null;
    if (originValue !== baseline.origin) payload.origin = originValue;
    if (destination !== baseline.destination) payload.destination = destination;
    if (startDate !== baseline.start) payload.start_date = startDate;
    if (
      (endValue || "") !== baseline.end ||
      els.tripEnd.dataset.hadInvalid === "1"
    ) {
      payload.end_date = endValue;
    }
    if ((purposeValue || "") !== baseline.purpose) {
      payload.purpose = purposeValue;
    }
  } else {
    payload.origin = els.tripOrigin.value.trim();
    payload.destination = destination;
    payload.start_date = startDate;
    payload.end_date = els.tripEnd.value || null;
    // 空目的按「未显式提供」发送：新增时后端落默认「出差」，命中既有
    // 行程（重复提交同一趟）时保留用户改过的目的，不会被重置成模板值
    payload.purpose = els.tripPurpose.value.trim() || null;
  }
  // 编辑但没有任何字段变化：服务端会以 400「没有需要更新的字段」拒绝，
  // 按成功 no-op 处理（表单复位由下方 unchanged 分支完成）
  const noFieldChanges = Boolean(editingId) && Object.keys(payload).length === 1;
  const submitted = {
    editingId,
    // 用表单值而不是 payload：只提交改动字段时 payload.origin 可能缺省
    origin: els.tripOrigin.value.trim(),
    destination,
    startDate,
    endDate: els.tripEnd.value || "",
    purpose: els.tripPurpose.value.trim(),
  };
  els.tripSubmit.disabled = true;
  // 保存请求已发出：允许点「取消」只会清空表单、让在途写入继续落库，
  // 随后又弹出「已添加」——与知识库表单同口径禁用
  els.tripCancel.disabled = true;
  // 响应返回前可能已切换用户：迟到响应不能把旧用户行程写回新用户的
  // 同 id 行程（trip_id 按用户从 trip_1 起编号，会跨账号污染编辑回填）
  const requestUserId = payload.user_id;
  try {
    let message = "行程已更新";
    if (editingId) {
      if (!noFieldChanges) {
        const result = await getJSON(
          `/api/trips/${encodeURIComponent(editingId)}`,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          },
        );
        if (
          result &&
          result.trip &&
          result.trip.trip_id &&
          state.userId === requestUserId
        ) {
          // 服务端返回的保存后行程：刷新在途 / 失败时再次编辑也回填新值
          state.tripLatest.set(result.trip.trip_id, result.trip);
        }
        message += conflictNote(result.conflicts);
      }
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
    // 请求期间用户可能已开始编辑另一条行程 / 录入下一条：不能把正在
    // 填写的表单关掉并清空
    const unchanged =
      state.tripEditingId === submitted.editingId &&
      els.tripDestination.value.trim() === submitted.destination &&
      els.tripOrigin.value.trim() === submitted.origin &&
      els.tripStart.value === submitted.startDate &&
      (els.tripEnd.value || "") === submitted.endDate &&
      els.tripPurpose.value.trim() === submitted.purpose;
    if (unchanged) {
      els.tripForm.hidden = true;
      resetTripForm();
    }
    // 静默：失败时「刷新数据概览失败」不能覆盖刚显示的保存成功提示
    await refreshStatus({ silentFailure: true });
  } catch (error) {
    if (editingId && error.status === 404) {
      // 编辑目标已被其它标签页 / CLI 删除：退出编辑态（保留当前字段作为
      // 新增草稿），否则「保存修改」永远 404（与知识库保存 404 同口径）。
      // 期间已切换用户：结果属于原用户，不能提示 / 改动新用户正在编辑的表单
      if (state.userId === requestUserId) {
        toast(`${error.message}（该行程已被删除，已退出编辑，可重新添加）`);
        if (state.tripEditingId === editingId) {
          state.tripEditingId = null;
          els.tripSubmit.textContent = "保存行程";
          els.tripEnd.dataset.hadInvalid = "";
        }
      }
      await refreshStatus({ silentFailure: true });
    } else {
      toast(error.message);
    }
  } finally {
    els.tripSubmit.disabled = false;
    els.tripCancel.disabled = false;
  }
}

function exportSafeName(text) {
  const cleaned = String(text || "aligo")
    .replace(/[\\/:*?"<>|\s]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return Array.from(cleaned).slice(0, 40).join("") || "aligo";
}

function exportSessionHtml() {
  if (!state.sessionId) return;
  if (!cancelPendingEdits()) {
    toast("正在保存修改，请稍候");
    return;
  }
  // 走服务端导出：面板 DOM 只渲染最近 HISTORY_RENDER_WINDOW 条（大历史
  // 窗口化），克隆 DOM 会把长会话静默截断并带上「显示更早消息」按钮；
  // 服务端按完整会话渲染（与 Markdown 导出、CLI session-export --html
  // 同一实现）
  downloadFromApi(
    `/api/sessions/${encodeURIComponent(state.sessionId)}/export?user_id=${encodeURIComponent(
      state.userId,
    )}&format=html`,
    `aligo_${state.sessionId}.html`,
  ).then((ok) => {
    if (ok) toast("HTML 已导出");
  });
}

const USER_IMPORT_MAX_BYTES = 50 * 1024 * 1024;

async function importUserBackup() {
  // 先取走并清空选择：发送中提前返回时保留文件的话，之后再选同一文件
  // 不会触发 change，导入看起来「没反应」
  const file = els.importDataFile.files && els.importDataFile.files[0];
  els.importDataFile.value = "";
  if (state.busy) {
    toast("请等待当前回复完成");
    return;
  }
  if (!file) return;
  if (file.size > USER_IMPORT_MAX_BYTES) {
    toast("备份文件不能超过 50 MB");
    return;
  }
  // 恢复目标身份在选择文件时确定：读取文件期间可能切换用户，确认框与
  // 请求都必须写进当初选择的用户，不能跟着 state.userId 漂移
  const targetUser = state.userId;
  // 读取 / 解析期间也占住入口：大备份读取可达数秒，此前按钮要到确认之后
  // 才禁用，期间可再次打开文件选择并发启动第二个恢复（双确认、双 POST）
  els.importData.disabled = true;
  els.importData.textContent = "读取中…";
  try {
    await _runUserBackupImport(file, targetUser);
  } finally {
    els.importData.disabled = false;
    els.importData.textContent = "恢复备份";
  }
}

async function _runUserBackupImport(file, targetUser) {
  if (!file) return;
  let backupText;
  try {
    // 严格 UTF-8 解码：GBK / ANSI 备份此前被静默替换成 U+FFFD 仍能导入，
    // 中文正文永久损坏；CLI 对同一文件会明确拒绝。非 UTF-8 直接提示重存。
    const buffer = await file.arrayBuffer();
    backupText = new TextDecoder("utf-8", { fatal: true }).decode(buffer);
  } catch {
    toast("备份文件不是 UTF-8 编码，请另存为 UTF-8 后重试");
    return;
  }
  let backup;
  try {
    backup = JSON.parse(backupText);
  } catch {
    toast("无法解析备份 JSON");
    return;
  }
  if (!backup || typeof backup !== "object" || Array.isArray(backup)) {
    toast("备份文件格式不正确");
    return;
  }
  const counts = {
    preferences: Object.keys(backup.preferences || {}).length,
    trips: Array.isArray(backup.trip_history) ? backup.trip_history.length : 0,
    messages: Array.isArray(backup.chat_history) ? backup.chat_history.length : 0,
    knowledge: Array.isArray(backup.knowledge) ? backup.knowledge.length : 0,
  };
  if (
    !counts.preferences &&
    !counts.trips &&
    !counts.messages &&
    !counts.knowledge
  ) {
    toast("备份中没有可恢复的数据");
    return;
  }
  if (state.userId !== targetUser) {
    // 读取 / 解析期间切换了用户：恢复进旧用户会与界面上显示的身份不符，
    // 直接取消（文件内容仍在用户手里，重新选择即可）
    toast("用户已切换，已取消恢复备份");
    return;
  }
  // 读取 / 解析期间面板被关闭（Esc / 遮罩 / 关闭按钮，大备份可达数秒）：
  // 不再弹确认，更不能在看不见的界面里写库（与行程 / 知识导入同口径）
  if (els.memorySheet.hidden) return;
  const source = backup.user_id ? `（来自 ${backup.user_id}）` : "";
  const knowledgeNote = counts.knowledge
    ? `\n知识片段 ${counts.knowledge} 条将合并到共享知识库。`
    : "";
  const confirmed = window.confirm(
    `恢复备份${source}？\n偏好 ${counts.preferences} 项、行程 ${counts.trips} 条、` +
      `聊天消息 ${counts.messages} 条将合并到当前用户 ${targetUser}。` +
      knowledgeNote,
  );
  if (!confirmed) return;
  els.importData.textContent = "恢复中…";
  try {
    // 大备份的批量写入可能跑几分钟（单 worker + 用户锁）：默认 120 秒
    // 超时会在服务端仍在写入时前端就报失败并跳过刷新，放宽到 10 分钟
    const result = await getJSON(
      `/api/import?user_id=${encodeURIComponent(targetUser)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ backup }),
      },
      600000,
    );
    // 服务端 skipped 是「处理说明」列表（含未跳过的保留说明与一条概括
    // 多条截断的汇总），不能当跳过条数：跳过口径用结构化计数
    const messagesSkipped = Number(result.messages_skipped || 0);
    const knowledgeNote =
      result.knowledge_imported || result.knowledge_skipped
        ? `，知识片段新增 ${result.knowledge_imported} / 跳过 ${result.knowledge_skipped} 条`
        : "";
    // 与 CLI 同口径：隔离账号 + 无写回数据（recovery_incomplete）时
    // 不能以「已恢复」开头，否则同一条 toast 先声称恢复成功再追加警告；
    // ok=false 且非知识片段失败 = 备份条目全部被墓碑期 / 保留字会话
    // 跳过（什么都没恢复），同样不能用「已恢复」开头
    const emptied =
      result.ok === false && !result.recovery_incomplete && !result.knowledge_error;
    const restorePrefix = result.recovery_incomplete
      ? "已写入："
      : emptied
        ? "未恢复："
        : "已恢复：";
    let restoreMessage =
      `${restorePrefix}偏好 ${result.preferences} 项，行程新增 ${result.trips_created} / ` +
      `更新 ${result.trips_updated} 条，消息 ${result.messages_added} 条` +
      knowledgeNote +
      (messagesSkipped ? `，跳过重复消息 ${messagesSkipped} 条` : "");
    if (result.knowledge_error) {
      // 与摘要合并成一条：同一 tick 的第二次 toast 会复用同一元素并重置
      // 计时，恢复计数摘要永远不会显示，只剩知识片段失败提示
      restoreMessage += `；知识片段未恢复：${result.knowledge_error}`;
    }
    if (result.recovery_incomplete) {
      // 隔离账号 + 备份无可写回的用户数据：不能显示为恢复成功
      restoreMessage +=
        "；⚠ 用户数据未恢复：备份中没有可写回的用户数据，账号仍处于记忆隔离状态，请使用含偏好 / 行程 / 消息的备份重试";
    }
    if (emptied) {
      // 服务端已把「没有可恢复的数据」写进 skipped：原样展示第一条
      // 命中原因（含会话处于墓碑期 / 保留字），否则用固定文案兜底
      const reasons = Array.isArray(result.skipped) ? result.skipped : [];
      const reason = reasons.find(
        (item) =>
          typeof item === "string" && item.includes("没有可恢复的数据"),
      );
      restoreMessage +=
        "；" +
        (reason ||
          "没有可恢复的数据（备份中的会话均处于墓碑期或使用了保留字，已全部跳过）");
    }
    if (state.userId !== targetUser) {
      // 合并成一条：同一 tick 的第二次 toast 会覆盖计数摘要
      restoreMessage +=
        "；用户已切换，面板未刷新（恢复结果写入原用户，切回后重新打开面板即可看到）";
    }
    toast(restoreMessage);
    if (state.userId !== targetUser) {
      // 面板刷新会显示新用户的旧数据，跳过刷新
    } else {
      // 刷新一律静默且互不阻断：任一子刷新失败（限流 / 存储抖动）产生的
      // toast 会在同一 tick 覆盖刚显示的恢复摘要
      await Promise.allSettled([
        refreshStatus({ silentFailure: true }),
        loadSessions(undefined, { silentFailure: true }),
        refreshKnowledge(),
        // 备份可能带 knowledge 段：片段列表与分类同步刷新，否则库 chip 显示
        // 新增数量、下方列表仍是恢复前的旧数据
        refreshKnowledgeDocuments({ silentFailure: true }),
        refreshKnowledgeCategories(),
      ]);
      // 当前会话若也在备份里，重拉一次展示恢复出的消息；不在时保留现有界面。
      // 正在编辑提问 / 切换会话时跳过：整体重渲染会销毁编辑框并作废在途加载
      if (
        state.sessionId &&
        !state.sessionLoading &&
        !document.querySelector(".user-edit-input") &&
        !document.querySelector(".user-edit-form")
      ) {
        await openSession(state.sessionId, {
          silent: true,
          keepOnError: true,
          preserveOnEmpty: true,
        });
      }
    }
  } catch (error) {
    const errorText = String(error.message || "");
    const status = Number(error.status || 0);
    if (status === 408) {
      // 408 = 请求体读取超时：服务端尚未解析、零写入，可安全重试
      toast("上传超时：备份未写入服务器，请重试（网络较慢时可换更小的备份）");
    } else if (status === 503 && errorText.startsWith("恢复部分完成")) {
      // 批量写入中途失败：部分数据已落库，如实展示服务端文案并刷新视图
      toast(errorText);
      if (state.userId === targetUser) {
        await Promise.allSettled([
          refreshStatus({ silentFailure: true }),
          loadSessions(undefined, { silentFailure: true }),
          refreshKnowledgeDocuments({ silentFailure: true }),
        ]);
      }
    } else if (!status && errorText.includes("超时")) {
      // 客户端放弃等待不代表服务端没在写：单 worker 队列 / 大批量写入
      // 仍会继续，如实提示并尽力刷新一次当前视图
      toast("恢复请求超时：服务端可能仍在写入，请稍后刷新面板确认结果");
      if (state.userId === targetUser) {
        refreshStatus({ silentFailure: true });
        loadSessions(undefined, { silentFailure: true });
      }
    } else if (!status || status >= 500) {
      // 服务端 5xx / 连接中断：写入可能已部分生效（通用 503 注明
      // 「恢复可能部分成功」），按结果未知处理并尽力刷新当前视图
      toast(
        `恢复结果未知：${errorText}；服务端可能已部分写入，已刷新面板，可重试补齐剩余部分`,
      );
      if (state.userId === targetUser) {
        await Promise.allSettled([
          refreshStatus({ silentFailure: true }),
          loadSessions(undefined, { silentFailure: true }),
          refreshKnowledgeDocuments({ silentFailure: true }),
        ]);
      }
    } else {
      toast(`恢复失败：${errorText}`);
    }
  }
}

/* ---------- 执行路线 ---------- */

// 与 prefDisplayName / agentDisplayName 同口径：意图名来自模型输出，
// 原型链成员（constructor / __proto__）不能当映射值
function intentDisplayName(key) {
  return Object.prototype.hasOwnProperty.call(INTENT_NAMES, key)
    ? INTENT_NAMES[key]
    : key;
}

function summarizeIntents(intents) {
  if (!Array.isArray(intents)) return "";
  const names = intents
    .map((item) => {
      if (typeof item === "string") return intentDisplayName(item);
      if (item && typeof item === "object") {
        const key = item.type || item.intent || item.name || "";
        return intentDisplayName(key);
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
      stop = upsertStop(`agent:${name}:extra`, agentDisplayName(name), note, status);
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
        const label = agentDisplayName(agentName);
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
      const retry = chipButton("refresh", "重试", () => retryFailed(root, regenText));
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
  if (document.querySelector(".user-edit-input")) {
    if (!window.confirm("正在编辑的修改尚未保存，新建对话将丢弃修改。继续？")) {
      return;
    }
  }
  sessionLoadToken += 1;
  state.sessionLoading = false;
  state.sessionLoadingId = null;
  state.sessionId = null;
  state.partialHistory = false;
  state.retiredThreadVisible = false;
  safeStorageRemove("session", "aligo.sessionId");
  syncSessionControls();
  setConversationTitle("新对话");
  showWelcome();
  if (leaveArchivedViewForNewConversation()) {
    // 归档 / 搜索过滤下新建对话：切回普通列表并重载，否则新会话不会出现在侧栏
    loadSessions("", { reset: true });
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
  remove.dataset.action = "remove";
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
    restore.dataset.action = "restore";
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
    pin.dataset.action = "pin";
    pin.title = session.pinned ? "取消置顶" : "置顶会话";
    pin.setAttribute("aria-label", pin.title);
    pin.innerHTML = icon("pin");
    pin.addEventListener("click", (event) => {
      event.stopPropagation();
      togglePin(session);
    });

    const rename = document.createElement("button");
    rename.type = "button";
    rename.dataset.action = "rename";
    rename.title = "重命名会话";
    rename.setAttribute("aria-label", "重命名会话");
    rename.innerHTML = icon("edit");
    rename.addEventListener("click", (event) => {
      event.stopPropagation();
      startRenameSession(item, session);
    });

    const archive = document.createElement("button");
    archive.type = "button";
    archive.dataset.action = "archive";
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
  const focused = document.activeElement;
  const focusInfo =
    focused instanceof HTMLElement && els.history.contains(focused)
      ? {
          id: focused.closest(".history-item")?.dataset.id || "",
          action: focused.dataset.action || "",
          // 「加载更多」「归档匹配提示」不在 .history-item 内，用 key 定位
          key: focused.dataset.focusKey || "",
          // 目标行可能因删除 / 归档而消失：记住旧索引，找不到目标时就近兜底
          index: (() => {
            const items = Array.from(
              els.history.querySelectorAll(".history-item"),
            );
            const item = focused.closest(".history-item");
            return item ? items.indexOf(item) : items.length;
          })(),
        }
      : null;
  // 有正在重命名的输入框时记录 id 与已输入内容，重建列表后恢复：
  // 置顶 / 归档 / 删除 / 加载更多等操作触发的刷新不能吞掉正在输入的标题
  const renameInput = els.history.querySelector(".hi-input");
  const renameSnapshot = renameInput
    ? {
        id: renameInput.closest(".history-item")?.dataset.id || "",
        value: renameInput.value,
      }
    : null;
  els.history.innerHTML = "";

  const appendArchivedHint = () => {
    if (state.showArchived || state.archivedMatches <= 0) return;
    const hint = document.createElement("button");
    hint.type = "button";
    hint.className = "history-more archived-hint";
    hint.dataset.focusKey = "archived-hint";
    hint.textContent = `另有 ${state.archivedMatches} 条归档会话匹配`;
    hint.addEventListener("click", async () => {
      state.showArchived = true;
      updateArchivedToggle();
      // 失败时 loadSessions 已按已渲染快照回滚并重渲染（快照为 null 时
      // 回滚为非归档视图），这里只收敛按钮态
      await loadSessions(state.searchQuery, { reset: true });
      updateArchivedToggle();
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
    restoreHistoryFocus(focusInfo);
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

  const viewAligned =
    (state.sessionsLoadedQuery === null ||
      state.searchQuery === state.sessionsLoadedQuery) &&
    (state.sessionsLoadedArchived === null ||
      state.showArchived === state.sessionsLoadedArchived);
  if (viewAligned && state.sessions.length < state.sessionTotal) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "history-more";
    more.dataset.focusKey = "more-sessions";
    more.textContent = `加载更多会话（已显示 ${state.sessions.length} / 共 ${state.sessionTotal}）`;
    more.addEventListener("click", loadMoreSessions);
    els.history.appendChild(more);
  }
  appendArchivedHint();
  restoreHistoryFocus(focusInfo);
  restoreRenameSnapshot(renameSnapshot);
}

function restoreRenameSnapshot(snapshot) {
  if (!snapshot || !snapshot.id) return;
  const item = els.history.querySelector(
    `.history-item[data-id="${CSS.escape(snapshot.id)}"]`,
  );
  const session = state.sessions.find(
    (entry) => entry.session_id === snapshot.id,
  );
  if (!item || !session) return;
  startRenameSession(item, session, snapshot.value);
}

function restoreHistoryFocus(info) {
  if (!info) return;
  if (info.key) {
    const keyed = els.history.querySelector(
      `[data-focus-key="${CSS.escape(info.key)}"]`,
    );
    if (keyed) {
      keyed.focus();
      return;
    }
  }
  if (info.id) {
    const item = els.history.querySelector(
      `.history-item[data-id="${CSS.escape(info.id)}"]`,
    );
    if (item) {
      const target = info.action
        ? item.querySelector(`[data-action="${info.action}"]`)
        : item;
      (target || item).focus();
      return;
    }
  }
  // 行 / 按钮已消失（删除、归档、取消归档、分页取尽）：浏览器移除焦点
  // 元素不会转移焦点，不兜底时 activeElement 落到 body、键盘用户下一次
  // Tab 从页面顶部重新开始。按旧位置就近聚焦剩余行，列表清空时回搜索框
  const items = Array.from(els.history.querySelectorAll(".history-item"));
  if (items.length) {
    const index = Math.min(
      Math.max(0, Number.isFinite(info.index) ? info.index : 0),
      items.length - 1,
    );
    items[index].focus();
    return;
  }
  if (els.historySearch) els.historySearch.focus();
}

async function togglePin(session) {
  try {
    await getJSON(`/api/sessions/${encodeURIComponent(session.session_id)}/pin`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: state.userId, pinned: !session.pinned }),
    });
    toast(session.pinned ? "已取消置顶" : "已置顶会话");
    // 静默：收尾刷新失败不能覆盖刚显示的操作结果
    await loadSessions(state.searchQuery, { silentFailure: true });
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
      loadSessions(state.searchQuery, {
        reset: true,
        silentFailure: true,
      }),
      refreshStatus({ silentFailure: true }),
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

function leaveArchivedViewForNewConversation() {
  // 新对话永远不在归档列表 / 搜索结果里：停留在任一过滤视图都会让新会话
  // 从侧栏消失。返回是否发生了视图切换，调用方据此决定是否立即重载列表
  let changed = false;
  if (state.showArchived) {
    state.showArchived = false;
    updateArchivedToggle();
    changed = true;
  }
  if (state.searchQuery || (els.historySearch && els.historySearch.value)) {
    // 搜索框与状态词必须同步清空：只清一侧会让高亮 / 分页 / 归档子请求
    // 继续使用旧词。同时取消在途防抖，避免旧词再次被应用
    clearTimeout(searchTimer);
    if (els.historySearch) els.historySearch.value = "";
    state.searchQuery = "";
    changed = true;
  }
  return changed;
}

async function loadSessions(query = state.searchQuery, options = {}) {
  const previousQuery = state.searchQuery;
  state.searchQuery = String(query || "").trim();
  const requestId = (sessionsRequestId += 1);
  state.sessionsLoading = true;
  // 重置（搜索 / 切换用户）取第一页；其它刷新（重命名 / 置顶 / 删除）
  // 保留用户已经「加载更多」展开的数量
  const targetCount = options.reset ? 50 : Math.max(50, state.sessions.length);
  const limit = Math.min(SESSION_REFRESH_LIMIT_MAX, targetCount);
  const params = new URLSearchParams({
    user_id: state.userId,
    limit: String(limit),
    offset: "0",
  });
  if (state.searchQuery) params.set("q", state.searchQuery);
  if (state.showArchived) params.set("archived", "1");
  try {
    const data = await getJSON(`/api/sessions?${params}`, undefined, 30000);
    if (requestId !== sessionsRequestId) return;
    state.sessions = data.sessions || [];
    state.sessionsOffset = state.sessions.length;
    state.sessionTotal = Number(data.total || state.sessions.length);
    // 单页上限 1000：刷新一次取回整个已加载窗口；仅在超过上限的极端
    // 情况按刷新前数量继续取后续页并合并，达到目标或服务端取尽为止
    if (!options.reset && state.sessions.length < targetCount) {
      const seen = new Set(state.sessions.map((item) => item.session_id));
      let serverOffset = state.sessions.length;
      while (state.sessions.length < targetCount) {
        const moreParams = new URLSearchParams(params);
        moreParams.set("offset", String(serverOffset));
        const more = await getJSON(
          `/api/sessions?${moreParams}`,
          undefined,
          30000,
        );
        if (requestId !== sessionsRequestId) return;
        const page = more.sessions || [];
        if (!page.length) break;
        serverOffset += page.length;
        for (const item of page) {
          if (!seen.has(item.session_id)) {
            seen.add(item.session_id);
            state.sessions.push(item);
          }
        }
        if (page.length < limit) break;
      }
      state.sessionsOffset = serverOffset;
    }
    // 快照紧跟数据写入：归档匹配是独立子请求，期间被后续 reset 取代时
    // 本请求会早退——快照必须已经与 state.sessions / offset 对齐
    state.sessionsLoadedQuery = state.searchQuery;
    state.sessionsLoadedArchived = state.showArchived;
    const archivedCount = Number(data.archived_count || 0);
    if (data.archived_count !== undefined) {
      updateArchivedToggle(archivedCount);
    }
    // 普通视图搜索时提示归档会话中的匹配数量（不把归档内容混进主列表）。
    // 没有归档会话时该子请求的 total 必为 0（归档列表是 archived∩live
    // 的子集），白跑一次全量搜索没有意义。探针不阻塞主结果上屏：它要再
    // 扫一遍全量归组（大账号近 200ms），等它对「加载更多」还持有锁；
    // 返回后仅在列表仍是同一查询 / 视图时补提示
    if (state.searchQuery && !state.showArchived && archivedCount > 0) {
      const probedQuery = state.searchQuery;
      const probedArchived = state.showArchived;
      const probedUser = state.userId;
      const archivedParams = new URLSearchParams({
        user_id: probedUser,
        limit: "1",
        offset: "0",
        archived: "1",
        q: probedQuery,
      });
      getJSON(`/api/sessions?${archivedParams}`, undefined, 30000)
        .then((archivedData) => {
          if (
            state.userId !== probedUser ||
            state.showArchived !== probedArchived ||
            state.sessionsLoadedQuery !== probedQuery ||
            state.sessionsLoadedArchived !== probedArchived
          ) {
            return;
          }
          state.archivedMatches = Number(archivedData.total || 0);
          renderHistory();
        })
        .catch(() => {});
    }
  } catch (error) {
    if (requestId !== sessionsRequestId) return;
    state.sessionsLoading = false;
    if (options.reset) {
      // 失败不能把查询词留成新词：列表与分页游标仍是旧查询的。回滚目标
      // 必须是「当前列表实际渲染的查询」（快照），而不是「上一次请求的
      // 查询词」——请求被后一次取代时 previousQuery 从未渲染过，状态与
      // 列表会分叉，「加载更多」随后把两个查询的结果混进同一列表
      state.searchQuery =
        state.sessionsLoadedQuery === null
          ? previousQuery
          : state.sessionsLoadedQuery;
      state.showArchived = Boolean(state.sessionsLoadedArchived);
      updateArchivedToggle();
      // 搜索框里仍是这次失败的新词时同步回滚：否则可见查询与实际过滤
      // （归档切换 / 加载更多 / 高亮）长期分叉，用户会把旧结果当成新词结果。
      // 只在输入框仍等于本次请求词时改写，避免覆盖请求期间新输入的草稿；
      // 输入法合成期间绝不改写（会打断 / 提交 IME 组合，compositionend
      // 会重新调度搜索，可见词与 state.searchQuery 自然收敛）
      if (
        !state.historyComposing &&
        els.historySearch &&
        els.historySearch.value.trim() === String(query || "").trim() &&
        els.historySearch.value !== state.searchQuery
      ) {
        clearTimeout(searchTimer);
        els.historySearch.value = state.searchQuery;
      }
    }
    // 存储故障 / 网络抖动不能伪装成「没有历史」：保留已有列表并提示
    // （恢复备份后的批量刷新走静默：失败提示会覆盖恢复摘要）
    if (!options.silentFailure) {
      toast(`加载会话列表失败：${error.message || error}`);
    }
    if (!state.sessions.length) {
      // 空列表也要让调用方（归档切换）知道失败并回滚状态位；列表内容
      // 已清空，渲染快照随之失效（任何查询都可重试）
      state.sessionTotal = 0;
      state.archivedMatches = 0;
      state.sessionsLoadedQuery = null;
      state.sessionsLoadedArchived = null;
      els.history.innerHTML =
        '<div class="history-empty">加载会话列表失败，请稍后重试</div>';
      return false;
    }
    renderHistory();
    return false;
  }
  if (requestId !== sessionsRequestId) return;
  state.sessionsLoading = false;
  state.archivedMatches = 0;
  renderHistory();
  return true;
}

async function loadMoreSessions() {
  // 视图切换 / 新搜索的第一页仍在加载时，旧列表的 offset 不能用于新查询
  if (state.sessionsLoading) return;
  const alignedQuery =
    state.sessionsLoadedQuery === null ||
    state.searchQuery === state.sessionsLoadedQuery;
  const alignedArchived =
    state.sessionsLoadedArchived === null ||
    state.showArchived === state.sessionsLoadedArchived;
  if (!alignedQuery || !alignedArchived) {
    // 当前状态与已渲染列表不一致（搜索失败回滚前 / 归档切换中）：
    // 以新查询 + 旧游标取页会把两个查询的结果混进同一列表。快照为
    // null 表示还没有任何经 loadSessions 渲染的列表，按当前状态放行
    return;
  }
  const viewQuery = state.searchQuery;
  const viewArchived = state.showArchived;
  const totalBefore = state.sessionTotal;
  const requestId = (sessionsRequestId += 1);
  const params = new URLSearchParams({
    user_id: state.userId,
    limit: "50",
    offset: String(state.sessionsOffset),
  });
  if (state.sessionsLoadedQuery) params.set("q", state.sessionsLoadedQuery);
  if (state.sessionsLoadedArchived) params.set("archived", "1");
  try {
    const data = await getJSON(`/api/sessions?${params}`, undefined, 30000);
    if (requestId !== sessionsRequestId) return;
    if (viewQuery !== state.searchQuery || viewArchived !== state.showArchived) return;
    const totalNum = Number(data.total);
    if (Number.isFinite(totalNum) && totalNum !== totalBefore) {
      // 分页期间列表被其它标签页 / CLI 增删：offset 游标已失真（删除靠前
      // 会话会让「加载更多」永久跳过中间条目），按当前已加载数量从 0
      // 重取整窗；本页不再并入、也不按取回条数前进
      await loadSessions(state.searchQuery, { silentFailure: true });
      return;
    }
    const seen = new Set(state.sessions.map((session) => session.session_id));
    const fetched = data.sessions || [];
    for (const session of fetched) {
      if (!seen.has(session.session_id)) {
        seen.add(session.session_id);
        state.sessions.push(session);
      }
    }
    // 游标按「取回的条数」前进：头部并发插入时本页可能与已显示内容重叠
    // （去重跳过），但窗口必须继续向前，否则「加载更多」永远停在原地
    state.sessionsOffset += fetched.length;
    // total=0 是合法值（其它标签页 / CLI 已删光）：真值判断会把它当缺失
    if (Number.isFinite(totalNum)) state.sessionTotal = totalNum;
    if (totalNum === 0 && !fetched.length) {
      // 服务端确认该查询下已无会话：陈旧行不能继续算「共 N 条」
      state.sessions = [];
      state.sessionsOffset = 0;
    } else if (!fetched.length && state.sessions.length < state.sessionTotal) {
      // 空页但 total 仍大于已显示数量：其它标签页 / CLI 把未加载的靠后
      // 会话移到列表头部（total 不变），offset 窗口已被推过服务端行尾，
      // 被移动的会话不会再落入任何后续窗口——按已加载数量从 0 重取整窗，
      // 否则「加载更多」永久停在 N-1/N 且该会话不可见
      await loadSessions(state.searchQuery, { silentFailure: true });
      return;
    }
    renderHistory();
  } catch (error) {
    // 已作废的分页请求（新搜索 / 归档切换 / 新刷新）失败不能弹在当前视图上
    if (requestId !== sessionsRequestId) return;
    if (viewQuery !== state.searchQuery || viewArchived !== state.showArchived) return;
    toast(error.message);
  }
}

async function openSession(sessionId, options = {}) {
  if (state.busy) {
    if (!options.silent) toast("请等待当前回复完成");
    return false;
  }
  // 正在删除的会话不能再打开：DELETE 在途期间发出的详情 GET 会读到删除前
  // 的消息，可能在 DELETE 成功、侧栏行已移除之后才返回，把已删除会话
  // 写回当前状态
  if (state.deletingSessions.has(sessionId)) {
    if (!options.silent) toast("会话正在删除");
    return false;
  }
  // 行内编辑中的草稿只存在于 DOM：切换会话会整段重建消息区、草稿静默丢失。
  // 静默重载（停止对账 / 外部刷新）由调用方保证不在编辑时触发
  if (!options.silent && document.querySelector(".user-edit-input")) {
    if (!window.confirm("正在编辑的修改尚未保存，切换会话将丢弃修改。继续？")) {
      return;
    }
  }
  // 静默重载（停止对账）会重建整个消息区：用户此时可能正在向上翻历史，
  // 重建后强制滚到底会把阅读位置夺走——记录重载前的滚动位置
  const previousScrollTop = els.scroll.scrollTop;
  const wasAtBottom = atBottom();
  const token = ++sessionLoadToken;
  if (sessionLoadController) sessionLoadController.abort();
  sessionLoadController = new AbortController();
  state.sessionLoading = true;
  state.sessionLoadingId = sessionId;
  // 加载期间隐藏旧会话的「编辑」入口，避免点击后无反馈
  refreshEditButtons();
  try {
    const data = await getJSON(
      `/api/sessions/${encodeURIComponent(sessionId)}?user_id=${encodeURIComponent(state.userId)}`,
      { signal: sessionLoadController.signal },
    );
    // 加载期间用户可能已开始新对话或切换会话，避免覆盖当前界面
    if (state.busy || token !== sessionLoadToken) return;
    if (!data.messages || !data.messages.length) {
      if (options.preserveOnEmpty) return;
      const known = state.sessions.some(
        (item) => item.session_id === sessionId,
      );
      if (known) {
        // 列表里的会话已无消息：多半是其它标签页 / CLI 已删除。不能按
        // 「新会话」处理（会把用户当前正在看的对话切走），移除幽灵行并提示
        state.sessions = state.sessions.filter(
          (item) => item.session_id !== sessionId,
        );
        state.sessionTotal = Math.max(0, state.sessionTotal - 1);
        state.sessionsOffset = Math.max(0, state.sessionsOffset - 1);
        renderHistory();
        toast("这个会话已不存在，已从列表移除");
        // 明确告知调用方「没有加载到目标会话」：sendMessage 的发送前
        // 守卫按 loaded !== true 中止，否则会把提问写进已删除的会话
        return false;
      }
      if (sessionId !== state.sessionId) {
        // 目标不是当前对话（陈旧的状态卡片 / 行程行指向已删除会话）：
        // 不能按「新会话」把用户正在看的对话切走；刷新状态卡即可清掉
        // 服务端已删除的陈旧引用
        if (!options.silent) toast("这个会话已不存在或还没有消息");
        refreshStatus({ silentFailure: true }).catch(() => {});
        return;
      }
      if (!options.silent) toast("这个会话还没有消息");
      newSession();
      return;
    }
    state.sessionId = sessionId;
    // 打开会话成功后，可见线程属于该会话：清除「上一轮未落库、下一条
    // 消息发送前清空线程」的标记，否则刚加载的历史会在下一次发送时被清空
    state.retiredThreadVisible = false;
    safeStorageSet("session", "aligo.sessionId", sessionId);
    syncSessionControls();

    const session = state.sessions.find((item) => item.session_id === sessionId);
    // 详情响应里的标题比侧栏列表新（列表可能是并发刷新前的快照）
    setConversationTitle(
      (data.title && data.title !== "新对话" ? data.title : "") ||
        (session ? session.title : "") ||
        "对话",
    );

    // 先快照上次渲染状态：下面会清零 partialHistory，无法再区分「上次
    // 窗口本就不含末尾（搜索命中 / 手动收起）」与「上次渲染到末尾、
    // 之后又有新消息落库」——后者复用旧上界会把刚落库的整轮问答藏进
    // 「显示更新消息」并误置 partialHistory（编辑 / 重新生成全部消失）
    const previousRender =
      state.renderedStart && state.renderedStart.sessionId === sessionId
        ? state.renderedStart
        : null;
    const previousPartial = Boolean(previousRender && state.partialHistory);
    els.messages.innerHTML = "";
    state.partialHistory = false;
    // 大历史按窗口渲染：整段同步逐条 renderMarkdown + innerHTML 会冻结主线程
    // （备份导入允许 20 万条消息）；「显示更早消息」从已取回的数据里补一批
    const totalMessages = data.messages.length;
    const matchIndex = Number.isInteger(options.matchIndex) ? options.matchIndex : -1;
    let renderStart = Math.max(0, totalMessages - HISTORY_RENDER_WINDOW);
    // 静默重载（停止对账）：用户此前可能点开过更早的消息，恢复同一渲染
    // 起点重建，否则已展开的历史从 DOM 消失、旧 scrollTop 指向其它内容。
    // 只对同一会话生效；展开过的条数是用户自己的选择，不会无限增长
    if (
      options.silent &&
      state.renderedStart &&
      state.renderedStart.sessionId === sessionId &&
      state.renderedStart.start < renderStart
    ) {
      renderStart = state.renderedStart.start;
    }
    let renderEnd = totalMessages;
    if (matchIndex >= 0 && matchIndex < renderStart) {
      // 搜索定位的目标在首屏窗口之外：命中处上下各留一半上下文，只渲染
      // **一个窗口**并在下方补「显示更新消息」。从目标一路渲染到会话结尾
      // 会绕过窗口上限，备份导入允许 20 万条消息，长会话会冻结主线程
      renderStart = Math.max(
        0,
        matchIndex - Math.floor(HISTORY_RENDER_WINDOW / 2),
      );
      renderEnd = Math.min(totalMessages, renderStart + HISTORY_RENDER_WINDOW);
    }
    if (
      options.silent &&
      previousPartial &&
      Number(previousRender.end) > renderStart &&
      Number(previousRender.end) < totalMessages
    ) {
      // 上次本就是不完整的窗口（搜索命中 / 用户手动收起）：静默重载恢复
      // 同一上界，不能把「命中处到会话结尾」整段同步渲染（#713 消除的
      // 整段渲染冻结）。上次已渲染到末尾时绝不复用旧上界——新落库的
      // 消息必须立即可见
      renderEnd = Number(previousRender.end);
    }
    const pairTextBefore = (index) => {
      for (let i = index - 1; i >= 0; i -= 1) {
        if (data.messages[i].role === "user") return data.messages[i].content;
      }
      return "";
    };
    const buildNodes = (start, end) => {
      const nodes = [];
      let lastUserText = pairTextBefore(start);
      let windowLastAssistantNode = null;
      let windowLastAssistantMeta = null;
      for (let index = start; index < end; index += 1) {
        const message = data.messages[index];
        if (message.role === "user") {
          lastUserText = message.content;
          nodes.push(renderUserNode(message.content));
        } else {
          const node = renderAssistantNode(message.content, {
            timestamp: message.timestamp,
            regenText: lastUserText,
            meta: message.meta || null,
          });
          windowLastAssistantNode = node;
          windowLastAssistantMeta = message.meta || null;
          nodes.push(node);
        }
      }
      for (const node of nodes) {
        // 批量历史一次挂入窗口内的全部消息（最多 200 条）：rise 入场动画
        // 会让整屏同时播放，长会话打开 / 补批时明显掉帧。批量节点固定
        // 关闭动画；实时流式追加的单条消息不受影响
        if (node.classList) node.classList.add("msg-static");
      }
      return {
        nodes,
        lastAssistantNode: windowLastAssistantNode,
        lastAssistantMeta: windowLastAssistantMeta,
      };
    };
    const initial = buildNodes(renderStart, renderEnd);
    initial.nodes.forEach((node) => els.messages.appendChild(node));
    state.renderedStart = { sessionId, start: renderStart, end: renderEnd };
    state.partialHistory = renderEnd < totalMessages;
    const lastAssistantNode = initial.lastAssistantNode;
    const lastAssistantMeta = initial.lastAssistantMeta;

    let earlierButton = null;
    if (renderStart > 0) {
      earlierButton = document.createElement("button");
      earlierButton.type = "button";
      earlierButton.className = "earlier-more";
      const updateEarlierButton = () => {
        earlierButton.textContent = `显示更早消息（还有 ${renderStart} 条）`;
      };
      updateEarlierButton();
      els.messages.insertBefore(earlierButton, els.messages.firstChild);
      earlierButton.addEventListener("click", () => {
        const nextStart = Math.max(0, renderStart - HISTORY_RENDER_WINDOW);
        const older = buildNodes(nextStart, renderStart);
        const anchor = earlierButton ? earlierButton.nextSibling : els.messages.firstChild;
        const before = els.scroll.scrollHeight;
        older.nodes.forEach((node) => els.messages.insertBefore(node, anchor));
        // 补入的内容把已有节点向下推：同步滚动量，保持当前阅读位置
        els.scroll.scrollTop += els.scroll.scrollHeight - before;
        renderStart = nextStart;
        state.renderedStart = { sessionId, start: renderStart, end: renderEnd };
        if (renderStart === 0) {
          earlierButton.remove();
          earlierButton = null;
        } else {
          updateEarlierButton();
        }
        refreshEditButtons();
        // 补入更早的回答后只保留最后一轮（DOM 末尾）的「重新生成」：
        // 旧回答重新长出按钮，点击会按提问文本匹配删除服务端最新一轮
        trimRegenButtons();
      });
    }

    let laterButton = null;
    if (renderEnd < totalMessages) {
      laterButton = document.createElement("button");
      laterButton.type = "button";
      laterButton.className = "earlier-more";
      const updateLaterButton = () => {
        laterButton.textContent = `显示更新消息（还有 ${
          totalMessages - renderEnd
        } 条）`;
      };
      updateLaterButton();
      els.messages.appendChild(laterButton);
      laterButton.addEventListener("click", () => {
        const nextEnd = Math.min(totalMessages, renderEnd + HISTORY_RENDER_WINDOW);
        const newer = buildNodes(renderEnd, nextEnd);
        newer.nodes.forEach((node) => els.messages.insertBefore(node, laterButton));
        renderEnd = nextEnd;
        // 与「显示更早消息」同口径：窗口状态必须跟随 DOM，否则下一次
        // 静默重载会按旧上界把刚展开的末尾重新折叠
        state.renderedStart = { sessionId, start: renderStart, end: renderEnd };
        if (renderEnd >= totalMessages) {
          laterButton.remove();
          laterButton = null;
          // 会话末尾重新进入 DOM：编辑 / 重新生成恢复指向真正的最后一轮
          state.partialHistory = false;
        } else {
          updateLaterButton();
        }
        refreshEditButtons();
        trimRegenButtons();
      });
    }

    if (lastAssistantNode && lastAssistantMeta) {
      addFollowUps(lastAssistantNode, lastAssistantMeta.agents || []);
    }
    trimRegenButtons();
    refreshEditButtons();
    markActiveHistory();
    const localMatch =
      matchIndex >= renderStart && matchIndex < renderEnd
        ? matchIndex - renderStart
        : -1;
    const matchNode =
      localMatch >= 0
        ? els.messages.querySelectorAll(".msg")[localMatch]
        : null;
    if (matchNode) {
      matchNode.classList.add("msg-flash");
      matchNode.scrollIntoView({ block: "center" });
      setTimeout(() => matchNode.classList.remove("msg-flash"), 1800);
    } else if (options.silent && !wasAtBottom) {
      // 静默重载保留阅读位置（内容只会在末尾追加，旧位置仍指向同一段历史）
      els.scroll.scrollTop = previousScrollTop;
    } else {
      scrollToBottom(true);
    }
    closeMobileSidebar();
    // 用户回到带「待对账停止回合」的会话（停止后切走、再切回）：重新对账。
    // setTimeout 0 让 flush 在 finally 复位 sessionLoading 之后执行；此时
    // token / 会话都与登记一致，已落库则静默重载，未落库则把提问放回输入框
    if (
      !options.silent &&
      state.pendingStoppedTurns.some((item) => item.sessionId === sessionId)
    ) {
      setTimeout(() => {
        flushPendingStoppedTurns().catch(() => {});
      }, 0);
    }
    return true;
  } catch (error) {
    // 被后续加载主动中止：这不是失败，静默返回（finally 按 token 清理状态）
    if (error && error.name === "AbortError") return false;
    // 请求已被新对话 / 会话切换作废：过期的失败不能在当前界面弹提示
    if (token !== sessionLoadToken || state.busy) return false;
    if (!options.silent) {
      toast(`加载会话失败：${error.message}`);
    } else {
      if (options.toastOnError) {
        // 调用方已先移除界面节点（如「重试」确认已落库后）：重载失败必须
        // 明说，不能继续提示「已按服务端历史刷新」
        toast(`加载会话失败：${error.message}`);
      }
      if (!options.keepOnError) {
        // 启动恢复失败（服务重启 / 网络抖动）：丢弃旧会话 ID，避免下一条消息
        // 被写进界面从未展示过的隐藏会话。后台静默刷新（停止生成后的同步）
        // 传 keepOnError，临时失败不能把已知存在的会话从界面上清掉
        state.sessionId = null;
        state.partialHistory = false;
        state.renderedStart = null;
        safeStorageRemove("session", "aligo.sessionId");
        syncSessionControls();
        showWelcome();
      }
    }
    return false;
  } finally {
    if (token === sessionLoadToken) {
      state.sessionLoading = false;
      state.sessionLoadingId = null;
      // 加载期间 refreshEditButtons 被跳过，这里补回编辑入口
      refreshEditButtons();
    }
  }
}

function startRenameSession(item, session, initialValue) {
  if (item.querySelector(".hi-input")) return;
  const titleEl = item.querySelector(".hi-title");
  if (!titleEl) return;

  const input = document.createElement("input");
  input.className = "hi-input";
  input.maxLength = 60;
  input.value =
    initialValue === undefined ? session.title || "" : initialValue;
  titleEl.replaceWith(input);
  input.focus();
  if (initialValue === undefined) {
    input.select();
  } else {
    input.setSelectionRange(input.value.length, input.value.length);
  }
  let settled = false;

  const cancel = () => {
    if (settled) return;
    settled = true;
    if (input.isConnected) input.replaceWith(titleEl);
    // 取消移除了当前焦点输入框：把焦点还给会话条目，键盘用户不丢位置
    if (item.isConnected) item.focus();
    if (state.historyRefreshPending) {
      state.historyRefreshPending = false;
      loadSessions();
    }
  };

  const save = async () => {
    if (settled) return;
    const value = input.value.trim();
    settled = true;
    try {
      const saved = await getJSON(
        `/api/sessions/${encodeURIComponent(session.session_id)}/title`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_id: state.userId, title: value }),
        },
      );
      toast(value ? "会话已重命名" : "已恢复自动标题");
      // 先移除输入框再刷新：否则 renderHistory 会把「正在重命名」的
      // 快照恢复出来，已保存的输入框重新出现
      if (input.isConnected) input.remove();
      // save 自己会刷新：清掉挂起的补刷新，避免重复请求
      state.historyRefreshPending = false;
      await loadSessions();
      // 保存路径同样移除了焦点输入框：列表重建后把焦点还给该会话条目
      const refreshed = els.history.querySelector(
        `.history-item[data-id="${CSS.escape(session.session_id)}"]`,
      );
      if (refreshed) refreshed.focus();
      if (state.sessionId === session.session_id) {
        const updated = state.sessions.find((s) => s.session_id === session.session_id);
        // PUT 响应里的 title 是权威值（空串 = 恢复自动标题，自动标题由
        // 列表刷新提供）；刷新失败 / 被并发刷新作废时不再回退旧标题
        const savedTitle =
          saved && typeof saved.title === "string" ? saved.title : "";
        setConversationTitle(
          savedTitle || (updated && updated.title) || "对话",
        );
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

// 会话已被删除（成功响应或对账确认）：当前视图切回新对话状态。
// 不用 newSession()：它会自增会话加载序号，把正在进行的其它会话切换一并作废
function clearDeletedSessionView(sessionId) {
  if (state.sessionId !== sessionId) return;
  state.sessionId = null;
  state.partialHistory = false;
  state.renderedStart = null;
  safeStorageRemove("session", "aligo.sessionId");
  syncSessionControls();
  setConversationTitle("新对话");
  showWelcome();
  // 删除归档会话后进入「新对话」状态：必须离开归档视图，
  // 否则随后新建的会话不会出现在侧栏（与 newSession 同口径）
  leaveArchivedViewForNewConversation();
}

async function deleteSession(sessionId) {
  if (state.busy) {
    toast("请等待当前回复完成");
    return;
  }
  // 同一会话的删除只发一次：双击确认的第二个 DELETE 返回 deleted:0 仍走
  // 成功路径，会把 sessionTotal / sessionsOffset 再各减一
  if (state.deletingSessions.has(sessionId)) return;
  if (!window.confirm("删除这个会话？聊天记录会从长期记忆中移除。")) return;
  // 响应返回前可能已切换用户：迟到响应不能改写新用户的会话列表 / 总数 /
  // 游标（会话按用户隔离，B 的列表与 A 的删除无关）
  const requestUserId = state.userId;
  state.deletingSessions.add(sessionId);
  // 只在删除的正是「正在加载的会话」时作废这次加载；删除其它会话不能
  // 把用户刚点击的会话切换静默丢弃（之后消息会写进错误的会话）
  if (state.sessionLoadingId === sessionId) {
    sessionLoadToken += 1;
    state.sessionLoading = false;
    state.sessionLoadingId = null;
  }
  try {
    await getJSON(
      `/api/sessions/${encodeURIComponent(sessionId)}?user_id=${encodeURIComponent(requestUserId)}`,
      { method: "DELETE" },
    );
    // 入口只作废了删除开始前的加载；DELETE 在途期间新发起的加载若已越过
    // 入口检查（守卫上线前、或删除完成后立刻点击），成功后再作废一次，
    // 避免详情响应迟到把已删除会话写回
    if (state.sessionLoadingId === sessionId) {
      sessionLoadToken += 1;
      state.sessionLoading = false;
      state.sessionLoadingId = null;
    }
    if (state.userId !== requestUserId) return;
    clearDeletedSessionView(sessionId);
    // 先本地移除会话行：随后刷新若失败（旧数据保留策略），不能留下
    // 一条点击后切到「新对话」的幽灵会话
    state.sessions = state.sessions.filter(
      (item) => item.session_id !== sessionId,
    );
    state.sessionTotal = Math.max(0, state.sessionTotal - 1);
    state.sessionsOffset = Math.max(0, state.sessionsOffset - 1);
    renderHistory();
    // 静默：收尾刷新失败不能覆盖「会话已删除」的成功提示
    await loadSessions(state.searchQuery, { silentFailure: true });
    // 状态卡片（「即将出行」等）可能引用已删除的会话：不刷新时点击会
    // 打开一个不存在的会话、把当前对话视图重置
    refreshStatus({ silentFailure: true }).catch(() => {});
    toast("会话已删除");
  } catch (error) {
    const status = Number((error && error.status) || 0);
    if (status && status < 500) {
      // 409（生成中）/ 422 / 429：确定失败，DELETE 没有执行
      if (state.userId === requestUserId) toast(`删除失败：${error.message}`);
      return;
    }
    // 超时 / 断连 / 代理 5xx：服务端可能已完成删除（墓碑先落库，重复
    // 删除也返回 200）。切号后的迟到失败不属于当前用户，直接忽略
    if (state.userId !== requestUserId) return;
    const reconciled = await loadSessions(state.searchQuery, {
      silentFailure: true,
    });
    const stillListed = state.sessions.some(
      (item) => item.session_id === sessionId,
    );
    if (reconciled === true && !stillListed) {
      clearDeletedSessionView(sessionId);
      refreshStatus({ silentFailure: true }).catch(() => {});
      toast("会话已删除（响应中断，已按服务端列表确认）");
    } else if (reconciled === true) {
      toast(`删除失败：${error.message}（已刷新，会话仍在，请重试）`);
    } else {
      toast(`删除结果未知：${error.message}；请刷新页面确认会话是否仍在`);
    }
  } finally {
    state.deletingSessions.delete(sessionId);
    // 入口 / 成功后作废在途会话加载时，被作废的 openSession 的 finally
    // 因 token 不匹配不会再补回「编辑」入口，这里统一补一次
    refreshEditButtons();
  }
}

/* ---------- 发送消息（SSE 流式） ---------- */

// 当前会话正在删除（DELETE 在途）：删除成功会连本轮一起移除、失败则因
// 用户锁 409。发送与「先删后发」的入口都必须先拦下。按会话 id 判定：
// 删除的是侧栏其它会话时，向当前对话发送仍然合法
function currentSessionDeleting() {
  return Boolean(state.sessionId) && state.deletingSessions.has(state.sessionId);
}

function setBusy(busy) {
  state.busy = busy;
  els.sendBtn.disabled = busy;
  // 非流式阶段（删除并重发 / 会话加载）也会置 busy，但此时没有可中止的
  // 回合：显示「停止」只会让用户点到无反应的按钮
  els.stopBtn.hidden = !busy || !state.controller;
}

async function refreshSessionsAfterTurn() {
  // 回复结束后刷新会话列表（新会话需出现在侧栏）。侧栏重命名输入框
  // 打开时重建列表会打断输入：登记待刷新，由重命名收尾补一次
  if (document.querySelector(".hi-input")) {
    state.historyRefreshPending = true;
    return;
  }
  await loadSessions();
}

async function sendMessage(text, options = {}) {
  const message = String(text || "").trim();
  if (!message || state.busy) return;
  // 程序化合并的草稿（停止 / 中断回填）可绕过 textarea 的 maxlength：
  // 超限时必须在清空输入、渲染、请求之前拦下，否则服务端 422 后原文
  // 只剩在必然失败的「重试」节点里
  if ([...message].length > CHAT_MESSAGE_MAX) {
    toast(`提问超过 ${CHAT_MESSAGE_MAX} 字，无法发送；请精简后再试`);
    els.input.focus();
    return;
  }
  if (!state.ready) {
    // 初始化请求尚未完成：此时发送会把消息写进尚未展示历史记录的旧会话
    toast("页面正在载入，请稍候…");
    return;
  }
  if (state.sessionLoading) {
    toast("会话正在加载，请稍候");
    return;
  }
  if (currentSessionDeleting()) {
    toast("会话正在删除，请稍候");
    return;
  }
  if (state.partialHistory && state.sessionId) {
    // 搜索命中视图只渲染了会话中间一段：直接发送会把新问答接在窗口末尾、
    // 中间历史缺失（发送后的静默重载还会把整段中间一起补渲染）。先回到
    // 最新的尾部窗口再发送。等待期间用户可能切换到别的会话：恢复完成后
    // 目标会话已变时必须中止，不能把提问写进刚打开的会话
    const targetSessionId = state.sessionId;
    const loaded = await openSession(targetSessionId);
    if (
      loaded !== true ||
      state.sessionId !== targetSessionId ||
      state.sessionLoading
    ) {
      toast("会话已切换或加载失败，请重试");
      return;
    }
  }
  const draft = els.input.value;
  if (!options.skipEditConfirm && !confirmDiscardPendingEdit("发送新消息")) {
    return;
  }

  cancelPendingEdits();
  sessionLoadToken += 1;
  // 欢迎页 / 删除后直接发送会新建未归档会话：离开归档视图，
  // 否则回复后的列表刷新仍按 archived=1 过滤掉新会话
  if (!state.sessionId) leaveArchivedViewForNewConversation();
  state.lastUserText = message;
  state.controller = new AbortController();
  setBusy(true);
  trimRegenButtons();
  document.querySelectorAll(".follow-ups").forEach((row) => row.remove());

  let clearedRetiredThread = false;
  // 本轮渲染的用户气泡（echo:false 的重新生成不新建）：HttpError 分支
  // 需要把它与错误气泡一并移除，避免重发时出现两条相同的提问气泡
  let turnUserNode = null;
  let turnCleared = false;
  mutate(() => {
    if (state.retiredThreadVisible) {
      // 上一轮回答因会话被删除而未落库：可见线程不属于接下来的新会话，
      // 从干净线程开始，避免两个服务端会话的历史混在一起
      state.retiredThreadVisible = false;
      els.messages.innerHTML = "";
      clearedRetiredThread = true;
    }
    hideWelcome();
    // 线程被清空时 DOM 里已没有可复用的用户气泡（重新生成走 echo:false），
    // 必须补渲染，否则新会话只剩一条没有对应提问的回答
    if (options.echo !== false || clearedRetiredThread) {
      turnUserNode = renderUserNode(message);
      els.messages.appendChild(turnUserNode);
    }
  }, true);
  // 生成期间不保留上一条消息的编辑入口（结束后由 refreshEditButtons 移动到新提问）
  els.messages.querySelectorAll(".user-edit").forEach((node) => node.remove());
  // 按钮触发的发送（重新生成 / 追问 / 欢迎示例）不应丢掉正在输入的草稿
  if (draft.trim() === message) {
    els.input.value = "";
    resizeInput();
  }

  const assistant = createAssistantMessage(message);
  assistant.trace.start();
  mutate(() => els.messages.appendChild(assistant.root), true);

  // 本轮唯一标识：服务端写进助手消息的 _meta，停止后对账用它精确判断
  // 「这一轮是否已落库」（比较文本会把重复提问误判成已保存）。重试按钮
  // 复用原 id，让同一逻辑回合的重复提交命中服务端幂等回放
  const turnId = options.turnId || randomId();
  // 幂等键：网络层重试 / 断连重连时服务端按同一键回放已完成的回合，
  // 不重复执行、不重复落库（重试按钮复用原键；重新生成是新回合、用新键）
  const idempotencyKey = options.idempotencyKey || randomId();
  // 重试按钮据此在重发前确认「这一轮是否已被服务端落库」，避免重复问答
  assistant.root.dataset.turnId = turnId;
  assistant.root.dataset.idempotencyKey = idempotencyKey;
  let terminal = null;
  // 首字节之前没有心跳可依：服务端排队等对话锁最多 50s，给 90s 上限，
  // 防止链路黑洞时 fetch 永不 settle、busy 永久为真
  let headerTimedOut = false;
  const headerTimer = setTimeout(() => {
    headerTimedOut = true;
    if (state.controller) state.controller.abort();
  }, 90000);
  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        user_id: state.userId,
        session_id: state.sessionId,
        client_turn_id: turnId,
        idempotency_key: idempotencyKey,
      }),
      signal: state.controller.signal,
    });
    clearTimeout(headerTimer);
    if (!response.ok) {
      // 错误体读取同样要有时限：超时抛普通 Error（不是 AbortError），
      // 走「连接中断」兜底，finally 正常复位 busy；不能 abort 共享的
      // controller，否则会被当成用户点「停止」
      const detail = await withTimeout(
        errorMessage(response),
        ERROR_BODY_TIMEOUT_MS,
        `读取错误响应超时（HTTP ${response.status}），请重试`,
      );
      if (response.status === 404) {
        // 删除发生在流建立之前：错误事件分支不会触发，这里同样要切回
        // 新对话状态，否则之后每条消息都会 404，界面还停留在已删除会话上
        resetDeletedSessionState(message);
        toast("会话已被删除，下一条消息将开始新对话");
      }
      throw new HttpError(detail, response.status);
    }
    await readEventStream(response.body, (event) => {
      // 已收到 done / error 后忽略后续事件：连接错误不能覆盖已完成的回答
      if (terminal) return;
      if (event.type === "done" || event.type === "error") terminal = event.type;
      handleStreamEvent(assistant, event, message);
    });
    if (!terminal) {
      throw new Error("连接意外中断，回答可能不完整");
    }
    // 已降级（熔断 open/half_open 或技能缺失）：回合成功不能把顶栏改回
    // 「在线」，以 /api/health 的服务端裁决为准（仍降级则保持，恢复则更新）
    if (els.health.classList.contains("health warn")) {
      refreshHealth();
    } else {
      els.health.className = terminal === "error" ? "health err" : "health ok";
      // 圆点变色同时更新文案：只改 class 会让顶栏在连接异常后仍显示「在线」
      els.healthText.textContent = terminal === "error" ? "回答出错" : "在线";
      // 从「连接异常 / 服务未连接」恢复的连接成功：再让服务端健康校正 title
      if (terminal !== "error") refreshHealth();
    }
  } catch (error) {
    // 响应头阶段的看门狗触发的 abort 不是用户点「停止」：按连接超时处理，
    // 不能显示成「已停止生成」并跳过对账
    if (headerTimedOut && error && error.name === "AbortError") {
      error = new Error("连接超时，未能开始接收回答");
    }
    if (error.name === "AbortError") {
      // 服务端已发过 done / error 后再中止（停止按钮晚点、切换会话）：
      // 保留已完成的回答，不能改成「已停止」
      if (!terminal) {
        mutate(() => {
          assistant.trace.fail("已停止");
          assistant.fail("已停止生成。");
        });
        // 是否落库不能以 plan 事件为准：服务端在 plan 之后才执行编排，
        // 编排成功就会把提问与回答一起写入历史（即使客户端已停止读取）。
        // 有会话 ID 时稍后与服务端历史对账，按结果决定重渲染还是回填输入；
        // 没有会话 ID 说明首帧之前就中止，提问肯定没有落库，直接回填。
        const sessionId = state.sessionId;
        if (sessionId) {
          const token = sessionLoadToken;
          rememberStoppedTurn(sessionId, message, turnId, "已停止");
          setTimeout(
            () => reconcileStoppedTurn(sessionId, message, token, turnId),
            600,
          );
        } else {
          restoreUnconfirmedDraft(message, "已停止");
        }
      }
    } else if (error && error.name === "HttpError") {
      // 服务端已明确响应（429 限流 / 503 锁占用 / 422 校验等）：不是连接
      // 中断。展示服务端原因，不执行停止对账（服务端没有执行本轮，重发
      // 由用户决定），也不把顶栏健康状态改成「连接异常」。
      // 本轮必然未落库：移除刚渲染的提问与错误气泡、原因用 toast 展示，
      // 否则提问气泡留在界面（会话仍存在时不会消失），用户直接回车重发
      // 会再渲染一条相同的提问，同一问题在界面上出现两次。
      // 404（会话已删除）由上面的 HTTP 错误分支处理并已清空消息区
      if (error.status !== 404) {
        turnCleared = true;
        mutate(() => {
          if (turnUserNode) turnUserNode.remove();
          assistant.root.remove();
        });
        toast(error.message);
      } else {
        mutate(() => {
          assistant.trace.fail(error.message);
          assistant.fail(error.message);
        });
      }
      restoreDraftText(message);
    } else if (!terminal) {
      // 浏览器原生网络错误文案（Failed to fetch / Load failed）直接展示
      // 成英文且不解释，统一改成中文兜底
      const reason =
        error &&
        error.message &&
        !/failed to fetch|load failed|networkerror|network request failed/i.test(
          error.message,
        )
          ? error.message
          : "无法连接服务器，请检查网络后重试";
      mutate(() => {
        assistant.trace.fail(reason);
        assistant.fail(reason);
      });
      els.health.className = "health err";
      els.healthText.textContent = "连接异常";
      // 流在 done / error 之前中断（代理超时 / 断网 / 后端重启）：服务端
      // 可能已完成原子落库，同样要与历史对账——否则用户点「重试」会在
      // 历史里留下两条相同问答
      const sessionId = state.sessionId;
      if (sessionId) {
        const token = sessionLoadToken;
        rememberStoppedTurn(sessionId, message, turnId, "连接中断");
        setTimeout(
          () =>
            reconcileStoppedTurn(
              sessionId,
              message,
              token,
              turnId,
              "连接中断",
            ),
          600,
        );
      } else {
        restoreUnconfirmedDraft(message, "连接中断");
      }
    }
  } finally {
    clearTimeout(headerTimer);
    state.controller = null;
    // 成功完成（done）才把本轮提问视为已写入服务端历史；停止 / 失败 / 中断
    // 的提问没有落库，标记后不再显示「编辑」入口。
    // HttpError 已移除本轮气泡（turnCleared）：此时 DOM 末尾是上一轮真实
    // 已落库的提问，按 terminal=null 标记会误删它的「编辑」入口
    if (state.sessionId && !turnCleared) {
      const users = els.messages.querySelectorAll(".msg.user");
      const lastUser = users[users.length - 1];
      if (lastUser) applyTurnPersistence(lastUser, terminal, assistant.persisted);
    }
    setBusy(false);
    refreshEditButtons();
    // 只保留最新一条回答的「重新生成」：旧按钮对应的已不是最后一轮，
    // 点击只会触发 mismatch，甚至在外层历史中插入重复问答
    trimRegenButtons();
    // 弹层打开时只不抢焦点，会话列表与状态仍要刷新（新会话需出现在侧栏）
    const active = document.activeElement;
    const editing =
      active &&
      active !== els.input &&
      (active.tagName === "INPUT" ||
        active.tagName === "TEXTAREA" ||
        active.tagName === "SELECT" ||
        active.isContentEditable);
    if (!isModalOpen() && !editing) els.input.focus();
    await refreshSessionsAfterTurn();
    // refreshStatus 内部会在偏好编辑框存在时跳过渲染（编辑结束后补刷新）
    refreshStatus();
    // 上一轮停止 / 中断的对账可能被本回合的 busy / token 推进跳过：
    // 回合结束后补跑，否则已落库的回合永远停在「已停止生成」，未落库的
    // 提问也没有任何恢复入口
    flushPendingStoppedTurns().catch(() => {});
  }
}

/* 停止 / 中断后对账：服务端可能在停止前已完成编排并把整轮问答写入历史，
   也可能什么都没写。以助手消息 _meta 里的 client_turn_id 为准（原子落库
   保证用户 + 助手成对出现，回合标识精确到本次提问）：已落库就按服务端
   状态重渲染；连续几次都没出现，说明提问没有保存，重渲染旧历史后把原文
   放回输入框，避免永久丢失。 */
function restoreDraftText(text) {
  // 未落库的提问放回输入框：已有草稿时合并而不是丢弃原文
  const draftNow = els.input.value;
  if (!String(text || "").trim()) return;
  if (!draftNow.trim()) {
    els.input.value = text;
  } else if (!draftNow.includes(text)) {
    els.input.value = `${text}

${draftNow}`;
  }
  resizeInput();
}

/* 没有会话 ID 的停止 / 中断：客户端未收到首帧（含会话 ID），但服务端可能
   已在响应包装器里 drain 并落库——不能断言「提问肯定没有落库」。保留草稿
   回填，同时提示结果未知，引导走消息上的「重试」（先对账、命中幂等缓存时
   不会重复执行），避免用户直接重发产生重复问答。 */
function restoreUnconfirmedDraft(message, reason) {
  if (!els.input.value.trim()) {
    els.input.value = message;
    resizeInput();
  }
  toast(
    `${reason}，暂未确认保存结果；请先点上方消息的「重试」核对（不会重复执行），避免直接重发`,
  );
}

function rememberStoppedTurn(sessionId, message, turnId, reason) {
  // 停止 / 连接中断的回合先登记：对账可能在窗口内被新回合的 busy / token
  // 变化跳过，登记保留到有新结论为止
  if (!sessionId || !turnId) return;
  if (
    state.pendingStoppedTurns.some(
      (item) => item.sessionId === sessionId && item.turnId === turnId,
    )
  ) {
    return;
  }
  state.pendingStoppedTurns.push({ sessionId, message, turnId, reason });
}

function forgetStoppedTurn(sessionId, turnId) {
  state.pendingStoppedTurns = state.pendingStoppedTurns.filter(
    (item) => !(item.sessionId === sessionId && item.turnId === turnId),
  );
}

/* 新回合结束后补跑被跳过的停止对账：已落库则静默重载一致的历史，未落库
   则把提问放回输入框（与草稿合并）。不在 busy / 加载 / 编辑中执行。 */
async function flushPendingStoppedTurns() {
  if (!state.pendingStoppedTurns.length) return;
  if (state.busy || state.sessionLoading) return;
  if (
    document.querySelector(".user-edit-input") ||
    document.querySelector(".pref-input") ||
    document.querySelector(".hi-input")
  ) {
    return;
  }
  // 会话已切换：其它会话的登记不能动当前界面。登记本身必须保留——
  // 停止时留在旧会话消息区的提问气泡会随 openSession 重建消失，只有
  // 「回到该会话再对账」能把未落库的提问放回输入框
  for (const item of state.pendingStoppedTurns.slice()) {
    if (state.busy || state.sessionLoading) return;
    await reconcileStoppedTurn(
      item.sessionId,
      item.message,
      sessionLoadToken,
      item.turnId,
      item.reason,
    ).catch(() => {});
  }
}

async function reconcileStoppedTurn(sessionId, message, token, turnId, reason = "已停止") {
  // 返回值区分「暂不能动界面、等结束后补跑」（deferred，保留登记）与
  // 「已有结论」：只有后者清除待对账登记
  const key = `${sessionId}\u0000${turnId || ""}`;
  if (reconcilingStoppedTurns.has(key)) return "deferred";
  reconcilingStoppedTurns.add(key);
  try {
    const status = await reconcileStoppedTurnImpl(
      sessionId,
      message,
      token,
      turnId,
      reason,
    );
    if (status !== "deferred") {
      forgetStoppedTurn(sessionId, turnId);
    }
    return status;
  } finally {
    reconcilingStoppedTurns.delete(key);
  }
}

async function reconcileStoppedTurnImpl(sessionId, message, token, turnId, reason = "已停止") {
  if (!message) return;
  // 本函数自己会调用 openSession（它自增 sessionLoadToken）：只认启动时
  // 捕获的 token 会让 openSession 之后的检查恒假——最终确认与回填输入
  // 变成死代码，提问文本在消息区被清空后没有任何副本
  let expectedToken = token;
  const sameSession = () => sessionId === state.sessionId;
  const editingNow = () =>
    Boolean(
      document.querySelector(".user-edit-input") ||
        document.querySelector(".pref-input") ||
        document.querySelector(".hi-input"),
    );
  // 同一会话有新回合在跑（busy）或 token 被推进（新发送 / 静默重载）：
  // 现在不能动界面，但登记要保留，由 flushPendingStoppedTurns 补跑
  const shouldDefer = () =>
    sameSession() &&
    !editingNow() &&
    (state.busy || expectedToken !== sessionLoadToken);
  const canSync = () =>
    sameSession() && !state.busy && expectedToken === sessionLoadToken && !editingNow();
  const isPersisted = (messages) => {
    if (turnId) {
      // 精确匹配：助手消息的 _meta 带着本轮唯一的 client_turn_id。
      // 只比较「最后两条是相同文本的 user + assistant」会把重复提问
      // 误判成本轮已保存（实际上回答的是上一轮）
      return messages.some(
        (item) => item && item.meta && item.meta.client_turn_id === turnId,
      );
    }
    const last = messages[messages.length - 1];
    const prev = messages[messages.length - 2];
    return Boolean(
      last &&
        prev &&
        prev.role === "user" &&
        String(prev.content) === message &&
        last.role === "assistant",
    );
  };
  // 服务端断连后最多等 10 秒 drain（超时后落库线程仍可能继续）：判定期限
  // 必须覆盖它，前 4 次 700ms 快速命中，之后放慢到 1.5s 直到 ~12 秒
  let sawValidResponse = false;
  for (let attempt = 0; attempt < 10; attempt += 1) {
    // 会话已切换 / 新建对话：登记必须保留，等用户回到该会话时再对账；
    // 同会话的 busy / token / 编辑中语义不变（编辑中放弃按 done 处理）
    if (!canSync()) return !sameSession() || shouldDefer() ? "deferred" : "done";
    let data = null;
    try {
      data = await getJSON(
        `/api/sessions/${encodeURIComponent(sessionId)}?user_id=${encodeURIComponent(state.userId)}`,
        {},
        5000,
      );
    } catch (_) {
      data = null;
    }
    sawValidResponse = sawValidResponse || Boolean(
      data && Array.isArray(data.messages),
    );
    // 会话已切换 / 新建对话：登记必须保留，等用户回到该会话时再对账；
    // 同会话的 busy / token / 编辑中语义不变（编辑中放弃按 done 处理）
    if (!canSync()) return !sameSession() || shouldDefer() ? "deferred" : "done";
    if (data && Array.isArray(data.messages)) {
      const messages = data.messages;
      if (isPersisted(messages)) {
        await openSession(sessionId, {
          silent: true,
          keepOnError: true,
          preserveOnEmpty: true,
        });
        return;
      }
      if (attempt === 9) {
        const tokenBefore = sessionLoadToken;
        await openSession(sessionId, {
          silent: true,
          keepOnError: true,
          preserveOnEmpty: true,
        });
        if (sessionLoadToken !== tokenBefore + 1) {
          // 期间用户点击了其它会话（会话加载序号被额外推进）：不能认领
          // 那次加载的 token 把旧会话的提问写进新会话的输入框
          return;
        }
        // openSession 自增了 sessionLoadToken：后续检查认新 token
        expectedToken = sessionLoadToken;
        // openSession 的重新拉取可能刚好包含迟到的落库（服务端 drain 上限
        // 10 秒，客户端判定期限已覆盖）：以最新数据为准再确认一次，避免界面
        // 已显示保存的问答却把提问塞回输入框并提示未保存，用户重发后历史
        // 出现两条相同问答
        let confirmUnknown = false;
        try {
          const latest = await getJSON(
            `/api/sessions/${encodeURIComponent(sessionId)}?user_id=${encodeURIComponent(state.userId)}`,
            {},
            15000,
          );
          if (
            canSync() &&
            latest &&
            Array.isArray(latest.messages) &&
            isPersisted(latest.messages)
          ) {
            // 两次拉取之间刚落库：界面还是上一次（不含本轮）的重建结果，
            // 必须再刷一次——否则刚问的问题从消息区消失且没有任何提示
            await openSession(sessionId, {
              silent: true,
              keepOnError: true,
              preserveOnEmpty: true,
            });
            return;
          }
        } catch (_) {
          // 确认请求超时 / 失败：落库结果未知
          confirmUnknown = true;
        }
        // 会话已切换 / 新建对话：登记必须保留，等用户回到该会话时再对账；
        // 同会话的 busy / token / 编辑中语义不变（编辑中放弃按 done 处理）
        if (!canSync()) return !sameSession() || shouldDefer() ? "deferred" : "done";
        if (confirmUnknown) {
          // openSession 的重新拉取可能已渲染出刚落库的一轮：把提问塞回
          // 输入框会让用户重发、历史出现两条相同问答——只提示未知
          toast(`${reason}，暂未确认保存结果，请稍后刷新查看，避免重复发送`);
          return;
        }
        restoreDraftText(message);
        toast(
          `${reason}，暂未确认保存结果；提问未保存，已放回输入框（与草稿合并），请核对后重发`,
        );
        return;
      }
    }
    await new Promise((resolve) => setTimeout(resolve, attempt < 4 ? 700 : 1500));
  }
  // 判定期内始终拿不到有效响应（连续超时 / 服务不可达）：落库结果未知。
  // 不把提问塞回输入框——服务端 drain 可能已经落库，诱导重发会产生重复
  // 问答；期间用户可能已切换会话 / 用户，也要避免写进新的输入框
  if (!canSync()) return shouldDefer() ? "deferred" : "done";
  if (!sawValidResponse) {
    toast(`${reason}，暂未确认保存结果，请稍后刷新查看，避免重复发送`);
    return;
  }
  restoreDraftText(message);
  toast(
    `${reason}，暂未确认保存结果；提问未保存，已放回输入框（与草稿合并），请核对后重发`,
  );
}

// 服务端每 15s 发一次 ": keep-alive"：任意字节（含注释行）都会完成本轮
// read 并重置计时。连续 45s（3 个心跳周期）一个字节都没有，说明链路半开
// （移动网络切换 / 休眠唤醒 / NAT 空闲超时），read() 可能永不 settle——
// 没有看门狗时 busy 永久为真，界面卡在生成中且无法发送
const STREAM_IDLE_TIMEOUT_MS = 45000;

function readWithIdleTimeout(reader) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error("连接长时间无响应，已中断"));
    }, STREAM_IDLE_TIMEOUT_MS);
    reader.read().then(
      (result) => {
        clearTimeout(timer);
        resolve(result);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

async function readEventStream(body, onEvent) {
  if (!body) throw new Error("浏览器不支持流式响应");
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  // 按 SSE 分帧（\n\n）：同一事件的多个 data: 行拼接后再解析；注释行忽略
  const dispatch = () => {
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const chunk = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const dataLines = [];
      chunk.split("\n").forEach((line) => {
        if (!line || line.startsWith(":")) return;
        const colon = line.indexOf(":");
        const field = colon === -1 ? line : line.slice(0, colon);
        if (field !== "data") return;
        let payload = colon === -1 ? "" : line.slice(colon + 1);
        if (payload.startsWith(" ")) payload = payload.slice(1);
        dataLines.push(payload);
      });
      if (dataLines.length) {
        let event = null;
        try {
          event = JSON.parse(dataLines.join("\n"));
        } catch {
          event = null; // 忽略无法解析的事件
        }
        // 非对象帧（如 data: null）不再交给 handleStreamEvent 触发 TypeError
        if (event && typeof event === "object") onEvent(event);
      }
      boundary = buffer.indexOf("\n\n");
    }
  };

  try {
    while (true) {
      const { done, value } = await readWithIdleTimeout(reader);
      if (done) {
        // EOF 收尾：上游代理可能把「截断在帧尾空行前」的 EOF 当正常结束，
        // 此时最后一帧的 JSON 其实已完整（只是缺终止空行）。只在最后一行
        // 是 data: 时补空行再派发一次；半截 JSON 解析失败会被忽略，
        // 与现状一致。异常 / 中止路径不会走到这里，不会在 abort 后补发事件
        buffer += decoder.decode();
        buffer = buffer.replace(/\r\n/g, "\n");
        const lastLine =
          buffer
            .split("\n")
            .filter((line) => line.trim())
            .pop() || "";
        if (/^data\s*:/.test(lastLine)) buffer += "\n\n";
        dispatch();
        break;
      }
      buffer += decoder.decode(value, { stream: true });

      // 代理 / 标准 SSE 服务可能使用 CRLF 分帧：先归一化（跨 chunk 的
      // \r\n 在下次追加后会合成完整序列再被替换），否则 indexOf("\n\n")
      // 永远命中不了、整条流在 EOF 时被当作「连接意外中断」
      buffer = buffer.replace(/\r\n/g, "\n");
      dispatch();
    }
  } finally {
    // 超时 / 中途异常时释放底层连接（测试替身可能只有 read，没有 cancel）
    if (typeof reader.cancel === "function") reader.cancel().catch(() => {});
  }
}

function resetDeletedSessionState(restoreText) {
  state.sessionId = null;
  state.partialHistory = false;
  state.renderedStart = null;
  state.retiredThreadVisible = false;
  safeStorageRemove("session", "aligo.sessionId");
  syncSessionControls();
  setConversationTitle("新对话");
  // 会话被删除后必须清空消息区：否则旧对话继续显示，后续消息写进全新
  // 会话却追加在旧气泡下面，界面与真实历史前后不一致
  showWelcome();
  if (restoreText) {
    // 未能写入服务端的提问放回输入框（已有草稿时合并）：会话已被其它
    // 窗口 / CLI 删除时这是原文唯一的留存处，不能因输入框非空就丢弃
    restoreDraftText(restoreText);
  }
}

/* 依据本轮回合终态与落库结果设置「临时提问」标记：
   只有 done 且服务端确认已落库（persisted !== false）才清除；停止 / 失败 /
   墓碑否决（persisted=false）一律保留标记，避免出现编辑入口与刷新后无声丢失。
   幂等回放的 error 终态（上一请求已处理、响应未送达）带 persisted=true：
   该轮其实已写入历史，同样清除。 */
function applyTurnPersistence(lastUser, terminal, persisted) {
  if (!lastUser) return;
  if ((terminal === "done" || persisted === true) && persisted !== false) {
    delete lastUser.dataset.transient;
  } else {
    lastUser.dataset.transient = "1";
  }
}

function handleStreamEvent(assistant, event, userText) {
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
        safeStorageSet("session", "aligo.sessionId", event.session_id);
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
        safeStorageSet("session", "aligo.sessionId", event.session_id);
        syncSessionControls();
      }
      if (els.convTitle.textContent === "新对话") {
        setConversationTitle(clampText(state.lastUserText, 40));
      }
      assistant.persisted = event.persisted;
      mutate(() => {
        assistant.trace.finish(event.elapsed);
        assistant.setReply(event.reply, event);
      }, true);
      if (event.persisted === false) {
        // 落库被墓碑否决（会话已在其它窗口 / 终端删除）：回答没有写入
        // 历史。保留已显示的回答，但把提问标记为临时、切回新对话状态，
        // 并明确提示——否则刷新后该轮无声消失，编辑 / 重新生成也会失败
        const users = els.messages.querySelectorAll(".msg.user");
        const lastUser = users[users.length - 1];
        if (lastUser) lastUser.dataset.transient = "1";
        state.sessionId = null;
        state.partialHistory = false;
        state.renderedStart = null;
        // 可见线程属于已删除的会话：下一条消息属于新会话，发送前清空
        // 该线程；标题同步复位（否则新会话的问答挂在旧标题下）
        state.retiredThreadVisible = true;
        setConversationTitle("新对话");
        safeStorageRemove("session", "aligo.sessionId");
        syncSessionControls();
        toast("会话已在其它窗口或终端删除，本轮回答未保存");
      }
      break;
    }
    case "error": {
      const message = event.message || "处理失败";
      // 幂等回放（该轮其实已落库、只是上次响应没送达）：标记为已保存，
      // 并登记到既有停止对账队列——sendMessage 的 finally 会按
      // client_turn_id 复核历史并静默重载，否则界面停在失败态、
      // 已保存的回答始终不可见，后续消息还会接进看不到前文的会话
      if (event.idempotent_replay) {
        if (event.persisted === true) assistant.persisted = true;
        if (state.sessionId) {
          rememberStoppedTurn(
            state.sessionId,
            userText,
            (assistant.root && assistant.root.dataset.turnId) || "",
            "上一请求已处理",
          );
        }
      }
      // 会话在其它标签页 / CLI 被删除后，继续用旧 id 发送只会反复失败，
      // 墓碑过期后还会复活该会话；这里直接切回新对话状态
      if (message.includes("会话已删除")) {
        resetDeletedSessionState(userText);
        toast("会话已被删除，下一条消息将开始新对话");
      }
      mutate(() => {
        assistant.trace.fail(message);
        assistant.fail(message);
      });
      break;
    }
    default:
      break;
  }
}

/* ---------- 状态与用户 ---------- */

function storageParts(storage) {
  if (!storage) return ["本地存储"];
  const parts = [];
  const shortTerm =
    storage.short_term === "redis"
      ? storage.short_term_degraded
        ? "Redis（降级中）"
        : "Redis"
      : "本地内存";
  parts.push(shortTerm);
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
    // 耗时磁贴以计时样本数为准：SSE 长连接只计入请求数、不进耗时统计，
    // 纯流式流量下 total>0 而 duration_samples=0（后端用 0.0 哨兵表示
    // 「没测过」），按 total 判断会把「无样本」渲染成 0 ms
    const samples =
      requests.duration_samples === undefined
        ? total
        : Number(requests.duration_samples || 0);
    const tiles = [
      ["API 请求数", total.toLocaleString("zh-CN")],
      [
        "平均耗时",
        samples ? `${Math.round(Number(requests.avg_duration_ms || 0))} ms` : "—",
      ],
      [
        "最大耗时",
        samples ? `${Math.round(Number(requests.max_duration_ms || 0))} ms` : "—",
      ],
      [
        "P95 耗时",
        samples ? `${Math.round(Number(requests.p95_duration_ms || 0))} ms` : "—",
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

async function loadExpandedTrips(options = {}) {
  // 展开视图按服务端 q 过滤完整历史（此前只过滤最近 200 条的本地缓存，
  // 更早的行程会被误报为无匹配）；limit 仍是最近 200 条匹配。
  // 返回是否已渲染：refreshStatus 据此避免随后再整块渲染一遍（面板打开且
  // 展开时一次刷新会把整块面板建两次、天气请求发两次）
  // 视图已收起 / 面板已关时不再请求：防抖定时器可能在收起后才触发
  if (!state.tripsExpanded || (els.memorySheet && els.memorySheet.hidden)) {
    return false;
  }
  const userId = state.userId;
  const requestId = (tripsRequestId += 1);
  const query = state.tripsFilter.trim();
  const status = state.tripsStatusFilter;
  // 与徽标同口径：status 过滤用浏览器本地日期（服务端可能 UTC）
  const suffix =
    (query ? `&q=${encodeURIComponent(query)}` : "") +
    (status
      ? `&status=${encodeURIComponent(status)}&today=${todayISO()}`
      : "");
  try {
    const tripsData = await getJSON(
      `/api/trips?user_id=${encodeURIComponent(userId)}&limit=200&order=desc${suffix}`,
    );
    if (requestId !== tripsRequestId || userId !== state.userId) return false;
    state.tripsAll = tripsData.trips || [];
    state.tripsAllTotal = Number(tripsData.total || state.tripsAll.length);
    state.tripsLoadedFilter = query;
    state.tripsLoadedStatus = status;
  } catch (error) {
    if (requestId !== tripsRequestId || userId !== state.userId) return false;
    // 保留旧列表：接口短暂失败不应把已展示的行程清空
    if (!options.silentFailure) {
      toast(`加载行程列表失败：${error.message || error}`);
    }
  }
  if (preferenceEditActive()) {
    // 记忆面板里正在编辑偏好：整体重渲染会销毁输入框，等保存 / 取消后的刷新补齐
    state.statusStale = true;
    return false;
  }
  renderOverview();
  return true;
}

async function refreshStatus(options = {}) {
  const requestId = (statusRequestId += 1);
  const userId = state.userId;
  // 展开行程的列表已由 loadExpandedTrips 渲染过时，收尾不再重复整块渲染
  let tripsRendered = false;
  // tz：用量按浏览器时区分桶与取「近 7 天」窗口（服务端可能是 UTC，
  // 否则今天 0-8 点 / 跨时区时「今天」高亮与合计窗口整体偏移）
  let zone = "";
  try {
    zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    zone = "";
  }
  try {
    const data = await getJSON(
      // today：next_trip / 概览与状态徽标同口径（服务端可能 UTC）
      `/api/status?user_id=${encodeURIComponent(userId)}&today=${todayISO()}` +
        (zone ? `&tz=${encodeURIComponent(zone)}` : ""),
      undefined,
      30000,
    );
    // 丢弃过期响应：归档 / 切换用户后，旧请求可能晚到并覆盖新的归档计数
    if (requestId !== statusRequestId || userId !== state.userId) return;
    state.status = data;
    // 展开视图只在面板可见时重拉：面板关闭期间每条消息都重新请求 200 条
    // 行程并重建两次列表纯属浪费；重新打开面板时 openMemorySheet 会再刷新
    const sheetOpen = els.memorySheet && !els.memorySheet.hidden;
    if (state.tripsExpanded && Number(data.trip_count || 0) > 0 && sheetOpen) {
      tripsRendered = await loadExpandedTrips(options);
    } else if (!data.trip_count) {
      tripsRequestId += 1;
      state.tripsExpanded = false;
      state.tripsAll = [];
      state.tripsAllTotal = 0;
      state.tripsLoadedFilter = "";
      state.tripsLoadedStatus = "";
    }
  } catch {
    if (requestId !== statusRequestId || userId !== state.userId) return;
    if (state.status) {
      // 存储故障 / 网络抖动：保留已渲染的数据概览，不能清空成「无法获取」
      if (!options.silentFailure) {
        toast("刷新数据概览失败，已保留上次结果");
      }
      updateUserCard();
      return;
    }
    state.status = null;
  }
  updateUserCard();
  // 记忆面板里正在编辑的偏好输入框会随 renderOverview 重建被销毁：
  // 在真正渲染前（异步请求返回后）再检查一次，编辑期间保留旧视图，
  // 编辑结束的保存 / 取消路径会各补一次刷新
  if (!preferenceEditActive()) {
    if (!tripsRendered) renderOverview();
    state.statusStale = false;
  } else {
    // 本次刷新被跳过：数据已写入 state.status 但未渲染，标记过期，
    // 由编辑结束的保存 / 取消 / 未修改回车路径补齐
    state.statusStale = true;
  }
}

function preferenceEditActive() {
  const input = document.querySelector(".pref-input");
  return Boolean(input && input.getClientRects().length > 0);
}

function renderOverviewUnlessEditing() {
  // 有进行中的偏好行内编辑：整体重渲染会销毁输入框且不触发 blur/cancel
  // （已输入内容丢失），标记待刷新，等面板关闭或下次状态刷新再重建
  if (preferenceEditActive()) {
    state.statusStale = true;
    return;
  }
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
    els.exportTripsHtml.hidden = true;
    if (els.tripsDedupe) els.tripsDedupe.hidden = true;
    els.expandTrips.hidden = true;
    els.stats.innerHTML = "";
    els.usageStats.innerHTML = "";
    els.usageAgents.innerHTML = "";
    els.usageTrend.hidden = true;
    els.storageChips.innerHTML = "";
    return;
  }

  // 记忆面板关闭时，每回合结束的状态刷新只需更新侧栏可见的「即将出行」
  // 卡片与归档入口：面板里的偏好 / 行程 / 统计 / 用量都不可见，重建它们
  // （展开 200 条行程约 2000 个元素 + 700 多个监听器）纯属浪费。
  // 打开面板时 openMemorySheet 会再刷新一次，内容不会停留在旧数据
  if (els.memorySheet && els.memorySheet.hidden) {
    const sidebarTrips = state.tripsExpanded
      ? state.tripsAll
      : Array.isArray(status.trips)
        ? status.trips
        : [];
    renderUpcoming(status.next_trip || pickUpcomingTrip(sidebarTrips));
    updateArchivedToggle(Number(status.archived_count || 0));
    return;
  }

  const preferences = Object.entries(status.preferences || {}).filter(([, value]) => {
    if (Array.isArray(value)) return value.length > 0;
    return value !== null && value !== undefined && value !== "";
  });
  // /api/status 是权威快照：清掉已删除 / 已清空类型的合并基线，否则下一次
  // 「添加偏好」会用旧列表整值 PUT，把另一标签页 / CLI 已删除的条目复活
  const presentPrefTypes = new Set(preferences.map(([key]) => key));
  for (const type of Array.from(preferenceLatest.keys())) {
    if (!presentPrefTypes.has(type)) preferenceLatest.delete(type);
  }
  if (!preferences.length) {
    els.overview.innerHTML =
      '<div class="empty">还没有偏好记录。聊天时告诉我「我喜欢住汉庭」，我会记住。</div>';
  } else {
    els.overview.innerHTML = "";
    preferences.forEach(([key, value]) => {
      const row = document.createElement("div");
      row.className = "pref-row";
      row.dataset.prefType = key;
      const label = document.createElement("span");
      label.className = "pref-name";
      label.textContent = prefDisplayName(key);
      const val = document.createElement("span");
      val.className = "pref-value";
      val.textContent = formatPreferenceValue(value);

      const actions = document.createElement("span");
      actions.className = "pref-actions";
      preferenceLatest.set(key, value);
      const edit = document.createElement("button");
      edit.type = "button";
      edit.title = "编辑偏好";
      // aria-label 带上偏好名：多行时读屏要能区分操作对象（title 保持
      // 固定值，focusRowEdit 依赖 button[title="编辑偏好"] 定位）
      edit.setAttribute("aria-label", `编辑偏好 ${label.textContent}`);
      edit.innerHTML = icon("edit");
      edit.addEventListener("click", () =>
        startEditPreference(
          row,
          key,
          preferenceLatest.has(key) ? preferenceLatest.get(key) : value,
        ),
      );
      const remove = document.createElement("button");
      remove.type = "button";
      remove.title = "删除偏好";
      remove.setAttribute("aria-label", `删除偏好 ${label.textContent}`);
      remove.innerHTML = icon("trash");
      remove.addEventListener("click", () =>
        deletePreference(key, label.textContent, row),
      );
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
  // 展开视图的筛选已由服务端完成（任意空白分词、词序无关、跨字段）：
  // 这里不能再按整串 includes 二次过滤，否则多词查询会被误判为无匹配
  const filterText = state.tripsExpanded ? state.tripsFilter.trim() : "";
  // 收起视图只有最近 5 条（/api/status）且筛选控件随收起隐藏：此时不应用
  // 状态筛选，否则列表被不可见的条件裁剪（还可能空态误报、连「查看全部」
  // 入口都随条数减少消失，用户无从清除）；展开视图由服务端 ?status= 过滤
  const statusValue = state.tripsExpanded ? state.tripsStatusFilter : "";
  const trips = allTrips;
  els.tripFilterRow.hidden = !state.tripsExpanded;
  if (state.tripsExpanded) {
    // 刷新状态时保留筛选框内容与光标位置
    if (els.tripFilter.value !== state.tripsFilter) {
      els.tripFilter.value = state.tripsFilter;
    }
    if (els.tripStatusFilter && els.tripStatusFilter.value !== state.tripsStatusFilter) {
      els.tripStatusFilter.value = state.tripsStatusFilter;
    }
    const statusText =
      els.tripStatusFilter && statusValue
        ? els.tripStatusFilter.options[els.tripStatusFilter.selectedIndex]?.textContent || ""
        : "";
    const countText = filterText
      ? `匹配 ${state.tripsAllTotal || trips.length} 条${
          state.tripsAllTotal > trips.length
            ? `（已显示最近 ${trips.length} 条）`
            : ""
        }`
      : "";
    // 列表内容对应的是「加载时的筛选」，不是输入框里的新条件：刷新失败
    // 会保留旧列表（不弹 toast 时尤其隐蔽），不能再声称「匹配 N 条」
    const filtersLoaded =
      state.tripsLoadedFilter === state.tripsFilter.trim() &&
      state.tripsLoadedStatus === state.tripsStatusFilter;
    els.tripFilterCount.textContent =
      !filtersLoaded && (filterText || statusValue)
        ? "筛选加载失败，列表为上次结果"
        : statusText && !filterText
          ? statusText
          : countText;
  }
  els.trips.innerHTML = "";
  if (!trips.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent =
      filterText || statusValue ? "没有匹配的行程" : "还没有行程记录";
    els.trips.appendChild(empty);
  } else {
    // 状态接口按记录顺序（旧→新）返回，界面新→旧展示；
    // 展开列表已按 order=desc 取回最近若干条，直接使用
    const ordered = state.tripsExpanded ? [...trips] : [...trips].reverse();
    ordered.forEach((trip) => {
      // 记录最近一次渲染的服务端值：刷新在途 / 失败时再点「编辑」用它回填
      // （缺编号的行不能进缓存：undefined 键会互相覆盖）
      const tripIdText = trip.trip_id ? String(trip.trip_id) : "";
      if (tripIdText) state.tripLatest.set(tripIdText, trip);
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
      // 列表 / 状态接口把已列出的重叠截断到 20 条，真实总数在
      // conflict_count：忽略它会把「与 200 条重叠」显示成「等 20 条」
      const conflictTotal = Number(trip.conflict_count) || conflicts.length;
      if (conflicts.length) {
        const badge = document.createElement("span");
        badge.className = "trip-status conflict";
        badge.textContent = "重叠";
        const lines = conflicts.map((item) => {
          const dates = item.start_date
            ? `（${item.start_date}${item.end_date ? ` ~ ${item.end_date}` : ""}）`
            : "";
          return `${item.origin || "—"} → ${item.destination || "—"}${dates}`;
        });
        if (conflictTotal > conflicts.length) {
          lines.push(`等 ${conflictTotal} 条`);
        }
        badge.title = lines.join("\n");
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
        row.setAttribute("role", "button");
        row.tabIndex = 0;
        const openTrip = () => {
          closeMemorySheet();
          openSession(trip.session_id);
        };
        row.addEventListener("click", openTrip);
        row.addEventListener("keydown", (event) => {
          if (event.target !== row) return;
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            openTrip();
          }
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
        downloadFromApi(
          `/api/trips/${encodeURIComponent(trip.trip_id)}/calendar?user_id=${encodeURIComponent(
            state.userId,
          )}`,
          `aligo_${trip.trip_id}.ics`,
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
        // trip_id 按用户从 trip_1 起编号：迟到响应到达时用户可能已切换，
        // 新账号的同号行程 / 正在编辑的表单不能被旧账号的结果清掉
        const requestUserId = state.userId;
        try {
          await getJSON(
            `/api/trips/${encodeURIComponent(trip.trip_id)}?user_id=${encodeURIComponent(
              requestUserId,
            )}`,
            { method: "DELETE" },
          );
          if (state.userId === requestUserId) {
            toast("行程已删除");
            // 正在编辑的就是这条行程：先退出编辑态，否则「保存修改」
            // 永远 404 且表单没有取消入口
            if (state.tripEditingId === trip.trip_id) resetTripForm();
          }
          // 先移除本地行：随后刷新若失败（旧数据保留策略），不能留下
          // 一条删除按钮永久禁用的幽灵行程
          row.remove();
          await refreshStatus({ silentFailure: true });
        } catch (error) {
          if (error.status === 404) {
            if (state.userId === requestUserId) {
              toast("行程不存在，已刷新列表");
              if (state.tripEditingId === trip.trip_id) resetTripForm();
            }
            row.remove();
            await refreshStatus({ silentFailure: true });
          } else {
            if (state.userId === requestUserId) toast(error.message);
            remove.disabled = false;
          }
        }
      });

      // 历史数据可能缺 trip_id（旧格式 / 手写 JSON）：按 id 寻址的编辑、
      // 日历、删除都会 404（删除提示成功后行又复活）。禁用按钮而不是
      // 让用户反复失败
      if (!tripIdText) {
        for (const button of [edit, calendar, remove]) {
          button.disabled = true;
          button.title = "历史数据缺少行程编号，无法编辑 / 删除";
        }
      }
      actions.append(edit, calendar, remove);
      row.appendChild(actions);

      els.trips.appendChild(row);
    });
  }

  els.exportTrips.hidden = !trips.length;
  els.exportTripsCsv.hidden = !trips.length;
  els.exportTripsHtml.hidden = !trips.length;
  // 展开且带筛选时导出的是筛选子集（与列表同口径）：按钮文案必须体现，
  // 否则用户以为拿到的是全部行程
  const tripsFilterActive = Boolean(filterText) || Boolean(statusValue);
  els.exportTrips.textContent = tripsFilterActive
    ? "导出筛选结果日历"
    : "导出全部日历";
  els.exportTrips.title = tripsFilterActive
    ? "按当前关键词 / 状态筛选导出（与列表同口径）"
    : "把全部行程导出为一个日历文件";
  if (els.tripsDedupe) els.tripsDedupe.hidden = !trips.length;
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

  const travel = status.travel_summary || {};
  const stats = [
    ["历史行程", status.trip_count || 0],
    ["出行天数", Number(travel.total_days || 0)],
    ["到访城市", Number(travel.cities || 0)],
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
    storage.short_term === "redis"
      ? storage.short_term_degraded
        ? "短期记忆 · Redis（降级中）"
        : "短期记忆 · Redis"
      : "短期记忆 · 进程内",
    storage.long_term === "postgres" ? "长期记忆 · PostgreSQL" : "长期记忆 · JSON",
  ];
  chips.forEach((text) => {
    const chip = document.createElement("span");
    chip.className = "badge";
    chip.textContent = text;
    els.storageChips.appendChild(chip);
  });
  const fallback =
    (storage.short_term_configured &&
      (storage.short_term !== "redis" || Boolean(storage.short_term_degraded))) ||
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
      chip.textContent = `${agentDisplayName(agent.name)} ×${agent.count}`;
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

function normalizeISODate(value) {
  // 只接受 YYYY-M-D / YYYY/M/D 形态并补零：历史数据可能未补零（2026-1-5）
  // 或来自备份保真的斜杠写法（2026/3/12），字符串字典序会把它们排错；
  // 斜杠与后端 trip_interval 的身份口径一致，否则后端显示了状态徽标、
  // 前端却解析失败不出徽标。全角数字（２０２６-０３-１２）同样按身份
  // 口径归一：后端 normalize_identity_date 用 Python \d + int() 接受它，
  // 前端的 JS \d 只认 ASCII，不转就出现「后端算进行中、前端无徽标」
  const text = String(value || "")
    .replace(/[０-９]/g, (ch) => String.fromCharCode(ch.charCodeAt(0) - 0xfee0))
    .trim();
  const match = text.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$/);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  // 形状合法但非真实日历日（备份保真可带入 2026-02-31 / 2026-12-32）：
  // 后端 strptime 失败时按「无日期」处理（不出徽标、status 筛选与统计
  // 不计入），前端必须同口径，否则行上显示「已结束」而筛选/导出查不到。
  // 用算术判断而不是 Date 往返比较：JS 会把 0-99 年映射到 1900+，与
  // Python strptime 对 0001-0099 年的接受口径不一致
  if (year < 1 || year > 9999 || month < 1 || month > 12 || day < 1) return null;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const monthDays = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (day > monthDays[month - 1]) return null;
  return `${match[1]}-${match[2].padStart(2, "0")}-${match[3].padStart(2, "0")}`;
}

function tripStatus(trip) {
  const start = normalizeISODate(trip.start_date);
  if (!start) return null;
  const rawEnd = normalizeISODate(trip.end_date);
  // 返程缺失 / 非法 / 9999-12-31 占位时按出发当日计算：占位值与后端
  // trip_interval 同口径（模型常用「返程未定」占位，按真实区间会让行程
  // 永久「进行中」并与所有未来行程重叠）；任意非日期文本此前会让
  // 「今天 > end」恒为假，也是同样的永久进行中
  const placeholderEnd = rawEnd && rawEnd.startsWith("9999-");
  const end = rawEnd && !placeholderEnd && rawEnd >= start ? rawEnd : start;
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
      String(
        normalizeISODate(b.trip.end_date) || normalizeISODate(b.trip.start_date) || "",
      ).localeCompare(
        String(
          normalizeISODate(a.trip.end_date) ||
            normalizeISODate(a.trip.start_date) ||
            "",
        ),
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
    loadUpcomingWeather("");
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
  loadUpcomingWeather(trip.destination || "", trip.start_date || "");
}

let upcomingWeatherSeq = 0;

function formatWeatherTemp(value) {
  if (value === null || value === undefined || value === "") return "";
  const number = Number(value);
  return Number.isFinite(number) ? `${Math.round(number)}°C` : "";
}

async function loadUpcomingWeather(city, startDate) {
  const el = els.upcomingWeather;
  if (!el) return;
  const name = String(city || "").trim();
  if (!name) {
    // 让在途请求失效，否则上一个城市的天气会落到当前卡片上
    upcomingWeatherSeq += 1;
    el.textContent = "";
    el.hidden = true;
    return;
  }
  const seq = ++upcomingWeatherSeq;
  // 服务端已有 10 分钟缓存，这里不再叠加前端缓存，避免展示过期数据
  el.textContent = "";
  el.hidden = true;
  try {
    const data = await getJSON(`/api/weather?city=${encodeURIComponent(name)}`);
    if (seq !== upcomingWeatherSeq) return;
    const current = data.current || {};
    const daily = Array.isArray(data.daily) ? data.daily : [];
    // 3 天窗口内优先展示出发日预报，窗口外退回今日
    let forecast = daily.length ? daily[0] : null;
    let forecastLabel = "今日";
    // 历史行程的出发日期可能未补零（2026-11-5），预报日期恒为零填充；
    // 不归一直接全等比较会匹配不到，卡片退回今日天气
    const start = normalizeISODate(startDate);
    if (start && daily.length) {
      const match = daily.find((item) => item && item.date === start);
      if (match) {
        forecast = match;
        if (start !== todayISO()) forecastLabel = "出发日";
      }
    }
    const parts = [];
    if (current.description) parts.push(current.description);
    const currentTemp = formatWeatherTemp(current.temperature);
    if (currentTemp) parts.push(currentTemp);
    if (forecast) {
      const low = formatWeatherTemp(forecast.min);
      const high = formatWeatherTemp(forecast.max);
      if (low && high) parts.push(`${forecastLabel} ${low}~${high}`);
    }
    const text = parts.join(" · ");
    if (!text) return;
    el.textContent = data.stale ? `${text}（较早）` : text;
    el.hidden = false;
  } catch {
    if (seq === upcomingWeatherSeq) el.hidden = true;
  }
}

const LIST_PREF_TYPES = new Set(["hotel_brands", "airlines"]);

// 偏好值序列化后的字符上限：与 PUT /api/preferences 的
// _PREFERENCE_VALUE_MAX_JSON、utils.preference_values.PREFERENCE_VALUE_MAX_CHARS
// 及存储 / CLI / 导入同一口径（此前前端硬编码 500，长偏好能看不能改）
const PREFERENCE_VALUE_MAX_CHARS = 10000;

function preferenceValueTooLarge(value) {
  // 服务端按 JSON 序列化后的长度计（引号 / 转义 / 列表括号会略增），
  // 前端在输入阶段就拦下，避免直接收到英文 422。服务端用 Python 的
  // len() 按码点计数，这里必须用 Array.from 展开（String.length 是
  // UTF-16 码元数）——否则星面字符等补充平面文本会被前端误拦。
  try {
    return Array.from(JSON.stringify(value)).length > PREFERENCE_VALUE_MAX_CHARS;
  } catch {
    return false;
  }
}

function newPreferenceValue(type, text) {
  if (LIST_PREF_TYPES.has(type)) {
    const items = text
      .split(/[、，,]/)
      .map((item) => item.trim())
      .filter(Boolean);
    // 全是分隔符 / 空白时返回空数组，由调用方提示「请输入至少一个」——
    // 原样返回 "、" 会把分隔符当作偏好值写入
    return items;
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

function prefFormDirty() {
  // 添加偏好表单有未保存输入（收起面板也不会清空输入）
  return Boolean(els.prefValue && els.prefValue.value.trim());
}

async function addPreference(event) {
  event.preventDefault();
  const type = els.prefType.value;
  const text = els.prefValue.value.trim();
  if (!text) {
    toast("请输入偏好值");
    return;
  }
  if (LIST_PREF_TYPES.has(type) && !newPreferenceValue(type, text).length) {
    toast("请输入至少一个偏好项（不能只有分隔符）");
    return;
  }
  let value = newPreferenceValue(type, text);
  // 响应返回前可能已切换用户：请求体与写回都按发起时的用户（合并基线
  // 刷新也可能异步等待，必须先固定归属）
  const requestUserId = state.userId;
  if (LIST_PREF_TYPES.has(type) && Array.isArray(value)) {
    // 「添加偏好」对列表型偏好是追加语义：面板唯一的整值覆盖入口是行内
    // 「编辑」。直接 PUT 覆盖会让既有品牌 / 航司静默丢失（存储层 PUT 是
    // 整值替换，不合并）
    // 合并基线以 /api/status 权威快照为准、preferenceLatest 兜底：偏好
    // 行内编辑期间被跳过的状态刷新不会写回 preferenceLatest，先取缓存
    // 会让对话侧新增的条目被旧列表覆盖
    let snapshot = state.status && state.status.preferences;
    let existing =
      snapshot && Object.prototype.hasOwnProperty.call(snapshot, type)
        ? snapshot[type]
        : preferenceLatest.get(type);
    if (existing === undefined && !snapshot) {
      // 无权威快照（切号后 /api/status 失败或超时）：追加没有合并基线，
      // 直接 PUT 会把账号里已有的品牌 / 航司整值覆盖掉。先刷新一次再
      // 决定，仍拿不到就拒绝保存，而不是静默退化成覆盖
      await refreshStatus({ silentFailure: true });
      if (state.userId !== requestUserId) {
        toast("已切换用户，本次偏好未保存");
        return;
      }
      snapshot = state.status && state.status.preferences;
      existing =
        snapshot && Object.prototype.hasOwnProperty.call(snapshot, type)
          ? snapshot[type]
          : preferenceLatest.get(type);
      if (!snapshot) {
        toast("无法读取现有偏好，暂不能追加保存，请稍后重试");
        return;
      }
    }
    const merged = Array.isArray(existing)
      ? existing.filter(
          (item) => item !== null && String(item).trim() !== "",
        )
      : existing !== undefined &&
          existing !== null &&
          String(existing).trim() !== ""
        ? [existing]
        : [];
    for (const item of value) {
      if (!merged.includes(item)) merged.push(item);
    }
    value = merged;
  }
  if (preferenceValueTooLarge(value)) {
    toast(`偏好值过大（序列化后超过 ${PREFERENCE_VALUE_MAX_CHARS} 字符）`);
    return;
  }
  els.prefSubmit.disabled = true;
  els.prefCancel.disabled = true;
  try {
    const result = await getJSON(
      `/api/preferences/${encodeURIComponent(type)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: requestUserId,
          value,
        }),
      },
    );
    toast("偏好已保存");
    // 把本次保存值写回合并基线与状态缓存：收尾刷新失败时 preferenceLatest
    // 仍是旧列表，下一次「添加偏好」会以旧值为基线整值覆盖，刚保存的
    // 条目被静默删除（state.status 会在 renderOverview 时回写缓存，必须同步）
    if (state.userId === requestUserId) {
      const saved =
        result && result.value !== undefined ? result.value : value;
      preferenceLatest.set(type, saved);
      if (state.status && state.status.preferences) {
        state.status.preferences[type] = saved;
      }
    }
    // 请求期间用户可能已在填写下一条偏好：输入已变化时不重置表单
    if (els.prefType.value === type && els.prefValue.value.trim() === text) {
      resetPreferenceForm();
    }
    // 静默：失败时「刷新数据概览失败」不能覆盖刚显示的保存成功提示
    await refreshStatus({ silentFailure: true });
  } catch (error) {
    const status = Number((error && error.status) || 0);
    if (!status || status >= 500) {
      // 无 HTTP 状态（超时 / 断连）或 5xx：PUT 是 UPSERT，请求可能已被
      // 服务端执行。不能当作确定失败只提示错误，否则下一次「添加偏好」
      // 仍以旧基线合并，刚写入的条目会被覆盖（与行内编辑 / 删除同口径）
      toast(
        `保存结果未知：${error.message}；服务端可能已保存，请稍后刷新确认`,
      );
      if (state.userId === requestUserId) {
        preferenceLatest.set(type, value);
        if (state.status && state.status.preferences) {
          state.status.preferences[type] = value;
        }
      }
      await refreshStatus({ silentFailure: true });
    } else {
      toast(error.message);
    }
  } finally {
    els.prefSubmit.disabled = false;
    els.prefCancel.disabled = false;
  }
}

function formatPreferenceValue(value) {
  if (Array.isArray(value)) {
    return value
      .map((item) =>
        item !== null && typeof item === "object" ? JSON.stringify(item) : String(item),
      )
      .join("、");
  }
  if (value !== null && typeof value === "object") return JSON.stringify(value);
  return String(value ?? "");
}

function preferenceInputValue(value) {
  // 数组项可能是对象：直接 join 会渲染成 "[object Object]"，保存时回写的
  // 就是一串字面量。对象数组整段按 JSON 文本回填——按「、」拼接出来的
  // 文本（{"a":1}、{"b":2}）不是合法 JSON，用户改一个字段就必然提示
  // 「格式无效」，等于无法编辑
  if (Array.isArray(value)) {
    if (value.some((item) => item !== null && typeof item === "object")) {
      return JSON.stringify(value);
    }
    return formatPreferenceValue(value);
  }
  if (value !== null && typeof value === "object") return JSON.stringify(value);
  return String(value ?? "");
}

function preferenceBlurNeedsConfirm(currentValue, nextValue) {
  // 与 save() 同口径：<input> 赋值会剥离 CR/LF，含换行的原始值直接比较
  // 恒不相等（未修改也会被当成有改动）
  return nextValue !== preferenceInputValue(currentValue).replace(/[\r\n]/g, "");
}

function preferenceOutputValue(original, text) {
  // 未做修改（输入框内容与回填一致）时沿用原值：对象 / 对象数组不会被
  // 字面量化成字符串（从对象变成 JSON 字符串同样是数据损坏）
  if (text === preferenceInputValue(original)) return original;
  if (Array.isArray(original)) {
    if (original.some((item) => item !== null && typeof item === "object")) {
      // 对象数组不能按「、，,」拆分（会把对象拆成字符串碎片）：整段按
      // JSON 解析；解析失败返回 undefined 由调用方提示
      try {
        const parsed = JSON.parse(text);
        return Array.isArray(parsed) ? parsed : undefined;
      } catch {
        return undefined;
      }
    }
    // 与「添加偏好」保持一致：ASCII 逗号同样视为分隔符
    const parts = text
      .split(/[、，,]/)
      .map((item) => item.trim())
      .filter(Boolean);
    // 数值 / 布尔列表按原元素类型回写：直接 split 会把 [1,2,3] 变成
    // ["1","2","3"]（服务端契约支持数值列表原样往返，类型被静默改写）
    if (original.length && original.every((item) => typeof item === "number")) {
      if (!parts.every((part) => Number.isFinite(Number(part)))) {
        return undefined;
      }
      return parts.map((part) => Number(part));
    }
    if (original.length && original.every((item) => typeof item === "boolean")) {
      if (!parts.every((part) => /^(true|false)$/i.test(part))) {
        return undefined;
      }
      return parts.map((part) => part.toLowerCase() === "true");
    }
    return parts;
  }
  if (original !== null && typeof original === "object") {
    try {
      const parsed = JSON.parse(text);
      return parsed !== null && typeof parsed === "object" ? parsed : undefined;
    } catch {
      return undefined;
    }
  }
  const trimmed = text.trim();
  if (typeof original === "number" && trimmed !== "") {
    const number = Number(trimmed);
    if (Number.isFinite(number)) return number;
    // 1e999 / Infinity 会被 JSON 序列化成 null，服务端按空值拒绝：
    // 明确判为格式无效；非数字文本仍按文本保存（既有行为）
    if (Number.isNaN(number)) return trimmed;
    return undefined;
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
  // 与服务端序列化上限（10000 字符）及存储 / CLI / 导入口径一致：
  // 此前硬编码 500，长偏好「能看不能改」
  input.maxLength = PREFERENCE_VALUE_MAX_CHARS;
  input.setAttribute(
    "aria-label",
    `偏好值：${prefDisplayName(type)}（回车保存，Esc 取消）`,
  );
  input.value = preferenceInputValue(currentValue);
  valueEl.replaceWith(input);
  input.focus();
  input.select();
  let settled = false;

  const focusRowEdit = () => {
    // 行内编辑框被替换掉后焦点会落到 body：还原本行的「编辑」按钮。
    // 只有焦点确实丢失（body / 无）时才抢回：保存其它行触发的一次刷新
    // 可能让本行重新挂载，若无条件 focus，会把另一个正在编辑的行的输入框
    // blur 掉（blur → cancel），用户刚输入的偏好被静默丢弃
    const active = document.activeElement;
    if (active && active !== document.body) return;
    let target = row;
    if (typeof target.isConnected === "boolean" && !target.isConnected) {
      // refreshStatus 已重建概览：旧行脱离文档，按其类型找新行
      const rows = els.overview.querySelectorAll(".pref-row");
      target =
        Array.from(rows).find((item) => item.dataset.prefType === type) || row;
    }
    const editButton = target.querySelector('button[title="编辑偏好"]');
    if (editButton) editButton.focus();
  };

  const cancel = () => {
    if (settled) return;
    settled = true;
    if (input.isConnected) input.replaceWith(valueEl);
    focusRowEdit();
    // 不在 blur（mousedown 阶段）里重建列表：那会把本次点击的目标节点
    // 换掉、吞掉 click。标记为过期，面板关闭时统一补一次刷新。
    state.statusStale = true;
  };

  const save = async () => {
    if (settled) return;
    const displayValue = preferenceInputValue(currentValue);
    // <input> 赋值会剥离 CR/LF：含换行的原始值直接比较恒不相等，
    // 「未修改」保护失效，回车保存会把去换行的派生值 PUT 回去
    const expectedInput = displayValue.replace(/[\r\n]/g, "");
    if (displayValue && input.value === expectedInput) {
      // 用户没有改动：直接关闭编辑器。存储值可能含换行 / 首尾空白，而
      // <input> 赋值会做 HTML 值清洗、保存又 trim——继续 PUT 会把清洗后
      // 的派生值写回，未修改的偏好被静默改写
      settled = true;
      if (input.isConnected) input.replaceWith(valueEl);
      // 编辑期间被 refreshStatus 跳过的渲染不会自己补：未修改回车不发
      // 请求也不刷新，结束时按过期标记重建一次，否则本轮新增的偏好 /
      // 行程在面板上一直不可见
      if (state.statusStale) {
        state.statusStale = false;
        renderOverview();
      }
      focusRowEdit();
      return;
    }
    const text = input.value.trim();
    if (!text) {
      toast("偏好内容不能为空");
      input.focus();
      return;
    }
    settled = true;
    const value = preferenceOutputValue(currentValue, text);
    if (value === undefined) {
      settled = false;
      toast("偏好格式无效：对象请用合法 JSON，数值列表请填写数字");
      input.focus();
      return;
    }
    if (Array.isArray(value) && !value.length) {
      settled = false;
      toast("偏好内容不能为空");
      input.focus();
      return;
    }
    if (preferenceValueTooLarge(value)) {
      settled = false;
      toast(`偏好值过大（序列化后超过 ${PREFERENCE_VALUE_MAX_CHARS} 字符）`);
      input.focus();
      return;
    }
    // 响应返回前可能已切换用户：旧用户的偏好在切换后 resolve 不能把值
    // 写回已清空的 preferenceLatest（否则新用户再次追加列表型偏好时会
    // 把它合并进自己的保存请求）
    const requestUserId = state.userId;
    try {
      await getJSON(`/api/preferences/${encodeURIComponent(type)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: requestUserId, value }),
      });
      toast("偏好已更新");
      // 先把行内显示改成新值再撤掉编辑框：状态请求慢时不能长时间显示旧值，
      // 否则用户以为没保存成功，再次编辑会把旧值 PUT 回去。最新值同时写入
      // preferenceLatest：刷新失败时再次点「编辑」也必须回填新值
      if (state.userId === requestUserId) {
        preferenceLatest.set(type, value);
        valueEl.textContent = formatPreferenceValue(value);
        // 同步状态快照：收尾 refreshStatus 失败时 renderOverview 会用旧
        // state.status 把刚保存的值回滚，并把缓存一起覆盖回旧值
        if (state.status && state.status.preferences) {
          state.status.preferences[type] = value;
        }
      }
      if (input.isConnected) input.replaceWith(valueEl);
      // 静默：失败时「刷新数据概览失败」不能覆盖刚显示的保存成功提示
      await refreshStatus({ silentFailure: true });
      focusRowEdit();
    } catch (error) {
      const status = Number((error && error.status) || 0);
      if (!status || status >= 500) {
        // 无 HTTP 状态（超时 / 断连）或 5xx：PUT 是 UPSERT，请求可能已被
        // 服务端执行。不能当作确定失败把显示与 preferenceLatest 留在旧值，
        // 否则用户取消后再编辑会用旧值回填、下次保存把服务端已写入的新值
        // 覆盖回去（与备份恢复 / 行程去重的「结果未知」口径一致）
        settled = true;
        toast(
          `保存结果未知：${error.message}；服务端可能已保存，已按新值显示，` +
            "请稍后刷新确认",
        );
        if (state.userId === requestUserId) {
          preferenceLatest.set(type, value);
          valueEl.textContent = formatPreferenceValue(value);
          if (state.status && state.status.preferences) {
            state.status.preferences[type] = value;
          }
        }
        if (input.isConnected) input.replaceWith(valueEl);
        await refreshStatus({ silentFailure: true });
        focusRowEdit();
        return;
      }
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
    if (settled) return;
    if (!preferenceBlurNeedsConfirm(currentValue, input.value)) {
      // 未修改：静默还原
      cancel();
      return;
    }
    // 已修改：失焦不再静默丢弃草稿（点空白 / Tab / 关闭面板都会触发），
    // 先确认是否保存；取消走原有的还原路径
    if (window.confirm("偏好修改尚未保存，是否保存？")) save();
    else cancel();
  });
}

async function deletePreference(type, label, row) {
  if (!window.confirm(`删除偏好「${label}」？`)) return;
  const requestUserId = state.userId;
  try {
    await getJSON(
      `/api/preferences/${encodeURIComponent(type)}?user_id=${encodeURIComponent(
        requestUserId,
      )}`,
      { method: "DELETE" },
    );
    toast("偏好已删除");
    // 先本地移除并清掉最新值：refreshStatus 失败（静默）时不会留下已删除
    // 的行，残留行再次「编辑」保存也不会用旧值把偏好 PUT 回来。状态快照
    // 同步删除，否则刷新失败后 renderOverview 会让已删行复活、并把旧值
    // 写回「添加偏好」的合并基线。响应迟到且期间用户已切换时不能动新
    // 用户的基线（与 addPreference / startEditPreference 同口径）
    if (state.userId === requestUserId) {
      preferenceLatest.delete(type);
      if (state.status && state.status.preferences) {
        delete state.status.preferences[type];
      }
    }
    if (row && row.isConnected) row.remove();
    await refreshStatus({ silentFailure: true });
  } catch (error) {
    const status = Number((error && error.status) || 0);
    // 超时 / 断连 / 5xx（PostgreSQL 提交后断连按 503 上报）：删除可能已在
    // 服务端生效——先对账刷新并提示结果未知，避免残留行再编辑后复活
    const unknownOutcome = !status || status >= 500;
    toast(
      unknownOutcome
        ? `删除结果未知：${error.message}（已刷新，请确认偏好是否仍在）`
        : `删除失败：${error.message}`,
    );
    if (unknownOutcome) await refreshStatus({ silentFailure: true });
  }
}

async function refreshHealth() {
  try {
    const data = await getJSON("/api/health", undefined, 30000);
    state.health = data;
    const breaker = String(data.circuit_breaker || "closed");
    if (data.status === "degraded" || breaker === "open" || breaker === "half_open") {
      // 熔断打开 / 半开（或技能缺失）时后端标记 degraded：顶栏不能显示
      // 「在线」把 503 降级状态掩盖过去
      els.health.className = "health warn";
      els.healthText.textContent = "降级";
      els.health.title = `服务降级（熔断器 ${breaker}）· ${data.model || "模型"} · v${
        data.version || ""
      }`;
    } else {
      els.health.className = "health ok";
      els.healthText.textContent = "在线";
      els.health.title = `${data.model || "模型"} · ${
        data.has_api_key ? "密钥已配置" : "未配置密钥"
      } · v${data.version || ""}`;
    }
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
  // 与 theme.js 一致：内联 color-scheme 保证滚动条 / 原生控件跟随主题，
  // 运行期切换时也要同步，否则会压住样式表里的深色 color-scheme
  document.documentElement.style.colorScheme = theme;
  try {
    safeStorageSet("local", "aligo.theme", theme);
  } catch {
    /* 隐私模式下忽略 */
  }
  updateThemeButton();
}

function updateThemeButton() {
  const dark = currentTheme() === "dark";
  els.themeToggle.innerHTML = icon(dark ? "sun" : "moon");
  const label = dark ? "切换到浅色主题" : "切换到深色主题";
  els.themeToggle.title = label;
  // 固定 aria-label「切换主题」无法让读屏得知当前档位：同步可访问名与
  // pressed 状态（与 archivedToggle 同款写法）
  els.themeToggle.setAttribute("aria-label", label);
  els.themeToggle.setAttribute("aria-pressed", String(dark));
}

function isMobile() {
  return window.matchMedia("(max-width: 900px)").matches;
}

function focusSearch() {
  if (isMobile()) {
    els.app.classList.add("side-open");
    els.scrim.hidden = false;
  } else if (els.app.classList.contains("side-collapsed")) {
    // 折叠的侧栏带 inert，里面的搜索框无法聚焦：先展开再聚焦
    els.app.classList.remove("side-collapsed");
    try {
      safeStorageSet("local", "aligo.sideCollapsed", "0");
    } catch {
      /* 忽略 */
    }
  }
  syncSidebarA11y();
  els.historySearch.focus();
  els.historySearch.select();
}

function toggleSidebar() {
  let focusTarget = null;
  if (isMobile()) {
    const open = !els.app.classList.contains("side-open");
    els.app.classList.toggle("side-open", open);
    els.scrim.hidden = !open;
    if (open) {
      // 打开抽屉后焦点必须进入抽屉：否则 Tab 会操作被抽屉与遮罩挡住的顶栏。
      // 聚焦必须等到 syncSidebarA11y() 移除 inert 之后（inert 内的元素
      // 无法获得焦点，先 focus 会静默失败）；窄屏下侧栏里的折叠按钮是
      // display:none，必须挑第一个「可见」的可聚焦元素
      focusTarget =
        [...els.sidebar.querySelectorAll(
          'button:not([disabled]), input:not([disabled]), ' +
            'a[href], [tabindex]:not([tabindex="-1"])',
        )].find((el) => el.offsetParent !== null) || null;
    }
  } else {
    // 收起时焦点若正停在被收起（即将 inert）的侧栏里，浏览器会把焦点
    // 重置到 body，下一次 Tab 从文档顶部重来：先记下再在施加 inert 后
    // 还给顶栏菜单按钮（所有宽度可见，再激活即可展开）
    const hadFocusInside =
      document.activeElement instanceof HTMLElement &&
      els.sidebar.contains(document.activeElement);
    const collapsed = els.app.classList.toggle("side-collapsed");
    if (collapsed && hadFocusInside) focusTarget = els.menuBtn;
    try {
      safeStorageSet("local", "aligo.sideCollapsed", collapsed ? "1" : "0");
    } catch {
      /* 忽略 */
    }
  }
  syncSidebarA11y();
  if (focusTarget) focusTarget.focus();
}

function syncSidebarA11y() {
  if (!els.sidebar) return;
  const hidden = isMobile()
    ? !els.app.classList.contains("side-open")
    : els.app.classList.contains("side-collapsed");
  els.sidebar.toggleAttribute("inert", hidden);
  els.sidebar.setAttribute("aria-hidden", hidden ? "true" : "false");
  // 顶栏与侧栏的开关按钮同步展开状态：静态标签「展开/收起侧栏」不反映
  // 当前状态，读屏无法得知按下后会往哪个方向变化
  if (els.menuBtn) els.menuBtn.setAttribute("aria-expanded", String(!hidden));
  if (els.collapseSidebar) {
    els.collapseSidebar.setAttribute("aria-expanded", String(!hidden));
  }
}

function closeMobileSidebar() {
  const hadFocusInside =
    document.activeElement instanceof HTMLElement &&
    els.sidebar.contains(document.activeElement);
  els.app.classList.remove("side-open");
  els.scrim.hidden = true;
  syncSidebarA11y();
  // 焦点还停在刚被隐藏（inert）的抽屉里：还给顶栏菜单按钮，避免掉到 body
  if (hadFocusInside && isMobile()) els.menuBtn.focus();
}

function restoreSidebar() {
  if (!isMobile() && safeStorageGet("local", "aligo.sideCollapsed") === "1") {
    els.app.classList.add("side-collapsed");
  }
  syncSidebarA11y();
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
    // 瞬时失败（429 / 网络抖动 / 导入持锁 503）保留上一次有效状态，
    // 与片段列表「失败保留已渲染数据」一致；从未成功加载过才提示未知
    if (!els.knowledgeChip.firstElementChild) {
      els.knowledgeChip.innerHTML = "";
      const chip = document.createElement("span");
      chip.className = "badge err";
      chip.textContent = "知识库 · 状态未知";
      els.knowledgeChip.appendChild(chip);
    }
  }
}

const KB_PAGE_SIZE = 20;
let kbSearchTimer = null;
// 知识库列表 / 语义检索共用的请求序号：只渲染最后一次发起的请求结果，
// 避免先发出的子串搜索晚到后覆盖语义检索结果（或反之）
let kbListSeq = 0;
// 语义检索请求序号：被静默刷新作废的旧请求结束时不能复位按钮忙碌态
let kbSemanticSeq = 0;

function exitKbSemanticMode() {
  state.kbSemanticMode = false;
  state.kbSemanticQuery = "";
}

function knowledgeQueryParams(
  offset,
  query = state.kbQuery,
  category = state.kbCategory,
  limit = KB_PAGE_SIZE,
) {
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (query) params.set("q", query);
  if (category) params.set("category", category);
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

async function refreshKnowledgeDocuments(
  { refreshCategories = true, silentFailure = false } = {},
) {
  if (!els.kbList) return;
  if (state.kbSemanticMode) {
    // 保持在语义检索模式：删除 / 编辑 / 导入后刷新的是当前语义结果，
    // 而不是悄悄切回关键词列表。收尾刷新的 silentFailure 契约透传：
    // 429 / 写锁 503 等瞬态失败不能覆盖刚显示的成功提示
    await runSemanticSearch({ silent: true, silentFailure });
    // 语义结果不随分类筛选变化，但删除 / 编辑 / 导入会改变分类计数：
    // 之前这里直接 return，分类下拉一直显示旧计数
    if (refreshCategories) refreshKnowledgeCategories();
    return;
  }
  const seq = ++kbListSeq;
  // 查询 / 分类快照：请求发出后用户可能继续输入，响应必须按发起时的
  // 条件入账（否则新查询的结果会以旧条件渲染）
  const query = state.kbQuery;
  const category = state.kbCategory;
  // 删除 / 编辑 / 导入后的刷新保留已「加载更多」的分页深度与滚动位置：
  // 固定回第一页会丢弃 240px 列表里的其余条目并把视口跳回顶部。服务端
  // 单页上限 100，超过的部分（罕见）刷新后退回前 100 条。
  const depth = Math.min(100, Math.max(KB_PAGE_SIZE, state.kbItems.length));
  const scrollTop = els.kbList.scrollTop;
  state.kbListLoading = true;
  // 已有结果时保留列表直到新数据到达（失败不清空、不闪烁）
  if (!state.kbItems.length) {
    els.kbList.textContent = "加载中…";
  }
  // 刷新在途 / 条件与已渲染列表不一致时隐藏「加载更多」：否则点击会以
  // 新查询 + 旧游标取页，把两个查询的结果混在一起
  updateKnowledgeMore();
  // 分类计数只随库内容变化：关键词搜索的每次防抖刷新都重扫整库会白白
  // 消耗知识库限流额度（与片段列表共用 60 次/分钟桶）
  if (refreshCategories) refreshKnowledgeCategories();
  try {
    const data = await getJSON(
      `/api/knowledge/documents?${knowledgeQueryParams(0, query, category, depth)}`,
    );
    if (seq !== kbListSeq) return;
    state.kbTotal = Number(data.total || 0);
    state.kbOffset = (data.items || []).length;
    state.kbLoadedQuery = query;
    state.kbLoadedCategory = category;
    state.kbListFailed = false;
    renderKnowledgeDocuments(data.items || [], false);
    announceKbListSummary();
    // 恢复滚动位置：重建 DOM 后浏览器会把 overflow 容器归零
    els.kbList.scrollTop = scrollTop;
  } catch (error) {
    if (seq !== kbListSeq) return;
    state.kbListFailed = true;
    const hasRendered = state.kbItems.length > 0 || state.kbShowingSemantic;
    if (hasRendered) {
      // 任何失败（429 限流 / 导入写锁 503 / 导出槽位 / 网络抖动）都不清空
      // 已渲染的列表：内容仍在屏幕上，错误只作为提示；静默刷新（恢复 /
      // 导入 / 删除后的收尾）连 toast 也不弹，避免覆盖操作结果摘要
      if (!silentFailure) toast(error.message);
    } else {
      els.kbList.textContent = error.message;
      if (els.kbListStatus) els.kbListStatus.textContent = error.message;
      state.kbItems = [];
      state.kbOffset = 0;
      state.kbTotal = 0;
      state.kbLoadedQuery = null;
      state.kbLoadedCategory = null;
    }
  } finally {
    if (seq === kbListSeq) {
      state.kbListLoading = false;
      updateKnowledgeMore();
    }
  }
}

async function loadMoreKnowledgeDocuments() {
  if (!els.kbList || state.kbSemanticMode) return;
  if (state.kbListLoading) return;
  if (
    state.kbQuery !== state.kbLoadedQuery ||
    state.kbCategory !== state.kbLoadedCategory
  ) {
    // 条件与已渲染列表不一致（刷新失败保留旧结果 / 新查询未成功）：
    // 以新查询 + 旧游标取页会把两个查询的结果混在一起
    return;
  }
  // 自增序号：翻页与刷新互相作废对方响应，避免第一页被追加两次
  const seq = ++kbListSeq;
  els.kbMore.disabled = true;
  try {
    const data = await getJSON(
      `/api/knowledge/documents?${knowledgeQueryParams(
        state.kbOffset,
        state.kbLoadedQuery,
        state.kbLoadedCategory,
      )}`,
    );
    // 语义检索可能在请求期间开始：不能再把关键词行追加进语义结果
    if (seq !== kbListSeq || state.kbSemanticMode) return;
    state.kbTotal = Number(data.total || 0);
    // 游标按实际取回的条数前进（含被去重跳过的行），否则并发插入后
    // 每次仍以同一个 offset 请求，加载更多永远只返回同一窗口
    state.kbOffset += (data.items || []).length;
    renderKnowledgeDocuments(data.items || [], true);
    announceKbListSummary();
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
  const aligned =
    !state.kbListLoading &&
    state.kbQuery === state.kbLoadedQuery &&
    state.kbCategory === state.kbLoadedCategory;
  els.kbMore.hidden = !aligned || state.kbOffset >= state.kbTotal;
}

function knowledgePreview(content, query) {
  const text = String(content || "");
  const chars = Array.from(text);
  const needle = String(query || "").trim();
  if (!needle) {
    return sliceCodePoints(text, 140);
  }
  // 大小写不敏感匹配直接在原文上做：toLowerCase 可能改变字符串长度
  // （İ → i̇ 变成两个码点），用 lower 串的下标切原文会让窗口整体错位，
  // 预览里既看不到关键词也没有高亮（与服务端 _ci_find 同一口径）
  const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(escaped, "i").exec(text);
  if (!match) {
    return sliceCodePoints(text, 140);
  }
  const index = match.index;
  // UTF-16 下标换算成码点下标，避免窗口边界劈开 emoji 代理对
  const startIndex = Math.max(0, Array.from(text.slice(0, index)).length - 48);
  const end = Math.min(chars.length, startIndex + 140);
  return `${startIndex > 0 ? "…" : ""}${chars.slice(startIndex, end).join("")}${
    end < chars.length ? "…" : ""
  }`;
}

// 列表本身承载可交互按钮，不能当实时区（role=status 会把整份列表重复
// 朗读、打断输入）：只在独立的隐藏节点播报一行摘要
function announceKbListSummary() {
  if (!els.kbListStatus) return;
  const shown = state.kbItems.length;
  const total = state.kbTotal;
  let next;
  if (!shown) {
    next = state.kbQuery
      ? "没有匹配的知识片段。"
      : state.kbCategory
        ? "该分类下没有知识片段。"
        : "还没有知识片段，可以在下方添加。";
  } else {
    next = state.kbQuery
      ? `匹配 ${total} 条，已显示 ${shown} 条`
      : `共 ${total} 条，已显示 ${shown} 条`;
  }
  // 内容相同不重复播报：刷新 / 翻页频繁触发，重复写入会反复朗读
  if (els.kbListStatus.textContent !== next) {
    els.kbListStatus.textContent = next;
  }
}

function renderKnowledgeDocuments(items, append) {
  if (!append) {
    els.kbList.innerHTML = "";
    state.kbShowingSemantic = false;
    state.kbItems = [];
  }
  if (!append && !items.length) {
    const empty = document.createElement("p");
    empty.className = "kb-empty";
    empty.textContent = state.kbQuery
      ? "没有匹配的知识片段。"
      : state.kbCategory
        ? "该分类下没有知识片段。"
        : "还没有知识片段，可以在下方添加。";
    els.kbList.appendChild(empty);
    return;
  }
  // 分页期间库被其它标签页 / CLI 改动会让 offset 窗口整体后移：追加时按
  // 已渲染 id 去重，否则同一片段出现两行，删除任一行会连带删除同一条记录
  const seen = new Set(state.kbItems.map((id) => String(id)));
  for (const doc of items) {
    const docKey = String(doc.id);
    if (seen.has(docKey)) continue;
    seen.add(docKey);
    const row = document.createElement("div");
    row.className = "pref-row kb-row";
    row.dataset.id = String(doc.id);
    // 整组删除按 group 同步移除本地同组分片：收尾刷新失败时不能把
    // 已删除的其它分片留成幽灵行
    row.dataset.group = String((doc.metadata && doc.metadata.group) || "");

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
    const docParts = Number(doc.metadata && doc.metadata.parts);
    if (docParts > 1) {
      bits.push(`分片 ${doc.metadata.part || "?"}/${docParts}`);
    }
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
  const parts = Number(doc.metadata && doc.metadata.parts);
  const group = String((doc.metadata && doc.metadata.group) || "");
  if (parts > 1 && !group) {
    // 旧数据没有 group，无法定位其余分片，单条编辑会留下孤儿分片（服务端 400）
    edit.disabled = true;
    edit.title = "旧的多分片记录请整组删除后重新添加，暂不支持单条编辑";
    edit.setAttribute(
      "aria-label",
      `知识片段 ${doc.id} 属于旧的多分片记录，不支持单条编辑`,
    );
  } else if (parts > 1) {
    edit.title = `编辑整份文档（${parts} 片）`;
    edit.setAttribute(
      "aria-label",
      `编辑知识片段 ${doc.id} 所属的整份文档（共 ${parts} 片）`,
    );
  } else {
    edit.title = "编辑片段";
    edit.setAttribute("aria-label", `编辑知识片段 ${doc.id}`);
  }
  edit.innerHTML = icon("edit");
  edit.addEventListener("click", () => startEditKnowledge(doc));
  const del = document.createElement("button");
  del.type = "button";
  const delParts = Number(doc.metadata && doc.metadata.parts);
  const delGroup = delParts > 1 && Boolean(doc.metadata && doc.metadata.group);
  del.title = delGroup ? `删除整组（${delParts} 片）` : "删除片段";
  del.setAttribute(
    "aria-label",
    delGroup
      ? `删除知识片段 ${doc.id} 所在的整组分片（共 ${delParts} 片）`
      : `删除知识片段 ${doc.id}`,
  );
  del.innerHTML = icon("trash");
  del.addEventListener("click", () => removeKnowledgeDocument(doc));
  actions.append(edit, del);
  return actions;
}

async function runSemanticSearch({ silent = false, silentFailure = false } = {}) {
  const query = silent ? state.kbSemanticQuery : els.kbSearch.value.trim();
  if (!query) {
    if (!silent) toast("请输入搜索内容");
    return;
  }
  // 取消尚未触发的子串搜索刷新，并让已发出的列表请求作废
  clearTimeout(kbSearchTimer);
  const seq = ++kbListSeq;
  const semanticSeq = ++kbSemanticSeq;
  // 先进入语义模式并记录查询词：请求在途时删除 / 编辑片段触发的
  // refreshKnowledgeDocuments 会静默重跑当前语义检索，而不是启动关键词
  // 刷新（自增序列）把本次结果静默作废
  const wasSemanticMode = state.kbSemanticMode;
  const previousSemanticQuery = state.kbSemanticQuery;
  state.kbSemanticMode = true;
  state.kbSemanticQuery = query;
  els.kbSemantic.disabled = true;
  els.kbSemantic.textContent = "检索中…";
  // 语义检索期间隐藏「加载更多」：它属于关键词列表，点击结果不应掺入语义视图
  els.kbMore.hidden = true;
  els.kbMore.disabled = true;
  // 已渲染的列表先整体搬进 Fragment 暂存：检索失败（429 / 503 / 模型缺失）
  // 时放回，不能把用户正在看的列表换成一行错误文本（与关键词刷新
  // 「失败保留已渲染列表」同口径）
  let stashed = null;
  if (!silent && els.kbList.firstChild) {
    stashed = document.createDocumentFragment();
    while (els.kbList.firstChild) stashed.appendChild(els.kbList.firstChild);
  }
  // 记录本次写入的占位节点：并发关键词刷新失败时列表按「保留旧数据」
  // 策略不写 DOM，占位文案会一直留着，finally 里按节点同一性判断还原
  let placeholder = null;
  if (!silent) {
    els.kbList.textContent = "语义检索中…（首次使用需加载向量模型）";
    placeholder = els.kbList.firstChild;
  }
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
      // 收尾刷新（删除 / 编辑 / 导入 / 备份恢复之后）失败：静默保留
      // 当前语义视图，不能 toast 覆盖成功提示、也不能把用户正在看的
      // 语义结果换成关键词列表；非静默场景仍回退关键词列表，避免
      // 面板卡在旧结果
      if (silentFailure) return;
      exitKbSemanticMode();
      state.kbQuery = els.kbSearch.value.trim();
      toast(`语义结果刷新失败：${error.message}`);
      await refreshKnowledgeDocuments();
    } else if (stashed) {
      // 失败恢复暂存列表：语义模式与否沿用检索前的状态，错误改用 toast，
      // 面板不会只剩一行错误文本、失去返回关键词列表的入口
      state.kbSemanticMode = wasSemanticMode;
      state.kbSemanticQuery = previousSemanticQuery;
      // 先清掉占位文本：否则恢复的列表上方残留「语义检索中…」一行
      els.kbList.textContent = "";
      els.kbList.appendChild(stashed);
      updateKnowledgeMore();
      toast(error.message);
    } else {
      // 首次检索失败且没有已渲染内容：撤销预置的语义模式，显示错误文本
      if (!wasSemanticMode) state.kbSemanticMode = false;
      els.kbList.textContent = error.message;
    }
  } finally {
    // 并发关键词刷新失败时列表保持「保留旧数据」不写 DOM，本次语义请求
    // 又因 seq 作废直接返回：只有本次写的占位节点仍占据列表首位时才还原
    // 暂存列表，避免面板永久停在「语义检索中…」且原列表丢失
    if (stashed && placeholder && els.kbList.firstChild === placeholder) {
      els.kbList.textContent = "";
      els.kbList.appendChild(stashed);
      updateKnowledgeMore();
    }
    // 只看最新一次语义请求：被静默刷新作废的旧请求结束时，不能把仍在途
    // 的新请求的忙碌态清掉（按钮提前可点会再发第三个请求）
    if (semanticSeq === kbSemanticSeq) {
      els.kbSemantic.disabled = false;
      els.kbSemantic.textContent = "语义搜索";
      els.kbMore.disabled = false;
    }
  }
}

function setKbPendingResult(seq, text) {
  kbResultPending = { seq, text };
  if (els.kbEvalResult) {
    els.kbEvalResult.hidden = false;
    els.kbEvalResult.textContent = text;
  }
}

function clearKbPendingResult(seq) {
  if (!kbResultPending) return;
  if (seq !== null && kbResultPending.seq !== seq) return;
  if (els.kbEvalResult && els.kbEvalResult.textContent === kbResultPending.text) {
    els.kbEvalResult.hidden = true;
    els.kbEvalResult.classList.remove("warn");
  }
  kbResultPending = null;
}

async function runKbEval() {
  if (!els.kbEval) return;
  const seq = (kbActionResultSeq += 1);
  els.kbEval.disabled = true;
  els.kbEval.setAttribute("aria-busy", "true");
  els.kbEval.textContent = "评测中…";
  setKbPendingResult(seq, "检索评测中…（首次使用需加载向量模型）");
  try {
    const data = await getJSON("/api/knowledge/eval");
    const pct = (value) => `${Math.round(Number(value || 0) * 100)}%`;
    const hit5 = Number(data.hit5 || 0);
    const warning = String(data.warning || "").trim();
    // 未命中的问题（rank 为 0）直接列出前 3 个，便于定位要补充的知识
    const rows = Array.isArray(data.rows) ? data.rows : [];
    const missed = rows
      .filter((row) => row && !Number(row.rank))
      .map((row) => String(row.query || "").trim())
      .filter(Boolean);
    let missedText = "";
    if (missed.length) {
      const shown = missed
        .slice(0, 3)
        .map((query) => `「${sliceCodePoints(query, 20)}」`)
        .join("、");
      missedText = ` · 未命中 ${missed.length} 个：${shown}${missed.length > 3 ? " 等" : ""}`;
    }
    if (seq !== kbActionResultSeq) {
      clearKbPendingResult(seq);
      return;
    }
    kbResultPending = null;
    if (els.kbEvalResult) {
      els.kbEvalResult.textContent =
        `检索评测：${data.total} 个问题 · hit@1 ${pct(data.hit1)}` +
        ` · hit@3 ${pct(data.hit3)} · hit@5 ${pct(data.hit5)}` +
        ` · MRR ${Number(data.mrr || 0).toFixed(2)}` +
        (hit5 < 0.8 ? " · 低于 80% 基线" : "") +
        (data.note ? ` · ${data.note}` : "") +
        (warning ? ` · ${warning}` : "") +
        missedText;
      els.kbEvalResult.classList.toggle("warn", hit5 < 0.8 || Boolean(warning));
    }
  } catch (error) {
    if (seq === kbActionResultSeq && els.kbEvalResult) {
      kbResultPending = null;
      els.kbEvalResult.textContent = `评测失败：${error.message}`;
    } else {
      clearKbPendingResult(seq);
    }
  } finally {
    els.kbEval.disabled = false;
    els.kbEval.removeAttribute("aria-busy");
    els.kbEval.textContent = "检索评测";
  }
}

async function runKbDuplicates() {
  if (!els.kbDuplicates) return;
  const seq = (kbActionResultSeq += 1);
  els.kbDuplicates.disabled = true;
  els.kbDuplicates.setAttribute("aria-busy", "true");
  els.kbDuplicates.textContent = "检查中…";
  setKbPendingResult(seq, "正在检查重复知识片段…");
  if (els.kbEvalResult) els.kbEvalResult.classList.remove("warn");
  try {
    const data = await getJSON("/api/knowledge/duplicates?limit=20");
    if (seq !== kbActionResultSeq) {
      clearKbPendingResult(seq);
      return;
    }
    kbResultPending = null;
    const groups = Array.isArray(data.groups) ? data.groups : [];
    const totalGroups = Number(data.total_groups || 0);
    if (els.kbEvalResult) {
      if (!totalGroups) {
        els.kbEvalResult.textContent = "没有重复内容的独立知识片段";
      } else {
        const parts = groups.map((group) => {
          const ids = (Array.isArray(group.ids) ? group.ids : [])
            .map((docId) => `#${docId}`)
            .join("、");
          const preview = sliceCodePoints(String(group.preview || "").trim(), 24);
          return preview ? `${ids}（${preview}）` : ids;
        });
        const suffix =
          totalGroups > groups.length
            ? ` · 其余 ${totalGroups - groups.length} 组未显示`
            : "";
        els.kbEvalResult.textContent =
          `重复检查：${totalGroups} 组（共 ${Number(data.total_fragments || 0)} 条）：` +
          parts.join("；") +
          suffix +
          " · 确认后可在列表删除多余片段";
        els.kbEvalResult.classList.toggle("warn", true);
      }
    }
  } catch (error) {
    if (seq === kbActionResultSeq && els.kbEvalResult) {
      kbResultPending = null;
      els.kbEvalResult.textContent = `重复检查失败：${error.message}`;
    } else {
      clearKbPendingResult(seq);
    }
  } finally {
    els.kbDuplicates.disabled = false;
    els.kbDuplicates.removeAttribute("aria-busy");
    els.kbDuplicates.textContent = "查重复";
  }
}

function failedIdsText(failed) {
  return failed.map((item) => `#${item && item.id}`).join("、");
}

async function runKbDedupe() {
  if (!els.kbDedupe) return;
  const seq = (kbActionResultSeq += 1);
  // 删除请求是否已发出：超时 / 断连时服务端可能已部分或全部删除，
  // 必须区分「未执行」与「结果未知」（与导入路径同口径）
  let deleteSent = false;
  els.kbDedupe.disabled = true;
  els.kbDedupe.setAttribute("aria-busy", "true");
  els.kbDedupe.textContent = "检查中…";
  try {
    const preview = await getJSON("/api/knowledge/duplicates/cleanup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: true }),
    });
    if (seq !== kbActionResultSeq) return;
    const planned = Number(preview.planned || 0);
    if (!planned) {
      kbResultPending = null;
      if (els.kbEvalResult) {
        els.kbEvalResult.hidden = false;
        els.kbEvalResult.classList.remove("warn");
        els.kbEvalResult.textContent = "没有重复片段可清理";
      }
      return;
    }
    // 面板在扫描期间被关闭：不再弹确认，更不能在看不见的界面里执行删除
    // 只看记忆面板本身：快捷键浮层打开时 isModalOpen() 也为真，
    // 面板已关仍会弹确认并在看不见的界面里写库
    if (els.memorySheet.hidden) {
      // 面板已关：被本次动作作废的评测 / 查重复不再有收尾方，清掉进行中文案
      clearKbPendingResult(null);
      return;
    }
    const ok = window.confirm(
      `将删除 ${planned} 条重复片段（${Number(preview.groups || 0)} 组，每组保留最早一条）。继续？`,
    );
    if (!ok) {
      // 取消不覆盖之前的结果，但被本次动作作废的「进行中」文案必须清掉
      clearKbPendingResult(null);
      return;
    }
    els.kbDedupe.textContent = "清理中…";
    deleteSent = true;
    const result = await getJSON("/api/knowledge/duplicates/cleanup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: false }),
    });
    // 删除已经发生：即使期间有其它动作占用了结果条（查重复 / 评测），
    // 也必须刷新列表并提示结果，否则已删除的重复项仍显示在列表里
    const stale = seq !== kbActionResultSeq;
    const removed = Number(result.removed || 0);
    const failed = Array.isArray(result.failed) ? result.failed : [];
    const skipped = Array.isArray(result.skipped) ? result.skipped : [];
    const actualPlanned = Number(result.planned || 0);
    const drift =
      actualPlanned !== planned
        ? `（库内已有变化：计划 ${planned} → ${actualPlanned}）`
        : "";
    const summary =
      `已清理 ${removed} 条重复片段` +
      (skipped.length ? ` · ${skipped.length} 条已跳过（期间已删除或内容变化）` : "") +
      (failed.length ? ` · ${failed.length} 条删除失败：${failedIdsText(failed)}` : "") +
      drift;
    if (!stale && els.kbEvalResult) {
      els.kbEvalResult.hidden = false;
      els.kbEvalResult.classList.toggle("warn", failed.length > 0);
      els.kbEvalResult.textContent = summary;
    }
    // 清理删掉的可能是正在编辑的片段（每组保留最早一条，列表按 id 倒序
    // 展示，被删的常是最靠前 / 最常被编辑的行）：退出编辑态，否则
    // 「保存修改」只会对已删除 id 永远 404。plan 视图最多 2000 组（#929），
    // 超大清理可能截断——命中不了时退回下面的 404 分支处理
    const removedIds = new Set();
    if (Array.isArray(result.plan)) {
      for (const item of result.plan) {
        for (const id of (item && item.remove) || []) {
          removedIds.add(Number(id));
        }
      }
    }
    if (state.kbEditingId && removedIds.has(Number(state.kbEditingId))) {
      resetKnowledgeForm();
      toast("正在编辑的片段已被清理，已退出编辑");
    }
    // 本地先移除真正删除成功的行：收尾刷新失败时会保留旧 DOM，
    // 被清理的重复片段不能继续显示并可操作（与单条删除同口径）。
    // skipped / failed 的 id 实际仍在库里，不能从列表抹掉
    const skippedIds = new Set(skipped.map((item) => String(item && item.id)));
    const failedIds = new Set(failed.map((item) => String(item && item.id)));
    const dedupeDeletedIds = new Set(
      [...removedIds]
        .filter(
          (id) => !skippedIds.has(String(id)) && !failedIds.has(String(id)),
        )
        .map((id) => String(id)),
    );
    if (dedupeDeletedIds.size) {
      for (const id of dedupeDeletedIds) {
        const row = els.kbList.querySelector(`.kb-row[data-id="${id}"]`);
        if (row) row.remove();
      }
      state.kbItems = state.kbItems.filter(
        (item) => !dedupeDeletedIds.has(String(item)),
      );
      if (state.kbTotal > 0) {
        state.kbTotal = Math.max(
          state.kbItems.length,
          state.kbTotal - dedupeDeletedIds.size,
        );
      }
      const note = els.kbList.querySelector(".kb-note");
      if (note) {
        note.textContent = state.kbQuery
          ? `匹配 ${state.kbTotal} 条，已显示 ${state.kbItems.length} 条`
          : `共 ${state.kbTotal} 条，已显示 ${state.kbItems.length} 条`;
      }
    }
    // 清理同样改变片段总数：知识库状态 chip 一并刷新
    await Promise.all([
      refreshKnowledge(),
      refreshKnowledgeDocuments({ refreshCategories: true }),
    ]);
    if (stale) toast(summary);
  } catch (error) {
    // 删除已发出但没有明确响应（超时 / 断连 / 代理 5xx 无 JSON）：
    // cleanup_duplicate_contents 在写锁内不可取消，可能已部分或全部删除。
    // 429 / 503 只会在删除开始前返回（限流 / 写锁忙 / 库缺失），
    // 按普通失败处理；预览（dry_run）阶段的失败不涉及删除。
    const status = Number((error && error.status) || 0);
    const unknownOutcome =
      deleteSent && (!status || (status >= 500 && status !== 503));
    const suffix = unknownOutcome
      ? "（服务端可能已部分或全部删除，已刷新）"
      : "";
    if (seq === kbActionResultSeq && els.kbEvalResult) {
      els.kbEvalResult.hidden = false;
      els.kbEvalResult.textContent = `清理重复失败：${error.message}${suffix}`;
    } else if (unknownOutcome) {
      toast(`${error.message}${suffix}`);
    }
    if (unknownOutcome) {
      await Promise.all([
        refreshKnowledge(),
        refreshKnowledgeDocuments({
          refreshCategories: true,
          silentFailure: true,
        }),
      ]);
    }
  } finally {
    els.kbDedupe.disabled = false;
    els.kbDedupe.removeAttribute("aria-busy");
    els.kbDedupe.textContent = "清理重复";
  }
}

function renderSemanticResults(query, results) {
  els.kbList.innerHTML = "";
  state.kbShowingSemantic = true;
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
    row.dataset.group = String((doc.metadata && doc.metadata.group) || "");
    const main = document.createElement("div");
    main.className = "kb-main";
    const text = document.createElement("div");
    text.className = "kb-text";
    const content = String(doc.content || "");
    const preview = sliceCodePoints(content, 160);
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

async function removeKnowledgeDocument(doc) {
  const docId = Number(typeof doc === "object" && doc ? doc.id : doc);
  const metadata = (typeof doc === "object" && doc && doc.metadata) || {};
  const parts = Number(metadata.parts);
  const group = String(metadata.group || "").trim();
  // 只有带 group 的多分片记录能整组删除。旧版多分片（parts>1 无 group）
  // 没有「其余分片」可定位：服务端只接受单条删除——带 group=1 会 400，
  // 面板将永远删不掉（文案同样不能声称「整组已删除」）
  const groupFlag = Boolean(group);
  const wholeGroup = groupFlag;
  const legacyParts = parts > 1 && !groupFlag;
  if (state.kbSaving) {
    toast("正在保存知识片段，请稍候");
    return;
  }
  const label = wholeGroup
    ? `整组分片（#${docId}，共 ${parts} 片）`
    : legacyParts
      ? `旧版多分片记录中的这一片（#${docId}，共 ${parts} 片，其余分片需逐条删除）`
      : `知识片段 #${docId}`;
  if (!window.confirm(`删除${label}？`)) return;
  if (state.kbDeleting.has(docId)) return;
  state.kbDeleting.add(docId);
  const row = els.kbList.querySelector(`.kb-row[data-id="${docId}"]`);
  const button = row?.querySelector(
    'button[title="删除片段"], button[title^="删除整组"]',
  );
  if (button) button.disabled = true;
  let removed = false;
  // 整组删除：服务端返回实际删除的分片数（count）。收尾刷新失败时只能
  // 依赖本地同步，否则同组其它分片留成幽灵行、总数少算 N-1
  let deletedCount = 1;
  const deletedIds = new Set([String(docId)]);
  try {
    const payload = await getJSON(
      `/api/knowledge/documents/${docId}${groupFlag ? "?group=1" : ""}`,
      { method: "DELETE" },
    );
    removed = true;
    const group = String((metadata && metadata.group) || "");
    if (wholeGroup && group) {
      for (const other of els.kbList.querySelectorAll(".kb-row")) {
        if (other.dataset.group === group) deletedIds.add(String(other.dataset.id));
      }
      const count = Number(payload && payload.count);
      deletedCount =
        Number.isFinite(count) && count > 0 ? count : deletedIds.size;
    }
    toast(
      wholeGroup
        ? "已删除整组分片"
        : legacyParts
          ? "已删除该分片（旧版多分片记录的其余分片仍在，可逐条删除）"
          : "知识片段已删除",
    );
  } catch (error) {
    if (error.status === 404) {
      removed = true;
      toast("知识片段已不存在，已刷新列表");
    } else if (error.status === 400) {
      // 快照已过期（另一会话把该片段改成了多分片 / 整组结构变化）：
      // DELETE 的 400 只来自结构冲突；刷新后该行按 group/parts 渲染出
      // 「删除整组」，与编辑保存 409 分支同口径。silentFailure 避免
      // 刷新失败时的提示覆盖本条错误
      toast(`${error.message}（已刷新列表）`);
      refreshKnowledgeDocuments({ silentFailure: true });
    } else {
      toast(error.message);
    }
  } finally {
    state.kbDeleting.delete(docId);
  }
  if (removed) {
    const editingGroup = String(
      (state.kbEditingMetadata && state.kbEditingMetadata.group) || "",
    );
    const deletesEditingGroup =
      wholeGroup &&
      Boolean(editingGroup) &&
      editingGroup === String(metadata.group || "");
    // 整组删除时，正在编辑的可能是同组的另一分片：同样要退出编辑态
    if (state.kbEditingId === docId || deletesEditingGroup) resetKnowledgeForm();
    // 先移除本地行与状态：收尾刷新失败会按「保留旧数据」策略不动 DOM，
    // 不能留下删除按钮已禁用、实际已不存在的幽灵片段（与行程删除同款）
    for (const id of deletedIds) {
      const target = els.kbList.querySelector(`.kb-row[data-id="${id}"]`);
      if (target) target.remove();
    }
    state.kbItems = state.kbItems.filter(
      (item) => !deletedIds.has(String(item)),
    );
    // 计数只在刷新失败时才会用到：同步本地 total 与「共 N 条」备注
    if (state.kbTotal > 0) {
      state.kbTotal = Math.max(state.kbItems.length, state.kbTotal - deletedCount);
    }
    const note = els.kbList.querySelector(".kb-note");
    if (note) {
      note.textContent = state.kbQuery
        ? `匹配 ${state.kbTotal} 条，已显示 ${state.kbItems.length} 条`
        : `共 ${state.kbTotal} 条，已显示 ${state.kbItems.length} 条`;
    }
    await Promise.all([
      refreshKnowledge(),
      // 收尾刷新的 429 / 写锁 503 不能覆盖刚显示的成功提示（与导入同口径）
      refreshKnowledgeDocuments({ silentFailure: true }),
    ]);
    els.kbList.focus();
  } else if (button) {
    button.disabled = false;
  }
}

async function startEditKnowledge(doc) {
  if (state.kbSaving) {
    toast("正在保存知识片段，请稍候");
    return;
  }
  // 表单里已有未提交的新增 / 编辑草稿时先确认：点行内「编辑」会直接
  // 覆盖，用户会以为刚输入的内容被保存了。同一行再次点击也不例外——
  // 编辑期间列表仍可点击，此前同 id 会跳过确认直接重置 textarea。
  // 分类输入框同样是可提交的草稿：只改分类、正文未动时也要确认，
  // 否则回填会把分类修改静默覆盖
  const editingSameDoc = state.kbEditingId === doc.id;
  const originalCategory = doc.metadata?.category
    ? String(doc.metadata.category)
    : "";
  const contentDirty =
    els.kbContent.value.trim() &&
    (!editingSameDoc || els.kbContent.value !== state.kbEditBaseline);
  const categoryDirty =
    state.kbEditingId !== null &&
    els.kbCategory.value.trim() !== originalCategory;
  if (contentDirty || categoryDirty) {
    if (!window.confirm("表单里有未提交的内容，编辑将覆盖，继续？")) return;
  }
  const seq = (state.kbEditSeq += 1);
  const parts = Number(doc.metadata && doc.metadata.parts);
  const group = String((doc.metadata && doc.metadata.group) || "");
  if (parts > 1 && !group) {
    toast("旧的多分片记录请整组删除后重新添加，暂不支持单条编辑");
    return;
  }
  let content = doc.content || "";
  let groupParts = 0;
  let docSize = 0;
  if (parts > 1) {
    // 整组编辑：回填整篇文档（按 part 拼接），保存时服务端整组替换
    // /group 冷启动可能数百毫秒：记录发起时的输入与占位文案，回填前若
    // 用户已开始键入就放弃，避免响应到达后静默覆盖刚输入的内容
    const baseline = els.kbContent.value;
    const previousPlaceholder = els.kbContent.placeholder;
    els.kbContent.placeholder = "正在读取整份文档…";
    try {
      const data = await getJSON(
        `/api/knowledge/documents/${encodeURIComponent(doc.id)}/group`,
      );
      // 请求期间用户可能点了另一行的编辑或开始了保存：只认最后一次操作
      if (seq !== state.kbEditSeq || state.kbSaving) return;
      if (els.kbContent.value !== baseline) {
        els.kbContent.placeholder = previousPlaceholder;
        toast("输入内容已变化，已取消整份编辑回填");
        return;
      }
      content = String(data.content || "");
      groupParts = Number(data.parts || parts);
      // 上限按文档大小（分片长度之和）判断；回填文本含分片间分隔符，略长
      docSize = Number(data.size || content.length);
      const missingParts = Number(data.missing_parts || 0);
      if (data.partial && missingParts > 0) {
        // 残缺组（整组替换中断 / 导入缺片）：回填只有现有分片，保存会
        // 按现有内容整组重建、清除缺片标记——必须让用户知情后再进入编辑
        if (
          !window.confirm(
            `该文档当前缺少 ${missingParts} 个分片（共 ${groupParts} 片）。` +
              "保存会用现有内容重建整组并清除缺片标记，继续编辑？",
          )
        ) {
          els.kbContent.placeholder = previousPlaceholder;
          return;
        }
      }
    } catch (error) {
      if (seq !== state.kbEditSeq || state.kbSaving) return;
      els.kbContent.placeholder = previousPlaceholder;
      toast(`读取整份文档失败：${error.message}`);
      return;
    }
    if (!content) {
      els.kbContent.placeholder = previousPlaceholder;
      toast("整份文档为空，无法编辑");
      return;
    }
    if (docSize > 20000) {
      els.kbContent.placeholder = previousPlaceholder;
      toast("整份文档超过 20000 字，请拆分后维护");
      return;
    }
  }
  state.kbEditingId = doc.id;
  state.kbEditingGroupParts = groupParts;
  // 乐观并发快照：保存时随 PUT 提交，服务端写锁内比对（并发编辑 409）
  state.kbEditExpectParts = groupParts > 1 ? Number(groupParts) : 1;
  state.kbEditExpectGroup = groupParts > 1 ? String(group || "") : "";
  // 元数据快照：另一会话只改分类 / 标题时保存也要 409，而不是把对方
  // 的修改用旧快照静默改回
  state.kbEditExpectMetadata = { ...(doc.metadata || {}) };
  // 保留完整 metadata：PUT 会整体覆盖，只回填分类会丢掉 title / source 等字段
  state.kbEditingMetadata = { ...(doc.metadata || {}) };
  // 与后端 JOINED_MAX_CHARS（文档 20000 字 + 分片分隔符余量 100）对齐，
  // 超出的内容会被 PUT 以 422 拒绝
  els.kbContent.maxLength = groupParts > 1 ? 20100 : 2000;
  els.kbContent.placeholder =
    groupParts > 1
      ? `正在编辑整份文档（${groupParts} 片），保存后整组替换`
      : "新增知识片段，例如：一线城市住宿标准为每晚 600 元";
  els.kbContent.value = content;
  // textarea 赋值会把 CRLF 归一为 LF：基线记录归一后的实际值，
  // 否则含 \r\n 的片段每次点同一行「编辑」都会误弹覆盖确认
  state.kbEditBaseline = els.kbContent.value;
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
  state.kbEditSeq += 1; // 作废可能在途的整组回填请求
  state.kbEditingId = null;
  state.kbEditBaseline = "";
  state.kbEditingMetadata = {};
  state.kbEditingGroupParts = 0;
  state.kbEditExpectParts = null;
  state.kbEditExpectGroup = null;
  state.kbEditExpectMetadata = null;
  els.kbContent.maxLength = 2000;
  els.kbContent.placeholder = "新增知识片段，例如：一线城市住宿标准为每晚 600 元";
  els.kbSubmit.textContent = "添加片段";
  els.kbCancel.hidden = true;
  if (clear) {
    els.kbContent.value = "";
    els.kbCategory.value = "";
  }
}

function kbFormDirty() {
  // 知识表单是否有未保存草稿（新增：有内容即草稿；编辑：正文偏离基线
  // 或分类与载入时不同）。切换用户会 resetKnowledgeForm 清空表单，
  // 与聊天行内编辑同口径先确认，避免静默丢数据。
  const contentValue = els.kbContent.value;
  const contentDirty =
    contentValue.trim() &&
    (state.kbEditingId === null || contentValue !== state.kbEditBaseline);
  const categoryDirty =
    state.kbEditingId !== null &&
    els.kbCategory.value.trim() !==
      String((state.kbEditingMetadata && state.kbEditingMetadata.category) || "");
  return Boolean(contentDirty || categoryDirty);
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
  const editingGroupParts = state.kbEditingGroupParts;
  const metadata = editingId ? { ...state.kbEditingMetadata } : {};
  // 保存开始即作废在途的整组回填：否则慢响应可能在保存失败后覆盖表单
  state.kbEditSeq += 1;
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
    const data = await getJSON(
      editingId
        ? `/api/knowledge/documents/${encodeURIComponent(editingId)}`
        : "/api/knowledge/documents",
      {
        method: editingId ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        // 编辑时携带进入编辑时的正文 / 分片快照（乐观并发）：服务端写锁内
        // 比对，另一标签页改过则 409，而不是用旧内容静默覆盖对方修改
        body: JSON.stringify({
          content,
          metadata,
          ...(editingId
            ? {
                expect: state.kbEditBaseline,
                expect_parts: state.kbEditExpectParts,
                expect_group: state.kbEditExpectGroup,
                expect_metadata: state.kbEditExpectMetadata,
              }
            : {}),
        }),
      },
    );
    state.kbSaving = false;
    const parts = Number((data && data.parts) || 0);
    const unchanged = Boolean(data && data.unchanged);
    let message;
    if (editingId && unchanged) {
      // 服务端判定内容未修改时提前返回、不带 parts：不能按「1 片已更新」
      // 谎报，用户会以为整份文档被重建过
      message =
        editingGroupParts > 1
          ? `整份文档内容未修改（${editingGroupParts} 片）`
          : "知识片段内容未修改";
    } else if (editingId && editingGroupParts > 1) {
      // 内容缩短成单条时服务端不再返回 parts：提示按实际（1 片）展示
      const resultParts = parts > 1 ? parts : 1;
      message = `整份文档已更新（${resultParts} 片）`;
    } else if (parts > 1) {
      message = `长内容已拆分为 ${parts} 条片段（编辑任一分片会整份替换）`;
    } else {
      message = editingId ? "知识片段已更新" : "知识片段已添加";
    }
    const leftover = Array.isArray(data && data.leftover_ids) ? data.leftover_ids : [];
    if (leftover.length) {
      // 整组替换逐条删旧分片：失败项由服务端报告，提示用户稍后清理
      message += `；${leftover.length} 个旧分片未删除，请稍后重试或手动删除`;
    }
    toast(message);
    // 提交期间用户可能继续输入下一条，只清空仍是本次提交内容的输入框
    if (els.kbContent.value.trim() === content) els.kbContent.value = "";
    if (els.kbCategory.value.trim() === category) els.kbCategory.value = "";
    if (state.kbEditingId === editingId) resetKnowledgeForm({ clear: false });
    await Promise.all([
      refreshKnowledge(),
      // 收尾刷新的 429 / 写锁 503 不能覆盖刚显示的成功提示（与导入同口径）
      refreshKnowledgeDocuments({ silentFailure: true }),
    ]);
  } catch (error) {
    if (error && error.status === 409) {
      // 并发编辑冲突：刷新列表让用户看到库内最新内容；保留表单草稿
      // （不 reset），用户可对照后重新载入再保存
      toast(`${error.message}（已刷新列表，可取消后重新编辑）`);
      refreshKnowledgeDocuments({ silentFailure: true });
    } else if (error && error.status === 404 && editingId) {
      // 编辑目标已被删除（另一标签页 / 清理重复）：退出编辑态，保留草稿
      toast(`${error.message}（该片段已被删除，已退出编辑，可重新添加）`);
      resetKnowledgeForm({ clear: false });
      refreshKnowledgeDocuments({ silentFailure: true });
    } else {
      toast(error.message);
    }
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

// 服务端 /api/chat 的提问上限（Pydantic max_length=4000，按码点计）：
// 重新生成 / 编辑重发会先删除服务端上一轮再发送，超过上限的重发必然
// 422——必须在删除之前拦截，否则上一轮已被删、新提问又发不出去
// 会话详情首屏最多渲染的消息条数：备份导入允许 20 万条，整段同步逐条
// renderMarkdown + innerHTML 会冻结主线程数秒到数十秒；按窗口渲染，
// 「显示更早消息」从已取回的数据里再补一批（不再请求网络）
const HISTORY_RENDER_WINDOW = 200;
const CHAT_MESSAGE_MAX = 4000;
// 与服务端 /api/import 同一上限；实际写入按 200 条/批发送，单批远小于
// 请求体限制。旧版在文件解析前硬性拒绝 >2MB（与 CLI kb-import 不一致，
// 大知识库导出无法通过面板恢复）
const KB_IMPORT_MAX_BYTES = 50 * 1024 * 1024;
// 预览接口一次提交整份文档（2MB 请求体上限）：超出的文件跳过预览，
// 直接按条数确认后分批导入（重复项在导入时仍会被跳过）
const KB_IMPORT_PREVIEW_MAX_BYTES = 2 * 1024 * 1024;

// 服务端按请求重组多分片文档：同组的全部片段必须落在同一个请求里，
// 否则一篇文档会被拆成几段分别导入（内容截断）。同组片段在导出中
// 不一定相邻（整份编辑后首片保留原 id、其余分片是新 id），因此先按
// group 聚合为不可拆分的单元，再把单元打包进每批。纯函数便于单测。
function buildKnowledgeImportBatches(documents) {
  const MAX_BATCH = 20000;
  // 服务端单次请求体上限 2 MB：按 JSON 序列化长度的 UTF-8 上界（×3）
  // 控制每批字节数，避免「200 条 × 长内容 / 大元数据」的批次被 413
  const MAX_BATCH_BYTES = 1536 * 1024;
  const units = [];
  const groupUnits = new Map();
  for (const doc of documents) {
    const group =
      doc && doc.metadata && doc.metadata.group
        ? String(doc.metadata.group)
        : "";
    if (!group) {
      units.push([doc]);
      continue;
    }
    let unit = groupUnits.get(group);
    if (!unit) {
      unit = [];
      groupUnits.set(group, unit);
      units.push(unit);
    }
    unit.push(doc);
  }
  const batches = [];
  let batch = [];
  let batchBytes = 0;
  for (const unit of units) {
    if (unit.length > MAX_BATCH) {
      return { error: `存在超过 ${MAX_BATCH} 片的文档组，无法导入；请先拆分该文档` };
    }
    const unitBytes = JSON.stringify(unit).length * 3;
    if (unitBytes > MAX_BATCH_BYTES) {
      return { error: "存在单条（或分片组）过大的知识片段，无法分批上传；请拆分后重试" };
    }
    if (
      batch.length &&
      (batch.length >= 200 ||
        batch.length + unit.length > MAX_BATCH ||
        batchBytes + unitBytes > MAX_BATCH_BYTES)
    ) {
      batches.push(batch);
      batch = [];
      batchBytes = 0;
    }
    batch.push(...unit);
    batchBytes += unitBytes;
  }
  if (batch.length) batches.push(batch);
  return { batches };
}

/* 知识导入的跳过摘要（预览用）：布局冲突的片段并没有导入，不能只报
   「已存在」——否则用户会把备份当作已恢复。 */
function knowledgeImportSkipSummary(preview) {
  const skippedCount = Number((preview && preview.skipped) || 0);
  if (!skippedCount) return "";
  const reasonParts = [];
  const duplicateCount = Number((preview && preview.skipped_duplicate) || 0);
  const invalidCount = Number((preview && preview.skipped_invalid) || 0);
  const conflictCount = Number((preview && preview.skipped_conflict) || 0);
  if (duplicateCount) reasonParts.push(`${duplicateCount} 条已存在`);
  if (invalidCount) reasonParts.push(`${invalidCount} 条无效`);
  if (conflictCount) reasonParts.push(`${conflictCount} 条布局冲突未导入`);
  return reasonParts.length
    ? `（跳过 ${skippedCount} 条：${reasonParts.join(" / ")}）`
    : `（跳过 ${skippedCount} 条已存在 / 无效内容）`;
}

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
      toast("JSON 文件不能超过 50 MB");
      return;
    }
    let data;
    try {
      // 严格 UTF-8 解码（与备份恢复同口径）：GBK / ANSI 保存的文件此前被
      // 宽松解码成 U+FFFD 乱码后仍能导入，损坏的知识片段写进共享知识库；
      // 先解码再解析，非 UTF-8 直接提示另存
      const buffer = await file.arrayBuffer();
      const text = new TextDecoder("utf-8", { fatal: true }).decode(buffer);
      data = JSON.parse(text);
    } catch (error) {
      toast(
        error instanceof SyntaxError
          ? "无法解析 JSON 文件"
          : "JSON 文件不是 UTF-8 编码，请另存为 UTF-8 后重试",
      );
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
    // 先预览（不写库）：告诉用户将导入 / 跳过多少条，再确认
    let confirmText = `导入 ${documents.length} 条知识片段？`;
    if (file.size <= KB_IMPORT_PREVIEW_MAX_BYTES) {
      try {
      const preview = await getJSON("/api/knowledge/import/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ documents }),
      });
      // 预览已判定越过全局导出上限：导入必被服务端拒绝，先提示原因
      // （先删除旧片段再导入），不进入确认 / 分批上传
      const limitProblem = String(preview.export_limit_problem || "");
      if (limitProblem) {
        toast(limitProblem);
        return;
      }
      const wouldImport = Number(preview.would_import || 0);
      const skippedText = knowledgeImportSkipSummary(preview);
      if (!wouldImport) {
        // 全部已存在 / 无效：继续分批上传只会白耗导入队列与限流额度，
        // 每批还要全库扫描判重——直接结束并说明原因
        toast(`没有需要导入的知识片段${skippedText}`);
        return;
      }
      confirmText = `将导入 ${wouldImport} 条知识片段${skippedText}？`;
      } catch (error) {
        // 预览失败（如知识库暂不可用）不阻断：退回按文件条数确认
        confirmText = `导入 ${documents.length} 条知识片段？`;
      }
    } else {
      confirmText += "（文件较大，已跳过导入前预览；重复项会在导入时自动跳过）";
    }
    // 预览期间面板被关闭：不再弹确认，更不能在看不见的界面里写库
    // 只看记忆面板本身：快捷键浮层打开时 isModalOpen() 也为真，
    // 面板已关仍会弹确认并在看不见的界面里写库
    if (els.memorySheet.hidden) return;
    if (!window.confirm(confirmText)) return;

    els.kbImport.textContent = "导入中…";
    const planned = buildKnowledgeImportBatches(documents);
    if (planned.error) {
      toast(planned.error);
      return;
    }
    const batches = planned.batches;

    let done = 0;
    let conflictSkipped = 0;
    for (const batch of batches) {
      const result = await getJSON("/api/knowledge/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ documents: batch }),
      });
      imported += result.imported || 0;
      skipped += result.skipped || 0;
      conflictSkipped += Number(result.skipped_conflict || 0);
      done += batch.length;
      els.kbImport.textContent = `导入中… ${done}/${documents.length}`;
    }
    toast(
      `已导入 ${imported} 条知识片段${skipped ? `，跳过 ${skipped} 条` : ""}` +
        (conflictSkipped
          ? `（其中 ${conflictSkipped} 条因布局冲突未导入，请整份编辑或删除整组后重试）`
          : ""),
    );
    // 收尾刷新全部静默：429 / 写锁 503 的 toast 会覆盖刚显示的导入摘要
    await Promise.all([
      refreshKnowledge(),
      refreshKnowledgeDocuments({ silentFailure: true }),
    ]);
  } catch (error) {
    const status = Number((error && error.status) || 0);
    const queueBusy =
      status === 503 &&
      // 与服务端 /api/knowledge/import 槽位繁忙文案（server.py
      // 「已有知识库导入任务在处理…」）精确对应；本函数只请求知识库导入
      String(error.message || "").includes("导入任务在处理");
    if (imported > 0) {
      toast(`已导入 ${imported} 条后失败：${error.message}（重试会跳过已导入内容）`);
      await Promise.all([
        refreshKnowledge(),
        refreshKnowledgeDocuments({ silentFailure: true }),
      ]);
    } else if (!queueBusy && (!status || status >= 500)) {
      // 首批结果未知（客户端超时 / 代理 504 / 断连）：服务端导入在专用
      // 执行器里不可取消，该批可能已经写入——提示未知状态并刷新，
      // 不能让用户以为什么都没导入
      toast(
        `${error.message}（服务端可能已写入部分片段，已刷新列表；` +
          "重试会跳过已导入内容）",
      );
      await Promise.all([
        refreshKnowledge(),
        refreshKnowledgeDocuments({ silentFailure: true }),
      ]);
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

function shortcutFocusable() {
  const scope = els.shortcutOverlay.querySelector(".shortcut-card") || els.shortcutOverlay;
  return [
    ...scope.querySelectorAll(
      'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), ' +
        'select:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
    ),
  ].filter((el) => el.offsetParent !== null || el === document.activeElement);
}

function trapShortcutFocus(event) {
  // 陷阱必须绑在 document 上：点击卡片内非可聚焦文字会把焦点送回 body，
  // 绑在弹层上的 keydown 收不到后续 Tab，焦点会逃到 aria-modal 后面的页面
  if (event.key !== "Tab" || els.shortcutOverlay.hidden) return;
  const focusable = shortcutFocusable();
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  const inside = els.shortcutOverlay.contains(active);
  if (event.shiftKey && (active === first || !inside)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (active === last || !inside)) {
    event.preventDefault();
    first.focus();
  }
}

function openMemorySheet() {
  // 打开者是 body / 弹层内部控件时记住它没有意义：关闭时 focus 会落回
  // body 或已被隐藏的元素，回退按钮才是稳定落点（与 openShortcuts 同款）
  const active = document.activeElement;
  memorySheetOpener =
    active &&
    active !== document.body &&
    active !== document.documentElement &&
    !els.memorySheet.contains(active)
      ? active
      : els.memoryBtn || null;
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
  // 面板关闭后筛选防抖不能再补发请求（展开列表已不可见，收起时也清）
  clearTimeout(tripFilterTimer);
  if (state.statusStale) {
    state.statusStale = false;
    refreshStatus();
  }
  const opener = memorySheetOpener;
  memorySheetOpener = null;
  if (opener && opener.isConnected && !opener.hidden) {
    opener.focus();
    return;
  }
  // 触发按钮已被移除或隐藏时回到输入框，避免焦点落到 body
  els.input?.focus();
}

function isModalOpen() {
  return Boolean(
    (els.shortcutOverlay && !els.shortcutOverlay.hidden) ||
      (els.memorySheet && !els.memorySheet.hidden),
  );
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
  const raw = els.userIdInput.value.trim();
  if (!raw) {
    // 空输入不再隐式落到 web_user：共享默认账号往往已有别人的数据
    toast("请输入用户 ID（留空不会切换账号）");
    return;
  }
  const value = raw;
  if (RESERVED_DEVICE_NAMES.has(value.toLowerCase())) {
    // 设备保留名（con / nul / com1…）满足字符规则但不被服务端接受：
    // 固定文案会让用户反复改大小写仍无法成功
    toast("用户 ID 不能使用系统设备保留名（如 con / nul / com1），请换一个");
    return;
  }
  if (!isValidId(value)) {
    toast("用户 ID 只能包含中文、字母、数字、下划线和短横线");
    return;
  }
  // 长期身份是否成功落盘：失败时不能谎称「已切换用户」——重载 / 新标签页
  // 会回退到上一个账号，后续对话写进别人的记忆
  let userIdPersisted = true;
  if (value !== state.userId) {
    if (
      document.querySelector(".user-edit-input") ||
      kbFormDirty() ||
      tripFormDirty() ||
      prefFormDirty() ||
      els.input.value.trim()
    ) {
      if (
        !window.confirm(
          "当前有未保存的内容（含聊天草稿），切换用户将丢弃。继续？",
        )
      ) {
        return;
      }
    }
    sessionLoadToken += 1;
    // 作废进行中的会话加载，并清掉 loading 标志：旧加载的 finally 会因
    // token 不匹配而跳过重置，否则之后一直提示「会话正在加载」
    state.sessionLoading = false;
    state.sessionLoadingId = null;
    state.userId = value;
    els.historySearch.value = "";
    state.searchQuery = "";
    clearTimeout(searchTimer);
    // 两个存储都必须实际写入（session 供当前标签页刷新，local 供重载 /
    // 新标签页）；safeStorageSet 自身捕获异常并返回是否成功
    const savedLocal = safeStorageSet("local", "aligo.userId", value);
    safeStorageSet("session", "aligo.userId", value);
    userIdPersisted = savedLocal;
    state.sessionId = null;
    state.partialHistory = false;
    state.renderedStart = null;
    state.retiredThreadVisible = false;
    state.pendingStoppedTurns = [];
    safeStorageRemove("session", "aligo.sessionId");
    syncSessionControls();
    setConversationTitle("新对话");
    // 聊天草稿与其余表单同口径：上一用户未发送的文本不能留给新用户
    // （输入框仍可回车，会按新用户的 user_id 发出）
    els.input.value = "";
    resizeInput();
    showWelcome();
    updateUserCard();
    state.showArchived = false;
    state.archivedCount = 0;
    updateArchivedToggle(0);
    // 清掉上一个用户遗留的行程 / 知识 / 偏好编辑状态与最新值缓存，避免提交到新用户
    resetTripForm();
    state.tripLatest.clear();
    // 列表型偏好（酒店 / 航司）追加时以该缓存为「既有值」：不清理会把
    // 上一用户的品牌静默合并进新用户的保存请求
    preferenceLatest.clear();
    els.tripForm.hidden = true;
    resetKnowledgeForm();
    resetPreferenceForm();
    state.tripsExpanded = false;
    state.tripsAll = [];
    state.tripsAllTotal = 0;
    state.tripsFilter = "";
    state.tripsStatusFilter = "";
    state.tripsLoadedFilter = "";
    state.tripsLoadedStatus = "";
    // 合成状态可能因弹层关闭 / 切换用户丢失 compositionend：不清会永久
    // 跳过行程筛选刷新
    state.tripsComposing = false;
    clearTimeout(tripFilterTimer);
    // 筛选输入框与筛选行同样重置：renderOverview 在无 status 时不会渲染
    // 行程区，新用户会带着上一个用户的筛选文本和展开的筛选行
    if (els.tripFilter) els.tripFilter.value = "";
    if (els.tripFilterRow) els.tripFilterRow.hidden = true;
    // 立即清空上一个用户的列表与面板：新用户的 /api/sessions、/api/status
    // 返回前旧行仍可点击，而删除 / 编辑按当前 user_id 执行，会误伤新用户的同名记录
    closeMemorySheet();
    state.status = null;
    state.sessions = [];
    state.sessionTotal = 0;
    state.archivedMatches = 0;
    // 快照属于上一个用户的已渲染列表：不清掉的话，首屏请求失败时
    // 失败分支会按陈旧快照把上一用户的查询词 / 归档视图回滚给新用户
    state.sessionsLoadedQuery = null;
    state.sessionsLoadedArchived = null;
    els.history.innerHTML = "";
    renderOverview();
    await loadSessions(state.searchQuery, { reset: true });
    refreshStatus();
  }
  toast(
    userIdPersisted
      ? "已切换用户"
      : "已切换用户（仅当前标签页有效）：浏览器未能保存用户 ID，重新打开页面会恢复原账号",
  );
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
    // Safari/WebKit 确认输入法候选词的回车：compositionend 先于 keydown
    // 派发，此时 isComposing 已为 false，只能靠 keyCode 229（VK_PROCESS）
    // 识别；不拦截会把还没输完的提问直接发送
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage(els.input.value);
    }
  });

  els.stopBtn.addEventListener("click", () => {
    if (!state.controller) return;
    // 中止后的对账（是否已落库 → 重渲染或回填输入）在 sendMessage 的
    // AbortError 分支统一处理：超时中断与服务端已落库的情况走同一路径
    state.controller.abort();
  });

  els.kbForm.addEventListener("submit", addKnowledgeDocument);
  els.kbCancel.addEventListener("click", () => resetKnowledgeForm());
  els.kbSemantic.addEventListener("click", () => runSemanticSearch());
  if (els.kbEval) els.kbEval.addEventListener("click", () => runKbEval());
  if (els.kbDuplicates) {
    els.kbDuplicates.addEventListener("click", () => runKbDuplicates());
  }
  if (els.kbDedupe) {
    els.kbDedupe.addEventListener("click", () => runKbDedupe());
  }
  els.kbImport.addEventListener("click", () => els.kbImportFile.click());
  els.kbImportFile.addEventListener("change", importKnowledgeFile);
  const applyKnowledgeSearch = (force) => {
    clearTimeout(kbSearchTimer);
    if (state.kbComposing) {
      // 防抖定时器落在下一次合成期间：preedit 文本不是用户意图，
      // 跳过本次；compositionend 会重新调度
      return;
    }
    const wasSemanticMode = state.kbSemanticMode;
    exitKbSemanticMode();
    const query = els.kbSearch.value.trim();
    // 同值去重只用于防抖输入：刷新失败（429 / 网络错误）后的回车重试
    // 与清空重输必须能再次请求，不能被去重静默吞掉。语义模式下（DOM 是
    // 语义结果）退出时必须刷新——否则模式位已清、界面仍停在语义结果
    if (
      !force &&
      !wasSemanticMode &&
      query === state.kbQuery &&
      !state.kbListFailed
    )
      return;
    state.kbQuery = query;
    refreshKnowledgeDocuments({ refreshCategories: false });
  };
  const scheduleKnowledgeSearch = () => {
    clearTimeout(kbSearchTimer);
    // 600ms 防抖：服务端每次搜索都整库扫描，并与写入 / 导入共用
    // 60 次/分钟的限流桶——250ms 停顿即发请求会自伤式耗尽额度
    kbSearchTimer = setTimeout(() => applyKnowledgeSearch(false), 600);
  };
  els.kbSearch.addEventListener("input", (event) => {
    // 输入法合成期间不发请求：合成结束再由 compositionend 触发
    if (event.isComposing) return;
    scheduleKnowledgeSearch();
  });
  els.kbSearch.addEventListener("compositionstart", () => {
    state.kbComposing = true;
  });
  els.kbSearch.addEventListener("compositionend", () => {
    state.kbComposing = false;
    scheduleKnowledgeSearch();
  });
  els.kbSearch.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing) {
      event.preventDefault();
      applyKnowledgeSearch(true);
    }
  });
  els.kbCategoryFilter.addEventListener("change", () => {
    exitKbSemanticMode();
    state.kbCategory = els.kbCategoryFilter.value;
    // 退出语义模式后列表按 state.kbQuery 过滤：把搜索框里未提交的输入
    // 同步进来，否则用户刚输入的词被切换分类静默丢弃（与语义刷新失败
    // 的退出路径同口径）
    state.kbQuery = els.kbSearch.value.trim();
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
  els.archivedToggle.addEventListener("click", async () => {
    state.showArchived = !state.showArchived;
    updateArchivedToggle();
    // 失败时 loadSessions 内部已按「已渲染快照」回滚视图位并重渲染；
    // 这里不能再按点击前捕获的值覆盖——重叠点击时它是瞬态值，会与
    // 快照 / 列表数据分叉
    await loadSessions(state.searchQuery, { reset: true });
    updateArchivedToggle();
  });
  byId("brandHome").addEventListener("click", (event) => {
    event.preventDefault();
    newSession();
  });

  const scheduleHistorySearch = () => {
    clearTimeout(searchTimer);
    // 合成期间的 preedit 拼音不是用户意图：不调度；compositionend 会补
    if (state.historyComposing) return;
    searchTimer = setTimeout(() => {
      // 定时器触发时再复查合成状态并读当时的输入值（调度时的值可能
      // 已被继续输入的候选词取代）
      if (state.historyComposing) return;
      loadSessions(els.historySearch.value, { reset: true });
    }, 250);
  };
  els.historySearch.addEventListener("input", (event) => {
    // 输入法合成期间不发请求：合成结束再由 compositionend 触发
    if (event.isComposing) return;
    scheduleHistorySearch();
  });
  els.historySearch.addEventListener("compositionstart", () => {
    state.historyComposing = true;
  });
  els.historySearch.addEventListener("compositionend", () => {
    state.historyComposing = false;
    scheduleHistorySearch();
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
    downloadFromApi(
      `/api/export?user_id=${encodeURIComponent(state.userId)}`,
      `aligo_${exportSafeName(state.userId)}.json`,
    );
  });
  els.exportDataMd.addEventListener("click", () => {
    downloadFromApi(
      `/api/export?user_id=${encodeURIComponent(state.userId)}&format=md`,
      `aligo_${exportSafeName(state.userId)}.md`,
    );
  });
  els.exportDataKnowledge.addEventListener("click", () => {
    // 一体化备份：用户数据 + 全局知识库片段（与 CLI user-export
    // --with-knowledge 同一 JSON 形状，恢复走同一个「恢复备份」按钮）
    downloadFromApi(
      `/api/export?user_id=${encodeURIComponent(state.userId)}&with_knowledge=1`,
      `aligo_${exportSafeName(state.userId)}_with_knowledge.json`,
    );
  });
  els.exportSessions.addEventListener("click", () => {
    downloadFromApi(
      `/api/sessions/export?user_id=${encodeURIComponent(state.userId)}`,
      `aligo_sessions_${exportSafeName(state.userId)}.md`,
    );
  });
  els.exportSessionsHtml.addEventListener("click", () => {
    // 与 CLI sessions-export --html 同一渲染口径：单文件内嵌样式、可分享 / 打印
    downloadFromApi(
      `/api/sessions/export?user_id=${encodeURIComponent(state.userId)}&format=html`,
      `aligo_sessions_${exportSafeName(state.userId)}.html`,
    );
  });
  if (els.importData && els.importDataFile) {
    els.importData.addEventListener("click", () => els.importDataFile.click());
    els.importDataFile.addEventListener("change", importUserBackup);
  }

  els.themeToggle.addEventListener("click", () => {
    applyTheme(currentTheme() === "dark" ? "light" : "dark");
  });

  els.shortcutsBtn.addEventListener("click", openShortcuts);
  els.closeShortcuts.addEventListener("click", closeShortcuts);
  els.shortcutOverlay.addEventListener("click", (event) => {
    if (event.target === els.shortcutOverlay) closeShortcuts();
  });
  document.addEventListener("keydown", trapShortcutFocus);

  const warnPartialPrint = () => {
    if (
      state.partialHistory ||
      (state.renderedStart && state.renderedStart.start > 0)
    ) {
      // 面板 DOM 只渲染已显示的窗口：打印 / 导出 PDF 会静默截断更早
      // 或更新的历史（partialHistory 覆盖会话末尾未渲染的搜索命中窗口）
      toast("打印只包含当前已显示的消息；完整历史请使用「导出 HTML」");
    }
  };
  els.printChat.addEventListener("click", () => {
    if (!cancelPendingEdits()) {
      toast("正在保存修改，请稍候");
      return;
    }
    warnPartialPrint();
    window.print();
  });

  // 浏览器菜单 / Ctrl+P 不经过顶栏按钮：打印前同样退出编辑态，否则
  // 打印稿里这一轮只剩空 textarea 与保存 / 取消按钮，原提问缺失
  window.addEventListener("beforeprint", () => {
    cancelPendingEdits();
    warnPartialPrint();
  });

  els.copyLink.addEventListener("click", () => {
    if (!state.sessionId) return;
    if (!isValidSessionId(state.sessionId)) {
      toast("该会话不支持分享链接");
      return;
    }
    copyText(shareUrl(), "链接已复制");
  });

  els.exportChat.addEventListener("click", triggerExport);
  els.exportHtml.addEventListener("click", exportSessionHtml);
  els.exportTrips.addEventListener("click", () => {
    // 与列表同口径：展开视图下的关键词 / 状态筛选随导出一起提交
    // （服务端 format=ics 支持 q / status），与 CSV / HTML 导出一致
    exportTripsWithQuery("ics", "aligo_trips.ics");
  });
  const exportTripsWithQuery = (format, filename) => {
    // 与列表同口径：展开视图下的关键词与状态筛选随导出一起提交（服务端
    // 支持 q / status），否则筛选后的导出会包含被筛掉的全部行程
    const query = state.tripsExpanded ? state.tripsFilter.trim() : "";
    const status = state.tripsExpanded ? state.tripsStatusFilter || "" : "";
    // status 筛选与列表 / 徽标同一口径：带上浏览器本地日期
    const suffix =
      (query ? `&q=${encodeURIComponent(query)}` : "") +
      (status
        ? `&status=${encodeURIComponent(status)}&today=${todayISO()}`
        : "");
    const formatParam = format ? `&format=${format}` : "";
    downloadFromApi(
      `/api/trips/export?user_id=${encodeURIComponent(state.userId)}${suffix}${formatParam}`,
      filename,
    );
  };
  els.exportTripsCsv.addEventListener("click", () => {
    exportTripsWithQuery("", "aligo_trips.csv");
  });
  els.exportTripsHtml.addEventListener("click", () => {
    // 可打印 / 分享的报销台账（与 CLI trips-export --html 同一渲染口径）
    exportTripsWithQuery("html", "aligo_trips.html");
  });
  if (els.tripsDedupe) {
    els.tripsDedupe.addEventListener("click", async () => {
      if (els.tripsDedupe.disabled) return;
      const button = els.tripsDedupe;
      // 固定发起时的用户：清理期间切换用户不能把去重打到另一个账号，
      // 迟到的预览 / 结果也不能改写新账号的界面与草稿
      const requestUserId = state.userId;
      // 清理请求是否已发出：超时 / 断连 / 503 时服务端可能已部分删除
      let applySent = false;
      button.disabled = true;
      button.textContent = "检查中…";
      try {
        const preview = await getJSON("/api/trips/dedupe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_id: requestUserId, dry_run: true }),
        });
        // 只看记忆面板本身：快捷键浮层打开时 isModalOpen() 也为真，
        // 面板已关仍会弹确认并在看不见的界面里写库；期间切换用户时
        // 结果属于原账号，同样不能再弹确认
        if (els.memorySheet.hidden || state.userId !== requestUserId) return;
        const count = Number(preview.would_remove || 0);
        const ambiguous = Array.isArray(preview.ambiguous_ids)
          ? preview.ambiguous_ids
          : [];
        if (!count) {
          if (ambiguous.length) {
            // 歧义 id 可能把唯一重复组过滤掉：与接口 409 的提示口径一致，
            // 不能假装「没有重复」
            toast(
              `有 ${ambiguous.length} 个行程编号同时属于多条不同行程，无法自动清理，请运行 python cli.py trips-dedupe 人工处理`,
            );
          } else {
            toast("没有重复行程可清理");
          }
          return;
        }
        if (
          !window.confirm(
            `发现 ${count} 条重复行程（同路线 + 同起止日期），将保留最早一条。继续清理？`,
          )
        ) {
          toast("已取消清理重复行程");
          return;
        }
        applySent = true;
        const result = await getJSON("/api/trips/dedupe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          // 服务端默认 dry_run=true（只预览）：真实删除必须显式传 false
          body: JSON.stringify({ user_id: requestUserId, dry_run: false }),
        });
        const removedCount = Number(result.removed || 0);
        if (state.userId === requestUserId) {
          toast(`已清理 ${removedCount} 条重复行程`);
          // 去重可能删掉正在编辑的行程：退出编辑态，避免「保存修改」永远 404
          if (removedCount > 0 && state.tripEditingId) resetTripForm();
          await refreshStatus({ silentFailure: true });
        }
      } catch (error) {
        if (applySent) {
          // 服务端 apply_trip_dedupe 中途失败会返回 503「已删除 N 条」，
          // 客户端超时 / 断连时也可能已部分删除：刷新并退出编辑态，
          // 避免用户对已删除行反复「保存修改」得到 404
          if (state.userId === requestUserId) {
            toast(
              `清理重复行程失败：${error.message || error}（服务端可能已删除部分行程，已刷新）`,
            );
            if (state.tripEditingId) resetTripForm();
            await refreshStatus({ silentFailure: true });
          }
        } else {
          toast(`清理重复行程失败：${error.message || error}`);
        }
      } finally {
        button.disabled = false;
        button.textContent = "清理重复";
      }
    });
  }
  if (els.tripImport) {
    els.tripImport.addEventListener("click", () => {
      if (els.tripImportFile) els.tripImportFile.click();
    });
  }
  if (els.tripImportFile) {
    els.tripImportFile.addEventListener("change", async () => {
      const file = els.tripImportFile.files && els.tripImportFile.files[0];
      els.tripImportFile.value = "";
      await importTripsFromFile(file);
    });
  }
  if (els.kbExport) {
    els.kbExport.addEventListener("click", (event) => {
      event.preventDefault();
      downloadFromApi("/api/knowledge/export", "aligo-knowledge.json");
    });
  }
  els.addPrefBtn.addEventListener("click", () => {
    els.prefForm.hidden = !els.prefForm.hidden;
    if (!els.prefForm.hidden) els.prefValue.focus();
  });
  els.prefForm.addEventListener("submit", addPreference);
  els.prefCancel.addEventListener("click", resetPreferenceForm);

  els.expandTrips.addEventListener("click", toggleTripsExpanded);
  const scheduleTripFilter = () => {
    clearTimeout(tripFilterTimer);
    if (state.tripsComposing) return;
    tripFilterTimer = setTimeout(() => {
      // 防抖定时器可能跨进下一次合成：preedit 文本不是用户意图；
      // 期间收起视图 / 关闭面板时也不再请求
      if (state.tripsComposing || !state.tripsExpanded) return;
      loadExpandedTrips();
    }, 250);
  };
  els.tripFilter.addEventListener("input", (event) => {
    // 文本始终同步：renderOverview 会按 state.tripsFilter 回写输入框，
    // 合成中不同步会覆盖 IME 正在组合的内容
    state.tripsFilter = els.tripFilter.value;
    clearTimeout(tripFilterTimer);
    // 合成期间的 preedit 拼音不是用户意图：只同步文本，不调度请求
    if (event.isComposing) return;
    if (state.tripsExpanded) {
      // 关键词在服务端过滤完整历史（防抖，避免每次按键都请求）
      scheduleTripFilter();
    } else {
      renderOverview();
    }
  });
  els.tripFilter.addEventListener("compositionstart", () => {
    state.tripsComposing = true;
    clearTimeout(tripFilterTimer);
  });
  els.tripFilter.addEventListener("compositionend", () => {
    state.tripsComposing = false;
    state.tripsFilter = els.tripFilter.value;
    if (state.tripsExpanded) scheduleTripFilter();
    else renderOverview();
  });
  if (els.tripStatusFilter) {
    els.tripStatusFilter.addEventListener("change", () => {
      state.tripsStatusFilter = els.tripStatusFilter.value;
      if (state.tripsExpanded) {
        // 展开视图交给服务端按状态过滤完整历史（与 CLI --status 同口径）
        loadExpandedTrips();
      } else {
        renderOverview();
      }
    });
  }
  els.addTripBtn.addEventListener("click", () => {
    if (els.tripForm.hidden) {
      resetTripForm();
      els.tripForm.hidden = false;
      els.tripDestination.focus();
    } else {
      // 编辑 / 草稿态再次点击「添加行程」等同关闭表单：与 startEditTrip /
      // 切换用户同口径，先确认再丢弃未提交内容
      if (
        tripFormDirty() &&
        !window.confirm("表单里有未提交的内容，关闭将丢弃，继续？")
      ) {
        return;
      }
      els.tripForm.hidden = true;
      resetTripForm();
    }
  });
  els.tripForm.addEventListener("submit", addTrip);
  if (els.tripCancel) {
    els.tripCancel.addEventListener("click", () => {
      els.tripForm.hidden = true;
      resetTripForm();
    });
  }

  els.menuBtn.addEventListener("click", toggleSidebar);
  els.collapseSidebar.addEventListener("click", toggleSidebar);
  els.scrim.addEventListener("click", closeMobileSidebar);

  window.addEventListener("keydown", (event) => {
    // 输入法组合中的按键（如用 Esc 取消候选词）不触发展开 / 关闭等全局行为
    if (event.isComposing) return;
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
    // 记忆面板打开时 Escape 应先关闭面板；isModalOpen() 的提前返回会让
    // 后面的关闭逻辑永远够不到
    if (event.key === "Escape" && els.memorySheet && !els.memorySheet.hidden) {
      event.preventDefault();
      closeMemorySheet();
      return;
    }
    // 弹层打开时快捷键不穿透到背后的侧栏搜索框
    if (isModalOpen()) return;
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
    syncSidebarA11y();
  });
}

/* ---------- 启动 ---------- */

async function init() {
  welcomeTemplate = els.messages.querySelector("[data-welcome]").cloneNode(true);
  els.userIdInput.value = state.userId;
  bindEvents();
  if (state.urlSessionRejected) {
    // 深链接里的会话 ID 非法（设备名会话可用；export 等保留字拒绝）
    state.urlSessionRejected = false;
    toast("链接中的会话 ID 无效，已忽略");
  }
  if (state.rejectedUserId) {
    // 存储的用户 ID 被规则拒绝：已回退 web_user，提示用户修正或切回原账号
    const rejected = String(state.rejectedUserId);
    state.rejectedUserId = null;
    const chars = Array.from(rejected);
    const shown = chars.length > 24 ? `${chars.slice(0, 24).join("")}…` : rejected;
    toast(`原用户 ID「${shown}」不符合当前规则，已临时切换到 web_user；请在「记忆与数据」中修正`);
  }
  updateThemeButton();
  restoreSidebar();
  updateUserCard();
  resizeInput();

  const initToken = sessionLoadToken;
  const initSessionId = state.sessionId;
  await Promise.all([refreshHealth(), loadSessions(), refreshStatus()]);

  // 初始化期间用户可能已点击会话 / 新建对话：此时不要再按启动快照恢复，
  // 否则会作废用户的加载请求、甚至把正在编辑的内容重载掉
  if (state.ready || (sessionLoadToken === initToken && state.sessionId === initSessionId)) {
    if (state.sessionId) {
      await openSession(state.sessionId, { silent: true });
      // 静默恢复期间用户可能点击了别的会话：恢复被中止、或点击的会话
      // 加载失败时，sessionId 仍是启动快照 / 点击目标而界面从未展示它，
      // 必须清掉，否则下一条消息写进隐藏会话
      const displayed =
        state.renderedStart && state.renderedStart.sessionId === state.sessionId;
      if (
        (sessionLoadToken !== initToken ||
          state.sessionId !== initSessionId) &&
        !displayed
      ) {
        state.sessionId = null;
        state.partialHistory = false;
        state.renderedStart = null;
        state.retiredThreadVisible = false;
        safeStorageRemove("session", "aligo.sessionId");
        showWelcome();
      } else if (!displayed) {
        // 恢复目标本身加载失败（网络 / 503）：会话从未展示，同样不能把
        // 后续消息写进去
        state.sessionId = null;
        state.partialHistory = false;
        state.retiredThreadVisible = false;
        safeStorageRemove("session", "aligo.sessionId");
        showWelcome();
      }
    } else {
      showWelcome();
    }
  } else {
    // 用户的点击作废了启动恢复：若当前会话从未被渲染（例如点击的会话
    // 加载失败 / 是幽灵空会话），清掉旧会话 ID——后续发送不能写进界面
    // 从未展示过的隐藏会话；已成功加载并展示的会话保持不变
    const displayed =
      state.renderedStart && state.renderedStart.sessionId === state.sessionId;
    if (!displayed) {
      state.sessionId = null;
      state.partialHistory = false;
      state.renderedStart = null;
      safeStorageRemove("session", "aligo.sessionId");
      showWelcome();
    }
  }
  syncSessionControls();
  state.ready = true;
  // 初始化期间用户可能已打开记忆面板 / 在侧栏搜索框输入：此时抢焦点会把
  // 输入内容送进被弹层遮住的聊天框，回车直接变成发送
  const active = document.activeElement;
  if (!isModalOpen() && (!active || active === document.body)) {
    els.input.focus();
  }
  requestAnimationFrame(resizeInput);
}

init();
