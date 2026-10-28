# 对标成熟项目：做成 Agent Memory Skill 的优化路线

> 2026-10 调研。对照对象：OpenClaw 自带 `memory-core`、mem0、Graphiti / Zep、claude-mem。
> 均读的是各项目 2026-10 下旬的源码和文档。

## 一句话结论

**OpenClaw 自带的 `memory-core` 已经覆盖了本项目大部分"基础设施"卖点**：`MEMORY.md` 每次会话自动注入；
`memory_search` 混合检索（向量 + BM25，按 相关度 × 时间衰减 × 重要性 排序，再用 MMR 去重）；
dreaming 后台整理（light → REM → deep 三阶段，按阈值晋升到 `MEMORY.md`）；压缩上下文前自动保存记忆。

所以不该在"检索"和"整理"上和宿主正面竞争，而是做宿主**没有**的那一层：
**结构化事实层**。具体包括 Fact / Belief 和置信度、更正检测与失效、时间落地、可审计的历史。

## 能力对照

| 能力 | OpenClaw memory-core | mem0 | Graphiti / Zep | claude-mem | 本项目（本次之后） |
|------|------|------|------|------|------|
| 存储 | Markdown + SQLite | 向量库 + SQLite 历史 | 时序知识图谱 | SQLite + Chroma | JSONL（SQLite 后端实验中） |
| 检索 | 混合 + 衰减 + MMR | 向量 + BM25 + 可选 rerank | 图 + 混合 | 三层渐进披露 | 关键词倒排 + 实体，`search --json` → `get` 渐进披露 |
| 写入决策 | dreaming 阈值晋升 + LLM 选合并/取代 | LLM 只 ADD + 哈希去重 + 关联已有记忆 | LLM 抽边 + 时间失效 | LLM 压缩观察 | 规则：重复 / 合并 / 更正失效 |
| 更正处理 | supersede（取代） | 新增并关联旧记忆，不改写旧记忆 | 旧边写 `invalid_at`，保留历史 | 无专门处理 | ✅ 旧记忆写 `invalid_at` / `superseded_by`，移入归档 |
| 时间 | 日记 30 天半衰期 | 相对时间按"观察日期"换算为绝对日期 | 双时间轴（事件时间 / 录入时间） | 时间线视图 | ✅ 相对时间落地 + `valid_at` / `event_date` / 事件后过期 |
| 历史审计 | 改写前备份 `MEMORY.md` | history 表 | 边的时间戳 | 原始观察 | ✅ `history.jsonl` |
| 来源可信度 | provenance taint gate | 按 user / agent 归属 | 按 episode 溯源 | `<private>` 标签 | ❌ 待做 |
| 自动捕获 | 压缩前 flush、session 摄入 | SDK 调用方触发 | SDK 调用方触发 | 生命周期 hooks | ❌ 只能靠模型主动调用 |
| 评测 | — | LoCoMo | LongMemEval | 有 evals 目录 | ❌ 待做 |

## 本次已落地

| 改动 | 借鉴自 | 说明 |
|------|--------|------|
| 更正 → 失效而非删除 | Graphiti `resolve_edge_contradictions` | 强更正（Tier 1）给旧记忆写 `invalid_at`、`superseded_by` 并移入归档池；弱更正（Tier 2）只降权 |
| 操作历史 | mem0 history 表 | `layer2/history.jsonl` 记录 ADD / NOOP / MERGE / INVALIDATE / DOWNGRADE / ARCHIVE，MERGE 带 `old_content`，可回滚 |
| 相对时间落地 | mem0 抽取提示词"按 Observation Date 换算" | `src/temporal.py`：今天/明天/下周三/下个月15号等换算成绝对日期并标注在原文；未来事件在事件次日过期归档 |
| 渐进披露检索 | claude-mem 三层检索 | `search --json` 只返回 id + 摘要 + 分数；`get <id>` 取全文（含归档池） |
| SKILL.md 规范化 | OpenClaw skill 规范 | `metadata.openclaw` 替代旧的 `clawdbot`；一行描述（<160 字）；用 `{baseDir}` 路径；写清楚"何时写 / 何时查 / 怎么用结果"；长文档移到 `references/` |
| 修复失效的集成说明 | OpenClaw 文档 | 旧 SKILL.md 让用户配 `systemPromptFiles`，OpenClaw 没有这个配置项，Layer 1 快照实际从未被自动注入 |
| 噪声过滤误伤 | — | `2026-11-04`、电话号码会被当成算式过滤掉，已修正 |
| 去掉启动噪音 | — | 可选模块缺失的警告默认不再打印（`MEMORY_DEBUG=1` 可打开）；`utcnow` 弃用警告已消除 |

