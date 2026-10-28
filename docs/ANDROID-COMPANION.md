# Android 大肥鱼接入

新增 `src/companion_api.py` 将现有记忆引擎暴露为带令牌的快照处理接口；不依赖 DSH，也不改变原有 CLI 的使用方式。

Android 客户端与完整运行说明位于 [dafeiyu-android/memory-service](https://github.com/ktao732084-arch/dafeiyu-android/tree/main/memory-service)。手机保存主副本，服务不将其写入你原有的个人记忆目录。适配器调用本项目的噪声过滤、重要性、时间落地、去重/更正、历史归档、索引、检索、可选 embeddings 和 LLM 写入判断；不将模型生成的猜测转为用户事实，不启用旧版可选的任意提取器、QMD 命令或宿主调度。

## 启动

Python 3.10+，默认仅需标准库：

```powershell
$env:MEMORY_SERVICE_TOKEN = python -c 'import secrets; print(secrets.token_urlsafe(32))'
# 将此变量的值填写到手机「记忆引擎」的访问令牌中，不要提交到 Git
python -X utf8 src/companion_api.py --host 127.0.0.1 --port 8787
```

USB 联调运行 `adb reverse tcp:8787 tcp:8787`，手机服务地址填 `http://127.0.0.1:8787`。监听其他地址需要 `--certfile` / `--keyfile`；Android 其他地址只接受 HTTPS。

服务读取 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MEMORY_LLM_JUDGE_MODEL` 和 `MEMORY_EMBEDDING_*` 配置。客户端 `judge` / `semantic` 开关决定本次是否请求这些能力。没有聊天 Key 就不启用写入模型；没有向量 provider 就保留关键词检索。ONNX 仍使用本项目原有实现和依赖，权重不随服务分发。

## 协议 1

所有请求带 `Authorization: Bearer <MEMORY_SERVICE_TOKEN>`。

- `GET /health`：协议版本、引擎来源提交、已配置的 provider，不返回 Key。
- `POST /v1/evaluate`：`protocol=1`、`now`（毫秒）、`utc_offset_minutes`、`query`、`state`、`incoming`、`semantic`、`judge`。
- `state` 包含 `memories` / `archive` / `audit` / `vector_cache`。每条原话记录必须有 `id`、`text`、`source`、`at`；可带 `pinned`、合并来源 `sources`、原引擎字段 `engine`。
- `incoming` 最多 3 条，只应传用户有依据的原话。相对时间的解析文本放在 `engine.content`，不会覆盖 `text`。按说出时的手机日期解析，事件失效时间按手机 UTC 偏移转换。
- 返回新的 `state`、检索 ID 列表 `hits`、被过滤项 `rejected`、配置能力 `features`、降级标记 `degraded`。查询会记录召回次数；过期清理无需等待新写入。

120 条活跃、240 条归档、512 条整理事件、3 MiB HTTP 请求上限；鉴权失败 401，输入不合法 400，过大 413，工作槽满 429，引擎异常 503。服务只允许两个并发工作进程，单次 110 秒上限，子进程环境、目录和索引独立；父进程负责临时目录清理。它不是公开多租户托管平台。

## 测试与已知局限

```powershell
python -X utf8 tests/test_companion_api.py
python -X utf8 eval/run_companion_eval.py --json companion-results.json
```

评测复用 `eval/cases` 的 84 个场景，不修改预期。规则模式仍是 145/166，隐含变化、同义表达等限制继续公开保留。Android 通过真实 HTTP 重放同一批场景，验证协议与引擎行为一致；真实云模型质量、真实 ONNX 权重和手机整机体验需另测。

集成同时收紧 `llm_judge`：编号不接受布尔值、浮点数或字符串冒充数组；截断响应和 `reasoning_content` 不能触发旧事实失效。字符串形式的整数编号仍兼容。

已合入上游 `9862c93` 的思考模型 token 预算修复：`MEMORY_LLM_JUDGE_MAX_TOKENS` 默认 2048。该上游提交报告语义检索 + DeepSeek 模型达到 164/166；本次跨端联调仅独立复测规则模式和受控模型接口，不把上游报告记作 Android 真机或本次真实模型实测。
