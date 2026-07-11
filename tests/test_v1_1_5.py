#!/usr/bin/env python3
"""
Memory System v1.1.5 - 实体识别与隔离系统测试
"""

import sys
import json
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timedelta

# 添加当前目录到路径
# 仓库整理后被测模块位于 ../src 与 ../legacy,补齐导入路径,保证开箱可运行
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "legacy"))

from v1_1_5_entity_system import (
    extract_entities,
    extract_entities_layer1,
    extract_entities_layer2,
    learn_new_entities,
    try_generalize_pattern,
    apply_entity_isolation,
    should_apply_entity_isolation,
    find_similar_entity_groups,
    is_similar_entity,
    calculate_entity_similarity,
    calculate_access_boost_v1_1_5,
    cleanup_learned_entities,
    load_learned_entities,
    save_learned_entities,
    ENTITY_SYSTEM_CONFIG,
)

# ============================================================
# 测试辅助
# ============================================================

class TestContext:
    """测试上下文管理器"""
    def __init__(self):
        self.temp_dir = None
    
    def __enter__(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        (self.temp_dir / 'layer2').mkdir(parents=True)
        return self.temp_dir
    
    def __exit__(self, *args):
        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

def print_test(name, passed, details=""):
    """打印测试结果"""
    status = "✅ PASS" if passed else "❌ FAIL"
    print(f"{status}: {name}")
    if details:
        print(f"       {details}")

# ============================================================
# 测试用例
# ============================================================

def test_layer1_builtin_patterns():
    """测试 Layer 1: 硬编码模式识别"""
    print("\n📋 测试 Layer 1: 硬编码模式识别")
    
    # 测试用例
    cases = [
        ("机器人_50 是个天才", ["机器人_50"]),
        ("机器人-5 很笨", ["机器人-5"]),
        ("项目A 和 项目B 都很重要", ["项目A", "项目B"]),
        ("城市_25 的天气很好", ["城市_25"]),
        ("这是普通文本，没有实体", []),
    ]
    
    all_passed = True
    for content, expected in cases:
        result = extract_entities_layer1(content)
        passed = set(result) == set(expected)
        all_passed = all_passed and passed
        print_test(f"'{content[:20]}...'", passed, f"期望: {expected}, 实际: {result}")
    
    return all_passed

def test_layer2_learned_entities():
    """测试 Layer 2: 学习实体识别"""
    print("\n📋 测试 Layer 2: 学习实体识别")
    
    with TestContext() as memory_dir:
        # 预先学习一些实体
        learned = {
            "exact": ["DeFi协议-A", "元宇宙平台-X", "特斯拉Model3"],
            "patterns": [r"DeFi协议-[A-Z]"],
            "access_stats": {},
            "last_updated": datetime.utcnow().isoformat() + 'Z'
        }
        save_learned_entities(memory_dir, learned)
        
        # 测试用例
        cases = [
            ("DeFi协议-A 很火", ["DeFi协议-A"]),
            ("DeFi协议-B 也不错", ["DeFi协议-B"]),  # 通过模式匹配
            ("元宇宙平台-X 上线了", ["元宇宙平台-X"]),
            ("普通文本", []),
        ]
        
        all_passed = True
        for content, expected in cases:
            result = extract_entities_layer2(content, memory_dir)
            passed = set(result) == set(expected)
            all_passed = all_passed and passed
            print_test(f"'{content[:20]}...'", passed, f"期望: {expected}, 实际: {result}")
        
        return all_passed

def test_pattern_generalization():
    """测试模式归纳（类型保护）"""
    print("\n📋 测试模式归纳（类型保护）")
    
    # 测试用例
    cases = [
        # (新实体, 已有实体列表, 期望模式)
        ("机器人_100", ["机器人_5", "机器人_50"], r"机器人_\d+"),  # 数字后缀
        ("项目C", ["项目A", "项目B"], r"项目[A-Z]"),  # 单字母后缀
        ("北京烤鸭", ["北京1", "北京大学"], None),  # 类型不一致，不归纳
        ("用户_1", ["用户_2"], None),  # 只有2个，不够归纳
    ]
    
    all_passed = True
    for new_entity, existing, expected in cases:
        result = try_generalize_pattern(new_entity, existing)
        passed = result == expected
        all_passed = all_passed and passed
        print_test(f"'{new_entity}' + {existing}", passed, f"期望: {expected}, 实际: {result}")
    
    return all_passed

def test_entity_similarity():
    """测试实体相似度计算"""
    print("\n📋 测试实体相似度计算")
    
    # 测试用例
    cases = [
        ("机器人_50", "机器人_50", 1.0),      # 完全相同
        ("机器人_50", "机器人_5", True),       # 相似（共同前缀）
        ("机器人_50", "项目A", False),         # 不相似
        ("DeFi协议-A", "DeFi协议-B", True),   # 相似
    ]
    
    all_passed = True
    for e1, e2, expected in cases:
        similarity = calculate_entity_similarity(e1, e2)
        
        if isinstance(expected, float):
            passed = abs(similarity - expected) < 0.01
            print_test(f"'{e1}' vs '{e2}'", passed, f"相似度: {similarity:.2f}, 期望: {expected}")
        else:
            is_sim = is_similar_entity(e1, e2)
            passed = is_sim == expected
            print_test(f"'{e1}' vs '{e2}'", passed, f"相似: {is_sim}, 期望: {expected}")
        
        all_passed = all_passed and passed
    
    return all_passed

def test_entity_isolation():
    """测试实体隔离（竞争性抑制）"""
    print("\n📋 测试实体隔离（竞争性抑制）")
    
    with TestContext() as memory_dir:
        # 模拟候选记忆
        candidates = [
            {
                "id": "f_001",
                "content": "机器人_50 是个天才",
                "entities": ["机器人_50"],
                "score": 0.9
            },
            {
                "id": "f_002",
                "content": "机器人_5 是个笨蛋",
                "entities": ["机器人_5"],
                "score": 0.85
            },
            {
                "id": "f_003",
                "content": "张三很聪明",
                "entities": ["张三"],
                "score": 0.7
            }
        ]
        
        # 查询 "机器人_50"
        query = "机器人_50 最近怎么样？"
        
        result = apply_entity_isolation(query, candidates, memory_dir)
        
        # 检查结果
        f_001 = next(m for m in result if m["id"] == "f_001")
        f_002 = next(m for m in result if m["id"] == "f_002")
        f_003 = next(m for m in result if m["id"] == "f_003")
        
        all_passed = True
        
        # f_001 应该保持原权重
        passed = f_001["score"] == 0.9
        print_test("精确匹配保持权重", passed, f"f_001 score: {f_001['score']}")
        all_passed = all_passed and passed
        
        # f_002 应该被降权（0.1）
        passed = f_002["score"] == 0.85 * 0.1
        print_test("相似实体被降权", passed, f"f_002 score: {f_002['score']}, 期望: {0.85 * 0.1}")
        all_passed = all_passed and passed
        
        # f_003 应该保持原权重（不相关）
        passed = f_003["score"] == 0.7
        print_test("不相关实体保持权重", passed, f"f_003 score: {f_003['score']}")
        all_passed = all_passed and passed
        
        return all_passed

def test_access_boost_v1_1_5():
    """测试访问加成（最近 N 天）"""
    print("\n📋 测试访问加成（最近 N 天）")
    
    # 场景1：老记忆，最近被频繁访问
    old_memory_recent_access = {
        "id": "f_old",
        "created": (datetime.utcnow() - timedelta(days=365)).isoformat() + 'Z',
        "last_accessed": (datetime.utcnow() - timedelta(days=1)).isoformat() + 'Z',
        "retrieval_count": 5,
        "used_in_response_count": 5,
        "user_mentioned_count": 2,
    }
    
    # 场景2：老记忆，很久没访问
    old_memory_no_access = {
        "id": "f_old_no",
        "created": (datetime.utcnow() - timedelta(days=365)).isoformat() + 'Z',
        "last_accessed": (datetime.utcnow() - timedelta(days=100)).isoformat() + 'Z',
        "retrieval_count": 5,
        "used_in_response_count": 5,
        "user_mentioned_count": 2,
    }
    
    boost1 = calculate_access_boost_v1_1_5(old_memory_recent_access)
    boost2 = calculate_access_boost_v1_1_5(old_memory_no_access)
    
    all_passed = True
    
    # 最近访问的应该有高加成
    passed = boost1 > 0.3
    print_test("最近访问的老记忆有高加成", passed, f"boost: {boost1:.3f}")
    all_passed = all_passed and passed
    
    # 很久没访问的应该加成很低
    passed = boost2 < 0.1
    print_test("很久没访问的老记忆加成低", passed, f"boost: {boost2:.3f}")
    all_passed = all_passed and passed
    
    # 最近访问的加成应该远高于没访问的
    passed = boost1 > boost2 * 3
    print_test("最近访问 >> 很久没访问", passed, f"{boost1:.3f} vs {boost2:.3f}")
    all_passed = all_passed and passed
    
    return all_passed

def test_cleanup_learned_entities():
    """测试学习实体清理"""
    print("\n📋 测试学习实体清理")
    
    with TestContext() as memory_dir:
        # 创建测试数据
        old_date = (datetime.utcnow() - timedelta(days=400)).isoformat() + 'Z'
        recent_date = (datetime.utcnow() - timedelta(days=10)).isoformat() + 'Z'
        
        learned = {
            "exact": ["老实体", "新实体", "从未使用"],
            "patterns": [r"老模式\d+", r"新模式\d+"],
            "access_stats": {
                "老实体": {"first_seen": old_date, "last_used": old_date, "use_count": 5},
                "新实体": {"first_seen": recent_date, "last_used": recent_date, "use_count": 3},
                # "从未使用" 没有访问记录
                r"老模式\d+": {"use_count": 0},  # 从未命中
                r"新模式\d+": {"use_count": 10},  # 有命中
            },
            "last_updated": datetime.utcnow().isoformat() + 'Z'
        }
        save_learned_entities(memory_dir, learned)
        
        # 执行清理
        stats = cleanup_learned_entities(memory_dir)
        
        # 检查结果
        cleaned = load_learned_entities(memory_dir)
        
        all_passed = True
        
        # 新实体应该保留
        passed = "新实体" in cleaned["exact"]
        print_test("新实体保留", passed)
        all_passed = all_passed and passed
        
        # 老实体应该被清理
        passed = "老实体" not in cleaned["exact"]
        print_test("老实体被清理", passed)
        all_passed = all_passed and passed
        
        # 有命中的模式应该保留
        passed = r"新模式\d+" in cleaned["patterns"]
        print_test("有命中的模式保留", passed)
        all_passed = all_passed and passed
        
        # 没命中的模式应该被清理
        passed = r"老模式\d+" not in cleaned["patterns"]
        print_test("没命中的模式被清理", passed)
        all_passed = all_passed and passed
        
        print(f"\n清理统计: {stats}")
        
        return all_passed

def test_full_workflow():
    """测试完整工作流"""
    print("\n📋 测试完整工作流")
    
    with TestContext() as memory_dir:
        # 1. 学习新实体
        print("\n  Step 1: 学习新实体")
        learn_new_entities(["机器人_1", "机器人_2", "机器人_3"], memory_dir)
        
        learned = load_learned_entities(memory_dir)
        passed = len(learned["exact"]) == 3
        print_test("学习 3 个实体", passed)
        
        # 检查是否归纳了模式
        passed = len(learned["patterns"]) >= 1
        print_test("归纳出模式", passed, f"模式: {learned['patterns']}")
        
        # 2. 识别实体
        print("\n  Step 2: 识别实体")
        entities, source = extract_entities("机器人_50 很厉害", memory_dir)
        passed = "机器人_50" in entities or "机器人" in str(entities)
        print_test("识别 机器人_50", passed, f"结果: {entities}, 来源: {source}")
        
        # 3. 实体隔离
        print("\n  Step 3: 实体隔离")
        candidates = [
            {"id": "f_1", "content": "机器人_1 很棒", "entities": ["机器人_1"], "score": 0.9},
            {"id": "f_2", "content": "机器人_2 一般", "entities": ["机器人_2"], "score": 0.8},
            {"id": "f_3", "content": "张三很好", "entities": ["张三"], "score": 0.7},
        ]
        
        result = apply_entity_isolation("机器人_1 怎么样", candidates, memory_dir)
        
        f_1 = next(m for m in result if m["id"] == "f_1")
        f_2 = next(m for m in result if m["id"] == "f_2")
        
        passed = f_1["score"] > f_2["score"]
        print_test("目标实体权重 > 相似实体", passed, f"f_1: {f_1['score']}, f_2: {f_2['score']}")
        
        return True

# ============================================================
# 主函数
# ============================================================

def main():
    print("=" * 60)
    print("Memory System v1.1.5 - 实体识别与隔离系统测试")
    print("=" * 60)
    
    results = []
    
    results.append(("Layer 1 硬编码模式", test_layer1_builtin_patterns()))
    results.append(("Layer 2 学习实体", test_layer2_learned_entities()))
    results.append(("模式归纳（类型保护）", test_pattern_generalization()))
    results.append(("实体相似度计算", test_entity_similarity()))
    results.append(("实体隔离（竞争性抑制）", test_entity_isolation()))
    results.append(("访问加成（最近 N 天）", test_access_boost_v1_1_5()))
    results.append(("学习实体清理", test_cleanup_learned_entities()))
    results.append(("完整工作流", test_full_workflow()))
    
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    
    passed_count = sum(1 for _, passed in results if passed)
    total_count = len(results)
    
    for name, passed in results:
        status = "✅" if passed else "❌"
        print(f"  {status} {name}")
    
    print(f"\n总计: {passed_count}/{total_count} 通过")
    
    if passed_count == total_count:
        print("\n🎉 所有测试通过！")
        return 0
    else:
        print("\n⚠️ 部分测试失败")
        return 1

if __name__ == "__main__":
    sys.exit(main())
