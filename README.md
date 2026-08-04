# agy-proxy — Antigravity CLI 自有模型翻译代理(内部 Demo)

在**不改一行客户端代码、不碰服务端、不动计费**的前提下,让 Antigravity CLI 的推理由任意自有模型提供(已验证 Claude on Vertex 全 agentic 能力)。

> ⚠️ **定位:内部演示 / 可行性佐证。** 依赖本机自签 CA 解 TLS,且 Antigravity 客户端后台自更新可能随时改变协议。**不可下发给外部客户。** 面向客户的过渡方案请用 MCP 委派插件;正式解法是拿到客户端源码后重启 `*_COMPATIBLE`/BYOM 休眠通路。

---

## 1. 背景与关键结论

### 为什么需要它

Antigravity CLI 的模型受三层限制,端侧无任何配置文件可以加模型:

1. **编译进二进制的模型表** — Go 包 `google3/third_party/jetski/models/model_config`
   (`GetModelInfoMap` / `DefaultChatModelConfig` / `WithAllowBYOM`);
2. **服务端下发的可用列表** — `FetchAvailableModels` RPC
   (`google.internal.cloud.code.v1internal`,cloudcode-pa)+
   Codeium 系 `CascadeModelConfigs` / `CascadeAllowedModelsConfig`;
3. **服务端实验开关** — `CASCADE_DEFAULT_MODEL_OVERRIDE` / `CustomModelInfoOverride` /
   `SetBaseExperiments`。

二进制符号表同时证明:协议层完整保留了 Windsurf 时代的 BYOK 能力
(`MODEL_CLAUDE_4_OPUS_BYOK`、`MODEL_ANTHROPIC_COMPATIBLE`、`MODEL_BEDROCK_COMPATIBLE`、
`MODEL_GOOGLE_GEMINI_INTERNAL_BYOM` 等枚举),只是被产品策略在服务端关闭。

### 本方案为什么可行

实测确认了两个关键架构事实:

- **agy(Go 二进制)无证书锁定**,接受 `HTTPS_PROXY` + `SSL_CERT_FILE` 自签 CA;
- **cortex agent 循环在端侧运行**:GCP 项目登录模式下,推理是一条标准 JSON 的
  `POST …/publishers/google/models/<model>:streamGenerateContent?alt=sse`
  (Vertex GenerateContent 格式,含完整系统提示词、19 个工具定义、会话历史),
  服务端只是模型网关。因此在网络层做**协议翻译**即可换掉"大脑",
  无需伪造任何有状态的服务端语义。

### 架构

```
Antigravity TUI
   │ HTTPS (HTTPS_PROXY + 自签 CA)
   ▼
mitmproxy :18080 + translate.py
   ├── 非推理流量(OAuth/配额/实验/遥测/更新)──────▶ Google 原样透传
   └── 推理调用(*:generateContent)在代理短路:
         Gemini GenerateContentRequest
           ⇄ 双向翻译(消息/系统提示词/工具/工具结果)
         后端二选一:
           anthropic-vertex → Claude @ Vertex AI rawPredict(ADC 认证)
           openai           → 任意 OpenAI 兼容 endpoint(含本地 mock)
```

翻译失败 **fail-open**:该轮请求原样透传给真实 Google 后端,agy 永不挂死。

### 已验证能力(2026-07-27,agy v1.1.7 / claude-opus-4-8)

| 能力 | 状态 |
|---|---|
| 纯文本对话 | ✅ |
| 系统提示词 + 19 个工具定义翻译 | ✅ |
| Claude `tool_use` → Gemini `functionCall`(含单轮并行多工具) | ✅ |
| 权限确认对话框正常触发 | ✅ |
| 工具结果回传(`functionResponse` → `tool_result`,强制归入 user 消息) | ✅ |
| 多轮 agentic 任务闭环(bash + 文件写入 + 汇报) | ✅ |
| 登录 / 配额 / 实验 / 遥测透传 | ✅ 全部 200 |

---

