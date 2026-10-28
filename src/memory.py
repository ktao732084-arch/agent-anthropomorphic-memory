#!/usr/bin/env python3
"""
Memory System v1.4.0 - 三层记忆架构 CLI

⚠️  版本管理规范:
    - 版本号以 git tag 为准,禁止在文件头自封版本
    - 发布新版本:git tag vX.Y.Z -m "说明" && git push --tags
    - 当前 tag: v1.4.0

功能模块(按版本):
  v1.1.7  三层记忆 + LLM 深度集成 + 实体识别
  v1.2.x  QMD 集成 + 白天轻量检查 + SQLite 后端
  v1.3.0  幻觉防御(NoiseFilter + MemoryOperator + ConflictResolver + CacheManager)
          ScaledBackend + AsyncIndexer(>5000条自动启用)
  v1.4.0  时序引擎(TemporalQueryEngine + FactEvolutionTracker + EvidenceTracker)
          ProactiveEngine 接入 consolidation Phase 6.8
  v1.6.0  向量检索(VectorEmbedding + VectorIndex + HybridSearch)
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from embeddings import VectorStore, cosine, get_embedder
from llm_judge import get_judge
from embeddings import load_settings as load_embedding_settings
from temporal import ground_relative_dates

# 导入 v1.1 新增模块
try:
    from v1_1_commands import *
    from v1_1_config import *
    from v1_1_helpers import *

    V1_1_ENABLED = True
except ImportError:
    V1_1_ENABLED = False
    if os.environ.get("MEMORY_DEBUG"):
        print("⚠️ v1.1 模块未找到,部分功能不可用", file=sys.stderr)

# 导入 v1.1.5 实体系统模块(用于实体隔离和学习实体清理)
try:
    from v1_1_5_entity_system import ENTITY_SYSTEM_CONFIG

    V1_1_5_ENABLED = True
except ImportError:
    V1_1_5_ENABLED = False
    # 静默失败,不打印警告(功能会优雅降级)

# 导入 v1.1.7 LLM 深度集成模块
try:
    from v1_1_7_llm_integration import (
        INTEGRATION_STATS,
        LLM_INTEGRATION_CONFIG,
        LLMIntegrationStats,
        call_llm_with_fallback,
        detect_semantic_complexity,
        get_api_key,
        should_use_llm_for_filtering,
        smart_extract_entities,
        smart_filter_segment,
    )

    V1_1_7_ENABLED = True
except ImportError:
    V1_1_7_ENABLED = False
    # 静默失败,功能会优雅降级

# 导入 v1.6.0 向量检索模块
try:
    from hybrid_search import HybridSearchEngine, create_hybrid_search_engine
    from vector_embedding import VectorEmbeddingEngine, get_embedding_engine
    from vector_index import VectorIndexManager, build_vector_index

    VECTOR_SEARCH_ENABLED = True
except ImportError:
    VECTOR_SEARCH_ENABLED = False

# 导入 v1.5.2 TF-IDF + RRF 混合检索
try:
    from tfidf_engine import tfidf_search, rrf_merge, build_tfidf_index
    TFIDF_ENABLED = True
except ImportError:
    TFIDF_ENABLED = False

# 导入主动记忆引擎模块
try:
    from proactive_engine import Intent, ProactiveMemoryEngine, Suggestion, create_engine
    from proactive_executor import ProactiveExecutor, ProactiveSession, create_session

    PROACTIVE_ENABLED = True
except ImportError:
    PROACTIVE_ENABLED = False
    # 静默失败,功能会优雅降级

# 导入 v1.3.0 幻觉防御模块
try:
    from noise_filter import NoiseFilter, contains_secret
    from memory_operator import MemoryOperator
    from conflict_resolver import ConflictResolver

    _noise_filter_instance = NoiseFilter()
    HALLUCINATION_DEFENSE_ENABLED = True
except ImportError:
    HALLUCINATION_DEFENSE_ENABLED = False
    _noise_filter_instance = None

    def contains_secret(text):
        return False

# 导入 v1.4.0 时序引擎模块
try:
    from temporal_engine import (
        TemporalQueryEngine,
        FactEvolutionTracker,
        EvidenceTracker,
        create_temporal_engine,
        create_evolution_tracker,
        create_evidence_tracker,
    )
    TEMPORAL_ENGINE_ENABLED = True
except ImportError:
    TEMPORAL_ENGINE_ENABLED = False

# 导入扩展后端模块(阈值控制,>5000 条自动启用)
try:
    from scaled_backend import ScaledBackend
    from async_indexer import AsyncIndexer
    SCALED_BACKEND_AVAILABLE = True
except ImportError:
    SCALED_BACKEND_AVAILABLE = False

# 导入多级缓存模块
try:
    from cache_manager import CacheManager
    _cache_manager_instance = CacheManager()
    CACHE_MANAGER_ENABLED = True
except ImportError:
    CACHE_MANAGER_ENABLED = False
    _cache_manager_instance = None

SCALED_BACKEND_THRESHOLD = 5000

# ============================================================
# LLM 调用模块(v1.1.3 新增)
# ============================================================


def get_llm_config():
    """从环境变量获取 LLM 配置"""
    return {
        "api_key": os.environ.get("OPENAI_API_KEY"),
        "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "model": os.environ.get("MEMORY_LLM_MODEL", "gpt-3.5-turbo"),
        "enabled": os.environ.get("MEMORY_LLM_ENABLED", "true").lower() == "true",
    }


def call_llm(prompt, system_prompt=None, max_tokens=1000):
    """
    调用 LLM(使用用户的 API Key)
    使用 curl 避免 Python requests SSL 兼容性问题

    返回: (success: bool, result: str, error: str)
    """
    config = get_llm_config()

    # 检查是否启用 LLM
    if not config["enabled"]:
        return False, None, "LLM fallback disabled"

    # 检查 API Key
    if not config["api_key"]:
        return False, None, "OPENAI_API_KEY not found in environment"

    try:
        import subprocess, json as _json

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        data = _json.dumps({
            "model": config["model"],
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.3
        })

        result = subprocess.run(
            [
                "curl", "-s", "--max-time", "60",
                f"{config['base_url']}/chat/completions",
                "-H", f"Authorization: Bearer {config['api_key']}",
                "-H", "Content-Type: application/json",
                "-d", data,
            ],
            capture_output=True, text=True, timeout=70
        )

        if result.returncode != 0:
            LLM_STATS["errors"] += 1
            return False, None, f"curl failed: {result.stderr}"

        resp = _json.loads(result.stdout)

        if "error" in resp:
            LLM_STATS["errors"] += 1
            return False, None, f"API error: {resp['error']}"

        message = resp["choices"][0]["message"]
        content = message.get("content", "")

        # 兼容思考模型：content 为空时尝试 reasoning_content
        if not content.strip():
            content = message.get("reasoning_content", "")

        # 统计 token 使用
        usage = resp.get("usage", {})
        LLM_STATS["total_tokens"] += usage.get("total_tokens", 0)

        return True, content.strip(), None

    except subprocess.TimeoutExpired:
        LLM_STATS["errors"] += 1
        return False, None, "LLM call timed out (60s)"
    except Exception as e:
        LLM_STATS["errors"] += 1
        return False, None, f"LLM call failed: {str(e)}"


# ============================================================
# 配置
# ============================================================

DEFAULT_CONFIG = {
    "version": "1.4.0",
    "decay_rates": {"fact": 0.008, "belief": 0.07, "summary": 0.025},
    "thresholds": {"archive": 0.05, "summary_trigger": 3},
    "token_budget": {"layer1_total": 2000},
    "consolidation": {"fallback_hours": 48},
    "conflict_detection": {"enabled": True, "penalty": 0.2},
    "llm_fallback": {
        "enabled": True,
        "phase2_filter": True,
        "phase3_extract": True,
        "phase4b_verify": False,
        "min_confidence": 0.6,
    },
    "funnel": {
        "tier2_threshold_lower": 0.35,
        "tier2_threshold_upper": 0.70,
        "tier3_default_ttl_days": 3,
        "tier3_reactivation_extend_days": 3,
        "tier3_max_ttl_days": 14,
    },
    "access_tracking": {
        "enabled": True,
        "boost_coefficient": 0.2,
        "max_boost": 0.5,
        "weights": {"retrieval": 1.0, "used_in_response": 2.0, "user_mentioned": 3.0},
        "protection_days": {"strong": 3, "medium": 7, "weak": 14},
    },
    "time_sensitivity": {
        "enabled": True,
        "immediate_hours": 12,
        "short_term_days": 2,
        "medium_term_days": 10,
        "long_term_days": 35,
        "event_after_hours": 2,
    },
    "vector": {
        "enabled": False,
        "provider": "openai",
        "model": "text-embedding-3-small",
        "dimension": 1536,
        "backend": "sqlite",
        "hybrid_search": {
            "keyword_weight": 0.3,
            "vector_weight": 0.7,
            "min_score": 0.2,
        },
        "cache": {"enabled": True, "max_size": 10000},
        "qdrant": {"host": "localhost", "port": 6333, "collection": "memory"},
    },
    "proactive": {
        "enabled": True,
        "intent_window_size": 10,
        "intent_timeout_minutes": 30,
        "suggestion_interval_seconds": 60,
        "min_confidence_for_suggestion": 0.5,
        "max_suggestions_per_hour": 5,
        "cache_ttl_minutes": 5,
        "proactive_threshold_confidence": 0.7,
        "proactive_threshold_messages": 3,
    },
    "dashboard": {
        "enabled": True,
        "host": "localhost",
        "port": 9090,
        "auth": {"enabled": False, "password": ""},
        "websocket": {"enabled": True, "heartbeat_interval": 30},
    },
}

# ============================================================
# v1.2.0 废话前置过滤器(规则强化)
# ============================================================

NOISE_PATTERNS = {
    # 纯语气词/感叹词(直接跳过,不进入 LLM)
    "pure_interjection": [
        r"^[哈嘿呵嗯啊哦噢呃唉嘛吧啦呀咯嘞哇喔]+[~～.!!??]*$",  # 哈哈哈/嗯嗯/啊啊啊
        r"^[oO]+[kK]+[~～.!!??]*$",  # ok, OK, okok
        r"^[yY]e+[sS]*[~～.!!??]*$",  # yes, yeees
        r"^[nN]o+[~～.!!??]*$",  # no, nooo
        r"^[lL][oO]+[lL]+[~～.!!??]*$",  # lol, looool
    ],
    # 简单确认/应答(直接跳过)
    "simple_ack": [
        r"^(好的?|行|可以|没问题|收到|了解|明白|懂了?|知道了?|OK|ok|嗯|对|是的?)[~～.!!??]*$",
        r"^(谢谢|感谢|thanks?|thx)[~～.!!??]*$",
        r"^(不用|不必|算了|没事|无所谓)[~～.!!??]*$",
    ],
    # 纯表情/符号
    "emoji_only": [
        r"^[\U0001F300-\U0001F9FF\U0001FA00-\U0001FAFF\u2600-\u26FF\u2700-\u27BF\s~～.!!??]+$",  # emoji
        r"^[..,,!!??~～\s]+$",  # 纯标点
    ],
    # 过短内容(<3字符,排除数字和特殊标记)
    "too_short": [
        r"^.{0,2}$",  # 0-2个字符
    ],
}


def is_noise(content: str) -> tuple[bool, str]:
    """
    前置废话检测,返回 (是否废话, 匹配的类别)
    v1.3.0: 优先使用 NoiseFilter,降级回原有规则
    """
    content = content.strip()

    if contains_secret(content):
        return True, "secret"

    # v1.3.0: 优先用 NoiseFilter(更强的 4 层过滤)
    if HALLUCINATION_DEFENSE_ENABLED and _noise_filter_instance:
        try:
            result = _noise_filter_instance.is_noise({"content": content})
            if result:
                return True, "noise_filter"
        except Exception:
            pass  # 降级到原有规则

    # 原有规则兜底
    for category, patterns in NOISE_PATTERNS.items():
        for pattern in patterns:
            if re.match(pattern, content):
                return True, category

    return False, ""


# ============================================================
# 重要性规则配置
# ============================================================

IMPORTANCE_RULES = {
    # 身份/健康/安全 → 1.0
    "identity_health_safety": {
        "keywords": [
            "过敏",
            "疾病",
            "病",
            "健康",
            "安全",
            "紧急",
            "危险",
            "我叫",
            "我是",
            "我的名字",
            "身份证",
            "电话",
            "地址",
            "密码",
            "账号",
            "银行",
            "死",
            "生命",
        ],
        "score": 1.0,
    },
    # 偏好/关系/状态变更 → 0.8
    "preference_relation_status": {
        "keywords": [
            "喜欢",
            "讨厌",
            "爱",
            "恨",
            "偏好",
            "习惯",
            "朋友",
            "家人",
            "父母",
            "妈妈",
            "爸爸",
            "兄弟",
            "姐妹",
            "换工作",
            "搬家",
            "毕业",
            "结婚",
            "离婚",
            "分手",
            "开始",
            "结束",
            "改变",
        ],
        "score": 0.8,
    },
    # 项目/任务/目标 → 0.7
    "project_task_goal": {
        "keywords": ["项目", "任务", "目标", "计划", "deadline", "截止", "开发", "设计", "实现", "完成", "进度"],
        "score": 0.7,
    },
    # 一般事实 → 0.5
    "general_fact": {
        "keywords": [],  # 默认
        "score": 0.5,
    },
    # 临时信息 → 0.2
    "temporary": {
        "keywords": ["今天", "明天", "刚才", "一会儿", "待会", "马上", "顺便", "随便", "无所谓"],
        "score": 0.2,
    },
}

# 显式信号加成
EXPLICIT_SIGNALS = {
    "boost_high": {"keywords": ["记住", "永远记住", "一定要记住", "以后都", "永远都"], "boost": 0.5},
    "boost_medium": {"keywords": ["重要", "关键", "必须", "一定"], "boost": 0.3},
    "boost_low": {"keywords": ["注意", "别忘了"], "boost": 0.2},
    "reduce": {"keywords": ["顺便说一下", "随便问问", "不重要", "无所谓"], "boost": -0.2},
}

# 实体识别模式(v1.1.2 改进:支持正则模式)
ENTITY_PATTERNS = {
    "person": {
        "fixed": ["我", "你", "他", "她", "用户", "Ktao", "Tkao"],
        "patterns": [
            r"[A-Z][a-z]+",  # 英文人名:John, Mary(移除\b)
        ],
    },
    "project": {
        "fixed": ["项目", "系统", "工具", "应用", "App"],
        "patterns": [
            r"项目_\d+",  # 项目_1, 项目_25
            r"[A-Z][a-zA-Z0-9-]+",  # OpenClaw, Memory-System(移除\b)
        ],
    },
    "location": {
        "fixed": ["北京", "上海", "深圳", "广州", "杭州", "河南", "郑州"],
        "patterns": [
            r"城市_\d+",  # 城市_1, 城市_50
            r"地点_\d+",  # 地点_1, 地点_50
        ],
    },
    "organization": {
        "fixed": ["公司", "学校", "大学", "医院", "团队"],
        "patterns": [
            r"组织_\d+",  # 组织_1, 组织_50
            r"团队_\d+",  # 团队_1, 团队_50
        ],
    },
}

# v1.1.6 新增:引号实体模式(优先级高于通用词)
# 支持中英文引号:""『』""''《》
QUOTED_ENTITY_PATTERNS = [
    # 中文单引号""
    "\u300c([^\u300d]+)\u300d",
    # 中文双引号『』
    "\u300e([^\u300f]+)\u300f",
    # 中文弯引号""
    "\u201c([^\u201d]+)\u201d",
    # 中文弯引号''
    "\u2018([^\u2019]+)\u2019",
    # 书名号《》
    "\u300a([^\u300b]+)\u300b",
    # 英文单引号
    r"'([^']+)'",
    # 英文双引号
    r'"([^"]+)"',
]

# 冲突覆盖信号(v1.1.1 新增,v1.1.6 扩展)
# Tier 1: 高置信度修正信号 → 直接触发冲突覆盖
OVERRIDE_SIGNALS_TIER1 = [
    "不再",
    "改成",
    "换成",
    "搬到",
    "现在是",
    "已经是",
    "不是",
    "而是",
    "修正",
    "更正",
    "变成",
    "其实是",
    "实际上",
    "事实上",
    "准确说",
]

# Tier 2: 中置信度修正信号 → 标记为"可能冲突",降权但不自动覆盖
OVERRIDE_SIGNALS_TIER2 = ["逗你的", "开玩笑", "骗你的", "瞎说的", "胡说", "说错了", "说反了", "搞错了", "弄错了", "不对"]

# 合并(向后兼容)
OVERRIDE_SIGNALS = OVERRIDE_SIGNALS_TIER1 + OVERRIDE_SIGNALS_TIER2

# 冲突降权系数
CONFLICT_PENALTY = 0.2

# LLM 调用统计(v1.1.3 新增)
LLM_STATS = {"phase2_calls": 0, "phase3_calls": 0, "total_tokens": 0, "errors": 0}

# ============================================================
# 工具函数
# ============================================================


def _utcnow():
    """当前 UTC 时间(naive),替代已弃用的 datetime.utcnow()

    设置环境变量 MEMORY_NOW(ISO 时间,如 2026-03-01T10:00:00Z)可固定"现在",
    用于评测回放跨天对话和可复现测试。
    """
    fixed = os.environ.get("MEMORY_NOW")
    if fixed:
        return datetime.fromisoformat(fixed.replace("Z", "+00:00")).replace(tzinfo=None)
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_memory_dir():
    """获取记忆系统根目录

    优先级:
    1. MEMORY_DIR 环境变量(直接指定记忆目录路径)
    2. WORKSPACE 环境变量 + /memory
    3. 当前工作目录 + /memory
    """
    memory_dir = os.environ.get("MEMORY_DIR")
    if memory_dir:
        return Path(memory_dir)
    workspace = os.environ.get("WORKSPACE", os.getcwd())
    return Path(workspace) / "memory"


def get_config():
    """读取配置"""
    config_path = get_memory_dir() / "config.json"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    return DEFAULT_CONFIG


def save_config(config):
    """保存配置"""
    config_path = get_memory_dir() / "config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def load_jsonl(path):
    """读取 JSONL 文件"""
    if not path.exists():
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def save_jsonl(path, records):
    """保存 JSONL 文件"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def generate_id(prefix, content):
    """生成唯一ID"""
    date_str = _utcnow().strftime("%Y%m%d")
    hash_str = hashlib.md5(content.encode()).hexdigest()[:6]
    return f"{prefix}_{date_str}_{hash_str}"


