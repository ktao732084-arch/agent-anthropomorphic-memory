"""Replay the unchanged 84 scenarios against the phone snapshot protocol.

This reports known engine limitations; it does not turn all product expectations
into passing assertions. Use --json to compare per-assertion results across runs.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import companion_api as api
from run_eval import load_cases, check_state, matches, report

def run_case(case):
    state = dict(memories=[], archive=[], audit=[])
    rows, sequence = [], 0
    def call(now, incoming=(), query=''):
        nonlocal state
        response = api.run_isolated(dict(protocol=1, now=now, utc_offset_minutes=0,
                                        state=state, incoming=list(incoming), query=query,
                                        semantic=False, judge=False))
        state = response['state']
        return response
    for day, session in enumerate(case['sessions'], 1):
        now = int(datetime.fromisoformat(session['date'] + 'T10:00:00+00:00').timestamp() * 1000)
        for message in session.get('say', []):
            sequence += 1
            call(now, [dict(id='r' + str(sequence), text=message, source='turn' + str(sequence), at=now, pinned=False)])
        call(now + int(13.5 * 3600000))
        active = [dict(id=r['id'], content=r.get('engine', {}).get('content', r['text'])) for r in state['memories']]
        checks = check_state(session.get('expect', {}), active)
        for query in session.get('queries', []):
            response = call(now + int(13.5 * 3600000), query=query['q'])
            by_id = {r['id']: r.get('engine', {}).get('content', r['text']) for r in state['memories']}
            ranked = [by_id[i] for i in response['hits'][:3] if i in by_id]
            hit = query.get('hit')
            if hit:
                checks.append(('hit', any(matches(c, hit) for c in ranked), query['q']))
            if query.get('top1'):
                checks.append(('top1', bool(ranked) and matches(ranked[0], query['top1']), query['q']))
            if query.get('stale'):
                checks.append(('stale', not any(matches(c, query['stale']) and not (hit and matches(c, hit)) for c in ranked), query['q']))
        for metric, passed, detail in checks:
            rows.append(dict(case=case['id'], category=case['category'], day=day, metric=metric,
                             passed=bool(passed), detail=detail, active=[r['content'] for r in active]))
    return rows

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--json')
    parser.add_argument('-j', type=int, default=4)
    args = parser.parse_args()
    cases = load_cases()
    with ThreadPoolExecutor(max_workers=args.j) as pool:
        results = [r for rows in pool.map(run_case, cases) for r in rows]
    report(results, True)
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