## 长期记忆端到端评测（2026-10-28）

用 MemoryBank、PerLTQA、LoCoMo 和作者本人的真实对话做了端到端评测（`eval/longterm/`）：
公开集 33%、私有集 50%，而"完美记忆"（全文给 LLM）是 79% / 90%。归因后的优先级：

1. **AI 说过的话也要记**（评测开关验证：中文公开集 +18 个百分点）
2. **衰减不能等于硬遗忘**：衰减只影响排序，归档池也要能检索（验证：PerLTQA +16 个百分点）
3. **检索改用 BM25 并做长度归一化**：长消息霸占检索结果，是私有集的主要失败原因
4. **写入时提炼事实**（mem0 式抽取），不要原样存整条长消息

详见 `eval/longterm/README.md`。

## 下一步（按优先级）

### P0：先能证明效果

1. ✅ **已完成**：见 `eval/`，84 个用例，基线 73%，第一轮修复后 87%，剩余问题见 `eval/README.md`。原计划：**建一个小评测集**。50–100 段中文对话，覆盖：更正、时间、偏好变化、噪声、多实体，
   每段写好期望留下的记忆和期望检索结果。思路参考 LoCoMo / LongMemEval，但规模小到能在
   CI 里跑。没有评测，后面每一项"优化"都无法证明有用。
2. **把 Layer 1 真正送进上下文**。三种做法：
   - 写入 `MEMORY.md` 里的托管区块：最简单，但 dreaming 也会重写 `MEMORY.md`，需要验证不会冲掉这个区块
   - 写到 `memory/*.md`：会被 `memory_search` 索引到，但不会自动注入
   - 做成插件，用 `before_prompt_build` hook 注入：claude-mem 在 OpenClaw 上就是这么做的，最可靠

### P1：补齐质量短板

3. **可选的 LLM 抽取**，用 mem0 式提示词：
   - 只做 ADD，并关联已有记忆的 id
   - 记录变化时把"从什么变成什么"写进同一条记忆
   - 去掉代词，每条记忆单独读也能看懂

   规则路径保留，作为 0 Token 兜底。
4. ✅ **语义相似度（部分完成）**：`src/embeddings.py` 支持 OpenAI 兼容接口或本地 ONNX 模型，
   用于语义检索和匹配带更正词的旧记忆，评测 87% → 92%。实测 embedding 无法区分"换词的不同事实"
   与"同义改写"，所以没有用于合并；隐式更正和同义去重留给下一项。
4b. ✅ **LLM 判断**：`src/llm_judge.py`，规则判为新增且有相关旧记忆时，让 LLM 判断"重复 / 取代 / 新增"。
   语义检索 + DeepSeek-V4.1-Flash 评测 164/166 (99%)，14 个"应新增"的对照判断无一误判。
   剩余 2 个失败是极短查询和跨语言检索，属于 embedding 模型能力问题。
5. **来源门控**（参考 OpenClaw provenance）。只有用户本人的消息能产生 Fact；网页、工具输出、
   子 Agent 的内容最多存成 Belief，或直接不存，防止记忆被投毒。
6. **作用域**（参考 mem0 的 user / agent / run 过滤）。多 Agent 或多用户共用时必需。

### P2：从 skill 升级为插件

7. skill 只能靠模型自觉调用，自动化程度有天花板。下一步可以做成 OpenClaw memory 插件
   （`kind: "memory"`）或 Claude Code 插件，用生命周期 hook 自动捕获：
   - claude-mem：SessionStart 注入，PostToolUse / Stop 记录
   - OpenClaw：`before_prompt_build` 注入，`before_compaction` 时保存
8. 拆分 `memory.py`（4600+ 行），把未开源模块（vector、proactive、dashboard 等）的命令注册移出主流程。
9. 支持 `<private>` 标签，被包住的内容不进记忆（claude-mem）。

## 不建议照抄的

- **向量库 / 图数据库**：这些重型基础设施会丢掉"纯标准库、零依赖、本地运行"这个最大的差异点。
  语义能力优先复用宿主已有的 embedding。
- **把检索做得比宿主更强**：`memory-core` 的混合检索和 MMR 已经很成熟，投入产出比低。
