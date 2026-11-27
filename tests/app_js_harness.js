/*
 * app.js 行为测试用的 Node 桩环境（由 tests/test_app_js_behaviors.py 调用）。
 *
 * 用法：node tests/app_js_harness.js <app.js 路径> <race|preview>
 *
 * 在 node:vm 里加载 webapp/static/app.js（去掉末尾的 init()，不启动页面），
 * 用 Proxy 假节点填好 document / window / storage，再直接调用目标函数，
 * 把观察结果以 JSON 打到 stdout 供 Python 断言。
 */
"use strict";

const fs = require("fs");
const vm = require("vm");

const appPath = process.argv[2];
const scenario = process.argv[3];

function makeNode() {
  const classes = new Set();
  const listeners = {};
  const base = {
    classList: {
      add(name) {
        classes.add(name);
      },
      remove(name) {
        classes.delete(name);
      },
      toggle(name, force) {
        const enable = force === undefined ? !classes.has(name) : Boolean(force);
        if (enable) classes.add(name);
        else classes.delete(name);
        return enable;
      },
      contains(name) {
        return classes.has(name);
      },
    },
    style: {},
    dataset: {},
    children: [],
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    append(...nodes) {
      for (const node of nodes) this.children.push(node);
    },
    insertBefore(child) {
      return child;
    },
    remove() {
      this.removed = true;
    },
    removeChild() {},
    replaceChildren() {},
    setAttribute() {},
    getAttribute() {
      return null;
    },
    removeAttribute() {},
    addEventListener(type, handler) {
      (listeners[type] ||= []).push(handler);
    },
    removeEventListener(type, handler) {
      const registered = listeners[type] || [];
      const index = registered.indexOf(handler);
      if (index >= 0) registered.splice(index, 1);
    },
    focus() {},
    blur() {},
    click() {
      for (const handler of listeners.click || []) {
        handler.call(this, {
          target: this,
          preventDefault() {},
          stopPropagation() {},
        });
      }
    },
    cloneNode() {
      return makeNode();
    },
    querySelector() {
      return makeNode();
    },
    querySelectorAll() {
      return [];
    },
    closest() {
      return null;
    },
    matches() {
      return false;
    },
    scrollIntoView() {},
    getBoundingClientRect() {
      return { top: 0, bottom: 0, left: 0, right: 0, height: 0, width: 0 };
    },
    innerHTML: "",
    textContent: "",
    value: "",
    checked: false,
    disabled: false,
    hidden: false,
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
    offsetHeight: 0,
    offsetTop: 0,
    files: [],
    accept: "",
    href: "",
    download: "",
  };
  return new Proxy(base, {
    get(target, prop) {
      if (prop in target) return target[prop];
      if (typeof prop === "string") return () => makeNode();
      return undefined;
    },
    set(target, prop, value) {
      target[prop] = value;
      return true;
    },
  });
}

function makeStorage() {
  const map = new Map();
  return {
    getItem(key) {
      return map.has(key) ? map.get(key) : null;
    },
    setItem(key, value) {
      map.set(key, String(value));
    },
    removeItem(key) {
      map.delete(key);
    },
    clear() {
      map.clear();
    },
    key() {
      return null;
    },
    get length() {
      return map.size;
    },
  };
}

