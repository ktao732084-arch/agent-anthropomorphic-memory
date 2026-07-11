#!/usr/bin/env python3
"""
Memory System v1.1 测试脚本
"""

import sys
import os
from pathlib import Path

# 添加脚本目录到路径
# 仓库整理后被测模块位于 ../src 与 ../legacy,补齐导入路径,保证开箱可运行
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "legacy"))

from v1_1_config import *
from v1_1_helpers import *

def test_tier1_patterns():
    """测试第一级强匹配"""
    print("=" * 60)
    print("测试第一级强匹配")
    print("=" * 60)
    
    test_cases = [
        "我叫张三",
        "我对花生过敏",
        "今天3点开会",
        "明天去超市买菜",
        "我喜欢吃苹果"
    ]
    
    for content in test_cases:
        result = check_tier1_patterns(content)
        print(f"\n内容: {content}")
        if result:
            print(f"  类型: {result['type']}")
            print(f"  过期时间: {result['expires_at']}")
            print(f"  是否永久: {result['is_permanent']}")
        else:
            print("  未匹配")

def test_access_boost():
    """测试访问加成计算"""
    print("\n" + "=" * 60)
    print("测试访问加成计算")
    print("=" * 60)
    
    test_memory = {
        "id": "f_test_001",
        "content": "测试记忆",
        "importance": 0.8,
        "created": "2026-02-01T00:00:00Z",
        "retrieval_count": 10,
        "used_in_response_count": 5,
        "user_mentioned_count": 2
    }
    
    weighted_count = calculate_weighted_access_count(test_memory)
    boost = calculate_access_boost(test_memory)
    
    print(f"\n记忆: {test_memory['content']}")
    print(f"  检索次数: {test_memory['retrieval_count']}")
    print(f"  用于回复: {test_memory['used_in_response_count']}")
    print(f"  用户提及: {test_memory['user_mentioned_count']}")
    print(f"  加权访问次数: {weighted_count}")
    print(f"  访问加成: {boost:.2f} ({boost*100:.0f}%)")

def test_time_sensitivity():
    """测试时间敏感检测"""
    print("\n" + "=" * 60)
    print("测试时间敏感检测")
    print("=" * 60)
    
    test_cases = [
        ("今天晚上8点开会", 0.6),
        ("明天去医院", 0.5),
        ("这周完成报告", 0.7),
        ("我喜欢吃苹果", 0.8)
    ]
    
    for content, importance in test_cases:
        result = call_llm_time_sensor(content, importance)
        print(f"\n内容: {content}")
        print(f"  重要性: {importance}")
        print(f"  类型: {result['type']}")
        print(f"  过期时间: {result['expires_at']}")
        print(f"  是否永久: {result['is_permanent']}")

def test_decay_protection():
    """测试衰减保护"""
    print("\n" + "=" * 60)
    print("测试衰减保护")
    print("=" * 60)
    
    test_memories = [
        {
            "id": "f_test_001",
            "importance": 0.8,
            "score": 0.8,
            "final_score": 0.8,
            "last_accessed": "2026-02-04T00:00:00Z"  # 1天前
        },
        {
            "id": "f_test_002",
            "importance": 0.8,
            "score": 0.8,
            "final_score": 0.8,
            "last_accessed": "2026-01-29T00:00:00Z"  # 7天前
        },
        {
            "id": "f_test_003",
            "importance": 0.8,
            "score": 0.8,
            "final_score": 0.8,
            "last_accessed": "2026-01-15T00:00:00Z"  # 21天前
        }
    ]
    
    config = {
        "decay_rates": {
            "fact": 0.008,
            "belief": 0.07,
            "summary": 0.025
        }
    }
    
    result = phase6_decay_with_access_protection(test_memories, config)
    
    for mem in result:
        days_since = (datetime.utcnow() - datetime.fromisoformat(mem['last_accessed'].replace('Z', '+00:00')).replace(tzinfo=None)).days
        print(f"\n记忆: {mem['id']}")
        print(f"  最后访问: {days_since} 天前")
        print(f"  衰减前: {0.8:.3f}")
        print(f"  衰减后: {mem['score']:.3f}")
        print(f"  衰减率: {(1 - mem['score']/0.8)*100:.1f}%")

if __name__ == '__main__':
    print("🧪 Memory System v1.1 功能测试\n")
    
    test_tier1_patterns()
    test_access_boost()
    test_time_sensitivity()
    test_decay_protection()
    
    print("\n" + "=" * 60)
    print("✅ 所有测试完成")
    print("=" * 60)
