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
  const page1 = Array.from({ length: 200 }, (_, i) => ({ session_id: "s-" + i }));
  const page2 = Array.from({ length: 50 }, (_, i) => ({ session_id: "s-" + (200 + i) }));
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
  const second = await waitRequest(env);
  second.resolve({ sessions: page2, total: 300 });
  await promise;
  return {
    count: run("state.sessions.length"),
    offset: run("state.sessionsOffset"),
    first_url: first.args[0],
    second_url: second.args[0],
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
  const runScenario = async (status, detail) => {
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
    return run(`({
      toasts: __toasts.slice(),
      healthText: els.healthText.textContent,
      healthClass: els.health.className,
      reconcileCalls: __reconcileCalls.slice(),
    })`);
  };
  return {
    rateLimited: await runScenario(429, "请求过于频繁，请稍后再试"),
    unavailable: await runScenario(503, "同一用户已有对话正在进行"),
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
  return {
    cut: html.includes("已截断显示"),
    cappedLength: html.length,
    normalUntouched: !normal.includes("已截断显示") && normal.includes("普通内容"),
    userCut: userText.includes("已截断显示"),
    userCappedLength: userText.length,
    userNormalUntouched: smallText === "普通提问",
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
    globalThis.__row = row;
  `);
  await run('deletePreference("home_location", "常住地", __row)');
  results.deleteSuccess = run(`({
    rowRemoved: __row.removed === true,
    latestHas: preferenceLatest.has("home_location"),
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
  return { merged, restored, pending, refreshed, archived, tripReclick, tripReclickClean, tripLatestView };
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
  await run("openSession('D')");
  const deletingRequests = run("__requests.length");
  run("state.deletingSessions.delete('D');");
  const promise = run("openSession('deletedY')");
  await tick();
  run("__requests[0].resolve({ messages: [] });");
  await promise;
  return {
    deleting_requests: deletingRequests,
    session_id: run("state.sessionId"),
    new_session_calls: run("__newSessionCalls"),
    toasts: run("__toasts.slice()"),
  };
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
  } else {
    throw new Error(`unknown scenario: ${scenario}`);
  }
  process.stdout.write(JSON.stringify(payload));
}

main().catch((error) => {
  process.stderr.write(String((error && error.stack) || error));
  process.exit(1);
});