function buildEnv(source) {
  const doc = {
    getElementById() {
      return makeNode();
    },
    querySelector() {
      return makeNode();
    },
    querySelectorAll() {
      return [];
    },
    createElement() {
      return makeNode();
    },
    createTextNode() {
      return makeNode();
    },
    addEventListener() {},
    removeEventListener() {},
    body: makeNode(),
    documentElement: makeNode(),
    activeElement: null,
    title: "",
  };
  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    setImmediate,
    URL,
    URLSearchParams,
    TextEncoder,
    TextDecoder,
    HTMLElement: Object,
    CSS: { escape: (value) => String(value).replace(/["\\]/g, "\\$&") },
    document: doc,
    localStorage: makeStorage(),
    sessionStorage: makeStorage(),
    navigator: {
      clipboard: { writeText: async () => {} },
      language: "zh-CN",
      userAgent: "node-harness",
    },
    location: {
      href: "http://localhost/",
      origin: "http://localhost",
      assign() {},
      reload() {},
    },
    matchMedia: () => ({
      matches: false,
      addEventListener() {},
      removeEventListener() {},
    }),
    requestAnimationFrame: () => 0,
    cancelAnimationFrame() {},
    fetch: async () => ({ ok: true, json: async () => ({}) }),
    alert() {},
    confirm: () => true,
    prompt: () => "",
    addEventListener() {},
    removeEventListener() {},
    scrollTo() {},
    innerWidth: 1280,
    innerHeight: 800,
    devicePixelRatio: 1,
    Blob: class Blob {},
    File: class File {},
    FormData: class FormData {},
    AbortController: class AbortController {
      constructor() {
        this.signal = {};
        this.aborted = false;
      }

      abort() {
        this.aborted = true;
      }
    },
    __toasts: [],
    __showWelcome: 0,
    __requests: [],
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  // 所有出站请求都排队，由场景代码逐个 resolve / reject
  vm.runInContext(
    `
    // 覆盖前保留原函数引用：个别场景需要验证真实的渲染 / 编辑入口逻辑
    globalThis.__origRenderUserNode = renderUserNode;
    globalThis.__origRefreshEditButtons = refreshEditButtons;
    getJSON = (...args) =>
      new Promise((resolve, reject) => {
        __requests.push({ args, resolve, reject });
      });
    toast = (message) => __toasts.push(String(message));
    renderHistory = () => {};
    renderUserNode = () => ({});
    renderAssistantNode = () => ({});
    addFollowUps = () => {};
    trimRegenButtons = () => {};
    refreshEditButtons = () => {};
    syncSessionControls = () => {};
    setConversationTitle = () => {};
    markActiveHistory = () => {};
    scrollToBottom = () => {};
    closeMobileSidebar = () => {};
    newSession = () => {};
    showWelcome = () => { __showWelcome += 1; };
    `,
    sandbox
  );
  return sandbox;
}

function runIn(sandbox, code) {
  return vm.runInContext(code, sandbox);
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

async function scenarioRace(source) {
  const results = {};

  // (a) 已作废的分页请求失败：不弹 toast
  {
    const env = buildEnv(source);
    const promise = runIn(env, "loadMoreSessions()");
    await tick();
    runIn(env, 'state.sessionId = "s1"; sessionsRequestId += 1;');
    env.__requests[0].reject(new Error("boom"));
    await promise;
    results.stale_load_more_toasts = runIn(env, "__toasts.slice()");
  }

  // (b) 已作废的会话加载失败：不清空当前会话、不弹提示
  {
    const env = buildEnv(source);
    const promiseA = runIn(env, 'openSession("A")');
    await tick();
    const promiseB = runIn(env, 'openSession("B")');
    await tick();
    // B 成功：当前会话切到 B
    env.__requests[1].resolve({
      messages: [{ role: "user", content: "hi" }],
    });
    await promiseB;
    // A 的迟到的失败：必须被令牌挡掉
    env.__requests[0].reject(new Error("boom"));
    await promiseA;
    results.stale_open_session = {
      sessionId: runIn(env, "state.sessionId"),
      showWelcome: runIn(env, "__showWelcome"),
      toasts: runIn(env, "__toasts.slice()"),
    };
  }

  // (c) 未作废的分页请求失败：照常提示
  {
    const env = buildEnv(source);
    const promise = runIn(env, "loadMoreSessions()");
    await tick();
    env.__requests[0].reject(new Error("boom"));
    await promise;
    results.live_load_more_toasts = runIn(env, "__toasts.slice()");
  }

  // (d) 未作废的静默恢复失败（keepOnError 未设）：清空会话并回到欢迎页
  {
    const env = buildEnv(source);
    runIn(env, 'state.sessionId = "A";');
    const promise = runIn(env, 'openSession("A", { silent: true })');
    await tick();
    env.__requests[0].reject(new Error("boom"));
    await promise;
    results.live_silent_open = {
      sessionId: runIn(env, "state.sessionId"),
      showWelcome: runIn(env, "__showWelcome"),
      toasts: runIn(env, "__toasts.slice()"),
    };
  }

  return results;
}

function scenarioPreview(source) {
  const env = buildEnv(source);
  const run = (content, query) =>
    runIn(
      env,
      `knowledgePreview(${JSON.stringify(content)}, ${JSON.stringify(query)})`
    );
  const dotted = run("İ".repeat(60) + "needle" + "X".repeat(300), "needle");
  const emoji = run("😀".repeat(100) + "needle" + "X".repeat(300), "needle");
  const hasLoneSurrogate = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(
    emoji
  );
  let metaError = null;
  try {
    run("C++ (v2)", "(");
    run("a+b", "a+b");
  } catch (error) {
    metaError = String(error && error.message ? error.message : error);
  }
  const upper = run("Some NEEDLE here", "needle");
  const plain = run("前文 ".repeat(80) + "目标关键词" + "后文 ".repeat(80), "目标关键词");
  return {
    dotted: { text: dotted, contains: dotted.includes("needle") },
    emoji: {
      contains: emoji.includes("needle"),
      lone_surrogate: hasLoneSurrogate,
    },
    meta_error: metaError,
    uppercase: { text: upper, contains: upper.includes("NEEDLE") },
    plain: { contains: plain.includes("目标关键词") },
  };
}

function scenarioRender(source) {
  const env = buildEnv(source);
  const invoke = (name, ...args) =>
    runIn(env, `${name}(...${JSON.stringify(args)})`);
  return {
    escape_html: invoke("escapeHtml", "<b>\"&'"),
    highlight_dotted: invoke("highlightSnippet", "İstanbul 出差", "İstanbul"),
    agent_display_mixed: invoke("agentDisplayName", "Trip-Budget"),
    agent_display_unknown: invoke("agentDisplayName", "custom_agent"),
    highlight_plain: invoke("highlightSnippet", "no match here", "istanbul"),
    highlight_meta: invoke("highlightSnippet", "a+b (x)", "a+b"),
    fence_info: invoke(
      "renderMarkdown",
      '```js title="a"\nconst x = "<>";\n```'
    ),
    fence_four: invoke("renderMarkdown", "````\n```inner\n````"),
    inline_code: invoke("renderMarkdown", "`**[x]**`"),
    link_paren: invoke(
      "renderMarkdown",
      "[wiki](https://en.wikipedia.org/wiki/Foo_(bar))"
    ),
    link_bold_url: invoke(
      "renderMarkdown",
      "[wiki](https://en.wikipedia.org/wiki/Foo**bar**)"
    ),
    table_escaped_pipe: invoke(
      "renderMarkdown",
      "| a\\|b | c |\n| --- | --- |\n| 1 | 2 |"
    ),
    table_code_pipe: invoke(
      "renderMarkdown",
      "| `a|b` | c |\n| --- | --- |\n| 1 | 2 |"
    ),
    table_backslash_escape: invoke(
      "renderMarkdown",
      "| C:\\temp | a\\*b |\n| --- | --- |\n| x | y |"
    ),
    link_label_code: invoke(
      "renderMarkdown",
      "[`patch`](https://example.com)"
    ),
    id_reserved_nul: invoke("isValidId", "NUL"),
    id_reserved_com1: invoke("isValidId", "com1"),
    id_ok_console: invoke("isValidId", "console"),
    id_astral_33: invoke("isValidId", "𝐀".repeat(33)),
    id_astral_52: invoke("isValidId", "𝐀".repeat(52)),
    id_astral_53: invoke("isValidId", "𝐀".repeat(53)),
    id_astral_65: invoke("isValidId", "𝐀".repeat(65)),
    session_device_name: invoke("isValidSessionId", "con"),
    session_export_reserved: invoke("isValidSessionId", "export"),
    session_bad_chars: invoke("isValidSessionId", "bad id!"),
    crlf_heading: invoke(
      "renderMarkdown",
      "## 标题" + String.fromCharCode(13, 10) + "正文",
    ),
    crlf_fence: invoke(
      "renderMarkdown",
      "```" + String.fromCharCode(13, 10) + "code" + String.fromCharCode(13, 10) + "```",
    ),
    cr_only: invoke("renderMarkdown", "## 标题" + String.fromCharCode(13) + "正文"),
    sentinel_injection: invoke(
      "renderInline",
      "x0y 0 z"
    ),
    table: invoke(
      "renderMarkdown",
      "| 日期 | 天气 |\n| --- | --- |\n| 11-06 | 小雨 |"
    ),
    heading: invoke("renderMarkdown", "# 标题"),
    slice_emoji: invoke("sliceCodePoints", "😀😀😀", 2),
    pref_object: invoke("formatPreferenceValue", { a: 1 }),
    pref_list: invoke("formatPreferenceValue", ["汉庭", { b: 2 }]),
    date_unpadded: invoke("normalizeISODate", "2026-1-5"),
    date_slash: invoke("normalizeISODate", "2026/3/12"),
    date_bad: invoke("normalizeISODate", "下周一"),
    date_invalid_calendar: invoke("normalizeISODate", "2026-02-31"),
    date_leap_valid: invoke("normalizeISODate", "2024-02-29"),
    date_full_width: invoke("normalizeISODate", "２０２６-０３-１２"),
    status_placeholder_end: invoke("tripStatus", {
      start_date: "2020-01-01",
      end_date: "9999-12-31",
    }),
  };
}

async function scenarioDuplicates(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const results = {};

  // 无重复
  let promise = run("runKbDuplicates()");
  await tick();
  env.__requests[0].resolve({
    total_groups: 0,
    total_fragments: 0,
    shown: 0,
    groups: [],
  });
  await promise;
  results.empty = run("els.kbEvalResult.textContent");

  // 有重复且超过展示上限
  promise = run("runKbDuplicates()");
  await tick();
  env.__requests[1].resolve({
    total_groups: 3,
    total_fragments: 7,
    shown: 2,
    groups: [
      { ids: [3, 7], preview: "完全相同的内容" },
      { ids: [9], preview: "" },
    ],
  });
  await promise;
  results.groups = run("els.kbEvalResult.textContent");

  // 失败
  promise = run("runKbDuplicates()");
  await tick();
  env.__requests[2].reject(new Error("网络抖动"));
  await promise;
  results.error = run("els.kbEvalResult.textContent");
  results.button = run("els.kbDuplicates.textContent");
  return results;
}

async function scenarioEval(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const results = {};

  // 达标：hit@5 = 100%，但有 1 个未命中问题
  let promise = run("runKbEval()");
  await tick();
  env.__requests[0].resolve({
    total: 30,
    hit1: 0.64,
    hit3: 0.93,
    hit5: 1.0,
    mrr: 0.8,
    rows: [
      { query: "住宿标准", rank: 0 },
      { query: "报销时限", rank: 2 },
    ],
  });
  await promise;
  results.ok_text = run("els.kbEvalResult.textContent");
  results.ok_warn = run("els.kbEvalResult.classList.contains('warn')");
  results.ok_button = run("els.kbEval.textContent");

  // 低于基线 + 告警：标黄并带提示
  promise = run("runKbEval()");
  await tick();
  env.__requests[1].resolve({
    total: 30,
    hit1: 0.3,
    hit3: 0.5,
    hit5: 0.6,
    mrr: 0.4,
    rows: [],
    warning: "评测期间共享锁不可用（可能存在并发评测），结果仅供参考",
  });
  await promise;
  results.low_text = run("els.kbEvalResult.textContent");
  results.low_warn = run("els.kbEvalResult.classList.contains('warn')");

  // 失败
  promise = run("runKbEval()");
  await tick();
  env.__requests[2].reject(new Error("服务暂不可用"));
  await promise;
  results.error = run("els.kbEvalResult.textContent");
  results.error_button = run("els.kbEval.textContent");
  results.error_busy = run("els.kbEval.getAttribute('aria-busy')");
  return results;
}

async function scenarioDedupe(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run("__kbRefreshes = 0; refreshKnowledgeDocuments = async () => { __kbRefreshes += 1; };");
  run("__kbChipRefreshes = 0; refreshKnowledge = async () => { __kbChipRefreshes += 1; };");
  run(
    "__confirmResult = true; __confirmCalls = 0; confirm = () => { __confirmCalls += 1; return __confirmResult; };"
  );
  const results = {};

  // 没有可清理的重复：不弹确认、不发删除请求
  let promise = run("runKbDedupe()");
  await tick();
  env.__requests[0].resolve({ planned: 0, groups: 0, removed: 0, failed: [] });
  await promise;
  results.empty = {
    text: run("els.kbEvalResult.textContent"),
    requests: run("__requests.length"),
    confirm_calls: run("__confirmCalls"),
  };

  // 取消确认：只发 dry-run，不执行删除，也不覆盖之前的结果
  run("__confirmResult = false; __confirmCalls = 0;");
  promise = run("runKbDedupe()");
  await tick();
  env.__requests[1].resolve({ planned: 2, groups: 1, removed: 0, failed: [] });
  await promise;
  results.cancel = {
    text: run("els.kbEvalResult.textContent"),
    requests: run("__requests.length"),
    confirm_calls: run("__confirmCalls"),
  };

  // 确认执行：dry-run + 删除；期间库内变化（计划 2 → 3）+ 跳过 + 失败；
  // 正在编辑的片段被清理：必须退出编辑态（否则「保存修改」永远 404）
  run(`
    __confirmResult = true; __confirmCalls = 0;
    state.kbEditingId = 12;
    state.kbEditBaseline = "待编辑";
    els.kbSubmit.textContent = "保存修改";
  `);
  promise = run("runKbDedupe()");
  await tick();
  env.__requests[2].resolve({ planned: 2, groups: 1, removed: 0, failed: [] });
  await tick();
  env.__requests[3].resolve({
    planned: 3,
    groups: 1,
    removed: 1,
    skipped: [{ id: 12, reason: "片段不存在或已被删除" }],
    failed: [{ id: 11, error: "x" }],
    plan: [{ keep: 11, remove: [12] }],
  });
  await promise;
  results.applied = {
    text: run("els.kbEvalResult.textContent"),
    warn: run("els.kbEvalResult.classList.contains('warn')"),
    hidden: run("els.kbEvalResult.hidden"),
    requests: run("__requests.length"),
    button: run("els.kbDedupe.textContent"),
    editing: run("state.kbEditingId"),
    submit: run("els.kbSubmit.textContent"),
    chip_refreshes: run("__kbChipRefreshes"),
  };

  // 删除期间用户点了「查重复 / 评测」：结果条被新动作占用，但删除已发生，
  // 列表仍必须刷新并用 toast 告知结果（否则已删除项还显示在列表里）
  run("__confirmResult = true; __confirmCalls = 0; __refreshesBefore = __kbRefreshes;");
  promise = run("runKbDedupe()");
  await tick();
  env.__requests[4].resolve({ planned: 1, groups: 1, removed: 0, failed: [] });
  await tick();
  run("kbActionResultSeq += 1;");
  env.__requests[5].resolve({
    planned: 1,
    groups: 1,
    removed: 1,
    skipped: [],
    failed: [],
  });
  await promise;
  results.stale = {
    refreshes: run("__kbRefreshes - __refreshesBefore"),
    toasts: run("__toasts.slice()"),
  };

  // 接口失败：提示并复位按钮
  promise = run("runKbDedupe()");
  await tick();
  env.__requests[6].reject(new Error("boom"));
  await promise;
  results.error = {
    text: run("els.kbEvalResult.textContent"),
    button: run("els.kbDedupe.textContent"),
    disabled: run("els.kbDedupe.disabled"),
  };
  return results;
}

async function scenarioKbPanelFixes(source) {
  // 知识库面板：刷新失败保留列表 / 语义同值输入退出模式 / 预览零导入 /
  // 只改分类再次点编辑要确认
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const results = {};
  run(`
    const makeField = () => ({
      value: "",
      hidden: false,
      textContent: "",
      placeholder: "",
      handlers: {},
      addEventListener(type, fn) {
        (this.handlers[type] = this.handlers[type] || []).push(fn);
      },
      dispatch(type, event) {
        (this.handlers[type] || []).forEach((fn) => fn(event || {}));
      },
      focus() {},
      setSelectionRange() {},
      querySelectorAll: () => [],
    });
    els.memorySheet.hidden = false;
    els.kbSearch = makeField();
    els.kbContent = makeField();
    els.kbCategory = makeField();
    els.kbSubmit = makeField();
    els.kbCancel = makeField();
    els.kbList = {
      textContent: "",
      innerHTML: "",
      scrollTop: 140,
      appendChild() {},
      focus() {},
      querySelector: () => null,
      querySelectorAll: () => [],
    };
    els.kbMore = {
      hidden: true,
      disabled: false,
      addEventListener() {},
      focus() {},
    };
    state.userId = "u1";
    state.kbItems = [11, 12];
    state.kbOffset = 2;
    state.kbTotal = 2;
    state.kbLoadedQuery = "";
    state.kbLoadedCategory = "";
    renderKnowledgeDocuments = (items, append) => {
      state.kbItems = append ? state.kbItems.concat(items) : items.slice();
      if (!append) state.kbShowingSemantic = false;
    };
    refreshKnowledgeCategories = () => {};
    refreshKnowledge = async () => {};
    globalThis.__kbChipRefreshes = 0;
    setTimeout = (cb) => {
      try {
        cb();
      } catch (_) {
        // 调度内的异常由用例自身的状态断言暴露
      }
      return 0;
    };
    bindEvents();
  `);

  // 1) 刷新遇到 503（导入写锁）：保留已渲染列表；静默刷新不弹 toast
  run(`
    __toasts.length = 0;
    getJSON = async () => {
      const error = new Error("知识库正在写入（导入进行中），请稍后重试");
      error.status = 503;
      throw error;
    };
  `);
  await run("refreshKnowledgeDocuments({ silentFailure: true })");
  results.refresh_keep = run(
    "({ items: state.kbItems.slice(), text: els.kbList.textContent, toasts: __toasts.slice() })",
  );
  await run("refreshKnowledgeDocuments()");
  results.refresh_toast = run("__toasts.slice()");

  // 2) 语义模式下输入同值：退出语义模式并刷新关键词列表（不能停在语义 DOM）
  run(`
    __toasts.length = 0;
    state.kbSemanticMode = true;
    state.kbSemanticQuery = "foo";
    state.kbQuery = "foo";
    state.kbShowingSemantic = true;
    els.kbSearch.value = "foo";
    globalThis.__kbRequests = [];
    getJSON = async (url) => {
      __kbRequests.push(url);
      return { total: 0, items: [] };
    };
  `);
  run('els.kbSearch.dispatch("input", {});');
  await tick();
  results.semantic_exit = run(
    "({ mode: state.kbSemanticMode, requests: __kbRequests.slice(), showingSemantic: state.kbShowingSemantic })",
  );

  // 3) 导入预览 would_import=0：确认框不再出现、也不分批上传
  run(`
    __toasts.length = 0;
    globalThis.__importCalls = 0;
    confirm = () => true;
    els.kbImport = { disabled: false, textContent: "", addEventListener() {} };
    els.kbImportFile = {
      value: "",
      files: [{
        size: 64,
        arrayBuffer: async () =>
          new TextEncoder().encode('{"documents":[{"content":"已存在"}]}').buffer,
      }],
    };
    getJSON = async (url) => {
      if (url.startsWith("/api/knowledge/import/preview")) {
        return { would_import: 0, skipped: 1, skipped_duplicate: 1 };
      }
      __importCalls += 1;
      return { imported: 0, skipped: 0 };
    };
  `);
  await run("importKnowledgeFile()");
  results.preview_zero = run(
    "({ toasts: __toasts.slice(), imports: __importCalls })",
  );

  // 4) 只改分类、正文未动时再次点同一行编辑：必须弹覆盖确认
  run(`
    __toasts.length = 0;
    globalThis.__confirmCalls = 0;
    confirm = () => {
      __confirmCalls += 1;
      return true;
    };
    state.kbEditingId = 5;
    state.kbEditBaseline = "原始内容";
    state.kbEditingMetadata = { category: "旧分类" };
    els.kbContent.value = "原始内容";
    els.kbCategory.value = "新分类";
  `);
  await run(
    'startEditKnowledge({ id: 5, content: "原始内容", metadata: { category: "旧分类" } })',
  );
  results.category_guard = run(
    "({ confirms: __confirmCalls, category: els.kbCategory.value })",
  );
  return results;
}

async function scenarioArbitrate(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);

  // 评测先发起（慢），随后查重复先返回；评测的迟到响应不能覆盖重复列表
  const evalPromise = run("runKbEval()");
  await tick();
  const duplicatePromise = run("runKbDuplicates()");
  await tick();
  env.__requests[1].resolve({
    total_groups: 1,
    total_fragments: 2,
    shown: 1,
    groups: [{ ids: [3, 7], preview: "重复内容" }],
  });
  await duplicatePromise;
  const afterDuplicates = run("els.kbEvalResult.textContent");

  env.__requests[0].resolve({
    total: 30,
    hit1: 0.6,
    hit3: 0.9,
    hit5: 1.0,
    mrr: 0.8,
    rows: [],
  });
  await evalPromise;
  return {
    after_duplicates: afterDuplicates,
    final_text: run("els.kbEvalResult.textContent"),
    eval_button: run("els.kbEval.textContent"),
    duplicates_button: run("els.kbDuplicates.textContent"),
  };
}

async function scenarioDecode(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const decode = runIn(env, "decodeImportFile");
  const importFile = runIn(env, "importTripsFromFile");
  const gbkBytes = [196, 191, 181, 196, 181, 216, 44, 201, 207, 186, 163];
  const utf8Bytes = Array.from(Buffer.from("目的地,杭州", "utf8"));
  const results = {};
  results.gbk = await decode({
    size: gbkBytes.length,
    arrayBuffer: async () => new Uint8Array(gbkBytes).buffer,
  });
  results.utf8 = await decode({
    size: utf8Bytes.length,
    arrayBuffer: async () => new Uint8Array(utf8Bytes).buffer,
  });
  const utf16Bytes = Array.from(Buffer.from("﻿目的地,上海", "utf16le"));
  results.utf16_bom = await decode({
    size: utf16Bytes.length,
    arrayBuffer: async () => new Uint8Array(utf16Bytes).buffer,
  });
  const utf16NoBom = Array.from(
    Buffer.from("origin,destination\n北京,上海\n", "utf16le")
  );
  results.utf16_nobom = await decode({
    size: utf16NoBom.length,
    arrayBuffer: async () => new Uint8Array(utf16NoBom).buffer,
  });
  await importFile({
    size: 2 * 1024 * 1024 + 1,
    arrayBuffer: async () => {
      throw new Error("超大文件不应读取");
    },
  });
  results.oversize_toasts = run("__toasts.slice()");
  results.oversize_requests = run("__requests.length");
  results.oversize_button = {
    text: run("els.tripImport.textContent"),
    disabled: run("els.tripImport.disabled"),
  };

  // 导入进行中：按钮禁用并显示「导入中…」，完成后复位；重复选择被忽略
  run("__refreshCalls = 0;");
  run(
    "refreshStatus = async (options = {}) => { __refreshCalls += 1; __refreshOptions = options; if (!options.silentFailure) { __toasts.push('刷新数据概览失败'); } };"
  );
  run(
    "__lastConfirm = null; confirm = (message) => { __lastConfirm = message; return true; };"
  );
  run("__importCalls = 0; __urls = [];");
  run(
    "getJSON = (url) => { __importCalls += 1; __urls.push(url); return new Promise((resolve) => { __resolveImport = resolve; }); };"
  );
  let pendingError = null;
  const pending = importFile({
    size: utf8Bytes.length,
    arrayBuffer: async () => new Uint8Array(utf8Bytes).buffer,
  }).catch((error) => {
    pendingError = String((error && error.stack) || error);
  });
  await tick();
  results.pending_error = pendingError;
  results.busy = {
    text: run("els.tripImport.textContent"),
    disabled: run("els.tripImport.disabled"),
  };
  await importFile({
    size: utf8Bytes.length,
    arrayBuffer: async () => new Uint8Array(utf8Bytes).buffer,
  });
  results.busy_requests = run("__importCalls");
  run(
    "__resolveImport({ rows: 1, created: 1, updated: 0, skipped: [{ line: 3, reason: '目的地为空' }, { line: 7, reason: '出发日期格式应为 YYYY-MM-DD' }], skipped_total: 73 })"
  );
  await tick();
  run(
    "__resolveImport({ created: 1, updated: 0, skipped: [], skipped_total: 60 })"
  );
  await pending;
  await tick();
  results.after_import = {
    text: run("els.tripImport.textContent"),
    disabled: run("els.tripImport.disabled"),
    toasts: run("__toasts.slice()"),
    confirm: run("__lastConfirm"),
    urls: run("__urls.slice()"),
    refresh_calls: run("__refreshCalls"),
    refresh_options: run("__refreshOptions"),
  };

  // 预览期间关闭面板：不弹确认，也不写入
  run(
    "els.memorySheet.hidden = true; els.shortcutOverlay.hidden = true; __confirmCalls = 0; __importCalls = 0; __urls = [];"
  );
  run("confirm = () => { __confirmCalls += 1; return true; };");
  const closed = importFile({
    size: utf8Bytes.length,
    arrayBuffer: async () => new Uint8Array(utf8Bytes).buffer,
  });
  await tick();
  run(
    "__resolveImport({ rows: 1, created: 1, updated: 0, skipped: [], skipped_total: 0 })"
  );
  await closed;
  await tick();
  results.closed = {
    confirm_calls: run("__confirmCalls"),
    calls: run("__importCalls"),
    urls: run("__urls.slice()"),
  };
  run("els.memorySheet.hidden = false; els.shortcutOverlay.hidden = false;");

  // 解码 / 请求期间切换用户：导入仍落在选择文件时的用户
  run("state.userId = 'user-a'; __lastUrl = null; __importCalls = 0; __urls = [];");
  run(
    "getJSON = (url) => { __importCalls += 1; __lastUrl = url; __urls.push(url); return new Promise((resolve) => { __resolveImport = resolve; }); };"
  );
  const pinned = importFile({
    size: utf8Bytes.length,
    arrayBuffer: async () => new Uint8Array(utf8Bytes).buffer,
  });
  await tick();
  run("state.userId = 'user-b';");
  run(
    "__resolveImport({ rows: 1, created: 1, updated: 0, skipped: [], skipped_total: 0 })"
  );
  await tick();
  run("__resolveImport({ created: 1, updated: 0, skipped: [] })");
  await pinned;
  await tick();
  results.pinned_url = run("__lastUrl");
  results.pinned_urls = run("__urls.slice()");
  results.user_after = run("state.userId");

  // 预览后取消：只发 dry-run，不写库
  run("state.userId = 'user-c'; __confirmResult = false; __confirmCalls = 0; __importCalls = 0; __urls = [];");
  run(
    "confirm = () => { __confirmCalls += 1; return __confirmResult; };"
  );
  const cancelled = importFile({
    size: utf8Bytes.length,
    arrayBuffer: async () => new Uint8Array(utf8Bytes).buffer,
  });
  await tick();
  run(
    "__resolveImport({ rows: 1, created: 1, updated: 0, skipped: [], skipped_total: 0 })"
  );
  await cancelled;
  await tick();
  results.cancel = {
    calls: run("__importCalls"),
    confirm_calls: run("__confirmCalls"),
    last_toast: run("__toasts.slice()").slice(-1)[0],
    urls: run("__urls.slice()"),
  };

  // 字符数上限（与 POST /api/trips/import 的 content 上限一致）
  const bigText = Buffer.alloc(500_001, 97);
  const beforeCalls = run("__importCalls");
  await importFile({
    size: bigText.length,
    arrayBuffer: async () => new Uint8Array(bigText).buffer,
  });
  results.char_limit = {
    last_toast: run("__toasts.slice()").slice(-1)[0],
    calls_delta: run("__importCalls") - beforeCalls,
    button: {
      text: run("els.tripImport.textContent"),
      disabled: run("els.tripImport.disabled"),
    },
  };
  return results;
}

async function scenarioSessionExportHtml(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    __downloads = [];
    __toasts = [];
    downloadFromApi = (url, filename) => {
      __downloads.push({ url, filename });
      return Promise.resolve(true);
    };
    cancelPendingEdits = () => true;
    toast = (text) => __toasts.push(text);
    state.sessionId = "s1";
    state.userId = "u1";
  `);
  await run("exportSessionHtml()");
  await run("new Promise((resolve) => setTimeout(resolve, 0))");
  return {
    downloads: run("__downloads.slice()"),
    toasts: run("__toasts.slice()"),
  };
}

function scenarioTripsExport(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    __downloads = [];
    downloadFromApi = async (url, filename) => {
      __downloads.push({ url, filename });
    };
    state.userId = "u-trips";
    state.tripsExpanded = true;
    state.tripsFilter = "上海";
    state.tripsStatusFilter = "past";
    bindEvents();
  `);
  // 展开视图：关键词与状态筛选随导出 URL 提交
  run("els.exportTrips.click()");
  const filtered = run("__downloads.slice()");
  // 收起视图：不带筛选，但仍走 /api/trips/export?format=ics
  run("state.tripsExpanded = false;");
  run("els.exportTrips.click()");
  const collapsed = run("__downloads.slice()");
  return { filtered, collapsed };
}

async function scenarioTripsDedupe(source) {
  const runScenario = async (preview, confirmResult, applyResult) => {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    run(`
      __requests = [];
      __toasts = [];
      __confirmCalls = 0;
      __confirmMessage = "";
      __refreshed = 0;
      confirm = (message) => { __confirmCalls += 1; __confirmMessage = message; return ${confirmResult}; };
      refreshStatus = async () => { __refreshed += 1; };
      getJSON = (url, options) => {
        __requests.push({ url, options: options || {} });
        let isPreview = false;
        try {
          isPreview =
            JSON.parse((options && options.body) || "{}").dry_run === true;
        } catch (error) {
          isPreview = false;
        }
        const data = isPreview ? ${JSON.stringify(preview)} : ${JSON.stringify(applyResult)};
        return Promise.resolve(data);
      };
      state.userId = "u-dedupe";
      bindEvents();
    `);
    run("els.tripsDedupe.click();");
    for (let i = 0; i < 50; i += 1) {
      await tick();
      // 按钮在 finally 复位：等待它从「检查中…」恢复即表示整段流程结束
      if (!run("els.tripsDedupe.disabled")) break;
    }
    return run(`({
      urls: __requests.map((item) => item.url),
      methods: __requests.map((item) => (item.options || {}).method || ""),
      bodies: __requests.map((item) => {
        try {
          return JSON.parse((item.options && item.options.body) || "null");
        } catch (error) {
          return null;
        }
      }),
      contentTypes: __requests.map(
        (item) => ((item.options || {}).headers || {})["Content-Type"] || "",
      ),
      toasts: __toasts.slice(),
      confirmCalls: __confirmCalls,
      confirmMessage: __confirmMessage,
      refreshed: __refreshed,
      button: {
        text: els.tripsDedupe.textContent,
        disabled: els.tripsDedupe.disabled,
      },
    })`);
  };
  return {
    empty: await runScenario({ would_remove: 0 }, "true", { removed: 0 }),
    ambiguous: await runScenario(
      { would_remove: 0, ambiguous_ids: ["trip_2"] },
      "true",
      { removed: 0 },
    ),
    cancel: await runScenario({ would_remove: 2 }, "false", { removed: 2 }),
    applied: await runScenario({ would_remove: 2 }, "true", { removed: 2 }),
  };
}

async function scenarioStartup(source) {
  const runScenario = async (userNavigates) => {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    run(`
      __opened = [];
      refreshHealth = async () => {};
      refreshStatus = async () => {};
      state.sessionId = "saved-session";
      state.ready = false;
      sessionLoadToken = 5;
      openSession = async (sessionId, options) => {
        sessionLoadToken += 1;
        __opened.push([sessionId, options || {}]);
      };
      loadSessions = async () => {
        if (${userNavigates}) {
          await openSession("clicked-session", {});
        }
      };
    `);
    await run("init()");
    return {
      opened: run("__opened.slice()"),
      ready: run("state.ready"),
      sessionId: run("state.sessionId"),
    };
  };
  return {
    restore: await runScenario(false),
    user_clicked: await runScenario(true),
  };
}

async function scenarioPanelFixes(source) {
  const results = {};

  // (a) 会话分页游标：头部插入一整页导致本页与已显示完全重叠时仍向前推进
  {
    const env = buildEnv(source);
    runIn(
      env,
      "state.sessions = Array.from({length: 50}, (_, i) => ({session_id: 'old' + i, title: 't' + i}));" +
        "state.sessionsOffset = 50; state.sessionsLoading = false; state.sessionTotal = 110;",
    );
    const promise = runIn(env, "loadMoreSessions()");
    await tick();
    const url = String(env.__requests[0].args[0]);
    const overlap = [];
    for (let i = 0; i < 25; i += 1) overlap.push({ session_id: "old" + i, title: "t" + i });
    for (let i = 0; i < 25; i += 1) overlap.push({ session_id: "new" + i, title: "n" + i });
    env.__requests[0].resolve({ sessions: overlap, total: 110 });
    await promise;
    const second = runIn(env, "loadMoreSessions()");
    await tick();
    const secondUrl = String(env.__requests[1].args[0]);
    env.__requests[1].resolve({ sessions: [], total: 110 });
    await tick();
    // 空页但 total 仍大于已显示数量：从 0 重取整窗，不能永久卡在 99/100
    const thirdUrl = String(env.__requests[2].args[0]);
    const reloaded = [];
    for (let i = 0; i < 75; i += 1) reloaded.push({ session_id: "r" + i, title: "r" + i });
    env.__requests[2].resolve({ sessions: reloaded, total: 110 });
    await tick();
    await second;
    results.sessions_cursor = {
      first_url: url,
      second_url: secondUrl,
      third_url: thirdUrl,
      offset: runIn(env, "state.sessionsOffset"),
      count: runIn(env, "state.sessions.length"),
    };
  }

  // (b) 列表中的会话服务端已无消息：移除幽灵行、不切到新对话
  {
    const env = buildEnv(source);
    runIn(
      env,
      "state.sessions = [{session_id: 's1', title: 'x'}];" +
        "state.sessionTotal = 1; state.sessionsOffset = 1; state.sessionId = 'keep';",
    );
    const promise = runIn(env, 'openSession("s1")');
    await tick();
    env.__requests[0].resolve({ messages: [] });
    await promise;
    results.stale_session = {
      sessions: runIn(env, "state.sessions.length"),
      sessionId: runIn(env, "state.sessionId"),
      toasts: runIn(env, "__toasts.slice()"),
    };
  }

  // (c) 知识导入分批：整份文件不再按条数拒绝，批次受条数与字节预算约束
  {
    const env = buildEnv(source);
    const plain = runIn(
      env,
      "buildKnowledgeImportBatches(Array.from({length: 25000}, (_, i) => ({content: 'c' + i, metadata: {}})))",
    );
    const sizes = plain.batches.map((batch) => batch.length);
    results.kb_batches = {
      batch_count: plain.batches.length,
      total: sizes.reduce((sum, n) => sum + n, 0),
      max_batch: Math.max(...sizes),
    };
    const grouped = runIn(
      env,
      "buildKnowledgeImportBatches([" +
        "{content: 'a', metadata: {group: 'g1', part: '1', parts: '3'}}," +
        "{content: 'b', metadata: {}}," +
        "{content: 'c', metadata: {group: 'g1', part: '2', parts: '3'}}," +
        "{content: 'd', metadata: {group: 'g1', part: '3', parts: '3'}}" +
        "])",
    );
    results.kb_group_batches = grouped.batches.map((batch) =>
      batch.map((doc) => String((doc.metadata || {}).content || doc.content)),
    );
    const oversize = runIn(
      env,
      "buildKnowledgeImportBatches([{content: 'x'.repeat(600000), metadata: {}}])",
    );
    const huge_group = runIn(
      env,
      "buildKnowledgeImportBatches(Array.from({length: 20001}, (_, i) => ({content: 'p' + i, metadata: {group: 'big', part: String(i + 1), parts: '20001'}})))",
    );
    results.kb_errors = {
      oversize: oversize.error || "",
      huge_group: huge_group.error || "",
    };
  }

  return results;
}

async function waitRequest(env) {
  for (let i = 0; i < 120; i += 1) {
    if (env.__requests.length) return env.__requests.shift();
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error("getJSON request not queued");
}

async function scenarioReconcile(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const oldMessages = [{ role: "user", content: "旧提问" }];
  const newMessages = [
    { role: "user", content: "你好" },
    {
      role: "assistant",
      content: "回答",
      meta: { client_turn_id: "turn-1" },
    },
  ];
  run(`
    document.querySelector = () => null;
    state.userId = "u1";
    state.sessionId = "s1";
    state.busy = false;
    sessionLoadToken = 5;
    els.input.value = "";
    globalThis.__openCalls = 0;
    globalThis.__extra = 0;
    openSession = async () => {
      __openCalls += 1;
      sessionLoadToken += 1 + (globalThis.__extra || 0);
    };
    resizeInput = () => {};
  `);
  // 判定期限已拉长到 ~12 秒（覆盖服务端 10 秒 drain）：VM 内的定时器
  // 立即触发，测试不真等；异常在回调里吞掉，避免打乱场景
  run("setTimeout = (cb) => { try { cb(); } catch (_) {} return 0; };");

  const runCase = async (finalMessages) => {
    run(
      '__toasts.length = 0; globalThis.__openCalls = 0; els.input.value = ""; sessionLoadToken = 5; globalThis.__extra = 0;'
    );
    const promise = run('reconcileStoppedTurn("s1", "你好", 5, "turn-1")');
    const timeouts = [];
    // 判定期限覆盖服务端 10 秒 drain：前 9 次轮询 + 第 10 次（进入最终确认
    // 分支前的那次）都先给旧数据；VM 内 setTimeout 立即触发，测试不真等
    for (let i = 0; i < 10; i += 1) {
      const request = await waitRequest(env);
      timeouts.push(request.args[2]);
      request.resolve({ messages: oldMessages });
      await tick();
    }
    // 最终确认：openSession 后追加的确认请求
    const confirm = await waitRequest(env);
    timeouts.push(confirm.args[2]);
    confirm.resolve({ messages: finalMessages });
    await promise;
    await tick();
    return {
      input: run("els.input.value"),
      toasts: run("__toasts.slice()"),
      open_calls: run("__openCalls"),
      timeouts,
    };
  };

  const persistedLate = await runCase(newMessages);
  const stillMissing = await runCase(oldMessages);

  // 最终确认请求失败 / 超时：落库结果未知——openSession 可能已渲染出刚
  // 落库的一轮，不能把提问塞回输入框（诱导重发会产生重复问答）
  const runConfirmFailed = async () => {
    run(
      '__toasts.length = 0; globalThis.__openCalls = 0; els.input.value = ""; sessionLoadToken = 5; globalThis.__extra = 0;'
    );
    const promise = run('reconcileStoppedTurn("s1", "你好", 5, "turn-1")');
    for (let i = 0; i < 10; i += 1) {
      const request = await waitRequest(env);
      request.resolve({ messages: oldMessages });
      await tick();
    }
    const confirm = await waitRequest(env);
    confirm.reject(new Error("timeout"));
    await promise;
    await tick();
    return {
      input: run("els.input.value"),
      toasts: run("__toasts.slice()"),
    };
  };
  const confirmFailed = await runConfirmFailed();

  // 判定期内始终拿不到有效响应（连续超时）：结果未知，同样不回填输入框
  const runAllFailed = async () => {
    run(
      '__toasts.length = 0; globalThis.__openCalls = 0; els.input.value = ""; sessionLoadToken = 5; globalThis.__extra = 0;'
    );
    const promise = run('reconcileStoppedTurn("s1", "你好", 5, "turn-1")');
    for (let i = 0; i < 10; i += 1) {
      const request = await waitRequest(env);
      request.reject(new Error("timeout"));
      await tick();
    }
    await promise;
    await tick();
    return {
      input: run("els.input.value"),
      toasts: run("__toasts.slice()"),
      open_calls: run("__openCalls"),
    };
  };
  const allFailed = await runAllFailed();

  // 判定期内前 9 次拿到了有效历史、第 10 次轮询失败：按「已确认未落库」
  // 处理（回填输入框），不能因最后一次失败误判结果未知
  const runLastPollFailed = async () => {
    run(
      '__toasts.length = 0; globalThis.__openCalls = 0; els.input.value = ""; sessionLoadToken = 5; globalThis.__extra = 0;'
    );
    const promise = run('reconcileStoppedTurn("s1", "你好", 5, "turn-1")');
    for (let i = 0; i < 9; i += 1) {
      const request = await waitRequest(env);
      request.resolve({ messages: oldMessages });
      await tick();
    }
    const last = await waitRequest(env);
    last.reject(new Error("429"));
    await promise;
    await tick();
    return {
      input: run("els.input.value"),
      toasts: run("__toasts.slice()"),
    };
  };
  const lastPollFailed = await runLastPollFailed();

  // 对账期间用户点击了另一个会话：openSession 自增 token 后又被用户点击
  // 推进一次，不能认领该 token 把旧提问写进新会话的输入框
  run(
    '__toasts.length = 0; globalThis.__openCalls = 0; els.input.value = ""; sessionLoadToken = 5; globalThis.__extra = 1;'
  );
  const interleavedPromise = run('reconcileStoppedTurn("s1", "你好", 5, "turn-1")');
  for (let i = 0; i < 10; i += 1) {
    const request = await waitRequest(env);
    request.resolve({ messages: oldMessages });
    await tick();
  }
  await interleavedPromise;
  await tick();
  run("globalThis.__extra = 0;");
  const interleaved = {
    input: run("els.input.value"),
    toasts: run("__toasts.slice()"),
    open_calls: run("__openCalls"),
  };

  return {
    persistedLate,
    stillMissing,
    interleaved,
    confirmFailed,
    allFailed,
    lastPollFailed,
  };
}


async function scenarioKbSearchFlow(source) {
  // 知识库搜索：防抖 / 去重 / 429 保留列表 / 分页对齐 / 输入法守卫
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    const makeField = () => ({
      value: "",
      handlers: {},
      addEventListener(type, fn) {
        (this.handlers[type] = this.handlers[type] || []).push(fn);
      },
      dispatch(type, event) {
        (this.handlers[type] || []).forEach((fn) => fn(event || {}));
      },
    });
    els.kbSearch = makeField();
    els.kbCategory = makeField();
    els.kbList = { textContent: "", innerHTML: "", appendChild() {}, focus() {} };
    els.kbMore = {
      hidden: true,
      disabled: false,
      addEventListener() {},
      focus() {},
    };
    globalThis.__rendered = 0;
    renderKnowledgeDocuments = (items, append) => {
      state.kbItems = append ? state.kbItems.concat(items) : items.slice();
      globalThis.__rendered += 1;
    };
    refreshKnowledgeCategories = () => {};
    state.userId = "u1";
    setTimeout = (cb) => { try { cb(); } catch (_) {} return 0; };
    bindEvents();
  `);
  const rows = (prefix, count) =>
    Array.from({ length: count }, (_, i) => ({
      id: prefix + i,
      content: prefix + i,
      metadata: {},
    }));

  // 1) foo 搜索成功：20/40，加载更多可见
  run('els.kbSearch.value = "foo"; els.kbSearch.dispatch("input", {});');
  (await waitRequest(env)).resolve({ total: 40, items: rows("foo-", 20) });
  await tick();
  const fooState = run(
    '({ items: state.kbItems.length, moreHidden: els.kbMore.hidden })'
  );

  // 2) 输入 bar 刷新收 429：列表保留、加载更多隐藏、toast 提示
  run('els.kbSearch.value = "bar"; els.kbSearch.dispatch("input", {});');
  const limited = await waitRequest(env);
  limited.reject(
    Object.assign(new Error("知识库操作过于频繁，请约 60 秒后重试"), {
      status: 429,
    })
  );
  await tick();
  const after429 = run(
    '({ items: state.kbItems.length, moreHidden: els.kbMore.hidden, toasts: __toasts.slice() })'
  );

  // 3) 条件不一致时「加载更多」不得发请求（否则新查询 + 旧游标混合列表）
  run("loadMoreKnowledgeDocuments();");
  await tick();
  const moreBlocked = env.__requests.length === 0;

  // 4) 回车强制重试同一查询：不被去重吞掉
  run(
    'els.kbSearch.dispatch("keydown", { key: "Enter", isComposing: false, preventDefault() {} });'
  );
  const retry = await waitRequest(env);
  retry.resolve({ total: 1, items: rows("bar-", 1) });
  await tick();
  const afterRetry = run(
    '({ query: state.kbQuery, loaded: state.kbLoadedQuery, items: state.kbItems.length })'
  );

  // 5) 输入法合成期间按回车不发请求；compositionend 恢复调度
  run(`
    els.kbSearch.dispatch("compositionstart", {});
    els.kbSearch.value = "baz";
    els.kbSearch.dispatch("keydown", { key: "Enter", isComposing: false, preventDefault() {} });
  `);
  await tick();
  const composeBlocked = env.__requests.length === 0;
  run('els.kbSearch.dispatch("compositionend", {});');
  const composed = await waitRequest(env);
  composed.resolve({ total: 0, items: [] });
  await tick();
  return {
    fooState,
    after429,
    moreBlocked,
    afterRetry,
    composeBlocked,
    composedUrl: composed.args[0],
  };
}

async function scenarioRetryGuard(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    state.userId = "u1";
    state.sessionId = "s1";
    state.busy = false;
    state.ready = true;
    state.sessionLoading = false;
    globalThis.__sent = [];
    globalThis.__opened = 0;
    globalThis.__removed = 0;
    sendMessage = async (text) => { __sent.push(text); };
    openSession = async () => { __opened += 1; return true; };
    setBusy = () => {};
    trimRegenButtons = () => {};
  `);
  const makeTurnNode = () =>
    run(
      '(() => { const d = document.createElement("div"); d.dataset.turnId = "turn-1"; d.remove = () => { globalThis.__removed += 1; }; globalThis.__node = d; return d; })()'
    );
  const runCase = async (messages) => {
    makeTurnNode();
    run("__toasts.length = 0; __sent = []; __opened = 0; __removed = 0;");
    const promise = run('retryFailed(__node, "你好")');
    const request = await waitRequest(env);
    request.resolve({ messages });
    await promise;
    await tick();
    return {
      sent: run("__sent.slice()"),
      opened: run("__opened"),
      removed: run("__removed"),
      toasts: run("__toasts.slice()"),
      timeout: request.args[2],
    };
  };
  const persisted = await runCase([
    { role: "user", content: "你好" },
    { role: "assistant", content: "回答", meta: { client_turn_id: "turn-1" } },
  ]);
  const missing = await runCase([{ role: "user", content: "旧提问" }]);
  return { persisted, missing };
}

async function scenarioDownloadGuards(source) {
  // 下载：并发点击被抑制；服务端挂起时 abort 超时并复位在途标记
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    globalThis.__toasts = [];
    toast = (message) => { __toasts.push(message); };
    globalThis.__controllers = [];
    globalThis.__calls = 0;
    AbortController = class {
      constructor() {
        this.signal = {
          listeners: [],
          addEventListener: (_, fn) => this.signal.listeners.push(fn),
        };
        __controllers.push(this);
      }
      abort() {
        this.signal.listeners.forEach((fn) => fn());
      }
    };
    fetch = (url, options) => new Promise((resolve, reject) => {
      __calls += 1;
      const call = __calls;
      if (call === 1) {
        // 第一个请求挂起，直到被 abort
        options.signal.addEventListener("abort", () => {
          const err = new Error("aborted");
          err.name = "AbortError";
          reject(err);
        });
        return;
      }
      resolve({
        ok: true,
        status: 200,
        headers: {
          get: (name) =>
            String(name).toLowerCase() === "content-disposition"
              ? 'attachment; filename="x.json"'
              : "",
        },
        blob: async () => ({ __blob: true }),
      });
    });
    URL.createObjectURL = () => "blob:x";
    URL.revokeObjectURL = () => {};
    document.body.appendChild = () => {};
  `);
  run('__first = downloadFromApi("/api/export", "f.json")');
  const second = await run('downloadFromApi("/api/export", "f.json")');
  run("__controllers[0].abort()");
  run("__first.then((value) => { globalThis.__firstResult = value; });");
  await new Promise((resolve) => setTimeout(resolve, 10));
  const firstResult = run("globalThis.__firstResult");
  // 第一次结束后在途标记必须复位：同一 URL 可以重新下载
  const third = await run('downloadFromApi("/api/export", "f.json")');
  return {
    second,
    firstResult,
    third,
    calls: run("__calls"),
    toasts: run("__toasts.slice()"),
  };
}

async function scenarioSessionRefresh(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const page1 = Array.from({ length: 250 }, (_, i) => ({ session_id: "s-" + i }));
  run(`
    state.userId = "u1";
    state.searchQuery = "";
    state.showArchived = false;
    state.sessions = Array.from({ length: 250 }, (_, i) => ({ session_id: "old-" + i }));
    state.sessionTotal = 300;
    state.sessionsOffset = 250;
    renderHistory = () => {};
    updateArchivedToggle = () => {};
    markActiveHistory = () => {};
    renderArchivedHint = () => {};
  `);
  const promise = run("loadSessions(state.searchQuery, {})");
  const first = await waitRequest(env);
  first.resolve({ sessions: page1, total: 300 });
  await promise;
  return {
    count: run("state.sessions.length"),
    offset: run("state.sessionsOffset"),
    first_url: first.args[0],
    // 单次请求即可满足目标条数时不应再发第二页（有在途请求会留在队列里）
    pending_requests: env.__requests.length,
  };
}

function scenarioEditGuard(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    state.userId = "u1";
    state.busy = false;
    globalThis.__asked = 0;
    globalThis.__fakeEdit = document.createElement("input");
    document.querySelector = (selector) =>
      selector === ".user-edit-input" ? __fakeEdit : null;
    window.confirm = () => { __asked += 1; return false; };
  `);
  run('openSession("s1", {})');
  const blocked = { asked: run("__asked"), queued: env.__requests.length };
  run("__asked = 0; window.confirm = () => { __asked += 1; return true; };");
  run('openSession("s1", {})');
  return { blocked, allowedQueued: env.__requests.length };
}

async function scenarioSse(source) {
  const env = buildEnv(source);
  const chunks = [
    'data: {"type":"session","session_id":"s"}\r\n\r\n',
    ': keepalive\r\n\r\n',
    'data: {"type":\ndata: "done","reply":"ok"}\r\n\r\n',
    'data: null\n\n',
  ];
  return runIn(
    env,
    `(async () => {
      const chunks = ${JSON.stringify(chunks)};
      const events = [];
      const encoded = chunks.map((chunk) => new TextEncoder().encode(chunk));
      let index = 0;
      const body = {
        getReader: () => ({
          read: async () =>
            index < encoded.length
              ? { done: false, value: encoded[index++] }
              : { done: true, value: undefined },
        }),
      };
      await readEventStream(body, (event) => events.push(event));
      return events;
    })()`
  );
}

async function scenarioSseEof(source) {
  const runCase = async (chunks) => {
    const env = buildEnv(source);
    return runIn(
      env,
      `(async () => {
        const chunks = ${JSON.stringify(chunks)};
        const events = [];
        const encoded = chunks.map((chunk) => new TextEncoder().encode(chunk));
        let index = 0;
        const body = {
          getReader: () => ({
            read: async () =>
              index < encoded.length
                ? { done: false, value: encoded[index++] }
                : { done: true, value: undefined },
          }),
        };
        await readEventStream(body, (event) => events.push(event));
        return events;
      })()`
    );
  };
  return {
    // EOF 前缺少终止空行：最后一行是完整 data:，必须补发
    noBlank: await runCase(['data: {"type":"done","reply":"tail"}']),
    // JSON 中途截断：补空行后解析失败应被忽略（不误派发半帧）
    truncated: await runCase(['data: {"type":"done","reply":"']),
    // 正常带终止空行：行为不变
    withBlank: await runCase(['data: {"type":"done","reply":"ok"}\n\n']),
  };
}

async function scenarioReplayError(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    __reconcileCalls = [];
    refreshStatus = async () => {};
    loadSessions = async () => {};
    reconcileStoppedTurn = async (sessionId, message, token, turnId, reason) => {
      __reconcileCalls.push({ sessionId, message, turnId, reason });
      return "done";
    };
    state.ready = true;
    state.userId = "u1";
    state.sessionId = "s1";
    state.partialHistory = false;
    document.querySelector = () => null;
    els.messages = {
      children: [],
      appendChild(node) { this.children.push(node); return node; },
      insertBefore(node) { this.children.push(node); return node; },
      querySelectorAll(sel) {
        if (sel === ".msg.user") return this.children.filter((n) => n.role === "user" && !n.removed);
        return [];
      },
      querySelector() { return null; },
      set innerHTML(value) { if (value === "") this.children.length = 0; },
      get innerHTML() { return ""; },
      get firstChild() { return this.children[0] || null; },
    };
    renderUserNode = (text) => ({
      __msg: true, role: "user", text, dataset: {},
      remove() { this.removed = true; },
      querySelector() { return null; },
      querySelectorAll() { return []; },
    });
    renderAssistantNode = (text) => ({ __msg: true, role: "assistant", text });
    hideWelcome = () => {};
    trimRegenButtons = () => {};
    refreshEditButtons = () => {};
    const frames = [
      "data: " + JSON.stringify({ type: "session", session_id: "s1" }) + String.fromCharCode(10) + String.fromCharCode(10),
      "data: " + JSON.stringify({
        type: "error",
        message: "上一请求已处理但响应未送达，请刷新会话历史",
        idempotent_replay: true,
        persisted: true,
      }) + String.fromCharCode(10) + String.fromCharCode(10),
    ];
    const bytes = new TextEncoder().encode(frames.join(""));
    fetch = async () => {
      let sent = false;
      return {
        ok: true,
        status: 200,
        body: {
          getReader: () => ({
            read: async () => {
              if (sent) return { done: true, value: undefined };
              sent = true;
              return { done: false, value: bytes };
            },
          }),
        },
        json: async () => ({}),
      };
    };
  `);
  await run('sendMessage("重放提问")');
  await new Promise((resolve) => setTimeout(resolve, 80));
  return run(`(() => {
    const user = els.messages.children.find((n) => n.role === "user");
    return {
      transient: user ? (user.dataset.transient || "none") : "missing",
      reconcileCalls: __reconcileCalls.slice(),
      toasts: __toasts.slice(),
      pending: state.pendingStoppedTurns.length,
    };
  })()`);
}

async function scenarioCopy(source) {
  const env = buildEnv(source);
  const removed = [];
  const originalCreate = env.document.createElement;
  env.document.createElement = (tag) => {
    const node = originalCreate(tag);
    const originalRemove = node.remove.bind(node);
    node.remove = () => {
      removed.push(tag);
      originalRemove();
    };
    return node;
  };
  const previous = {
    focusCalls: 0,
    focus() {
      this.focusCalls += 1;
    },
  };
  env.document.activeElement = previous;
  env.document.execCommand = () => {
    throw new Error("blocked");
  };
  env.navigator.clipboard = null;
  env.isSecureContext = false;
  await runIn(env, `(async () => { await copyText("x"); })()`);
  return {
    removed,
    focus_restored: previous.focusCalls,
    toasts: env.__toasts,
  };
}

async function scenarioMetricsPanel(source) {
  const results = {};
  const payloads = {
    sseOnly: {
      requests_total: 3,
      duration_samples: 0,
      avg_duration_ms: 0,
      max_duration_ms: 0,
      p95_duration_ms: 0,
    },
    sampled: {
      requests_total: 3,
      duration_samples: 2,
      avg_duration_ms: 12.4,
      max_duration_ms: 30,
      p95_duration_ms: 20,
    },
  };
  for (const [key, requests] of Object.entries(payloads)) {
    const env = buildEnv(source);
    runIn(env, 'els.metricsStats = document.createElement("div");');
    const promise = runIn(env, "refreshMetrics()");
    await tick();
    env.__requests[0].resolve({
      requests,
      circuit_breaker: { state: "closed" },
    });
    await promise;
    results[key] = runIn(
      env,
      "els.metricsStats.children.map((box) => box.children.map((node) => String(node.textContent)))"
    );
  }
  return results;
}

async function scenarioDeleteTurn(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run('state.userId = "u1"; state.sessionId = "s1";');
  const promise = run('deleteLastTurn("问题", "turn-1")');
  const request = await waitRequest(env);
  const body = JSON.parse(request.args[1].body);
  request.resolve({ removed: 2 });
  return {
    turn_id: body.turn_id,
    expect: body.expect,
    removed: await promise,
  };
}

async function scenarioScrollPreserve(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const messages = [
    { role: "user", content: "旧问题", timestamp: "" },
    { role: "assistant", content: "回答", timestamp: "", meta: {} },
  ];
  run(`
    state.userId = "u1";
    state.busy = false;
    state.sessions = [];
    state.sessionTotal = 1;
    els.scroll.scrollHeight = 1000;
    els.scroll.clientHeight = 200;
    els.scroll.scrollTop = 100;
    globalThis.__bottomCalls = 0;
    scrollToBottom = () => {
      __bottomCalls += 1;
      els.scroll.scrollTop = els.scroll.scrollHeight;
    };
    renderUserNode = () => document.createElement("div");
    renderAssistantNode = () => document.createElement("div");
    closeMobileSidebar = () => {};
    loadSessions = async () => {};
    refreshStatus = () => {};
  `);
  const runOpen = async (options) => {
    const promise = run(`openSession("s1", ${JSON.stringify(options)})`);
    const request = await waitRequest(env);
    request.resolve({ messages });
    await promise;
    return { scrollTop: run("els.scroll.scrollTop"), calls: run("__bottomCalls") };
  };
  run("els.scroll.scrollTop = 100; __bottomCalls = 0;");
  const silent = await runOpen({ silent: true });
  run("els.scroll.scrollTop = 100; __bottomCalls = 0;");
  const normal = await runOpen({});
  return { silent, normal };
}

async function scenarioHistoryWindow(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const messages = [];
  for (let i = 0; i < 500; i += 1) {
    if (i % 2 === 0) {
      messages.push({ role: "user", content: "问题 " + i, timestamp: "" });
    } else {
      messages.push({
        role: "assistant",
        content: "回答 " + i,
        timestamp: "",
        meta: { client_turn_id: "turn-" + i },
      });
    }
  }
  run(`
    state.userId = "u1";
    state.busy = false;
    state.sessions = [];
    state.sessionTotal = 1;
    els.messages = {
      children: [],
      set innerHTML(value) { if (value === "") this.children.length = 0; },
      get innerHTML() { return ""; },
      get firstChild() { return this.children[0] || null; },
      appendChild(node) { this.children.push(node); return node; },
      insertBefore(node, ref) {
        const index = ref ? this.children.indexOf(ref) : -1;
        if (index >= 0) this.children.splice(index, 0, node);
        else this.children.push(node);
        return node;
      },
      querySelectorAll() { return this.children.filter((n) => n.__msg); },
      querySelector() { return null; },
    };
    renderUserNode = (text) => ({ __msg: true, role: "user", text });
    renderAssistantNode = (text) => ({ __msg: true, role: "assistant", text });
    const originalCreate = document.createElement.bind(document);
    document.createElement = (tag) => {
      const node = originalCreate(tag);
      node.remove = () => {
        const index = els.messages.children.indexOf(node);
        if (index >= 0) els.messages.children.splice(index, 1);
      };
      return node;
    };
    closeMobileSidebar = () => {};
    loadSessions = async () => {};
    refreshStatus = () => {};
  `);
  const promise = run('openSession("s1", {})');
  const request = await waitRequest(env);
  request.resolve({ messages });
  await promise;
  const snapshot = () =>
    run(`(() => {
      const msgs = els.messages.children.filter((n) => n.__msg === true);
      const button = els.messages.children.find(
        (n) => n.className === "earlier-more",
      );
      return {
        count: msgs.length,
        hasButton: Boolean(button),
        buttonText: button ? button.textContent : "",
      };
    })()`);
  const initial = snapshot();
  run('els.messages.children.find((n) => n.className === "earlier-more").click()');
  const afterFirst = snapshot();
  run('els.messages.children.find((n) => n.className === "earlier-more").click()');
  const afterSecond = snapshot();
  return { initial, afterFirst, afterSecond };
}

async function scenarioSearchJump(source) {
  // 搜索命中在默认窗口之外：初始只渲染一个窗口 + 「显示更新消息」按钮，
  // 不能从命中处一路渲染到会话结尾
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const messages = [];
  for (let i = 0; i < 600; i += 1) {
    if (i % 2 === 0) {
      messages.push({ role: "user", content: "问题 " + i, timestamp: "" });
    } else {
      messages.push({
        role: "assistant",
        content: "回答 " + i,
        timestamp: "",
        meta: { client_turn_id: "turn-" + i },
      });
    }
  }
  run(`
    state.userId = "u1";
    state.busy = false;
    state.sessions = [];
    state.sessionTotal = 1;
    els.messages = {
      children: [],
      set innerHTML(value) { if (value === "") this.children.length = 0; },
      get innerHTML() { return ""; },
      get firstChild() { return this.children[0] || null; },
      appendChild(node) { this.children.push(node); return node; },
      insertBefore(node, ref) {
        const index = ref ? this.children.indexOf(ref) : -1;
        if (index >= 0) this.children.splice(index, 0, node);
        else this.children.push(node);
        return node;
      },
      querySelectorAll() { return this.children.filter((n) => n.__msg); },
      querySelector() { return null; },
    };
    renderUserNode = (text) => ({ __msg: true, role: "user", text });
    renderAssistantNode = (text) => ({ __msg: true, role: "assistant", text });
    const originalCreate = document.createElement.bind(document);
    document.createElement = (tag) => {
      const node = originalCreate(tag);
      node.remove = () => {
        const index = els.messages.children.indexOf(node);
        if (index >= 0) els.messages.children.splice(index, 1);
      };
      return node;
    };
    closeMobileSidebar = () => {};
    loadSessions = async () => {};
    refreshStatus = () => {};
    trimRegenButtons = () => {};
    refreshEditButtons = () => {};
  `);
  const promise = run('openSession("s1", { matchIndex: 10 })');
  const request = await waitRequest(env);
  request.resolve({ messages });
  await promise;
  const snapshot = () =>
    run(`(() => {
      const msgs = els.messages.children.filter((n) => n.__msg === true);
      const buttons = els.messages.children.filter(
        (n) => n.className === "earlier-more",
      );
      const earlier = buttons.find((n) => n.textContent.startsWith("显示更早消息"));
      const later = buttons.find((n) => n.textContent.startsWith("显示更新消息"));
      return {
        count: msgs.length,
        earlierText: earlier ? earlier.textContent : "",
        laterText: later ? later.textContent : "",
        partial: state.partialHistory,
        renderedStart: state.renderedStart && state.renderedStart.start,
      };
    })()`);
  const initial = snapshot();
  // 静默重载（备份恢复后）：必须保持命中窗口的上下界，不能渲染到结尾
  const silentReload = run('openSession("s1", { silent: true })');
  (await waitRequest(env)).resolve({ messages });
  await silentReload;
  const silentSnapshot = snapshot();
  const clickLater = () =>
    run(`(() => {
      const button = els.messages.children.find(
        (n) => n.className === "earlier-more" && n.textContent.startsWith("显示更新消息"),
      );
      if (button) button.click();
    })()`);
  clickLater();
  const afterFirst = snapshot();
  clickLater();
  const afterSecond = snapshot();
  clickLater();
  const afterThird = snapshot();
  return { initial, silent: silentSnapshot, afterFirst, afterSecond, afterThird };
}

async function scenarioSessionSearchFailure(source) {
  // 搜索请求失败：查询词要回到「已渲染列表」的查询，列表与「加载更多」
  // 仍按旧查询工作；被后一次请求取代的失败同样不能把状态滚到未渲染词
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    state.userId = "u1";
    state.sessions = [{ session_id: "s-old", title: "旧" }];
    state.sessionsOffset = 1;
    state.sessionTotal = 1;
    state.searchQuery = "old";
    state.sessionsLoadedQuery = "old";
    state.sessionsLoadedArchived = false;
    state.sessionsLoading = false;
    els.history = {
      innerHTML: "",
      querySelector() { return null; },
      querySelectorAll() { return []; },
    };
    renderHistory = () => {};
    toast = (text) => { (globalThis.__toasts ||= []).push(text); };
    els.historySearch = { value: "new" };
  `);
  const promise = run('loadSessions("new", { reset: true })');
  const request = await waitRequest(env);
  request.reject(new Error("boom"));
  await promise;
  const single = {
    searchQuery: run("state.searchQuery"),
    searchInput: run("els.historySearch.value"),
    sessions: run("state.sessions.map((s) => s.session_id)"),
    toasts: run("globalThis.__toasts || []"),
  };

  // 被取代的重试：A 在途时同词重试 B，B 失败——回滚目标是已渲染查询
  // （old），不是从未渲染过的 foo；A 晚到的成功也必须被作废
  const a = run('loadSessions("foo", { reset: true })');
  const requestA = await waitRequest(env);
  const b = run('loadSessions("foo", { reset: true })');
  const requestB = await waitRequest(env);
  requestB.reject(new Error("timeout"));
  await b;
  requestA.resolve({ sessions: [{ session_id: "foo-1", title: "f" }], total: 1 });
  await a;
  const superseded = {
    searchQuery: run("state.searchQuery"),
    loaded: run("state.sessionsLoadedQuery"),
    sessions: run("state.sessions.map((s) => s.session_id)"),
  };

  // 状态与已渲染快照不一致时「加载更多」不发请求
  run('state.searchQuery = "drift"');
  run("loadMoreSessions()");
  await tick();
  const moreBlocked = env.__requests.length === 0;

  // 加载更多返回空页且 total=0（其它标签页 / CLI 已删光）：不能保留旧 total
  run(`
    state.searchQuery = "old";
    state.sessionsLoadedQuery = "old";
    state.showArchived = false;
    state.sessionsLoadedArchived = false;
    state.sessions = Array.from({ length: 50 }, (_, i) => ({ session_id: "s-" + i, title: "t" }));
    state.sessionsOffset = 50;
    state.sessionTotal = 300;
  `);
  const morePromise = run("loadMoreSessions()");
  const moreRequest = await waitRequest(env);
  moreRequest.resolve({ sessions: [], total: 0 });
  // total 从 300 变为 0：分页游标失真，loadMoreSessions 会按已加载数量
  // 从 0 重取整窗（重置后的第一页为空，loadSessions 会再补一页以凑够
  // 目标数量），两个请求都由本场景给出空响应
  const reloadRequest = await waitRequest(env);
  reloadRequest.resolve({ sessions: [], total: 0 });
  const refillRequest = await waitRequest(env);
  refillRequest.resolve({ sessions: [], total: 0 });
  await morePromise;
  const emptyPage = {
    total: run("state.sessionTotal"),
    count: run("state.sessions.length"),
    offset: run("state.sessionsOffset"),
  };
  return { single, superseded, moreBlocked, emptyPage };
}