def now_iso():
    """当前时间 ISO 格式"""
    return _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def append_jsonl(path, record):
    """追加单条记录到 JSONL 文件"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def log_history(memory_dir, events):
    """追加操作历史到 layer2/history.jsonl(借鉴 mem0 的 history 表,用于审计和回滚)"""
    ts = now_iso()
    for event in events:
        append_jsonl(memory_dir / "layer2/history.jsonl", {"ts": ts, **event})


def _parse_utc(ts):
    """解析 ISO 时间戳为 naive UTC datetime,失败返回 None"""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def archive_stale_memories(memory_dir):
    """把已过期(expires_at 已过)或已失效(invalid_at 已设置)的记忆移入归档池

    不删除任何记录;每次移动写一条历史事件。
    返回: 移动条数
    """
    now = _parse_utc(now_iso())
    moved = 0
    events = []
    for mem_type in ["facts", "beliefs", "summaries"]:
        active_path = memory_dir / f"layer2/active/{mem_type}.jsonl"
        records = load_jsonl(active_path)
        keep, stale = [], []
        for r in records:
            expires = _parse_utc(r.get("expires_at"))
            if r.get("invalid_at"):
                r["archived_reason"] = "superseded"
                stale.append(r)
            elif expires and expires <= now:
                r["archived_reason"] = "expired"
                stale.append(r)
            else:
                keep.append(r)
        if not stale:
            continue
        save_jsonl(active_path, keep)
        for r in stale:
            r["archived_at"] = now_iso()
            append_jsonl(memory_dir / f"layer2/archive/{mem_type}.jsonl", r)
            events.append({"op": "ARCHIVE", "id": r["id"], "reason": r["archived_reason"], "content": r["content"]})
        moved += len(stale)
    log_history(memory_dir, events)
    return moved


# 这些类别的记忆不会因为提到了某个日期就过期
PERMANENT_CATEGORIES = {"identity_health_safety", "preference_relation_status"}

# 有明确未来日期的安排,Phase 2 筛选时的最低重要性(高于默认阈值 0.3)
DATED_PLAN_MIN_IMPORTANCE = 0.5


def ground_record_time(record, category, observed_at=None):
    """把相对时间换算成绝对日期(原地修改 record)

    - content 中的 "下周三" 等标注为绝对日期
    - valid_at: 记忆开始成立的时间(观察时间)
    - event_date: 文中第一个精确到天的日期
    - 未来事件且非永久类别: expires_at 设为事件次日,过期后移入归档
    """
    observed_at = observed_at or record.get("created") or now_iso()
    observed = (_parse_utc(observed_at) or _utcnow()).date()
    record["content"], event_date = ground_relative_dates(record["content"], observed)
    record.setdefault("valid_at", observed_at)
    if event_date:
        record["event_date"] = event_date.isoformat()
        if event_date >= observed and category not in PERMANENT_CATEGORIES and not record.get("expires_at"):
            record["expires_at"] = f"{(event_date + timedelta(days=1)).isoformat()}T00:00:00Z"
            record["is_permanent"] = False


# ============================================================
# Phase 2: 重要性筛选 - rule_filter()
# ============================================================


def calculate_importance(content):
    """
    基于规则计算内容的重要性分数
    返回: (importance_score, matched_category)
    """
    content_lower = content.lower()

    # 1. 检查内在重要性(从高到低)
    for category in ["identity_health_safety", "preference_relation_status", "project_task_goal", "temporary"]:
        rule = IMPORTANCE_RULES[category]
        for keyword in rule["keywords"]:
            if keyword in content or keyword in content_lower:
                base_score = rule["score"]
                break
        else:
            continue
        break
    else:
        # 默认为一般事实
        base_score = IMPORTANCE_RULES["general_fact"]["score"]
        category = "general_fact"

    # 2. 检查显式信号加成
    boost = 0
    for signal_type, signal_config in EXPLICIT_SIGNALS.items():
        for keyword in signal_config["keywords"]:
            if keyword in content:
                boost = (
                    max(boost, signal_config["boost"])
                    if signal_config["boost"] > 0
                    else min(boost, signal_config["boost"])
                )
                break

    # 3. 计算最终分数
    final_score = min(1.0, max(0.0, base_score + boost))

    return final_score, category


def rule_filter(segments, threshold=0.3, use_llm_fallback=True):
    """
    Phase 2: 重要性筛选(v1.1.7:智能 LLM 集成)

    v1.1.7 改进:
    - 语义复杂度检测:识别需要 LLM 处理的复杂内容
    - 扩大 LLM 触发区间:0.2~0.5(原 0.2~0.3)
    - LLM 失败回退:失败时回退到规则结果,不丢弃

    输入: 语义片段列表
    输出: 筛选后的重要片段列表(带 importance 标注)
    """
    config = get_config()
    llm_enabled = config.get("llm_fallback", {}).get("enabled", True) and use_llm_fallback
    phase2_llm = config.get("llm_fallback", {}).get("phase2_filter", True)

    filtered = []
    noise_skipped = 0  # v1.2.0 统计

    for segment in segments:
        content = segment.get("content", "") if isinstance(segment, dict) else segment
        source = segment.get("source", "unknown") if isinstance(segment, dict) else "unknown"
        created = segment.get("created") if isinstance(segment, dict) else None

        # v1.2.0: 前置废话过滤(跳过明显废话,不进入 LLM)
        is_noise_content, noise_category = is_noise(content)
        if is_noise_content:
            noise_skipped += 1
            continue

        # 1. 规则判断
        rule_importance, rule_category = calculate_importance(content)
        # 有明确日期的安排("明天去医院复查")短期内有用,不因"临时"被筛掉;过期后会自动归档
        observed = (_parse_utc(created) or _utcnow()).date()
        _, event_date = ground_relative_dates(content, observed)
        if event_date and event_date >= observed:
            rule_importance = max(rule_importance, DATED_PLAN_MIN_IMPORTANCE)

        # 2. v1.1.7: 智能 LLM 集成
        if V1_1_7_ENABLED and llm_enabled and phase2_llm:
            # 使用智能筛选(自动决定是否调用 LLM)
            smart_result = smart_filter_segment(
                content=content,
                rule_importance=rule_importance,
                rule_category=rule_category,
                config_dict=config,
            )

            importance = smart_result["importance"]
            category = smart_result["category"]
            method = smart_result["method"]

            # 记录统计
            if smart_result.get("llm_stats"):
                INTEGRATION_STATS.record_phase2(smart_result["llm_stats"])
            if smart_result.get("complexity", {}).get("is_complex"):
                INTEGRATION_STATS.record_complexity_trigger()

            # 判断是否保留
            if importance >= threshold:
                result = {
                    "content": content,
                    "importance": importance,
                    "category": category,
                    "source": source,
                    "created": created,
                    "method": method,
                    "complexity": smart_result.get("complexity", {}),
                }
                filtered.append(result)
        else:
            # 回退到原有逻辑(v1.1.6 及之前)
            if rule_importance >= threshold:
                result = {
                    "content": content,
                    "importance": rule_importance,
                    "category": rule_category,
                    "source": source,
                    "created": created,
                    "method": "rule",
                }
                filtered.append(result)
            elif rule_importance >= threshold - 0.1:
                # 不确定区间,尝试 LLM
                if llm_enabled and phase2_llm:
                    llm_result = llm_filter_segment(content)
                    if llm_result:
                        importance = llm_result.get("importance", rule_importance)
                        if importance >= threshold:
                            result = {
                                "content": content,
                                "importance": importance,
                                "category": llm_result.get("category", rule_category),
                                "source": source,
                                "created": created,
                                "method": "llm",
                            }
                            filtered.append(result)

    return filtered

    return filtered


def llm_filter_segment(content):
    """
    使用 LLM 判断片段重要性

    返回: {"importance": float, "category": str} 或 None
    """
    LLM_STATS["phase2_calls"] += 1

    system_prompt = """你是一个记忆重要性评估专家.
评估用户输入的重要性(0-1),并分类.

分类标准:
- identity_health_safety (1.0): 身份/健康/安全相关
- preference_relation_status (0.8): 偏好/关系/状态变更
- project_task_goal (0.7): 项目/任务/目标
- general_fact (0.5): 一般事实
- temporary (0.2): 临时信息

返回 JSON 格式:
{"importance": 0.8, "category": "preference_relation_status", "reason": "简短理由"}"""

    prompt = f"""评估以下内容的重要性:

内容:{content}

返回 JSON:"""

    success, result, error = call_llm(prompt, system_prompt, max_tokens=500)

    if success:
        try:
            # 尝试解析 JSON
            import re

            json_match = re.search(r"\{[^}]+\}", result)
            if json_match:
                data = json.loads(json_match.group())
                return {
                    "importance": float(data.get("importance", 0.5)),
                    "category": data.get("category", "general_fact"),
                }
        except:
            pass

    return None


# ============================================================
# Phase 3: 深度提取 - template_extract()
# ============================================================


def extract_entities(content, memory_dir=None, use_llm_fallback=True):
    """
    从内容中提取实体

    v1.1.6 改进:四层识别架构
    - Layer 0: 引号实体(优先级最高,v1.1.6 新增)
    - Layer 1: 硬编码模式(ENTITY_PATTERNS)
    - Layer 2: 学习过的实体(learned_entities.json)
    - Layer 3: LLM 提取 + 自动学习

    参数:
        content: 要提取的内容
        memory_dir: 记忆目录(用于加载学习实体,可选)
        use_llm_fallback: 是否启用 LLM 兜底

    返回:
        list: 提取的实体列表
    """
    import re

    entities = []
    matched_positions = set()

    # ===== Layer 0: 引号实体(v1.1.6 新增,优先级最高)=====
    # 引号内的内容通常是专有名词,优先提取
    for pattern in QUOTED_ENTITY_PATTERNS:
        for match in re.finditer(pattern, content):
            # 提取引号内的内容(group(1) 是括号捕获的部分)
            quoted_text = match.group(1) if match.lastindex else match.group()
            if quoted_text and len(quoted_text) > 1 and quoted_text not in entities:
                entities.append(quoted_text)
                # 标记位置,避免后续重复匹配
                start, end = match.span()
                for i in range(start, end):
                    matched_positions.add(i)

    # ===== Layer 1: 硬编码模式(原有逻辑)=====
    for entity_type, config in ENTITY_PATTERNS.items():
        # 1. 固定词匹配
        if "fixed" in config:
            for word in config["fixed"]:
                if word in content:
                    entities.append(word)

        # 2. 正则模式匹配
        if "patterns" in config:
            for pattern in config["patterns"]:
                for match in re.finditer(pattern, content):
                    matched_text = match.group()
                    start, end = match.span()

                    if not any(start < pos < end or pos == start for pos in matched_positions):
                        entities.append(matched_text)
                        for i in range(start, end):
                            matched_positions.add(i)

    # ===== Layer 2: 学习过的实体(v1.1.5 新增)=====
    if V1_1_5_ENABLED and memory_dir:
        from v1_1_5_entity_system import load_learned_entities

        learned = load_learned_entities(memory_dir)

        # 精确匹配
        for exact in learned.get("exact", []):
            if exact in content and exact not in entities:
                entities.append(exact)

        # 学习的模式匹配
        for pattern in learned.get("patterns", []):
            try:
                for match in re.finditer(pattern, content):
                    matched_text = match.group()
                    if matched_text not in entities:
                        entities.append(matched_text)
            except re.error:
                continue

    # ===== Layer 3: LLM 兜底(v1.1.5 新增)=====
    if not entities and V1_1_5_ENABLED and use_llm_fallback:
        config = get_config()
        llm_enabled = config.get("llm_fallback", {}).get("enabled", True)

        if llm_enabled:
            llm_result = llm_extract_entities(content)
            if llm_result:
                entities = llm_result.get("entities", [])

                # 学习新实体
                if entities and memory_dir:
                    from v1_1_5_entity_system import learn_new_entities

                    learn_new_entities(entities, memory_dir)

    # ===== 去重和过滤(原有逻辑)=====
    entities = [e for e in set(entities) if e and len(e) > 1]

    # 过滤:如果短实体是长实体的子串,移除短实体
    final_entities = []
    sorted_entities = sorted(entities, key=len, reverse=True)

    for entity in sorted_entities:
        is_substring = False
        for other in final_entities:
            if entity in other and entity != other:
                is_substring = True
                break
        if not is_substring:
            final_entities.append(entity)

    return final_entities


def classify_memory_type(content, importance):
    """
    判断记忆类型: fact / belief / summary
    """
    content_lower = content.lower()

    # 推断性词汇 → belief
    belief_indicators = [
        "可能",
        "也许",
        "大概",
        "应该",
        "似乎",
        "看起来",
        "我觉得",
        "我认为",
        "我猜",
        "估计",
        "probably",
        "maybe",
    ]
    for indicator in belief_indicators:
        if indicator in content_lower:
            return "belief"

    # 聚合性词汇 → summary
    summary_indicators = ["总结", "综上", "总的来说", "概括", "整体上"]
    for indicator in summary_indicators:
        if indicator in content_lower:
            return "summary"

    # 默认 → fact
    return "fact"


def template_extract(filtered_segments, use_llm_fallback=True, memory_dir=None):
    """
    Phase 3: 深度提取(v1.1.5:三层实体识别 + LLM 兜底)
    将筛选后的片段转为结构化 facts/beliefs

    模板匹配优先,LLM 兜底
    """
    config = get_config()
    llm_enabled = config.get("llm_fallback", {}).get("enabled", True) and use_llm_fallback
    phase3_llm = config.get("llm_fallback", {}).get("phase3_extract", True)

    # 获取 memory_dir
    if memory_dir is None:
        memory_dir = get_memory_dir()

    extracted = {"facts": [], "beliefs": [], "summaries": []}

    for segment in filtered_segments:
        content = segment["content"]
        importance = segment["importance"]
        source = segment.get("source", "unknown")
        method = segment.get("method", "rule")

        # 1. v1.1.5: 三层实体识别(传入 memory_dir)
        entities = extract_entities(content, memory_dir=memory_dir, use_llm_fallback=llm_enabled and phase3_llm)
        mem_type = classify_memory_type(content, importance)
        _, content_category = calculate_importance(content)

        # 2. 构建记录
        record = {
            "id": generate_id(mem_type[0], content),
            "content": content,
            "importance": importance,
            "score": importance,
            "entities": entities,
            "created": now_iso(),
            "source": source,
            "extract_method": method,
            # v1.1.4 新增字段
            "expires_at": None,
            "is_permanent": True,
            "access_count": 0,
            "retrieval_count": 0,
            "used_in_response_count": 0,
            "user_mentioned_count": 0,
            "last_accessed": None,
            "access_boost": 0.0,
            "tier3_tracked": False,
            "reactivation_count": 0,
            "final_score": importance,
            # v1.5.0: identity 保护标签（BMAM Identity Preservation）
            "is_identity": content_category == "identity_health_safety",
        }

        # v1.1.4: 时间敏感检测
        if V1_1_ENABLED:
            # 第一级强匹配
            tier1_result = check_tier1_patterns(content)
            if tier1_result:
                record["expires_at"] = tier1_result.get("expires_at")
                record["is_permanent"] = tier1_result.get("is_permanent", True)
            # 第二级 LLM 介入(灰色地带)
            elif 0.35 <= importance <= 0.70:
                llm_result = call_llm_time_sensor(content, importance)
                record["expires_at"] = llm_result.get("expires_at")
                record["is_permanent"] = llm_result.get("is_permanent", True)

        # 相对时间 → 绝对日期(不覆盖上面已设置的 expires_at)
        ground_record_time(record, content_category, segment.get("created"))

        # belief 需要额外字段
        if mem_type == "belief":
            record["confidence"] = 0.6
            record["basis"] = f"从对话推断: {content[:50]}..."

        # 分类存储
        extracted[f"{mem_type}s"].append(record)

    return extracted


def llm_extract_entities(content):
    """
    使用 LLM 提取实体和记忆类型

    返回: {"entities": [...], "type": "fact/belief"} 或 None
    """
    LLM_STATS["phase3_calls"] += 1

    system_prompt = """你是一个实体提取专家.
从用户输入中提取关键实体(人物/地点/项目/组织等).

返回 JSON 格式:
{"entities": ["实体1", "实体2"], "type": "fact", "reason": "简短理由"}

type 可选值:
- fact: 确定的事实
- belief: 推断或不确定的信息"""

    prompt = f"""从以下内容中提取实体:

内容:{content}

