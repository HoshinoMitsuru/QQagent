// 冒烟测试：验证 3.0 的「用户主动调教上下文」机制。
// 用 vm 沙箱桩掉 seal / fetch / 定时器，直接驱动 onNotCommandReceived 与 .ai 指令。
// 运行：node test/teach-smoke-test.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const code = fs.readFileSync(path.join(__dirname, "..", "小清澈3.0.js"), "utf8");

// ── 可控定时器 ──────────────────────────────
let timers = [];
let timerSeq = 0;
const sandboxSetTimeout = (fn, ms) => { const id = ++timerSeq; timers.push({ id, fn, ms }); return id; };
const sandboxClearTimeout = (id) => { timers = timers.filter((t) => t.id !== id); };
const tick = () => new Promise((r) => setImmediate(r));
async function flushTimers() {
  for (let i = 0; i < 20 && timers.length; i++) {
    const batch = timers; timers = [];
    for (const t of batch) { try { t.fn(); } catch (e) { console.log("TIMER ERR", e.message); } }
    await tick(); await tick();
  }
}

// ── fetch 桩 ────────────────────────────────
const requests = [];
const sentToAI = () => requests.filter((r) => r.body && !JSON.stringify(r.body.messages || "").includes("image_url"));
const sandboxFetch = async (url, options) => {
  const body = options && options.body ? JSON.parse(options.body) : null;
  requests.push({ url, body });
  return { ok: true, json: async () => ({ choices: [{ message: { content: "AI回复" } }] }), text: async () => "" };
};

// ── seal 桩 ─────────────────────────────────
const replies = [];
const store = new Map();
const cfgDefaults = new Map();
const registered = [];
const seal = {
  replyToSender: (ctx, msg, text) => replies.push(text),
  ext: {
    find: () => undefined,
    new: (name) => ({
      name, cmdMap: {},
      storageGet: (k) => (store.has(k) ? store.get(k) : undefined),
      storageSet: (k, v) => store.set(k, String(v)),
    }),
    register: (e) => registered.push(e),
    newCmdItemInfo: () => ({}),
    newCmdExecuteResult: (b) => ({ solved: b }),
    registerStringConfig: (e, k, v) => cfgDefaults.set(k, v),
    registerIntConfig: (e, k, v) => cfgDefaults.set(k, v),
    registerFloatConfig: (e, k, v) => cfgDefaults.set(k, v),
    registerBoolConfig: (e, k, v) => cfgDefaults.set(k, v),
    getStringConfig: (e, k) => (cfgDefaults.has(k) ? cfgDefaults.get(k) : ""),
    getIntConfig: (e, k) => (cfgDefaults.has(k) ? cfgDefaults.get(k) : 0),
    getFloatConfig: (e, k) => (cfgDefaults.has(k) ? cfgDefaults.get(k) : 0),
    getBoolConfig: (e, k) => (cfgDefaults.has(k) ? cfgDefaults.get(k) : false),
  },
};

const sandbox = {
  seal, fetch: sandboxFetch, setTimeout: sandboxSetTimeout, clearTimeout: sandboxClearTimeout,
  console, Date, JSON, Math, Number, String, Array, Object, Set, Promise, Error, RegExp, Boolean,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox);

const ext = registered[0];
const GROUP = "g1";
const mkMsg = (userId, text, groupId) => ({
  messageType: groupId ? "group" : "private",
  groupId: groupId || "",
  message: text,
  sender: { userId, nickname: "用户" + userId, card: "" },
});
const mkCtx = (privilegeLevel) => ({ privilegeLevel: privilegeLevel || 0 });
const mkArgs = (...args) => ({ getArgN: (i) => (i >= 1 && i <= args.length ? args[i - 1] : undefined) });

const fire = async (userId, text, groupId, lv) => {
  ext.onNotCommandReceived(mkCtx(lv), mkMsg(userId, text, groupId));
  await tick(); await tick(); await tick(); await tick();
};
const runCmd = async (userId, argsList, groupId, lv) => {
  ext.cmdMap["ai"].solve(mkCtx(lv), mkMsg(userId, ".ai " + argsList.join(" "), groupId), mkArgs(...argsList));
  await tick(); await tick();
};
const historyOf = (scope) => {
  const raw = store.get("AiChat:history:" + scope);
  return raw ? JSON.parse(raw) : [];
};
const reset = () => { store.clear(); timers = []; requests.length = 0; replies.length = 0; };

