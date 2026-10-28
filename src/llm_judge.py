#!/usr/bin/env python3
"""
可选的 LLM 写入判断（借鉴 mem0：写入时让 LLM 对照相关旧记忆做决定）

规则和语义相似度只能可靠处理"带更正词的更正"和"字面重复"。下面两类要理解意思才能判断：
- 没有更正词的变化：搬家、换工作、分手、偏好改变、计划已完成
- 换个说法的重复："我养了一只橘猫叫年糕" / "我家年糕是只橘猫"

启用后，规则判定为"新增"的记忆会和最相关的几条旧记忆一起交给 LLM，三选一：
    ADD        新信息，和旧记忆可以同时成立（花生过敏 / 花粉过敏）
    DUPLICATE  同一件事换了说法 → 按重复合并
    SUPERSEDE  新信息让旧记忆不再成立 → 旧记忆失效并归档（不删除）

默认关闭。启用与配置（与 LLM 兜底共用接口配置）：

    MEMORY_LLM_JUDGE=on
    OPENAI_API_KEY / OPENAI_BASE_URL     任何 OpenAI 兼容的 /chat/completions 接口
    MEMORY_LLM_JUDGE_MODEL               默认取 MEMORY_LLM_MODEL，再默认 gpt-4o-mini

建议同时启用语义向量（src/embeddings.py）：候选旧记忆按语义相似度挑选；
没有向量时只能按字面重叠挑选，"住在北京 / 住在上海"这类会漏掉。

任何失败（网络、超时、格式不对、编号越界）都返回 None，由调用方按规则结果处理。
"""

import json
import os
import re
import sys
import urllib.request
from typing import Callable, Dict, List, Optional

SYSTEM_PROMPT = "你是个人记忆库的写入审核员，判断一条新记忆和已有记忆的关系。只输出 JSON，不要输出其他内容。"

# 注意：示例不要取自 eval/cases/ 中的用例，否则评测结果会虚高
USER_PROMPT = """新记忆（{new_date}）：{new_content}

已有记忆：
{candidates}

判断新记忆和已有记忆的关系，三选一：
- DUPLICATE：新记忆和某条已有记忆说的是同一件事，只是说法不同，没有推翻任何内容。
- SUPERSEDE：新记忆让某条（或几条）已有记忆不再成立。例如搬家、换工作、分手、换了偏好、
  计划已经完成或取消、原来的说法有误。
- ADD：新记忆是新信息，和已有记忆可以同时成立。例如"对酒精过敏"和"对乳胶过敏"、
  "王五喜欢踢足球"和"赵六喜欢踢足球"、"在苏州出生"和"在杭州上班"。
拿不准时选 ADD。

只输出 JSON：{{"decision": "ADD 或 DUPLICATE 或 SUPERSEDE", "targets": [已有记忆编号], "reason": "一句话理由"}}"""

DECISIONS = {"ADD", "DUPLICATE", "SUPERSEDE"}


def load_settings() -> Dict:
    return {
        "enabled": os.environ.get("MEMORY_LLM_JUDGE", "off").lower() in ("1", "on", "true", "yes"),
        "api_key": os.environ.get("OPENAI_API_KEY"),
        "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "model": os.environ.get("MEMORY_LLM_JUDGE_MODEL") or os.environ.get("MEMORY_LLM_MODEL") or "gpt-4o-mini",
        "max_candidates": int(os.environ.get("MEMORY_LLM_JUDGE_CANDIDATES", "5")),
        # 有语义向量时，候选旧记忆的最低语义相似度
        "candidate_min_semantic": float(os.environ.get("MEMORY_LLM_JUDGE_MIN_SEMANTIC", "0.5")),
        # 没有语义向量时，候选旧记忆的最低字面重叠率。由 LLM 做最终判断，宁可多给候选：
        # 0.15 能覆盖只共享一个人名的情况（"我女朋友叫苏晴" / "我和苏晴分手了"）
        "candidate_min_literal": float(os.environ.get("MEMORY_LLM_JUDGE_MIN_LITERAL", "0.15")),
    }


def build_messages(new: Dict, candidates: List[Dict]) -> List[Dict]:
    lines = [f"[{i}]（{_date(c)}）{c['content']}" for i, c in enumerate(candidates, 1)]
    prompt = USER_PROMPT.format(new_date=_date(new), new_content=new["content"], candidates="\n".join(lines))
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]


def _date(record: Dict) -> str:
    return (record.get("valid_at") or record.get("created") or "日期未知")[:10]


def parse_decision(text: str, n_candidates: int) -> Optional[Dict]:
    """解析 LLM 输出；格式不对或编号越界时返回 None"""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    decision = str(data.get("decision", "")).upper()
    if decision not in DECISIONS:
        return None
    try:
        targets = sorted({int(t) for t in data.get("targets") or []})
    except (TypeError, ValueError):
        return None
    if any(t < 1 or t > n_candidates for t in targets):
        return None
    if decision != "ADD" and not targets:
        return None
    return {"decision": decision, "targets": targets if decision != "ADD" else [], "reason": str(data.get("reason", ""))}


class LLMJudge:
    def __init__(self, settings: Dict, transport: Optional[Callable[[List[Dict]], str]] = None):
        self.settings = settings
        self.transport = transport or self._chat
        self.calls = 0
        self.failures = 0

    def _chat(self, messages: List[Dict]) -> str:
        body = json.dumps(
            {"model": self.settings["model"], "messages": messages, "temperature": 0, "max_tokens": 300}
        ).encode("utf-8")
        req = urllib.request.Request(
            self.settings["base_url"].rstrip("/") + "/chat/completions",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.settings['api_key']}"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            message = json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]
        # 兼容思考模型：content 为空时取 reasoning_content
        return message.get("content") or message.get("reasoning_content") or ""

    def judge(self, new: Dict, candidates: List[Dict]) -> Optional[Dict]:
        """
        返回 {"decision", "targets": [候选记录], "reason"}；失败时返回 None
        """
        if not candidates:
            return None
        self.calls += 1
        try:
            text = self.transport(build_messages(new, candidates))
        except Exception as e:
            self.failures += 1
            print(f"⚠️ LLM 判断失败,按规则处理: {e}", file=sys.stderr)
            return None
        parsed = parse_decision(text, len(candidates))
        if parsed is None:
            self.failures += 1
            print(f"⚠️ LLM 判断返回格式无法解析,按规则处理: {text[:200]!r}", file=sys.stderr)
            return None
        parsed["targets"] = [candidates[i - 1] for i in parsed["targets"]]
        return parsed


def get_judge(settings: Optional[Dict] = None) -> Optional[LLMJudge]:
    """启用且有 API Key 时返回 LLMJudge，否则 None"""
    settings = settings or load_settings()
    if not settings["enabled"]:
        return None
    if not settings["api_key"]:
        print("⚠️ 已设置 MEMORY_LLM_JUDGE 但缺少 OPENAI_API_KEY,LLM 判断未启用", file=sys.stderr)
        return None
    return LLMJudge(settings)