返回 JSON:"""

    success, result, error = call_llm(prompt, system_prompt, max_tokens=150)

    if success:
        try:
            import re

            json_match = re.search(r"\{[^}]+\}", result)
            if json_match:
                data = json.loads(json_match.group())
                return {"entities": data.get("entities", []), "type": data.get("type", "fact")}
        except:
            pass

    return None


# ============================================================
# Phase-4A: Facts 去重合并
# ============================================================

# v1.1.6: 去重配置
DEDUP_CONFIG = {
    "min_overlap_ratio": 0.3,  # 带修正信号时,重叠比例达到 30% 视为在修正旧记忆
    "merge_overlap_ratio": 0.6,  # 无修正信号时,重叠比例达到 60% 视为同一条记忆(合并)
    "tier1_penalty": 0.1,  # Tier 1 信号:强降权(保留 10%)
    "tier2_penalty": 0.4,  # Tier 2 信号:弱降权(保留 40%)
}


def tokenize_chinese(text):
    """
    简单的中文分词(字符级 + 英文单词)
    对于中文,使用 2-gram;对于英文,使用空格分词
    """
    import re

    tokens = set()

    # 提取英文单词
    english_words = re.findall(r"[a-zA-Z]+", text)
    tokens.update(english_words)

    # 提取中文字符的 2-gram
    chinese_chars = re.findall(r"[\u4e00-\u9fff]", text)
    for i in range(len(chinese_chars) - 1):
        tokens.add(chinese_chars[i] + chinese_chars[i + 1])

    # 单个中文字符也加入(用于短文本)
    tokens.update(chinese_chars)

    return tokens


def deduplicate_facts(new_facts, existing_facts, events=None):
    """
    Phase-4A - Facts dedup + conflict detection
    v1.3.0: 优先使用 MemoryOperator + ConflictResolver,降级回原有规则

    返回: (merged_facts, duplicate_count, downgraded_count)
    """
    # v1.3.0: 优先用 MemoryOperator
    if HALLUCINATION_DEFENSE_ENABLED:
        try:
            return _deduplicate_with_operator(new_facts, existing_facts, events)
        except Exception as e:
            print(f"⚠️ MemoryOperator 去重失败,降级到旧版规则: {e}", file=sys.stderr)

    return _deduplicate_legacy(new_facts, existing_facts)


def _deduplicate_with_operator(new_facts, existing_facts, events=None):
    """v1.3.0: 使用 MemoryOperator + ConflictResolver 去重

    existing_facts 中被合并/降权的记录会被原地修改,调用方需要回写。
    同一批新 facts 之间也会互相去重。
    events 不为 None 时,每个决策追加一条历史事件(见 log_history)。

    强修正(Tier 1)不删除旧记忆,而是写入 invalid_at / superseded_by
    (借鉴 Graphiti 的时间失效),由 archive_stale_memories 移入归档池保留历史。
    """
    if events is None:
        events = []
    # 语义向量(可选):带更正词但字面重叠太少时,用来找被更正的旧记忆
    store = get_vector_store(get_memory_dir())
    semantic_kwargs = {}
    if store:
        try:
            store.ensure([f["content"] for f in existing_facts + new_facts])
            store.save()
            semantic_kwargs = {
                "semantic_similarity": store.similarity,
                "semantic_conflict_threshold": float(store.settings["correction_min_score"]),
            }
        except Exception as e:
            print(f"⚠️ 语义向量不可用,仅用字面相似度: {e}", file=sys.stderr)
            store = None

    operator = MemoryOperator(
        similarity_threshold=DEDUP_CONFIG["merge_overlap_ratio"],
        conflict_threshold=DEDUP_CONFIG["min_overlap_ratio"],
        override_signals=(OVERRIDE_SIGNALS_TIER1, OVERRIDE_SIGNALS_TIER2),
        **semantic_kwargs,
    )
    # LLM 判断(可选):规则判为新增时,对照相关旧记忆判断是否重复或已被取代
    judge = get_judge()

    merged = []
    duplicate_count = 0
    downgraded_count = 0

    for new_fact in new_facts:
        operation, target = operator.decide_operation(new_fact, existing_facts + merged)
        targets = [target] if target else []
        llm = None

        if operation == "ADD" and judge:
            candidates = _judge_candidates(new_fact, existing_facts + merged, store, operator, judge.settings)
            verdict = judge.judge(new_fact, candidates)
            if verdict and verdict["decision"] != "ADD":
                llm = {"decided_by": "llm", "reason": verdict["reason"]}
                targets = verdict["targets"]
                operation = "UPDATE" if verdict["decision"] == "DUPLICATE" else "SUPERSEDE"

        if operation == "NOOP":
            duplicate_count += 1
            events.append({"op": "NOOP", "id": new_fact["id"], "content": new_fact["content"]})

        elif operation == "ADD":
            merged.append(new_fact)
            events.append({"op": "ADD", "id": new_fact["id"], "content": new_fact["content"]})

        elif operation == "UPDATE" and targets:
            _merge_duplicate(targets[0], new_fact, events, llm)
            duplicate_count += 1

        elif operation == "DELETE" and targets:
            _supersede(targets[0], new_fact, operator.last_override_tier, events)
            merged.append(new_fact)
            downgraded_count += 1
            events.append({"op": "ADD", "id": new_fact["id"], "content": new_fact["content"]})

        elif operation == "SUPERSEDE" and targets:
            for old in targets:
                _supersede(old, new_fact, 1, events, llm)
            merged.append(new_fact)
            downgraded_count += len(targets)
            events.append({"op": "ADD", "id": new_fact["id"], "content": new_fact["content"]})

    return merged, duplicate_count, downgraded_count


def _merge_duplicate(target, new_fact, events, llm=None):
    """同一件事的两种说法:保留更完整的那一种("我喜欢跑步" → "我喜欢跑步,每周跑三次")"""
    if len(MemoryOperator._normalize(new_fact["content"])) > len(MemoryOperator._normalize(target["content"])):
        events.append(
            {
                "op": "MERGE",
                "id": target["id"],
                "from": new_fact["id"],
                "old_content": target["content"],
                "content": new_fact["content"],
                **(llm or {}),
            }
        )
        target["content"] = new_fact["content"]
        target["importance"] = max(target.get("importance", 0), new_fact.get("importance", 0))
        target["score"] = max(target.get("score", 0), new_fact.get("score", 0))
        target["updated"] = now_iso()
        target["merged_from"] = new_fact["id"]
    else:
        events.append(
            {"op": "NOOP", "id": new_fact["id"], "content": new_fact["content"], "duplicate_of": target["id"], **(llm or {})}
        )


def _supersede(target, new_fact, tier, events, llm=None):
    """新记忆更正旧记忆:强更正(tier 1)写入 invalid_at 后归档,弱更正(tier 2)只降权并从检索中隐藏"""
    penalty = DEDUP_CONFIG["tier1_penalty"] if tier == 1 else DEDUP_CONFIG["tier2_penalty"]
    target["score"] = target.get("score", target.get("importance", 0.5)) * penalty
    target["override_tier"] = tier
    target["conflict_downgraded"] = True
    target["downgrade_reason"] = new_fact["id"]
    target["downgrade_at"] = now_iso()
    if tier == 1:
        target["invalid_at"] = new_fact.get("created", now_iso())
        target["superseded_by"] = new_fact["id"]
    events.append(
        {
            "op": "INVALIDATE" if tier == 1 else "DOWNGRADE",
            "id": target["id"],
            "by": new_fact["id"],
            "content": target["content"],
            **(llm or {}),
        }
    )


def _judge_candidates(new_fact, existing, store, operator, settings):
    """挑选交给 LLM 判断的旧记忆:有语义向量时按语义相似度,否则按字面重叠率"""
    pool = [r for r in existing if r is not new_fact and not r.get("invalid_at")]
    if store:
        scored = [(store.similarity(new_fact["content"], r["content"]), r) for r in pool]
        min_score = settings["candidate_min_semantic"]
    else:
        scored = [(operator._calculate_similarity(new_fact["content"], r["content"]), r) for r in pool]
        min_score = settings["candidate_min_literal"]
    scored = sorted((x for x in scored if x[0] >= min_score), key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[: settings["max_candidates"]]]


def _deduplicate_legacy(new_facts, existing_facts):
    """原有去重逻辑(v1.1.6),作为降级兜底"""
    merged = []
    duplicate_count = 0
    downgraded_count = 0

    existing_by_entity = {}
    for fact in existing_facts:
        for entity in fact.get("entities", []):
            if entity not in existing_by_entity:
                existing_by_entity[entity] = []
            existing_by_entity[entity].append(fact)

    for new_fact in new_facts:
        is_duplicate = False
        new_content = new_fact["content"].lower()
        new_entities = new_fact.get("entities", [])

        has_tier1_override = any(signal in new_fact["content"] for signal in OVERRIDE_SIGNALS_TIER1)
        has_tier2_override = any(signal in new_fact["content"] for signal in OVERRIDE_SIGNALS_TIER2)
        has_override = has_tier1_override or has_tier2_override

        for entity in new_entities:
            if entity in existing_by_entity:
                for existing in existing_by_entity[entity]:
                    existing_content = existing["content"].lower()
                    new_tokens = tokenize_chinese(new_content)
                    existing_tokens = tokenize_chinese(existing_content)
                    overlap = len(new_tokens & existing_tokens)
                    min_len = min(len(new_tokens), len(existing_tokens))
                    overlap_ratio = overlap / max(min_len, 1)

                    is_similar = (
                        new_content in existing_content
                        or existing_content in new_content
                        or overlap_ratio >= DEDUP_CONFIG["min_overlap_ratio"]
                    )

                    if is_similar:
                        if has_override and overlap_ratio >= DEDUP_CONFIG["min_overlap_ratio"]:
                            old_score = existing.get("score", existing.get("importance", 0.5))
                            if has_tier1_override:
                                penalty = DEDUP_CONFIG["tier1_penalty"]
                                existing["override_tier"] = 1
                            else:
                                penalty = DEDUP_CONFIG["tier2_penalty"]
                                existing["override_tier"] = 2
                            existing["score"] = old_score * penalty
                            existing["conflict_downgraded"] = True
                            existing["downgrade_reason"] = new_fact["id"]
                            existing["downgrade_at"] = now_iso()
                            downgraded_count += 1
                        else:
                            if new_fact["importance"] > existing.get("importance", 0):
                                existing["content"] = new_fact["content"]
                                existing["importance"] = new_fact["importance"]
                                existing["score"] = max(existing.get("score", 0), new_fact["score"])
                            is_duplicate = True
                            duplicate_count += 1
                        break
            if is_duplicate:
                break

        if not is_duplicate:
            merged.append(new_fact)

    return merged, duplicate_count, downgraded_count


# ============================================================
# Phase-4B: Beliefs 验证 - code_verify_belief()
# ============================================================


def code_verify_belief(belief, facts):
    """
    Phase-4B: Beliefs 验证
    检查 belief 是否被 facts 证实

    返回: ("confirmed" | "contradicted" | "unchanged", updated_belief)
    """
    belief_content = belief["content"].lower()
    belief_entities = belief.get("entities", [])

    for fact in facts:
        fact_content = fact["content"].lower()
        fact_entities = fact.get("entities", [])

        # 检查实体重叠
        entity_overlap = set(belief_entities) & set(fact_entities)
        if not entity_overlap:
            continue

        # 检查内容关系
        # 1. 证实:fact 包含 belief 的核心内容
        belief_words = set(belief_content.split())
        fact_words = set(fact_content.split())
        overlap_ratio = len(belief_words & fact_words) / max(len(belief_words), 1)

        if overlap_ratio > 0.5:
            # 被证实 → 升级为 fact
            upgraded = belief.copy()
            upgraded["id"] = generate_id("f", belief["content"])
            upgraded["confidence"] = 1.0  # 升级为确定
            upgraded["verified_by"] = fact["id"]
            upgraded["verified_at"] = now_iso()
            return "confirmed", upgraded

        # 2. 矛盾检测(简单版:否定词)
        negation_words = ["不", "没", "无", "非", "否", "别", "不是", "没有"]
        belief_has_neg = any(neg in belief_content for neg in negation_words)
        fact_has_neg = any(neg in fact_content for neg in negation_words)

        if belief_has_neg != fact_has_neg and overlap_ratio > 0.3:
            # 可能矛盾 → 降低置信度
            updated = belief.copy()
            updated["confidence"] = max(0.1, belief.get("confidence", 0.6) - 0.3)
            updated["contradiction_hint"] = fact["id"]
            return "contradicted", updated

    return "unchanged", belief


# ============================================================
# Phase-4C: Summaries 生成
# ============================================================


def generate_summaries(facts, existing_summaries, trigger_count=3):
    """
    Phase-4C: Summaries 生成 + 自动 Reflection（v1.5.0）

    触发条件（Stanford GA Reflection 机制）：
    1. 实体 facts >= trigger_count 且从未生成摘要 → 首次生成
    2. 实体 facts >= 8 且距上次摘要 > 7 天 → 自动 Reflection（重新生成）

    返回: 新生成的 summaries 列表（不含已有摘要）
    """
    new_summaries = []
    now = _utcnow()

    # 按实体分组 facts
    facts_by_entity = {}
    for fact in facts:
        for entity in fact.get("entities", []):
            if entity not in facts_by_entity:
                facts_by_entity[entity] = []
            facts_by_entity[entity].append(fact)

    # 检查已有摘要：记录每个实体最近一次摘要时间
    entity_last_summary = {}
    for summary in existing_summaries:
        for entity in summary.get("entities", []):
            created = summary.get("created", "")
            if entity not in entity_last_summary or created > entity_last_summary[entity]:
                entity_last_summary[entity] = created

    for entity, entity_facts in facts_by_entity.items():
        last_summary_str = entity_last_summary.get(entity)
        should_generate = False

        if not last_summary_str and len(entity_facts) >= trigger_count:
            # 首次生成
            should_generate = True
        elif last_summary_str and len(entity_facts) >= 8:
            # Reflection：facts 足够多且距上次摘要超过 7 天
            try:
                last_dt = datetime.fromisoformat(last_summary_str.replace("Z", "+00:00")).replace(tzinfo=None)
                days_since = (now - last_dt).days
                if days_since >= 7:
                    should_generate = True
            except Exception:
                pass

        if not should_generate:
            continue

        # 按重要性排序，取 top-5
        sorted_facts = sorted(entity_facts, key=lambda x: x.get("importance", 0), reverse=True)
        top_facts = sorted_facts[:5]

        # identity facts 优先纳入
        identity_facts = [f for f in entity_facts if f.get("is_identity")]
        if identity_facts:
            top_facts = identity_facts[:3] + [f for f in top_facts if f not in identity_facts][:2]

        reflection_tag = "（自动Reflection）" if last_summary_str else ""
        summary_content = f"关于{entity}的综合认知{reflection_tag}（基于{len(entity_facts)}条记录）: " + \
                          "; ".join([f["content"][:40] for f in top_facts])

        avg_importance = sum(f.get("importance", 0.5) for f in top_facts) / len(top_facts)

        summary = {
            "id": generate_id("s", summary_content),
            "content": summary_content,
            "importance": avg_importance,
            "score": avg_importance,
            "entities": [entity],
            "source_facts": [f["id"] for f in top_facts],
            "created": now_iso(),
            "is_reflection": bool(last_summary_str),
        }
        new_summaries.append(summary)

    return new_summaries


# ============================================================
# Phase-4D: Entities 更新
# ============================================================


def update_entities(facts, beliefs, summaries, memory_dir):
    """
    Phase-4D: Entities 更新
    维护实体档案
    """
    entities_dir = memory_dir / "layer2/entities"
    index_path = entities_dir / "_index.json"

    # 加载现有索引
    if index_path.exists():
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
    else:
        index = {"entities": []}

    # 收集所有实体
    all_entities = set()
    entity_facts = {}
    entity_beliefs = {}
    entity_summaries = {}

    for fact in facts:
        for entity in fact.get("entities", []):
            all_entities.add(entity)
            if entity not in entity_facts:
                entity_facts[entity] = []
            entity_facts[entity].append(fact["id"])

    for belief in beliefs:
        for entity in belief.get("entities", []):
            all_entities.add(entity)
            if entity not in entity_beliefs:
                entity_beliefs[entity] = []
            entity_beliefs[entity].append(belief["id"])

    for summary in summaries:
        for entity in summary.get("entities", []):
            all_entities.add(entity)
            if entity not in entity_summaries:
                entity_summaries[entity] = []
            entity_summaries[entity].append(summary["id"])

    # 更新每个实体的档案
    updated_count = 0
    for entity in all_entities:
        entity_id = hashlib.md5(entity.encode()).hexdigest()[:8]
        entity_path = entities_dir / f"{entity_id}.json"

        entity_data = {
            "id": entity_id,
            "name": entity,
            "facts": entity_facts.get(entity, []),
            "beliefs": entity_beliefs.get(entity, []),
            "summaries": entity_summaries.get(entity, []),
            "updated": now_iso(),
        }

        with open(entity_path, "w", encoding="utf-8") as f:
            json.dump(entity_data, f, indent=2, ensure_ascii=False)

        # 更新索引
        if entity not in [e["name"] for e in index["entities"]]:
            index["entities"].append(
                {
                    "id": entity_id,
                    "name": entity,
                    "count": len(entity_facts.get(entity, [])) + len(entity_beliefs.get(entity, [])),
                }
            )

        updated_count += 1

    # 保存索引
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

    return updated_count


# ============================================================
# Router 逻辑 - 智能检索系统
# ============================================================

# 触发条件关键词
TRIGGER_KEYWORDS = {
    "layer0_explicit": ["你还记得", "帮我回忆", "之前说过", "上次提到", "我告诉过你"],
    "layer0_time": ["之前", "以前", "上次", "昨天", "前几天", "那时候", "当时"],
    "layer1_preference": ["我喜欢", "我讨厌", "我偏好", "我习惯", "我爱", "我恨"],
    "layer1_identity": ["我是", "我叫", "我的名字", "关于我"],
    "layer1_relation": ["朋友", "家人", "同事", "父母", "兄弟", "姐妹"],
    "layer1_project": ["项目", "任务", "计划", "目标", "进度"],
}

# 查询类型配置
QUERY_CONFIG = {
    "precise": {"initial": 15, "rerank": 10, "final": 8},
    "topic": {"initial": 25, "rerank": 16, "final": 13},
    "broad": {"initial": 35, "rerank": 25, "final": 18},
}

# 会话缓存
_session_cache = {}
_cache_ttl = 1800  # 30分钟


def get_cache_key(query):
    """生成缓存键"""
    return hashlib.md5(query.encode()).hexdigest()[:12]


def get_cached_result(query):
    """获取缓存结果(v1.3.0: 优先用 CacheManager L2 缓存)"""
    key = get_cache_key(query)

    if CACHE_MANAGER_ENABLED and _cache_manager_instance:
        try:
            result = _cache_manager_instance.get_query_result(key)
            if result is not None:
                return result
        except Exception:
            pass  # 降级到内存缓存

    # 原有内存缓存兜底
    if key in _session_cache:
        entry = _session_cache[key]
        if _utcnow().timestamp() - entry["time"] < _cache_ttl:
            return entry["result"]
        else:
            del _session_cache[key]
    return None


def set_cached_result(query, result):
    """设置缓存结果(v1.3.0: 同时写入 CacheManager L2 缓存)"""
    key = get_cache_key(query)

    if CACHE_MANAGER_ENABLED and _cache_manager_instance:
        try:
            _cache_manager_instance.set_query_result(key, result)
        except Exception:
            pass  # 降级到内存缓存

    # 原有内存缓存兜底
    _session_cache[key] = {"time": _utcnow().timestamp(), "result": result}


def detect_trigger_layer(query):
    """
    检测查询触发的层级
    返回: (layer, trigger_type, matched_keywords)
    """
    query_lower = query.lower()

    # Layer 0: 显式请求或时间引用
    for trigger_type in ["layer0_explicit", "layer0_time"]:
        keywords = TRIGGER_KEYWORDS[trigger_type]
        matched = [kw for kw in keywords if kw in query_lower]
        if matched:
            return 0, trigger_type, matched

    # Layer 1: 偏好/身份/关系/项目
    for trigger_type in ["layer1_preference", "layer1_identity", "layer1_relation", "layer1_project"]:
        keywords = TRIGGER_KEYWORDS[trigger_type]
        matched = [kw for kw in keywords if kw in query_lower]
        if matched:
            return 1, trigger_type, matched

    # Layer 2: 默认(任务类型映射)
    return 2, "default", []


def classify_query_type(query, trigger_layer):
    """
    分类查询类型: precise / topic / broad
    """
    query_lower = query.lower()

    # 精准查询:具体问题/特定实体
    precise_indicators = ["是什么", "是谁", "在哪", "什么时候", "多少", "具体"]
    if any(ind in query_lower for ind in precise_indicators) or trigger_layer == 0:
        return "precise"

    # 广度查询:总结/概览/所有
    broad_indicators = ["所有", "全部", "总结", "概括", "列出", "有哪些"]
    if any(ind in query_lower for ind in broad_indicators):
        return "broad"

    # 默认:主题查询
    return "topic"


def keyword_search(query, memory_dir, limit=20):
    """
    基于关键词的检索
    返回: [(memory_id, score, content), ...]
    """
    import re

    # 加载关键词索引
    keywords_path = memory_dir / "layer2/index/keywords.json"
    if not keywords_path.exists():
        return []

    with open(keywords_path, encoding="utf-8") as f:
        keywords_index = json.load(f)

    # 提取查询关键词(改进版);索引中英文已转小写,查询也转小写
    query_words = set()
    segments = re.split(r'[,.!?/;:""' r"()\[\][]\s]+", query.lower())
    for seg in segments:
        seg = seg.strip()
        if len(seg) >= 2:
            query_words.add(seg)
        # 提取2-4字子串
        for i in range(len(seg)):
            for length in [2, 3, 4]:
                if i + length <= len(seg):
                    sub = seg[i : i + length]
                    if len(sub) >= 2:
                        query_words.add(sub)

    # 计算每个记忆的匹配分数
    memory_scores = {}
    for word in query_words:
        if word in keywords_index:
            for mem_id in keywords_index[word]:
                if mem_id not in memory_scores:
                    memory_scores[mem_id] = 0
                memory_scores[mem_id] += 1

    # 加载记忆内容
    results = []
    all_memories = {}
    for mem_type in ["facts", "beliefs", "summaries"]:
        records = load_jsonl(memory_dir / f"layer2/active/{mem_type}.jsonl")
        for r in records:
            all_memories[r["id"]] = r

    # 排序并返回
    sorted_ids = sorted(memory_scores.keys(), key=lambda x: memory_scores[x], reverse=True)
    for mem_id in sorted_ids[:limit]:
        if mem_id in all_memories:
            mem = all_memories[mem_id]
            results.append(
                {
                    "id": mem_id,
                    "score": memory_scores[mem_id],
                    "content": mem.get("content", ""),
                    "importance": mem.get("importance", 0.5),
                    "memory_score": mem.get("score", 0.5),
                    "type": "fact" if mem_id.startswith("f_") else ("belief" if mem_id.startswith("b_") else "summary"),
                    "entities": mem.get("entities", []),
                    "last_accessed": mem.get("last_accessed"),
                    "is_identity": mem.get("is_identity", False),
                    "confidence": mem.get("confidence"),
                }
            )

    return results


def entity_search(query, memory_dir, limit=20):
    """
    基于实体的检索
    返回: [(memory_id, score, content), ...]
    """
    # 加载实体索引
    relations_path = memory_dir / "layer2/index/relations.json"
    if not relations_path.exists():
        return []

    with open(relations_path, encoding="utf-8") as f:
        relations_index = json.load(f)

    # 检查查询中是否包含已知实体
    matched_entities = []
    for entity in relations_index.keys():
        if entity in query:
            matched_entities.append(entity)

    if not matched_entities:
        return []

    # 收集相关记忆
    memory_ids = set()
    for entity in matched_entities:
        entity_data = relations_index[entity]
        for mem_type in ["facts", "beliefs", "summaries"]:
            memory_ids.update(entity_data.get(mem_type, []))

    # 加载记忆内容
    results = []
    all_memories = {}
    for mem_type in ["facts", "beliefs", "summaries"]:
        records = load_jsonl(memory_dir / f"layer2/active/{mem_type}.jsonl")
        for r in records:
            all_memories[r["id"]] = r

    for mem_id in list(memory_ids)[:limit]:
        if mem_id in all_memories:
            mem = all_memories[mem_id]
            results.append(
                {
                    "id": mem_id,
                    "score": len([e for e in matched_entities if e in mem.get("entities", [])]),
                    "content": mem.get("content", ""),
                    "importance": mem.get("importance", 0.5),
                    "memory_score": mem.get("score", 0.5),
                    "type": "fact" if mem_id.startswith("f_") else ("belief" if mem_id.startswith("b_") else "summary"),
                    "entities": mem.get("entities", []),
                    "last_accessed": mem.get("last_accessed"),
                    "is_identity": mem.get("is_identity", False),
                }
            )

    return results


def rerank_results(results, query, limit, memory_dir=None):
    """
    重排序检索结果

    v1.1.5 改进:集成实体隔离(竞争性抑制)
    v1.5.0 改进:三维检索评分 recency × importance × relevance（Stanford GA）
               identity facts 衰减减半（BMAM Identity Preservation）

    综合考虑: recency + 记忆重要性 + 匹配分数 + 实体隔离
    """
    import math

    now = _utcnow()

    # 1. 计算三维综合分数（v1.5.0）
    for r in results:
        # Recency: 指数衰减，半衰期 7 天；identity facts 半衰期 14 天
        last_accessed = r.get("last_accessed") or r.get("created", "")
        try:
            dt = datetime.fromisoformat(last_accessed.replace("Z", "+00:00")).replace(tzinfo=None)
            days_ago = max(0, (now - dt).days)
        except Exception:
            days_ago = 30
        decay_lambda = 0.05 if r.get("is_identity") else 0.1  # identity 衰减更慢
        recency = math.exp(-decay_lambda * days_ago)

        importance = r.get("importance", 0.5)
        relevance = r.get("score", 0) * 0.5 + r.get("memory_score", 0.5) * 0.5

        r["final_score"] = 0.35 * recency + 0.35 * importance + 0.30 * relevance

    # 2. v1.1.5: 实体隔离(竞争性抑制)
    if V1_1_5_ENABLED and results:
        from v1_1_5_entity_system import (
            ENTITY_SYSTEM_CONFIG,
            find_similar_entity_groups,
        )

        if memory_dir is None:
            memory_dir = get_memory_dir()

        # 从查询中提取实体
        query_entities = extract_entities(query, memory_dir=memory_dir, use_llm_fallback=False)

        if query_entities:
            # 收集所有候选实体
            all_candidate_entities = set()
            for r in results:
                all_candidate_entities.update(r.get("entities", []))

            # 找出相似实体组
            similar_groups = find_similar_entity_groups(query_entities, list(all_candidate_entities))

            if similar_groups:
                inhibition_factor = ENTITY_SYSTEM_CONFIG["isolation"]["inhibition_factor"]

                for r in results:
                    mem_entities = set(r.get("entities", []))

                    # 精确匹配查询实体 → 保持权重
                    if mem_entities & set(query_entities):
                        continue

                    # 包含相似但不同的实体 → 抑制
                    for group in similar_groups:
                        if mem_entities & group and not (mem_entities & set(query_entities)):
                            r["final_score"] *= inhibition_factor
                            r["isolation_applied"] = True
                            r["isolation_reason"] = f"竞争性抑制: {mem_entities & group}"
                            break

    # 3. 按综合分数排序
    results.sort(key=lambda x: x["final_score"], reverse=True)
    return results[:limit]


def spreading_activation(results, query, memory_dir, spread_factor=0.3, max_spread=5):
    """
    Spreading Activation（ACT-R 启发）
    检索结果中的实体通过共现关系激活关联记忆

    spread_factor: 激活传播衰减系数（默认 0.3）
    max_spread: 每个实体最多激活的额外记忆数
    """
    if not results:
        return results

    # 收集已有结果的实体
    activated_entities = set()
    existing_ids = {r["id"] for r in results}
    for r in results[:5]:  # 只从 top-5 传播，避免噪声
        activated_entities.update(r.get("entities", []))

    if not activated_entities:
        return results

    # 加载 relations 和 facts
    relations_path = Path(memory_dir) / "layer2/index/relations.json"
    facts_path = Path(memory_dir) / "layer2/active/facts.jsonl"
    if not relations_path.exists() or not facts_path.exists():
        return results

    try:
        relations = json.loads(relations_path.read_text())
        all_facts = load_jsonl(facts_path)
    except Exception:
        return results

    # 建立 id→fact 索引
    fact_index = {f["id"]: f for f in all_facts}

    # 通过共现关系找关联记忆
    spread_records = []
    seen_spread = set()

    for entity in activated_entities:
        entity_data = relations.get(entity, {})
        related_fact_ids = entity_data.get("facts", []) if isinstance(entity_data, dict) else []

        count = 0
        for fid in related_fact_ids:
            if fid in existing_ids or fid in seen_spread:
                continue
            fact = fact_index.get(fid)
            if not fact:
                continue
            fact_copy = fact.copy()
            # 传播激活：原始 final_score × spread_factor
            base_score = fact_copy.get("final_score", fact_copy.get("importance", 0.5))
            fact_copy["final_score"] = base_score * spread_factor
            fact_copy["spread_from"] = entity
            # 确保 type 字段存在
            if "type" not in fact_copy:
                fid = fact_copy.get("id", "")
                fact_copy["type"] = "fact" if fid.startswith("f_") else ("belief" if fid.startswith("b_") else "summary")
            spread_records.append(fact_copy)
            seen_spread.add(fid)
            count += 1
            if count >= max_spread:
                break

    return results + spread_records


def format_injection(results, confidence_threshold_high=0.8, confidence_threshold_low=0.5):
    """
    格式化注入结果
    - 高置信度(>0.8): 直接注入,无标记
    - 中置信度(0.5-0.8): 注入 + 来源标记
    - 低置信度(<0.5): 仅提供引用路径
    """
    output = {
        "direct": [],  # 直接注入
        "marked": [],  # 带标记注入
        "reference": [],  # 仅引用
    }

    for r in results:
        confidence = r.get("memory_score", 0.5)

        if confidence >= confidence_threshold_high:
            output["direct"].append({"content": r["content"], "type": r["type"]})
        elif confidence >= confidence_threshold_low:
            output["marked"].append({"content": r["content"], "type": r["type"], "source": r["id"]})
        else:
            output["reference"].append({"id": r["id"], "preview": r["content"][:50] + "..."})

    return output


# ============================================================
# v1.2.0 QMD 集成
# ============================================================


def _get_qmd_env():
    """获取 QMD 运行环境"""
    home = os.path.expanduser("~")
    return {
        **os.environ,
        "PATH": f"{home}/.bun/bin:{os.environ.get('PATH', '')}",
        "NO_COLOR": "1",
    }


def qmd_available(memory_dir=None):
    """
    检查 QMD 是否可用(v1.2.1 增强版)

    检查项:
    1. qmd 命令是否存在
    2. qmd status 是否正常
    3. health.lock 是否存在(写入中断标记)
    """
    try:
        env = _get_qmd_env()
        result = subprocess.run(["qmd", "status"], capture_output=True, timeout=5, env=env)
        if result.returncode != 0:
            return False

        # v1.2.1: 检查 health.lock(写入中断标记)
        if memory_dir is None:
            memory_dir = get_memory_dir()
        qmd_dir = Path(memory_dir) / ".qmd"
        if (qmd_dir / "health.lock").exists():
            # 上次写入中断,静默 fallback 到非 QMD 模式
            return False

        return True
    except:
        return False


def qmd_search(query, collection="curated", limit=20):
    """
    使用 QMD 进行检索

    参数:
        query: 查询字符串
        collection: QMD 集合名称
        limit: 返回结果数量

    返回:
        [{"docid": ..., "score": ..., "snippet": ..., "file": ...}, ...]
        或空列表
    """
    try:
        env = _get_qmd_env()

        # 提取关键词(优先英文/专有名词,然后中文实体词)
        keywords = []
        # 英文单词和专有名词(优先)
        keywords.extend(re.findall(r"[A-Za-z][A-Za-z0-9_-]+", query))
        # 中文词组(3字以上,避免"是谁"这类疑问词)
        keywords.extend(re.findall(r"[\u4e00-\u9fa5]{3,}", query))

        # 如果没有提取到关键词,尝试2字中文词
        if not keywords:
            keywords.extend(re.findall(r"[\u4e00-\u9fa5]{2,}", query))

        # 如果还是没有,用原始查询
        search_query = " ".join(keywords) if keywords else query

        # 使用 BM25 搜索
        result = subprocess.run(
            ["qmd", "search", search_query, "-c", collection, "-n", str(limit)],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )

        if result.returncode == 0 and result.stdout.strip() and "No results" not in result.stdout:
            return _parse_qmd_output(result.stdout)

    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass

    return []


def _parse_qmd_output(output):
    """解析 QMD search 输出"""
    results = []
    current = None
    in_content = False

    for line in output.split("\n"):
        # 新结果开始:qmd://curated/facts.md:7 #8ec92f
        if line.startswith("qmd://"):
            if current:
                results.append(current)
            parts = line.split()
            file_info = parts[0] if parts else ""
            docid = parts[1] if len(parts) > 1 else ""
            current = {
                "file": file_info.split(":")[0] if ":" in file_info else file_info,
                "line": file_info.split(":")[1].split()[0] if ":" in file_info else "1",
                "docid": docid,
                "score": 0,
                "snippet": "",
            }
            in_content = False
        elif current:
            if line.startswith("Score:"):
                # Score:  35%
                try:
                    score_str = line.replace("Score:", "").strip().replace("%", "")
                    current["score"] = float(score_str) / 100
                except:
                    pass
            elif line.startswith("@@"):
                # @@ -6,4 @@ 开始内容区域
                in_content = True
            elif line.startswith("Title:"):
                pass  # 跳过
            elif in_content and line.strip():
                # 内容行
                current["snippet"] += line + "\n"

    if current:
        results.append(current)

    return results


def extract_memory_id_from_snippet(snippet):
    """
    从 QMD 返回的 snippet 中提取 memory_id

    格式示例:
    [f_20260207_a6b928] 用户名字是Ktao...
    """
    match = re.search(r"\[([fbs]_\d{8}_[a-f0-9]+)\]", snippet)
    return match.group(1) if match else None


def export_for_qmd(memory_dir):
    """
    将 JSONL 转换为 QMD 友好的 Markdown 格式(v1.2.1 增强版)

    新增:
    - health.lock 写入锁(防止脏数据)
    - meta.json 元数据(版本/更新时间/记忆数)
    - .qmd/ 目录结构
    """
    memory_dir = Path(memory_dir)

    # v1.2.1: 使用 .qmd/ 目录
    qmd_dir = memory_dir / ".qmd"
    qmd_index_dir = qmd_dir / "index"
    qmd_index_dir.mkdir(parents=True, exist_ok=True)

    # 同时保留原有位置(兼容性)
    legacy_dir = memory_dir / "layer2/qmd-index"
    legacy_dir.mkdir(parents=True, exist_ok=True)

    # v1.2.1: 写入前创建锁
    lock_file = qmd_dir / "health.lock"
    lock_file.touch()

    total_count = 0

    try:
        for mem_type in ["facts", "beliefs", "summaries"]:
            records = load_jsonl(memory_dir / f"layer2/active/{mem_type}.jsonl")
            total_count += len(records)

            md_content = f"# {mem_type.title()}\n\n"
            md_content += f"> Generated: {now_iso()} | Count: {len(records)}\n\n"

            for r in records:
                # 格式:[memory_id] 内容
                md_content += f"[{r['id']}] {r['content']}\n\n"

                if r.get("entities"):
                    md_content += f"**Entities**: {', '.join(r['entities'])}\n\n"

                md_content += "---\n\n"

            # 写入新位置
            output_path = qmd_index_dir / f"{mem_type}.md"
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(md_content)

            # 写入兼容位置
            legacy_path = legacy_dir / f"{mem_type}.md"
            with open(legacy_path, "w", encoding="utf-8") as f:
                f.write(md_content)

        # v1.2.1: 写入 meta.json
        meta = {
            "version": "1.2.1",
            "updated": now_iso(),
            "count": total_count,
            "types": {
                "facts": len(load_jsonl(memory_dir / "layer2/active/facts.jsonl")),
                "beliefs": len(load_jsonl(memory_dir / "layer2/active/beliefs.jsonl")),
                "summaries": len(load_jsonl(memory_dir / "layer2/active/summaries.jsonl")),
            },
        }
        meta_file = qmd_dir / "meta.json"
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

    finally:
        # v1.2.1: 完成后删除锁
        if lock_file.exists():
            lock_file.unlink()

    return qmd_index_dir


def router_search(query, memory_dir=None, use_qmd=True, use_vector=True):
    """
    Router 主入口:智能检索记忆(v1.6.0 向量检索增强版)

    搜索顺序:
    1. Pending (Hot Store) - 未索引的新记忆
    2. 向量检索 - 语义相似度匹配(v1.6.0 新增)
    3. QMD 索引 - 已索引,快速语义搜索
    4. 关键词/实体索引 - 原有逻辑
    5. LLM 兜底 - QMD 不可用时

    参数:
        query: 用户查询
        memory_dir: 记忆目录(可选)
        use_qmd: 是否使用 QMD 检索(默认 True)
        use_vector: 是否使用向量检索(默认 True)

    返回:
        {
            "trigger_layer": 0/1/2,
            "trigger_type": str,
            "query_type": "precise"/"topic"/"broad",
            "results": [...],
            "injection": {...},
            "cached": bool,
            "qmd_used": bool,
            "vector_used": bool,
            "pending_hits": int
        }
    """
    if memory_dir is None:
        memory_dir = get_memory_dir()

    cached = get_cached_result(query)
    if cached:
        cached["cached"] = True
        return cached

    # v1.4.0: 时序查询前置——有时间表达式时优先走时序引擎
    if TEMPORAL_ENGINE_ENABLED:
        try:
            temporal_engine = create_temporal_engine(memory_dir)
            temporal_result = temporal_engine.temporal_search(query)
            if temporal_result["has_temporal"] and temporal_result["results"]:
                injection = format_injection(temporal_result["results"])
                result = {
                    "trigger_layer": 0,
                    "trigger_type": "temporal",
                    "matched_keywords": [],
                    "query_type": "precise",
                    "results": temporal_result["results"],
                    "injection": injection,
                    "stats": {
                        "pending_hits": 0,
                        "vector_hits": 0,
                        "keyword_hits": 0,
                        "entity_hits": 0,
                        "qmd_hits": 0,
                        "merged": len(temporal_result["results"]),
                        "final": len(temporal_result["results"]),
                    },
                    "qmd_used": False,
                    "vector_used": False,
                    "pending_hits": 0,
                    "cached": False,
                    "temporal_used": True,
                    "time_range": temporal_result["time_range"],
                }
                set_cached_result(query, result)
                return result
        except Exception:
            pass  # 降级到原有检索流程

    trigger_layer, trigger_type, matched_keywords = detect_trigger_layer(query)

    query_type = classify_query_type(query, trigger_layer)
    config = QUERY_CONFIG[query_type]

    pending_results = search_pending(query, memory_dir)

    vector_results = []
    vector_used = False
    if use_vector and VECTOR_SEARCH_ENABLED:
        vector_results = _vector_search(query, memory_dir, config["initial"])
        if vector_results:
            vector_used = True

    # 语义向量检索(可选,未配置时为空)
    semantic_results = []
    store = get_vector_store(memory_dir)
    if store:
        try:
            semantic_results = semantic_search(query, memory_dir, store, config["initial"])
            store.save()
        except Exception as e:
            print(f"⚠️ 语义检索失败,仅使用关键词检索: {e}", file=sys.stderr)

    # v1.5.2: TF-IDF 语义检索
    tfidf_results = []
    if TFIDF_ENABLED:
        try:
            tfidf_results = tfidf_search(query, memory_dir, top_k=config["initial"])
        except Exception:
            pass

    qmd_results = []
    qmd_used = False
    if use_qmd and qmd_available(memory_dir):
        qmd_raw = qmd_search(query, collection="curated", limit=config["initial"])
        if qmd_raw and len(qmd_raw) > 0:
            qmd_used = True
            all_records = _load_all_active_records(memory_dir)
            for qr in qmd_raw:
                mem_id = extract_memory_id_from_snippet(qr.get("snippet", ""))
                if mem_id and mem_id in all_records:
                    record = all_records[mem_id].copy()
                    record["qmd_score"] = qr.get("score", 0)
                    record["match_source"] = "qmd"
                    qmd_results.append(record)

    keyword_results = keyword_search(query, memory_dir, limit=config["initial"])
    entity_results = entity_search(query, memory_dir, limit=config["initial"])

    # v1.5.2: RRF 合并多路结果（pending 直接保留，其余走 RRF）
    if TFIDF_ENABLED and (tfidf_results or qmd_results or keyword_results or entity_results):
        rrf_input = [
            l for l in [tfidf_results, qmd_results, keyword_results, entity_results, vector_results, semantic_results] if l
        ]
        rrf_merged = rrf_merge(rrf_input, k=60, top_n=config["rerank"])
        # pending 优先，RRF 结果去重追加
        seen_ids = {r["id"] for r in pending_results}
        merged_results = list(pending_results)
        for r in rrf_merged:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                merged_results.append(r)
    else:
        # 降级：原有简单合并
        seen_ids = set()
        merged_results = []
        for r in pending_results:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                merged_results.append(r)
        # 同一条记忆被多路命中时保留先出现的(关键词结果优先于语义结果)
        for r in vector_results + qmd_results + keyword_results + entity_results + semantic_results:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                merged_results.append(r)

    # 已被更正降权的旧说法不再出现在检索结果里(仍可用 get 按 ID 取回)
    corrected = _corrected_ids(memory_dir)
    merged_results = [r for r in merged_results if r.get("id") not in corrected]

    # 字面命中优先,语义结果只补在后面:避免"重要性高但不相关"的语义近邻排到精确命中前面
    literal = [r for r in merged_results if r.get("match_source") != "semantic"]
    semantic_only = [r for r in merged_results if r.get("match_source") == "semantic"]
    reranked = rerank_results(literal, query, config["rerank"], memory_dir=memory_dir)
    reranked += rerank_results(semantic_only, query, config["rerank"], memory_dir=memory_dir)
    reranked = reranked[: config["rerank"]]

    # v1.5.0: Spreading Activation（ACT-R）
    # spread 记录单独保留 top-3，追加到 final 结果末尾
    try:
        spread_all = spreading_activation(reranked, query, memory_dir)
        spread_bonus = [r for r in spread_all if r.get("spread_from")]
        spread_bonus.sort(key=lambda x: x["final_score"], reverse=True)
        spread_bonus = spread_bonus[:3]
    except Exception as e:
        import traceback
        print(f"⚠️  Spreading Activation 失败: {e}")
        traceback.print_exc()
        spread_bonus = []

    final_results = reranked[: config["final"]] + [r for r in spread_bonus if r.get("id") not in corrected]

    injection = format_injection(final_results)

    result = {
        "trigger_layer": trigger_layer,
        "trigger_type": trigger_type,
        "matched_keywords": matched_keywords,
        "query_type": query_type,
        "results": final_results,
        "injection": injection,
        "stats": {
            "pending_hits": len(pending_results),
            "vector_hits": len(vector_results),
            "keyword_hits": len(keyword_results),
            "entity_hits": len(entity_results),
            "qmd_hits": len(qmd_results),
            "merged": len(merged_results),
            "final": len(final_results),
        },
        "qmd_used": qmd_used,
        "vector_used": vector_used,
        "pending_hits": len(pending_results),
        "cached": False,
    }

    set_cached_result(query, result)

    return result


def _vector_search(query: str, memory_dir: Path, limit: int = 20) -> list:
    """
    向量检索(v1.6.0 新增)

    使用混合检索引擎进行语义搜索
    """
    config = get_config()
    vector_config = config.get("vector", {})

    if not vector_config.get("enabled", False):
        return []

    try:
        embedding_engine = get_embedding_engine(
            provider=vector_config.get("provider", "openai"),
            model=vector_config.get("model"),
        )
        if embedding_engine is None:
            return []

        hybrid_config = vector_config.get("hybrid_search", {})
        hybrid_engine = create_hybrid_search_engine(
            memory_dir=memory_dir,
            embedding_engine=embedding_engine,
            backend=vector_config.get("backend", "sqlite"),
            keyword_weight=hybrid_config.get("keyword_weight", 0.3),
            vector_weight=hybrid_config.get("vector_weight", 0.7),
            min_score=hybrid_config.get("min_score", 0.2),
        )
        if hybrid_engine is None:
            return []

        results = hybrid_engine.search(
            query=query,
            memory_dir=memory_dir,
            top_k=limit,
            use_keyword=False,
            use_vector=True,
        )

        formatted_results = []
        for r in results:
            formatted_results.append(
                {
                    "id": r.id,
                    "content": r.content,
                    "score": r.score,
                    "vector_score": r.vector_score,
                    "keyword_score": r.keyword_score,
                    "importance": r.metadata.get("importance", 0.5),
                    "memory_score": r.score,
                    "type": r.metadata.get("type", "fact"),
                    "entities": [],
                    "match_source": "vector",
                }
            )

        return formatted_results

    except Exception as e:
        print(f"⚠️ 向量检索失败: {e}")
        return []


def _corrected_ids(memory_dir):
    """活跃池中被更正降权(conflict_downgraded)的记忆 ID"""
    return {
        r["id"]
        for mem_type in ["facts", "beliefs", "summaries"]
        for r in load_jsonl(memory_dir / f"layer2/active/{mem_type}.jsonl")
        if r.get("conflict_downgraded")
    }


def get_vector_store(memory_dir):
    """启用语义向量(见 src/embeddings.py)时返回 VectorStore,否则返回 None"""
    settings = load_embedding_settings(get_config())
    embedder = get_embedder(settings)
    return VectorStore(memory_dir, embedder, settings) if embedder else None


def semantic_search(query, memory_dir, store, limit=20):
    """语义检索:按查询与记忆的余弦相似度返回结果(格式同 keyword_search)"""
    records = list(_load_all_active_records(memory_dir).values())
    store.ensure([r["content"] for r in records])
    query_vec = store.embed_query(query)
    min_score = float(store.settings["search_min_score"])

    scored = []
    for r in records:
        vec = store.get(r["content"])
        if vec is not None:
            sim = cosine(query_vec, vec)
            if sim >= min_score:
                scored.append((sim, r))
    scored.sort(key=lambda x: x[0], reverse=True)

    return [
        {
            "id": r["id"],
            "score": sim,
            "content": r.get("content", ""),
            "importance": r.get("importance", 0.5),
            "memory_score": r.get("score", 0.5),
            "type": r.get("type", "fact"),
            "entities": r.get("entities", []),
            "last_accessed": r.get("last_accessed"),
            "is_identity": r.get("is_identity", False),
            "confidence": r.get("confidence"),
            "match_source": "semantic",
        }
        for sim, r in scored[:limit]
    ]


def _get_active_memory_count(memory_dir):
    """获取活跃记忆总数"""
    count = 0
    for mem_type in ["facts", "beliefs", "summaries"]:
        path = memory_dir / f"layer2/active/{mem_type}.jsonl"
        count += sum(1 for _ in load_jsonl(path))
    return count


def _load_all_active_records(memory_dir):
    """加载所有活跃记录,返回 {id: record} 字典
    v1.3.0: 超过 SCALED_BACKEND_THRESHOLD 自动切换 ScaledBackend
    """
    # 阈值判断:超过 5000 条自动启用 ScaledBackend
    if SCALED_BACKEND_AVAILABLE:
        try:
            count = _get_active_memory_count(memory_dir)
            if count >= SCALED_BACKEND_THRESHOLD:
                backend = ScaledBackend(memory_dir)
                all_mems = backend.get_all_active_memories()
                return {m["id"]: m for m in all_mems}
        except Exception:
            pass  # 降级到原有逻辑

    # 原有 JSONL 逻辑
    records = {}
    for mem_type in ["facts", "beliefs", "summaries"]:
        path = memory_dir / f"layer2/active/{mem_type}.jsonl"
        for r in load_jsonl(path):
            r["type"] = mem_type.rstrip("s")
            records[r["id"]] = r
    return records


def cmd_search(args):
    """执行记忆检索"""
    memory_dir = get_memory_dir()

    if not memory_dir.exists():
        print("❌ 记忆系统未初始化")
        return

    query = args.query
    result = router_search(query, memory_dir)

    if args.json:
        # 给 Agent 的精简索引:先看摘要和 ID,需要细节再用 get 取全文
        print(
            json.dumps(
                {
                    "query": query,
                    "results": [
                        {
                            "id": r["id"],
                            "type": r["type"],
                            "content": r["content"][:120],
                            "score": round(r.get("final_score", 0), 3),
                            "importance": r.get("importance"),
                            "confidence": r.get("confidence"),
                        }
                        for r in result["results"][: args.limit]
                    ],
                },
                ensure_ascii=False,
            )
        )
        return

    print(f"🔍 检索: {query}")
    print("=" * 50)
    print(f"触发层级: Layer {result['trigger_layer']} ({result['trigger_type']})")
    print(f"查询类型: {result['query_type']}")
    print(f"匹配关键词: {result['matched_keywords']}")
    print(f"缓存命中: {'是' if result['cached'] else '否'}")
    print()
    print("📊 检索统计:")
    print(f"   关键词命中: {result['stats']['keyword_hits']}")
    print(f"   实体命中: {result['stats']['entity_hits']}")
    print(f"   合并后: {result['stats']['merged']}")
    print(f"   最终结果: {result['stats']['final']}")
    print()

    if result["results"]:
        print("📋 检索结果:")
        for i, r in enumerate(result["results"][:10]):
            print(f"   {i + 1}. [{r['type'][0].upper()}] {r['content'][:50]}...")
            print(f"      score={r['final_score']:.2f}, importance={r['importance']:.1f}")
    else:
        print("📋 无匹配结果")

    print()
    print("💉 注入建议:")
    inj = result["injection"]
    print(f"   直接注入: {len(inj['direct'])} 条")
    print(f"   带标记注入: {len(inj['marked'])} 条")
    print(f"   仅引用: {len(inj['reference'])} 条")


def cmd_get(args):
    """按 ID 取记忆全文(含归档池),输出 JSON"""
    memory_dir = get_memory_dir()
    found = {}
    for pool in ["active", "archive"]:
        for mem_type in ["facts", "beliefs", "summaries"]:
            for r in load_jsonl(memory_dir / f"layer2/{pool}/{mem_type}.jsonl"):
                if r["id"] in args.ids and r["id"] not in found:
                    found[r["id"]] = {**r, "type": mem_type.rstrip("s"), "pool": pool}
    missing = [i for i in args.ids if i not in found]
    print(json.dumps({"memories": [found[i] for i in args.ids if i in found], "missing": missing}, ensure_ascii=False))


# ============================================================
# 初始化命令
# ============================================================


def cmd_init(args):
    """初始化记忆系统目录结构"""
    memory_dir = get_memory_dir()

    # 创建目录结构
    dirs = ["layer1", "layer2/active", "layer2/archive", "layer2/entities", "layer2/index", "state"]

    for d in dirs:
        (memory_dir / d).mkdir(parents=True, exist_ok=True)

    # 创建默认配置
    config_path = memory_dir / "config.json"
    if not config_path.exists():
        save_config(DEFAULT_CONFIG)

    # 创建空的 JSONL 文件
    jsonl_files = [
        "layer2/active/facts.jsonl",
        "layer2/active/beliefs.jsonl",
        "layer2/active/summaries.jsonl",
        "layer2/archive/facts.jsonl",
        "layer2/archive/beliefs.jsonl",
        "layer2/archive/summaries.jsonl",
    ]

    for f in jsonl_files:
        path = memory_dir / f
        if not path.exists():
            path.touch()

    # 创建索引文件
    index_files = {
        "layer2/index/keywords.json": {},
        "layer2/index/timeline.json": {},
        "layer2/index/relations.json": {},
        "layer2/entities/_index.json": {"entities": []},
    }

    for f, default in index_files.items():
        path = memory_dir / f
        if not path.exists():
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(default, fp, indent=2, ensure_ascii=False)

    # 创建状态文件
    state_files = {
        "state/consolidation.json": {
            "last_run": None,
            "last_success": None,
            "current_phase": None,
            "phase_data": {},
            "retry_count": 0,
        },
        "state/rankings.json": {"updated": None, "rankings": []},
    }

    for f, default in state_files.items():
        path = memory_dir / f
        if not path.exists():
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(default, fp, indent=2, ensure_ascii=False)

    # 创建初始 Layer 1 快照
    snapshot_path = memory_dir / "layer1/snapshot.md"
    if not snapshot_path.exists():
        snapshot_content = f"""# 工作记忆快照
