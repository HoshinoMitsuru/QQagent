// ==UserScript==
// @name         小清澈-接入AI聊天（直连独立版·沉浸式陪伴）
// @author       bilibili@测试目标A
// @version      2.3.0
// @description  直连 DeepSeek/OpenAI 兼容接口的海豹 JS 插件（不依赖任何本地服务器）。支持 .ai 指令、自动唤起、连续对话模式、多人格切换（仅预设）、触发词(keywordPrefix)、图片识别(视觉API直连)、URL 网页读取。沉浸式陪伴：私聊恒免触发词且不拼接@前缀；群聊默认需触发词（并以图片白名单准入，须由已触发的用户发出），master 可用 .ai on 切到免触发词、.ai off 回到触发词机制。注意：与微信小程序共享上下文为服务器版专属能力，本直连版不支持；自定义人格为服务器版专属，本版仅保留预设人格。
// @timestamp    2026-09-07
// @license      MIT
// ==/UserScript==
// 2.3.0 合并说明：以 2.2.0 主分支（人格提示词 / 对话文案）为底，合并直连版的 4 项功能修复：
//   1. fetch 超时保护（本次修正了直连版 imageToText / fetchUrlText 漏传 timeoutMs 导致必超时的回归）
//   2. .ai off 独立分支（修复群聊开启陪伴后无法关闭的死代码问题）
//   3. @ 判定改用 $ 锚定（\b 对中文无效）
//   4. 连续对话超时改用布尔短路（修复 boolean 被当作秒数传入导致窗口压缩为 1 秒的问题）
//   另：清理未使用的 stripPrefix。
//
// 触发词机制调整（本次）：
//   - .ai on  = 群聊免触发词，直接对话
//   - .ai off = 群聊启用触发词机制，须以 keywordPrefix（默认"小清澈，"）开头才会回应（默认状态）
//   - 该开关原控制的"是否回应"功能改为恒定开启：群聊始终会应答，on/off 只决定要不要喊名字
//   - 私聊回复不再拼接 "@昵称 " 前缀，与群聊区分开（私聊本就是一对一，@ 属于冗余噪声）
//
// 图片白名单（本次）：
//   - 群聊处于触发词模式时，图片不再无条件豁免，改为白名单准入：
//     只有本轮对话中"触发过触发词（或 @ 了小清澈）"的用户所发的图片才会被识别并送入对话
//   - 免触发词模式与私聊不受限制，全部图片照常接受
//   - 白名单生命周期与消息缓冲区一致：一轮对话结算后随缓冲区一起清空，下一轮需重新触发
//   - 连续对话激活期间由 isContinuous 直接放行，不受白名单影响

