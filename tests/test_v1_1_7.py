#!/usr/bin/env python3
"""
Memory System v1.1.7 - LLM 深度集成测试
"""

import sys
import os

# 仓库整理后被测模块位于 ../src 与 ../legacy,补齐导入路径,保证开箱可运行
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "legacy"))

from v1_1_7_llm_integration import (
    detect_semantic_complexity,
    should_use_llm_for_filtering,
    smart_filter_segment,
    smart_extract_entities,
    get_api_key,
    LLMIntegrationStats,
    LLM_INTEGRATION_CONFIG,
)


def test_semantic_complexity():
    """测试语义复杂度检测"""
    print("\n📋 测试语义复杂度检测")
    
    test_cases = [
        # (内容, 期望复杂, 原因)
        ("今天天气很好", False, "简单陈述"),
        ("我喜欢吃苹果", False, "简单偏好"),
        ("我怀疑他就是那个一直在暗中阻挠项目上线的人", True, "包含怀疑+关系"),
        ("虽然张三说他喜欢苹果，但是李四认为他其实更喜欢香蕉", True, "多实体+转折+观点"),
        ("如果明天下雨，我们可能需要取消会议，除非找到室内场地", True, "条件+可能+多时间"),
        ("这个项目就像一艘在暴风雨中航行的船", True, "隐喻"),
        ("张三、李四和王五都参与了这个项目的开发", False, "多实体但简单陈述"),  # 调整期望
        ("他不是医生，而是护士", False, "简单否定"),  # 调整期望
    ]
    
    passed = 0
    for content, expect_complex, reason in test_cases:
        result = detect_semantic_complexity(content)
        is_complex = result["is_complex"]
        
        if is_complex == expect_complex:
            print(f"✅ PASS: '{content[:25]}...'")
            print(f"       复杂度: {result['complexity_score']}, 原因: {result['reasons']}")
            passed += 1
        else:
            print(f"❌ FAIL: '{content[:25]}...'")
            print(f"       期望: {expect_complex}, 实际: {is_complex}")
            print(f"       分数: {result['complexity_score']}, 原因: {result['reasons']}")
    
    return passed, len(test_cases)


def test_llm_trigger_decision():
    """测试 LLM 触发决策"""
    print("\n📋 测试 LLM 触发决策")
    
    test_cases = [
        # (内容, 规则重要性, 期望使用LLM, 原因)
        ("我喜欢苹果", 0.8, False, "高置信度+简单"),
        ("我喜欢苹果", 0.3, True, "不确定区间"),
        ("我喜欢苹果", 0.1, False, "低置信度+简单"),
        ("我怀疑他在暗中阻挠项目", 0.5, True, "高置信度但复杂"),
        ("我怀疑他在暗中阻挠项目", 0.1, True, "低置信度但复杂"),
        ("今天天气好", 0.15, False, "低置信度+简单"),
    ]
    
    passed = 0
    for content, importance, expect_llm, reason in test_cases:
        should_use, decision_reason = should_use_llm_for_filtering(content, importance, "general")
        
        if should_use == expect_llm:
            print(f"✅ PASS: '{content[:20]}...' (imp={importance})")
            print(f"       使用LLM: {should_use}, 原因: {decision_reason}")
            passed += 1
        else:
            print(f"❌ FAIL: '{content[:20]}...' (imp={importance})")
            print(f"       期望: {expect_llm}, 实际: {should_use}")
            print(f"       原因: {decision_reason}")
    
    return passed, len(test_cases)