> 生成时间: {now_iso()} | 状态: 初始化

## 说明
记忆系统已初始化,尚无记忆数据.
执行 `memory.py consolidate` 开始整合记忆.
"""
        with open(snapshot_path, "w", encoding="utf-8") as f:
            f.write(snapshot_content)

    # v1.2.1: 创建 .gitignore(保护 .qmd/ 目录)
    gitignore_path = memory_dir / ".gitignore"
    if not gitignore_path.exists():
        with open(gitignore_path, "w", encoding="utf-8") as f:
            f.write("# Memory System v1.2.1\n")
            f.write("# QMD 索引目录(二进制文件,不应提交到 git)\n")
            f.write(".qmd/\n")

    print("✅ 记忆系统初始化完成")
    print(f"   目录: {memory_dir}")
    print(f"   配置: {memory_dir / 'config.json'}")


# ============================================================
# 状态命令
# ============================================================


def cmd_status(args):
    """显示系统状态"""
    memory_dir = get_memory_dir()

    if not memory_dir.exists():
        print("❌ 记忆系统未初始化,请先运行: memory.py init")
        return

    # 读取状态
    state_path = memory_dir / "state/consolidation.json"
    if state_path.exists():
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}

    # 统计记忆数量
    active_facts = len(load_jsonl(memory_dir / "layer2/active/facts.jsonl"))
    active_beliefs = len(load_jsonl(memory_dir / "layer2/active/beliefs.jsonl"))
    active_summaries = len(load_jsonl(memory_dir / "layer2/active/summaries.jsonl"))
    archive_facts = len(load_jsonl(memory_dir / "layer2/archive/facts.jsonl"))
    archive_beliefs = len(load_jsonl(memory_dir / "layer2/archive/beliefs.jsonl"))
    archive_summaries = len(load_jsonl(memory_dir / "layer2/archive/summaries.jsonl"))

    active_total = active_facts + active_beliefs + active_summaries
    archive_total = archive_facts + archive_beliefs + archive_summaries

    print("🧠 Memory System Status")
    print("=" * 40)
    print(f"目录: {memory_dir}")
    print()
    print("📊 记忆统计")
    print(f"   活跃池: {active_total} 条")
    print(f"     - Facts: {active_facts}")
    print(f"     - Beliefs: {active_beliefs}")
    print(f"     - Summaries: {active_summaries}")
    print(f"   归档池: {archive_total} 条")
    print()
    print("⏰ Consolidation")
    print(f"   上次运行: {state.get('last_run', '从未')}")
    print(f"   上次成功: {state.get('last_success', '从未')}")
    print(f"   当前阶段: {state.get('current_phase', '无')}")
    print()
    settings = load_embedding_settings(get_config())
    if settings["provider"] == "none":
        print("🔎 语义检索: 未启用(仅关键词,见 src/embeddings.py)")
    else:
        store = get_vector_store(memory_dir)
        if store:
            print(f"🔎 语义检索: {store.embedder.model_id},已缓存 {len(store.vectors)} 条向量")
        else:
            print(f"🔎 语义检索: 已配置 {settings['provider']} 但不可用,仅关键词")


def cmd_stats(args):
    """显示详细统计"""
    memory_dir = get_memory_dir()

    if not memory_dir.exists():
        print("❌ 记忆系统未初始化")
        return

    # 加载所有记忆
    facts = load_jsonl(memory_dir / "layer2/active/facts.jsonl")
    beliefs = load_jsonl(memory_dir / "layer2/active/beliefs.jsonl")
    summaries = load_jsonl(memory_dir / "layer2/active/summaries.jsonl")

    # 按重要性分组
    importance_groups = {
        "critical": 0,  # 0.9-1.0
        "high": 0,  # 0.7-0.9
        "medium": 0,  # 0.4-0.7
        "low": 0,  # 0-0.4
    }

    all_records = facts + beliefs + summaries
    for r in all_records:
        imp = r.get("importance", 0.5)
        if imp >= 0.9:
            importance_groups["critical"] += 1
        elif imp >= 0.7:
            importance_groups["high"] += 1
        elif imp >= 0.4:
            importance_groups["medium"] += 1
        else:
            importance_groups["low"] += 1

    print("📊 Memory System Stats")
    print("=" * 40)
    print(f"Total: {len(all_records)} memories")
    print()
    print("By Type:")
    print(f"  Facts: {len(facts)} ({len(facts) * 100 // max(len(all_records), 1)}%)")
    print(f"  Beliefs: {len(beliefs)} ({len(beliefs) * 100 // max(len(all_records), 1)}%)")
    print(f"  Summaries: {len(summaries)} ({len(summaries) * 100 // max(len(all_records), 1)}%)")
    print()
    print("By Importance:")
    print(f"  Critical (0.9-1.0): {importance_groups['critical']}")
    print(f"  High (0.7-0.9): {importance_groups['high']}")
    print(f"  Medium (0.4-0.7): {importance_groups['medium']}")
    print(f"  Low (0-0.4): {importance_groups['low']}")


# ============================================================
# 手动操作命令
# ============================================================


def cmd_capture(args):
    """手动添加记忆"""
    memory_dir = get_memory_dir()

    if not memory_dir.exists():
        print("❌ 记忆系统未初始化")
        return

    content = args.content
    mem_type = args.type
    importance = args.importance

    # 输入验证
    if not content or not content.strip():
        print("❌ 错误: 内容不能为空")
        return

    # 与 consolidate Phase 2 使用同一套噪声/敏感信息过滤
    noise, noise_category = is_noise(content)
    if noise:
        if noise_category == "secret":
            print("⚠️ 检测到密码、密钥、证件号等敏感信息,未保存")
        else:
            print("ℹ️ 判定为寒暄、提问或一次性请求,未保存")
        return

    # 未指定 importance 时按规则计算(与 consolidate Phase 2 一致)
    auto_importance, category = calculate_importance(content)
    if importance is None:
        importance = auto_importance
    # 限制 importance 在 0-1 范围
    elif importance < 0:
        importance = 0
        print("⚠️ 警告: importance 已调整为 0")
    elif importance > 1:
        importance = 1
        print("⚠️ 警告: importance 已调整为 1")
    entities = args.entities.split(",") if args.entities else []

    record = {
        "id": generate_id(mem_type[0], content),
        "content": content,
        "importance": importance,
        "score": importance,  # 初始 score = importance
        "entities": entities,
        "created": now_iso(),
        "source": "manual",
    }

    if mem_type == "belief":
        record["confidence"] = args.confidence

    ground_record_time(record, category)
    content = record["content"]

    if mem_type == "fact":
        # facts 与 consolidate Phase 4a 走同一套去重/冲突检测
        path = memory_dir / "layer2/active/facts.jsonl"
        existing_facts = load_jsonl(path)
        events = []
        merged, dup_count, downgrade_count = deduplicate_facts([record], existing_facts, events)
        log_history(memory_dir, events)
        if not merged:
            # 旧记忆可能已被新内容更新,回写后重建索引
            save_jsonl(path, existing_facts)
            rebuild_index(memory_dir)
            print("ℹ️ 与已有记忆重复,已合并,未新增")
            return
        if downgrade_count:
            save_jsonl(path, existing_facts + merged)
            archive_stale_memories(memory_dir)
            print(f"⚠️ 检测到修正,已降权 {downgrade_count} 条旧记忆")
        else:
            append_jsonl(path, record)
    else:
        path = memory_dir / f"layer2/active/{mem_type}s.jsonl"
        append_jsonl(path, record)
        log_history(memory_dir, [{"op": "ADD", "id": record["id"], "content": record["content"]}])

    # 立即更新索引,不必等下一次 consolidate 才能检索到
    rebuild_index(memory_dir)

    print(f"✅ 记忆已添加: {record['id']}")
    print(f"   类型: {mem_type}")
    print(f"   重要性: {importance}")
    print(f"   内容: {content[:50]}...")


def cmd_archive(args):
    """手动归档记忆"""
    memory_dir = get_memory_dir()
    memory_id = args.id

    # 在活跃池中查找
    for mem_type in ["facts", "beliefs", "summaries"]:
        active_path = memory_dir / f"layer2/active/{mem_type}.jsonl"
        archive_path = memory_dir / f"layer2/archive/{mem_type}.jsonl"

        records = load_jsonl(active_path)
        found = None
        remaining = []

        for r in records:
            if r.get("id") == memory_id:
                found = r
            else:
                remaining.append(r)

        if found:
            # 保存剩余记录
            save_jsonl(active_path, remaining)
            # 追加到归档
            with open(archive_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(found, ensure_ascii=False) + "\n")
            print(f"✅ 已归档: {memory_id}")
            return

    print(f"❌ 未找到记忆: {memory_id}")


# ============================================================
# Consolidation 命令
# ============================================================


def rebuild_index(memory_dir):
    """重建关键词索引和实体关系索引(Phase 6,capture 后也会调用)"""
    keywords_index = {}
    relations_index = {}

    # 中文分词辅助函数
    def extract_keywords(text):
        """提取关键词(改进版:保留连字符词)"""
        import re

        keywords = set()

        # 1. 优先提取连字符词(memory-system, v1.1, API-key等)
        hyphen_words = re.findall(r"[a-zA-Z0-9][-a-zA-Z0-9.]+", text)
        for word in hyphen_words:
            if len(word) > 1:
                keywords.add(word.lower())

        # 2. 中文:整段 + 2-gram(v1.4.1 修复)
        #    旧版把整句连续中文当作单个索引键,导致查询"医学生"
        #    永远匹配不到索引键"用户是医学生"。
        #    现在对每段连续中文同时索引整段(支持全短语查询)和
        #    2-gram,与 keyword_search 查询端的 2-4 字子串切分保证有交集。
        # 中文紧跟字母/数字的词(如"项目B"),让单个字母也能被检索
        for mixed in re.findall(r"[\u4e00-\u9fa5]{2}[a-z0-9]+", text.lower()):
            keywords.add(mixed)

        chinese_runs = re.findall(r"[\u4e00-\u9fa5]{2,}", text)
        for run in chinese_runs:
            keywords.add(run)
            for i in range(len(run) - 1):
                keywords.add(run[i : i + 2])

        # 3. 提取纯英文单词(不含连字符的)
        english_words = re.findall(r"\b[a-zA-Z]{2,}\b", text)
        for word in english_words:
            keywords.add(word.lower())

        return keywords

    for mem_type in ["facts", "beliefs", "summaries"]:
        records = load_jsonl(memory_dir / f"layer2/active/{mem_type}.jsonl")
        for r in records:
            # 改进的关键词提取
            content = r.get("content", "")
            keywords = extract_keywords(content)
            for word in keywords:
                if word not in keywords_index:
                    keywords_index[word] = []
                if r["id"] not in keywords_index[word]:
                    keywords_index[word].append(r["id"])

            # 实体关系
            for entity in r.get("entities", []):
                if entity not in relations_index:
                    relations_index[entity] = {"facts": [], "beliefs": [], "summaries": []}
                relations_index[entity][mem_type].append(r["id"])

    with open(memory_dir / "layer2/index/keywords.json", "w", encoding="utf-8") as f:
        json.dump(keywords_index, f, indent=2, ensure_ascii=False)
    with open(memory_dir / "layer2/index/relations.json", "w", encoding="utf-8") as f:
        json.dump(relations_index, f, indent=2, ensure_ascii=False)

    # 语义向量(可选):只为新增或改写的记忆计算
    store = get_vector_store(memory_dir)
    if store:
        contents = [r.get("content", "") for r in _load_all_active_records(memory_dir).values()]
        try:
            store.prune(contents)
            store.ensure(contents)
            store.save()
        except Exception as e:
            print(f"⚠️ 语义向量更新失败,下次再试: {e}", file=sys.stderr)


def cmd_consolidate(args):
    """执行 Consolidation 流程"""
    memory_dir = get_memory_dir()

    if not memory_dir.exists():
        print("❌ 记忆系统未初始化,请先运行: memory.py init")
        return

    config = get_config()

    # 检查是否需要执行
    state_path = memory_dir / "state/consolidation.json"
    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)

    if not args.force and state.get("last_success"):
        last_success = datetime.fromisoformat(state["last_success"].replace("Z", "+00:00"))
        hours_since = (_utcnow() - last_success.replace(tzinfo=None)).total_seconds() / 3600
        fallback_hours = config["consolidation"]["fallback_hours"]

        if hours_since < 20:  # 至少间隔 20 小时
            print(f"⏭️ 跳过: 距离上次成功仅 {hours_since:.1f} 小时")
            print("   使用 --force 强制执行")
            return

    print("🧠 开始 Consolidation...")
    print("=" * 40)

    # 更新状态
    state["last_run"] = now_iso()
    state["current_phase"] = 0
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    try:
        # 用于存储中间结果
        phase_data = state.get("phase_data", {})

        # Phase 0: 过期/已失效记忆移入归档池
        if not args.phase or args.phase == 0:
            print("\n🗑️ Phase 0: 清理过期记忆")
            expired_count = archive_stale_memories(memory_dir)
            print(f"   归档 {expired_count} 条过期/失效记忆")
            print("   ✅ 完成")

        # Phase 1: 轻量全量(切分片段)
        if not args.phase or args.phase == 1:
            print("\n📋 Phase 1: 轻量全量(切分片段)")
            segments = []
            if args.input:
                with open(args.input, encoding="utf-8") as f:
                    raw_text = f.read()
                for line in raw_text.split("\n"):
                    line = line.strip()
                    if line:
                        segments.append({"content": line, "source": args.input})
                print(f"   从文件读取 {len(segments)} 个片段")
            else:
                # 自动读取 pending.jsonl
                pending = load_pending(memory_dir)
                if pending:
                    for item in pending:
                        content = item.get("content", "").strip()
                        if content:
                            segments.append({
                                "content": content,
                                "source": item.get("source", "user"),
                                "created": item.get("created", ""),
                                "session": item.get("session", ""),
                            })
                    print(f"   从 pending.jsonl 读取 {len(segments)} 个片段")
                    # 清空 pending（已处理）
                    save_pending(memory_dir, [])
                    print("   pending.jsonl 已清空")
                else:
                    print("   [跳过] pending.jsonl 为空")
            phase_data["segments"] = segments
            print("   ✅ 完成")

        # Phase 2: 重要性筛选
        if not args.phase or args.phase == 2:
            print("\n🎯 Phase 2: 重要性筛选")
            segments = phase_data.get("segments", [])
            if segments:
                filtered = rule_filter(segments, threshold=0.3)
                phase_data["filtered"] = filtered
                print(f"   输入: {len(segments)} 片段")
                print(f"   筛选后: {len(filtered)} 片段 (threshold=0.3)")
                for f in filtered[:3]:
                    print(f"     - [{f['importance']:.1f}] {f['content'][:40]}...")
            else:
                phase_data["filtered"] = []
                print("   [跳过] 无输入片段")
            print("   ✅ 完成")

        # Phase 3: 深度提取
        if not args.phase or args.phase == 3:
            print("\n📝 Phase 3: 深度提取")
            filtered = phase_data.get("filtered", [])
            if filtered:
                extracted = template_extract(filtered)
                phase_data["extracted"] = extracted
                print("   提取结果:")
                print(f"     - Facts: {len(extracted['facts'])}")
                print(f"     - Beliefs: {len(extracted['beliefs'])}")
                print(f"     - Summaries: {len(extracted['summaries'])}")
            else:
                phase_data["extracted"] = {"facts": [], "beliefs": [], "summaries": []}
                print("   [跳过] 无筛选片段")
            print("   ✅ 完成")

        # Phase 4: Layer 2 维护
        if not args.phase or args.phase == 4:
            print("\n🔧 Phase 4: Layer 2 维护")
            extracted = phase_data.get("extracted", {"facts": [], "beliefs": [], "summaries": []})

            # 加载现有记忆
            existing_facts = load_jsonl(memory_dir / "layer2/active/facts.jsonl")
            existing_beliefs = load_jsonl(memory_dir / "layer2/active/beliefs.jsonl")
            existing_summaries = load_jsonl(memory_dir / "layer2/active/summaries.jsonl")

            # 4a: Facts 去重合并
            print("   4a: Facts 去重合并 + 冲突检测")
            new_facts = extracted.get("facts", [])
            if new_facts:
                events = []
                merged_facts, dup_count, downgrade_count = deduplicate_facts(new_facts, existing_facts, events)
                print(f"       新增: {len(merged_facts)}, 去重: {dup_count}, 降权: {downgrade_count}")
                if dup_count or downgrade_count:
                    # 旧 facts 被原地合并/降权,与新 facts 一起整体重写
                    save_jsonl(memory_dir / "layer2/active/facts.jsonl", existing_facts + merged_facts)
                else:
                    for fact in merged_facts:
                        append_jsonl(memory_dir / "layer2/active/facts.jsonl", fact)
                log_history(memory_dir, events)
                if downgrade_count:
                    archive_stale_memories(memory_dir)
            else:
                print("       [跳过] 无新 facts")

            # 4b: Beliefs 验证
            print("   4b: Beliefs 验证")
            new_beliefs = extracted.get("beliefs", [])
            all_facts = existing_facts + extracted.get("facts", [])
            confirmed_count = 0
            contradicted_count = 0

            for belief in new_beliefs:
                status, updated = code_verify_belief(belief, all_facts)
                if status == "confirmed":
                    # 升级为 fact
                    append_jsonl(memory_dir / "layer2/active/facts.jsonl", updated)
                    confirmed_count += 1
                elif status == "contradicted":
                    # 降低置信度后保存
                    append_jsonl(memory_dir / "layer2/active/beliefs.jsonl", updated)
                    contradicted_count += 1
                else:
                    # 保持不变
                    append_jsonl(memory_dir / "layer2/active/beliefs.jsonl", belief)

            print(f"       证实→升级: {confirmed_count}, 矛盾→降权: {contradicted_count}")

            # 4c: Summaries 生成
            print("   4c: Summaries 生成")
            all_facts_now = load_jsonl(memory_dir / "layer2/active/facts.jsonl")
            trigger_count = config["thresholds"].get("summary_trigger", 3)
            new_summaries = generate_summaries(all_facts_now, existing_summaries, trigger_count)
            if new_summaries:
                for summary in new_summaries:
                    append_jsonl(memory_dir / "layer2/active/summaries.jsonl", summary)
                print(f"       生成: {len(new_summaries)} 条新摘要")
            else:
                print("       [跳过] 无需生成摘要")

            # 4d: Entities 更新
            print("   4d: Entities 更新")
            all_facts_final = load_jsonl(memory_dir / "layer2/active/facts.jsonl")
            all_beliefs_final = load_jsonl(memory_dir / "layer2/active/beliefs.jsonl")
            all_summaries_final = load_jsonl(memory_dir / "layer2/active/summaries.jsonl")
            entity_count = update_entities(all_facts_final, all_beliefs_final, all_summaries_final, memory_dir)
            print(f"       更新: {entity_count} 个实体档案")

            print("   ✅ 完成")

        # Phase 5: 权重更新
        if not args.phase or args.phase == 5:
            print("\n⚖️ Phase 5: 权重更新")
            decay_rates = config["decay_rates"]
            archive_threshold = config["thresholds"]["archive"]

            # 5a: 应用访问加成(v1.1.5 已在 v1_1_helpers.calculate_access_boost 中修复)
            if V1_1_ENABLED:
                print("   5a: 应用访问加成")
                for mem_type in ["facts", "beliefs", "summaries"]:
                    active_path = memory_dir / f"layer2/active/{mem_type}.jsonl"
                    records = load_jsonl(active_path)
                    records = phase5_rank_with_access_boost(records)
                    save_jsonl(active_path, records)
                print("   ✅ 访问加成完成")

            # 5b: v1.1.5 清理废弃的学习实体
            if V1_1_5_ENABLED:
                print("   5b: 清理废弃学习实体")
                from v1_1_5_entity_system import cleanup_learned_entities

                cleanup_stats = cleanup_learned_entities(memory_dir)
                print(f"       清理: {cleanup_stats['exact_removed']} 实体, {cleanup_stats['patterns_removed']} 模式")
                print(
                    f"       保留: {cleanup_stats['exact_remaining']} 实体, {cleanup_stats['patterns_remaining']} 模式"
                )

            # 5c: 衰减(v1.4.1: 时间驱动的指数衰减,含访问保护)
            #
            # score *= e^(-effective_rate × days)
            #   - days = 距离衰减时钟参考点的天数,参考点取
            #     max(last_decayed, last_accessed, created):
            #     · last_decayed 保证多次运行不重复扣减(时间驱动而非次数驱动)
            #     · last_accessed 提供访问保护——最近被访问的记忆衰减时钟重置
            #   - effective_rate = decay_rate × (1 - importance × 0.5)
            #     importance=1.0 的记忆衰减率减半(重要性保护)
            # 半衰期(importance=0.5 时): fact≈116天 / belief≈13天 / summary≈37天
            print("   5c: 衰减更新")
            import math as _math

            archived_count = 0
            _now_dt = _utcnow()

            def _parse_ts(ts):
                if not ts:
                    return None
                try:
                    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)
                except (ValueError, TypeError):
                    return None

            for mem_type in ["facts", "beliefs", "summaries"]:
                active_path = memory_dir / f"layer2/active/{mem_type}.jsonl"
                archive_path = memory_dir / f"layer2/archive/{mem_type}.jsonl"

                records = load_jsonl(active_path)

                decay_rate = decay_rates.get(mem_type.rstrip("s"), 0.01)
                for r in records:
                    candidates = [
                        _parse_ts(r.get("last_decayed")),
                        _parse_ts(r.get("last_accessed")),
                        _parse_ts(r.get("created")),
                    ]
                    candidates = [c for c in candidates if c is not None]
                    if candidates:
                        ref_dt = max(candidates)
                        days = max(0.0, (_now_dt - ref_dt).total_seconds() / 86400)
                    else:
                        days = 0.0  # 无任何时间戳:只打点,不衰减

                    importance = r.get("importance", 0.5)
                    effective_rate = decay_rate * (1 - importance * 0.5)
                    r["score"] = r.get("score", importance) * _math.exp(-effective_rate * days)
                    r["last_decayed"] = now_iso()

                remaining = []
                to_archive = []

                for r in records:
                    if r.get("score", 0) < archive_threshold:
                        to_archive.append(r)
                        archived_count += 1
                    else:
                        remaining.append(r)

                save_jsonl(active_path, remaining)
                if to_archive:
                    existing = load_jsonl(archive_path)
                    save_jsonl(archive_path, existing + to_archive)

            print(f"   衰减完成,归档 {archived_count} 条")
            print("   ✅ 完成")

        # Phase 6: 索引更新
        if not args.phase or args.phase == 6:
            print("\n📇 Phase 6: 索引更新")
            rebuild_index(memory_dir)

            print("   ✅ 完成")

        # v1.2.1: Phase 6.5 - QMD 索引更新
        if not args.phase or args.phase in [6, 7]:
            if qmd_available(memory_dir):
                print("\n🔍 Phase 6.5: QMD 索引更新")
                try:
                    export_for_qmd(memory_dir)
                    # 读取 meta.json 显示统计
                    meta_path = memory_dir / ".qmd/meta.json"
                    if meta_path.exists():
                        with open(meta_path) as f:
                            meta = json.load(f)
                        print(f"   记忆数: {meta.get('count', 0)}")
                    print("   ✅ 完成")
                except Exception as e:
                    print(f"   ⚠️ QMD 更新失败: {e}")
                    print("   继续使用基础索引...")

        # Phase 6.8: 主动记忆引擎更新(v1.4.0 新增)
        if PROACTIVE_ENABLED and (not args.phase or args.phase in [6, 7]):
            try:
                print("\n🤖 Phase 6.8: 主动记忆引擎更新")
                proactive_engine = create_engine(memory_dir)

                # 用最新的 facts 喂给引擎,更新意图状态
                recent_facts = load_jsonl(memory_dir / "layer2/active/facts.jsonl")
                recent_facts.sort(key=lambda x: x.get("created", ""), reverse=True)

                fed_count = 0
                for fact in recent_facts[:20]:  # 只取最新 20 条
                    proactive_engine.process_message(fact.get("content", ""), role="user")
                    fed_count += 1

                # 保存引擎状态
                proactive_engine.save_state()

                # 获取主动建议
                suggestion = proactive_engine.get_next_suggestion()
                stats = proactive_engine.get_stats()
                print(f"   喂入记忆: {fed_count} 条")
                print(f"   意图检测: {stats.get('intents_detected', 0)} 个")
                print(f"   主动建议: {stats.get('suggestions_generated', 0)} 条")
                if suggestion:
                    print(f"   最新建议: [{suggestion.type}] {suggestion.content[:50]}...")
                print("   ✅ 完成")
            except Exception as e:
                print(f"   ⚠️ 主动记忆引擎更新失败: {e}")

        # Phase 6.9: 过时扫描 (v1.5.0 新增)
        if not args.phase or args.phase in [6, 7]:
            print("\n🔍 Phase 6.9: 过时扫描")

            stale_days = config.get("memory", {}).get("stale_days", 30)  # 默认 30 天
            now = _utcnow()
            stale_count = 0
            updated_verified = 0

            for mem_type in ["facts", "beliefs", "summaries"]:
                path = memory_dir / f"layer2/active/{mem_type}.jsonl"
                records = load_jsonl(path)
                updated_records = []

                for r in records:
                    # 获取 last_verified 或 created
                    verified_str = r.get("last_verified") or r.get("created", "")
                    if not verified_str:
                        # 没有任何时间信息，设置为当前时间
                        r["last_verified"] = now_iso()
                        updated_verified += 1
                        updated_records.append(r)
                        continue

                    try:
                        # 解析时间
                        verified_str = verified_str.replace("Z", "+00:00")
                        verified_date = datetime.fromisoformat(verified_str)
                        if verified_date.tzinfo:
                            verified_date = verified_date.replace(tzinfo=None)
                        days_since = (now - verified_date).days

                        if days_since > stale_days:
                            # 标记为过时（不更新 last_verified，让天数继续增长）
                            r["stale"] = True
                            r["stale_days"] = days_since
                            stale_count += 1
                        else:
                            # 非过时：清除标记，更新 last_verified
                            r.pop("stale", None)
                            r.pop("stale_days", None)
                            r["last_verified"] = now_iso()
                            updated_verified += 1
                    except Exception:
                        # 时间解析失败，设置当前时间
                        r["last_verified"] = now_iso()
                        updated_verified += 1

                    updated_records.append(r)

                # 保存更新后的记录
                save_jsonl(path, updated_records)

            print(f"   过时标记: {stale_count} 条 (>{stale_days}天未验证)")
            print(f"   新增验证时间: {updated_verified} 条")
            print("   ✅ 完成")

        # Phase 7: Layer 1 快照
        if not args.phase or args.phase == 7:
            print("\n📸 Phase 7: Layer 1 快照")

            # 收集所有活跃记忆并排序
            all_records = []
            for mem_type in ["facts", "beliefs", "summaries"]:
                records = load_jsonl(memory_dir / f"layer2/active/{mem_type}.jsonl")
                for r in records:
                    r["_type"] = mem_type
                all_records.extend(records)

            # 按 score 排序
            all_records.sort(key=lambda x: x.get("score", 0), reverse=True)

            # 统计各类型数量
            facts_count = len([r for r in all_records if r["_type"] == "facts"])
            beliefs_count = len([r for r in all_records if r["_type"] == "beliefs"])
            summaries_count = len([r for r in all_records if r["_type"] == "summaries"])

            # 按重要性分组
            critical = [r for r in all_records if r.get("importance", 0) >= 0.9]
            high = [r for r in all_records if 0.7 <= r.get("importance", 0) < 0.9]
            downgraded = [r for r in all_records if r.get("conflict_downgraded", False)]

            # 提取实体统计
            all_entities = set()
            for r in all_records:
                all_entities.update(r.get("entities", []))

            # 生成增强版快照
            snapshot = f"""# 工作记忆快照
