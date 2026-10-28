#!/usr/bin/env python3
"""
去重 / 冲突检测回归测试

覆盖 MemoryOperator 路径曾经失效的场景：
- 实体为空时所有比较被跳过（默认配置下实体提取常为空）
- 中文按空格分词导致相似度恒为 0
- 完全相同的内容重复写入
- 同一批新记忆之间不互相去重
"""

import sys
import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "legacy"))

from memory import deduplicate_facts, DEDUP_CONFIG


def fact(fid, content, importance=1.0, entities=None):
    return {
        "id": fid,
        "content": content,
        "entities": entities or [],
        "importance": importance,
        "score": importance,
        "created": "2026-10-28T00:00:00Z",
    }


def check(name, ok, detail=""):
    print(f"{'✅ PASS' if ok else '❌ FAIL'}: {name}")
    if detail:
        print(f"       {detail}")
    return int(ok)


def test_correction_without_entities():
    """无实体时，带修正信号的新记忆应降权旧记忆"""
    old = fact("f_old", "记住，我对花生过敏，很严重")
    new = fact("f_new", "其实我不对花生过敏，之前说错了")
    merged, dup, down = deduplicate_facts([new], [old])
    return check(
        "无实体的修正（花生）",
        down == 1 and merged == [new] and old.get("override_tier") == 2
        and abs(old["score"] - DEDUP_CONFIG["tier2_penalty"]) < 0.01,
        f"新增 {len(merged)}, 降权 {down}, 旧记忆 score={old['score']:.2f} tier={old.get('override_tier')}",
    )


def test_exact_duplicate():
    """完全相同的内容不应重复写入"""
    old = fact("f_old", "我叫张三，是一名医学生")
    merged, dup, _ = deduplicate_facts([fact("f_new", "我叫张三，是一名医学生")], [old])
    return check("完全相同的内容", merged == [] and dup == 1, f"新增 {len(merged)}, 去重 {dup}")


def test_duplicate_within_batch():
    """同一批新记忆之间也要去重"""
    batch = [fact("f_a", "我喜欢吃苹果"), fact("f_b", "我喜欢吃苹果")]
    merged, dup, _ = deduplicate_facts(batch, [])
    return check("同批次重复", len(merged) == 1 and dup == 1, f"新增 {len(merged)}, 去重 {dup}")


def test_unrelated_not_merged():
    """只共享少量字的不同记忆不应被合并"""
    existing = [fact("f_old", "我喜欢吃苹果", 0.8)]
    new = fact("f_new", "我喜欢看电影", 0.8)
    merged, dup, down = deduplicate_facts([new], existing)
    return check(
        "不相关记忆不合并",
        merged == [new] and dup == 0 and down == 0 and existing[0]["content"] == "我喜欢吃苹果",
        f"新增 {len(merged)}, 去重 {dup}, 降权 {down}",
    )


def test_common_words_not_override():
    """'从'、'到' 这类常用字不应被当作修正信号"""
    existing = [fact("f_old", "我每天从家走到学校", 0.5)]
    new = fact("f_new", "我每天从家骑车到学校", 0.5)
    _, _, down = deduplicate_facts([new], existing)
    return check("常用字不触发降权", down == 0, f"降权 {down}")


if __name__ == "__main__":
    tests = [
        test_correction_without_entities,
        test_exact_duplicate,
        test_duplicate_within_batch,
        test_unrelated_not_merged,
        test_common_words_not_override,
    ]
    passed = sum(t() for t in tests)
    print("\n" + "=" * 60)
    print(f"总计: {passed}/{len(tests)} 通过")
    if passed == len(tests):
        print("🎉 所有测试通过！")
    print("=" * 60)
    sys.exit(0 if passed == len(tests) else 1)
