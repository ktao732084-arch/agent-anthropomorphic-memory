#!/usr/bin/env python3
"""Authenticated, stateless Android adapter for the real memory engine.

The phone owns the durable snapshot. Each request executes in an isolated worker
and temporary directory. No user-selected filenames, commands or model URLs.
Python 3.10+; standard library only unless the operator enables ONNX embeddings.
"""
import argparse
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import hmac
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import threading

PROTOCOL = 1
ENGINE_REVISION = '9862c930b367bbb0b5fa4b2c751914604cd0b548'
MAX_BYTES = 3 * 1024 * 1024
MAX_ACTIVE, MAX_ARCHIVE, MAX_AUDIT = 120, 240, 512
ENGINE_FIELDS = ('content', 'created', 'importance', 'score', 'entities', 'valid_at',
                 'event_date', 'expires_at', 'invalid_at', 'superseded_by',
                 'override_tier', 'conflict_downgraded', 'downgrade_reason',
                 'downgrade_at', 'archived_at', 'archived_reason', 'is_identity',
                 'last_accessed', 'access_count', 'updated', 'merged_from')


def capabilities():
    provider = os.environ.get('MEMORY_EMBEDDING_PROVIDER', 'none')
    return dict(protocol=PROTOCOL, engine_revision=ENGINE_REVISION,
                embeddings=provider, llm_judge=bool(os.environ.get('OPENAI_API_KEY')),
                storage='request-scoped', max_active=MAX_ACTIVE)


def validate_record(r):
    if not isinstance(r, dict):
        raise ValueError('record must be an object')
    for key, limit in [('id', 100), ('text', 240), ('source', 120)]:
        if not isinstance(r.get(key), str) or not 1 <= len(r[key]) <= limit:
            raise ValueError('invalid record ' + key)
    if not re.fullmatch(r'[\w.-]+', r['id']):
        raise ValueError('invalid id')
    if type(r.get('at')) is not int or not 0 <= r['at'] <= 4102444800000:
        raise ValueError('invalid observation time')
    sources = r.get('sources', [r['source']])
    if not isinstance(sources, list) or len(sources) > 200 or any(not isinstance(s, str) or len(s) > 120 for s in sources):
        raise ValueError('invalid sources')
    engine = r.get('engine', {})
    if not isinstance(engine, dict) or len(json.dumps(engine)) > 8000:
        raise ValueError('invalid engine metadata')
    if 'content' in engine and (not isinstance(engine['content'], str) or len(engine['content']) > 1500):
        raise ValueError('invalid resolved content')


def validate(payload):
    if not isinstance(payload, dict) or payload.get('protocol') != PROTOCOL:
        raise ValueError('unsupported protocol')
    if type(payload.get('now')) is not int or not 0 <= payload['now'] <= 4102444800000:
        raise ValueError('invalid now')
    offset = payload.get('utc_offset_minutes', 0)
    if type(offset) is not int or not -840 <= offset <= 840:
        raise ValueError('invalid timezone offset')
    query = payload.get('query', '')
    if not isinstance(query, str) or len(query) > 16000:
        raise ValueError('invalid query')
    state = payload.get('state')
    if not isinstance(state, dict):
        raise ValueError('invalid state')
    ids = set()
    for name, rows, limit in [('memories', state.get('memories', []), MAX_ACTIVE),
                              ('archive', state.get('archive', []), MAX_ARCHIVE),
                              ('incoming', payload.get('incoming', []), 3)]:
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError('too many ' + name)
        for r in rows:
            validate_record(r)
            if r['id'] in ids:
                raise ValueError('duplicate id')
            ids.add(r['id'])
    audit = state.get('audit', [])
    if not isinstance(audit, list) or len(audit) > MAX_AUDIT:
        raise ValueError('invalid audit')
    cache = state.get('vector_cache', {})
    if not isinstance(cache, dict) or len(json.dumps(cache)) > 1800000:
        raise ValueError('invalid vector cache')
    vectors = cache.get('vectors', {})
    if not isinstance(vectors, dict) or len(vectors) > MAX_ACTIVE:
        raise ValueError('invalid vectors')
    for key, vector in vectors.items():
        if not re.fullmatch('[a-f0-9]{16}', key) or not isinstance(vector, list) or not 1 <= len(vector) <= 4096:
            raise ValueError('invalid vector')
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
            raise ValueError('non-finite vector')