> 生成时间: {now_iso()} | 活跃记忆: {len(all_records)} | 实体: {len(all_entities)}

---

## 🔴 关键信息 (importance ≥ 0.9)
"""
            for r in critical[:5]:
                snapshot += f"- **{r.get('content', '')}**\n"
            if not critical:
                snapshot += "- (无)\n"

            snapshot += """
## 🟠 重要信息 (importance 0.7-0.9)
"""
            for r in high[:5]:
                snapshot += f"- {r.get('content', '')}\n"
            if not high:
                snapshot += "- (无)\n"

            # 新增:降权记忆标注
            if downgraded:
                snapshot += """
## 📉 已降权记忆 (冲突覆盖)
"""
                for r in downgraded[:5]:
                    content = r.get("content", "")[:40]
                    old_score = r.get("importance", 0.5)
                    new_score = r.get("score", 0)
                    snapshot += f"- ~~{content}~~ (Score: {old_score:.2f} → {new_score:.2f})\n"

            snapshot += """
## 📊 记忆排名 (Top 15)
| # | Score | 内容 |
|---|-------|------|
"""
            for i, r in enumerate(all_records[:15]):
                score = r.get("score", 0)
                content_text = r.get("content", "")[:40]
                mem_type = r["_type"][0].upper()  # F/B/S
                snapshot += f"| {i + 1} | {score:.2f} | [{mem_type}] {content_text} |\n"

            snapshot += """