async function scenarioSilentRestoreWindow(source) {
  // 静默重载恢复已展开的渲染起点，不把窗口重置回最近 200 条
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const messages = [];
  for (let i = 0; i < 500; i += 1) {
    messages.push({
      role: i % 2 === 0 ? "user" : "assistant",
      content: "消息 " + i,
      timestamp: "",
      meta: i % 2 === 0 ? null : { client_turn_id: "turn-" + i },
    });
  }
  run(`
    state.userId = "u1";
    state.busy = false;
    state.sessions = [];
    state.sessionTotal = 1;
    els.messages = {
      children: [],
      set innerHTML(value) { if (value === "") this.children.length = 0; },
      get innerHTML() { return ""; },
      get firstChild() { return this.children[0] || null; },
      appendChild(node) { this.children.push(node); return node; },
      insertBefore(node, ref) {
        const index = ref ? this.children.indexOf(ref) : -1;
        if (index >= 0) this.children.splice(index, 0, node);
        else this.children.push(node);
        return node;
      },
      querySelectorAll() { return this.children.filter((n) => n.__msg); },
      querySelector() { return null; },
    };
    renderUserNode = (text) => ({ __msg: true, role: "user", text });
    renderAssistantNode = (text) => ({ __msg: true, role: "assistant", text });
    const originalCreate = document.createElement.bind(document);
    document.createElement = (tag) => {
      const node = originalCreate(tag);
      node.remove = () => {
        const index = els.messages.children.indexOf(node);
        if (index >= 0) els.messages.children.splice(index, 1);
      };
      return node;
    };
    closeMobileSidebar = () => {};
    loadSessions = async () => {};
    refreshStatus = () => {};
    trimRegenButtons = () => {};
    refreshEditButtons = () => {};
  `);
  const first = run('openSession("s1", {})');
  (await waitRequest(env)).resolve({ messages });
  await first;
  const initialCount = run(
    "els.messages.children.filter((n) => n.__msg === true).length",
  );
  run(`els.messages.children
    .find((n) => n.className === "earlier-more")
    .click()`);
  const expandedStart = run("state.renderedStart.start");
  const expandedCount = run(
    "els.messages.children.filter((n) => n.__msg === true).length",
  );
  const reload = run('openSession("s1", { silent: true })');
  (await waitRequest(env)).resolve({ messages });
  await reload;
  const restored = {
    count: run("els.messages.children.filter((n) => n.__msg === true).length"),
    start: run("state.renderedStart.start"),
    hasEarlier: run(
      'els.messages.children.some((n) => n.className === "earlier-more")',
    ),
  };
  return { initialCount, expandedStart, expandedCount, restored };
}

function scenarioChatPersisted(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const results = {};

  // 落库被墓碑否决：done 带 persisted=false —— 保留回答、提问标记为临时、
  // 清空会话并提示；不能被 sendMessage 的 finally 当成成功清掉 transient
  run(`
    globalThis.__userNodes = [document.createElement("div")];
    els.messages.querySelectorAll = (sel) =>
      sel === ".msg.user" ? __userNodes : [];
    globalThis.__assistant = {
      trace: {
        finish() { globalThis.__finished = (globalThis.__finished || 0) + 1; },
        fail() {},
      },
      setReply(reply) { globalThis.__reply = reply; },
    };
    state.userId = "u1";
    state.sessionId = "s-old";
    state.lastUserText = "你好";
    sessionStorage.setItem("aligo.sessionId", "s-old");
    handleStreamEvent(
      __assistant,
      {
        type: "done",
        session_id: "s-new",
        reply: "回答",
        elapsed: 0.2,
        persisted: false,
      },
      "你好",
    );
  `);
  results.veto = {
    sessionId: run("state.sessionId"),
    storedSession: run('sessionStorage.getItem("aligo.sessionId")'),
    transient: run('__userNodes[0].dataset.transient || null'),
    toasts: run("__toasts.slice()"),
    finished: run("__finished || 0"),
    reply: run("__reply || null"),
  };

  // 正常落库 / 停止 / 否决：applyTurnPersistence 的标记口径
  run(`
    globalThis.__userNodes = [document.createElement("div")];
    __userNodes[0].dataset.transient = "1";
    applyTurnPersistence(__userNodes[0], "done", true);
  `);
  const okTransient = run('__userNodes[0].dataset.transient || null');
  run(`
    globalThis.__userNodes = [document.createElement("div")];
    applyTurnPersistence(__userNodes[0], "done", false);
  `);
  const vetoHelper = run('__userNodes[0].dataset.transient || null');
  run(`
    globalThis.__userNodes = [document.createElement("div")];
    applyTurnPersistence(__userNodes[0], "aborted", undefined);
  `);
  const stopped = run('__userNodes[0].dataset.transient || null');
  results.ok = {
    transient: okTransient,
    veto_helper: vetoHelper,
    stopped,
  };
  return results;
}

async function scenarioKbSemanticRefreshQuiet(source) {
  // 语义模式下的收尾刷新：silentFailure 必须透传——429 时保留语义视图、
  // 不 toast、不切回关键词列表；非静默对照仍按原逻辑退出并提示
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    els.kbList = { textContent: "", scrollTop: 0, firstChild: null, appendChild() {} };
    els.kbListStatus = { textContent: "" };
    els.kbMore = { hidden: true, disabled: false };
    els.kbSemantic = { disabled: false, textContent: "语义搜索" };
    els.kbSearch = { value: "住宿" };
    globalThis.__rendered = 0;
    renderSemanticResults = () => { __rendered += 1; };
    renderKnowledgeDocuments = () => {};
    refreshKnowledgeCategories = () => {};
    announceKbListSummary = () => {};
    updateKnowledgeMore = () => {};
    state.kbSemanticMode = true;
    state.kbSemanticQuery = "住宿";
    state.kbQuery = "";
    state.kbItems = [];
  `);
  const drain = async () => {
    for (let i = 0; i < 20; i += 1) {
      await tick();
      if (!env.__requests.length) break;
      const request = env.__requests.shift();
      request.resolve({ total: 0, items: [], categories: [], results: [] });
    }
  };
  const limited = () =>
    Object.assign(new Error("知识库操作过于频繁，请约 60 秒后重试"), {
      status: 429,
    });

  const quietPromise = run(
    "refreshKnowledgeDocuments({ silentFailure: true, refreshCategories: false })",
  );
  const quietReq = await waitRequest(env);
  quietReq.reject(limited());
  await quietPromise;
  const quiet = run(
    "({ mode: state.kbSemanticMode, semanticQuery: state.kbSemanticQuery, toasts: __toasts.slice(), rendered: __rendered })",
  );
  await drain();

  let settled = false;
  const noisyPromise = run("refreshKnowledgeDocuments({ refreshCategories: false })");
  const noisyReq = await waitRequest(env);
  noisyReq.reject(limited());
  noisyPromise.then(
    () => {
      settled = true;
    },
    () => {
      settled = true;
    },
  );
  for (let i = 0; i < 30 && !settled; i += 1) {
    await tick();
    for (const request of env.__requests.splice(0)) {
      request.resolve({ total: 0, items: [], categories: [] });
    }
  }
  const noisy = run("({ mode: state.kbSemanticMode, toasts: __toasts.slice() })");
  return { quiet, noisy, settled };
}

async function scenarioKbCategoryDraft(source) {
  // 语义模式下切换分类：搜索框里未提交的输入不能被静默丢弃
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    const makeField = () => ({
      value: "",
      handlers: {},
      addEventListener(type, fn) {
        (this.handlers[type] = this.handlers[type] || []).push(fn);
      },
      dispatch(type, event) {
        (this.handlers[type] || []).forEach((fn) => fn(event || {}));
      },
    });
    els.kbSearch = makeField();
    els.kbCategoryFilter = makeField();
    els.kbList = { textContent: "", scrollTop: 0, firstChild: null, appendChild() {} };
    els.kbListStatus = { textContent: "" };
    els.kbMore = { hidden: true, disabled: false, addEventListener() {} };
    els.kbSemantic = {
      disabled: false,
      textContent: "语义搜索",
      addEventListener() {},
    };
    renderSemanticResults = () => {};
    renderKnowledgeDocuments = () => {};
    refreshKnowledgeCategories = () => {};
    announceKbListSummary = () => {};
    updateKnowledgeMore = () => {};
    state.kbQuery = "";
    state.kbCategory = "";
    bindEvents();
  `);
  // 输入「差旅」但防抖未触发（未回车），随即发起语义搜索
  run('els.kbSearch.value = "差旅";');
  const semanticPromise = run("runSemanticSearch()");
  const searchReq = await waitRequest(env);
  searchReq.resolve({ results: [], total: 0 });
  await semanticPromise;
  const beforeMode = run("state.kbSemanticMode");
  // 切换分类：退出语义模式后的关键词刷新必须带上未提交的搜索词
  run('els.kbCategoryFilter.value = "住宿"; els.kbCategoryFilter.dispatch("change", {});');
  const docReq = await waitRequest(env);
  const url = docReq.args[0];
  docReq.resolve({ total: 0, items: [] });
  await tick();
  return {
    beforeMode,
    url,
    query: run("state.kbQuery"),
    mode: run("state.kbSemanticMode"),
  };
}

