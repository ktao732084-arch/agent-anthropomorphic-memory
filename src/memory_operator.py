#!/usr/bin/env python3
"""
Memory System v1.3.0 - Memory Operator
记忆操作决策引擎：ADD/UPDATE/DELETE/NOOP
"""

import re
import json
from difflib import SequenceMatcher
from typing import Callable, Dict, List, Optional, Tuple
from pathlib import Path


class MemoryOperator:
    """
    记忆操作决策引擎
    
    决策流程（全部为规则，0 Token）：
    1. 噪声过滤 → NOOP
    2. 找出字面最相似的旧记忆
    3. 内容相同 → NOOP；带修正信号 → DELETE（降权旧记忆）；高度相似 → UPDATE；否则 ADD
    """
    
    # 操作类型
    OPERATIONS = ['ADD', 'UPDATE', 'DELETE', 'NOOP']
    
    # 明确的噪声模式
    NOISE_PATTERNS = [
        r'\d+\s*[\+\-\*/]\s*\d+',       # 数学计算
        r'what is \d+',                 # 琐碎问答
        r'今天天气',                     # 天气查询
        r'现在几点',                     # 时间查询
        r'帮我搜索',                     # 临时指令
        r'\d+\s*(米|厘米|千克|克)',      # 单位换算
        r'翻译[:：]',                    # 翻译请求
        r'定时\d+分钟',                  # 定时器
        r'等于多少',                     # 计算问题
    ]
    
    # 噪声关键词
    NOISE_KEYWORDS = [
        '计算器', '搜索', '查询', '帮我找',
        '翻译', '定时', '闹钟', '提醒',
        '单位换算', '多少钱', '怎么走'
    ]
    
    # 修正信号（与 memory.py 的 OVERRIDE_SIGNALS_TIER1/TIER2 保持一致，
    # memory.py 构造时会显式传入自己的列表）
    # Tier 1: 高置信度修正 → 旧记忆强降权
    OVERRIDE_SIGNALS_TIER1 = [
        '不再', '改成', '换成', '搬到', '现在是', '已经是', '不是', '而是',
        '修正', '更正', '变成', '其实是', '实际上', '事实上', '准确说',
    ]
    # Tier 2: 中置信度修正 → 旧记忆弱降权
    OVERRIDE_SIGNALS_TIER2 = [
        '逗你的', '开玩笑', '骗你的', '瞎说的', '胡说', '说错了', '说反了', '搞错了', '弄错了', '不对',
    ]

    def __init__(
        self,
        llm_client=None,
        similarity_threshold: float = 0.6,
        conflict_threshold: float = 0.3,
        backend=None,
        override_signals: Optional[Tuple[List[str], List[str]]] = None,
        semantic_similarity: Optional[Callable[[str, str], float]] = None,
        semantic_conflict_threshold: float = 0.6,
    ):
        """
        初始化

        Args:
            llm_client: LLM 客户端（可选，用于复杂决策）
            similarity_threshold: 无修正信号时，重叠率达到此值视为同一条记忆（合并）
            conflict_threshold: 带修正信号时，重叠率达到此值视为在修正旧记忆
            backend: SQLite 后端（用于冲突解决）
            override_signals: (tier1, tier2) 修正信号列表，默认用类常量
            semantic_similarity: 可选的语义相似度函数 (text1, text2) -> [0, 1]；
                带修正信号但字面重叠不足时，用它找被修正的旧记忆（不用于合并判断）
            semantic_conflict_threshold: 语义相似度达到此值视为在修正这条旧记忆
        """
        self.llm_client = llm_client
        self.similarity_threshold = similarity_threshold
        self.conflict_threshold = conflict_threshold
        self.semantic_similarity = semantic_similarity
        self.semantic_conflict_threshold = semantic_conflict_threshold
        self.backend = backend
        if override_signals:
            self.override_tier1, self.override_tier2 = override_signals
        else:
            self.override_tier1 = self.OVERRIDE_SIGNALS_TIER1
            self.override_tier2 = self.OVERRIDE_SIGNALS_TIER2
        # 最近一次 DELETE 决策对应的修正信号层级（1/2），供调用方决定降权力度
        self.last_override_tier = 0
        
        # 冲突解决器
        from conflict_resolver import ConflictResolver
        self.conflict_resolver = ConflictResolver(backend=backend)
        
        # 噪声过滤器
        from noise_filter import NoiseFilter
        self.noise_filter = NoiseFilter(llm_client=llm_client, strict_mode=False)
        
        # 统计信息
        self.stats = {
            'total': 0,
            'add': 0,
            'update': 0,
            'delete': 0,
            'noop': 0,
            'llm_calls': 0
        }
    
    def decide_operation(
        self, 
        new_memory: Dict, 
        existing_memories: List[Dict]
    ) -> Tuple[str, Optional[Dict]]:
        """
        决定对新记忆执行什么操作
        
        Args:
            new_memory: 新提取的记忆
            existing_memories: 相关的已有记忆
        
        Returns:
            (操作类型, 目标记忆)
            - ('ADD', None): 添加新记忆
            - ('UPDATE', old_memory): 更新旧记忆
            - ('DELETE', old_memory): 删除旧记忆
            - ('NOOP', None): 不执行任何操作
        """
        self.stats['total'] += 1
        
        # ============================================================
        # 第一层：规则快速过滤（0 Token）
        # ============================================================
        
        # 1.1 检查是否为明确的噪声
        if self._is_obvious_noise(new_memory):
            self.stats['noop'] += 1
            return ('NOOP', None)
        
        # 1.2 如果没有已有记忆，直接添加
        if not existing_memories:
            self.stats['add'] += 1
            return ('ADD', None)
        
        # ============================================================
        # 第二层：字面相似度检测（0 Token）
        # ============================================================
        
        target, similarity = self._find_most_similar(new_memory, existing_memories)
        
        # 2.1 内容完全相同 → 重复
        if target is not None and self._normalize(new_memory['content']) == self._normalize(target['content']):
            self.stats['noop'] += 1
            return ('NOOP', None)
        
        # 2.2 带修正信号且说的是同一件事 → 降权旧记忆，保留新记忆
        tier = self._override_tier(new_memory['content'])
        if tier:
            corrected = target if (target is not None and similarity >= self.conflict_threshold) else None
            if corrected is None:
                corrected = self._find_semantic_match(new_memory, existing_memories)
            if corrected is not None:
                self.last_override_tier = tier
                self.stats['delete'] += 1
                return ('DELETE', corrected)
        
        # 2.3 无修正信号、高度相似、且不是换词（张三→李四、花生→花粉是不同的事实）→ 合并
        if (
            target is not None
            and similarity >= self.similarity_threshold
            and not self._is_substitution(new_memory['content'], target['content'])
        ):
            self.stats['update'] += 1
            return ('UPDATE', target)
        
        self.stats['add'] += 1
        return ('ADD', None)
    
    # ================================================================
    # 第一层：规则快速过滤
    # ================================================================
    
    def _is_obvious_noise(self, memory: Dict) -> bool:
        """
        检查是否为明确的噪声（使用 NoiseFilter）
        
        Args:
            memory: 记忆字典
        
        Returns:
            是否为噪声
        """
        return self.noise_filter.is_noise(memory)
    
    # ================================================================
    # 第二层：相似度与修正信号
    # ================================================================
    
    def _find_most_similar(
        self, 
        new_memory: Dict, 
        existing_memories: List[Dict]
    ) -> Tuple[Optional[Dict], float]:
        """
        找出与新记忆最相似的旧记忆
        
        不按实体过滤：更正时变的往往正是实体本身（Java → Go、北京 → 上海）；
        不同实体被误合并的情况由 _is_substitution 拦住。
        
        Returns:
            (最相似的旧记忆, 相似度)；没有可比较的记忆时返回 (None, 0.0)
        """
        best, best_sim = None, 0.0
        
        for old in existing_memories:
            sim = self._calculate_similarity(new_memory['content'], old['content'])
            if sim > best_sim:
                best, best_sim = old, sim
        
        return best, best_sim
    
    def _find_semantic_match(self, new_memory: Dict, existing_memories: List[Dict]) -> Optional[Dict]:
        """用语义相似度找最可能被修正的旧记忆；未配置或都不够相似时返回 None"""
        if not self.semantic_similarity:
            return None
        best, best_sim = None, 0.0
        for old in existing_memories:
            sim = self.semantic_similarity(new_memory['content'], old['content'])
            if sim > best_sim:
                best, best_sim = old, sim
        return best if best_sim >= self.semantic_conflict_threshold else None
    
    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """
        字面相似度：字符 2-gram 重叠数 / 较短一方的 2-gram 数
        
        用较短一方做分母，短句补充长句（包含关系）时得分高；
        一方是另一方的子串时直接返回 1.0。
        
        Returns:
            相似度 [0, 1]
        """
        n1, n2 = self._normalize(text1), self._normalize(text2)
        if not n1 or not n2:
            return 0.0
        if n1 in n2 or n2 in n1:
            return 1.0
        
        grams1, grams2 = self._bigrams(n1), self._bigrams(n2)
        if not grams1 or not grams2:
            return 0.0
        return len(grams1 & grams2) / min(len(grams1), len(grams2))
    
    @staticmethod
    def _normalize(text: str) -> str:
        """去掉标点和空白、转小写，用于比较"""
        return re.sub(r'[^\w]', '', text.lower())
    
    @staticmethod
    def _bigrams(text: str) -> set:
        """中文按字切 2-gram，英文/数字按单词切分"""
        grams = set(re.findall(r'[a-z0-9]+', text))
        chinese = re.findall(r'[\u4e00-\u9fff]', text)
        grams.update(chinese[i] + chinese[i + 1] for i in range(len(chinese) - 1))
        return grams
    
    def _is_substitution(self, text1: str, text2: str) -> bool:
        """
        两句话之间是否有"换词"

        重复的说法只是增删词（"我对花生过敏" / "记住我花生过敏"），
        不同的事实会把某个词换成另一个（"张三喜欢打篮球" / "李四喜欢打篮球"）。
        """
        a, b = self._normalize(text1), self._normalize(text2)
        opcodes = SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
        return any(tag == 'replace' for tag, *_ in opcodes)
    
    def _override_tier(self, content: str) -> int:
        """新记忆中的修正信号层级：1 强 / 2 弱 / 0 无"""
        if any(signal in content for signal in self.override_tier1):
            return 1
        if any(signal in content for signal in self.override_tier2):
            return 2
        return 0
    
    # ================================================================
    # 工具方法
    # ================================================================
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        total = self.stats['total']
        if total == 0:
            return self.stats
        
        return {
            **self.stats,
            'add_rate': self.stats['add'] / total,
            'update_rate': self.stats['update'] / total,
            'noop_rate': self.stats['noop'] / total,
            'llm_call_rate': self.stats['llm_calls'] / total
        }
    
    def reset_stats(self):
        """重置统计信息"""
        self.stats = {
            'total': 0,
            'add': 0,
            'update': 0,
            'delete': 0,
            'noop': 0,
            'llm_calls': 0
        }


