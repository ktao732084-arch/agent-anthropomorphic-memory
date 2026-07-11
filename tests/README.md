# 测试说明

所有测试可直接运行，无需设置 PYTHONPATH（文件头部已自动把 `../src` 和 `../legacy` 加入导入路径）：

```bash
python3 tests/test_v1.1.py
python3 tests/test_v1_1_5.py
python3 tests/test_v1_1_6.py
python3 tests/test_v1_1_7.py
python3 tests/test_concurrent.py
```

## 当前状态（2026-07）

| 文件 | 覆盖对象 | 结果 |
|------|---------|------|
| test_v1.1.py | legacy/v1_1_config + v1_1_helpers（重要性分级、时间敏感） | ✅ 全部通过 |
| test_v1_1_5.py | legacy/v1_1_5_entity_system（三层实体识别） | ✅ 8/8 |
| test_v1_1_6.py | src/memory.py（引号实体、去重阈值、冲突降权） | ⚠️ 11/17，见下 |
| test_v1_1_7.py | legacy/v1_1_7_llm_integration（语义复杂度、LLM 兜底） | ✅ 29/29 |
| test_concurrent.py | legacy/sqlite_backend_v1_2_5（线程安全） | ✅ 全部通过 |

## 已知失败（如实记录，待修）

**test_v1_1_6.py 有 6 个用例失败**：v1.1.6 引入的"冲突柔性降权"（OVERRIDE_SIGNALS
Tier 1/Tier 2 信号触发降权）和部分去重阈值行为，在后续版本迭代（v1.3.0 引入
MemoryOperator/ConflictResolver 接管去重路径）后已回归，现行代码不再触发这些降权。
失败用例：短句高重叠、包含关系、Tier 1"其实是"、Tier 2"逗你的"/"开玩笑"、花生冲突场景。

## 覆盖缺口（如实说明）

- `src/memory.py` 的 consolidation 主流程、`src/sqlite_backend.py`、
  `src/conflict_resolver.py`、`src/memory_operator.py`、`src/noise_filter.py`
  没有独立测试文件，仅有各模块 `__main__` 中的冒烟断言
  （如 `python3 src/noise_filter.py`，当前 13/14 通过）。
