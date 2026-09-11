// 冒烟测试：验证群聊触发词默认值与"图片白名单"准入机制。
// 用 vm 沙箱加载插件（桩掉 seal / fetch / 定时器），直接驱动 onNotCommandReceived 与 .ai 指令。
// 运行：node test/plugin-smoke-test.js [插件文件名，默认 小清澈2.3.js]
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const target = process.argv[2] || "小清澈2.3.js";
const code = fs.readFileSync(path.join(__dirname, "..", target), "utf8");
console.log(`[加载] ${target}`);

// ── 可控定时器 ──────────────────────────────
let timers = [];
let timerSeq = 0;
const sandboxSetTimeout = (fn, ms) => { const id = ++timerSeq; timers.push({ id, fn, ms }); return id; };
const sandboxClearTimeout = (id) => { timers = timers.filter((t) => t.id !== id); };
async function flushTimers() {
  for (let i = 0; i < 20 && timers.length; i++) {
    const batch = timers; timers = [];
    for (const t of batch) { try { t.fn(); } catch (e) { console.log("TIMER ERR", e.message); } }
    await tick(); await tick();
  }
}
const tick = () => new Promise((r) => setImmediate(r));

// ── fetch 桩：按 URL 返回不同的图片描述 ──────
const requests = [];
let visionSeq = 0;
const sandboxFetch = async (url, options) => {
  const body = options && options.body ? JSON.parse(options.body) : null;
  requests.push({ url, body });
  const isVision = body && body.messages && body.messages[0] && Array.isArray(body.messages[0].content);
  if (isVision) {
    const imgUrl = body.messages[0].content[0].image_url.url;
    visionSeq += 1;
    return { ok: true, json: async () => ({ choices: [{ message: { content: "图片描述#" + visionSeq + "(" + imgUrl + ")" } }] }), text: async () => "" };
  }
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
      name,
      cmdMap: {},
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
const mkMsg = (userId, text, groupId) => ({
  messageType: groupId ? "group" : "private",
  groupId: groupId || "",
  message: text,
  sender: { userId, nickname: "用户" + userId, card: "" },
});
const fire = async (userId, text, groupId) => {
  ext.onNotCommandReceived({}, mkMsg(userId, text, groupId));
  await tick(); await tick(); await tick(); await tick();
};

const reset = () => { store.clear(); timers = []; requests.length = 0; visionSeq = 0; };
const lastChatPayload = () => {
  const chat = requests.filter((r) => r.body && !JSON.stringify(r.body.messages).includes("image_url")).pop();
  return chat ? JSON.stringify(chat.body.messages) : "(无请求)";
};

(async () => {
  const G = "g1";
  cfgDefaults.set("imageRecognitionEnabled", true); // 模拟后台开启识图
  cfgDefaults.set("urlReadingEnabled", false);      // 关掉网页读取，避免干扰请求计数

  console.log("=== 场景1：群聊默认(需触发词)，A 带触发词发图 + B 无触发词发图 ===");
  reset();
  await fire("A", "小清澈，看看这个[CQ:image,file=https://x.com/a.jpg]", G);
  await fire("B", "[CQ:image,file=https://x.com/b.jpg]", G);
  requests.length = 0;
  await flushTimers();
  let sent = lastChatPayload();
  console.log("视觉API调用次数:", visionSeq, "(期望1)");
  console.log("A图被识别:", /图片描述#1/.test(sent), "(期望true)");
  console.log("B图被识别:", sent.includes("b.jpg"), "(期望false)");
  console.log("B消息内容已清空:", /【用户B】：\\n/.test(sent), "(期望true)");

  console.log("\n=== 场景2：群聊默认，仅无触发词用户发图 → 整轮拦截 ===");
  reset();
  await fire("C", "[CQ:image,file=https://x.com/c.jpg]", G);
  await flushTimers();
  console.log("视觉API调用次数:", visionSeq, "(期望0)");
  console.log("产生AI对话请求:", requests.length > 0, "(期望false)");

  console.log("\n=== 场景3：.ai on 免触发词后，任意用户发图都应接受 ===");
  reset();
  ext.cmdMap["ai"].solve({}, mkMsg("A", ".ai on", G), { getArgN: (i) => [null, "on"][i] });
  await fire("D", "[CQ:image,file=https://x.com/d.jpg]", G);
  await flushTimers();
  sent = lastChatPayload();
  console.log("视觉API调用次数:", visionSeq, "(期望1)");
  console.log("D图被识别:", /图片描述#1/.test(sent), "(期望true)");

  console.log("\n=== 场景4：.ai off 回到触发词，无触发词用户发图被拦截 ===");
  reset();
  ext.cmdMap["ai"].solve({}, mkMsg("A", ".ai off", G), { getArgN: (i) => [null, "off"][i] });
  await fire("E", "[CQ:image,file=https://x.com/e.jpg]", G);
  await flushTimers();
  console.log("视觉API调用次数:", visionSeq, "(期望0)");
  console.log("产生AI对话请求:", requests.length > 0, "(期望false)");

  console.log("\n=== 场景5：连续对话进行中，免触发词发图应被接受 ===");
  reset();
  await fire("F", "小清澈，在吗", G);
  await flushTimers();
  requests.length = 0; visionSeq = 0;
  await fire("F", "[CQ:image,file=https://x.com/f.jpg]", G);
  await flushTimers();
  sent = lastChatPayload();
  console.log("视觉API调用次数:", visionSeq, "(期望1)");
  console.log("F图被识别:", /图片描述#1/.test(sent), "(期望true)");
})();