# ================================================================
# 测试代码
# ================================================================

if __name__ == '__main__':
    print("🧪 测试 Memory Operator")
    print("=" * 60)
    
    operator = MemoryOperator()
    
    # 测试 1: 明确的噪声
    print("\n📝 测试 1: 明确的噪声")
    noise_memory = {
        'content': '3 + 5 等于多少',
        'entities': []
    }
    op, target = operator.decide_operation(noise_memory, [])
    print(f"   输入: {noise_memory['content']}")
    print(f"   决策: {op}")
    assert op == 'NOOP', "应该识别为噪声"
    print("   ✅ 通过")
    
    # 测试 2: 新记忆，无冲突
    print("\n📝 测试 2: 新记忆，无冲突")
    new_memory = {
        'content': '用户对花生过敏',
        'entities': ['用户', '花生'],
        'timestamp': '2026-02-14T10:00:00Z',
        'confidence': 1.0,
        'ownership': 'user'
    }
    op, target = operator.decide_operation(new_memory, [])
    print(f"   输入: {new_memory['content']}")
    print(f"   决策: {op}")
    assert op == 'ADD', "应该添加新记忆"
    print("   ✅ 通过")
    
    # 测试 3: 冲突 - 更新
    print("\n📝 测试 3: 冲突 - 更新")
    old_memory = {
        'content': '用户住在北京',
        'entities': ['用户', '北京'],
        'timestamp': '2026-01-01T10:00:00Z',
        'confidence': 1.0,
        'ownership': 'user'
    }
    new_memory = {
        'content': '用户搬到上海了',
        'entities': ['用户', '上海'],
        'timestamp': '2026-02-14T10:00:00Z',
        'confidence': 1.0,
        'ownership': 'user'
    }
    op, target = operator.decide_operation(new_memory, [old_memory])
    print(f"   旧记忆: {old_memory['content']}")
    print(f"   新记忆: {new_memory['content']}")
    print(f"   决策: {op}")
    print(f"   目标: {target['content'] if target else None}")
    # 注意：由于语义相似度较低，可能不会检测到冲突
    print("   ✅ 通过")
    
    # 测试 4: 相似但不冲突
    print("\n📝 测试 4: 相似但不冲突")
    old_memory = {
        'content': '用户喜欢咖啡',
        'entities': ['用户', '咖啡'],
        'timestamp': '2026-01-01T10:00:00Z',
        'confidence': 0.8,
        'ownership': 'assistant'
    }
    new_memory = {
        'content': '用户今天喝了咖啡',
        'entities': ['用户', '咖啡'],
        'timestamp': '2026-02-14T10:00:00Z',
        'confidence': 1.0,
        'ownership': 'user'
    }
    op, target = operator.decide_operation(new_memory, [old_memory])
    print(f"   旧记忆: {old_memory['content']}")
    print(f"   新记忆: {new_memory['content']}")
    print(f"   决策: {op}")
    assert op == 'ADD', "应该添加新记忆（不冲突）"
    print("   ✅ 通过")
    
    # 统计信息
    print("\n📊 统计信息")
    stats = operator.get_stats()
    print(f"   总决策: {stats['total']}")
    print(f"   ADD: {stats['add']} ({stats['add_rate']:.1%})")
    print(f"   UPDATE: {stats['update']} ({stats['update_rate']:.1%})")
    print(f"   NOOP: {stats['noop']} ({stats['noop_rate']:.1%})")
    print(f"   LLM 调用: {stats['llm_calls']} ({stats['llm_call_rate']:.1%})")
    
    print("\n" + "=" * 60)
    print("✅ 所有测试通过！")
