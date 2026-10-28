#!/usr/bin/env python3
"""
把公开的长期记忆评测集转换成统一格式

统一格式（每条是一个用户的完整时间线）：
{
  "id": "memorybank-张曼婷",
  "source": "memorybank" | "perltqa" | "locomo" | "private",
  "user": "张曼婷",
  "sessions": [
    {"date": "2023-04-27", "turns": [{"role": "user" | "assistant", "speaker": "张曼婷", "text": "..."}]}
  ],
  "questions": [
    {"q": "...", "answer": "..." | null, "category": "extraction" | "multi_session" | "temporal"
                 | "update" | "abstention" | "preference" | "assistant_said", "evidence": "..."}
  ]
}

数据集不随仓库分发（各有许可证），先运行 fetch_datasets.sh 下载到 DATA_DIR。
"""

import json
import random
import re
from pathlib import Path

# ============================================================
# MemoryBank（中文）：15 个用户 × 10 天的用户-AI 对话 + 每人约 7 个追问
# 原数据没有标准答案，gold 由 make_gold 用完整对话生成（见 run_longterm.py）
# ============================================================


def load_memorybank(data_dir: Path):
    root = data_dir / "MemoryBank-SiliconFriend/eval_data/cn"
    bank = json.loads((root / "memory_bank_cn.json").read_text(encoding="utf-8"))
    probes = {}
    for line in (root / "probing_questions_cn.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            probes.update(json.loads(line))

    timelines = []
    for user, info in bank.items():
        sessions = []
        for date, turns in sorted(info["history"].items()):
            items = []
            for t in turns:
                items.append({"role": "user", "speaker": user, "text": t["query"]})
                items.append({"role": "assistant", "speaker": "AI", "text": t["response"]})
            sessions.append({"date": date, "turns": items})
        questions = []
        for q in probes.get(user, []):
            # "你给我推荐/分享过……" 问的是 AI 自己说过的话（只写入用户消息时记不住）
            if re.search(r"你(曾经|之前)?(给|和|跟|向)?我?(推荐|分享|介绍|建议|推过)", q):
                category = "assistant_said"
            elif re.search(r"\d+月\d+[日号]|那天|这天", q):
                category = "temporal"
            else:
                category = "extraction"
            questions.append({"q": q, "answer": None, "category": category})
        timelines.append(
            {"id": f"memorybank-{user}", "source": "memorybank", "user": user, "sessions": sessions, "questions": questions}
        )
    return timelines


# ============================================================
# PerLTQA（中文）：人物与 AI 助手的带时间戳对话 + 基于对话的问答
# ============================================================

_CN_DATE = re.compile(r"(\d{4})[-年/.](\d{1,2})[-月/.](\d{1,2})")


def _parse_date(text):
    m = _CN_DATE.search(text)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    except ValueError:
        return None


def load_perltqa(data_dir: Path, characters=4, per_character=25, seed=7):
    root = data_dir / "PerLTQA/Dataset/zh"
    mems = json.loads((root / "perltmem.json").read_text(encoding="utf-8"))
    qas = json.loads((root / "perltqa.json").read_text(encoding="utf-8"))
    by_name = {}
    for item in mems:
        mem = list(item.values())[0] if len(item) == 1 else item
        by_name[mem["profile"]["Protagonist"]] = mem

    rng = random.Random(seed)
    candidates = [list(x)[0] for x in qas]
    qa_by_name = {list(x)[0]: list(x.values())[0] for x in qas}
    # 只选对话问答足够多的人物
    candidates = [n for n in candidates if sum(len(v) for v in qa_by_name[n].get("dialogues", {}).values()) >= per_character]
    chosen = sorted(rng.sample(candidates, min(characters, len(candidates))))

    timelines = []
    for name in chosen:
        mem = by_name[name]
        # 每段对话按其中第一个能解析的日期排序；解析不了的沿用前一段的日期
        dialogues = []
        for did, dlg in mem["dialogues"].items():
            stamps = list(dlg["contents"].keys())
            date = next((d for d in map(_parse_date, stamps) if d), None)
            dialogues.append((date, did, dlg))
        known = sorted(d for d, _, _ in dialogues if d)
        fallback = known[0] if known else "2022-01-01"
        dialogues.sort(key=lambda x: (x[0] or fallback, x[1]))

        sessions = []
        last = fallback
        for date, did, dlg in dialogues:
            last = date or last
            turns = []
            for lines in dlg["contents"].values():
                for line in _flatten(lines):
                    speaker, _, text = line.partition(":")
                    if not text:
                        speaker, _, text = line.partition("：")
                    role = "user" if speaker.strip() == name else "assistant"
                    turns.append({"role": role, "speaker": speaker.strip(), "text": text.strip()})
            sessions.append({"date": last, "turns": turns, "dialogue_id": did})

        pool = [
            {"q": q["Question"], "answer": q["Answer"], "category": "extraction", "evidence": did}
            for did, qs in qa_by_name[name].get("dialogues", {}).items()
            for q in qs
        ]
        questions = sorted(rng.sample(pool, min(per_character, len(pool))), key=lambda x: x["q"])
        timelines.append({"id": f"perltqa-{name}", "source": "perltqa", "user": name, "sessions": _merge_same_day(sessions), "questions": questions})
    return timelines


def _flatten(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, list):
        for y in x:
            yield from _flatten(y)


def _merge_same_day(sessions):
    merged = []
    for s in sessions:
        if merged and merged[-1]["date"] == s["date"]:
            merged[-1]["turns"].extend(s["turns"])
        else:
            merged.append({"date": s["date"], "turns": list(s["turns"])})
    return merged


# ============================================================
# LoCoMo（英文）：两个人之间几十个 session 的长对话 + 分类问答
# 类别：1 多跳 / 2 时间 / 3 开放推理 / 4 单跳 / 5 对抗（应答"没提过"）
# ============================================================

LOCOMO_CATEGORY = {1: "multi_session", 2: "temporal", 3: "inference", 4: "extraction", 5: "abstention"}
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}