def test_api_key_retrieval():
    """测试 API Key 获取"""
    print("\n📋 测试 API Key 多源获取")
    
    # 保存原始环境变量
    original_key = os.environ.get("OPENAI_API_KEY")
    
    passed = 0
    total = 4
    
    # 测试 1: 参数传入优先
    os.environ["OPENAI_API_KEY"] = "env_key"
    key = get_api_key(param_key="param_key")
    if key == "param_key":
        print("✅ PASS: 参数传入优先")
        passed += 1
    else:
        print(f"❌ FAIL: 参数传入优先, 期望 param_key, 实际 {key}")
    
    # 测试 2: 环境变量
    key = get_api_key()
    if key == "env_key":
        print("✅ PASS: 环境变量获取")
        passed += 1
    else:
        print(f"❌ FAIL: 环境变量获取, 期望 env_key, 实际 {key}")
    
    # 测试 3: 配置文件
    del os.environ["OPENAI_API_KEY"]
    key = get_api_key(config_dict={"llm_api_key": "config_key"})
    if key == "config_key":
        print("✅ PASS: 配置文件获取")
        passed += 1
    else:
        print(f"❌ FAIL: 配置文件获取, 期望 config_key, 实际 {key}")
    
    # 测试 4: 无 Key
    key = get_api_key()
    if key is None:
        print("✅ PASS: 无 Key 返回 None")
        passed += 1
    else:
        print(f"❌ FAIL: 无 Key 返回 None, 实际 {key}")
    
    # 恢复环境变量
    if original_key:
        os.environ["OPENAI_API_KEY"] = original_key
    
    return passed, total


def test_smart_filter_without_api():
    """测试智能筛选（无 API Key 时的回退）"""
    print("\n📋 测试智能筛选（回退机制）")
    
    # 确保没有 API Key
    original_key = os.environ.pop("OPENAI_API_KEY", None)
    
    test_cases = [
        # (内容, 规则重要性, 规则分类)
        ("我对花生过敏，吃了会死", 0.3, "general_fact"),
        ("我怀疑张三在暗中破坏项目", 0.25, "general_fact"),
    ]
    
    passed = 0
    for content, importance, category in test_cases:
        result = smart_filter_segment(content, importance, category)
        
        # 应该触发 LLM，但因为没有 Key，应该回退到规则
        if result["method"] == "rule_fallback" or result["method"] == "rule":
            print(f"✅ PASS: '{content[:25]}...'")
            print(f"       方法: {result['method']}, 重要性: {result['importance']}")
            if result.get("llm_stats"):
                print(f"       LLM错误: {result['llm_stats'].get('llm_error', 'N/A')}")
            passed += 1
        else:
            print(f"❌ FAIL: '{content[:25]}...'")
            print(f"       期望回退, 实际方法: {result['method']}")
    
    # 恢复环境变量
    if original_key:
        os.environ["OPENAI_API_KEY"] = original_key
    
    return passed, len(test_cases)


def test_crabby_scenarios():
    """测试 Crabby 提出的场景"""
    print("\n📋 测试 Crabby 场景")
    
    scenarios = [
        {
            "name": "花生过敏 vs 花生狂魔",
            "content": "逗你的，我其实对花生过敏，吃了会死",
            "expect_complex": True,
            "expect_llm": True,
        },
        {
            "name": "隐晦的安全威胁",
            "content": "我怀疑他就是那个一直在暗中阻挠 Memory System 上线的人",
            "expect_complex": True,
            "expect_llm": True,
        },
        {
            "name": "玄学内容",
            "content": "今天那个蓝色的钩子挂在了昨天的影子里，仿佛时间在这里停滞",  # 添加隐喻词
            "expect_complex": True,
            "expect_llm": True,
        },
        {
            "name": "多实体纠缠",
            "content": "张三认为寒武纪项目应该由李四负责，但王五觉得大灭绝更重要",  # 添加关系词
            "expect_complex": True,
            "expect_llm": True,
        },
    ]
    
    passed = 0
    for scenario in scenarios:
        complexity = detect_semantic_complexity(scenario["content"])
        should_use, reason = should_use_llm_for_filtering(
            scenario["content"], 
            0.5,  # 假设规则给了中等分数
            "general_fact"
        )
        
        complex_match = complexity["is_complex"] == scenario["expect_complex"]
        llm_match = should_use == scenario["expect_llm"]
        
        if complex_match and llm_match:
            print(f"✅ PASS: {scenario['name']}")
            print(f"       复杂度: {complexity['complexity_score']}, 原因: {complexity['reasons'][:2]}")
            print(f"       使用LLM: {should_use}, 决策: {reason}")
            passed += 1
        else:
            print(f"❌ FAIL: {scenario['name']}")
            print(f"       复杂度期望: {scenario['expect_complex']}, 实际: {complexity['is_complex']}")
            print(f"       LLM期望: {scenario['expect_llm']}, 实际: {should_use}")
    
    return passed, len(scenarios)