async function scenarioKbImportBusy(source) {
  // 导入槽位繁忙（503「已有知识库导入任务在处理」）：零写入拒绝不能被
  // 误报成「服务端可能已写入」，也不触发多余刷新
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    els.kbImport = { textContent: "", disabled: false };
    els.kbImportFile = {
      value: "x",
      files: [{
        size: 100,
        arrayBuffer: async () => new TextEncoder().encode(
          JSON.stringify([{ content: "片段" }]),
        ),
      }],
    };
    els.memorySheet = { hidden: false };
    globalThis.__refreshCalls = 0;
    refreshKnowledge = async () => { __refreshCalls += 1; };
    refreshKnowledgeDocuments = async () => { __refreshCalls += 1; };
    confirm = () => true;
    state.kbImporting = false;
  `);
  const promise = run("importKnowledgeFile()");
  const preview = await waitRequest(env);
  preview.resolve({ would_import: 1, skipped: 0 });
  const imp = await waitRequest(env);
  imp.reject(
    Object.assign(
      new Error("已有知识库导入任务在处理，请稍后再试（请求 ID：0123456789abcdef）"),
      { status: 503 },
    ),
  );
  await promise;
  return {
    toasts: run("__toasts.slice()"),
    pendingUrls: env.__requests.map((request) => request.args[0]),
    refreshCalls: run("__refreshCalls"),
    importing: run("state.kbImporting"),
    buttonText: run("els.kbImport.textContent"),
    disabled: run("els.kbImport.disabled"),
  };
}

async function scenarioKbDeleteStale400(source) {
  // 删除返回 400（片段已变成多分片 / 整组结构变化）：刷新列表让该行
  // 渲染出「删除整组」，与编辑保存 409 分支同口径
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    const row = {
      dataset: { id: "11", group: "" },
      removed: false,
      querySelector: () => null,
      remove() { this.removed = true; },
    };
    globalThis.__row = row;
    els.kbList = {
      querySelector: (selector) => (selector.includes('11') ? row : null),
      querySelectorAll: () => [],
    };
    els.memorySheet = { hidden: false };
    state.kbItems = [{ id: 11 }];
    state.kbDeleting = new Set();
    globalThis.__refreshArgs = [];
    refreshKnowledge = async () => {};
    refreshKnowledgeDocuments = async (options) => {
      __refreshArgs.push(options || {});
    };
    confirm = () => true;
  `);
  const promise = run("removeKnowledgeDocument({id: 11, metadata: {}})");
  const del = await waitRequest(env);
  del.reject(
    Object.assign(
      new Error(
        "该片段属于多分片文档，请整组删除（CLI 加 --group / 接口加 group=1）",
      ),
      { status: 400 },
    ),
  );
  await promise;
  return {
    toasts: run("__toasts.slice()"),
    refreshArgs: run("__refreshArgs.slice()"),
    removed: run("__row.removed"),
    deleting: run("Array.from(state.kbDeleting)"),
  };
}

function scenarioKbImportSummary(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  return {
    conflict: run(
      "knowledgeImportSkipSummary({skipped: 2, skipped_conflict: 2})",
    ),
    mixed: run(
      "knowledgeImportSkipSummary({skipped: 6, skipped_duplicate: 1, skipped_invalid: 2, skipped_conflict: 3})",
    ),
    none: run("knowledgeImportSkipSummary({skipped: 0})"),
    legacy: run(
      "knowledgeImportSkipSummary({skipped: 2, skipped_duplicate: 2})",
    ),
  };
}

async function scenarioKbDelete(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const results = {};
  // 删除成功后会刷新知识库状态 / 列表 / 分类：把后续请求逐个 resolve
  const flush = async (promise) => {
    for (let i = 0; i < 12; i += 1) {
      await tick();
      const pending = env.__requests.filter((request) => !request.__done);
      if (!pending.length) break;
      for (const request of pending) {
        request.__done = true;
        request.resolve({
          available: false,
          documents: 0,
          total: 0,
          items: [],
          categories: [],
        });
      }
    }
    await promise;
  };

  // 旧版多分片（parts>1 无 group）：服务端只接受单条删除，带 group=1 会 400
  let promise = run('removeKnowledgeDocument({id: 7, metadata: {parts: "3"}})');
  results.legacy_url = env.__requests[0].args[0];
  env.__requests[0].__done = true;
  env.__requests[0].resolve({ id: 7, deleted: true, group: false, count: 1 });
  await flush(promise);
  results.legacy_toasts = run("__toasts.slice()");

  // 带 group 的多分片：整组删除
  const beforeGroup = env.__requests.length;
  promise = run('removeKnowledgeDocument({id: 8, metadata: {parts: 2, group: "g1"}})');
  results.group_url = env.__requests[beforeGroup].args[0];
  env.__requests[beforeGroup].__done = true;
  env.__requests[beforeGroup].resolve({ id: 8, deleted: true, group: true, count: 2 });
  await flush(promise);
  results.group_toasts = run("__toasts.slice()");

  // 普通单片：不带 group
  const beforeSingle = env.__requests.length;
  promise = run("removeKnowledgeDocument({id: 9, metadata: {}})");
  results.single_url = env.__requests[beforeSingle].args[0];
  env.__requests[beforeSingle].__done = true;
  env.__requests[beforeSingle].resolve({ id: 9, deleted: true, group: false, count: 1 });
  await flush(promise);
  results.single_toasts = run("__toasts.slice()");
  return results;
}

async function scenarioKbDeleteGroupGhost(source) {
  // 整组删除 + 收尾刷新失败：同组其余分片必须本地移除，总数按服务端
  // count 递减，不能留下幽灵行 / 少算 N-1
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    const makeRow = (id, group) => ({
      dataset: { id: String(id), group: group || "" },
      removed: false,
      querySelector: () => null,
      remove() { this.removed = true; },
    });
    const rows = [makeRow(10, "g1"), makeRow(9, "g1"), makeRow(8, "g1"), makeRow(5, "")];
    globalThis.__rows = rows;
    globalThis.__note = { textContent: "" };
    els.kbList = {
      querySelector(selector) {
        const match = /^\\.kb-row\\[data-id="(\\d+)"\\]$/.exec(selector);
        if (match) {
          return rows.find((row) => row.dataset.id === match[1] && !row.removed) || null;
        }
        if (selector === ".kb-note") return globalThis.__note;
        return null;
      },
      querySelectorAll(selector) {
        return selector === ".kb-row" ? rows.filter((row) => !row.removed) : [];
      },
      focus() {},
    };
    globalThis.__toasts = [];
    toast = (message) => { __toasts.push(message); };
    confirm = () => true;
    state.kbItems = [10, 9, 8, 5];
    state.kbTotal = 4;
    state.kbQuery = "";
    state.kbDeleting = new Set();
    refreshKnowledge = async () => {};
    refreshKnowledgeDocuments = async () => {};
    getJSON = async (url, options) => {
      if (options && options.method === "DELETE") {
        return { id: 8, deleted: true, group: true, count: 3 };
      }
      return { total: 0, items: [], categories: [] };
    };
  `);
  await run('removeKnowledgeDocument({id: 8, metadata: {parts: "3", group: "g1"}})');
  return {
    removed: env.__rows.map((row) => [row.dataset.id, row.removed]),
    items: run("state.kbItems.slice()"),
    total: run("state.kbTotal"),
    note: run("__note.textContent"),
    toasts: run("__toasts.slice()"),
  };
}

async function scenarioKbDedupeLocalRemoval(source) {
  // 清理重复成功但收尾刷新失败：真实删除的行必须本地移除，
  // 已删除的重复片段不能继续显示并可操作
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    const makeRow = (id) => ({
      dataset: { id: String(id), group: "" },
      removed: false,
      querySelector: () => null,
      remove() { this.removed = true; },
    });
    const rows = [makeRow(3), makeRow(2), makeRow(1)];
    globalThis.__rows = rows;
    globalThis.__note = { textContent: "" };
    els.kbList = {
      querySelector(selector) {
        const match = /^\\.kb-row\\[data-id="(\\d+)"\\]$/.exec(selector);
        if (match) {
          return rows.find((row) => row.dataset.id === match[1] && !row.removed) || null;
        }
        if (selector === ".kb-note") return globalThis.__note;
        return null;
      },
      querySelectorAll(selector) {
        return selector === ".kb-row" ? rows.filter((row) => !row.removed) : [];
      },
      focus() {},
    };
    els.memorySheet.hidden = false;
    globalThis.__toasts = [];
    toast = (message) => { __toasts.push(message); };
    confirm = () => true;
    state.kbEditingId = null;
    state.kbItems = [3, 2, 1];
    state.kbTotal = 3;
    state.kbQuery = "";
    refreshKnowledge = async () => {};
    refreshKnowledgeDocuments = async () => {};
    let call = 0;
    getJSON = async () => {
      call += 1;
      if (call === 1) return { planned: 2, groups: 1 };
      return {
        removed: 2,
        planned: 2,
        failed: [],
        skipped: [],
        plan: [{ keep: 3, remove: [1, 2] }],
      };
    };
  `);
  await run("runKbDedupe()");
  return {
    removed: env.__rows.map((row) => [row.dataset.id, row.removed]),
    items: run("state.kbItems.slice()"),
    total: run("state.kbTotal"),
    note: run("__note.textContent"),
    result: run("els.kbEvalResult.textContent"),
  };
}

async function scenarioUserBackupDecode(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const results = {};
  // 非 UTF-8 备份：严格解码失败必须提示另存，且不发任何导入请求
  run(
    "els.importDataFile.files = [{ size: 4, arrayBuffer: async () => new Uint8Array([196, 191, 181, 196]).buffer }];",
  );
  run("confirm = () => true;");
  await run("importUserBackup()");
  results.gbk_toasts = run("__toasts.slice()");
  results.gbk_requests = run("__requests.length");
  // 读取 / 解析期间切换用户：不能把备份恢复进旧身份，也不能继续弹确认
  run(`
    state.userId = "u1";
    globalThis.__toasts.length = 0;
    els.importDataFile.files = [{
      size: 40,
      arrayBuffer: async () => {
        state.userId = "u2";
        return new TextEncoder()
          .encode('{"preferences": {"home_location": "北京"}}').buffer;
      },
    }];
  `);
  await run("importUserBackup()");
  results.switch_toasts = run("__toasts.slice()");
  results.switch_requests = run("__requests.length");
  return results;
}

async function scenarioUserBackupRestore(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const results = {};
  run(`
    state.userId = "u1";
    confirm = () => true;
    refreshStatus = async () => {};
    loadSessions = async () => {};
    refreshKnowledge = async () => {};
    refreshKnowledgeDocuments = async () => {};
    refreshKnowledgeCategories = async () => {};
    getJSON = async () => __importResult;
    els.importDataFile.files = [{
      size: 41,
      arrayBuffer: async () =>
        new TextEncoder()
          .encode('{"preferences": {"home_location": "北京"}}').buffer,
    }];
  `);
  const base = {
    preferences: 1,
    trips_created: 0,
    trips_updated: 0,
    messages_added: 0,
    knowledge_imported: 0,
    knowledge_skipped: 0,
    skipped: [],
  };
  run(
    `__importResult = ${JSON.stringify({
      ...base,
      preferences: 0,
      recovery_incomplete: true,
    })};`,
  );
  run("__toasts.length = 0;");
  await run("importUserBackup()");
  results.incomplete_toasts = run("__toasts.slice()");
  run(`__importResult = ${JSON.stringify(base)};`);
  run("__toasts.length = 0;");
  await run("importUserBackup()");
  results.ok_toasts = run("__toasts.slice()");
  run(
    `__importResult = ${JSON.stringify({
      ...base,
      preferences: 0,
      knowledge_error: "Milvus 写入失败",
      recovery_incomplete: true,
    })};`,
  );
  run("__toasts.length = 0;");
  await run("importUserBackup()");
  results.combo_toasts = run("__toasts.slice()");
  // ok=false（备份条目全部被墓碑期 / 保留字会话跳过）：不能以「已恢复」
  // 开头，原因取服务端 skipped 里的说明
  run(
    `__importResult = ${JSON.stringify({
      ...base,
      ok: false,
      skipped: [
        "没有可恢复的数据（备份中的会话均处于墓碑期或使用了保留字，已全部跳过）",
      ],
    })};`,
  );
  run("__toasts.length = 0;");
  await run("importUserBackup()");
  results.emptied_toasts = run("__toasts.slice()");

  // 错误分支：408（上传超时，零写入）/ 部分完成 503 / 普通失败 / 静默刷新
  run(`
    __refreshes = 0;
    refreshStatus = async () => { __refreshes += 1; };
    __makeErr = (message, status) => {
      const error = new Error(message);
      if (status) error.status = status;
      return error;
    };
  `);
  run(
    `getJSON = async () => { throw __makeErr("读取备份数据超时（300 秒），请重试", 408); };`,
  );
  run("__toasts.length = 0;");
  await run("importUserBackup()");
  results.upload_timeout_toasts = run("__toasts.slice()");

  run(
    `getJSON = async () => { throw __makeErr("恢复部分完成：已提交 2 条新增、1 条更新；重复导入按路线+日期去重，可安全重跑", 503); };`,
  );
  run("__toasts.length = 0; __refreshes = 0;");
  await run("importUserBackup()");
  results.partial_toasts = run("__toasts.slice()");
  results.partial_refreshes = run("__refreshes");

  run(`getJSON = async () => { throw __makeErr("内部错误", 500); };`);
  run("__toasts.length = 0; __refreshes = 0;");
  await run("importUserBackup()");
  results.failure_toasts = run("__toasts.slice()");
  results.failure_refreshes = run("__refreshes");
  // 4xx：服务端明确拒绝，按普通失败处理、不刷新
  run(`getJSON = async () => { throw __makeErr("恢复被拒绝", 409); };`);
  run("__toasts.length = 0; __refreshes = 0;");
  await run("importUserBackup()");
  results.failure_4xx_toasts = run("__toasts.slice()");
  results.failure_4xx_refreshes = run("__refreshes");
  // 连接中断（无状态码）：结果未知，提示并刷新
  run(`getJSON = async () => { throw new Error("Failed to fetch"); };`);
  run("__toasts.length = 0; __refreshes = 0;");
  await run("importUserBackup()");
  results.network_toasts = run("__toasts.slice()");
  results.network_refreshes = run("__refreshes");

  // 成功但刷新失败：全部静默（allSettled），刷新错误不能覆盖恢复摘要
  run(`getJSON = async () => __importResult;`);
  run(`refreshStatus = async () => { throw new Error("刷新失败"); };`);
  run(`__importResult = ${JSON.stringify(base)};`);
  run("__toasts.length = 0;");
  await run("importUserBackup()");
  results.silent_refresh_toasts = run("__toasts.slice()");
  return results;
}

async function scenarioUpcomingWeather(source) {
  const results = {};
  // 未补零出发日期（2026-1-5）也要与零填充预报日期匹配，展示出发日预报
  {
    const env = buildEnv(source);
    const promise = runIn(env, 'loadUpcomingWeather("杭州", "2020-1-5")');
    await tick();
    env.__requests[0].resolve({
      current: { description: "多云", temperature: 18 },
      daily: [
        { date: "2020-01-04", description: "晴", min: 10, max: 20 },
        { date: "2020-01-05", description: "小雨", min: 5, max: 16 },
        { date: "2020-01-06", description: "阴", min: 6, max: 17 },
      ],
    });
    await promise;
    results.unpadded = {
      text: runIn(env, "els.upcomingWeather.textContent"),
      hidden: runIn(env, "els.upcomingWeather.hidden"),
    };
  }
  // 出发日不在 3 天窗口内：退回今日（daily[0]），标签不是「出发日」
  {
    const env = buildEnv(source);
    const promise = runIn(env, 'loadUpcomingWeather("杭州", "2020-02-01")');
    await tick();
    env.__requests[0].resolve({
      current: {},
      daily: [{ date: "2020-01-04", description: "晴", min: 10, max: 20 }],
    });
    await promise;
    results.out_of_window = {
      text: runIn(env, "els.upcomingWeather.textContent"),
    };
  }
  return results;
}

async function scenarioHttpError(source) {
  const runScenario = async (status, detail, resend = false) => {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    run(`
      __reconcileCalls = [];
      refreshStatus = async () => {};
      loadSessions = async () => {};
      state.ready = true;
      state.userId = "u1";
      state.sessionId = "s1";
      state.partialHistory = false;
      els.input.value = "你好";
      renderUserNode = (text) => {
        const node = document.createElement("div");
        node.className = "msg user";
        node.textContent = text;
        node.__removedFlag = false;
        node.remove = function () {
          this.removed = true;
          this.__removedFlag = true;
        };
        return node;
      };
      fetch = async (url) => {
        if (String(url).includes("/api/chat/stream")) {
          return {
            ok: false,
            status: ${status},
            json: async () => ({ detail: ${JSON.stringify(detail)} }),
            body: null,
          };
        }
        __reconcileCalls.push(String(url));
        return { ok: true, status: 200, json: async () => ({ messages: [] }) };
      };
    `);
    await run(`sendMessage("你好")`);
    // 旧实现会在 600ms 后触发停止对账；等待足够长时间确认没有发生
    await new Promise((resolve) => setTimeout(resolve, 900));
    let afterResend = null;
    if (resend) {
      // 重发成功：HttpError 分支已移除旧气泡，界面只应剩一条提问
      run(`
        fetch = async (url) => {
          if (String(url).includes("/api/chat/stream")) {
            const line = "data: " + JSON.stringify({
              type: "done",
              session_id: "s1",
              reply: "重发回答",
              elapsed: 0.1,
              persisted: true,
            }) + String.fromCharCode(10) + String.fromCharCode(10);
            const bytes = new TextEncoder().encode(line);
            let sent = false;
            return {
              ok: true,
              status: 200,
              body: {
                getReader: () => ({
                  read: async () => {
                    if (sent) return { done: true, value: undefined };
                    sent = true;
                    return { done: false, value: bytes };
                  },
                }),
              },
              json: async () => ({}),
            };
          }
          return { ok: true, status: 200, json: async () => ({ messages: [] }) };
        };
      `);
      await run(`sendMessage("你好")`);
      afterResend = run(`({
        users: els.messages.children.filter(
          (n) => n && n.__removedFlag === false,
        ).length,
        toasts: __toasts.slice(),
      })`);
    }
    return run(`({
      toasts: __toasts.slice(),
      healthText: els.healthText.textContent,
      healthClass: els.health.className,
      reconcileCalls: __reconcileCalls.slice(),
      input: els.input.value,
      users: els.messages.children.filter(
        (n) => n && n.__removedFlag === false,
      ).length,
      afterResend: ${afterResend === null ? "null" : JSON.stringify(afterResend)},
    })`);
  };
  return {
    rateLimited: await runScenario(429, "请求过于频繁，请稍后再试"),
    unavailable: await runScenario(503, "同一用户已有对话正在进行"),
    resendRateLimited: await runScenario(429, "请求过于频繁，请稍后再试", true),
  };
}

async function scenarioInitStale(source) {
  const env = buildEnv(source);
  return runIn(
    env,
    `(async () => {
      refreshHealth = async () => {};
      refreshStatus = async () => {};
      loadSessions = async () => {};
      sessionStorage.setItem("aligo.sessionId", "T");
      state.sessionId = "T";
      state.ready = false;
      const pending = init();
      // 模拟初始化期间用户点击了其它会话（加载失败、从未渲染）
      sessionLoadToken += 1;
      await pending;
      return {
        ready: state.ready,
        sessionId: state.sessionId,
        stored: sessionStorage.getItem("aligo.sessionId"),
        welcomeShown: __showWelcome,
      };
    })()`
  );
}


async function scenarioSilentAppendWindow(source) {
  // 静默重载（停止对账 / 恢复后刷新）：
  // 1) 上次已渲染到末尾、之后落库了新消息：新消息必须立即可见、不能藏进
  //    「显示更新消息」，也不能误置 partialHistory（编辑/重新生成消失）；
  // 2) 用户手动点「显示更新消息」展开到末尾后，窗口状态要跟随；
  //    随后的静默重载不能按旧上界把展开的末尾重新折叠。
  const build = (count, prefix) => {
    const list = [];
    for (let i = 0; i < count; i += 1) {
      list.push({
        role: i % 2 === 0 ? "user" : "assistant",
        content: (prefix || "消息 ") + i,
        timestamp: "",
        meta: i % 2 === 0 ? null : { client_turn_id: "turn-" + i },
      });
    }
    return list;
  };
  const installDom = (run) => {
    run(`
      state.userId = "u1";
      state.busy = false;
      state.sessions = [];
      state.sessionTotal = 1;
      els.messages = {
        children: [],
        set innerHTML(value) { if (value === "") this.children.length = 0; },
        get innerHTML() { return ""; },
        get firstChild() { return this.children[0] || null; },
        appendChild(node) { this.children.push(node); return node; },
        insertBefore(node, ref) {
          const index = ref ? this.children.indexOf(ref) : -1;
          if (index >= 0) this.children.splice(index, 0, node);
          else this.children.push(node);
          return node;
        },
        querySelectorAll() { return this.children.filter((n) => n.__msg); },
        querySelector() { return null; },
      };
      renderUserNode = (text) => ({ __msg: true, role: "user", text });
      renderAssistantNode = (text) => ({ __msg: true, role: "assistant", text });
      const originalCreate = document.createElement.bind(document);
      document.createElement = (tag) => {
        const node = originalCreate(tag);
        node.remove = () => {
          const index = els.messages.children.indexOf(node);
          if (index >= 0) els.messages.children.splice(index, 1);
        };
        return node;
      };
      closeMobileSidebar = () => {};
      loadSessions = async () => {};
      refreshStatus = () => {};
      trimRegenButtons = () => {};
      refreshEditButtons = () => {};
    `);
  };
  const snapshot = (run) =>
    run(`(() => {
      const msgs = els.messages.children.filter((n) => n.__msg === true);
      const buttons = els.messages.children.filter(
        (n) => n.className === "earlier-more",
      );
      const later = buttons.find((n) => n.textContent.startsWith("显示更新消息"));
      return {
        count: msgs.length,
        partial: state.partialHistory,
        laterText: later ? later.textContent : "",
        renderedEnd: state.renderedStart ? state.renderedStart.end : null,
      };
    })()`);

  const results = {};

  // ---- 场景 1：追加消息后的静默重载 ----
  {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    installDom(run);
    const messages = build(10);
    const first = run('openSession("s1", {})');
    (await waitRequest(env)).resolve({ messages });
    await first;
    results.initial = snapshot(run);
    // 停止对账确认落库：服务端历史多出两条
    messages.push(
      { role: "user", content: "新提问", timestamp: "", meta: null },
      { role: "assistant", content: "新回答", timestamp: "", meta: {} },
    );
    const reload = run('openSession("s1", { silent: true })');
    (await waitRequest(env)).resolve({ messages });
    await reload;
    results.afterAppend = snapshot(run);
  }

  // ---- 场景 2：展开到末尾后再静默重载 ----
  {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    installDom(run);
    const messages = build(600);
    const open = run('openSession("s1", { matchIndex: 10 })');
    (await waitRequest(env)).resolve({ messages });
    await open;
    results.windowInitial = snapshot(run);
    const clickLater = () =>
      run(`(() => {
        const button = els.messages.children.find(
          (n) => n.className === "earlier-more" && n.textContent.startsWith("显示更新消息"),
        );
        if (button) button.click();
      })()`);
    for (let i = 0; i < 10; i += 1) clickLater();
    results.expanded = snapshot(run);
    const reload = run('openSession("s1", { silent: true })');
    (await waitRequest(env)).resolve({ messages });
    await reload;
    results.afterExpandReload = snapshot(run);
  }
  return results;
}