def evaluate(payload, directory):
    """Worker-only: call upstream filtering, temporal, dedup, archive and router."""
    validate(payload)
    os.environ['MEMORY_DIR'] = str(directory)
    now = datetime.fromtimestamp(payload['now'] / 1000, timezone.utc)
    os.environ['MEMORY_NOW'] = now.isoformat().replace('+00:00', 'Z')
    os.environ['MEMORY_LLM_ENABLED'] = 'false'  # no legacy extraction / fallback calls
    os.environ['MEMORY_LLM_JUDGE'] = 'on' if payload.get('judge') and os.environ.get('OPENAI_API_KEY') else 'off'
    if not payload.get('semantic'):
        os.environ['MEMORY_EMBEDDING_PROVIDER'] = 'none'
    import memory as engine
    from embeddings import content_key
    directory = Path(directory)
    engine.cmd_init(argparse.Namespace())
    state = copy.deepcopy(payload['state'])
    originals = {r['id']: r for r in state.get('memories', []) + state.get('archive', []) + payload.get('incoming', [])}
    zone = timezone(timedelta(minutes=payload.get('utc_offset_minutes', 0)))
    def to_engine(row):
        record = {k: v for k, v in row.get('engine', {}).items() if k in ENGINE_FIELDS}
        record.update(id=row['id'], source=row['source'])
        record.setdefault('content', row['text'])
        record.setdefault('created', datetime.fromtimestamp(row['at'] / 1000, timezone.utc).isoformat())
        weight, category = engine.calculate_importance(row['text'])
        record.setdefault('importance', max(weight, 0.8) if row.get('pinned') else weight)
        record.setdefault('score', record['importance'])
        record.setdefault('entities', [])
        record.setdefault('is_identity', category == 'identity_health_safety')
        if not record.get('valid_at'):
            observed = datetime.fromtimestamp(row['at'] / 1000, zone)
            engine.ground_record_time(record, category, observed.isoformat())
            if record.get('expires_at'):
                day = datetime.fromisoformat(record['expires_at'].replace('Z', '+00:00')).date()
                record['expires_at'] = datetime.combine(day, datetime.min.time(), zone).astimezone(timezone.utc).isoformat()
        return record
    active = [to_engine(r) for r in state.get('memories', [])]
    archived = [to_engine(r) for r in state.get('archive', [])]
    path = directory / 'layer2/active/facts.jsonl'
    archive_path = directory / 'layer2/archive/facts.jsonl'
    engine.save_jsonl(path, active)
    engine.save_jsonl(archive_path, archived)
    cache_path = directory / 'layer2/index/embeddings.json'
    settings = engine.load_embedding_settings(engine.get_config())
    scope = hashlib.sha256(json.dumps({k: settings.get(k) for k in ('provider', 'base_url', 'model', 'model_dir', 'query_prefix')}, sort_keys=True).encode()).hexdigest()
    if state.get('vector_cache', {}).get('service_scope') == scope:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(state['vector_cache']), encoding='utf-8')
    engine.archive_stale_memories(directory)
    active = engine.load_jsonl(path)
    events, rejected = [], []
    for row in payload.get('incoming', []):
        noise, reason = engine.is_noise(row['text'])
        if noise:
            rejected.append({'id': row['id'], 'reason': reason})
            continue
        record = to_engine(row)
        merged, _, _ = engine.deduplicate_facts([record], active, events)
        active.extend(merged)
        if len(active) > MAX_ACTIVE:
            raise ValueError('active memory capacity reached; delete or archive a memory')
    # Reattach quote provenance after engine merges; the engine's annotated content
    # is separate from immutable user quotes displayed by Android.
    for event in events:
        if event['op'] == 'MERGE':
            target, incoming = originals[event['id']], originals[event['from']]
            sources = list(dict.fromkeys(target.get('sources', [target['source']]) + incoming.get('sources', [incoming['source']])))
            target.update(text=incoming['text'], source=incoming['source'], at=incoming['at'], sources=sources[-200:],
                          pinned=bool(target.get('pinned') or incoming.get('pinned')))
        elif event['op'] == 'NOOP':
            incoming = originals[event['id']]
            target_id = event.get('duplicate_of')
            if not target_id:
                target_id = next((r['id'] for r in active if engine.MemoryOperator._normalize(r['content']) == engine.MemoryOperator._normalize(incoming['text'])), None)
            if target_id in originals:
                target = originals[target_id]
                target['sources'] = list(dict.fromkeys(target.get('sources', [target['source']]) + [incoming['source']]))[-200:]
                target['pinned'] = bool(target.get('pinned') or incoming.get('pinned'))
    engine.save_jsonl(path, active)
    engine.log_history(directory, events)
    engine.archive_stale_memories(directory)
    engine.rebuild_index(directory)
    results = engine.router_search(payload.get('query', ''), directory, use_qmd=False, use_vector=False)['results'] if payload.get('query') else []
    hits = [r['id'] for r in results[:10]]
    active = engine.load_jsonl(path)
    archived = engine.load_jsonl(archive_path)
    def to_phone(record):
        row = copy.deepcopy(originals[record['id']])
        row['engine'] = {k: v for k, v in record.items() if k in ENGINE_FIELDS}
        if record['id'] in hits:
            row['engine']['last_accessed'] = now.isoformat()
            row['engine']['access_count'] = int(row['engine'].get('access_count', 0)) + 1
        return row
    audit = state.get('audit', [])[:]
    for event in engine.load_jsonl(directory / 'layer2/history.jsonl'):
        audit.append({k: v for k, v in {**event, 'at': payload['now']}.items() if k in ('op', 'id', 'by', 'from', 'at', 'reason', 'decided_by')})
    cache = json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.exists() else {}
    keep = {content_key(r['content']) for r in active}
    if cache:
        cache['vectors'] = {k: v for k, v in cache.get('vectors', {}).items() if k in keep}
        cache['service_scope'] = scope
    return dict(protocol=PROTOCOL, engine_revision=ENGINE_REVISION,
                state=dict(memories=[to_phone(r) for r in active], archive=[to_phone(r) for r in archived[-MAX_ARCHIVE:]],
                           audit=audit[-MAX_AUDIT:], vector_cache=cache), hits=hits, rejected=rejected,
                features=dict(semantic=os.environ.get('MEMORY_EMBEDDING_PROVIDER', 'none'),
                              judge=os.environ.get('MEMORY_LLM_JUDGE') == 'on'))


