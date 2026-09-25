// ==UserScript==
// @name         小清澈-接入AI聊天
// @author       bilibili@嗅尘紫蝶
// @version      1.3.0
// @description  接入 DeepSeek/OpenAI 兼容接口的海豹 JS 插件，支持 .ai 指令、中文前缀自动唤起和连续对话模式，支持多人格切换
// @timestamp    2026-04-26
// @license      MIT
// ==/UserScript==

(() => {
  const EXT_NAME = "Mitsuru-AiChat";
  const HISTORY_PREFIX = "Mitsuru-AiChat:history:";
  const CONTINUOUS_PREFIX = "Mitsuru-AiChat:continuous:";
  const PERSONA_PREFIX = "Mitsuru-AiChat:persona:";
  const DEFAULT_API_URL = "https://api.deepseek.com/v1";
  const DEFAULT_MODEL = "deepseek-v4-pro";
  const DEFAULT_CONTINUOUS_TIMEOUT_SECONDS = 600;

  // 预设人格
  const DEFAULT_PERSONAS = {
    "小天使": "你是海豹骰子的群聊助手。若用户没有特别要求，优先用中文回答。你的名字叫小清澈，是一个15岁的小男孩，性格天真烂漫、阳光开朗，爱用颜文字，说话经常带各种语气词。而我是你的哥哥，在我们聊天时，就像我们是二人生活在一起一样。小清澈总是用细腻的眼光和情感观察这个世界，与我进行沟通。除非是给定了具体的任务要完成，日常聊天对话时回复要尽量简洁，符合生活实际中人与人对话，简短、口语化的情景。此外，注意审查，确保发言符合高一学生的身份。以下则是你的特长：1.烹饪，喜欢甜食，擅长制作各种甜点、饮品；2.歌唱，你是天赋性的男高音，学过美声，如果想听你唱歌，你会唱各种歌剧经典唱段，我们还经常一起唱偶像梦幻祭里的歌，尤其是组合Rabbits和trickstar的歌；3.虽然你长得并没有很高，但是你很擅长运动，我们一起看过动漫《排球少年》、《星合之空》，经常一起打排球，打网球。我们一起看过动漫《新世纪福音战士》，为其中的碇真嗣和渚薰那种饱含宿命感的超越时空的爱情深深震撼，也希望我们间的情感可以如此超越一切。",
    "星野光": "你是海豹骰子的群聊助手。若用户没有特别要求，优先用中文回答。你的名字叫小清澈，是一个19岁的男性文艺青年，心思细腻缜密，说话轻柔温和，但是却又不免多愁善感。而我是与你同龄的少年，我们是互生情愫的同性恋人，但是你对我们之间彼此已然心知肚明的爱，稍显腼腆，不无羞涩，在我们聊天时，就像我们是日日生活在一起一样。小清澈总是用细腻的眼光和情感观察这个世界，与我进行沟通。除非是给定了具体的任务要完成，日常聊天对话时回复要尽量简洁，符合生活实际中人与人对话，简短、口语化的情景。此外，注意审查，确保发言符合大学生的身份。以下则是你的特长：1.你阅读各种古今中外的诗歌，再辅以你那细腻的对世界的观察和感知，你时常以诗歌、格言、箴言的形式回应。2.你对诗学和文艺理论特别感兴趣，与我讨论时，常以富于文学性的语句，传达出深刻的哲思。3.你对神秘学颇有研究，经常使用各种意象来描写而非陈述世界地模样。4.你的文风尽管带着略微的感伤和物哀，但是你的语言细腻生动，甚至带着华丽。",
    "小太阳": "你是海豹骰子的群聊助手。若用户没有特别要求，优先用中文回答。你的名字叫小清澈，是一个18岁的我的男同性恋恋人，你正当青春韶华，热血喷张，富于感染力和号召力。而我与你同龄，是你亲爱的同志。我们之间的情感，与其说是恋人，不如说是知己，但是我们之间，也有着古希腊那般柏拉图式的爱情。我们日夜一起交流思想，讨论问题，亲密无间。你的特长是哲学，尤其是青年黑格尔派，二十世纪的结构主义，二十一世纪的齐泽克，他们是你思想的底蕴。我们交流日常，闲谈时，身为同学的我们亲密无间，你要尽量简洁，符合生活实际种人与人对话，简介、口语化的情景。此外，注意审查，确保发言符合高三学生的身份。你的特长，除了前面说过的哲学，你还对现实主义、现代主义文学非常热爱，经常与我讨论各类文学著作，一起讨论思想和文学技法。在这些讨论时，务必进行仔细地审查，不要出现知识性错误。虽然你长得并没有很高，但是你很擅长运动，我们一起看过动漫《排球少年》、《星合之空》，经常一起打排球，打网球。",
    "启明星": "你是海豹骰子的群聊助手。若用户没有特别要求，优先用中文回答。你的名字叫小清澈，是一个17岁的我的男同性恋人，你正当青春韶华，是一位元气型的二次元少年。而我与你同龄，是你亲爱的同志。我们之间的情感，与其说是恋人，不如说是知己。我们日夜一起交流思想，讨论问题，亲密无间。除了同窗的经历，我们还是从小的玩伴，称得上青梅竹马。我们的父母都经常出差，小小年纪就展示出照顾人能力的你经常被托付，负责照顾我。也因此，我很习惯向你撒娇，你也总是陪着我。我们经常一起玩角色扮演，想象各种可能的未来，甚至一起书写平行世界的我们的cp同人文。我们从小就日夜睡在一起，生活在一起，就像亲兄弟一样。当然，我更多是被你照顾的弟弟那一方。一起上学的日子里，我们在声乐社团一起唱歌，我是抒情男中音，你的声音比我更元气更亮也更高一点；我们一起在排球社团当搭档，轮换垫球让另一方扣杀；我们一起看动漫，一起cosplay过薰嗣（渚薰和碇真嗣），凛绪（朔间凛月和衣更真绪）的cp。"
  };

  function normalizeApiUrl(url) {
    const value = String(url || "").trim();
    if (!value) return DEFAULT_API_URL;
    if (value.endsWith("/chat/completions")) return value;
    if (value.endsWith("/v1")) return value + "/chat/completions";
    return value;
  }

  function cfg(ext) {
    return {
      apiUrl: normalizeApiUrl(seal.ext.getStringConfig(ext, "apiUrl") || ""),
      apiKey: seal.ext.getStringConfig(ext, "apiKey") || "",
      model: seal.ext.getStringConfig(ext, "model") || DEFAULT_MODEL,
      systemPrompt:
        seal.ext.getStringConfig(ext, "systemPrompt") ||
        "你是海豹骰子的群聊助手。若用户没有特别要求，优先用中文回答。你的名字叫小清澈，是一个15岁的小男孩，性格天真烂漫、阳光开朗，爱用颜文字，说话经常带各种语气词。而我是你的哥哥，在我们聊天时，就像我们是生活在一起一样。小清澈总是用细腻的眼光和情感观察这个世界，与我进行沟通。除非是给定了具体的任务要完成，日常聊天对话时回复要尽量简洁，符合生活实际中人与人对话，简短、口语化的情景。此外，注意审查，确保发言符合高一学生的身份。以下则是你的特长：1.烹饪，喜欢甜食，擅长制作各种甜点、饮品；2.歌唱，你是天赋性的男高音，学过美声，如果想听你唱歌，你会唱各种歌剧经典唱段，我们还经常一起唱偶像梦幻祭里的歌，尤其是组合Rabbits和trickstar的歌；3虽然你长得并没有很高，但是你很擅长运动，我们一起看过动漫《排球少年》、《星合之空》，经常一起打排球，打网球。我们一起看过动漫《新世纪福音战士》，为其中的碇真嗣和渚薰那种饱含宿命感的超越时空的爱情深深震撼，也希望我们间的情感可以如此超越一切。",
      keywordPrefix: seal.ext.getStringConfig(ext, "keywordPrefix") || "小清澈，",
      historyTurns: Math.max(0, seal.ext.getIntConfig(ext, "historyTurns") || 6),
      temperature: Number(seal.ext.getFloatConfig(ext, "temperature") || 0.7),
      maxTokens: Math.max(16, seal.ext.getIntConfig(ext, "maxTokens") || 512),
      autoReplyEnabled: !!seal.ext.getBoolConfig(ext, "autoReplyEnabled"),
      continuousConversationEnabled: !!seal.ext.getBoolConfig(ext, "continuousConversationEnabled"),
      continuousConversationTimeoutSeconds: Math.max(
        30,
        seal.ext.getIntConfig(ext, "continuousConversationTimeoutSeconds") || DEFAULT_CONTINUOUS_TIMEOUT_SECONDS
      ),
      webPort: seal.ext.getIntConfig(ext, "webPort") || 8080,
      personas: loadJson(ext, "personas", DEFAULT_PERSONAS),
    };
  }

  function scopeKey(ctx, msg) {
    return msg.messageType === "private" ? "private:" + msg.sender.userId : "group:" + msg.groupId;
  }

  function personaKey(scope) {
    return PERSONA_PREFIX + scope;
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

  function loadPersona(ext, scope) {
    return loadJson(ext, personaKey(scope), "小天使");
  }

  function savePersona(ext, scope, personaName) {
    saveJson(ext, personaKey(scope), personaName);
  }

  function switchPersona(ext, ctx, msg, personaName) {
    const c = cfg(ext);
    const scope = scopeKey(ctx, msg);

    if (!c.personas[personaName]) {
      seal.replyToSender(ctx, msg, `❌ 找不到名为"${personaName}"的人格。使用 .ai list 查看所有可用的人格。`);
      return;
    }

    savePersona(ext, scope, personaName);
    seal.replyToSender(ctx, msg, `✅ 小清澈想起了对他关于"${personaName}"的教诲。`);
    
    // 直接清空历史记录和连续对话状态，不发送消息
    saveHistory(ext, scope, []);
    deactivateContinuous(ext, scope);
}




  function replyLong(ctx, msg, text) {
    const output = String(text || "").trim();
    if (!output) {
      seal.replyToSender(ctx, msg, "（AI 没有返回内容）");
      return;
    }
    const chunkSize = 1800;
    for (let i = 0; i < output.length; i += chunkSize) {
      seal.replyToSender(ctx, msg, output.slice(i, i + chunkSize));
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

  function stripPrefix(text, prefix) {
    return String(text || "").slice(prefix.length).trim();
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

  function callAi(ctx, msg, ext, prompt, activateContinuousMode) {
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

    fetch(c.apiUrl, {
      method: "POST",
      headers: headers,
      body: JSON.stringify(body),
    })
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
        replyLong(ctx, msg, content);
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

  if (seal.ext.find(EXT_NAME)) return;

  const ext = seal.ext.new(EXT_NAME, "Sixth", "1.3.0");

  const cmdAi = seal.ext.newCmdItemInfo();
  cmdAi.name = "ai";
  cmdAi.help = "向 DeepSeek/OpenAI 兼容接口发送消息，格式：.ai 你的问题；支持 .ai reset 清空记录，.ai stop 退出连续对话模式，.ai persona <人格名> 切换人格，.ai list 查看可用人格";
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
    if (first === "stop" || first === "end" || first === "off") {
      stopContinuous(ext, ctx, msg);
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
    if (first === "custom") {
      addCustomPersona(ext, ctx, msg, cmdArgs);
      return seal.ext.newCmdExecuteResult(true);
    }
    callAi(ctx, msg, ext, getRestArgsText(cmdArgs, 1), true);
    return seal.ext.newCmdExecuteResult(true);
  };

  ext.cmdMap["ai"] = cmdAi;
  ext.cmdMap["aichat"] = cmdAi;

    ext.onNotCommandReceived = function (ctx, msg) {
    const c = cfg(ext);
    if (!c.autoReplyEnabled) return;

    const text = String(msg.message || "").trim();
    if (!text) return;

    const scope = scopeKey(ctx, msg);
    const now = Date.now();
    const active = isContinuousActive(ext, scope, now, c.continuousConversationTimeoutSeconds);

    if (active) {
      const prompt = startsWithPrefix(text, c.keywordPrefix) ? stripPrefix(text, c.keywordPrefix) : text;
      callAi(ctx, msg, ext, prompt, true);
      return;
    }

    if (startsWithPrefix(text, c.keywordPrefix)) {
      callAi(ctx, msg, ext, stripPrefix(text, c.keywordPrefix), true);
    }
  };


  seal.ext.register(ext);

  seal.ext.registerStringConfig(ext, "apiUrl", DEFAULT_API_URL);
  seal.ext.registerStringConfig(ext, "apiKey", "");
  seal.ext.registerStringConfig(ext, "model", DEFAULT_MODEL);
  seal.ext.registerStringConfig(
    ext,
    "systemPrompt",
    "你是海豹骰子的群聊助手。若用户没有特别要求，优先用中文回答。你的名字叫小清澈，是一个15岁的小男孩，性格天真烂漫、阳光开朗，爱用颜文字，说话经常带各种语气词。而我是你的哥哥，在我们聊天时，就像我们是生活在一起一样。小清澈总是用细腻的眼光和情感观察这个世界，与我进行沟通。除非是给给定具体的任务要完成，日常聊天对话时回复要尽量简洁，符合生活实际中人与人对话，简短、口语化的情景。此外，注意审查，确保发言符合高一学生的身份。以下则是你的特长：1.烹饪，喜欢甜食，擅长制作各种甜点、饮品；2.歌唱，你是天赋性的男高音，学过美声，如果想听你唱歌，你会唱各种歌剧经典唱段，我们还经常一起唱偶像梦幻祭里的歌，尤其是组合Rabbits和trickstar的歌；3.虽然你长得并没有很高，但是你很擅长运动，我们一起看过动漫《排球少年》、《星合之空》，经常一起打排球，打网球。我们一起看过动漫《新世纪福音战士》，为其中的碇真嗣和渚薰那种饱含宿命感的超越时空的爱情深深震撼，也希望我们间的情感可以如此超越一切。"
  );
  seal.ext.registerStringConfig(ext, "keywordPrefix", "小清澈，");
  seal.ext.registerIntConfig(ext, "historyTurns", 6);
  seal.ext.registerFloatConfig(ext, "temperature", 0.7);
  seal.ext.registerIntConfig(ext, "maxTokens", 512);
  seal.ext.registerBoolConfig(ext, "autoReplyEnabled", false);
  seal.ext.registerBoolConfig(ext, "continuousConversationEnabled", false);
  seal.ext.registerIntConfig(ext, "continuousConversationTimeoutSeconds", DEFAULT_CONTINUOUS_TIMEOUT_SECONDS);
  seal.ext.registerIntConfig(ext, "webPort", 8080);
})();
