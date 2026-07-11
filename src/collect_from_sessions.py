#!/usr/bin/env python3
"""
快速收集器：从对话转录本提取记忆到 pending buffer
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta
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
    """获取记忆系统目录"""
    workspace = get_workspace()
    return workspace / 'memory'

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

def extract_user_messages(session_file, hours=24):
    """从对话文件提取用户消息"""
    cutoff = datetime.now() - timedelta(hours=hours)
    messages = []
    
    try:
        with open(session_file, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                
                try:
                    entry = json.loads(line)
                    
                    # OpenClaw 格式: {"type":"message", "message":{"role":"user", ...}}
                    if entry.get('type') != 'message':
                        continue
                    
                    message = entry.get('message', {})
                    if message.get('role') != 'user':
                        continue
                    
                    # 检查时间
                    timestamp_str = entry.get('timestamp')
                    if timestamp_str:
                        timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                        if timestamp.replace(tzinfo=None) < cutoff:
                            continue
                    
                    # 提取内容 (content 是数组格式)
                    content_array = message.get('content', [])
                    if not content_array:
                        continue
                    
                    # 合并所有 text 类型的内容
                    text_parts = []
                    for item in content_array:
                        if isinstance(item, dict) and item.get('type') == 'text':
                            text_parts.append(item.get('text', ''))
                    
                    content = '\n'.join(text_parts).strip()
                    
                    # 过滤太短或无意义的消息
                    if len(content) < 10:
                        continue
                    
                    # 过滤系统消息和元数据
                    if 'Conversation info (untrusted metadata)' in content:
                        continue
                    if content.startswith('Read HEARTBEAT.md'):
                        continue
                    if content.startswith('System:'):
                        continue
                    
                    messages.append({
                        'content': content,
                        'timestamp': timestamp_str or datetime.now().isoformat() + 'Z',
                        'session': session_file.stem
                    })
                    
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    print(f"⚠️ 解析消息出错: {e}", file=sys.stderr)
                    continue
    
    except Exception as e:
        print(f"⚠️ 读取文件出错 {session_file}: {e}", file=sys.stderr)
    
    return messages

def generate_id(content, timestamp):
    """生成记忆 ID"""
    hash_input = f"{content}_{timestamp}"
    hash_hex = hashlib.md5(hash_input.encode()).hexdigest()[:6]
    date_str = timestamp[:10].replace('-', '')
    return f"p_{date_str}_{hash_hex}"

def collect_from_sessions(hours=24, dry_run=False):
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
        messages = extract_user_messages(session_file, hours)
        all_messages.extend(messages)
    
    print(f"💬 提取到 {len(all_messages)} 条用户消息")
    
    # 转换为 pending 格式
    new_count = 0
    for msg in all_messages:
        msg_id = generate_id(msg['content'], msg['timestamp'])
        
        # 去重
        if msg_id in existing_ids:
            continue
        
        pending_item = {
            'id': msg_id,
            'content': msg['content'],
            'source': 'user',
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
    
    args = parser.parse_args()
    
    try:
        new_count = collect_from_sessions(args.hours, args.dry_run)
        sys.exit(0 if new_count >= 0 else 1)
    except Exception as e:
        print(f"❌ 收集失败: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
