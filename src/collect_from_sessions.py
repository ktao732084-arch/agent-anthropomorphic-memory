#!/usr/bin/env python3
"""
快速收集器：从对话转录本提取记忆到 pending buffer
"""

import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
import hashlib

def get_workspace():
    """获取工作空间路径(WORKSPACE 环境变量优先,默认 ~/.openclaw/workspace)"""
    workspace = os.environ.get('WORKSPACE', os.path.expanduser('~/.openclaw/workspace'))
    return Path(workspace)

def get_sessions_dir():
    """获取对话转录本目录(OPENCLAW_SESSIONS_DIR 环境变量优先)"""
    sessions_dir = os.environ.get(
        'OPENCLAW_SESSIONS_DIR',
        os.path.expanduser('~/.openclaw/agents/main/sessions'),
    )
    return Path(sessions_dir)

def get_memory_dir():
    """获取记忆系统目录(与 memory.py 一致:MEMORY_DIR 优先,其次 WORKSPACE/memory)"""
    if os.environ.get('MEMORY_DIR'):
        return Path(os.environ['MEMORY_DIR'])
    return get_workspace() / 'memory'

def load_pending(memory_dir):
    """加载现有 pending buffer"""
    pending_path = memory_dir / 'layer2/pending.jsonl'
    if not pending_path.exists():
        return []
    
    pending = []
    with open(pending_path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                pending.append(json.loads(line))
    return pending

def save_pending(memory_dir, pending):
    """保存 pending buffer"""
    pending_path = memory_dir / 'layer2/pending.jsonl'
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(pending_path, 'w', encoding='utf-8') as f:
        for item in pending:
            f.write(json.dumps(item, ensure_ascii=False) + '\n')

# 渠道消息(Telegram/飞书等)前面会包一层或多层元数据:
#   Conversation info (untrusted metadata):
#   ```json
#   {...}
#   ```
#   [Sun 2026-03-01 10:00 GMT+8] 用户原话
_ENVELOPE = re.compile(r'^\s*(Conversation info|Sender) \(untrusted metadata\):\s*```(?:json)?\s*.*?```\s*', re.S)
_STAMP = re.compile(r'^\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{4}-\d{2}-\d{2} \d{1,2}:\d{2}(?: GMT[+-]\d+)?\]\s*')
# 不是用户本人说的话:系统事件、定时任务、心跳、压缩前记忆保存提示
_NOT_USER = ('System:', '[cron:', 'Read HEARTBEAT.md', 'Pre-compaction memory flush')
_ESCAPE = re.compile(r'\\(.)', re.S)


def _fix_escape(match):
    """合法转义原样保留;非法的(如脱敏后留下的反斜杠加 x)补一个反斜杠变成字面量"""
    return match.group(0) if match.group(1) in '\\"/bfnrtu' else '\\\\' + match.group(1)


def load_jsonl_lenient(line):
    """解析一行 JSONL;脱敏工具留下的非法转义(如 \\x)会先修复再解析"""
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return json.loads(_ESCAPE.sub(_fix_escape, line))


def user_text(raw):
    """
    从一条 user 消息里取出用户原话

    剥掉渠道元数据外壳和时间戳前缀;系统事件、定时任务等返回 None
    """
    text = raw.strip()
    while True:
        stripped = _ENVELOPE.sub('', text, count=1)
        if stripped == text:
            break
        text = stripped
    text = _STAMP.sub('', text).strip()
    if not text or text.startswith(_NOT_USER):
        return None
    return text


def extract_messages(session_file, hours=24, include_assistant=True):
    """提取用户原话和 AI 对外回复；不采集工具调用、工具输出或思考块。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours) if hours else None
    messages = []

    try:
        with open(session_file, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = load_jsonl_lenient(line)
                except json.JSONDecodeError:
                    continue

                # OpenClaw 格式: {"type":"message", "message":{"role":"user", ...}}
                if entry.get('type') != 'message':
                    continue
                message = entry.get('message', {})
                role = message.get('role')
                if role not in ({'user', 'assistant'} if include_assistant else {'user'}):
                    continue

                timestamp_str = entry.get('timestamp')
                if cutoff and timestamp_str:
                    timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    if timestamp < cutoff:
                        continue

                content = message.get('content', [])
                if isinstance(content, str):
                    raw = content
                else:
                    raw = '\n'.join(
                        item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') == 'text'
                    )
                text = user_text(raw) if role == 'user' else raw.strip()
                if role == 'assistant' and text in {'NO_REPLY', 'HEARTBEAT_OK'}:
                    continue
                # 太短的通常是"好的""嗯",交给 memory.py 的噪声过滤也行,这里先挡掉单字
                if not text or len(text) < 2:
                    continue

                messages.append({
                    'content': text,
                    'source': role,
                    'role': role,
                    'timestamp': timestamp_str or datetime.now(timezone.utc).isoformat(),
                    'session': session_file.stem
                })
    except OSError as e:
        print(f"⚠️ 读取文件出错 {session_file}: {e}", file=sys.stderr)

    return messages


def extract_user_messages(session_file, hours=24):
    """兼容旧调用方：只提取用户原话。"""
    return extract_messages(session_file, hours, include_assistant=False)


def generate_id(content, timestamp, source='user'):
    """生成记忆 ID"""
    hash_input = f"{content}_{timestamp}" if source == "user" else f"{source}_{content}_{timestamp}"
    hash_hex = hashlib.md5(hash_input.encode()).hexdigest()[:6]
    date_str = timestamp[:10].replace('-', '')
    return f"p_{date_str}_{hash_hex}"

def collect_from_sessions(hours=24, dry_run=False, include_assistant=True):
    """从对话转录本收集记忆"""
    sessions_dir = get_sessions_dir()
    memory_dir = get_memory_dir()
    
    if not sessions_dir.exists():
        print(f"❌ 对话目录不存在: {sessions_dir}")
        return -1
    
    if not memory_dir.exists():
        print("❌ 记忆系统未初始化")
        return -1
    
    # 加载现有 pending
    pending = load_pending(memory_dir)
    existing_ids = {item['id'] for item in pending}
    
    print(f"🔍 扫描最近 {hours} 小时的对话...")
    
    # 获取最近修改的对话文件
    cutoff = datetime.now() - timedelta(hours=hours)
    recent_files = []
    
    for session_file in sessions_dir.glob('*.jsonl'):
        mtime = datetime.fromtimestamp(session_file.stat().st_mtime)
        if mtime >= cutoff:
            recent_files.append(session_file)
    
    print(f"📁 找到 {len(recent_files)} 个最近的对话文件")
    
    # 提取消息
    all_messages = []
    for session_file in recent_files:
        messages = extract_messages(session_file, hours, include_assistant)
        all_messages.extend(messages)
    
    print(f"💬 提取到 {len(all_messages)} 条对话消息")
    
    # 转换为 pending 格式
    new_count = 0
    for msg in all_messages:
        msg_id = generate_id(msg['content'], msg['timestamp'], msg['source'])
        
        # 去重
        if msg_id in existing_ids:
            continue
        
        pending_item = {
            'id': msg_id,
            'content': msg['content'],
            'source': msg['source'],
            'role': msg['role'],
            'created': msg['timestamp'],
            'urgent': False,
            'importance': 0.5,  # 默认重要性
            'category': '',
            'session': msg['session']
        }
        
        pending.append(pending_item)
        existing_ids.add(msg_id)
        new_count += 1
    
    print(f"✅ 新增 {new_count} 条待处理记忆")
    
    if dry_run:
        print("\n🔍 Dry run 模式，不写入文件")
        print("\n预览前 5 条:")
        for item in pending[-5:]:
            print(f"  - [{item['id']}] {item['content'][:60]}...")
    else:
        # 保存
        save_pending(memory_dir, pending)
        print(f"💾 已保存到 pending buffer (总计 {len(pending)} 条)")
    
    return new_count

if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='从对话转录本收集记忆')
    parser.add_argument('--hours', type=int, default=24, help='收集最近 N 小时的对话 (默认 24)')
    parser.add_argument('--dry-run', action='store_true', help='预览模式，不实际写入')
    
    parser.add_argument('--user-only', action='store_true', help='仅采集用户消息（默认也采集 AI 回复）')
    args = parser.parse_args()
    
    try:
        new_count = collect_from_sessions(args.hours, args.dry_run, not args.user_only)
        sys.exit(0 if new_count >= 0 else 1)
    except Exception as e:
        print(f"❌ 收集失败: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