## 2. 快速开始

```bash
cd agy-proxy
./install.sh     # 一键安装:依赖 + CA + agy + Vertex Claude 连通性自检
./start.sh       # 启动翻译代理
./agy-claude     # 用 Claude 后端启动 Antigravity CLI(agy 的全部参数原样透传)
./stop.sh        # 停止代理与 mock
```

前置条件(anthropic-vertex 后端):

- `gcloud` ADC 可用(`gcloud auth application-default print-access-token` 成功),
  且对配置的项目有 `aiplatform.endpoints.predict` 权限;
- 该项目已启用 Vertex 上的 Claude(Model Garden)。

首次启动 agy 需要完成一次 Google OAuth 登录(无头环境会打印 URL + 粘贴授权码),
登录流量经代理透传,与本方案无关。

## 3. 配置(`agyproxy.env`)

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGY_BACKEND` | `anthropic-vertex` | `anthropic-vertex` 或 `openai` |
| `CLAUDE_PROJECT` | `<your-gcp-project>` | 已启用 Vertex Claude 的 GCP 项目 |
| `CLAUDE_REGION` | `global` | Vertex region |
| `CLAUDE_MODEL` | `claude-opus-4-8` | 任何 Vertex 上可用的 Claude 型号 |
| `OPENAI_BASE_URL` | 本地 mock `:19090` | OpenAI 兼容 endpoint;换成真实地址即接任意模型 |
| `OPENAI_API_KEY` / `NEW_MODEL_ID` | — | openai 后端的 key 与模型名 |
| `PROXY_PORT` | `18080` | 代理端口 |

改完 `./start.sh` 重启即生效。`openai` 后端 + 默认 URL 时,start.sh 会自动拉起
`mock_openai.py`(自报家门的演示后端,用于验证链路本身)。

## 4. 文件清单

| 文件 | 作用 |
|---|---|
| `translate.py` | mitmproxy addon:拦截判定、Gemini⇄Anthropic / Gemini⇄OpenAI 双向翻译、fail-open、样本落盘 |
| `mock_openai.py` | OpenAI 兼容 mock 后端(链路验证用) |
| `agyproxy.env` | 全部配置 |
| `install.sh` / `start.sh` / `stop.sh` | 安装 / 启停 |
| `agy-claude` | 带代理环境变量启动 agy 的包装器 |
| `dumps/` | 每轮推理的原始请求/翻译后请求/响应样本(做正式方案的 schema 参考) |
| `flows.log` / `mitmdump.out` | 流量日志 / 代理运行日志(看 `[inference] answered locally by …` 确认接管) |

## 5. 翻译要点(踩坑记录,写正式版必读)

1. **推理端点随登录方式不同**:GCP 项目登录 → `aiplatform.googleapis.com`
   (裸 GenerateContentRequest,SSE 无包装);Google 账号登录 → `cloudcode-pa.googleapis.com`
   (Code Assist 包装,SSE 外面多一层 `{"response": …}`)。translate.py 两者都处理,
   按 host 决定响应是否加包装。
2. **Gemini schema 类型是大写**(`OBJECT`/`STRING`),转 Anthropic `input_schema`
   前须递归转小写(`lc_schema`)。
3. **`tool_result` 必须在 user 消息里**:Antigravity 的 `functionResponse` 出现在
   role=`model` 的内容中,直接映射会被 Anthropic API 400。translate.py 强制把所有
   tool_result 拆到独立的 user 消息。
4. **tool_use id 配对**:Gemini 侧不保证回传 `functionCall.id`,translate.py 用
   FIFO 队列(`pending_tool_ids`)按序配对未带 id 的 `functionResponse`。
5. **自我调用防环**:后端本身也走 aiplatform 域名时,放行
   `/publishers/anthropic/` 路径,避免代理拦截自己的后端调用。
6. **禁止拦截其余 RPC**:`FetchAvailableModels`、`GetUserStatus`、实验、配额等
   一律透传——伪造它们意味着重写整个有状态后端,没有必要。

## 6. 计费说明

- 拦截命中时请求在代理短路,**不会发往 Gemini**,Google 侧零调用零计费;
- 唯一计费点是后端模型(Vertex Claude 按 Vertex 价格记到 `CLAUDE_PROJECT`);
- 翻译失败 fail-open 的那一轮会真实打到 Gemini(若项目有权限则由 Gemini 计费),
  单轮永远只有一个模型成功响应,不存在双重计费;
- **成本注意**:未加 prompt caching,agy 每轮携带完整上下文(>70KB),
  Claude 全额输入计费。长期使用应在 `to_anthropic()` 中给 system 与历史
  加 `cache_control` 断点(约省 90% 输入成本)。

## 7. 已知边界

- 响应为单块 SSE,非逐 token 流式(体感:短暂等待后整段出现);
- Gemini `thinkingConfig` 被丢弃(Claude 按无思考运行;需要时可映射为
  `thinking: {"type": "adaptive"}`);
- 状态栏与系统提示词仍显示/自称 Gemini——换的是大脑,不是铭牌;
- agy 后台自更新可能改变协议或加证书锁定,更新后需回归测试;
- 图片/多模态 parts 未翻译(遇到即 fail-open 透传)。

## 8. 接入新模型指南

按新模型的 API 协议分三种情况:

### A. OpenAI 兼容(Kimi / DeepSeek / Qwen / vLLM / 多数内部网关)—— 只改配置

`translate.py` 的 OpenAI 路径已支持完整 agentic 映射(工具调用历史、
`role:"tool"` 结果回传、FIFO id 配对,与 Claude 路径同构)。以 Kimi 为例:

```bash
# agyproxy.env
AGY_BACKEND=openai
OPENAI_BASE_URL=https://api.moonshot.cn/v1     # 国际版 api.moonshot.ai
OPENAI_API_KEY=sk-xxxx
NEW_MODEL_ID=<型号名>    # 先 curl $BASE/models -H "Authorization: Bearer $KEY" 查准确 ID