async function scenarioDeletedThreadSend(source) {
  // done(persisted=false)：回答保留可见，但下一条消息必须从干净线程开始
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    globalThis.__toasts = [];
    toast = (message) => { __toasts.push(message); };
    globalThis.__userNodes = [document.createElement("div")];
    els.messages.querySelectorAll = (sel) =>
      sel === ".msg.user" ? __userNodes : [];
    globalThis.__assistant = {
      trace: { finish() {}, fail() {} },
      setReply() {},
    };
    state.userId = "u1";
    state.sessionId = "s-old";
    state.lastUserText = "你好";
    handleStreamEvent(
      __assistant,
      { type: "done", session_id: "s-new", reply: "回答", elapsed: 0.2, persisted: false },
      "你好",
    );
  `);
  const veto = run(`({
    sessionId: state.sessionId,
    retired: state.retiredThreadVisible,
  })`);

  // 下一条消息：fetch 返回只有一条 done 事件的最小 SSE 流
  run(`
    state.ready = true;
    state.busy = false;
    state.sessionLoading = false;
    globalThis.__sentinel = { __sentinel: true };
    els.messages = {
      children: [__sentinel],
      set innerHTML(value) { if (value === "") this.children.length = 0; },
      get innerHTML() { return ""; },
      get firstChild() { return this.children[0] || null; },
      appendChild(node) { this.children.push(node); return node; },
      insertBefore(node, ref) {
        const index = ref ? this.children.indexOf(ref) : -1;
        if (index >= 0) this.children.splice(index, 0, node);
        else this.children.push(node);
        return node;
      },
      querySelectorAll() { return []; },
      querySelector() { return null; },
    };
    renderUserNode = (text) => ({ __msg: true, role: "user", text });
    renderAssistantNode = (text) => ({ __msg: true, role: "assistant", text });
    hideWelcome = () => {};
    trimRegenButtons = () => {};
    refreshEditButtons = () => {};
    globalThis.__fetchCalls = 0;
    globalThis.__bodies = [];
    const line = "data: " + JSON.stringify({
      type: "done",
      session_id: "s-new",
      reply: "新回答",
      elapsed: 0.1,
      persisted: true,
    }) + String.fromCharCode(10) + String.fromCharCode(10);
    const bytes = new TextEncoder().encode(line);
    fetch = async (url, options) => {
      __fetchCalls += 1;
      if (options && options.body) __bodies.push(JSON.parse(options.body));
      let sent = false;
      return {
        ok: true,
        status: 200,
        body: {
          getReader: () => ({
            read: async () => {
              if (sent) return { done: true, value: undefined };
              sent = true;
              return { done: false, value: bytes };
            },
          }),
        },
        json: async () => ({}),
      };
    };
    refreshStatus = async () => {};
    loadSessions = async () => {};
  `);
  await run('sendMessage("新提问")');
  const afterSend = run(`(() => {
    const kids = els.messages.children.slice();
    const messages = kids.filter((n) => n.__msg === true);
    return {
      retired: state.retiredThreadVisible,
      sessionId: state.sessionId,
      childCount: kids.length,
      sentinelRemoved: !kids.includes(__sentinel),
      roles: messages.map((n) => n.role),
    };
  })()`);
  return { veto, afterSend, bodies: run("globalThis.__bodies") };
}

async function scenarioRetiredThreadOpenSession(source) {
  // persisted=false 后打开另一个会话：可见线程属于新会话，标记必须复位，
  // 否则下一次发送会清空刚加载的历史
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    state.userId = "u1";
    state.busy = false;
    state.sessionLoading = false;
    state.sessionId = null;
    state.retiredThreadVisible = true;
    state.sessions = [{ session_id: "s2", title: "会话二" }];
    state.sessionTotal = 1;
    state.sessionsOffset = 0;
    els.messages.children = [];
    getJSON = async () => ({
      messages: [
        { role: "user", content: "历史提问", timestamp: "" },
        { role: "assistant", content: "历史回答", timestamp: "" },
      ],
      title: "会话二",
    });
    safeStorageSet = () => {};
    safeStorageRemove = () => {};
    syncSessionControls = () => {};
    setConversationTitle = () => {};
    hideWelcome = () => {};
    refreshEditButtons = () => {};
    refreshStatus = async () => {};
    loadSessions = async () => {};
  `);
  await run('openSession("s2")');
  const afterOpen = run(`({
    retired: state.retiredThreadVisible,
    sessionId: state.sessionId,
  })`);
  return { afterOpen };
}

async function scenarioRetiredThreadRegenerate(source) {
  // 会话被删除后的可见线程（retired）+「重新生成」：新会话必须同时渲染
  // 用户提问（echo:false 也不能只剩一条没有提问的回答）
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    globalThis.__toasts = [];
    toast = (message) => { __toasts.push(message); };
    document.querySelector = () => null;
    window.confirm = () => true;
    state.userId = "u1";
    state.sessionId = null;
    state.retiredThreadVisible = true;
    state.ready = true;
    state.busy = false;
    state.sessionLoading = false;
    state.partialHistory = false;
    els.input.value = "";
    globalThis.__sentinel = { __sentinel: true };
    els.messages = {
      children: [__sentinel],
      set innerHTML(value) { if (value === "") this.children.length = 0; },
      get innerHTML() { return ""; },
      get firstChild() { return this.children[0] || null; },
      appendChild(node) { this.children.push(node); return node; },
      insertBefore(node, ref) {
        const index = ref ? this.children.indexOf(ref) : -1;
        if (index >= 0) this.children.splice(index, 0, node);
        else this.children.push(node);
        return node;
      },
      querySelectorAll() { return []; },
      querySelector() { return null; },
    };
    renderUserNode = (text) => ({ __msg: true, role: "user", text });
    createAssistantMessage = () => ({
      root: { __msg: true, role: "assistant", dataset: {}, classList: { add() {}, remove() {}, contains() { return false; } } },
      trace: { start() {}, finish() {}, fail() {} },
      setReply() {},
      finish() {},
      fail() {},
    });
    hideWelcome = () => {};
    trimRegenButtons = () => {};
    refreshEditButtons = () => {};
    refreshStatus = async () => {};
    loadSessions = async () => {};
    cancelPendingEdits = () => {};
    resizeInput = () => {};
    setBusy = () => {};
    applyTurnPersistence = () => {};
    const line = "data: " + JSON.stringify({
      type: "done", session_id: "s-new", reply: "新回答", elapsed: 0.1, persisted: true,
    }) + String.fromCharCode(10) + String.fromCharCode(10);
    const bytes = new TextEncoder().encode(line);
    fetch = async () => {
      let sent = false;
      return {
        ok: true,
        status: 200,
        body: { getReader: () => ({ read: async () => (sent ? { done: true } : ((sent = true), { done: false, value: bytes })) }) },
        json: async () => ({}),
      };
    };
  `);
  run(
    'globalThis.__node = document.createElement("div"); __node.dataset = {}; __node.remove = () => { globalThis.__removed = true; };'
  );
  await run('regenerate(__node, "旧提问")');
  return run(`(() => {
    const messages = els.messages.children.filter((n) => n.__msg === true);
    return {
      removed: Boolean(globalThis.__removed),
      sentinelGone: !els.messages.children.includes(globalThis.__sentinel),
      roles: messages.map((n) => n.role),
    };
  })()`);
}

async function scenarioKnowledgeSkippedToast(source) {
  // /api/export?with_knowledge=1 的 X-Knowledge-Skipped / X-Knowledge-Omitted
  // 必须提示（不能把「知识不全 / 不含知识」的备份当完整成功静默下载）
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const mockResponse = (headers) => {
    run(`
      globalThis.__toasts = [];
      toast = (message) => { __toasts.push(message); };
      fetch = async () => ({
        ok: true,
        status: 200,
        headers: {
          get(name) {
            const key = String(name).toLowerCase();
            const values = ${JSON.stringify(headers)};
            if (Object.prototype.hasOwnProperty.call(values, key)) {
              return values[key];
            }
            return "";
          },
        },
        blob: async () => ({ __blob: true }),
      });
      URL.createObjectURL = () => "blob:x";
      URL.revokeObjectURL = () => {};
      document.body.appendChild = () => {};
    `);
  };
  mockResponse({
    "x-knowledge-skipped": "3",
    "x-aligo-export-skipped": "0",
    "content-disposition": 'attachment; filename="aligo_u.json"',
  });
  const ok = await run(
    'downloadFromApi("/api/export?with_knowledge=1", "fallback.json")'
  );
  const skippedToasts = run("__toasts.slice()");
  mockResponse({
    "x-knowledge-omitted": "over-limit",
    "x-aligo-export-skipped": "0",
    "content-disposition": 'attachment; filename="aligo_u.json"',
  });
  const omittedOk = await run(
    'downloadFromApi("/api/export?with_knowledge=1", "fallback.json")'
  );
  const omittedToasts = run("__toasts.slice()");
  // planning-failed 是「暂无法核对、稍后重试」而非「超限请清理」：文案必须不同
  mockResponse({
    "x-knowledge-omitted": "planning-failed",
    "x-aligo-export-skipped": "0",
    "content-disposition": 'attachment; filename="aligo_u.json"',
  });
  const planningOk = await run(
    'downloadFromApi("/api/export?with_knowledge=1", "fallback.json")'
  );
  const planningToasts = run("__toasts.slice()");
  return {
    ok,
    toasts: skippedToasts,
    omittedOk,
    omittedToasts,
    planningOk,
    planningToasts,
  };
}

function scenarioRenderCap(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const big = "长".repeat(300000);
  const html = run(`renderMarkdown(${JSON.stringify(big)})`);
  const normal = run('renderMarkdown("普通内容")');
  // 用户消息气泡同样要有渲染上限（备份恢复允许单条消息接近 50MB）
  const userText = run(`capRenderText(${JSON.stringify(big)})`);
  const smallText = run('capRenderText("普通提问")');
  // 截断点落在代理对中间时不能产出孤立代理（浏览器显示 U+FFFD）
  const emojiCut = "a".repeat(199999) + "\u{1F600}" + "b".repeat(10);
  const hasLoneSurrogate = run(`((text) => {
    for (let index = 0; index < text.length; index += 1) {
      const code = text.charCodeAt(index);
      if (code >= 0xd800 && code <= 0xdbff) {
        const next = text.charCodeAt(index + 1);
        if (!(next >= 0xdc00 && next <= 0xdfff)) return true;
      } else if (code >= 0xdc00 && code <= 0xdfff) {
        const prev = text.charCodeAt(index - 1);
        if (!(prev >= 0xd800 && prev <= 0xdbff)) return true;
      }
    }
    return false;
  })((globalThis.__cut = capRenderText(${JSON.stringify(emojiCut)})))`);
  const emojiCap = run("globalThis.__cut");
  const emojiMd = run(`renderMarkdown(${JSON.stringify(emojiCut)})`);
  const emojiMdLone = run(`((text) => {
    for (let index = 0; index < text.length; index += 1) {
      const code = text.charCodeAt(index);
      if (code >= 0xd800 && code <= 0xdbff) {
        const next = text.charCodeAt(index + 1);
        if (!(next >= 0xdc00 && next <= 0xdfff)) return true;
      }
    }
    return false;
  })(${JSON.stringify(emojiMd)})`);
  return {
    cut: html.includes("已截断显示"),
    cappedLength: html.length,
    normalUntouched: !normal.includes("已截断显示") && normal.includes("普通内容"),
    userCut: userText.includes("已截断显示"),
    userCappedLength: userText.length,
    userNormalUntouched: smallText === "普通提问",
    emojiCapLone: hasLoneSurrogate,
    emojiMdLone,
    emojiCapSuffix: emojiCap.endsWith("…（已截断显示）"),
    emojiCapLength: emojiCap.length,
  };
}

async function scenarioArchivedRollback(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    globalThis.__toasts = [];
    toast = (message) => { __toasts.push(message); };
    getJSON = () => Promise.reject(new Error("network down"));
    state.userId = "u1";
    state.showArchived = false;
    state.sessions = [{ session_id: "s1", title: "t", message_count: 1, updated_at: "" }];
    state.sessionsLoadedQuery = "";
    state.sessionsLoadedArchived = false;
    state.sessionsLoading = false;
    globalThis.__renderCalls = 0;
    const realRender = renderHistory;
    renderHistory = () => { __renderCalls += 1; return realRender(); };
    bindEvents();
  `);
  run("els.archivedToggle.click()");
  for (let i = 0; i < 50; i += 1) {
    await tick();
    if (!run("state.sessionsLoading")) break;
  }
  await new Promise((resolve) => setTimeout(resolve, 10));
  // 重叠点击：第二次翻转取代第一次请求；失败后视图位必须与已渲染快照一致
  run("els.archivedToggle.click(); els.archivedToggle.click();");
  for (let i = 0; i < 50; i += 1) {
    await tick();
    if (!run("state.sessionsLoading")) break;
  }
  await new Promise((resolve) => setTimeout(resolve, 10));
  return run(`({
    showArchived: state.showArchived,
    loadedArchived: state.sessionsLoadedArchived,
    toasts: __toasts.slice(),
    renderCalls: __renderCalls,
  })`);
}

async function scenarioDraftSendGuard(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    state.userId = "u1";
    state.ready = true;
    state.busy = false;
    state.sessionLoading = false;
    globalThis.__fetchCalls = 0;
    globalThis.__confirmAnswer = false;
    window.confirm = (message) => { globalThis.__confirmText = message; return __confirmAnswer; };
    document.querySelector = (sel) =>
      sel === ".user-edit-input" ? document.createElement("textarea") : null;
    fetch = async () => { __fetchCalls += 1; throw new Error("不应发送"); };
    els.input.value = "新提问";
  `);
  await run('sendMessage("新提问")');
  const rejected = run(`({
    fetchCalls: __fetchCalls,
    confirmText: globalThis.__confirmText || "",
    inputValue: els.input.value,
  })`);
  return { rejected };
}


async function scenarioEditorsRound2(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    globalThis.__toasts = [];
    toast = (message) => { __toasts.push(message); };
    state.userId = "u1";
  `);

  const results = {};

  // 对象型偏好：行内编辑按 JSON 整体解析（拆分会把对象拆成字符串碎片）
  results.prefObject = run(`({
    array: preferenceOutputValue(
      [{ name: "汉庭", level: 1 }],
      '[{"name":"如家","level":2}]',
    ),
    arrayInvalid: preferenceOutputValue(
      [{ name: "汉庭" }],
      '[{"name":',
    ),
    objectInvalid: preferenceOutputValue({ a: 1 }, "not json"),
    objectKeep: preferenceOutputValue({ a: 1 }, '{"a":2}'),
    plainList: preferenceOutputValue(["汉庭"], "汉庭、如家"),
  })`);

  // 仅分隔符的列表输入：拒绝并提示，不发 PUT
  run(`
    globalThis.__puts = 0;
    getJSON = async () => { __puts += 1; return {}; };
    refreshStatus = async () => {};
    state.sessions = [];
    els.prefValue.value = "、、 ，";
    els.prefType.value = "hotel_brands";
  `);
  await run('addPreference({ preventDefault() {} })');
  results.delimiterOnly = run(`({
    puts: __puts,
    toasts: __toasts.slice(),
    value: els.prefValue.value,
  })`);

  // 保存期间取消按钮禁用；请求结束后恢复
  run(`
    __toasts = [];
    globalThis.__resolvePut = null;
    // 列表型追加需要 /api/status 权威快照作为合并基线（无基线时不再退化成
    // 整值覆盖，见 addNoBaseline 用例）
    state.status = { preferences: { hotel_brands: [] } };
    getJSON = () => new Promise((resolve) => { __resolvePut = resolve; });
    els.prefValue.value = "汉庭";
    els.prefType.value = "hotel_brands";
  `);
  const saving = run('addPreference({ preventDefault() {} })');
  await new Promise((resolve) => setTimeout(resolve, 0));
  results.prefCancelDuring = run(`({
    cancelDisabled: els.prefCancel.disabled,
    submitDisabled: els.prefSubmit.disabled,
  })`);
  run("__resolvePut({})");
  await saving;
  results.prefCancelAfter = run(`({
    cancelDisabled: els.prefCancel.disabled,
    submitDisabled: els.prefSubmit.disabled,
  })`);

  // 数值 / 布尔列表行内编辑保留元素类型（不能静默变成字符串）
  results.typedLists = run(`({
    numbers: preferenceOutputValue([1, 2, 3], "1、2、4"),
    numbersInvalid: preferenceOutputValue([1, 2, 3], "1、x"),
    booleans: preferenceOutputValue([true, false], "true、false"),
    booleansInvalid: preferenceOutputValue([true, false], "true、也许"),
  })`);

  // 偏好编辑框上限与服务端 10000 字符口径一致
  results.prefMaxChars = run("PREFERENCE_VALUE_MAX_CHARS");

  // 列表型偏好「添加」是追加语义：与现有值合并去重后再 PUT；标量仍覆盖
  run(`
    __toasts = [];
    globalThis.__putBodies = [];
    preferenceLatest.clear();
    preferenceLatest.set("hotel_brands", ["汉庭", "如家"]);
    state.status = { preferences: { hotel_brands: ["汉庭", "如家"] } };
    getJSON = async (url, options) => {
      __putBodies.push(JSON.parse(options.body).value);
      return {};
    };
    refreshStatus = async () => {};
    els.prefValue.value = "锦江、汉庭";
    els.prefType.value = "hotel_brands";
  `);
  await run("addPreference({ preventDefault() {} })");
  results.addMerges = run("__putBodies[0]");
  run(`
    __putBodies = [];
    els.prefValue.value = "上海";
    els.prefType.value = "home_location";
  `);
  await run("addPreference({ preventDefault() {} })");
  results.addScalar = run("__putBodies[0]");

  // 行内编辑失焦：草稿已修改才需要确认（与 save() 同口径，换行剥离）
  results.blurPrompt = run(`({
    changed: preferenceBlurNeedsConfirm(["汉庭"], "如家"),
    unchanged: preferenceBlurNeedsConfirm(["汉庭"], "汉庭"),
    newline: preferenceBlurNeedsConfirm("a\\r\\nb", "ab"),
  })`);

  // 删除结果未知（超时 / 断连 / 5xx）：对账刷新并提示，不当作确定失败
  run(`
    __toasts = [];
    globalThis.__refreshes = [];
    window.confirm = () => true;
    getJSON = async () => { throw new Error("网络中断"); };
    refreshStatus = async (options) => { __refreshes.push(options || {}); };
  `);
  await run('deletePreference("hotel_brands", "酒店偏好", null)');
  results.deleteUnknown = run(`({
    toasts: __toasts.slice(),
    refreshes: __refreshes.slice(),
  })`);

  // 删除成功：本地移除行 + 静默刷新（刷新失败不能留下已删除的行）
  run(`
    __toasts = [];
    __refreshes = [];
    getJSON = async () => ({});
    const row = document.createElement("div");
    row.className = "pref-row";
    els.overview.appendChild(row);
    preferenceLatest.set("home_location", "上海");
    state.status = { preferences: { home_location: "上海" } };
    globalThis.__row = row;
  `);
  await run('deletePreference("home_location", "常住地", __row)');
  results.deleteSuccess = run(`({
    rowRemoved: __row.removed === true,
    latestHas: preferenceLatest.has("home_location"),
    statusHas: Boolean(
      state.status && state.status.preferences &&
      Object.prototype.hasOwnProperty.call(state.status.preferences, "home_location")
    ),
    refreshes: __refreshes.slice(),
    toasts: __toasts.slice(),
  })`);

  // 知识库同一行再次点编辑：草稿已修改时必须确认，拒绝后草稿保留
  run(`
    __toasts = [];
    globalThis.__confirmCalls = 0;
    globalThis.__confirmAnswer = false;
    window.confirm = () => { __confirmCalls += 1; return __confirmAnswer; };
    state.kbSaving = false;
    state.kbEditingId = "doc-1";
    state.kbEditBaseline = "原始内容";
    els.kbContent.value = "用户新输入的草稿";
    els.kbContent.placeholder = "";
    els.kbContent.setSelectionRange = () => {};
  `);
  await run('startEditKnowledge({ id: "doc-1", content: "原始内容", metadata: {} })');
  results.kbReclick = run(`({
    confirmCalls: __confirmCalls,
    kept: els.kbContent.value,
  })`);

  // Windows 设备保留名：提示真实原因
  run(`
    __toasts = [];
    els.userIdInput.value = "con";
    state.busy = false;
  `);
  await run("saveUser()");
  results.reservedHint = run("__toasts.slice()");

  // 链接 URL 里的行内代码占位符必须还原成原文（不带 <code>）
  results.href = run(
    'renderInline("[链接](http://example.com/`code`-x)")',
  );
  // 对象数组偏好：编辑框回填整段合法 JSON，改一个字段即可保存（按「、」
  // 拼接的文本不是合法 JSON，用户任何修改都会被判「格式无效」）
  results.prefObjectArrayInput = run(`({
    text: preferenceInputValue([{ name: "汉庭", level: 1 }]),
    roundtrip: preferenceOutputValue(
      [{ name: "汉庭", level: 1 }],
      preferenceInputValue([{ name: "汉庭", level: 2 }]),
    ),
    plainList: preferenceInputValue(["汉庭", "如家"]),
  })`);

  // 列表型追加以 /api/status 权威快照为准：preferenceLatest 落后（行内编辑
  // 期间被跳过的刷新）时，不能用旧列表覆盖对话侧新增的条目
  run(`
    __toasts = [];
    globalThis.__putBodies2 = [];
    preferenceLatest.clear();
    preferenceLatest.set("hotel_brands", ["汉庭"]);
    state.status = { preferences: { hotel_brands: ["汉庭", "全季"] } };
    getJSON = async (url, options) => {
      __putBodies2.push(JSON.parse(options.body).value);
      return {};
    };
    refreshStatus = async () => {};
    els.prefValue.value = "如家";
    els.prefType.value = "hotel_brands";
  `);
  await run("addPreference({ preventDefault() {} })");
  results.addStatusBaseline = run("__putBodies2[0]");

  // 无权威快照且刷新失败：追加没有合并基线，不得退化成整值覆盖
  run(`
    __toasts = [];
    globalThis.__putCount2 = 0;
    globalThis.__refreshCalls2 = 0;
    preferenceLatest.clear();
    state.status = null;
    getJSON = async () => { __putCount2 += 1; return {}; };
    refreshStatus = async () => { __refreshCalls2 += 1; };
    els.prefValue.value = "全季";
    els.prefType.value = "hotel_brands";
  `);
  await run("addPreference({ preventDefault() {} })");
  results.addNoBaseline = run(`({
    puts: __putCount2,
    refreshes: __refreshCalls2,
    toasts: __toasts.slice(),
  })`);

  // 保存结果未知（超时 / 断连）：提示未知、写回基线与状态并静默对账，
  // 下一次追加不会再用旧列表整值覆盖刚写入的值
  run(`
    __toasts = [];
    globalThis.__refreshes3 = 0;
    state.userId = "u1";
    preferenceLatest.clear();
    state.status = { preferences: { hotel_brands: ["汉庭"] } };
    getJSON = async () => { throw new Error("请求超时，请稍后重试"); };
    refreshStatus = async () => { __refreshes3 += 1; };
    els.prefValue.value = "全季";
    els.prefType.value = "hotel_brands";
  `);
  await run("addPreference({ preventDefault() {} })");
  results.addUnknownOutcome = run(`({
    toasts: __toasts.slice(),
    refreshes: __refreshes3,
    latest: preferenceLatest.get("hotel_brands"),
    status: state.status.preferences.hotel_brands,
  })`);

  // 数值偏好编辑拒绝非有限数（Infinity / 1e999 序列化成 null 会被
  // 服务端按空值拒绝）
  results.prefScalarFinite = run(`({
    infinity: preferenceOutputValue(3, "1e999"),
    normal: preferenceOutputValue(3, "4.5"),
  })`);

  return results;
}

async function scenarioTripExportQuality(source) {
  // 行程导出质量提示：返程日期被截断 / 回导会被跳过的数据必须 toast
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const mockDownload = (headers) => {
    run(`
      globalThis.__toasts = [];
      toast = (message) => { __toasts.push(message); };
      fetch = async () => ({
        ok: true,
        status: 200,
        headers: {
          get(name) {
            const key = String(name).toLowerCase();
            const values = ${JSON.stringify(headers)};
            if (Object.prototype.hasOwnProperty.call(values, key)) {
              return values[key];
            }
            return "";
          },
        },
        blob: async () => ({ __blob: true }),
      });
      URL.createObjectURL = () => "blob:x";
      URL.revokeObjectURL = () => {};
      document.body.appendChild = () => {};
    `);
  };
  mockDownload({
    "x-aligo-export-skipped": "1",
    "x-aligo-export-truncated-end": "2",
    "x-aligo-export-unimportable": "1",
    "content-disposition": 'attachment; filename="aligo_u_trips.ics"',
  });
  await run(
    'downloadFromApi("/api/trips/export?user_id=u&format=ics", "aligo_trips.ics")'
  );
  const toasts = run("__toasts.slice()");
  // 其它下载没有这些头：Number("" || 0) 不能误报
  mockDownload({ "content-disposition": 'attachment; filename="x"' });
  await run('downloadFromApi("/api/trips/export?user_id=u", "aligo_trips.csv")');
  const quiet = run("__toasts.slice()");
  return { toasts, quiet };
}

async function scenarioTripImportIcsPreview(source) {
  // ICS 预览：跳过项按「事件 N」标注、warnings 透传、请求体带浏览器时区
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const importFile = runIn(env, "importTripsFromFile");
  run(`
    globalThis.__toasts = [];
    globalThis.__confirmMessage = null;
    globalThis.__bodies = [];
    toast = (message) => { __toasts.push(message); };
    confirm = (message) => { __confirmMessage = message; return false; };
    refreshStatus = async () => {};
    getJSON = (url, options) => {
      __bodies.push(options && options.body ? JSON.parse(options.body) : null);
      return Promise.resolve({
        format: "ics",
        rows: 2,
        created: 0,
        updated: 2,
        skipped: [
          { line: 2, reason: "缺少或非法的 DTSTART" },
          { line: 3, reason: "缺少目的地（SUMMARY 为空）" },
        ],
        skipped_total: 2,
        warnings: [{ line: 4, reason: "第4-5行被解析为同一条行程" }],
        warnings_total: 1,
      });
    };
  `);
  const utf8Bytes = Array.from(Buffer.from("目的地,杭州", "utf8"));
  await importFile({
    size: utf8Bytes.length,
    arrayBuffer: async () => new Uint8Array(utf8Bytes).buffer,
  });
  return {
    confirm: run("__confirmMessage"),
    bodies: run("__bodies.slice()"),
    toasts: run("__toasts.slice()"),
  };
}