def _locomo_date(text):
    # 例："1:56 pm on 8 May, 2023"
    m = re.search(r"(\d{1,2}) (\w+),? (\d{4})", text)
    if not m:
        return None
    d, mon, y = m.groups()
    return f"{int(y):04d}-{_MONTHS.get(mon.lower(), 1):02d}-{int(d):02d}"


def load_locomo(data_dir: Path, conversations=2, per_category=12, seed=7):
    data = json.loads((data_dir / "locomo/data/locomo10.json").read_text(encoding="utf-8"))
    rng = random.Random(seed)
    timelines = []
    for sample in data[:conversations]:
        conv = sample["conversation"]
        a, b = conv["speaker_a"], conv["speaker_b"]
        keys = sorted(
            (k for k in conv if re.fullmatch(r"session_\d+", k)), key=lambda k: int(k.split("_")[1])
        )
        sessions = []
        for k in keys:
            date = _locomo_date(conv.get(f"{k}_date_time", "")) or (sessions[-1]["date"] if sessions else "2023-01-01")
            # 两个人都是"用户"：记忆系统要记住双方说的话，所以都按 user 写入，带上说话人
            turns = [{"role": "user", "speaker": t["speaker"], "text": f"{t['speaker']}: {t['text']}"} for t in conv[k]]
            sessions.append({"date": date, "turns": turns})

        by_cat = {}
        for q in sample["qa"]:
            by_cat.setdefault(q["category"], []).append(q)
        questions = []
        for cat, qs in sorted(by_cat.items()):
            for q in rng.sample(qs, min(per_category, len(qs))):
                answer = q.get("answer")
                questions.append(
                    {
                        "q": q["question"],
                        "answer": None if cat == 5 else str(answer),
                        "category": LOCOMO_CATEGORY[cat],
                        "evidence": ",".join(q.get("evidence", [])),
                    }
                )
        timelines.append({"id": f"locomo-{sample['sample_id']}", "source": "locomo", "user": f"{a}&{b}", "sessions": _merge_same_day(sessions), "questions": questions})
    return timelines


LOADERS = {"memorybank": load_memorybank, "perltqa": load_perltqa, "locomo": load_locomo}


if __name__ == "__main__":
    import sys

    data_dir = Path(sys.argv[1])
    for name, loader in LOADERS.items():
        tls = loader(data_dir)
        q = sum(len(t["questions"]) for t in tls)
        turns = sum(len(s["turns"]) for t in tls for s in t["sessions"])
        cats = {}
        for t in tls:
            for x in t["questions"]:
                cats[x["category"]] = cats.get(x["category"], 0) + 1
        print(f"{name}: {len(tls)} 条时间线, {sum(len(t['sessions']) for t in tls)} 天, {turns} 轮, {q} 题 {cats}")