./start.sh && ./agy-claude
```

验证顺序:先发一句纯文本 → 再跑一个"创建文件并汇报"类工具任务,
盯 `mitmdump.out` 里的 `[inference] answered locally by <model>`。

### B. Anthropic 协议(其他 Claude 部署)—— 改常量或小改

同一 Vertex 项目换型号:改 `CLAUDE_MODEL` 即可。要接 Anthropic 官方 API /
Bedrock 上的 Claude,复制 `call_claude_vertex()` 换 URL 和认证头(≈10 行)。

### C. 私有协议 —— 加一对函数(≈100 行)

参照 `to_anthropic()`/`anthropic_to_gcr()` 写 `to_X()`/`x_to_gcr()`,
在 `Translator.request()` 加一个 `BACKEND == "x"` 分支。需要处理的映射面:
system 提示词、多轮消息、工具定义(注意 schema 大写转小写)、
工具调用与结果的 id 配对、finishReason 与 usage。`dumps/` 里的真实样本
就是你的测试夹具(可参考本 README 第 5 节的踩坑清单)。

**通用注意**:上下文窗口——agy 单轮请求可超 70KB(系统提示词 + 19 工具 +
历史),上下文小于 128K 的模型跑长会话会溢出;工具调用能力弱的模型
(不支持并行调用、arguments 常输出非法 JSON)在 agentic 任务上表现会明显打折,
`openai_to_gcr()` 对非法 JSON arguments 已做兜底(包成 `{"_raw": ...}`)。

## 9. 后续路线

| 阶段 | 方案 | 状态 |
|---|---|---|
| 今天可交付客户 | MCP 委派插件(新模型做执行者,内置模型做编排) | 设计就绪 |
| 内部 demo | 本代理 | ✅ 已验证 |
| 正式方案 | 客户端源码:重启 `*_COMPATIBLE`/BYOM 通路 + 本地模型注册 + flag 门控 | 待源码权限 |