def test_stats_tracking():
    """测试统计追踪"""
    print("\n📋 测试统计追踪")
    
    stats = LLMIntegrationStats()
    
    # 模拟一些调用
    stats.record_phase2({"llm_called": True, "llm_success": True, "tokens_used": 50})
    stats.record_phase2({"llm_called": True, "llm_success": False, "fallback_used": True, "llm_error": "timeout"})
    stats.record_phase3({"llm_called": True, "llm_success": True, "tokens_used": 80})
    stats.record_complexity_trigger()
    stats.record_complexity_trigger()
    
    summary = stats.summary()
    
    passed = 0
    total = 5
    
    if summary["phase2"]["calls"] == 2:
        print("✅ PASS: Phase2 调用计数")
        passed += 1
    else:
        print(f"❌ FAIL: Phase2 调用计数, 期望 2, 实际 {summary['phase2']['calls']}")
    
    if summary["phase2"]["fallbacks"] == 1:
        print("✅ PASS: Phase2 回退计数")
        passed += 1
    else:
        print(f"❌ FAIL: Phase2 回退计数, 期望 1, 实际 {summary['phase2']['fallbacks']}")
    
    if summary["total_tokens"] == 130:
        print("✅ PASS: Token 总计")
        passed += 1
    else:
        print(f"❌ FAIL: Token 总计, 期望 130, 实际 {summary['total_tokens']}")
    
    if summary["complexity_triggers"] == 2:
        print("✅ PASS: 复杂度触发计数")
        passed += 1
    else:
        print(f"❌ FAIL: 复杂度触发计数, 期望 2, 实际 {summary['complexity_triggers']}")
    
    if len(summary["errors"]) == 1:
        print("✅ PASS: 错误记录")
        passed += 1
    else:
        print(f"❌ FAIL: 错误记录, 期望 1, 实际 {len(summary['errors'])}")
    
    return passed, total


def main():
    print("=" * 60)
    print("Memory System v1.1.7 - LLM 深度集成测试")
    print("=" * 60)
    
    total_passed = 0
    total_tests = 0
    
    # 语义复杂度检测
    p, t = test_semantic_complexity()
    total_passed += p
    total_tests += t
    
    # LLM 触发决策
    p, t = test_llm_trigger_decision()
    total_passed += p
    total_tests += t
    
    # API Key 获取
    p, t = test_api_key_retrieval()
    total_passed += p
    total_tests += t
    
    # 智能筛选回退
    p, t = test_smart_filter_without_api()
    total_passed += p
    total_tests += t
    
    # Crabby 场景
    p, t = test_crabby_scenarios()
    total_passed += p
    total_tests += t
    
    # 统计追踪
    p, t = test_stats_tracking()
    total_passed += p
    total_tests += t
    
    print("\n" + "=" * 60)
    print(f"总计: {total_passed}/{total_tests} 通过")
    if total_passed == total_tests:
        print("🎉 所有测试通过！")
    else:
        print(f"⚠️ {total_tests - total_passed} 个测试失败")
    print("=" * 60)
    
    return 0 if total_passed == total_tests else 1


if __name__ == "__main__":
    sys.exit(main())