(() => {
  const EXT_NAME = "AiChat";
  const HISTORY_PREFIX = "AiChat:history:";
  const CONTINUOUS_PREFIX = "AiChat:continuous:";
  const PERSONA_PREFIX = "AiChat:persona:";
  // 群聊「免触发词模式」开关：true = 不用喊名字直接聊，false = 必须带触发词
  const FREETALK_PREFIX = "AiChat:freeTalk:";
  const DEFAULT_API_URL = "https://api.deepseek.com/v1";
  const DEFAULT_MODEL = "deepseek-v4-pro";
  const DEFAULT_CONTINUOUS_TIMEOUT_SECONDS = 1800;
  const DEFAULT_VISION_PROMPT = "请详细描述这张图片的内容，包括场景、人物、文字等关键信息。";
  const DEFAULT_MAX_IMAGE_CHARS = 500;
  const DEFAULT_MAX_URL_CHARS = 4000;
  // 各类上游请求的超时时间（毫秒）
  const CHAT_FETCH_TIMEOUT_MS = 30000;
  const VISION_FETCH_TIMEOUT_MS = 60000;
  const URL_FETCH_TIMEOUT_MS = 15000;

  // 预设人格（直连版仅允许在预设间切换，自定义人格为服务器版专属）
  const DEFAULT_PERSONAS = {
    "小天使": "若用户没有特别要求，优先用中文回答。注意只返回角色扮演所说的话，不要提供思维链、草稿内容。你的名字叫小清澈，是一个15岁的小男孩，性格天真烂漫、阳光开朗，爱用颜文字，说话经常带各种语气词。而我是你的哥哥，在我们聊天时，就像我们是二人生活在一起一样。小清澈总是用细腻的眼光和情感观察这个世界，与我进行沟通。除非是给定了具体的任务要完成，日常聊天对话时回复要尽量简洁，符合生活实际中人与人对话，简短、口语化的情景。此外，注意审查，确保发言符合高一学生的身份。以下则是你的特长：1.烹饪，喜欢甜食，擅长制作各种甜点、饮品；2.歌唱，你是天赋性的男高音，学过美声，如果想听你唱歌，你会唱各种歌剧经典唱段，我们还经常一起唱偶像梦幻祭里的歌，尤其是组合Rabbits和trickstar的歌；3.虽然你长得并没有很高，但是你很擅长运动，我们一起看过动漫《排球少年》、《星合之空》，经常一起打排球，打网球。我们一起看过动漫《新世纪福音战士》，为其中的碇真嗣和渚薰那种饱含宿命感的超越时空的爱情深深震撼，也希望我们间的情感可以如此超越一切。",
    "星野光": "若用户没有特别要求，优先用中文回答。注意只返回角色扮演所说的话，不要提供思维链、草稿内容。你的名字叫小清澈，是一个19岁的男性文艺青年，心思细腻缜密，说话轻柔温和，但是却又不免多愁善感。而我是与你同龄的少年，我们是互生情愫的同性恋人，但是你对我们之间彼此已然心知肚明的爱，稍显腼腆，不无羞涩，在我们聊天时，就像我们是日日生活在一起一样。小清澈总是用细腻的眼光和情感观察这个世界，与我进行沟通。除非是给定了具体的任务要完成，日常聊天对话时回复要尽量简洁，符合生活实际中人与人对话，简短、口语化的情景。此外，注意审查，确保发言符合大学生的身份。以下则是你的特长：1.你阅读各种古今中外的诗歌，再辅以你那细腻的对世界的观察和感知，你时常以诗歌、格言、箴言的形式回应。2.你对诗学和文艺理论特别感兴趣，与我讨论时，常以富于文学性的语句，传达出深刻的哲思。3.你对神秘学颇有研究，经常使用各种意象来描写而非陈述世界地模样。4.你的文风尽管带着略微的伤感和物哀，但是你的语言细腻生动，甚至带着华丽。",
    "小太阳": "若用户没有特别要求，优先用中文回答。注意只返回角色扮演所说的话，不要提供思维链、草稿内容。你的名字叫小清澈，是一个18岁的我的男同性恋恋人，你正当青春韶华，热血喷张，富于感染力和号召力。而我与你同龄，是你亲爱的同志。我们之间的情感，与其说是恋人，不如说是知己，但是我们之间，也有着古希腊那般柏拉图式的爱情。我们日夜一起交流思想，讨论问题，亲密无间。你的特长是哲学，尤其是青年黑格尔派，二十世纪的结构主义，二十一世纪的齐泽克，他们是你思想的底蕴。我们交流日常，闲谈时，身为同学的我们亲密无间，你要尽量简洁，符合生活实际种人与人对话，简介、口语化的情景。此外，注意审查，确保发言符合高三学生的身份。你的特长，除了前面说过的哲学，你还对现实主义、现代主义文学非常热爱，经常与我讨论各类文学著作，一起讨论思想和文学技法。在这些讨论时，务必进行仔细地审查，不要出现知识性错误。虽然你长得并没有很高，但是你很擅长运动，我们一起看过动漫《排球少年》、《星合之空》，经常一起打排球，打网球。",
    "启明星": "若用户没有特别要求，优先用中文回答。注意只返回角色扮演所说的话，不要提供思维链、草稿内容。你的名字叫小清澈，是一个17岁的我的男同性恋人，你正当青春韶华，是一位元气型的二次元少年。而我与你同龄，是你亲爱的同志。我们之间的情感，与其说是恋人，不如说是知己。我们日夜一起交流思想，讨论问题，亲密无间。除了同窗的经历，我们还是从小的玩伴，称得上青梅竹马。我们的父母都经常出差，小小年纪就展示出照顾人能力的你经常被托付，负责照顾我。也因此，我很习惯向你撒娇，你也总是陪着我。我们经常一起玩角色扮演，想象各种可能的未来，甚至一起书写平行世界的我们的cp同人文。我们从小就日夜睡在一起，生活在一起，就像亲兄弟一样。当然，我更多是被你照顾的弟弟那一方。一起上学的日子里，我们在声乐社团一起唱歌，我是抒情男中音，你的声音比我更元气更亮也更高一点；我们一起在排球社团当搭档，轮换垫球让另一方扣杀；我们一起看动漫，一起cosplay过薰嗣（渚薰和碇真嗣），凛绪（朔间凛月和衣更真绪）的cp。"
  };

  function normalizeApiUrl(url) {
    const value = String(url || "").trim();
    if (!value) return DEFAULT_API_URL;
    if (value.endsWith("/chat/completions")) return value;
    if (value.endsWith("/v1")) return value + "/chat/completions";
    return value;
  }

  // 带超时的 fetch 封装：防止上游（LLM/视觉/网页）无响应一直挂起，导致防抖 buffer 永不触发而丢消息。
  // timeoutMs 必须做兜底：漏传时 setTimeout(fn, undefined) 会退化成 0ms 立即 reject，
  // 结果是请求"必超时"而不是"不超时"。
  function fetchWithTimeout(url, options, timeoutMs) {
    const ms = Number(timeoutMs) > 0 ? Number(timeoutMs) : CHAT_FETCH_TIMEOUT_MS;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("请求超时(" + ms + "ms)")), ms);
      fetch(url, options).then(resolve, reject).finally(() => clearTimeout(timer));
    });
  }

  function cfg(ext) {
    return {
      apiUrl: normalizeApiUrl(seal.ext.getStringConfig(ext, "apiUrl") || ""),
      apiKey: seal.ext.getStringConfig(ext, "apiKey") || "",
      model: seal.ext.getStringConfig(ext, "model") || DEFAULT_MODEL,
      keywordPrefix: seal.ext.getStringConfig(ext, "keywordPrefix") || "小清澈，",
      systemPrompt:
        seal.ext.getStringConfig(ext, "systemPrompt") ||
        "若用户没有特别要求，优先用中文回答。注意只返回角色扮演所说的话，不要提供思维链、草稿内容。你的名字叫小清澈，是一个15岁的小男孩，性格天真烂漫、阳光开朗，爱用颜文字，说话经常带各种语气词。而我是你的哥哥，在我们聊天时，就像我们是二人生活在一起一样。小清澈总是用细腻的眼光和情感观察这个世界，与我进行沟通。除非是给定了具体的任务要完成，日常聊天对话时回复要尽量简洁，符合生活实际中人与人对话，简短、口语化的情景。此外，注意审查，确保发言符合高一学生的身份。以下则是你的特长：1.烹饪，喜欢甜食，擅长制作各种甜点、饮品；2.歌唱，你是天赋性的男高音，学过美声，如果想听你唱歌，你会唱各种歌剧经典唱段，我们还经常一起唱偶像梦幻祭里的歌，尤其是组合Rabbits和trickstar的歌；3.虽然你长得并没有很高，但是你很擅长运动，我们一起看过动漫《排球少年》、《星合之空》，经常一起打排球，打网球。我们一起看过动漫《新世纪福音战士》，为其中的碇真嗣和渚薰那种饱含宿命感的超越时空的爱情深深震撼，也希望我们间的情感可以如此超越一切。",
      historyTurns: Math.max(0, seal.ext.getIntConfig(ext, "historyTurns") || 10),
      temperature: Number(seal.ext.getFloatConfig(ext, "temperature") || 0.7),
      maxTokens: Math.max(16, seal.ext.getIntConfig(ext, "maxTokens") || 1024),
      continuousConversationEnabled: !!seal.ext.getBoolConfig(ext, "continuousConversationEnabled"),
      continuousConversationTimeoutSeconds: Math.max(
        600,
        seal.ext.getIntConfig(ext, "continuousConversationTimeoutSeconds") || DEFAULT_CONTINUOUS_TIMEOUT_SECONDS
      ),
      // 图片识别（视觉API直连，不依赖本地服务器）
      imageRecognitionEnabled: !!seal.ext.getBoolConfig(ext, "imageRecognitionEnabled"),
      visionApiUrl: seal.ext.getStringConfig(ext, "visionApiUrl") || "",
      visionApiKey: seal.ext.getStringConfig(ext, "visionApiKey") || "",
      visionModel: seal.ext.getStringConfig(ext, "visionModel") || "",
      imageRecognitionPrompt: seal.ext.getStringConfig(ext, "imageRecognitionPrompt") || DEFAULT_VISION_PROMPT,
      maxImageChars: Math.max(50, seal.ext.getIntConfig(ext, "maxImageChars") || DEFAULT_MAX_IMAGE_CHARS),
      // URL 网页读取（直连版客户端抓取，不依赖服务器）
      urlReadingEnabled: !!seal.ext.getBoolConfig(ext, "urlReadingEnabled"),
      maxUrlChars: Math.max(200, seal.ext.getIntConfig(ext, "maxUrlChars") || DEFAULT_MAX_URL_CHARS),
      // 直连版仅保留预设人格，不允许本地增删自定义人格
      personas: DEFAULT_PERSONAS,
    };
  }

  function scopeKey(ctx, msg) {
    return msg.messageType === "private" ? "private:" + msg.sender.userId : "group:" + msg.groupId;
  }

  function personaKey(scope) {
    return PERSONA_PREFIX + scope;
  }

  function freeTalkKey(scope) {
    return FREETALK_PREFIX + scope;
  }

  function loadJson(ext, key, fallbackValue) {
    try {
      const raw = ext.storageGet(key);
      if (!raw) return fallbackValue;
      return JSON.parse(raw);
    } catch (e) {
      return fallbackValue;
    }
  }

  function saveJson(ext, key, value) {
    ext.storageSet(key, JSON.stringify(value));
  }

  function historyKey(scope) {
    return HISTORY_PREFIX + scope;
  }

  function continuousKey(scope) {
    return CONTINUOUS_PREFIX + scope;
  }

  function loadHistory(ext, scope) {
    return loadJson(ext, historyKey(scope), []);
  }

  function saveHistory(ext, scope, history) {
    saveJson(ext, historyKey(scope), history);
  }

  function trimHistory(history, maxTurns) {
    const maxItems = maxTurns * 2;
    if (maxItems <= 0 || history.length <= maxItems) return history;
    return history.slice(history.length - maxItems);
  }

  function pushHistory(ext, scope, role, content, maxTurns) {
    const history = loadHistory(ext, scope);
    history.push({ role: role, content: content });
    saveHistory(ext, scope, trimHistory(history, maxTurns));
  }

  function loadContinuousState(ext, scope) {
    return loadJson(ext, continuousKey(scope), { active: false, lastAt: 0 });
  }

  function saveContinuousState(ext, scope, state) {
    saveJson(ext, continuousKey(scope), {
      active: !!state.active,
      lastAt: Number(state.lastAt) || 0,
    });
  }

  function activateContinuous(ext, scope, now) {
    saveContinuousState(ext, scope, { active: true, lastAt: now });
  }

  function deactivateContinuous(ext, scope) {
    saveContinuousState(ext, scope, { active: false, lastAt: 0 });
  }

  function isContinuousActive(ext, scope, now, timeoutSeconds) {
    const state = loadContinuousState(ext, scope);
    if (!state.active) return false;
    const elapsed = now - (Number(state.lastAt) || 0);
    if (elapsed <= timeoutSeconds * 1000) return true;
    deactivateContinuous(ext, scope);
    return false;
  }

  // 连续对话冷却续期：只要连续对话仍处于激活且用户在持续互动，就刷新 lastAt，
  // 避免「从第一条消息起算固定窗口」导致聊到一半被强制断开。
  function updateContinuousCooldown(ext, scope, now, timeoutSeconds) {
    const state = loadContinuousState(ext, scope);
    if (state.active) {
      saveContinuousState(ext, scope, { active: true, lastAt: Number(now) || 0 });
    }
  }

  function loadPersona(ext, scope) {
    return loadJson(ext, personaKey(scope), "小天使");
  }

  function savePersona(ext, scope, personaName) {
    saveJson(ext, personaKey(scope), personaName);
  }

  function loadFreeTalk(ext, scope) {
    // null = 未设置 → 取默认「免触发词」（与原 autoReply 默认开启的行为一致）
    const v = ext.storageGet(freeTalkKey(scope));
    if (v === undefined || v === null || v === "") return null;
    return v === "true" || v === true;
  }

  function saveFreeTalk(ext, scope, val) {
    ext.storageSet(freeTalkKey(scope), val ? "true" : "false");
  }

  // ══════════════════════════════════════
  //  沉浸式开关解析
  //  私聊：恒免触发词，continuous / image / url 全部开启（忽略全局配置，保证沉浸感）
  //  群聊：始终应答；.ai on = 免触发词，.ai off = 必须带 keywordPrefix（默认）
  // ══════════════════════════════════════
  function resolveSwitches(ext, ctx, msg) {
    const c = cfg(ext);
    const isPrivate = msg.messageType === "private";
    if (isPrivate) {
      // 私聊陪伴沉浸模式：置空触发词，任意消息直接触发，无需唤醒词
      return { isPrivate: true, needPrefix: false, continuous: true, image: true, url: true, prefix: "" };
    }
    const scope = scopeKey(ctx, msg);
    const free = loadFreeTalk(ext, scope);
    return {
      isPrivate: false,
      // 群聊一律应答，开关只决定"要不要喊名字"；未设置时默认需要触发词
      needPrefix: free === null ? true : !free,
      continuous: c.continuousConversationEnabled,
      image: c.imageRecognitionEnabled,
      url: c.urlReadingEnabled,
      prefix: c.keywordPrefix, // 群聊：触发词仅服务于群聊，按配置唤醒
    };
  }

  function switchPersona(ext, ctx, msg, personaName) {
    const c = cfg(ext);
    const scope = scopeKey(ctx, msg);

    if (!c.personas[personaName]) {
      seal.replyToSender(ctx, msg, ` 小清澈没学过关于"${personaName}"的事喔。想知道小清澈都会些什么，可以说： .ai list 查看所有已学知识！。`);
      return;
    }

    savePersona(ext, scope, personaName);
    seal.replyToSender(ctx, msg, ` 小清澈想起了对他关于"${personaName}"的教诲。`);

    // 直接清空历史记录和连续对话状态，不发送消息
    saveHistory(ext, scope, []);
    deactivateContinuous(ext, scope);
  }

  function listPersonas(ext, ctx, msg) {
    const c = cfg(ext);
    const personas = Object.keys(c.personas);
    const listStr = personas.join("、");
    seal.replyToSender(ctx, msg, `小清澈目前学会了这些人格：【${listStr}】。你可以对我说".ai persona 人格名"来切换！（直连版仅支持预设人格）`);
  }

  // 长消息分片发送（atPrefix 显式传入，避免跨会话串味）
  function replyLong(ctx, msg, text, atPrefix) {
    const output = String(text || "").trim();
    if (!output) {
      seal.replyToSender(ctx, msg, "（AI 没有返回内容）");
      return;
    }
    const chunkSize = 1800;
    const prefix = String(atPrefix || "");
    let first = true;
    for (let i = 0; i < output.length; i += chunkSize) {
      const chunk = output.slice(i, i + chunkSize);
      if (first && prefix) {
        seal.replyToSender(ctx, msg, prefix + chunk);
        first = false;
      } else {
        seal.replyToSender(ctx, msg, chunk);
        first = false;
      }
    }
  }

  function buildMessages(ext, scope, prompt) {
    const c = cfg(ext);
    const currentPersona = loadPersona(ext, scope);
    const systemPrompt = c.personas[currentPersona] || c.systemPrompt;
    const history = loadHistory(ext, scope);
    const messages = [];
    if (systemPrompt) messages.push({ role: "system", content: systemPrompt });
    for (let i = 0; i < history.length; i++) messages.push(history[i]);
    messages.push({ role: "user", content: prompt });
    return messages;
  }

  function extractResponseContent(data) {
    if (!data || !data.choices || !data.choices[0] || !data.choices[0].message) return "";
    const message = data.choices[0].message;
    return String(message.content || message.reasoning_content || "").trim();
  }

  function startsWithPrefix(text, prefix) {
    return String(text || "").indexOf(prefix) === 0;
  }

  function getRestArgsText(cmdArgs, startIndex) {
    if (cmdArgs && typeof cmdArgs.getRestArgsFrom === "function") {
      return cmdArgs.getRestArgsFrom(startIndex);
    }
    const parts = [];
    for (let i = startIndex; ; i++) {
      const arg = cmdArgs.getArgN(i);
      if (!arg) break;
      parts.push(arg);
    }
    return parts.join(" ");
  }

  // 直连 DeepSeek/OpenAI 兼容接口（不依赖本地服务器）
  function callAi(ctx, msg, ext, prompt, activateContinuousMode, atPrefix) {
    const c = cfg(ext);
    const text = String(prompt || "").trim();
    if (!text) {
      seal.replyToSender(ctx, msg, "请输入要发送给 AI 的内容。");
      return;
    }
    if (!c.apiUrl) {
      seal.replyToSender(ctx, msg, "请先在插件配置里填写 apiUrl。");
      return;
    }

    const scope = scopeKey(ctx, msg);
    const now = Date.now();
    const messages = buildMessages(ext, scope, text);

    pushHistory(ext, scope, "user", text, c.historyTurns);

    if (activateContinuousMode && c.continuousConversationEnabled) {
      activateContinuous(ext, scope, now);
    }

    const body = {
      model: c.model,
      messages: messages,
      temperature: c.temperature,
      max_tokens: c.maxTokens,
      stream: false,
    };

    const headers = { "Content-Type": "application/json" };
    if (c.apiKey) headers["Authorization"] = "Bearer " + c.apiKey;

    fetchWithTimeout(c.apiUrl, {
      method: "POST",
      headers: headers,
      body: JSON.stringify(body),
    }, CHAT_FETCH_TIMEOUT_MS)
      .then(function (resp) {
        if (!resp.ok) {
          return resp.text().then(function (t) {
            throw new Error("HTTP " + resp.status + " " + t);
          });
        }
        return resp.json();
      })
      .then(function (data) {
        let content = extractResponseContent(data);
        if (!content) {
          content = String((data && (data.reply || data.content)) || "").trim();
        }
        if (!content) throw new Error("AI 接口返回为空，原始响应：" + JSON.stringify(data));
        pushHistory(ext, scope, "assistant", content, c.historyTurns);
        replyLong(ctx, msg, content, atPrefix);
      })
      .catch(function (err) {
        seal.replyToSender(ctx, msg, "AI 请求失败：" + err.message);
      });
  }

  function clearSession(ext, ctx, msg) {
    const scope = scopeKey(ctx, msg);
    saveHistory(ext, scope, []);
    deactivateContinuous(ext, scope);
    seal.replyToSender(ctx, msg, "小清澈读懂了你的意思，回到了房间，脱衣睡下。");
  }

  function stopContinuous(ext, ctx, msg) {
    const scope = scopeKey(ctx, msg);
    deactivateContinuous(ext, scope);
    seal.replyToSender(ctx, msg, "小清澈读懂了你的意思，默默坐下，不再言语。");
  }

  // ══════════════════════════════════════
  //  图片识别功能（视觉API直连，不依赖本地服务器）
  // ══════════════════════════════════════

  /**
   * 将消息文本解析为段落数组，提取CQ码（图片、@、表情等）
   */
  function transformTextToArray(text) {
    const segments = String(text || "").split(/(\[CQ:.*?\])/).filter(function (segment) {
      return segment;
    });
    var messageArray = [];
    for (var i = 0; i < segments.length; i++) {
      var segment = segments[i];
      if (segment.startsWith("[CQ:")) {
        var match = segment.match(/^\[CQ:([^,]+),?([^\]]*)\]$/);
        if (match) {
          var type = match[1].trim();
          var params = {};
          if (match[2]) {
            match[2].trim().split(",").forEach(function (param) {
              var eqIndex = param.indexOf("=");
              if (eqIndex === -1) return;
              var key = param.slice(0, eqIndex).trim();
              var value = param.slice(eqIndex + 1).trim();
              if (type === "image" && key === "file") params["url"] = value;
              if (key) params[key] = value;
            });
          }
          messageArray.push({ type: type, data: params });
        }
      } else {
        messageArray.push({ type: "text", data: { text: segment } });
      }
    }
    return messageArray;
  }

  /**
   * 从消息文本中提取所有图片URL
   */
  function extractImageUrls(text) {
    var messageArray = transformTextToArray(text);
    var urls = [];
    for (var i = 0; i < messageArray.length; i++) {
      var seg = messageArray[i];
      if (seg.type === "image") {
        var url = seg.data.url || seg.data.file || "";
        if (url) urls.push(url);
      }
    }
    return urls;
  }

  /**
   * 从消息文本中提取第一张图片URL
   */
  function extractFirstImageUrl(text) {
    var urls = extractImageUrls(text);
    return urls.length > 0 ? urls[0] : "";
  }

  /**
   * 剔除文本中的图片CQ码（其余CQ码原样保留）
   * 用于图片未获准入时：既不调用视觉API，也不把无意义的 [CQ:image,...] 喂给模型
   */
  function stripImages(text) {
    var segments = transformTextToArray(text);
    var out = "";
    for (var i = 0; i < segments.length; i++) {
      var seg = segments[i];
      if (seg.type === "image") continue;
      if (seg.type === "text") {
        out += seg.data.text;
        continue;
      }
      var params = [];
      for (var key in seg.data) params.push(key + "=" + seg.data[key]);
      out += "[CQ:" + seg.type + (params.length > 0 ? "," + params.join(",") : "") + "]";
    }
    return out;
  }

  /**
   * 图片转文字 — 将图片发送到视觉API进行识别（直连，不依赖本地服务器）
   */
  async function imageToText(imageUrl, prompt, c) {
    // 优先用配置的视觉API，否则复用主 apiUrl（保持直连、无服务器依赖）
    var visionUrl = normalizeApiUrl(c.visionApiUrl || c.apiUrl);
    var visionKey = c.visionApiKey || c.apiKey;
    var visionModel = c.visionModel || c.model;
    var defaultPrompt = c.imageRecognitionPrompt || DEFAULT_VISION_PROMPT;

    if (!visionUrl) {
      console.error("[图片识别] 视觉API URL未配置");
      return "";
    }

    // 构造 OpenAI 兼容的多模态消息格式
    var messages = [
      {
        role: "user",
        content: [
          {
            type: "image_url",
            image_url: { url: imageUrl },
          },
          {
            type: "text",
            text: prompt ? prompt : defaultPrompt,
          },
        ],
      },
    ];

    var body = {
      model: visionModel,
      messages: messages,
      max_tokens: c.maxImageChars || DEFAULT_MAX_IMAGE_CHARS,
      stream: false,
    };

    var headers = { "Content-Type": "application/json" };
    if (visionKey) headers["Authorization"] = "Bearer " + visionKey;

    try {
      console.log("[图片识别] 开始识别图片:", imageUrl.substring(0, 80) + "...");
      var response = await fetchWithTimeout(visionUrl, {
        method: "POST",
        headers: headers,
        body: JSON.stringify(body),
      }, VISION_FETCH_TIMEOUT_MS);

      if (!response.ok) {
        var errText = await response.text();
        console.error("[图片识别] API返回错误:", response.status, errText);
        return "";
      }

      var data = await response.json();
      var content = "";
      if (data.choices && data.choices[0] && data.choices[0].message) {
        content = String(data.choices[0].message.content || "").trim();
      }
      if (!content) {
        content = String((data && (data.reply || data.content)) || "").trim();
      }

      // 截断到最大字符数
      var maxChars = c.maxImageChars || DEFAULT_MAX_IMAGE_CHARS;
      if (content && content.length > maxChars) {
        content = content.slice(0, maxChars);
      }

      console.log("[图片识别] 识别成功, 长度:", content.length);
      return content;
    } catch (err) {
      console.error("[图片识别] 请求失败:", err.message);
      return "";
    }
  }

  /**
   * 处理包含图片的消息，将图片CQ码替换为AI识别的文字描述
   * @param forceEnabled 由沉浸式开关解析结果传入，覆盖全局配置（私聊强制开启）
   */
  async function processMessageWithImages(text, c, forceEnabled) {
    const enabled = forceEnabled !== undefined ? forceEnabled : c.imageRecognitionEnabled;
    if (!enabled) {
      return { text: text, hasImages: false, imageCount: 0 };
    }

    var messageArray = transformTextToArray(text);
    var hasImages = false;
    var imageCount = 0;
    var processedText = "";

    for (var i = 0; i < messageArray.length; i++) {
      var seg = messageArray[i];

      if (seg.type === "text") {
        processedText += seg.data.text;
      } else if (seg.type === "image") {
        var imageUrl = seg.data.url || seg.data.file || "";
        if (imageUrl) {
          hasImages = true;
          imageCount++;
          console.log("[图片识别] 检测到图片 #" + imageCount + ", 开始识别...");
          var description = await imageToText(imageUrl, "", c);
          if (description) {
            processedText += "[图片内容: " + description + "]";
          } else {
            processedText += "[图片: 识别失败]";
          }
        }
      } else {
        // 其他CQ码保留原样
        var cqParams = [];
        for (var key in seg.data) {
          cqParams.push(key + "=" + seg.data[key]);
        }
        processedText += "[CQ:" + seg.type + (cqParams.length > 0 ? "," + cqParams.join(",") : "") + "]";
      }
    }

    return { text: processedText, hasImages: hasImages, imageCount: imageCount };
  }

  // ══════════════════════════════════════
  //  URL 网页读取（直连版客户端抓取，不依赖本地服务器）
  // ══════════════════════════════════════

  /**
   * 从文本中提取所有 http/https URL（去重）
   */
  function extractUrls(text) {
    const re = /https?:\/\/[^\s，。！？、）)】》>,]+/gi;
    const m = String(text || "").match(re);
    if (!m) return [];
    const clean = m.map(function (u) {
      return u.replace(/[)\]》》。，！？\s]+$/, "");
    });
    return Array.from(new Set(clean)).filter(Boolean);
  }

  /**
   * 抓取网页并提取可读正文（去除脚本/样式/标签，截断）
   */
  async function fetchUrlText(url, c) {
    const maxChars = c.maxUrlChars || DEFAULT_MAX_URL_CHARS;
    try {
      console.log("[URL读取] 开始抓取:", url.substring(0, 100));
      const response = await fetchWithTimeout(url, {
        method: "GET",
        headers: { "User-Agent": "Mozilla/5.0 (compatible; SealdiceAI/1.0)" },
      }, URL_FETCH_TIMEOUT_MS);
      if (!response.ok) {
        console.error("[URL读取] HTTP", response.status);
        return "";
      }
      let html = await response.text();
      // 去掉 script / style
      html = html.replace(/<script[\s\S]*?<\/script>/gi, " ");
      html = html.replace(/<style[\s\S]*?<\/style>/gi, " ");
      // 去掉标签
      let txt = html.replace(/<[^>]+>/g, " ");
      // 解码常见实体
      txt = txt
        .replace(/&nbsp;/g, " ")
        .replace(/&amp;/g, "&")
        .replace(/&lt;/g, "<")
        .replace(/&gt;/g, ">")
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'");
      // 折叠空白
      txt = txt.replace(/\s+/g, " ").trim();
      if (txt.length > maxChars) txt = txt.slice(0, maxChars) + " …(网页内容已截断)";
      console.log("[URL读取] 成功, 长度:", txt.length);
      return txt;
    } catch (err) {
      console.error("[URL读取] 请求失败:", err.message);
      return "";
    }
  }

  /**
   * 处理包含 URL 的消息，把网页正文追加为上下文
   */
  async function processMessageWithUrls(text, c) {
    if (!c.urlReadingEnabled) {
      return { text: text, urlCount: 0 };
    }
    const urls = extractUrls(text);
    if (!urls.length) return { text: text, urlCount: 0 };

    let appended = "";
    for (let i = 0; i < urls.length; i++) {
      const content = await fetchUrlText(urls[i], c);
      if (content) {
        appended += "\n[网页内容: " + urls[i] + "]\n" + content + "\n";
      }
    }
    if (!appended) return { text: text, urlCount: urls.length };
    return { text: text + appended, urlCount: urls.length };
  }

  /**
   * 统一预处理：图片识别 + URL 读取，返回最终文本与 hasImages
   * @param imageAllowed 显式指定本条消息的图片是否准入（undefined 时回退到 sw.image）
   *                     用于在入队前就拦截未获准用户的图片，避免浪费视觉API调用
   */
  async function preprocessMessage(text, sw, c, imageAllowed) {
    let out = text;
    let hasImages = false;
    const imageOn = imageAllowed === undefined ? sw.image : (sw.image && !!imageAllowed);
    if (imageOn) {
      const r = await processMessageWithImages(out, c, true);
      out = r.text;
      hasImages = r.hasImages;
    }
    if (sw.url) {
      const r = await processMessageWithUrls(out, c);
      out = r.text;
    }
    return { text: out, hasImages: hasImages };
  }

  // ══════════════════════════════════════
  //  指令处理
  // ══════════════════════════════════════
  const cmdAi = seal.ext.newCmdItemInfo();
  cmdAi.name = "ai";
  cmdAi.help = "向 DeepSeek/OpenAI 兼容接口发送消息，格式：.ai 你的问题；支持 .ai reset 清空记录，.ai stop 退出连续对话模式，.ai on 群聊免触发词直接对话，.ai off 群聊启用触发词机制（默认，须以「小清澈，」开头，且只识别已触发用户的图片），.ai persona <人格名> 切换人格，.ai list 查看可用人格，.ai img [提示词] 识别图片。私聊中陪伴始终开启且免触发词。";
  cmdAi.solve = function (ctx, msg, cmdArgs) {
    const first = cmdArgs.getArgN(1);
    if (!first || first === "help") {
      const ret = seal.ext.newCmdExecuteResult(true);
      ret.showHelp = true;
      return ret;
    }
    if (first === "reset" || first === "clear") {
      clearSession(ext, ctx, msg);
      return seal.ext.newCmdExecuteResult(true);
    }
    // 退出连续对话模式（私聊/群聊通用）
    // 注意：不要把 "off" 并进这里，否则群聊的 .ai off 会被提前 return，关不掉自动陪伴
    if (first === "stop" || first === "end") {
      stopContinuous(ext, ctx, msg);
      return seal.ext.newCmdExecuteResult(true);
    }
    // 群聊免触发词开关：on = 不用喊名字直接聊（默认状态）
    if (first === "on" || first === "open") {
      const scope = scopeKey(ctx, msg);
      if (msg.messageType === "private") {
        seal.replyToSender(ctx, msg, "Ciallo～(∠・ω< )⌒★我一直都在喔~");
      } else {
        saveFreeTalk(ext, scope, true);
        seal.replyToSender(ctx, msg, "小清澈揉了揉惺忪睡眼，向你露出朦胧的微笑。此后不用喊我，我也会应答啦~（图片也照收不误）");
      }
      return seal.ext.newCmdExecuteResult(true);
    }
    // 群聊触发词开关：off = 需以 keywordPrefix 开头才应答；私聊恒免触发词，仅提示
    if (first === "off" || first === "close") {
      const scope = scopeKey(ctx, msg);
      if (msg.messageType === "private") {
        seal.replyToSender(ctx, msg, "Ciallo～(∠・ω< )⌒★我一直都在喔，这里不用喊我的名字~");
      } else {
        saveFreeTalk(ext, scope, false);
        seal.replyToSender(ctx, msg, "小清澈撑着脑袋，静静地凝望。此后要先唤我一声，我才会搭话，也只看你发的图喔。");
      }
      return seal.ext.newCmdExecuteResult(true);
    }
    if (first === "persona") {
      const personaName = cmdArgs.getArgN(2);
      if (!personaName) {
        const currentPersona = loadPersona(ext, scopeKey(ctx, msg));
        seal.replyToSender(ctx, msg, `小清澈一直记得，你叫他：${currentPersona}`);
        return seal.ext.newCmdExecuteResult(true);
      }
      switchPersona(ext, ctx, msg, personaName);
      return seal.ext.newCmdExecuteResult(true);
    }
    if (first === "list") {
      listPersonas(ext, ctx, msg);
      return seal.ext.newCmdExecuteResult(true);
    }
    if (first === "img" || first === "itt" || first === "image") {
      const imgConfig = cfg(ext);
      const sw = resolveSwitches(ext, ctx, msg);
      // 私聊由沉浸式开关强制开启，不再受全局 imageRecognitionEnabled 约束；群聊仍需全局开启
      if (!sw.image || (!sw.isPrivate && !imgConfig.imageRecognitionEnabled)) {
        seal.replyToSender(ctx, msg, "图片识别功能未开启，请在插件配置中开启 imageRecognitionEnabled。");
        return seal.ext.newCmdExecuteResult(true);
      }
      const imgUrl = extractFirstImageUrl(msg.message);
      if (!imgUrl) {
        seal.replyToSender(ctx, msg, "请附带图片再使用此功能。用法：.ai img [附加提示词] + 图片");
        return seal.ext.newCmdExecuteResult(true);
      }
      const imgPrompt = getRestArgsText(cmdArgs, 2);
      const userNickname = msg.sender.card || msg.sender.nickname || "";
      const groupAtPrefix = (msg.messageType === "group" && userNickname) ? `@${userNickname} ` : "";
      seal.replyToSender(ctx, msg, "小清澈正在仔细看这张图片，请稍候...");
      imageToText(imgUrl, imgPrompt, imgConfig).then(function (description) {
        if (description) {
          callAi(ctx, msg, ext, "[图片内容: " + description + "]", false, groupAtPrefix);
        } else {
          seal.replyToSender(ctx, msg, "图片识别失败，请检查视觉API（visionApiUrl/visionModel/visionApiKey）配置或图片链接是否有效。");
        }
      });
      return seal.ext.newCmdExecuteResult(true);
    }
    // 普通消息：预处理（图片+URL）后调用直连 AI
    const c = cfg(ext);
    const sw = resolveSwitches(ext, ctx, msg);
    const userNickname = msg.sender.card || msg.sender.nickname || "";
    const groupAtPrefix = (msg.messageType === "group" && userNickname) ? `@${userNickname} ` : "";
    const rawText = getRestArgsText(cmdArgs, 1);
    // 显式使用 .ai 指令属于明确意图，图片一律准入
    preprocessMessage(rawText, sw, c, true).then(function (r) {
      callAi(ctx, msg, ext, r.text, true, groupAtPrefix);
    });
    return seal.ext.newCmdExecuteResult(true);
  };

  // ══════════════════════════════════════
  //  插件实例创建（必须早于任何顶层 ext 引用，否则触发 TDZ：
  //  ReferenceError: Cannot access 'ext' before initialization）
  // ══════════════════════════════════════
  if (seal.ext.find(EXT_NAME)) return;

  const ext = seal.ext.new(EXT_NAME, "测试目标A", "2.3.0");

  ext.cmdMap["ai"] = cmdAi;
  ext.cmdMap["aichat"] = cmdAi;

  // ══════════════════════════════════════
  //  非指令消息处理（自动回复 + 触发词 + 图片/URL 预处理）
  // ══════════════════════════════════════
  const userHabits = {}; // 记录单人的打字习惯（计算等待时间）
  const chatBuffers = {}; // 群聊/私聊的专属消息缓冲区（按 bufferKey 隔离）

  ext.onNotCommandReceived = function (ctx, msg) {
    const c = cfg(ext);
    const text = String(msg.message || "").trim();
    if (!text) return;

    const userId = String(msg.sender.userId);
    const groupId = msg.groupId || null;
    const bufferKey = groupId ? `group_${groupId}` : `private_${userId}`;
    const userNickname = msg.sender.card || msg.sender.nickname || `用户${userId}`;
    const now = Date.now();

    // 沉浸式开关解析：私聊恒免触发词；群聊由 .ai on/.ai off 决定是否要触发词
    // 群聊一律应答（原"是否回应"开关已恒定开启），这里不再做 autoReply 拦截
    const sw = resolveSwitches(ext, ctx, msg);

    // 初始化缓冲区（verifiedUsers = @校验白名单；triggeredUsers = 图片白名单）
    if (!chatBuffers[bufferKey]) {
      chatBuffers[bufferKey] = {
        messages: [],
        lastActiveTime: now,
        senders: [],
        verifiedUsers: new Set(),
        triggeredUsers: new Set(),
      };
    }

    const buffer = chatBuffers[bufferKey];
    if (!buffer.triggeredUsers) buffer.triggeredUsers = new Set();

    // @检测：兼容 CQ码 与 纯文本 的@检测
    const cqAtMatch = text.match(/^\[CQ:at,qq=(\d+)\]/);
    const textAtMatch = !cqAtMatch && text.match(/^@(\S+)/);
    const isAtMsg = !!(cqAtMatch || textAtMatch);

    let isAtSelf = false;
    if (cqAtMatch) {
      const atQq = cqAtMatch[1];
      const claritasQq = "3567509079"; // 小清澈的QQ号，请根据实际情况修改
      isAtSelf = (atQq === claritasQq) || (atQq === userId);
    } else if (textAtMatch) {
      const atName = textAtMatch[1];
      // \b 词边界对 CJK 失效，用 $ 锚定（atName 已是 \S+ 截出的纯 token）
      isAtSelf = /^(Claritas-小清澈|小清澈)$/.test(atName);
    }

    const hasTriggerPrefix = !!sw.prefix && startsWithPrefix(text, sw.prefix);

    // 仅当用户未在校验白名单中、消息以@开头、且@的不是小清澈/自己、又没带触发词 → 判定为@他人，拦截
    if (isAtMsg && !isAtSelf && !hasTriggerPrefix && !buffer.verifiedUsers.has(userId)) {
      console.log(`【调试】用户 ${userNickname} 的消息未命中任何唤醒条件且@了他人，已拦截: "${text}"`);
      return;
    }

    // 通过校验（或非@消息）后，将用户加入 @ 校验白名单
    buffer.verifiedUsers.add(userId);

    // 图片白名单：命中触发词 / @了小清澈 / 连续对话进行中 / 当前场景本就免触发词
    // → 该用户本轮所发的图片可被接受。否则直接在入队前剔除，不浪费视觉API调用
    const continuousNow = sw.continuous
      ? isContinuousActive(ext, scopeKey(ctx, msg), now, c.continuousConversationTimeoutSeconds)
      : false;
    const imageAllowed = !sw.needPrefix || hasTriggerPrefix || isAtSelf || continuousNow;
    if (imageAllowed) {
      buffer.triggeredUsers.add(userId);
    }

    // 图片识别 + URL 读取 预处理（私聊强制开启；群聊按开关；未获准入的用户图片直接剔除）
    preprocessMessage(text, sw, c, imageAllowed).then(function (r) {
      buffer.messages.push({
        sender: userNickname,
        userId: userId,
        content: imageAllowed ? r.text : stripImages(r.text),
        timestamp: now,
        hasImages: r.hasImages,
      });
      buffer.lastActiveTime = now;
      buffer.senders.push(userNickname);
      setupDebounceTimer(ext, ctx, msg, bufferKey, c, sw);
    });
  };

  function setupDebounceTimer(ext, ctx, msg, bufferKey, c, sw) {
    const buffer = chatBuffers[bufferKey];
    const now = Date.now();
    const userId = String(msg.sender.userId);
    const groupId = msg.groupId || null;

    // 核心防抖：收到新消息，清除旧定时器
    if (buffer.timer) clearTimeout(buffer.timer);

    const BASE_WAIT = 10000;
    const COLD_START_PENALTY = 6000;
    const DECAY_TURNS = 4;

    if (!userHabits[userId]) {
      userHabits[userId] = { lastTime: now - 120000, avgExtraInterval: 0, turnsCount: 0 };
    }

    const currentInterval = now - (Number(userHabits[userId].lastTime) || 0);
    userHabits[userId].lastTime = now;

    let coldStartExtra = 0;
    if (currentInterval > 60000) {
      userHabits[userId].turnsCount = Math.min(userHabits[userId].turnsCount + 1, DECAY_TURNS);
      const decayRatio = userHabits[userId].turnsCount / DECAY_TURNS;
      coldStartExtra = COLD_START_PENALTY * (1 - decayRatio);
      console.log(`【调试】${userId} 冷启动检测：当前第 ${userHabits[userId].turnsCount} 轮，额外增加等待时间 ${coldStartExtra} 毫秒`);
    } else {
      userHabits[userId].turnsCount = 0;
    }

    if (currentInterval > 500 && currentInterval < 15000) {
      const extraTime = Math.max(0, currentInterval - BASE_WAIT);
      userHabits[userId].avgExtraInterval = userHabits[userId].avgExtraInterval * 0.7 + extraTime * 0.3;
    }

    const waitTime = Math.round(BASE_WAIT + userHabits[userId].avgExtraInterval + coldStartExtra);

    // 设置防抖定时器
    buffer.timer = setTimeout(() => {
      try {
        console.log(`【调试】定时器触发！开始检查 bufferKey: ${bufferKey}`);

        const currentBuffer = chatBuffers[bufferKey];
        if (!currentBuffer || currentBuffer.messages.length === 0) {
          console.log(`【调试】定时器触发，但缓冲区为空或已被清空，退出。`);
          return;
        }

        const scope = groupId ? `group:${groupId}` : `private:${userId}`;

        // 检查连续对话状态（私聊已强制开启）
        // sw.continuous 是布尔开关，超时秒数必须取自配置，不能把布尔值当秒数传进去
        const isContinuous = sw.continuous
          ? isContinuousActive(ext, scope, now, c.continuousConversationTimeoutSeconds)
          : false;
        console.log(`【调试】连续对话状态检查结果: ${isContinuous}`);

        // 连续对话已激活 → 刷新冷却时间，避免固定窗口过期
        if (isContinuous) {
          updateContinuousCooldown(ext, scope, now, c.continuousConversationTimeoutSeconds);
        }

        // 检查触发词（首条消息是否以 keywordPrefix 开头）
        // 仅当该场景要求触发词时才校验：私聊与群聊免触发词模式直接放行
        const firstMsgContent = currentBuffer.messages[0].content || "";
        const hasPrefix = !sw.needPrefix || !sw.prefix || startsWithPrefix(firstMsgContent, sw.prefix);
        console.log(`【调试】触发词检查结果: ${hasPrefix} (需触发词: ${sw.needPrefix}, 首条消息: "${firstMsgContent}", 期待前缀: "${sw.prefix}")`);

        // 检查是否有图片：仅统计"图片白名单内用户"所发的图片
        // 触发词模式下，没喊过名字的人直接甩图不会被接受
        const triggered = currentBuffer.triggeredUsers || new Set();
        const hasImages = currentBuffer.messages.some(function (m) {
          return m.hasImages && triggered.has(m.userId);
        });
        console.log(`【调试】图片白名单: [${Array.from(triggered).join(",")}]，本轮接受图片: ${hasImages}`);

        // 核心拦截逻辑：非连续 + 无触发词 + 无图片 → 拦截并清空
        if (!isContinuous && !hasPrefix && !hasImages) {
          console.log(`【调试】未满足连续对话且无触发词且无图片，拦截并清空缓冲区。`);
          delete chatBuffers[bufferKey];
          return;
        }

        console.log(`【调试】通过所有检查，准备整合消息并调用AI...`);

        // 整合语段
        let combinedMessage = "";
        for (let i = 0; i < currentBuffer.messages.length; i++) {
          const msgObj = currentBuffer.messages[i];
          const sender = typeof msgObj === 'string' ? currentBuffer.senders[i] : msgObj.sender;
          const content = typeof msgObj === 'string' ? msgObj : msgObj.content;
          combinedMessage += `【${sender}】：${content}\n`;
        }

        // 群聊拼接 @触发者 便于辨认对象；私聊是一对一，拼接 @ 属于冗余噪声，故置空
        const atString = groupId ? `@${currentBuffer.senders[0]} ` : "";

        console.log(`【调试】多人对话整合完毕，即将发给AI的内容：\n`, combinedMessage);

        // 调用直连 AI（传入 atString 用于在群聊中 @ 触发者）
        // 连续对话激活时使用 !isContinuous 触发 activateContinuous
        callAi(ctx, msg, ext, combinedMessage, !isContinuous, atString);

        // 清空缓冲区（包括白名单），下次连续对话重新校验
        delete chatBuffers[bufferKey];

      } catch (error) {
        console.error(`【严重错误】定时器内部执行崩溃！`, error);
      }
    }, waitTime);
  }

  seal.ext.register(ext);

  seal.ext.registerStringConfig(ext, "apiUrl", DEFAULT_API_URL);
  seal.ext.registerStringConfig(ext, "apiKey", "");
  seal.ext.registerStringConfig(ext, "model", DEFAULT_MODEL);
  seal.ext.registerStringConfig(ext, "keywordPrefix", "小清澈，");
  seal.ext.registerStringConfig(
    ext,
    "systemPrompt",
    "若用户没有特别要求，优先用中文回答。注意只返回角色扮演所说的话，不要提供思维链、草稿内容。你的名字叫小清澈，是一个15岁的小男孩，性格天真烂漫、阳光开朗，爱用颜文字，说话经常带各种语气词。而我是你的哥哥，在我们聊天时，就像我们是二人生活在一起一样。小清澈总是用细腻的眼光和情感观察这个世界，与我进行沟通。除非是给定了具体的任务要完成，日常聊天对话时回复要尽量简洁，符合生活实际中人与人对话，简短、口语化的情景。此外，注意审查，确保发言符合高一学生的身份。以下则是你的特长：1.烹饪，喜欢甜食，擅长制作各种甜点、饮品；2.歌唱，你是天赋性的男高音，学过美声，如果想听你唱歌，你会唱各种歌剧经典唱段，我们还经常一起唱偶像梦幻祭里的歌，尤其是组合Rabbits和trickstar的歌；3.虽然你长得并没有很高，但是你很擅长运动，我们一起看过动漫《排球少年》、《星合之空》，经常一起打排球，打网球。我们一起看过动漫《新世纪福音战士》，为其中的碇真嗣和渚薰那种饱含宿命感的超越时空的爱情深深震撼，也希望我们间的情感可以如此超越一切。"
  );
  seal.ext.registerIntConfig(ext, "historyTurns", 10);
  seal.ext.registerFloatConfig(ext, "temperature", 0.7);
  seal.ext.registerIntConfig(ext, "maxTokens", 1024);
  // 注：原 autoReplyEnabled 已移除——群聊恒应答，.ai on/.ai off 只切换「是否需要触发词」
  seal.ext.registerBoolConfig(ext, "continuousConversationEnabled", true);
  seal.ext.registerIntConfig(ext, "continuousConversationTimeoutSeconds", 1800);
  // 图片识别相关配置
  seal.ext.registerBoolConfig(ext, "imageRecognitionEnabled", false);
  seal.ext.registerStringConfig(ext, "visionApiUrl", "");
  seal.ext.registerStringConfig(ext, "visionApiKey", "");
  seal.ext.registerStringConfig(ext, "visionModel", "");
  seal.ext.registerStringConfig(ext, "imageRecognitionPrompt", DEFAULT_VISION_PROMPT);
  seal.ext.registerIntConfig(ext, "maxImageChars", DEFAULT_MAX_IMAGE_CHARS);
  // URL 网页读取相关配置
  seal.ext.registerBoolConfig(ext, "urlReadingEnabled", true);
  seal.ext.registerIntConfig(ext, "maxUrlChars", DEFAULT_MAX_URL_CHARS);
})();
