#!/usr/bin/env python3
"""
相对时间落地（借鉴 mem0：相对时间必须换算成绝对日期再存）

"我下周三有考试" 半年后再读就失去意义；"我下周三（2026-11-04）有考试" 永远有意义。
这里用规则把常见中文相对时间换算成绝对日期，标注在原文后面，并返回事件日期，
供调用方设置过期时间。纯标准库，0 Token。

故意不处理的情况：
- "每周三"、"周三"（不带 这/本/下）：多为周期性或指代不明，不标注
- "X月X日"：已经是绝对日期（缺年份时不猜）
"""

import re
from datetime import date, timedelta
from typing import Optional, Tuple

_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "七": 6}

_DAY_OFFSETS = {
    "大前天": -3,
    "前天": -2,
    "昨天": -1,
    "昨晚": -1,
    "昨日": -1,
    "今天": 0,
    "今晚": 0,
    "今早": 0,
    "今日": 0,
    "明天": 1,
    "明早": 1,
    "明晚": 1,
    "明日": 1,
    "后天": 2,
    "大后天": 3,
}

# 长词放前面，保证 "大后天" 不会被 "后天" 抢先匹配
_DAY_PATTERN = "|".join(sorted(_DAY_OFFSETS, key=len, reverse=True))
_WEEK_PATTERN = r"(?<!每)(下下|下|这|本|上)(?:个)?(?:周|星期|礼拜)([一二三四五六日天七])?"
_MONTH_PATTERN = r"(?<!每)(下|这|本|上)(?:个)?月(?:(\d{1,2})[号日])?"
_YEAR_PATTERN = r"(?<!每)(明年|今年|去年)"

_TOKEN_RE = re.compile(f"({_DAY_PATTERN})|{_WEEK_PATTERN}|{_MONTH_PATTERN}|{_YEAR_PATTERN}")

# 已经标注过的不再重复标注
_ANNOTATED_RE = re.compile(r"^（\d{4}")


def _resolve(match: re.Match, observed: date) -> Tuple[Optional[str], Optional[date]]:
    """返回 (标注文本, 精确到天的日期或 None)"""
    day_word, week_prefix, weekday, month_prefix, month_day, year_word = match.groups()

    if day_word:
        d = observed + timedelta(days=_DAY_OFFSETS[day_word])
        return d.isoformat(), d

    if week_prefix:
        week_shift = {"上": -1, "这": 0, "本": 0, "下": 1, "下下": 2}[week_prefix]
        monday = observed - timedelta(days=observed.weekday()) + timedelta(weeks=week_shift)
        if weekday:
            d = monday + timedelta(days=_WEEKDAYS[weekday])
            return d.isoformat(), d
        return f"{monday.isoformat()} 当周", None

    if month_prefix:
        month_shift = {"上": -1, "这": 0, "本": 0, "下": 1}[month_prefix]
        total = observed.year * 12 + (observed.month - 1) + month_shift
        year, month = divmod(total, 12)
        month += 1
        if month_day:
            try:
                d = date(year, month, int(month_day))
            except ValueError:
                return None, None
            return d.isoformat(), d
        return f"{year}-{month:02d}", None

    if year_word:
        shift = {"去年": -1, "今年": 0, "明年": 1}[year_word]
        return f"{observed.year + shift}年", None

    return None, None


def ground_relative_dates(text: str, observed: date) -> Tuple[str, Optional[date]]:
    """
    把文本中的相对时间换算成绝对日期并标注在后面

    Args:
        text: 原文
        observed: 这句话被说出的日期（观察日期，不是今天）

    Returns:
        (标注后的文本, 第一个精确到天的事件日期或 None)
    """
    parts = []
    last = 0
    event_date = None

    for match in _TOKEN_RE.finditer(text):
        if _ANNOTATED_RE.match(text[match.end():]):
            continue
        label, d = _resolve(match, observed)
        if not label:
            continue
        parts.append(text[last:match.end()])
        parts.append(f"（{label}）")
        last = match.end()
        if event_date is None and d is not None:
            event_date = d

    parts.append(text[last:])
    return "".join(parts), event_date


if __name__ == "__main__":
    obs = date(2026, 10, 28)  # 周三
    cases = [
        ("我下周三有个重要考试", "我下周三（2026-11-04）有个重要考试", date(2026, 11, 4)),
        ("明天下午开会", "明天（2026-10-29）下午开会", date(2026, 10, 29)),
        ("大后天出发", "大后天（2026-10-31）出发", date(2026, 10, 31)),
        ("我昨天去了北京", "我昨天（2026-10-27）去了北京", date(2026, 10, 27)),
        ("下个月15号交论文", "下个月15号（2026-11-15）交论文", date(2026, 11, 15)),
        ("我每周三去健身", "我每周三去健身", None),
        ("下周要搬家", "下周（2026-11-02 当周）要搬家", None),
        ("明年准备考研", "明年（2027年）准备考研", None),
        ("我对花生过敏", "我对花生过敏", None),
        ("明天（2026-10-29）开会", "明天（2026-10-29）开会", None),
    ]
    passed = 0
    for text, want_text, want_date in cases:
        got_text, got_date = ground_relative_dates(text, obs)
        ok = got_text == want_text and got_date == want_date
        passed += ok
        print(f"{'✅' if ok else '❌'} {text} → {got_text} ({got_date})")
    print(f"\n{passed}/{len(cases)} 通过")
