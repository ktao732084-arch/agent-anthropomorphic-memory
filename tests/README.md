# 测试说明

所有测试可直接运行，无需设置 PYTHONPATH（文件头部已自动把 `../src` 和 `../legacy` 加入导入路径）：

```bash
python3 tests/test_v1.1.py
python3 tests/test_v1_1_5.py
python3 tests/test_v1_1_6.py
python3 tests/test_v1_1_7.py
python3 tests/test_concurrent.py
python3 tests/test_dedup_regression.py
python3 tests/test_agent_features.py
python3 tests/test_embeddings.py   # 语义向量（用本地假服务测 OpenAI 兼容接口）
python3 tests/test_llm_judge.py    # LLM 写入判断（用本地假服务测链路）
python3 tests/test_collector.py    # 会话收集器（剥元数据外壳、修复非法转义）
python3 tests/test_consolidate_types.py  # summary 类型不导致 consolidate 崩溃
python3 src/temporal.py        # 相对时间换算自测
```

## 当前状态（2026-10）

| 文件 | 覆盖对象 | 结果 |
|------|---------|------|
| test_v1.1.py | legacy/v1_1_config + v1_1_helpers（重要性分级、时间敏感） | ✅ 全部通过 |
| test_v1_1_5.py | legacy/v1_1_5_entity_system（三层实体识别） | ✅ 8/8 |
| test_v1_1_6.py | src/memory.py（引号实体、去重阈值、冲突降权） | ✅ 18/18 |
| test_v1_1_7.py | legacy/v1_1_7_llm_integration（语义复杂度、LLM 兜底） | ✅ 29/29 |
| test_concurrent.py | legacy/sqlite_backend_v1_2_5（线程安全） | ✅ 全部通过 |
| test_dedup_regression.py | src/memory.py + memory_operator（无实体修正、完全重复、同批次去重、误合并、换词不合并、实体不同的更正、合并保留完整说法） | ✅ 8/8 |
| test_collector.py | src/collect_from_sessions.py（渠道元数据外壳、系统事件过滤、非法转义修复） | ✅ 3/3 |
| test_consolidate_types.py | consolidate 遇到 summary 类型不崩溃（回归） | ✅ 1/1 |
| test_llm_judge.py | src/llm_judge.py（输出解析、取代/重复链路、无候选不调用、出错按规则、提示词不含评测原句） | ✅ 9/9 |
| test_embeddings.py | src/embeddings.py（向量缓存、换模型失效、服务不可用降级、语义只用于更正不用于合并） | ✅ 8/8 |
| test_agent_features.py | CLI 端到端（时间落地、更正失效归档、history、search --json、get、过期归档） | ✅ 6/6 |

## 已修复的回归（2026-10）

v1.3.0 引入 MemoryOperator 接管去重路径后，"冲突柔性降权"和去重阈值一度失效，
test_v1_1_6.py 曾有 6 个用例失败。原因是 MemoryOperator 要求两条记忆有共同实体才比较
（默认配置下实体常为空），且按空格分词（中文没有空格，相似度恒为 0）。现已改为字符
2-gram 重叠率 + 分层修正信号，与 v1.1.6 的语义一致，并新增 test_dedup_regression.py。

已知局限：相似度是字面重叠，不是语义相似。像"我对花生过敏" → "逗你的，我其实最喜欢吃花生"
这类几乎不共享文字的反转仍检测不到，需要后续接入语义模型或 LLM。

## 覆盖缺口（如实说明）

- `src/memory.py` 的 consolidation 主流程、`src/sqlite_backend.py`、
  `src/conflict_resolver.py`、`src/noise_filter.py`
  没有独立测试文件，仅有各模块 `__main__` 中的冒烟断言
  （如 `python3 src/noise_filter.py`，当前 13/14 通过）。