let pass = 0, fail = 0;
const check = (name, actual, expected) => {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) pass++; else fail++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok ? "" : `  (期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)})`}`);
};

(async () => {
  cfgDefaults.set("imageRecognitionEnabled", true);

  console.log("=== S1 识别兼容性：各种写法都应被识别，且都不触发 AI 调用 ===");
  reset();
  const variants = [
    ["U1", "小清澈：我是哥哥"],
    ["U1", "小清澈:半角冒号"],
    ["U1", "小清澈 ： 冒号前后有空格"],
    ["U1", "Claritas-小清澈：英文名形式"],
    ["U1", "CLARITAS-小清澈:英文名大写"],
    ["U1", "[CQ:at,qq=3567509079] 小清澈：带at前缀"],
    ["U1", "小清澈：：重复冒号"],
  ];
  for (const [u, t] of variants) await fire(u, t);
  const h1 = historyOf("private:U1");
  check("识别条数 = 7", h1.length, 7);
  check("全部为 assistant 角色", h1.every((x) => x.role === "assistant"), true);
  check("全部带 _teach 标记", h1.every((x) => x._teach === true), true);
  check("不触发 AI 调用", sentToAI().length, 0);
  check("内容取出正确(重复冒号)", h1[6].content, "重复冒号");
  check("内容取出正确(带at)", h1[5].content, "带at前缀");

  console.log("\n=== S2 上下文组装：调教内容以 assistant 角色进入请求，元数据不泄漏 ===");
  reset();
  await fire("U2", "小清澈：我是哥哥，叫我哥哥就好");
  requests.length = 0;
  await fire("U2", "你好呀");
  await flushTimers();
  const req = sentToAI().pop();
  const msgs = req ? req.body.messages : [];
  check("产生 AI 请求", !!req, true);
  check("含 assistant 调教消息", msgs.some((m) => m.role === "assistant" && m.content === "我是哥哥，叫我哥哥就好"), true);
  check("末条为 user", msgs[msgs.length - 1].role, "user");
  check("_teach 未泄漏到请求体", JSON.stringify(msgs).includes("_teach"), false);
  check("_seq 未泄漏到请求体", JSON.stringify(msgs).includes("_seq"), false);

  console.log("\n=== S3 群聊权限：普通成员被拒，管理员通过 ===");
  reset();
  await fire("NORMAL", "小清澈：我要给机器人洗脑", GROUP, 0);
  check("普通成员未写入", historyOf("group:" + GROUP).length, 0);
  check("普通成员收到权限提示", replies.some((r) => r.includes("管理员")), true);
  await fire("ADMIN", "小清澈：我要给机器人洗脑", GROUP, 50);
  check("管理员写入成功", historyOf("group:" + GROUP).length, 1);

  console.log("\n=== S4 群聊普通消息仍按触发词规则工作 ===");
  reset();
  await fire("ADMIN", "小清澈，在吗", GROUP, 50);
  await flushTimers();
  check("带触发词触发 AI", sentToAI().length, 1);
  reset();
  await fire("ADMIN", "随便说句话", GROUP, 50);
  await flushTimers();
  check("无触发词被拦截", sentToAI().length, 0);

  console.log("\n=== S5 因果顺序：调教抢先落盘，更早的用户消息应回插到它之前 ===");
  reset();
  await fire("ADMIN", "小清澈，在吗", GROUP, 50);      // 进缓冲，等待防抖
  await fire("ADMIN", "小清澈：我在的", GROUP, 50);    // 立即落盘为 assistant
  const mid = historyOf("group:" + GROUP);
  check("调教已立即落盘", mid.length, 1);
  await flushTimers();
  const h5 = historyOf("group:" + GROUP);
  check("结算后共 3 条(用户+调教+AI回复)", h5.length, 3);
  check("第 1 条是 user(更早发生)", h5[0].role, "user");
  check("第 2 条是 assistant(调教)", h5[1].role === "assistant" && h5[1]._teach === true, true);
  check("第 3 条是 AI 回复", h5[2].role === "assistant" && !h5[2]._teach, true);

  console.log("\n=== S6 .ai teach 管理指令 ===");
  reset();
  await fire("U3", "小清澈：第一句");
  await fire("U3", "小清澈：第二句");
  replies.length = 0;
  await runCmd("U3", ["teach"]);
  check("列出调教条目", replies[0].includes("第二句") && replies[0].includes("2 句"), true);
  replies.length = 0;
  await runCmd("U3", ["teach", "undo"]);
  check("撤销提示包含被撤内容", replies[0].includes("第二句"), true);
  check("撤销后剩 1 条", historyOf("private:U3").length, 1);
  replies.length = 0;
  await runCmd("U3", ["teach", "clear"]);
  check("清空后 0 条", historyOf("private:U3").length, 0);

  console.log("\n=== S7 调教消息中的图片被剥离 ===");
  reset();
  await fire("U4", "小清澈：看图[CQ:image,file=https://x.com/a.jpg]就懂了");
  const h7 = historyOf("private:U4");
  check("写入 1 条", h7.length, 1);
  check("图片CQ已剥离", h7[0].content.includes("CQ:image"), false);
  check("保留文字", h7[0].content, "看图就懂了");

  console.log("\n=== S8 开关关闭后按普通消息处理 ===");
  reset();
  cfgDefaults.set("userTeachEnabled", false);
  await fire("U5", "小清澈：这句不该被记录");
  check("未写入历史", historyOf("private:U5").length, 0);
  await fire("U5", "普通消息");
  await flushTimers();
  check("走普通流程触发 AI", sentToAI().length, 1);
  cfgDefaults.set("userTeachEnabled", true);

  console.log("\n=== S9 触发词与调教符号撞车时，调教优先（不会误触发回复）===");
  reset();
  cfgDefaults.set("keywordPrefix", "小清澈：");
  await fire("U6", "小清澈：这句既是触发词写法也是调教写法");
  check("未触发 AI 调用", sentToAI().length, 0);
  check("按调教写入历史", historyOf("private:U6").length, 1);
  cfgDefaults.set("keywordPrefix", "小清澈，");

  console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
  process.exit(fail ? 1 : 0);
})();