## 🏷️ 实体索引
"""
            for entity in sorted(all_entities)[:10]:
                related = len([r for r in all_records if entity in r.get("entities", [])])
                snapshot += f"- **{entity}**: {related} 条相关记忆\n"

            snapshot += f"""
## 📈 统计概览
- **Facts**: {facts_count} 条 ({facts_count * 100 // max(len(all_records), 1)}%)
- **Beliefs**: {beliefs_count} 条 ({beliefs_count * 100 // max(len(all_records), 1)}%)
- **Summaries**: {summaries_count} 条 ({summaries_count * 100 // max(len(all_records), 1)}%)
- **关键信息**: {len(critical)} 条
- **重要信息**: {len(high)} 条

---
*Memory System v1.0 | 使用 memory_search 检索详细信息*
"""

            with open(memory_dir / "layer1/snapshot.md", "w", encoding="utf-8") as f:
                f.write(snapshot)

            # 保存排名
            rankings = [{"id": r["id"], "score": r.get("score", 0)} for r in all_records[:50]]
            with open(memory_dir / "state/rankings.json", "w", encoding="utf-8") as f:
                json.dump({"updated": now_iso(), "rankings": rankings}, f, indent=2, ensure_ascii=False)

            print("   ✅ 完成")

        # 更新成功状态
        state["last_success"] = now_iso()
        state["current_phase"] = None
        state["retry_count"] = 0
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

        print("\n" + "=" * 40)
        print("✅ Consolidation 完成!")

        # v1.5.0: Soul Health Report
        try:
            from soul_health import compute_soul_score, print_soul_report, save_soul_history, print_soul_trend
            soul_report = compute_soul_score(memory_dir)
            save_soul_history(soul_report, memory_dir)
            print_soul_report(soul_report)
            print_soul_trend(memory_dir)
        except ImportError:
            pass  # soul_health 未随仓库开源,缺失时静默跳过
        except Exception as e:
            print(f"\n⚠️  Soul Health 计算失败: {e}")

        # v1.1.7: 显示智能 LLM 集成统计
        if V1_1_7_ENABLED:
            summary = INTEGRATION_STATS.summary()
            total_calls = summary["phase2"]["calls"] + summary["phase3"]["calls"]
            if total_calls > 0:
                INTEGRATION_STATS.print_summary()
            else:
                print("\n💰 Token 节省: 100% (纯规则处理,无 LLM 调用)")
        # 回退到原有统计(v1.1.6 及之前)
        elif LLM_STATS["phase2_calls"] > 0 or LLM_STATS["phase3_calls"] > 0:
            print("\n📊 LLM 调用统计:")
            print(f"   Phase 2 (筛选): {LLM_STATS['phase2_calls']} 次")
            print(f"   Phase 3 (提取): {LLM_STATS['phase3_calls']} 次")
            print(f"   总 Token: {LLM_STATS['total_tokens']}")
            if LLM_STATS["errors"] > 0:
                print(f"   ⚠️  错误: {LLM_STATS['errors']} 次")
        else:
            print("\n💰 Token 节省: 100% (纯规则处理,无 LLM 调用)")

    except Exception as e:
        state["retry_count"] = state.get("retry_count", 0) + 1
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        print(f"\n❌ Consolidation 失败: {e}")
        raise


# ============================================================
# 维护命令
# ============================================================


def cmd_rebuild_index(args):
    """重建索引"""
    memory_dir = get_memory_dir()
    print("🔄 重建索引...")

    # 调用 Phase 6 逻辑
    args.phase = 6
    args.force = True
    cmd_consolidate(args)


# ============================================================
# v1.2.0 动态注入命令
# ============================================================


def cmd_export_qmd(args):
    """导出记忆为 QMD 索引格式"""
    memory_dir = get_memory_dir()

    if not memory_dir.exists():
        print("❌ 记忆系统未初始化")
        return

    print("📤 导出记忆到 QMD 索引格式...")
    qmd_index_dir = export_for_qmd(memory_dir)

    print(f"✅ 导出完成: {qmd_index_dir}")
    for f in qmd_index_dir.glob("*.md"):
        print(f"   - {f.name}")

    # v1.2.3: --auto-reload 自动执行 qmd 命令
    if getattr(args, "auto_reload", False):
        if qmd_available(memory_dir):
            print()
            print("🔄 自动更新 QMD 索引...")
            try:
                env = _get_qmd_env()
                # 添加到 collection(如果已存在则跳过)
                result1 = subprocess.run(
                    ["qmd", "collection", "add", str(qmd_index_dir), "--name", "curated", "--mask", "*.md"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env,
                )
                if result1.returncode != 0:
                    if "already exists" in result1.stderr:
                        print("   ℹ️ collection 'curated' 已存在,跳过添加")
                    else:
                        print(f"   ⚠️ collection add 失败: {result1.stderr.strip()}")
                else:
                    print("   ✅ collection add 完成")

                # 更新索引
                result2 = subprocess.run(["qmd", "update"], capture_output=True, text=True, timeout=60, env=env)
                if result2.returncode != 0:
                    print(f"   ⚠️ update 失败: {result2.stderr.strip()}")
                else:
                    print("   ✅ qmd update 完成")

                print("🎉 QMD 索引已自动更新")
            except subprocess.TimeoutExpired:
                print("   ⚠️ 命令超时")
            except Exception as e:
                print(f"   ⚠️ 执行失败: {e}")
        else:
            print()
            print("⚠️ QMD 不可用,跳过自动更新")
            print("💡 手动运行以下命令:")
            print(f"   qmd collection add {qmd_index_dir} --name curated --mask '*.md'")
            print("   qmd update")
    else:
        # 提示更新 QMD 索引
        print()
        print("💡 运行以下命令更新 QMD 索引:")
        print(f"   qmd collection add {qmd_index_dir} --name curated --mask '*.md'")
        print("   qmd update")
        print()
        print("   或使用 --auto-reload 自动执行:")
        print("   memory.py export-qmd --auto-reload")


def cmd_inject(args):
    """
    动态注入:根据用户消息检索相关记忆,输出可直接注入 prompt 的内容

    用法:
        memory.py inject "用户消息" [--max-tokens 500] [--format text|json]

    输出格式(text):
        ## 相关记忆
        - [fact] 用户名字是Ktao...
        - [belief] Ktao认为记忆系统很重要...

    输出格式(json):
        {"direct": [...], "marked": [...], "reference": [...]}
    """
    memory_dir = get_memory_dir()

    if not memory_dir.exists():
        if args.format == "json":
            print('{"error": "记忆系统未初始化"}')
        else:
            print("# 无相关记忆")
        return

    query = args.query
    max_tokens = args.max_tokens

    # 调用 router_search 检索
    result = router_search(query, memory_dir)

    if not result.get("results"):
        if args.format == "json":
            print('{"direct": [], "marked": [], "reference": []}')
        else:
            print("# 无相关记忆")
        return

    # 格式化输出
    injection = result.get("injection", format_injection(result["results"]))

    if args.format == "json":
        print(json.dumps(injection, ensure_ascii=False, indent=2))
    else:
        # 文本格式,适合直接注入 prompt
        lines = []

        # 直接注入的高置信度记忆
        if injection.get("direct"):
            lines.append("## 相关记忆")
            for item in injection["direct"][:5]:  # 最多5条
                type_tag = item.get("type", "fact")[0].upper()
                content = item["content"][:200]  # 截断
                lines.append(f"- [{type_tag}] {content}")

        # 带标记的中置信度记忆
        if injection.get("marked"):
            if not lines:
                lines.append("## 可能相关")
            for item in injection["marked"][:3]:  # 最多3条
                type_tag = item.get("type", "fact")[0].upper()
                content = item["content"][:150]
                source = item.get("source", "unknown")
                lines.append(f"- [{type_tag}] {content} (ref:{source})")

        # 控制总 token 数(粗略估计:1中文字≈1.5token)
        output = "\n".join(lines)
        estimated_tokens = len(output) * 1.5
        if estimated_tokens > max_tokens:
            # 截断
            char_limit = int(max_tokens / 1.5)
            output = output[:char_limit] + "\n..."

        print(output if output else "# 无相关记忆")


def cmd_validate(args):
    """验证数据完整性"""
    memory_dir = get_memory_dir()
    print("🔍 验证数据完整性...")

    errors = []

    # 检查目录结构
    required_dirs = ["layer1", "layer2/active", "layer2/archive", "layer2/entities", "layer2/index", "state"]
    for d in required_dirs:
        if not (memory_dir / d).exists():
            errors.append(f"缺少目录: {d}")

    # 检查 JSONL 文件格式
    for mem_type in ["facts", "beliefs", "summaries"]:
        for pool in ["active", "archive"]:
            path = memory_dir / f"layer2/{pool}/{mem_type}.jsonl"
            if path.exists():
                try:
                    records = load_jsonl(path)
                    for i, r in enumerate(records):
                        if "id" not in r:
                            errors.append(f"{path}:{i + 1} 缺少 id 字段")
                        if "content" not in r:
                            errors.append(f"{path}:{i + 1} 缺少 content 字段")
                except Exception as e:
                    errors.append(f"{path} 解析失败: {e}")

    if errors:
        print(f"❌ 发现 {len(errors)} 个问题:")
        for e in errors[:10]:
            print(f"   - {e}")
        if len(errors) > 10:
            print(f"   ... 还有 {len(errors) - 10} 个问题")
    else:
        print("✅ 数据完整性验证通过")


# ============================================================
# 主入口
# ============================================================

# ============================================================
# v1.2.2 白天轻量检查(Mini-Consolidate)
# ============================================================

# Urgent 检测规则(重要性 > 0.8 的内容)
URGENT_PATTERNS = {
    # 身份/健康/安全相关 - 最高优先级
    "critical": {
        "keywords": ["过敏", "疾病", "死", "生命", "紧急", "危险", "急救", "密码", "账号", "银行卡", "身份证"],
        "threshold": 0.9,
    },
    # 重要事件/决策
    "important": {
        "keywords": ["记住", "永远记住", "一定要记住", "重要", "关键", "决定", "确定", "最终"],
        "threshold": 0.8,
    },
    # 时间敏感(带明确时间点)
    "time_sensitive": {
        "patterns": [
            r"(今天|明天|后天|下周|下个月).*(必须|一定|截止|deadline)",
            r"(必须|一定).*(今天|明天|后天|下周)",
        ],
        "threshold": 0.8,
    },
}


def check_urgency(content: str) -> tuple[bool, float, str]:
    """
    检测内容是否为 urgent(需要优先处理)

    返回:
        (is_urgent, importance_score, matched_category)
    """
    content_lower = content.lower()

    # 检查关键词规则
    for category, rule in URGENT_PATTERNS.items():
        threshold = rule.get("threshold", 0.8)

        # 关键词匹配
        if "keywords" in rule:
            for keyword in rule["keywords"]:
                if keyword in content:
                    return True, threshold, category

        # 正则匹配
        if "patterns" in rule:
            for pattern in rule["patterns"]:
                if re.search(pattern, content):
                    return True, threshold, category

    return False, 0.5, ""


def load_pending(memory_dir) -> list:
    """加载 pending buffer"""
    pending_path = Path(memory_dir) / "layer2/pending.jsonl"
    if pending_path.exists():
        return load_jsonl(pending_path)
    return []


def save_pending(memory_dir, records: list):
    """保存 pending buffer"""
    pending_path = Path(memory_dir) / "layer2/pending.jsonl"
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    save_jsonl(pending_path, records)


def add_to_pending(memory_dir, content: str, source: str = "user") -> dict:
    """
    添加内容到 pending buffer

    返回添加的记录
    """
    is_urgent, importance, category = check_urgency(content)

    record = {
        "id": f"p_{_utcnow().strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}",
        "content": content,
        "source": source,
        "created": now_iso(),
        "urgent": is_urgent,
        "importance": importance,
        "category": category,
    }

    pending = load_pending(memory_dir)
    pending.append(record)
    save_pending(memory_dir, pending)

    return record


def search_pending(query: str, memory_dir=None) -> list:
    """
    搜索 pending buffer(Hot Store)
    简单关键词匹配,用于 router_search 的第一优先级
    """
    if memory_dir is None:
        memory_dir = get_memory_dir()

    pending = load_pending(memory_dir)
    if not pending:
        return []

    results = []
    query_lower = query.lower()
    query_words = set(re.findall(r"[\u4e00-\u9fa5]+|[a-zA-Z]+", query_lower))

    for record in pending:
        content_lower = record.get("content", "").lower()
        # 简单匹配:查询词出现在内容中
        score = 0
        for word in query_words:
            if word in content_lower:
                score += 1

        if score > 0:
            record_copy = record.copy()
            # 补充 router_search 需要的字段
            record_copy["type"] = "pending"
            record_copy["score"] = record.get("importance", 0.5)
            record_copy["final_score"] = record.get("importance", 0.5)
            record_copy["match_score"] = score / len(query_words) if query_words else 0
            record_copy["match_source"] = "pending"
            record_copy["entities"] = []
            results.append(record_copy)

    # 按匹配分数排序
    results.sort(key=lambda x: x["match_score"], reverse=True)
    return results


# ============================================================
# v1.5.0 健康度仪表盘
# ============================================================


def cmd_health_index(args):
    """生成 INDEX.md 健康度仪表盘"""
    memory_dir = get_memory_dir()
    cfg = get_config()
    stale_days = cfg.get("memory", {}).get("stale_days", 30)

    print("📊 生成健康度仪表盘...")

    # 收集所有记忆
    all_memories = []
    for mem_type in ["facts", "beliefs", "summaries"]:
        records = load_jsonl(memory_dir / f"layer2/active/{mem_type}.jsonl")
        for r in records:
            r["_type"] = mem_type
            all_memories.append(r)

    # 统计
    now = _utcnow()
    stats = {
        "total": len(all_memories),
        "active": 0,
        "stale": 0,
        "high_priority": 0,
        "conflict": 0,
    }

    # 分类记忆
    categories = {"facts": [], "beliefs": [], "summaries": []}
    for r in all_memories:
        mem_type = r.get("_type", "facts")

        # 计算状态
        verified_str = r.get("last_verified") or r.get("created", "")
        is_stale = False
        days_since = 0

        if verified_str:
            try:
                verified_str = verified_str.replace("Z", "+00:00")
                verified_date = datetime.fromisoformat(verified_str)
                if verified_date.tzinfo:
                    verified_date = verified_date.replace(tzinfo=None)
                days_since = (now - verified_date).days
                is_stale = days_since > stale_days
            except Exception:
                pass

        # 优先级
        importance = r.get("importance", 0.5)
        if importance >= 0.8:
            priority = "🔴"
            stats["high_priority"] += 1
        elif importance >= 0.5:
            priority = "🟡"
        else:
            priority = "⚪"

        # 状态
        if r.get("conflict_downgraded"):
            status = "🔀"
            stats["conflict"] += 1
        elif is_stale:
            status = "⚠️"
            stats["stale"] += 1
        else:
            status = "✅"
            stats["active"] += 1

        # 添加到分类
        categories[mem_type].append({
            "id": r.get("id", "?")[:12],
            "content": r.get("content", "")[:50],
            "priority": priority,
            "status": status,
            "days_since": days_since,
            "importance": importance,
        })

    # 生成 INDEX.md
    index_path = memory_dir / "INDEX.md"
    lines = [
        "# 记忆系统健康度仪表盘",
        "",
        f"> 生成时间: {now_iso()}",
        "",
        "## 📈 总览",
        "",
        f"| 指标 | 数量 |",
        f"|------|------|",
        f"| 总记忆数 | {stats['total']} |",
        f"| ✅ 活跃 | {stats['active']} |",
        f"| ⚠️ 过时 (>{stale_days}天) | {stats['stale']} |",
        f"| 🔴 高优先级 | {stats['high_priority']} |",
        f"| 🔀 冲突 | {stats['conflict']} |",
        "",
    ]

    # 状态标记说明
    lines.extend([
        "## 📋 状态标记说明",
        "",
        "| 标记 | 含义 |",
        "|------|------|",
        "| 🔴 | 高优先级 (importance ≥ 0.8) |",
        "| 🟡 | 中优先级 (0.5 ≤ importance < 0.8) |",
        "| ⚪ | 低优先级 (importance < 0.5) |",
        "| ✅ | 已验证 (30天内) |",
        "| ⚠️ | 过时 (>" + str(stale_days) + "天未验证) |",
        "| 🔀 | 存在冲突 |",
        "",
    ])

    # Facts 列表
    if categories["facts"]:
        lines.extend([
            "## Facts",
            "",
            "| ID | 内容 | 优先级 | 状态 | 天数 |",
            "|----|----|--------|------|------|",
        ])
        for item in sorted(categories["facts"], key=lambda x: -x["importance"])[:20]:
            content = item["content"][:40].replace("\n", " ").replace("|", "\\|")
            lines.append(f"| {item['id']} | {content}... | {item['priority']} | {item['status']} | {item['days_since']} |")
        lines.append("")

    # Beliefs 列表
    if categories["beliefs"]:
        lines.extend([
            "## Beliefs",
            "",
            "| ID | 内容 | 优先级 | 状态 | 天数 |",
            "|----|----|--------|------|------|",
        ])
        for item in sorted(categories["beliefs"], key=lambda x: -x["importance"])[:10]:
            content = item["content"][:40].replace("\n", " ").replace("|", "\\|")
            lines.append(f"| {item['id']} | {content}... | {item['priority']} | {item['status']} | {item['days_since']} |")
        lines.append("")

    # 写入文件
    with open(index_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"   总记忆: {stats['total']}")
    print(f"   ✅ 活跃: {stats['active']}")
    print(f"   ⚠️ 过时: {stats['stale']}")
    print(f"   🔀 冲突: {stats['conflict']}")
    print(f"✅ 已生成: {index_path}")


def cmd_mini_consolidate(args):
    """
    白天轻量检查:只处理 pending buffer

    流程:
    1. 读取 pending.jsonl
    2. Phase 2: 筛选(规则 + LLM 兜底)
    3. Phase 3: 提取(模板 + LLM)
    4. 写入 layer2/active/
    5. 清空 pending.jsonl
    6. 更新 Layer 1 快照(可选)
    """
    memory_dir = get_memory_dir()
    pending = load_pending(memory_dir)

    if not pending:
        print("📭 Pending buffer 为空,无需处理")
        return

    print("🔄 Mini-Consolidate 开始")
    print(f"   待处理: {len(pending)} 条")

    # 统计 urgent
    urgent_count = len([p for p in pending if p.get("urgent")])
    print(f"   其中 urgent: {urgent_count} 条")

    # Phase 2: 筛选
    print("\n🔍 Phase 2: 筛选")
    kept = []
    for record in pending:
        content = record.get("content", "")

        # 废话检测
        is_noise_result, noise_category = is_noise(content)
        if is_noise_result:
            print(f"   ❌ 跳过废话: {content[:30]}... ({noise_category})")
            continue

        # urgent 直接保留
        if record.get("urgent"):
            print(f"   ✅ 保留 (urgent): {content[:30]}...")
            kept.append(record)
            continue

        # 规则筛选
        importance, category = calculate_importance(content)
        if importance >= 0.5:
            record["importance"] = importance
            record["category"] = category
            print(f"   ✅ 保留 (importance={importance:.1f}): {content[:30]}...")
            kept.append(record)
        else:
            print(f"   ❌ 跳过 (importance={importance:.1f}): {content[:30]}...")

    print(f"   筛选后: {len(kept)} 条")

    if not kept:
        # 清空 pending
        save_pending(memory_dir, [])
        print("\n✅ Mini-Consolidate 完成(无有效内容)")
        return

    # Phase 3: 提取
    print("\n📝 Phase 3: 提取")
    extracted = []
    for record in kept:
        content = record["content"]
        importance = record.get("importance", 0.5)

        # 提取实体
        entities = extract_entities(content)

        # 构建新记录
        new_record = {
            "id": f"f_{_utcnow().strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}",
            "content": content,
            "type": "fact",
            "category": record.get("category", "general"),
            "importance": importance,
            "score": importance,
            "created": record.get("created", now_iso()),
            "updated": now_iso(),
            "entities": entities,
            "source": "mini-consolidate",
        }
        extracted.append(new_record)
        print(f"   ✅ 提取: {content[:40]}...")

    # 写入 active pool
    print("\n💾 写入 active pool")
    for record in extracted:
        mem_type = record["type"] + "s"  # fact -> facts
        if mem_type not in ["facts", "beliefs", "summaries"]:
            mem_type = "facts"

        active_path = memory_dir / f"layer2/active/{mem_type}.jsonl"
        existing = load_jsonl(active_path)
        existing.append(record)
        save_jsonl(active_path, existing)

    print(f"   写入 {len(extracted)} 条记录")

    # 清空 pending
    save_pending(memory_dir, [])
    print("   清空 pending buffer")

    # 更新 QMD 索引(如果可用)
    if qmd_available(memory_dir):
        print("\n🔍 更新 QMD 索引")
        try:
            export_for_qmd(memory_dir)
            print("   ✅ 完成")
        except Exception as e:
            print(f"   ⚠️ 失败: {e}")

    print("\n✅ Mini-Consolidate 完成")
    print(f"   处理: {len(pending)} → 保留: {len(extracted)}")


def cmd_add_pending(args):
    """添加内容到 pending buffer"""
    memory_dir = get_memory_dir()
    content = args.content
    source = args.source

    record = add_to_pending(memory_dir, content, source)

    print("✅ 已添加到 pending buffer")
    print(f"   ID: {record['id']}")
    print(f"   Urgent: {record['urgent']}")
    print(f"   Importance: {record['importance']:.1f}")
    if record.get("category"):
        print(f"   Category: {record['category']}")


def cmd_view_pending(args):
    """查看 pending buffer"""
    memory_dir = get_memory_dir()
    pending = load_pending(memory_dir)

    if not pending:
        print("📭 Pending buffer 为空")
        return

    print(f"📋 Pending Buffer ({len(pending)} 条)")
    print("=" * 50)

    urgent_count = 0
    for i, record in enumerate(pending):
        is_urgent = record.get("urgent", False)
        if is_urgent:
            urgent_count += 1

        urgent_mark = "🔴" if is_urgent else "⚪"
        importance = record.get("importance", 0.5)
        content = record.get("content", "")[:50]
        created = record.get("created", "")[:16]

        print(f"{urgent_mark} [{i + 1}] {content}...")
        print(f"   importance={importance:.1f} | {created}")

    print("=" * 50)
    print(f"总计: {len(pending)} 条 | Urgent: {urgent_count} 条")


# ============================================================
# 主动记忆引擎命令
# ============================================================

_proactive_engine_instance = None


def get_proactive_engine():
    """获取主动记忆引擎单例"""
    global _proactive_engine_instance
    if _proactive_engine_instance is None and PROACTIVE_ENABLED:
        memory_dir = get_memory_dir()
        config = get_config()
        proactive_config = config.get("proactive", {})
        _proactive_engine_instance = create_engine(memory_dir=memory_dir, config=proactive_config)
    return _proactive_engine_instance


def cmd_proactive_analyze(args):
    """分析消息意图"""
    engine = get_proactive_engine()
    if not engine:
        print("❌ 主动记忆引擎不可用")
        return

    result = engine.process_message(args.message, args.role)
    intent = result.get("intent")
    suggestions = result.get("suggestions", [])
    preloaded = result.get("preloaded_memories", [])

    if args.json:
        output = {
            "intent": intent.to_dict() if intent else None,
            "suggestions": [s.to_dict() for s in suggestions],
            "preloaded_memories": preloaded,
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return

    print("🔍 意图分析结果")
    print("=" * 50)

    if intent:
        print(f"意图类型: {intent.type}")
        print(f"主题: {intent.topic}")
        print(f"置信度: {intent.confidence:.2f}")
        print(f"关键词: {', '.join(intent.keywords) if intent.keywords else '无'}")
    else:
        print("未检测到明确意图")

    if suggestions:
        print()
        print("💡 生成的建议:")
        for i, s in enumerate(suggestions):
            print(f"   {i + 1}. {s.content}")

    if preloaded:
        print()
        print(f"📚 预加载记忆: {len(preloaded)} 条")


def cmd_proactive_context(args):
    """查看当前活跃上下文"""
    engine = get_proactive_engine()
    if not engine:
        print("❌ 主动记忆引擎不可用")
        return

    context = engine.get_active_context()

    print("🧠 当前活跃上下文")
    print("=" * 50)

    intents = context.get("active_intents", [])
    if intents:
        print("活跃意图:")
        for i, intent in enumerate(intents):
            print(
                f"   {i + 1}. [{intent['type']}] {intent['topic']} (置信度: {intent['confidence']:.2f}, 消息数: {intent['message_count']})"
            )
    else:
        print("活跃意图: 无")

    print()
    recent = context.get("recent_topics", [])
    if recent:
        print("最近消息:")
        for i, msg in enumerate(recent):
            print(f"   {i + 1}. {msg}...")

    print()
    print(f"待处理建议: {context.get('pending_suggestions', 0)} 条")


def cmd_proactive_suggestions(args):
    """获取主动建议"""
    engine = get_proactive_engine()
    if not engine:
        print("❌ 主动记忆引擎不可用")
        return

    should_act, reason = engine.should_proactive_act()

    print("💡 主动建议")
    print("=" * 50)

    if not should_act:
        print("当前无需主动行动")
        print(f"原因: {reason if reason else '无触发条件'}")
        return

    print(f"触发原因: {reason}")
    print()

    suggestions = []
    for _ in range(args.limit):
        suggestion = engine.get_next_suggestion()
        if suggestion:
            suggestions.append(suggestion)
        else:
            break

    if suggestions:
        for i, s in enumerate(suggestions):
            print(f"{i + 1}. [{s.type}] {s.content}")
            print(f"   优先级: {s.priority:.2f} | 触发: {s.triggered_by}")
    else:
        print("无待处理建议")


def cmd_proactive_stats(args):
    """查看主动记忆统计"""
    engine = get_proactive_engine()
    if not engine:
        print("❌ 主动记忆引擎不可用")
        return

    stats = engine.get_stats()

    print("📊 主动记忆统计")
    print("=" * 50)
    print(f"处理消息数: {stats.get('messages_processed', 0)}")
    print(f"检测意图数: {stats.get('intents_detected', 0)}")
    print(f"生成建议数: {stats.get('suggestions_generated', 0)}")
    print(f"预加载记忆数: {stats.get('memories_preloaded', 0)}")
    print()
    print(f"当前活跃意图: {stats.get('active_intents_count', 0)}")
    print(f"待处理建议: {stats.get('pending_suggestions', 0)}")
    print(f"最近消息数: {stats.get('recent_messages', 0)}")


def cmd_proactive_reset(args):
    """重置主动记忆引擎状态"""
    global _proactive_engine_instance
    if _proactive_engine_instance:
        _proactive_engine_instance.reset()
        print("✅ 主动记忆引擎已重置")
    else:
        print("⚠️ 主动记忆引擎未初始化")


# ============================================================
# v1.6.0: 向量检索命令
# ============================================================


def cmd_vector_build(args):
    """构建向量索引"""
    memory_dir = get_memory_dir()

    if not memory_dir.exists():
        print("❌ 记忆系统未初始化")
        return

    config = get_config()
    vector_config = config.get("vector", {})

    provider = args.provider or vector_config.get("provider", "openai")
    model = args.model or vector_config.get("model")

    print("🔨 构建向量索引")
    print(f"   提供者: {provider}")
    print(f"   模型: {model or '默认'}")
    print()

    try:
        embedding_engine = get_embedding_engine(provider=provider, model=model)
        if embedding_engine is None:
            print("❌ 嵌入引擎初始化失败")
            print("   请检查 API Key 配置或安装必要的依赖")
            return

        stats = build_vector_index(
            memory_dir=memory_dir,
            embedding_engine=embedding_engine,
            backend=vector_config.get("backend", "sqlite"),
            batch_size=args.batch_size,
        )

        print()
        print("📊 索引构建统计:")
        print(f"   总记忆数: {stats['total']}")
        print(f"   新索引: {stats['indexed']}")
        print(f"   已跳过: {stats['skipped']}")
        print(f"   失败: {stats['failed']}")

        if stats["indexed"] > 0:
            vector_config["enabled"] = True
            vector_config["dimension"] = embedding_engine.dimension
            config["vector"] = vector_config
            save_config(config)
            print()
            print("✅ 向量检索已自动启用")

    except Exception as e:
        print(f"❌ 构建索引失败: {e}")


def cmd_vector_search(args):
    """向量语义检索"""
    memory_dir = get_memory_dir()

    if not memory_dir.exists():
        print("❌ 记忆系统未初始化")
        return

    config = get_config()
    vector_config = config.get("vector", {})

    if not vector_config.get("enabled", False):
        print("⚠️ 向量检索未启用")
        print("   运行 'memory.py vector-build' 构建索引并启用")
        return

    try:
        embedding_engine = get_embedding_engine(
            provider=vector_config.get("provider", "openai"),
            model=vector_config.get("model"),
        )
        if embedding_engine is None:
            print("❌ 嵌入引擎初始化失败")
            return

        hybrid_config = vector_config.get("hybrid_search", {})
        hybrid_engine = create_hybrid_search_engine(
            memory_dir=memory_dir,
            embedding_engine=embedding_engine,
            backend=vector_config.get("backend", "sqlite"),
            keyword_weight=hybrid_config.get("keyword_weight", 0.3),
            vector_weight=hybrid_config.get("vector_weight", 0.7),
            min_score=hybrid_config.get("min_score", 0.2),
        )
        if hybrid_engine is None:
            print("❌ 混合检索引擎初始化失败")
            return

        results = hybrid_engine.search(
            query=args.query,
            memory_dir=memory_dir,
            top_k=args.top_k,
            use_keyword=True,
            use_vector=True,
            memory_type=args.type,
        )

        if args.json:
            output = {
                "query": args.query,
                "results": [
                    {
                        "id": r.id,
                        "content": r.content,
                        "score": r.score,
                        "vector_score": r.vector_score,
                        "keyword_score": r.keyword_score,
                        "metadata": r.metadata,
                    }
                    for r in results
                ],
            }
            print(json.dumps(output, indent=2, ensure_ascii=False))
            return

        print(f"🔍 向量检索: {args.query}")
        print("=" * 50)
        print(f"找到 {len(results)} 条结果")
        print()

        for i, r in enumerate(results):
            print(f"{i + 1}. [{r.metadata.get('type', 'fact')[0].upper()}] {r.content[:60]}...")
            print(f"   综合分数: {r.score:.3f} (向量: {r.vector_score:.3f}, 关键词: {r.keyword_score:.3f})")

    except Exception as e:
        print(f"❌ 向量检索失败: {e}")


def cmd_vector_status(args):
    """查看向量索引状态"""
    memory_dir = get_memory_dir()

    if not memory_dir.exists():
        print("❌ 记忆系统未初始化")
        return

    config = get_config()
    vector_config = config.get("vector", {})

    print("📊 向量索引状态")
    print("=" * 50)
    print(f"启用状态: {'✅ 已启用' if vector_config.get('enabled', False) else '❌ 未启用'}")
    print(f"提供者: {vector_config.get('provider', 'openai')}")
    print(f"模型: {vector_config.get('model', 'text-embedding-3-small')}")
    print(f"维度: {vector_config.get('dimension', 1536)}")
    print(f"后端: {vector_config.get('backend', 'sqlite')}")
    print()

    hybrid_config = vector_config.get("hybrid_search", {})
    print("混合检索配置:")
    print(f"   关键词权重: {hybrid_config.get('keyword_weight', 0.3)}")
    print(f"   向量权重: {hybrid_config.get('vector_weight', 0.7)}")
    print(f"   最小分数: {hybrid_config.get('min_score', 0.2)}")
    print()

    try:
        from vector_index import VectorIndexManager

        manager = VectorIndexManager(
            memory_dir=memory_dir,
            dimension=vector_config.get("dimension", 1536),
            backend=vector_config.get("backend", "sqlite"),
        )
        vector_count = manager.get_vector_count()
        print(f"已索引向量: {vector_count} 条")
    except Exception as e:
        print(f"⚠️ 无法获取向量数量: {e}")


def cmd_vector_config(args):
    """配置向量检索参数"""
    memory_dir = get_memory_dir()

    if not memory_dir.exists():
        print("❌ 记忆系统未初始化")
        return

    config = get_config()
    vector_config = config.get("vector", {})

    updated = False

    if args.enable:
        vector_config["enabled"] = True
        print("✅ 向量检索已启用")
        updated = True

    if args.disable:
        vector_config["enabled"] = False
        print("✅ 向量检索已禁用")
        updated = True

    if args.keyword_weight is not None:
        hybrid_config = vector_config.get("hybrid_search", {})
        hybrid_config["keyword_weight"] = args.keyword_weight
        vector_config["hybrid_search"] = hybrid_config
        print(f"✅ 关键词权重已更新: {args.keyword_weight}")
        updated = True

    if args.vector_weight is not None:
        hybrid_config = vector_config.get("hybrid_search", {})
        hybrid_config["vector_weight"] = args.vector_weight
        vector_config["hybrid_search"] = hybrid_config
        print(f"✅ 向量权重已更新: {args.vector_weight}")
        updated = True

    if args.min_score is not None:
        hybrid_config = vector_config.get("hybrid_search", {})
        hybrid_config["min_score"] = args.min_score
        vector_config["hybrid_search"] = hybrid_config
        print(f"✅ 最小分数阈值已更新: {args.min_score}")
        updated = True

    if updated:
        config["vector"] = vector_config
        save_config(config)
        print()
        print("💾 配置已保存")
    else:
        print("⚠️ 未指定任何配置更改")
        print("   使用 --enable/--disable 启用/禁用向量检索")
        print("   使用 --keyword-weight/--vector-weight 调整权重")
        print("   使用 --min-score 设置最小分数阈值")


def cmd_dashboard(args):
    """启动 Web 可视化面板"""
    try:
        import webbrowser

        from dashboard.api import run_server

        host = args.host
        port = args.port

        if args.memory_dir:
            os.environ["MEMORY_DIR"] = args.memory_dir

        if not args.no_browser:
            url = f"http://{host}:{port}"
            print(f"🌐 正在打开浏览器: {url}")
            webbrowser.open(url)

        run_server(host=host, port=port)
    except ImportError as e:
        print(f"❌ 无法启动 Dashboard: {e}")
        print("   请确保安装了依赖: pip install fastapi uvicorn")
    except Exception as e:
        print(f"❌ 启动失败: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Memory System v1.0 - 三层记忆架构 CLI", formatter_class=argparse.RawDescriptionHelpFormatter
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # init
    parser_init = subparsers.add_parser("init", help="初始化记忆系统")
    parser_init.set_defaults(func=cmd_init)

    # status
    parser_status = subparsers.add_parser("status", help="显示系统状态")
    parser_status.set_defaults(func=cmd_status)

    # stats
    parser_stats = subparsers.add_parser("stats", help="显示详细统计")
    parser_stats.set_defaults(func=cmd_stats)

    # capture
    parser_capture = subparsers.add_parser("capture", help="手动添加记忆")
    parser_capture.add_argument("content", help="记忆内容")
    parser_capture.add_argument("--type", choices=["fact", "belief", "summary"], default="fact", help="记忆类型")
    parser_capture.add_argument("--importance", type=float, default=None, help="重要性 (0-1),不指定则按规则自动计算")
    parser_capture.add_argument("--confidence", type=float, default=0.6, help="置信度 (belief 专用)")
    parser_capture.add_argument("--entities", default="", help="相关实体,逗号分隔")
    parser_capture.set_defaults(func=cmd_capture)

    # archive
    parser_archive = subparsers.add_parser("archive", help="手动归档记忆")
    parser_archive.add_argument("id", help="记忆 ID")
    parser_archive.set_defaults(func=cmd_archive)

    # consolidate
    parser_consolidate = subparsers.add_parser("consolidate", help="执行 Consolidation")
    parser_consolidate.add_argument("--force", action="store_true", help="强制执行")
    parser_consolidate.add_argument("--phase", type=int, choices=[0, 1, 2, 3, 4, 5, 6, 7], help="只执行指定阶段")
    parser_consolidate.add_argument("--input", help="输入文件路径(Phase 1 数据源)")
    parser_consolidate.set_defaults(func=cmd_consolidate)

    # rebuild-index
    parser_rebuild = subparsers.add_parser("rebuild-index", help="重建索引")
    parser_rebuild.set_defaults(func=cmd_rebuild_index)

    # validate
    parser_validate = subparsers.add_parser("validate", help="验证数据完整性")
    parser_validate.set_defaults(func=cmd_validate)

    # search
    parser_search = subparsers.add_parser("search", help="智能检索记忆")
    parser_search.add_argument("query", help="检索查询")
    parser_search.add_argument("--json", action="store_true", help="只输出精简 JSON(供 Agent 解析)")
    parser_search.add_argument("--limit", type=int, default=8, help="--json 模式下返回条数")
    parser_search.set_defaults(func=cmd_search)

    # get
    parser_get = subparsers.add_parser("get", help="按 ID 取记忆全文(JSON)")
    parser_get.add_argument("ids", nargs="+", help="记忆 ID")
    parser_get.set_defaults(func=cmd_get)

    # v1.1.4 新增命令
    if V1_1_ENABLED:
        # record-access
        parser_access = subparsers.add_parser("record-access", help="记录访问日志")
        parser_access.add_argument("id", help="记忆 ID")
        parser_access.add_argument(
            "--type", choices=["retrieval", "used_in_response", "user_mentioned"], default="retrieval", help="访问类型"
        )
        parser_access.add_argument("--query", help="查询内容")
        parser_access.add_argument("--context", help="上下文")
        parser_access.set_defaults(func=lambda args: cmd_record_access(args, get_memory_dir()))

        # view-access-log
        parser_view_access = subparsers.add_parser("view-access-log", help="查看访问日志")
        parser_view_access.add_argument("--limit", type=int, default=20, help="显示条数")
        parser_view_access.set_defaults(func=lambda args: cmd_view_access_log(args, get_memory_dir()))

        # view-expired-log
        parser_view_expired = subparsers.add_parser("view-expired-log", help="查看过期记忆日志")
        parser_view_expired.add_argument("--limit", type=int, default=20, help="显示条数")
        parser_view_expired.set_defaults(func=lambda args: cmd_view_expired_log(args, get_memory_dir()))

    # v1.2.0 inject 命令
    parser_inject = subparsers.add_parser("inject", help="动态注入:根据消息检索相关记忆")
    parser_inject.add_argument("query", help="用户消息")
    parser_inject.add_argument("--max-tokens", type=int, default=500, help="最大 token 数")
    parser_inject.add_argument("--format", choices=["text", "json"], default="text", help="输出格式")
    parser_inject.set_defaults(func=cmd_inject)

    # v1.2.0 export-qmd 命令
    parser_export_qmd = subparsers.add_parser("export-qmd", help="导出记忆为 QMD 索引格式")
    parser_export_qmd.add_argument("--auto-reload", action="store_true", help="自动执行 qmd 命令更新索引")
    parser_export_qmd.set_defaults(func=cmd_export_qmd)

    # v1.5.0: 健康度仪表盘命令
    parser_health_index = subparsers.add_parser("health-index", help="生成 INDEX.md 健康度仪表盘")
    parser_health_index.set_defaults(func=cmd_health_index)

    # v1.2.2: Mini-Consolidate 命令
    parser_mini = subparsers.add_parser("mini-consolidate", help="白天轻量检查:只处理 pending buffer")
    parser_mini.set_defaults(func=cmd_mini_consolidate)

    # v1.2.2: 添加到 pending 命令
    parser_add_pending = subparsers.add_parser("add-pending", help="添加内容到 pending buffer")
    parser_add_pending.add_argument("content", help="要添加的内容")
    parser_add_pending.add_argument("--source", default="user", help="来源标记")
    parser_add_pending.set_defaults(func=cmd_add_pending)

    # v1.2.2: 查看 pending 命令
    parser_view_pending = subparsers.add_parser("view-pending", help="查看 pending buffer")
    parser_view_pending.set_defaults(func=cmd_view_pending)

    # 主动记忆引擎命令
    if PROACTIVE_ENABLED:
        # proactive-analyze: 分析消息意图
        parser_proactive_analyze = subparsers.add_parser("proactive-analyze", help="分析消息意图")
        parser_proactive_analyze.add_argument("message", help="要分析的消息")
        parser_proactive_analyze.add_argument("--role", default="user", help="消息角色 (user/assistant)")
        parser_proactive_analyze.add_argument("--json", action="store_true", help="输出 JSON 格式")
        parser_proactive_analyze.set_defaults(func=cmd_proactive_analyze)

        # proactive-context: 查看当前活跃上下文
        parser_proactive_context = subparsers.add_parser("proactive-context", help="查看当前活跃上下文")
        parser_proactive_context.set_defaults(func=cmd_proactive_context)

        # proactive-suggestions: 获取主动建议
        parser_proactive_suggestions = subparsers.add_parser("proactive-suggestions", help="获取主动建议")
        parser_proactive_suggestions.add_argument("--limit", type=int, default=5, help="最大建议数")
        parser_proactive_suggestions.set_defaults(func=cmd_proactive_suggestions)

        # proactive-stats: 查看主动记忆统计
        parser_proactive_stats = subparsers.add_parser("proactive-stats", help="查看主动记忆统计")
        parser_proactive_stats.set_defaults(func=cmd_proactive_stats)

        # proactive-reset: 重置主动记忆引擎
        parser_proactive_reset = subparsers.add_parser("proactive-reset", help="重置主动记忆引擎状态")
        parser_proactive_reset.set_defaults(func=cmd_proactive_reset)

    # v1.6.0: 向量检索命令
    if VECTOR_SEARCH_ENABLED:
        # vector-build: 构建向量索引
        parser_vector_build = subparsers.add_parser("vector-build", help="构建向量索引")
        parser_vector_build.add_argument("--batch-size", type=int, default=100, help="批量处理大小")
        parser_vector_build.add_argument("--provider", choices=["openai", "huggingface", "local"], help="嵌入提供者")
        parser_vector_build.add_argument("--model", help="嵌入模型名称")
        parser_vector_build.set_defaults(func=cmd_vector_build)

        # vector-search: 向量检索
        parser_vector_search = subparsers.add_parser("vector-search", help="向量语义检索")
        parser_vector_search.add_argument("query", help="检索查询")
        parser_vector_search.add_argument("--top-k", type=int, default=10, help="返回结果数量")
        parser_vector_search.add_argument("--type", choices=["fact", "belief", "summary"], help="过滤记忆类型")
        parser_vector_search.add_argument("--json", action="store_true", help="输出 JSON 格式")
        parser_vector_search.set_defaults(func=cmd_vector_search)

        # vector-status: 查看向量索引状态
        parser_vector_status = subparsers.add_parser("vector-status", help="查看向量索引状态")
        parser_vector_status.set_defaults(func=cmd_vector_status)

        # vector-config: 配置向量检索
        parser_vector_config = subparsers.add_parser("vector-config", help="配置向量检索参数")
        parser_vector_config.add_argument("--enable", action="store_true", help="启用向量检索")
        parser_vector_config.add_argument("--disable", action="store_true", help="禁用向量检索")
        parser_vector_config.add_argument("--keyword-weight", type=float, help="关键词权重 (0-1)")
        parser_vector_config.add_argument("--vector-weight", type=float, help="向量权重 (0-1)")
        parser_vector_config.add_argument("--min-score", type=float, help="最小分数阈值 (0-1)")
        parser_vector_config.set_defaults(func=cmd_vector_config)

    # Dashboard 命令
    parser_dashboard = subparsers.add_parser("dashboard", help="启动 Web 可视化面板")
    parser_dashboard.add_argument("--host", default="localhost", help="服务器地址")
    parser_dashboard.add_argument("--port", type=int, default=9090, help="服务器端口")
    parser_dashboard.add_argument("--memory-dir", help="记忆目录路径(优先于 MEMORY_DIR 环境变量)")
    parser_dashboard.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser_dashboard.set_defaults(func=cmd_dashboard)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
