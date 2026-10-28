---
name: memory-system
description: 长期记忆：记录用户的事实、偏好和更正，按需检索；自动去重、更正失效、时间落地与衰减。用户说"记住"、提到过去或纠正旧信息时使用。
metadata: { "openclaw": { "emoji": "🧠", "requires": { "bins": ["python3"] } } }
---

# Memory System

本地、纯规则（默认 0 Token）的结构化记忆。区分 **Fact**（用户明确说的，可直接引用）
和 **Belief**（你推断的，要带不确定语气），自动处理重复、更正和过期。

所有命令都是：

```bash
python3 {baseDir}/src/memory.py <command> ...
```

数据目录默认是当前工作目录下的 `memory/`，可用环境变量 `MEMORY_DIR` 指定。
首次使用先运行 `init`。

## 什么时候写入

**要记**（用 `capture` 立即写入）：
- 用户明确要求："记住"、"以后都"、"别忘了"
- 身份、健康、安全：过敏、病史、姓名、联系方式
- 稳定偏好和关系："我讨厌香菜"、"张三是我导师"
- 更正旧信息："其实是……"、"我说错了"、"现在改成……"——直接 `capture` 新说法，
  系统会自动让旧记忆失效，不要手动删旧记忆
- 有日期的安排："下周三考试"——原样写入，系统会把相对时间换算成绝对日期，事件过后自动归档

**可以先缓冲**（用 `add-pending`，等 `consolidate` 批量处理）：对话中顺带提到、
重要性不确定的信息。

**不要记**：寒暄（"好的"、"哈哈"）、一次性查询（天气、计算、翻译）、
你自己的推测（除非用 `--type belief` 并给出 `--confidence`）、密码和密钥。

写入时用第一人称原话或 "用户……" 开头的完整句子，不要只写半句或代词。

```bash
python3 {baseDir}/src/memory.py capture "我对花生过敏"
python3 {baseDir}/src/memory.py capture --type belief --confidence 0.6 "用户可能在准备考研"
python3 {baseDir}/src/memory.py add-pending "周末去爬了香山"
```

## 什么时候检索

用户提到过去（"之前"、"上次"、"你还记得"）、问到偏好、人名、项目，或你需要
个性化回答时，先检索再回答：

```bash
python3 {baseDir}/src/memory.py search "花生" --json      # 精简索引：id + 摘要 + 分数
python3 {baseDir}/src/memory.py get f_20261028_477300      # 需要细节时按 id 取全文
```

先看 `search --json` 的摘要，确实需要时再 `get`，不要一次把所有记忆读进上下文。

### 怎么用检索结果

- `type: fact` 且分数高：可以直接引用
- `type: belief` 或 `confidence` < 0.8：用商量语气（"我记得你好像……？"）
- 结果互相矛盾时，以较新的为准，并向用户确认
- 检索不到就说不记得，不要编

## 定期整理

`consolidate` 跑完整的 7 阶段整理：处理 pending、去重、更正失效、生成摘要、
时间衰减、归档过期记忆、生成 Layer 1 快照。建议每天一次（同一天重复运行会自动跳过，
`--force` 强制执行）：

```bash
python3 {baseDir}/src/memory.py consolidate
```

Layer 1 快照在 `memory/layer1/snapshot.md`（约 2000 tokens，最重要的记忆）。
新会话开始、需要了解用户全貌时读它。

## 与 OpenClaw 自带记忆的关系

OpenClaw 自带的 `memory-core` 已经提供 `MEMORY.md` 注入、`memory_search` 混合检索
和 dreaming 后台整理。本 skill 不替代它，负责它没有的部分：

- Fact / Belief 区分和置信度
- 规则化的更正检测：旧记忆写入 `invalid_at` 后归档，不删除
- 相对时间落地和事件过期
- 完整操作历史 `memory/layer2/history.jsonl`，可审计、可回滚

推荐在 `AGENTS.md` 中加入一句："用户分享个人信息或纠正旧信息时，按 memory-system
skill 写入；回答涉及过去的问题前先用它检索。"

## 其他命令

| 命令 | 用途 |
|------|------|
| `status` / `stats` | 记忆数量、上次整理时间 |
| `inject "<用户消息>" --format json` | 按消息检索并输出可注入的记忆 |
| `archive <id>` | 手动归档一条记忆 |
| `validate` | 校验数据完整性 |
| `rebuild-index` | 重建检索索引 |

可选：设置 `OPENAI_API_KEY` 后，规则拿不准的内容会调用 LLM 兜底（见
[架构说明](references/architecture.md) 的 LLM 配置一节）。不设置则全部走规则。

架构、评分公式、衰减参数见 [references/architecture.md](references/architecture.md)。