async function scenarioKbDedupeUnknownOutcome(source) {
  // 清理重复：删除请求超时 / 断连（无状态码）时服务端可能已删除，
  // 必须提示「可能已删除」并刷新列表
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    globalThis.__toasts = [];
    globalThis.__kbRefreshes = 0;
    globalThis.__kbDocRefreshes = 0;
    toast = (message) => { __toasts.push(message); };
    confirm = () => true;
    refreshKnowledge = async () => { __kbRefreshes += 1; };
    refreshKnowledgeDocuments = async () => { __kbDocRefreshes += 1; };
    globalThis.__requests = [];
    getJSON = (url) => new Promise((resolve, reject) => {
      __requests.push({ resolve, reject });
    });
  `);
  const promise = run("runKbDedupe()");
  await tick();
  env.__requests[0].resolve({ planned: 1, groups: 1, removed: 0, failed: [] });
  await tick();
  env.__requests[1].reject(new Error("请求超时，请稍后重试"));
  await promise;
  return {
    text: run("els.kbEvalResult.textContent"),
    refreshes: run("__kbRefreshes"),
    doc_refreshes: run("__kbDocRefreshes"),
    button: run("els.kbDedupe.textContent"),
  };
}

async function scenarioKbUserSwitchDraft(source) {
  // 切换用户时知识表单存在未保存草稿：先确认，拒绝则保留草稿不切换
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    state.userId = "u1";
    state.busy = false;
    els.userIdInput.value = "u2";
    state.kbEditingId = null;
    state.kbEditBaseline = "";
    els.kbContent.value = "未保存的知识草稿";
    els.kbCategory.value = "";
    globalThis.__confirmCalls = 0;
    globalThis.__confirmAnswer = false;
    window.confirm = () => { __confirmCalls += 1; return __confirmAnswer; };
    loadSessions = async () => {};
    refreshStatus = () => {};
    closeMemorySheet = () => {};
    renderOverview = () => {};
  `);
  await run("saveUser()");
  const rejected = run(`({
    confirmCalls: __confirmCalls,
    userId: state.userId,
    draft: els.kbContent.value,
  })`);
  run("__confirmAnswer = true;");
  await run("saveUser()");
  const accepted = run(`({
    confirmCalls: __confirmCalls,
    userId: state.userId,
    draft: els.kbContent.value,
  })`);
  return { rejected, accepted };
}

async function scenarioFrontendDetailFixes(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  // 未落库提问与草稿合并
  run('els.input.value = "草稿";');
  run('restoreDraftText("你好");');
  const merged = run("els.input.value");
  run('els.input.value = "";');
  run('restoreDraftText("你好");');
  const restored = run("els.input.value");

  // 重命名输入框打开：结束刷新登记待补，收尾后正常刷新
  run(`
    __loads = 0;
    loadSessions = async () => { __loads += 1; };
    state.historyRefreshPending = false;
    document.querySelector = (sel) => (sel === ".hi-input" ? {} : null);
  `);
  await run("refreshSessionsAfterTurn()");
  const pending = run("({ pending: state.historyRefreshPending, loads: __loads })");
  run('document.querySelector = () => null; state.historyRefreshPending = false;');
  await run("refreshSessionsAfterTurn()");
  const refreshed = run("({ pending: state.historyRefreshPending, loads: __loads })");

  // 归档视图下进入新对话
  run('state.showArchived = true; updateArchivedToggle = () => { globalThis.__toggle = (globalThis.__toggle || 0) + 1; };');
  run("leaveArchivedViewForNewConversation();");
  const archived = run("({ show: state.showArchived, toggles: globalThis.__toggle || 0 })");

  // 同一行再次点编辑：有改动要确认，拒绝保留；未改动不弹
  run(`
    __confirmCalls = 0; __confirmAnswer = false;
    confirm = () => { __confirmCalls += 1; return __confirmAnswer; };
    state.tripEditingId = "trip_1";
    els.tripForm.hidden = false;
    els.tripOrigin.value = "北京"; els.tripDestination.value = "用户改的目的地";
    els.tripStart.value = "2026-12-01"; els.tripEnd.value = ""; els.tripPurpose.value = "私人";
  `);
  run('startEditTrip({ trip_id: "trip_1", origin: "北京", destination: "上海", start_date: "2026-11-20", end_date: "", purpose: "出差" });');
  const tripReclick = run("({ calls: __confirmCalls, destination: els.tripDestination.value })");
  run(`
    __confirmCalls = 0;
    els.tripDestination.value = "上海"; els.tripStart.value = "2026-11-20"; els.tripPurpose.value = "出差";
  `);
  run('startEditTrip({ trip_id: "trip_1", origin: "北京", destination: "上海", start_date: "2026-11-20", end_date: "", purpose: "出差" });');
  const tripReclickClean = run("__confirmCalls");

  // 刷新在途 / 失败时旧行编辑：回填最近一次服务端保存值
  run(`
    state.tripLatest.set("trip_1", { trip_id: "trip_1", origin: "北京", destination: "杭州", start_date: "2026-12-02", end_date: "", purpose: "会议" });
    __confirmAnswer = true;
    state.tripEditingId = null;
    startEditTrip({ trip_id: "trip_1", origin: "北京", destination: "上海", start_date: "2026-11-20", end_date: "", purpose: "出差" });
  `);
  const tripLatestView = run("els.tripDestination.value");
  run("state.tripLatest.clear();");

  // 偏好显示名按自有属性查表：constructor / toString 等原型链成员名
  // 不能显示成函数源码
  const prefDisplay = run(`({
    known: prefDisplayName("home_location"),
    constructorKey: prefDisplayName("constructor"),
    toStringKey: prefDisplayName("toString"),
    unknown: prefDisplayName("custom_type"),
  })`);

  // /api/status 是权威快照：renderOverview 回收已缺席类型的合并基线，
  // 否则下一次「添加偏好」会用旧列表整值 PUT，把已删除条目复活
  run(`
    __toasts = [];
    globalThis.__putBodies = [];
    preferenceLatest.clear();
    preferenceLatest.set("hotel_brands", ["汉庭", "如家"]);
    preferenceLatest.set("home_location", "上海");
    state.status = {
      preferences: { home_location: "北京" },
      trips: [],
      statistics: {},
      usage: {},
      storage: {},
    };
    state.tripsAll = [];
    state.tripsAllTotal = 0;
    state.tripsExpanded = false;
    els.storageChips.children.length = 0;
    renderOverview();
  `);
  const pruned = run(`({
    hotelHas: preferenceLatest.has("hotel_brands"),
    homeLatest: preferenceLatest.get("home_location"),
  })`);
  run(`
    getJSON = async (url, options) => {
      __putBodies.push(JSON.parse(options.body).value);
      return {};
    };
    refreshStatus = async () => {};
    els.prefValue.value = "锦江";
    els.prefType.value = "hotel_brands";
  `);
  await run("addPreference({ preventDefault() {} })");
  const prunedPut = run("__putBodies[0]");

  // 存储后端 chip 与顶栏同口径：运行中降级要显示「降级中」并给回退提示
  run(`
    state.status = {
      preferences: {},
      trips: [],
      statistics: {},
      usage: {},
      storage: {
        short_term: "redis",
        short_term_configured: true,
        short_term_degraded: true,
        long_term: "postgres",
      },
    };
    els.storageChips.children.length = 0;
    renderOverview();
  `);
  const degradedChips = run(
    "els.storageChips.children.map((node) => String(node.textContent))",
  );

  // 主题按钮暴露当前状态：aria-pressed 与可访问名随深浅色变化
  run(`
    globalThis.__themeAttrs = {};
    els.themeToggle.setAttribute = (key, value) => { __themeAttrs[key] = value; };
    document.documentElement.dataset.theme = "dark";
    updateThemeButton();
  `);
  const themeDarkAttrs = run(
    "({ label: globalThis.__themeAttrs['aria-label'], pressed: globalThis.__themeAttrs['aria-pressed'] })",
  );
  run("document.documentElement.dataset.theme = 'light'; updateThemeButton();");
  const themeLightAttrs = run(
    "({ label: globalThis.__themeAttrs['aria-label'], pressed: globalThis.__themeAttrs['aria-pressed'] })",
  );

  // 侧栏开关按钮的 aria-expanded 随收起 / 展开同步
  run(`
    globalThis.__menuAttrs = {};
    els.menuBtn.setAttribute = (key, value) => { __menuAttrs[key] = value; };
    els.app.classList.remove("side-collapsed");
    syncSidebarA11y();
  `);
  const sidebarExpanded = run("globalThis.__menuAttrs['aria-expanded']");
  run("els.app.classList.add('side-collapsed'); syncSidebarA11y();");
  const sidebarCollapsed = run("globalThis.__menuAttrs['aria-expanded']");

  // 知识列表摘要播报：内容相同不重复写入（避免反复朗读）
  run(`
    globalThis.__kbStatus = [];
    els.kbListStatus = {
      _value: "",
      set textContent(value) {
        if (value !== this._value) {
          this._value = value;
          __kbStatus.push(value);
        }
      },
      get textContent() {
        return this._value;
      },
    };
    state.kbQuery = "";
    state.kbCategory = "";
    state.kbTotal = 12;
    state.kbItems = [1, 2, 3, 4, 5];
    announceKbListSummary();
    announceKbListSummary();
  `);
  const kbAnnounce = run("__kbStatus.slice()");

  // agent / 意图显示名同样按自有属性查表：constructor / __proto__ 不能
  // 渲染成函数源码
  const agentDisplay = run(`({
    constructorKey: agentDisplayName("constructor"),
    protoKey: agentDisplayName("__proto__"),
    upperConstructor: agentDisplayName("Constructor"),
    known: agentDisplayName("Trip-Budget"),
    unknown: agentDisplayName("Greeting_Agent"),
    intentsConstructor: summarizeIntents(["constructor"]),
    intentsProtoList: summarizeIntents(["__proto__", "toString", "valueOf"]),
    intentsObject: summarizeIntents([{ type: "constructor" }]),
    intentsKnown: summarizeIntents([{ type: "trip_planning" }]),
    badge: makeBadge("constructor", "done").textContent,
  })`);

  return {
    merged,
    restored,
    pending,
    refreshed,
    archived,
    tripReclick,
    tripReclickClean,
    tripLatestView,
    prefDisplay,
    agentDisplay,
    pruned,
    prunedPut,
    degradedChips,
    themeDarkAttrs,
    themeLightAttrs,
    sidebarExpanded,
    sidebarCollapsed,
    kbAnnounce,
  };
}

async function scenarioUserSwitchFormsDraft(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    state.userId = "u1"; state.busy = false; els.userIdInput.value = "u2";
    state.kbEditingId = null; els.kbContent.value = "";
    state.tripEditingId = null; els.tripForm.hidden = false;
    els.tripDestination.value = "杭州"; els.tripStart.value = "2026-12-01";
    els.prefValue.value = "汉庭";
    __confirmCalls = 0; __confirmAnswer = false;
    window.confirm = () => { __confirmCalls += 1; return __confirmAnswer; };
    loadSessions = async () => {}; refreshStatus = () => {};
    closeMemorySheet = () => {}; renderOverview = () => {};
    resetTripForm = () => { els.tripDestination.value = ""; els.tripStart.value = ""; };
    resetPreferenceForm = () => { els.prefValue.value = ""; };
  `);
  await run("saveUser()");
  const rejected = run(`({
    confirmCalls: __confirmCalls,
    userId: state.userId,
    destination: els.tripDestination.value,
    pref: els.prefValue.value,
  })`);
  run("__confirmAnswer = true;");
  await run("saveUser()");
  const accepted = run(`({
    userId: state.userId,
    destination: els.tripDestination.value,
    pref: els.prefValue.value,
  })`);
  return { rejected, accepted };
}

async function scenarioUserSwitchComposer(source) {
  // 切换用户必须把聊天草稿纳入未保存确认并在切换后清空：上一用户未发送
  // 的文本不能留给新用户（输入框仍可回车，会按新 user_id 发出）
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    document.querySelector = () => null;
    state.userId = "u1";
    state.busy = false;
    els.input.value = "A-DRAFT-SECRET";
    els.userIdInput.value = "u2";
    globalThis.__confirmCalls = 0;
    globalThis.__confirmAnswer = false;
    window.confirm = () => { __confirmCalls += 1; return __confirmAnswer; };
    loadSessions = async () => {};
    refreshStatus = () => {};
    closeMemorySheet = () => {};
    renderOverview = () => {};
    resetTripForm = () => {};
    resetPreferenceForm = () => {};
  `);
  await run("saveUser()");
  const rejected = run(`({
    confirmCalls: __confirmCalls,
    userId: state.userId,
    composer: els.input.value,
  })`);
  run("__confirmAnswer = true;");
  await run("saveUser()");
  const accepted = run(`({
    confirmCalls: __confirmCalls,
    userId: state.userId,
    composer: els.input.value,
  })`);
  // 空输入不再隐式切到共享默认账号 web_user
  run(`
    state.userId = "u3";
    els.userIdInput.value = "   ";
    __toasts = [];
  `);
  await run("saveUser()");
  const empty = run(`({ userId: state.userId, toasts: __toasts.slice() })`);
  return { rejected, accepted, empty };
}

async function scenarioStoredUserFallback(source) {
  // 存储的非法用户 ID：仍回退 web_user，但必须记下被拒值供 init 提示
  const env = buildEnv(
    'localStorage.setItem("aligo.userId", "con");\n' + source,
  );
  const run = (code) => runIn(env, code);
  return run(`({
    userId: state.userId,
    rejected: state.rejectedUserId,
    localLeft: localStorage.getItem("aligo.userId"),
    sessionLeft: sessionStorage.getItem("aligo.userId"),
  })`);
}

async function scenarioDeletePreferenceLateResponse(source) {
  // A 的删除响应迟到且期间已切到 B：不能删掉 B 的偏好合并基线
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    document.querySelector = () => null;
    globalThis.__req = [];
    getJSON = (url, options) =>
      new Promise((resolve, reject) => { __req.push({ url, options, resolve, reject }); });
    state.userId = "user-a";
    state.status = { preferences: { hotel_brands: ["Hanting"] } };
    preferenceLatest.set("hotel_brands", ["Hanting"]);
    refreshStatus = async () => {};
  `);
  const promise = run('deletePreference("hotel_brands", "汉庭", null)');
  await tick();
  run(`
    state.userId = "user-b";
    state.status = { preferences: { hotel_brands: ["Rujia"] } };
    preferenceLatest.set("hotel_brands", ["Rujia"]);
  `);
  env.__req[0].resolve({});
  await promise;
  return {
    user_id: run("state.userId"),
    latest: run("preferenceLatest.get('hotel_brands') || null"),
    status: run(
      "(state.status.preferences && state.status.preferences.hotel_brands) || null"
    ),
  };
}

async function scenarioTripDedupeUnknown(source) {
  const results = {};
  for (const [key, status] of [["timeout", 0], ["partial503", 503]]) {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    run(`
      __toasts = []; __refreshed = 0; __reset = 0;
      confirm = () => true;
      refreshStatus = async () => { __refreshed += 1; };
      resetTripForm = () => { __reset += 1; state.tripEditingId = null; };
      state.userId = "u1"; state.tripEditingId = "trip_1";
      bindEvents();
      __pendingReq = [];
      getJSON = (url, options) =>
        new Promise((resolve, reject) => { __pendingReq.push({ resolve, reject }); });
    `);
    run("els.tripsDedupe.click();");
    await tick();
    run("__pendingReq[0].resolve({ would_remove: 2, ambiguous_ids: [] });");
    await tick();
    const errorExpr = status
      ? `(() => { const e = new Error("删除过程中失败：已删除 1 条"); e.status = 503; return e; })()`
      : `new Error("请求超时，请稍后重试")`;
    run(`__pendingReq[1].reject(${errorExpr});`);
    for (let i = 0; i < 50; i += 1) {
      await tick();
      if (!run("els.tripsDedupe.disabled")) break;
    }
    results[key] = run(`({
      toasts: __toasts.slice(),
      refreshed: __refreshed,
      reset: __reset,
      editing: state.tripEditingId,
      button: els.tripsDedupe.textContent,
    })`);
  }
  return results;
}

async function scenarioTripDedupeSwitchGuard(source) {
  // 预览返回前已切换用户：不能再弹确认、不能发出真实删除，也不能重置
  // 新账号正在编辑的行程表单
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    __toasts = []; __refreshed = 0; __reset = 0; __confirm = 0;
    confirm = () => { __confirm += 1; return true; };
    refreshStatus = async () => { __refreshed += 1; };
    resetTripForm = () => { __reset += 1; state.tripEditingId = null; };
    state.userId = "u1"; state.tripEditingId = "trip_1";
    bindEvents();
    __pendingReq = [];
    getJSON = (url, options) =>
      new Promise((resolve, reject) => { __pendingReq.push({ resolve, reject }); });
  `);
  run("els.tripsDedupe.click();");
  await tick();
  run('state.userId = "u2"; state.tripEditingId = "trip_9";');
  run("__pendingReq[0].resolve({ would_remove: 2, ambiguous_ids: [] });");
  for (let i = 0; i < 50; i += 1) {
    await tick();
    if (!run("els.tripsDedupe.disabled")) break;
  }
  return run(`({
    toasts: __toasts.slice(),
    confirmCalls: __confirm,
    requests: __pendingReq.length,
    refreshed: __refreshed,
    reset: __reset,
    editing: state.tripEditingId,
    button: els.tripsDedupe.textContent,
  })`);
}

async function scenarioTripEditPartial(source) {
  // 编辑行程只提交改动字段：整表单 PUT 会把刷新前旧值写回，静默回滚
  // CLI / 另一标签页刚保存的字段
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    __toasts = [];
    toast = (message) => { __toasts.push(message); };
    globalThis.__puts = [];
    getJSON = async (url, options) => {
      __puts.push({ url, body: JSON.parse(options.body) });
      return {
        trip: {
          trip_id: "trip_1",
          origin: "北京",
          destination: "上海",
          start_date: "2026-12-01",
          end_date: "2026-12-03",
          purpose: "客户会议",
        },
        conflicts: [],
      };
    };
    refreshStatus = async () => {};
    confirm = () => true;
    state.userId = "u1";
    state.tripLatest.clear();
    startEditTrip({ trip_id: "trip_1", origin: "北京", destination: "上海", start_date: "2026-12-01", end_date: "2026-12-03", purpose: "出差" });
    els.tripPurpose.value = "客户会议";
  `);
  await run("addTrip({ preventDefault() {} })");
  const partial = run("__puts.map((item) => item.body)");

  // 未改任何字段直接保存：不发请求（服务端会 400「没有需要更新的字段」）
  run(`
    __toasts = [];
    __puts = [];
    startEditTrip({ trip_id: "trip_1", origin: "北京", destination: "上海", start_date: "2026-12-01", end_date: "2026-12-03", purpose: "出差" });
  `);
  await run("addTrip({ preventDefault() {} })");
  const noop = run(`({
    puts: __puts.length,
    toasts: __toasts.slice(),
    editing: state.tripEditingId,
    formHidden: els.tripForm.hidden,
  })`);

  // 编辑另一行但未改动：跨行切换不应弹「未提交内容」确认
  run(`
    globalThis.__confirmCalls = 0;
    confirm = () => { __confirmCalls += 1; return false; };
    resetTripForm();
    els.tripOrigin.value = ""; els.tripDestination.value = ""; els.tripStart.value = "";
    els.tripEnd.value = ""; els.tripPurpose.value = "";
    state.tripLatest.clear();
    startEditTrip({ trip_id: "trip_1", origin: "北京", destination: "上海", start_date: "2026-12-01", end_date: "", purpose: "出差" });
    startEditTrip({ trip_id: "trip_2", origin: "杭州", destination: "成都", start_date: "2026-12-10", end_date: "", purpose: "培训" });
  `);
  const crossRow = run(`({
    confirmCalls: __confirmCalls,
    dest: els.tripDestination.value,
  })`);

  return { partial, noop, crossRow };
}

async function scenarioPanelRound(source) {
  // 行程空态文案、筛选导出按钮文案、重叠徽标总数
  const results = {};

  {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    run(`
      state.userId = "u1";
      els.memorySheet.hidden = false;
      state.tripsExpanded = true;
      state.tripsStatusFilter = "past";
      state.tripsFilter = "";
      state.tripsAll = [];
      state.tripsAllTotal = 0;
      state.tripsLoadedFilter = "";
      state.tripsLoadedStatus = "past";
      state.status = { preferences: {}, trips: [], statistics: {}, usage: {}, storage: {}, trip_count: 0 };
      renderOverview();
    `);
    results.emptyWithStatus = run(`({
      text: els.trips.children[0] ? els.trips.children[0].textContent : null,
      exportLabel: els.exportTrips.textContent,
      exportTitle: els.exportTrips.title,
    })`);
  }

  {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    run(`
      state.userId = "u1";
      els.memorySheet.hidden = false;
      state.tripsExpanded = true;
      state.tripsStatusFilter = "";
      state.tripsFilter = "";
      state.tripsAll = [];
      state.tripsAllTotal = 0;
      state.tripsLoadedFilter = "";
      state.tripsLoadedStatus = "";
      state.status = { preferences: {}, trips: [], statistics: {}, usage: {}, storage: {}, trip_count: 0 };
      renderOverview();
    `);
    results.emptyNoFilter = run(`({
      text: els.trips.children[0] ? els.trips.children[0].textContent : null,
      exportLabel: els.exportTrips.textContent,
    })`);
  }

  {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    run(`
      state.userId = "u1";
      els.memorySheet.hidden = false;
      state.tripsExpanded = true;
      state.tripsStatusFilter = "";
      state.tripsFilter = "";
      state.tripsAll = [{
        trip_id: "trip_1",
        origin: "北京",
        destination: "上海",
        start_date: "2026-01-01",
        end_date: "2026-01-02",
        purpose: "出差",
        conflicts: [{ origin: "杭州", destination: "成都", start_date: "2026-01-01", end_date: "2026-01-03" }],
        conflict_count: 25,
      }];
      state.tripsAllTotal = 1;
      state.tripsLoadedFilter = "";
      state.tripsLoadedStatus = "";
      state.status = { preferences: {}, trips: [], statistics: {}, usage: {}, storage: {}, trip_count: 1 };
      renderOverview();
    `);
    results.conflictBadge = run(`(() => {
      const row = els.trips.children[0];
      const meta = row && row.children[1];
      const badge = meta && meta.children.find(
        (node) => node && String(node.className || "").includes("conflict"),
      );
      return badge ? { title: badge.title, label: badge.getAttribute("aria-label") } : null;
    })()`);
  }

  return results;
}

async function scenarioKbPendingCancel(source) {
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    __req = [];
    __toasts = [];
    confirm = () => false;
    els.kbEvalResult.hidden = true;
    els.kbEvalResult.textContent = "";
    els.kbEvalResult.classList.remove("warn");
    getJSON = (url, options) =>
      new Promise((resolve, reject) => { __req.push({ url, resolve, reject }); });
  `);
  const evalPromise = run("runKbEval()");
  await tick();
  const duringEval = run("els.kbEvalResult.textContent");
  const dedupePromise = run("runKbDedupe()");
  await tick();
  run("__req[1].resolve({ planned: 2, groups: 1, removed: 0, failed: [] });");
  await dedupePromise;
  const afterCancel = run("({ text: els.kbEvalResult.textContent, hidden: els.kbEvalResult.hidden })");
  run(`__req[0].resolve({
    total: 30, hit1: 0.6, hit3: 0.9, hit5: 1, mrr: 0.8, rows: [],
  });`);
  await evalPromise;
  const final = run("({ text: els.kbEvalResult.textContent, hidden: els.kbEvalResult.hidden })");
  return { duringEval, afterCancel, final };
}

async function scenarioUserSwitchPreferenceCache(source) {
  // 切换用户必须清空 preferenceLatest：否则列表型偏好把上一用户的品牌
  // 合并进新用户的保存请求（跨账号数据污染）
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    document.querySelector = () => null;
    globalThis.__putBodies = [];
    getJSON = (url, options) => {
      if (options && options.method === "PUT") {
        __putBodies.push(JSON.parse(options.body));
        return Promise.resolve({});
      }
      return Promise.resolve({ sessions: [], total: 0 });
    };
    state.userId = "user-a";
    state.status = { preferences: { hotel_brands: ["汉庭"] } };
    renderOverview();
  `);
  const cachedBefore = run(
    "preferenceLatest.get('hotel_brands') ? preferenceLatest.get('hotel_brands').slice() : null"
  );
  run('els.userIdInput.value = "user-b";');
  await run("saveUser()");
  await tick();
  const clearedSize = run("preferenceLatest.size");
  // 新用户 B 没有酒店偏好：追加「如家」不能带上 A 的「汉庭」
  run(`
    state.status = { preferences: {} };
    renderOverview();
    els.prefType.value = "hotel_brands";
    els.prefValue.value = "如家";
  `);
  await run("addPreference({ preventDefault() {} })");
  await tick();
  return {
    cached_before: cachedBefore,
    cleared_size: clearedSize,
    put_bodies: run("__putBodies.slice()"),
    user_id: run("state.userId"),
  };
}

async function scenarioStopReconcileQueue(source) {
  // 停止后立刻再发消息：对账被新回合的 busy 短路时必须保留登记，
  // 新回合结束后补跑；已落库则静默重载而不是永远停在「已停止生成」
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    document.querySelector = () => null;
    state.userId = "u1";
    state.sessionId = "s1";
    state.busy = false;
    sessionLoadToken = 5;
    globalThis.__opens = [];
    globalThis.__req = [];
    openSession = (id) => { __opens.push(id); return Promise.resolve(true); };
    getJSON = (url) => new Promise((resolve) => { __req.push({ url, resolve }); });
    rememberStoppedTurn("s1", "Q1", "t1", "已停止");
  `);
  // 新回合在跑：对账立即 deferred，登记保留
  run("state.busy = true;");
  const first = await run('reconcileStoppedTurn("s1", "Q1", 5, "t1")');
  const pendingAfterDeferred = run("state.pendingStoppedTurns.length");
  // 新回合结束：flush 补跑，服务端已落库 → 静默重载
  run("state.busy = false;");
  const flush = run("flushPendingStoppedTurns()");
  await tick();
  const urls = run("__req.map((item) => item.url)");
  run(
    '__req[0].resolve({ messages: [{ role: "user", content: "Q1" }, { role: "assistant", content: "A1", meta: { client_turn_id: "t1" } }] });'
  );
  await flush;
  return {
    first,
    pending_after_deferred: pendingAfterDeferred,
    urls,
    opens: run("__opens.slice()"),
    pending_after_flush: run("state.pendingStoppedTurns.length"),
  };
}

