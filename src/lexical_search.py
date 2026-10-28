"""Dependency-free BM25 over the current searchable pool (including pending).

Chinese uses overlapping bigrams; Latin words and numeric identifiers remain
whole tokens. Identifier components also match queries such as a branch suffix.
No persistent index is required, so existing stores need no migration.
"""

import math
import re
from collections import Counter

_TOKENS = re.compile(r"[\u3400-\u9fff]+|[a-z0-9]+(?:[._/-][a-z0-9]+)*", re.I)


def tokenize(text):
    tokens = []
    for match in _TOKENS.finditer(text.casefold()):
        part = match.group()
        if "\u3400" <= part[0] <= "\u9fff":
            tokens.extend(part[i:i + 2] for i in range(len(part) - 1))
            if len(part) == 1:
                tokens.append(part)
        else:
            tokens.append(part)
            pieces = re.split(r"[._/-]", part)
            if len(pieces) > 1:
                tokens.extend(pieces)
    return tokens


def rank(query, records, k1=1.2, b=0.75):
    """Return copies of matching records with raw and normalized BM25 scores.

    IDF = log(1 + (N - df + .5)/(df + .5)); repeated query terms count once.
    Length is token count, including repetitions. Scores are normalized against
    the best match in this pool for use alongside bounded recency/importance.
    """
    terms = set(tokenize(query))
    if not terms or not records:
        return []
    counts = [Counter(tokenize(r.get("content", ""))) for r in records]
    lengths = [sum(c.values()) for c in counts]
    avg_length = sum(lengths) / len(records) or 1
    df = Counter(t for c in counts for t in terms if t in c)
    idf = {t: math.log1p((len(records) - n + 0.5) / (n + 0.5)) for t, n in df.items()}
    scored = []
    for record, counts_, length in zip(records, counts, lengths):
        norm = k1 * (1 - b + b * length / avg_length)
        score = sum(idf[t] * counts_[t] * (k1 + 1) / (counts_[t] + norm)
                    for t in terms if counts_[t])
        if score > 0:
            scored.append({**record, "bm25_score": score})
    scored.sort(key=lambda r: (-r["bm25_score"], r["id"]))
    best = scored[0]["bm25_score"] if scored else 1
    for record in scored:
        record["lexical_relevance"] = record["bm25_score"] / best
    return scored
