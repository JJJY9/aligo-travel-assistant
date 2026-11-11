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
    remove() {},
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
    total: 14,
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
    total: 14,
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

  // 确认执行：dry-run + 删除；期间库内变化（计划 2 → 3）+ 跳过 + 失败
  run("__confirmResult = true; __confirmCalls = 0;");
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
  });
  await promise;
  results.applied = {
    text: run("els.kbEvalResult.textContent"),
    warn: run("els.kbEvalResult.classList.contains('warn')"),
    hidden: run("els.kbEvalResult.hidden"),
    requests: run("__requests.length"),
    button: run("els.kbDedupe.textContent"),
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
    total: 14,
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
    await second;
    results.sessions_cursor = {
      first_url: url,
      second_url: secondUrl,
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
    openSession = async () => { __openCalls += 1; };
    resizeInput = () => {};
  `);

  const runCase = async (finalMessages) => {
    run('__toasts.length = 0; globalThis.__openCalls = 0; els.input.value = "";');
    const promise = run('reconcileStoppedTurn("s1", "你好", 5, "turn-1")');
    // attempt 0..2 的轮询，以及 attempt 3 的确认轮询：都先给旧数据
    for (let i = 0; i < 4; i += 1) {
      const request = await waitRequest(env);
      request.resolve({ messages: oldMessages });
      await tick();
    }
    // attempt 3：openSession 后追加的最终确认请求
    const confirm = await waitRequest(env);
    confirm.resolve({ messages: finalMessages });
    await promise;
    await tick();
    return {
      input: run("els.input.value"),
      toasts: run("__toasts.slice()"),
      open_calls: run("__openCalls"),
    };
  };

  const persistedLate = await runCase(newMessages);
  const stillMissing = await runCase(oldMessages);
  return { persistedLate, stillMissing };
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
  } else if (scenario === "arbitrate") {
    payload = await scenarioArbitrate(source);
  } else if (scenario === "decode") {
    payload = await scenarioDecode(source);
  } else if (scenario === "tripsExport") {
    payload = scenarioTripsExport(source);
  } else if (scenario === "tripsDedupe") {
    payload = await scenarioTripsDedupe(source);
  } else if (scenario === "panelFixes") {
    payload = await scenarioPanelFixes(source);
  } else if (scenario === "startup") {
    payload = await scenarioStartup(source);
  } else if (scenario === "reconcile") {
    payload = await scenarioReconcile(source);
  } else if (scenario === "chatPersisted") {
    payload = scenarioChatPersisted(source);
  } else if (scenario === "kbImportSummary") {
    payload = scenarioKbImportSummary(source);
  } else {
    throw new Error(`unknown scenario: ${scenario}`);
  }
  process.stdout.write(JSON.stringify(payload));
}

main().catch((error) => {
  process.stderr.write(String((error && error.stack) || error));
  process.exit(1);
});