async function scenarioOpenSessionGuards(source) {
  // 删除在途的会话不能再打开；陈旧引用指向已删除会话时不能把当前对话切走
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    document.querySelector = () => null;
    state.userId = "u1";
    state.sessionId = "A";
    state.sessions = [{ session_id: "X", title: "x" }];
    globalThis.__newSessionCalls = 0;
    newSession = () => { __newSessionCalls += 1; };
    getJSON = (url) => new Promise((resolve) => { __requests.push({ args: [url], resolve }); });
  `);
  run("state.deletingSessions.add('D');");
  const deletingReturn = await run("openSession('D')");
  const deletingRequests = run("__requests.length");
  run("state.deletingSessions.delete('D');");
  const promise = run("openSession('deletedY')");
  await tick();
  run("__requests[0].resolve({ messages: [] });");
  await promise;
  // 已知会话在服务端已无消息（其它标签页 / CLI 删除）：移除幽灵行并
  // 明确返回 false，供 sendMessage 中止发送
  run('state.sessions = [{ session_id: "G", title: "g" }];');
  const ghostPromise = run("openSession('G')");
  await tick();
  run("__requests[__requests.length - 1].resolve({ messages: [] });");
  const ghostReturn = await ghostPromise;
  return {
    deleting_requests: deletingRequests,
    // 未加载到目标会话时必须显式返回 false：sendMessage 的发送前守卫
    // 按 loaded !== true 中止，裸 return 会让提问写进已删除会话
    deleting_return: deletingReturn,
    ghost_return: ghostReturn,
    session_id: run("state.sessionId"),
    new_session_calls: run("__newSessionCalls"),
    toasts: run("__toasts.slice()"),
  };
}

async function scenarioWebappRound(source) {
  // Web 前端 app.js 复审一轮的回归：发送前守卫、焦点兜底、IME、超长拦截、
  // 重新生成反馈、偏好合并基线、语义暂存恢复、截断消息编辑入口
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const results = {};
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  // 1) 删除 / 归档 / 分页取尽后键盘焦点不能留给 body
  results.focus = run(`
    (() => {
      globalThis.__focusLog = [];
      const mk = (name) => ({
        focus() { __focusLog.push(name); },
        querySelector() { return null; },
      });
      els.history = {
        querySelector() { return null; },
        querySelectorAll(sel) {
          return sel === ".history-item" ? (globalThis.__items || []) : [];
        },
      };
      els.historySearch = mk("search");
      const out = {};
      globalThis.__items = [mk("s1"), mk("s2"), mk("s3")];
      restoreHistoryFocus({ id: "gone", action: "", key: "", index: 5 });
      out.clamped = __focusLog.slice();
      __focusLog.length = 0;
      restoreHistoryFocus({ id: "gone", action: "", key: "", index: 1 });
      out.indexed = __focusLog.slice();
      __focusLog.length = 0;
      globalThis.__items = [];
      restoreHistoryFocus({ id: "gone", action: "", key: "", index: 0 });
      out.empty = __focusLog.slice();
      return out;
    })()
  `);

  // 2) 超长提问（程序化合并草稿可绕过 maxlength）在清空输入 / 请求前拦截
  run(`
    globalThis.__fetchCalls = 0;
    fetch = async () => { __fetchCalls += 1; return { ok: true, body: null }; };
    state.ready = true;
    state.busy = false;
    state.sessionId = null;
    state.sessionLoading = false;
    state.partialHistory = false;
    els.input.value = "";
    __toasts.length = 0;
  `);
  await run(`sendMessage("x".repeat(CHAT_MESSAGE_MAX + 1))`);
  results.oversize = run(`({
    fetchCalls: __fetchCalls,
    toasts: __toasts.slice(),
    inputKept: els.input.value,
  })`);

  // 3) 无会话 ID 的停止 / 中断：草稿保留且提示结果未知
  results.unconfirmed = run(`
    (() => {
      globalThis.__toasts = [];
      els.input.value = "";
      restoreUnconfirmedDraft("合并后的提问", "已停止");
      const empty = { value: els.input.value, toasts: __toasts.slice() };
      els.input.value = "已有草稿";
      __toasts.length = 0;
      restoreUnconfirmedDraft("合并后的提问", "连接中断");
      return {
        empty,
        withDraft: { value: els.input.value, toasts: __toasts.slice() },
      };
    })()
  `);

  // 4) 重新生成：删除阶段按钮显示「准备中…」，失败 / mismatch 恢复可点
  const chipSetup = `
    globalThis.__chip = {
      dataset: { turnId: "t1" },
      disabled: false,
      attributes: {},
      label: { textContent: "重新生成" },
      querySelector(sel) { return sel === "span" ? __chip.label : null; },
      setAttribute(name, value) { __chip.attributes[name] = String(value); },
      removeAttribute(name) { delete __chip.attributes[name]; },
      remove() { globalThis.__chipRemoved += 1; },
    };
    globalThis.__chipRemoved = 0;
    globalThis.__sent = 0;
    sendMessage = async () => { __sent += 1; };
    deleteLastTurn = () => new Promise((resolve, reject) => {
      globalThis.__pendingDelete = { resolve, reject };
    });
    state.sessionId = "s1";
    state.ready = true;
    state.busy = false;
    state.sessionLoading = false;
    __toasts.length = 0;
  `;
  run(chipSetup);
  let promise = run('regenerate(__chip, "问题")');
  await tick();
  results.regeneratePending = run(`({
    disabled: __chip.disabled,
    label: __chip.label.textContent,
    ariaBusy: __chip.attributes["aria-busy"] || "",
  })`);
  run("__pendingDelete.resolve(false);");
  await promise;
  results.regenerateSuccess = run(`({
    removed: __chipRemoved,
    sent: __sent,
  })`);

  run(chipSetup);
  promise = run('regenerate(__chip, "问题")');
  await tick();
  run("__pendingDelete.resolve(true);");
  await promise;
  results.regenerateMismatch = run(`({
    disabled: __chip.disabled,
    label: __chip.label.textContent,
    ariaBusy: __chip.attributes["aria-busy"] || "",
    removed: __chipRemoved,
    sent: __sent,
    toasts: __toasts.slice(),
  })`);

  run(chipSetup);
  promise = run('regenerate(__chip, "问题")');
  await tick();
  run('__pendingDelete.reject(new Error("timeout"));');
  await promise;
  results.regenerateTimeout = run(`({
    disabled: __chip.disabled,
    label: __chip.label.textContent,
    toasts: __toasts.slice(),
  })`);

  // 5) 列表型偏好保存成功后写回合并基线与状态缓存（刷新失败也不覆盖）
  results.preference = await run(`
    (async () => {
      preferenceLatest.clear();
      preferenceLatest.set("hotel_brands", ["汉庭"]);
      state.userId = "u1";
      state.status = { preferences: { hotel_brands: ["汉庭"] } };
      els.prefType.value = "hotel_brands";
      els.prefValue.value = "如家";
      getJSON = async () => ({ type: "hotel_brands", value: ["汉庭", "如家"] });
      refreshStatus = async () => {};
      await addPreference({ preventDefault() {} });
      return {
        latest: preferenceLatest.get("hotel_brands"),
        status: state.status.preferences.hotel_brands,
      };
    })()
  `);

  // 6) 语义检索进行中关键词刷新失败：暂存列表必须还原，不能停在占位文案
  results.semanticStash = await run(`
    (async () => {
      const mkRow = (text) => ({ textContent: text });
      globalThis.__mkRow = mkRow;
      document.createDocumentFragment = () => ({
        __fragment: true,
        __children: [],
        appendChild(node) {
          // 模拟真实 DOM：节点从原父节点移入 fragment
          node.__stashed = true;
          this.__children.push(node);
          return node;
        },
      });
      els.kbList = {
        children: [mkRow("row1"), mkRow("row2")],
        get firstChild() {
          return this.children.find((node) => !node.__stashed) || null;
        },
        set textContent(value) {
          this.children = value ? [mkRow(value)] : [];
        },
        get textContent() {
          return this.children.length === 1 ? this.children[0].textContent : "";
        },
        appendChild(node) {
          if (node && node.__fragment) {
            for (const child of node.__children) {
              child.__stashed = false;
              this.children.push(child);
            }
          } else {
            // 与真实 DOM 一致：appendChild 是把节点从原位置移走
            const index = this.children.indexOf(node);
            if (index >= 0) this.children.splice(index, 1);
            this.children.push(node);
          }
          return node;
        },
        focus() {},
      };
      els.kbSearch.value = "语义词";
      els.kbSemantic = { disabled: false, textContent: "", addEventListener() {} };
      els.kbMore = { hidden: false, disabled: false };
      refreshKnowledgeCategories = () => {};
      updateKnowledgeMore = () => {};
      // 恢复排队式请求桩（上一节把 getJSON 换成了立即 resolve）
      getJSON = (...args) =>
        new Promise((resolve, reject) => {
          __requests.push({ args, resolve, reject });
        });
      state.kbSemanticMode = false;
      state.kbSemanticQuery = "";
      state.kbQuery = "";
      __requests.length = 0;
      const promise = runSemanticSearch();
      await new Promise((resolve) => setImmediate(resolve));
      const stashing = {
        first: els.kbList.children.length === 1,
        text: els.kbList.firstChild && els.kbList.firstChild.textContent,
      };
      // 并发的关键词刷新接管（seq 作废），随后关键词刷新失败保留了占位
      kbListSeq += 1;
      __requests[0].reject(new Error("429"));
      await promise;
      return {
        stashing,
        restored: els.kbList.children.map((node) => node.textContent),
      };
    })()
  `);

  // 7) 截断显示的超长用户消息不提供必然失败的编辑入口
  results.truncated = run(`
    (() => {
      const long = __origRenderUserNode("x".repeat(200001));
      const short = __origRenderUserNode("短消息");
      globalThis.__appended = 0;
      const mkLast = (truncated) => ({
        dataset: truncated ? { truncated: "1" } : {},
        querySelector(sel) {
          return sel === ".bubble" ? { querySelector: () => null } : null;
        },
        appendChild() { __appended += 1; },
      });
      const selectFor = (last) => (sel) => {
        if (sel === ".user-edit") return [];
        if (sel === ".msg.user") return [last];
        return [];
      };
      state.busy = false;
      state.sessionLoading = false;
      state.sessionId = "s1";
      state.partialHistory = false;
      els.messages = { querySelectorAll: selectFor(mkLast(true)) };
      __origRefreshEditButtons();
      const truncatedAppends = __appended;
      __appended = 0;
      els.messages = { querySelectorAll: selectFor(mkLast(false)) };
      __origRefreshEditButtons();
      return {
        longMarked: long.dataset.truncated === "1",
        shortMarked: short.dataset.truncated === "1",
        truncatedAppends,
        normalAppends: __appended,
      };
    })()
  `);

  // 8) 行程筛选：输入法合成期间不调度服务端过滤，合成结束补一次
  run(`
    const mkInput = () => {
      const listeners = {};
      return {
        value: "",
        listeners,
        addEventListener(type, handler) { (listeners[type] ||= []).push(handler); },
        dispatch(type, event) {
          (listeners[type] || []).forEach((handler) =>
            handler(Object.assign({ target: this, preventDefault() {} }, event)),
          );
        },
        focus() {},
      };
    };
    els.tripFilter = mkInput();
    els.tripFilterRow = { hidden: true };
    // 还原前几节替换过的节点，bindEvents 需要在它们上面注册监听
    els.history = document.createElement("div");
    els.historySearch = document.createElement("input");
    els.kbList = document.createElement("div");
    els.kbMore = document.createElement("button");
    els.kbSemantic = document.createElement("button");
    els.kbSearch = document.createElement("input");
    state.tripsExpanded = true;
    state.tripsComposing = false;
    state.tripsFilter = "";
    __requests.length = 0;
    bindEvents();
  `);
  run('els.tripFilter.dispatch("compositionstart", {});');
  run('els.tripFilter.value = "hang";');
  run('els.tripFilter.dispatch("input", { isComposing: true });');
  await sleep(320);
  results.tripComposing = run(
    '({ composing: state.tripsComposing, requests: __requests.length })'
  );
  run('els.tripFilter.dispatch("compositionend", {});');
  await sleep(320);
  results.tripComposed = run(
    '({ composing: state.tripsComposing, requests: __requests.length, filter: state.tripsFilter })'
  );

  return results;
}

async function scenarioStopReconcileParked(source) {
  // 停止后切走会话：待对账登记必须保留（提问气泡已随消息区重建消失），
  // 回到该会话时再对账，未落库的提问才能放回输入框
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    document.querySelector = () => null;
    state.userId = "u1";
    state.sessionId = "s2";
    state.busy = false;
    sessionLoadToken = 10;
    refreshStatus = async () => {};
    rememberStoppedTurn("s1", "未落库的问题", "t1", "已停止");
  `);
  const parked = await run(
    'reconcileStoppedTurn("s1", "未落库的问题", 10, "t1")'
  );
  const afterParked = run(
    "({ pending: state.pendingStoppedTurns.length, requests: __requests.length })"
  );
  // 在另一个会话里结束回合触发的 flush：不得丢弃旧会话登记、不得发请求
  await run("flushPendingStoppedTurns()");
  const afterFlush = run(
    "({ pending: state.pendingStoppedTurns.length, requests: __requests.length })"
  );
  // 用户回到 s1：openSession 成功分支应调度一次补对账
  run(`
    state.sessionId = "s1";
    globalThis.__flushes = 0;
    flushPendingStoppedTurns = () => { __flushes += 1; return Promise.resolve(); };
  `);
  const openedPromise = run('openSession("s1")');
  await tick();
  env.__requests[env.__requests.length - 1].resolve({
    title: "t",
    messages: [{ role: "user", content: "hi" }],
  });
  const opened = await openedPromise;
  await new Promise((resolve) => setTimeout(resolve, 20));
  return {
    parked,
    after_parked: afterParked,
    after_flush: afterFlush,
    opened,
    flushes: run("__flushes"),
    pending_after_open: run("state.pendingStoppedTurns.length"),
  };
}

async function scenarioStopReconcileSingle(source) {
  // 600ms 定时器与 flush 并发对账同一回合：只允许一条链真正执行
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    document.querySelector = () => null;
    state.userId = "u1";
    state.sessionId = "s1";
    globalThis.__implCalls = 0;
    rememberStoppedTurn("s1", "Q1", "t1", "已停止");
    reconcileStoppedTurnImpl = () => {
      __implCalls += 1;
      return new Promise((resolve) => setTimeout(() => resolve("deferred"), 40));
    };
  `);
  const first = run('reconcileStoppedTurn("s1", "Q1", 0, "t1")');
  const second = await run('reconcileStoppedTurn("s1", "Q1", 0, "t1")');
  const firstResult = await first;
  return {
    first: firstResult,
    second,
    impl_calls: run("__implCalls"),
    pending: run("state.pendingStoppedTurns.length"),
  };
}

async function scenarioEditChipRestore(source) {
  // 删除在途会话作废其加载：被作废的 openSession 不会补回编辑入口，
  // deleteSession 必须自己补一次
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    document.querySelector = () => null;
    refreshEditButtons = __origRefreshEditButtons;
    loadSessions = async () => true;
    refreshStatus = async () => {};
    function el(cls) {
      return {
        className: cls || "",
        dataset: {},
        children: [],
        parentNode: null,
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
        remove() {
          if (!this.parentNode) return;
          const index = this.parentNode.children.indexOf(this);
          if (index >= 0) this.parentNode.children.splice(index, 1);
        },
        querySelector(selector) {
          const all = this.querySelectorAll(selector);
          return all.length ? all[0] : null;
        },
        querySelectorAll(selector) {
          const out = [];
          const wantUser = selector === ".msg.user";
          const wantBubble = selector === ".bubble";
          const wantEdit = selector === ".user-edit";
          const wantForm = selector === ".user-edit-form";
          const walk = (node) => {
            if (!node || !node.children) return;
            for (const child of node.children) {
              if (child.removed === true) continue;
              const cls = String(child.className || "").split(/\\s+/);
              const hit =
                (wantUser && cls.includes("msg") && cls.includes("user")) ||
                (wantBubble && cls.includes("bubble")) ||
                (wantEdit && cls.includes("user-edit")) ||
                (wantForm && cls.includes("user-edit-form"));
              if (hit) out.push(child);
              walk(child);
            }
          };
          walk(this);
          return out;
        },
      };
    }
    const messages = el("messages");
    const user = el("msg user");
    user.appendChild(el("bubble"));
    messages.appendChild(user);
    messages.appendChild(el("msg assistant"));
    els.messages = messages;
    state.userId = "u1";
    state.sessionId = "B";
    state.busy = false;
    state.sessionLoading = false;
    state.partialHistory = false;
  `);
  run("refreshEditButtons();");
  const initial = run('els.messages.querySelectorAll(".user-edit").length');
  // openSession(A) 开始：入口先被移除，loading 期间不再补
  run(
    'state.sessionLoading = true; state.sessionLoadingId = "A"; sessionLoadToken += 1; refreshEditButtons();'
  );
  const duringLoad = run('els.messages.querySelectorAll(".user-edit").length');
  const promise = run('deleteSession("A")');
  await tick();
  env.__requests[0].resolve({ deleted: 1 });
  await promise;
  return {
    initial,
    during_load: duringLoad,
    after_delete: run('els.messages.querySelectorAll(".user-edit").length'),
    session_id: run("state.sessionId"),
    session_loading: run("state.sessionLoading"),
  };
}

async function scenarioTripLateUserGuard(source) {
  // A 的行程删除 / 编辑 404 响应迟到且期间已切到 B：trip_id 跨账号同号，
  // 不能清掉 B 正在编辑的表单或弹 A 的结果
  const deleteCase = async (switchUser) => {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    run(`
      document.querySelector = () => null;
      refreshStatus = async () => {};
      confirm = () => true;
      globalThis.__toasts = [];
      toast = (message) => { __toasts.push(message); };
      state.userId = "user-a";
      state.tripsExpanded = false;
      state.tripEditingId = "trip_1";
      els.tripDestination.value = "上海";
      els.tripSubmit.textContent = "保存修改";
      state.status = {
        preferences: {},
        trips: [{ trip_id: "trip_1", origin: "北京", destination: "上海", start_date: "2026-12-01", end_date: null, purpose: "出差" }],
        trip_count: 1,
        statistics: {},
        storage: {},
        usage: {},
      };
      renderOverview();
      globalThis.__removeBtn = null;
      const walk = (node) => {
        if (!node || !node.children) return;
        for (const child of node.children) {
          if (child.title === "删除行程") globalThis.__removeBtn = child;
          walk(child);
        }
      };
      walk(els.trips);
    `);
    run("__removeBtn.click();");
    // renderOverview 的天气请求也会入队：只取 DELETE
    let del = null;
    for (let i = 0; i < 200 && !del; i += 1) {
      const index = env.__requests.findIndex(
        (req) => req.args[1] && req.args[1].method === "DELETE"
      );
      if (index >= 0) del = env.__requests.splice(index, 1)[0];
      else await new Promise((resolve) => setTimeout(resolve, 20));
    }
    if (!del) throw new Error("trip DELETE not queued");
    if (switchUser) {
      run(`
        state.userId = "user-b";
        state.tripEditingId = "trip_1";
        els.tripForm.hidden = false;
        els.tripDestination.value = "B-杭州";
        els.tripSubmit.textContent = "保存修改";
      `);
    }
    del.resolve({ deleted: 1 });
    await new Promise((resolve) => setTimeout(resolve, 20));
    return run(`({
      editing: state.tripEditingId,
      destination: els.tripDestination.value,
      submit: els.tripSubmit.textContent,
      toasts: __toasts.slice(),
    })`);
  };
  const addCase = async (switchUser) => {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    run(`
      document.querySelector = () => null;
      refreshStatus = async () => {};
      globalThis.__toasts = [];
      toast = (message) => { __toasts.push(message); };
      state.userId = "user-a";
      state.tripEditingId = "trip_1";
      els.tripDestination.value = "上海";
      els.tripStart.value = "2026-12-01";
      els.tripSubmit.textContent = "保存修改";
    `);
    const promise = run('addTrip({ preventDefault() {} })');
    const put = await waitRequest(env);
    if (switchUser) {
      run(`
        state.userId = "user-b";
        state.tripEditingId = "trip_1";
        els.tripDestination.value = "B-杭州";
        els.tripSubmit.textContent = "保存修改";
      `);
    }
    put.reject(
      Object.assign(new Error("行程不存在或已被删除"), { status: 404 })
    );
    await promise;
    return run(`({
      editing: state.tripEditingId,
      destination: els.tripDestination.value,
      submit: els.tripSubmit.textContent,
      toasts: __toasts.slice(),
    })`);
  };
  return {
    deleteSwitched: await deleteCase(true),
    deleteSameUser: await deleteCase(false),
    addSwitched: await addCase(true),
    addSameUser: await addCase(false),
  };
}