def run_isolated(payload):
    validate(payload)
    env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
    # Parent owns cleanup, including worker timeouts and forced worker exits.
    with tempfile.TemporaryDirectory(prefix='companion-memory-') as tmp:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', '--worker-dir', tmp],
                                input=json.dumps(payload), capture_output=True, text=True,
                                encoding='utf-8', env=env, timeout=110)
    if result.returncode:
        raise RuntimeError('memory worker failed')
    return json.loads(result.stdout)


def make_server(host, port, token):
    if len(token) < 32 or any(c.isspace() for c in token):
        raise ValueError('MEMORY_SERVICE_TOKEN must contain at least 32 non-whitespace characters')
    slots = threading.BoundedSemaphore(2)
    class Handler(BaseHTTPRequestHandler):
        server_version = 'CompanionMemory/1'
        def log_message(self, *_):
            pass  # never log user quotes, authorization or model responses
        def setup(self):
            super().setup()
            self.connection.settimeout(120)
        def reply(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        def authorized(self):
            if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + token).encode()):
                self.reply(401, {'error': 'unauthorized'})
                return False
            return True
        def do_GET(self):
            if self.authorized():
                self.reply(200, capabilities()) if self.path == '/health' else self.reply(404, {'error': 'not found'})
        def do_POST(self):
            if not self.authorized():
                return
            if self.path != '/v1/evaluate':
                self.reply(404, {'error': 'not found'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= MAX_BYTES:
                    self.reply(413, {'error': 'request too large or empty'})
                    return
                payload = json.loads(self.rfile.read(size))
                validate(payload)
                if not slots.acquire(blocking=False):
                    self.reply(429, {'error': 'busy; retry later'})
                    return
                try:
                    result = run_isolated(payload)
                finally:
                    slots.release()
                self.reply(200, result)
            except (ValueError, TypeError, KeyError):
                self.reply(400, {'error': 'invalid memory request'})
            except Exception:
                self.reply(503, {'error': 'memory unavailable; keep local state'})
    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--certfile')
    parser.add_argument('--keyfile')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--worker-dir', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        payload = json.load(sys.stdin)
        # Discard engine progress diagnostics; output is exactly one JSON object.
        if not args.worker_dir:
            parser.error('worker directory required')
        diagnostics = io.StringIO()
        with contextlib.redirect_stdout(diagnostics), contextlib.redirect_stderr(diagnostics):
            result = evaluate(payload, args.worker_dir)
        result['degraded'] = any(word in diagnostics.getvalue() for word in ('不可用', '失败', '未设置'))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return
    if bool(args.certfile) != bool(args.keyfile):
        parser.error('--certfile and --keyfile must be provided together')
    if args.host not in ('127.0.0.1', 'localhost', '::1') and not args.certfile:
        parser.error('non-loopback listeners require TLS')
    server = make_server(args.host, args.port, os.environ.get('MEMORY_SERVICE_TOKEN', ''))
    if args.certfile:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.certfile, args.keyfile)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print(f'Memory service ready at {args.host}:{server.server_port}; protocol {PROTOCOL}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