async function scenarioDeleteInFlightSend(source) {
  // 当前会话删除在途时不能发送；删除的是别的会话时不影响发送
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    document.querySelector = () => null;
    confirm = () => true;
    refreshStatus = async () => {};
    loadSessions = async () => true;
    state.ready = true;
    state.userId = "u1";
    state.sessionId = "s1";
    state.busy = false;
    state.sessions = [{ session_id: "s1", title: "a" }];
    globalThis.__fetchCalls = 0;
    globalThis.__bodies = [];
    fetch = async (url, options) => {
      __fetchCalls += 1;
      __bodies.push(options && options.body ? JSON.parse(options.body) : null);
      return { ok: true, status: 200, body: null };
    };
  `);
  const del = run('deleteSession("s1")');
  await tick();
  const before = run("({ deleting: state.deletingSessions.has('s1'), busy: state.busy })");
  run('els.input.value = "追问";');
  await run('sendMessage("追问")');
  const blocked = run(`({
    toasts: __toasts.slice(),
    fetchCalls: __fetchCalls,
    input: els.input.value,
  })`);
  env.__requests[0].resolve({ deleted: 1 });
  await del;
  // 删除后立即提问：新会话正常发送
  await run('sendMessage("新提问")');
  const afterDelete = run(`({
    fetchCalls: __fetchCalls,
    lastBody: __bodies[__bodies.length - 1] || null,
  })`);

  // 反向：删除的是侧栏另一个会话，当前对话发送必须照常
  const env2 = buildEnv(source);
  const run2 = (code) => runIn(env2, code);
  run2(`
    document.querySelector = () => null;
    refreshStatus = async () => {};
    loadSessions = async () => true;
    state.ready = true;
    state.userId = "u1";
    state.sessionId = "s1";
    state.busy = false;
    state.deletingSessions.add("s2");
    globalThis.__fetchCalls = 0;
    globalThis.__bodies = [];
    fetch = async (url, options) => {
      __fetchCalls += 1;
      __bodies.push(options && options.body ? JSON.parse(options.body) : null);
      return { ok: true, status: 200, body: null };
    };
  `);
  await run2('sendMessage("正常提问")');
  const otherSession = run2(`({
    fetchCalls: __fetchCalls,
    body: __bodies[0] || null,
  })`);
  return {
    before,
    blocked,
    after_delete: afterDelete,
    other_session: otherSession,
  };
}

async function scenarioTripImportFailure(source) {
  // 导入部分失败（503）/ 客户端超时：已可能落库，必须刷新面板
  const importFile = (env) => runIn(env, "importTripsFromFile");
  const utf8Bytes = Array.from(Buffer.from("destination,杭州", "utf8"));
  const file = {
    size: utf8Bytes.length,
    arrayBuffer: async () => new Uint8Array(utf8Bytes).buffer,
  };
  const runCase = async (kind) => {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    run(`
      globalThis.__toasts = [];
      toast = (message) => { __toasts.push(message); };
      confirm = () => true;
      state.userId = "user-a";
      els.memorySheet.hidden = false;
      globalThis.__refreshCalls = 0;
      globalThis.__refreshOptions = [];
      refreshStatus = async (options = {}) => { __refreshCalls += 1; __refreshOptions.push(options); };
      globalThis.__urls = [];
      getJSON = (url) => {
        __urls.push(url);
        const kind = ${JSON.stringify(kind)};
        if (kind === "previewTimeout") {
          // 预览（dry_run）阶段超时：服务端零写入
          return Promise.reject(new Error("请求超时，请稍后重试"));
        }
        if (__urls.length === 1) {
          return Promise.resolve({ format: "csv", rows: 2, created: 1, updated: 0, skipped: [], skipped_total: 0 });
        }
        if (kind === "partial") {
          return Promise.reject(Object.assign(new Error("导入部分失败：已提交 1 条新增、0 条更新；重试会按同路线同日期去重，可安全重跑"), { status: 503 }));
        }
        if (kind === "timeout") {
          return Promise.reject(new Error("请求超时，请稍后重试"));
        }
        if (kind === "switched") {
          state.userId = "user-b";
          return Promise.reject(Object.assign(new Error("导入部分失败：已提交 1 条新增、0 条更新"), { status: 503 }));
        }
        return Promise.reject(Object.assign(new Error("bad request"), { status: 400 }));
      };
    `);
    await importFile(env)(file);
    return run(`({
      toasts: __toasts.slice(),
      refreshCalls: __refreshCalls,
      refreshOptions: __refreshOptions.slice(),
      urls: __urls.slice(),
      buttonText: els.tripImport.textContent,
      buttonDisabled: els.tripImport.disabled,
    })`);
  };
  return {
    partial: await runCase("partial"),
    timeout: await runCase("timeout"),
    badRequest: await runCase("badRequest"),
    switched: await runCase("switched"),
    previewTimeout: await runCase("previewTimeout"),
  };
}

async function scenarioUserSwitchStorageBlocked(source) {
  // 浏览器拒绝存储：切换仍对当前标签页生效，但提示必须说明重载会回退
  const env = buildEnv('localStorage.setItem("aligo.userId", "alice");\n' + source);
  const run = (code) => runIn(env, code);
  const map = new Map([["aligo.userId", "alice"]]);
  env.localStorage = {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem() {
      throw new Error("QuotaExceededError");
    },
    removeItem: (key) => map.delete(key),
  };
  env.sessionStorage = {
    getItem: () => null,
    setItem() {
      throw new Error("QuotaExceededError");
    },
    removeItem() {},
  };
  run(`
    document.querySelector = () => null;
    confirm = () => true;
    state.busy = false;
    els.userIdInput.value = "bob";
    loadSessions = async () => {};
    refreshStatus = () => {};
    closeMemorySheet = () => {};
    renderOverview = () => {};
    resetTripForm = () => {};
    resetKnowledgeForm = () => {};
    resetPreferenceForm = () => {};
  `);
  await run("saveUser()");
  const switched = run(
    '({ userId: state.userId, toasts: __toasts.slice(), local: localStorage.getItem("aligo.userId") })'
  );
  const reloadEnv = buildEnv(
    'localStorage.setItem("aligo.userId", "alice");\n' + source
  );
  return { switched, reload: runIn(reloadEnv, "state.userId") };
}

async function scenarioDeleteSessionLateResponse(source) {
  // A 的删除成功响应迟到且期间已切到 B：不能改 B 的列表 / 总数 / 游标
  const runCase = async (switchUser) => {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    run(`
      document.querySelector = () => null;
      confirm = () => true;
      refreshStatus = async () => {};
      loadSessions = async () => true;
      state.userId = "user-a";
      state.sessionId = "sess-a";
      state.sessions = [{ session_id: "sess-a" }];
      state.sessionTotal = 1;
      state.sessionsOffset = 1;
      state.busy = false;
      state.searchQuery = "";
    `);
    const promise = run('deleteSession("sess-a")');
    const del = await waitRequest(env);
    if (switchUser) {
      run(`
        state.userId = "user-b";
        state.sessionId = null;
        state.sessions = [{ session_id: "y" }, { session_id: "z" }];
        state.sessionTotal = 2;
        state.sessionsOffset = 2;
        state.sessionsLoadedQuery = "";
      `);
    }
    del.resolve({ deleted: 1 });
    await promise;
    return run(`({
      sessions: state.sessions.map((item) => item.session_id),
      total: state.sessionTotal,
      offset: state.sessionsOffset,
      toasts: __toasts.slice(),
    })`);
  };
  return {
    switched: await runCase(true),
    sameUser: await runCase(false),
  };
}

async function scenarioSessionDeleteUnknown(source) {
  // 删除响应中断（超时 / 5xx）：服务端可能已删除，必须按服务端列表对账
  const runCase = async (status) => {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    run(`
      document.querySelector = () => null;
      confirm = () => true;
      refreshStatus = async () => {};
      loadSessions = async () => {
        state.sessions = [{ session_id: "B" }];
        state.sessionTotal = 1;
        state.sessionsOffset = 1;
        return true;
      };
      state.userId = "u1";
      state.sessionId = "A";
      state.sessions = [{ session_id: "A" }, { session_id: "B" }];
      state.sessionTotal = 2;
      state.sessionsOffset = 2;
      state.searchQuery = "";
      state.busy = false;
    `);
    const promise = run("deleteSession('A')");
    const del = await waitRequest(env);
    del.reject(
      status
        ? Object.assign(new Error("请求失败（HTTP " + status + "）"), { status })
        : new Error("请求超时，请稍后重试")
    );
    await tick();
    const listRequests = env.__requests.length;
    await promise;
    const stateAfter = run(`({
      sessions: state.sessions.map((item) => item.session_id),
      total: state.sessionTotal,
      offset: state.sessionsOffset,
      sessionId: state.sessionId,
      welcome: __showWelcome,
      toasts: __toasts.slice(),
    })`);
    stateAfter.list_requests = listRequests;
    return stateAfter;
  };
  return {
    timeout: await runCase(0),
    serverError: await runCase(502),
    conflict: await runCase(409),
  };
}

async function scenarioTripFilterFailureStale(source) {
  // 展开行程刷新失败保留旧列表：计数文案不能假装旧结果就是新条件的匹配
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  run(`
    document.querySelector = () => null;
    globalThis.__toasts = [];
    toast = (message) => { __toasts.push(String(message)); };
    state.userId = "u1";
    state.status = { preferences: {}, trips: [], trip_count: 312, statistics: {}, storage: {}, usage: {} };
    state.tripsExpanded = true;
    state.tripsAll = [1, 2, 3].map((i) => ({
      trip_id: "trip_" + i, origin: "北京", destination: "上海",
      start_date: "2026-12-0" + i, end_date: null, purpose: "出差",
    }));
    state.tripsAllTotal = 312;
    state.tripsFilter = "";
    state.tripsStatusFilter = "";
    state.tripsLoadedFilter = "";
    state.tripsLoadedStatus = "";
    els.memorySheet.hidden = false;
  `);
  run('state.tripsFilter = "北京";');
  // renderOverview 里的天气请求也在 __requests 里：按 URL 找行程请求
  const waitTripsRequest = async () => {
    for (let i = 0; i < 200; i += 1) {
      const index = env.__requests.findIndex((req) =>
        String(req.args[0]).includes("/api/trips?")
      );
      if (index >= 0) return env.__requests.splice(index, 1)[0];
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    throw new Error("trips request not queued");
  };
  const promise = run("loadExpandedTrips()");
  const request = await waitTripsRequest();
  request.reject(Object.assign(new Error("读取行程失败"), { status: 503 }));
  await promise;
  const failed = run(`({
    count: els.tripFilterCount.textContent,
    rows: els.trips.children.length,
    toasts: __toasts.slice(),
  })`);
  // 静默失败（refreshStatus 路径）：不 toast，但同样不能声称匹配
  run('state.tripsFilter = "广州"; state.tripsAllTotal = 7; __toasts.length = 0;');
  const silent = run("loadExpandedTrips({ silentFailure: true })");
  const silentReq = await waitTripsRequest();
  silentReq.reject(Object.assign(new Error("读取行程失败"), { status: 429 }));
  await silent;
  const silentResult = run(`({
    count: els.tripFilterCount.textContent,
    toasts: __toasts.slice(),
  })`);
  // 成功后自愈：计数恢复为真实匹配数
  run('state.tripsFilter = "上海";');
  const success = run("loadExpandedTrips()");
  const successReq = await waitTripsRequest();
  successReq.resolve({
    trips: [{ trip_id: "trip_9", origin: "北京", destination: "上海", start_date: "2026-12-09", end_date: null, purpose: "出差" }],
    total: 1,
  });
  await success;
  const recovered = run("els.tripFilterCount.textContent");
  return { failed, silent: silentResult, recovered };
}

async function scenarioEditDeleteOutcome(source) {
  // 编辑重发的 deleteLastTurn 超时 / 5xx：结果未知，不能谎称失败
  const runCase = async (status, message) => {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    run(`
      document.querySelector = () => null;
      globalThis.__created = [];
      const __origCreate = document.createElement.bind(document);
      document.createElement = (tag) => {
        const node = __origCreate(tag);
        __created.push(node);
        return node;
      };
      state.userId = "u1";
      state.sessionId = "s1";
      state.busy = false;
      state.sessionLoading = false;
      state.ready = true;
      globalThis.__bubble = {
        textContent: "",
        children: [],
        querySelector: () => null,
        appendChild(child) { this.children.push(child); return child; },
      };
      globalThis.__node = {
        classList: { add() {}, remove() {} },
        querySelector: (sel) => (sel === ".bubble" ? __bubble : null),
      };
      els.messages = {
        querySelectorAll: (sel) =>
          sel === ".msg.assistant" ? [{ dataset: { turnId: "t1" } }] : [],
      };
    `);
    run('startEditUserNode(__node, "原问题");');
    run(
      '__created.find((node) => node.className === "user-edit-input").value = "修改后的问题";'
    );
    run(
      '__created.find((node) => node.className === "save").click();'
    );
    const request = await waitRequest(env);
    request.reject(
      status
        ? Object.assign(new Error(message), { status })
        : new Error(message)
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    return run(`({
      toasts: __toasts.slice(),
      saveText: __created.find((node) => node.className === "save").textContent,
      saveDisabled: __created.find((node) => node.className === "save").disabled,
    })`);
  };
  return {
    timeout: await runCase(0, "请求超时，请稍后重试"),
    serverError: await runCase(502, "请求失败（HTTP 502）"),
    conflict: await runCase(409, "会话正在生成回复，请稍后再试"),
  };
}

async function scenarioPerfRound(source) {
  // 性能热路径一轮的回归：
  // 1) 侧栏搜索的归档匹配探针不阻塞主结果：主列表先渲染，探针晚到只在
  //    列表仍是同一查询 / 视图时补提示，旧查询的迟到结果丢弃；
  // 2) 面板打开 + 行程展开：一次 refreshStatus 只整块渲染一次、只查一次
  //    天气（loadExpandedTrips 已渲染时不再重复 renderOverview）；
  // 3) 记忆面板隐藏时：只更新侧栏「即将出行 / 归档」，不重建面板内容。
  const results = {};

  // ---- 1) 归档探针不阻塞主结果 ----
  {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    run(`
      state.userId = "u1";
      state.searchQuery = "";
      state.showArchived = false;
      state.sessions = [];
      state.sessionTotal = 0;
      state.archivedMatches = 0;
      globalThis.__renders = 0;
      renderHistory = () => { __renders += 1; };
      updateArchivedToggle = () => {};
    `);
    const first = run('loadSessions("北京", { reset: true })');
    const mainReq = await waitRequest(env);
    mainReq.resolve({
      sessions: [{ session_id: "s1" }],
      total: 1,
      archived_count: 3,
    });
    await tick();
    results.beforeProbe = run(
      "({ renders: __renders, loading: state.sessionsLoading, matches: state.archivedMatches, sessions: state.sessions.length })",
    );
    const staleProbe = await waitRequest(env);
    results.probeUrl = staleProbe.args[0];
    // 探针返回前发起新搜索：旧探针结果必须丢弃
    const second = run('loadSessions("上海", { reset: true })');
    const mainReq2 = await waitRequest(env);
    mainReq2.resolve({
      sessions: [{ session_id: "s2" }],
      total: 1,
      archived_count: 3,
    });
    await tick();
    const freshProbe = await waitRequest(env);
    staleProbe.resolve({ total: 7 });
    await tick();
    results.afterStaleProbe = run(
      "({ matches: state.archivedMatches, query: state.sessionsLoadedQuery })",
    );
    freshProbe.resolve({ total: 2 });
    await second;
    await first;
    await tick();
    results.afterFreshProbe = run(
      "({ matches: state.archivedMatches, query: state.sessionsLoadedQuery })",
    );
  }

  // ---- 2) 展开 + 打开面板：一次刷新只渲染一块、只查一次天气 ----
  {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    run(`
      state.userId = "u1";
      state.tripsExpanded = true;
      state.tripsAll = [{ trip_id: "t1", origin: "北京", destination: "上海", start_date: "2026-12-01", end_date: "2026-12-02" }];
      state.tripsAllTotal = 1;
      els.memorySheet.hidden = false;
      document.querySelector = () => null;
      globalThis.__renderCount = 0;
      const __origRender = renderOverview;
      renderOverview = () => { __renderCount += 1; return __origRender(); };
      globalThis.__weather = 0;
      loadUpcomingWeather = async () => { __weather += 1; };
    `);
    const pending = run("refreshStatus({})");
    const statusReq = await waitRequest(env);
    statusReq.resolve({
      trip_count: 1,
      trips: [{ trip_id: "t1", origin: "北京", destination: "上海", start_date: "2026-12-01", end_date: "2026-12-02" }],
      next_trip: { trip_id: "t1", origin: "北京", destination: "上海", start_date: "2026-12-01", end_date: "2026-12-02" },
      preferences: {},
      archived_count: 0,
      chat_count: 3,
      statistics: {},
      usage: { by_day: {}, top_agents: [] },
      travel_summary: {},
      storage: {},
    });
    const tripsReq = await waitRequest(env);
    tripsReq.resolve({ trips: [], total: 1 });
    await pending;
    results.expandedRefresh = run(
      "({ renders: __renderCount, weather: __weather })",
    );
  }

  // ---- 3) 面板隐藏：只更新侧栏，不重建面板内容 ----
  {
    const env = buildEnv(source);
    const run = (code) => runIn(env, code);
    run(`
      state.userId = "u1";
      state.tripsExpanded = true;
      state.tripsAll = Array.from({ length: 200 }, (_, i) => ({
        trip_id: "t" + i,
        origin: "北京",
        destination: "上海",
        start_date: "2026-12-01",
        end_date: "2026-12-02",
      }));
      state.tripsAllTotal = 200;
      els.memorySheet.hidden = true;
      els.trips = {
        children: [],
        innerHTML: "",
        appendChild(node) { this.children.push(node); return node; },
      };
      els.overview = { innerHTML: "", appendChild() {} };
      globalThis.__created = 0;
      const __origCreate = document.createElement.bind(document);
      document.createElement = (tag) => { __created += 1; return __origCreate(tag); };
      globalThis.__weather = 0;
      globalThis.__upcoming = 0;
      globalThis.__archived = null;
      const __origUpcoming = renderUpcoming;
      renderUpcoming = (trip) => { __upcoming += 1; return __origUpcoming(trip); };
      loadUpcomingWeather = async () => { __weather += 1; };
      updateArchivedToggle = (count) => { __archived = count; };
      document.querySelector = () => null;
      state.status = {
        preferences: { home_location: "北京" },
        trips: [],
        trip_count: 200,
        archived_count: 4,
        next_trip: {
          trip_id: "t0",
          origin: "北京",
          destination: "上海",
          start_date: "2026-12-01",
          end_date: "2026-12-02",
        },
        statistics: {},
        usage: { by_day: {}, top_agents: [] },
        travel_summary: {},
      };
    `);
    run("renderOverview()");
    results.hiddenPanel = run(
      "({ created: __created, trips: els.trips.children.length, upcoming: __upcoming, weather: __weather, archived: __archived, tripsText: els.trips.innerHTML })",
    );
    // 切换用户（status=null）仍要清空隐藏面板：清空分支在早退之前
    run("state.status = null; renderOverview();");
    results.hiddenCleared = run("els.overview.innerHTML");
  }

  return results;
}

async function scenarioTheme(source) {
  // 主题初始化脚本：存储键必须与 app.js 的 applyTheme 一致，且同步
  // color-scheme（避免深色系统下浅色偏好首帧闪深色）
  const themePath = appPath.replace(/([\\/])app\.js$/, "$1theme.js");
  const themeSource = fs.readFileSync(themePath, "utf8");
  const env = buildEnv(source);
  const run = (code) => runIn(env, code);
  const results = {};
  run(`
    globalThis.__themeKeys = [];
    const __get = localStorage.getItem.bind(localStorage);
    const __set = localStorage.setItem.bind(localStorage);
    localStorage.getItem = (key) => { __themeKeys.push("get:" + key); return __get(key); };
    localStorage.setItem = (key, value) => { __themeKeys.push("set:" + key); return __set(key, value); };
  `);
  const loadTheme = () => vm.runInContext(themeSource, env);
  const snapshot = () =>
    run(
      '({ theme: document.documentElement.dataset.theme, scheme: document.documentElement.style.colorScheme || "" })'
    );

  // 1) 无存储偏好 + 系统浅色：回退浅色，并记录它读取的键
  loadTheme();
  const readKeys = run(
    "__themeKeys.filter((entry) => entry.startsWith('get:'))"
  );
  results.empty = snapshot();
  results.read_keys = readKeys;

  // 2) 预置存储为深色（用 theme.js 自己读过的键）：必须还原为深色
  const themeKey = readKeys.length ? readKeys[0].slice(4) : "";
  run(
    `localStorage.setItem(${JSON.stringify(themeKey)}, "dark"); __themeKeys.length = 0;`
  );
  loadTheme();
  results.stored = Object.assign(snapshot(), {
    key: themeKey,
    set_keys: run("__themeKeys.filter((entry) => entry.startsWith('set:'))"),
  });

  // 3) 无存储偏好 + 系统深色：跟随系统
  run("localStorage.clear(); __themeKeys.length = 0;");
  env.matchMedia = () => ({
    matches: true,
    addEventListener() {},
    removeEventListener() {},
  });
  loadTheme();
  results.system_dark = snapshot();

  // 4) app.js 的 applyTheme 必须写同一个键，并同步 color-scheme
  run('__themeKeys.length = 0; applyTheme("dark");');
  results.apply = Object.assign(snapshot(), {
    stored: run(`localStorage.getItem(${JSON.stringify(themeKey)})`),
    set_keys: run("__themeKeys.filter((entry) => entry.startsWith('set:'))"),
  });
  return results;
}

async function main() {
  let source = fs.readFileSync(appPath, "utf8");
  const stripped = source.replace(/\ninit\(\);\s*$/, "\n// init() disabled by harness\n");
  if (stripped === source) {
    throw new Error("未能移除 app.js 末尾的 init()，禁止启动页面");
  }
  source = stripped;
  let payload;
  if (scenario === "race") {
    payload = await scenarioRace(source);
  } else if (scenario === "preview") {
    payload = scenarioPreview(source);
  } else if (scenario === "render") {
    payload = scenarioRender(source);
  } else if (scenario === "sse") {
    payload = await scenarioSse(source);
  } else if (scenario === "sseEof") {
    payload = await scenarioSseEof(source);
  } else if (scenario === "replayError") {
    payload = await scenarioReplayError(source);
  } else if (scenario === "copy") {
    payload = await scenarioCopy(source);
  } else if (scenario === "duplicates") {
    payload = await scenarioDuplicates(source);
  } else if (scenario === "eval") {
    payload = await scenarioEval(source);
  } else if (scenario === "dedupe") {
    payload = await scenarioDedupe(source);
  } else if (scenario === "kbPanelFixes") {
    payload = await scenarioKbPanelFixes(source);
  } else if (scenario === "arbitrate") {
    payload = await scenarioArbitrate(source);
  } else if (scenario === "decode") {
    payload = await scenarioDecode(source);
  } else if (scenario === "sessionExportHtml") {
    payload = await scenarioSessionExportHtml(source);
  } else if (scenario === "tripsExport") {
    payload = scenarioTripsExport(source);
  } else if (scenario === "tripsDedupe") {
    payload = await scenarioTripsDedupe(source);
  } else if (scenario === "panelFixes") {
    payload = await scenarioPanelFixes(source);
  } else if (scenario === "startup") {
    payload = await scenarioStartup(source);
  } else if (scenario === "deleteTurn") {
    payload = await scenarioDeleteTurn(source);
  } else if (scenario === "retryGuard") {
    payload = await scenarioRetryGuard(source);
  } else if (scenario === "sessionRefresh") {
    payload = await scenarioSessionRefresh(source);
  } else if (scenario === "editGuard") {
    payload = scenarioEditGuard(source);
  } else if (scenario === "scrollPreserve") {
    payload = await scenarioScrollPreserve(source);
  } else if (scenario === "historyWindow") {
    payload = await scenarioHistoryWindow(source);
  } else if (scenario === "searchJump") {
    payload = await scenarioSearchJump(source);
  } else if (scenario === "silentRestoreWindow") {
    payload = await scenarioSilentRestoreWindow(source);
  } else if (scenario === "sessionSearchFailure") {
    payload = await scenarioSessionSearchFailure(source);
  } else if (scenario === "kbSearchFlow") {
    payload = await scenarioKbSearchFlow(source);
  } else if (scenario === "kbSemanticRefreshQuiet") {
    payload = await scenarioKbSemanticRefreshQuiet(source);
  } else if (scenario === "kbCategoryDraft") {
    payload = await scenarioKbCategoryDraft(source);
  } else if (scenario === "kbImportBusy") {
    payload = await scenarioKbImportBusy(source);
  } else if (scenario === "kbDeleteStale400") {
    payload = await scenarioKbDeleteStale400(source);
  } else if (scenario === "downloadGuards") {
    payload = await scenarioDownloadGuards(source);
  } else if (scenario === "reconcile") {
    payload = await scenarioReconcile(source);
  } else if (scenario === "chatPersisted") {
    payload = scenarioChatPersisted(source);
  } else if (scenario === "kbImportSummary") {
    payload = scenarioKbImportSummary(source);
  } else if (scenario === "kbDelete") {
    payload = await scenarioKbDelete(source);
  } else if (scenario === "kbDeleteGroupGhost") {
    payload = await scenarioKbDeleteGroupGhost(source);
  } else if (scenario === "kbDedupeLocalRemoval") {
    payload = await scenarioKbDedupeLocalRemoval(source);
  } else if (scenario === "userBackupDecode") {
    payload = await scenarioUserBackupDecode(source);
  } else if (scenario === "userBackupRestore") {
    payload = await scenarioUserBackupRestore(source);
  } else if (scenario === "upcomingWeather") {
    payload = await scenarioUpcomingWeather(source);
  } else if (scenario === "httpError") {
    payload = await scenarioHttpError(source);
  } else if (scenario === "initStale") {
    payload = await scenarioInitStale(source);
  } else if (scenario === "silentAppendWindow") {
    payload = await scenarioSilentAppendWindow(source);
  } else if (scenario === "deletedThreadSend") {
    payload = await scenarioDeletedThreadSend(source);
  } else if (scenario === "retiredThreadOpenSession") {
    payload = await scenarioRetiredThreadOpenSession(source);
  } else if (scenario === "renderCap") {
    payload = scenarioRenderCap(source);
  } else if (scenario === "archivedRollback") {
    payload = await scenarioArchivedRollback(source);
  } else if (scenario === "draftSendGuard") {
    payload = await scenarioDraftSendGuard(source);
  } else if (scenario === "editorsRound2") {
    payload = await scenarioEditorsRound2(source);
  } else if (scenario === "retiredThreadRegenerate") {
    payload = await scenarioRetiredThreadRegenerate(source);
  } else if (scenario === "knowledgeSkippedToast") {
    payload = await scenarioKnowledgeSkippedToast(source);
  } else if (scenario === "tripExportQuality") {
    payload = await scenarioTripExportQuality(source);
  } else if (scenario === "tripImportIcsPreview") {
    payload = await scenarioTripImportIcsPreview(source);
  } else if (scenario === "kbDedupeUnknownOutcome") {
    payload = await scenarioKbDedupeUnknownOutcome(source);
  } else if (scenario === "kbUserSwitchDraft") {
    payload = await scenarioKbUserSwitchDraft(source);
  } else if (scenario === "frontendDetailFixes") {
    payload = await scenarioFrontendDetailFixes(source);
  } else if (scenario === "userSwitchFormsDraft") {
    payload = await scenarioUserSwitchFormsDraft(source);
  } else if (scenario === "userSwitchComposer") {
    payload = await scenarioUserSwitchComposer(source);
  } else if (scenario === "storedUserFallback") {
    payload = await scenarioStoredUserFallback(source);
  } else if (scenario === "deletePreferenceLateResponse") {
    payload = await scenarioDeletePreferenceLateResponse(source);
  } else if (scenario === "tripDedupeUnknown") {
    payload = await scenarioTripDedupeUnknown(source);
  } else if (scenario === "kbPendingCancel") {
    payload = await scenarioKbPendingCancel(source);
  } else if (scenario === "userSwitchPreferenceCache") {
    payload = await scenarioUserSwitchPreferenceCache(source);
  } else if (scenario === "stopReconcileQueue") {
    payload = await scenarioStopReconcileQueue(source);
  } else if (scenario === "openSessionGuards") {
    payload = await scenarioOpenSessionGuards(source);
  } else if (scenario === "metricsPanel") {
    payload = await scenarioMetricsPanel(source);
  } else if (scenario === "webappRound") {
    payload = await scenarioWebappRound(source);
  } else if (scenario === "stopReconcileParked") {
    payload = await scenarioStopReconcileParked(source);
  } else if (scenario === "stopReconcileSingle") {
    payload = await scenarioStopReconcileSingle(source);
  } else if (scenario === "editChipRestore") {
    payload = await scenarioEditChipRestore(source);
  } else if (scenario === "tripLateUserGuard") {
    payload = await scenarioTripLateUserGuard(source);
  } else if (scenario === "deleteInFlightSend") {
    payload = await scenarioDeleteInFlightSend(source);
  } else if (scenario === "tripImportFailure") {
    payload = await scenarioTripImportFailure(source);
  } else if (scenario === "userSwitchStorageBlocked") {
    payload = await scenarioUserSwitchStorageBlocked(source);
  } else if (scenario === "deleteSessionLateResponse") {
    payload = await scenarioDeleteSessionLateResponse(source);
  } else if (scenario === "sessionDeleteUnknown") {
    payload = await scenarioSessionDeleteUnknown(source);
  } else if (scenario === "tripFilterFailureStale") {
    payload = await scenarioTripFilterFailureStale(source);
  } else if (scenario === "editDeleteOutcome") {
    payload = await scenarioEditDeleteOutcome(source);
  } else if (scenario === "perfRound") {
    payload = await scenarioPerfRound(source);
  } else if (scenario === "tripDedupeSwitchGuard") {
    payload = await scenarioTripDedupeSwitchGuard(source);
  } else if (scenario === "tripEditPartial") {
    payload = await scenarioTripEditPartial(source);
  } else if (scenario === "panelRound") {
    payload = await scenarioPanelRound(source);
  } else if (scenario === "theme") {
    payload = await scenarioTheme(source);
  } else {
    throw new Error(`unknown scenario: ${scenario}`);
  }
  process.stdout.write(JSON.stringify(payload));
}

main().catch((error) => {
  process.stderr.write(String((error && error.stack) || error));
  process.exit(1);
});
