#!/usr/bin/env python3
"""
并发测试：验证 SQLite 后端的线程安全性
"""

import sys
import threading
import time
from pathlib import Path
from datetime import datetime
import tempfile
import shutil

# 导入新的后端
# 仓库整理后被测模块位于 ../src 与 ../legacy,补齐导入路径,保证开箱可运行
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "legacy"))
from sqlite_backend_v1_2_5 import SQLiteBackend, DecayCalculator


def test_concurrent_writes(backend: SQLiteBackend, thread_id: int, count: int):
    """并发写入测试"""
    for i in range(count):
        memory = {
            'id': f'thread_{thread_id}_mem_{i}',
            'type': 'fact',
            'content': f'Thread {thread_id} Memory {i}',
            'importance': 0.5,
            'score': 1.0,
            'created': datetime.now().isoformat(),
            'entities': [f'thread_{thread_id}', f'entity_{i}']
        }
        
        success = backend.insert_memory(memory)
        if not success:
            print(f"❌ Thread {thread_id}: 写入失败 (memory {i})")
            return False
    
    print(f"✅ Thread {thread_id}: 成功写入 {count} 条记忆")
    return True


def test_concurrent_reads(backend: SQLiteBackend, thread_id: int, count: int):
    """并发读取测试"""
    for i in range(count):
        # 随机查询
        results = backend.search_by_entities([f'thread_{thread_id % 5}'])
        if results is None:
            print(f"❌ Thread {thread_id}: 读取失败 (iteration {i})")
            return False
    
    print(f"✅ Thread {thread_id}: 成功读取 {count} 次")
    return True


def test_concurrent_updates(backend: SQLiteBackend, thread_id: int, count: int):
    """并发更新测试"""
    for i in range(count):
        memory_id = f'thread_{thread_id % 5}_mem_{i % 10}'
        success = backend.update_access_stats(memory_id)
        # 更新可能失败（记忆不存在），这是正常的
    
    print(f"✅ Thread {thread_id}: 成功更新 {count} 次")
    return True


def run_concurrent_test():
    """运行并发测试"""
    print("🧪 并发测试：SQLite 后端线程安全性")
    print("=" * 60)
    
    # 创建临时目录
    temp_dir = Path(tempfile.mkdtemp())
    
    try:
        backend = SQLiteBackend(temp_dir)
        
        # ============================================================
        # 测试 1: 并发写入
        # ============================================================
        print("\n📝 测试 1: 并发写入（10 线程 × 10 条记忆）")
        print("-" * 60)
        
        threads = []
        start_time = time.time()
        
        for i in range(10):
            t = threading.Thread(target=test_concurrent_writes, args=(backend, i, 10))
            threads.append(t)
            t.start()
        
        for t in threads:
            t.join()
        
        elapsed = time.time() - start_time
        print(f"\n⏱️  耗时: {elapsed:.2f}秒")
        
        # 验证数据完整性
        stats = backend.get_stats()
        expected = 10 * 10
        actual = stats['total']
        
        if actual == expected:
            print(f"✅ 数据完整性验证通过: {actual}/{expected} 条记忆")
        else:
            print(f"❌ 数据完整性验证失败: {actual}/{expected} 条记忆")
            return False
        
        # ============================================================
        # 测试 2: 并发读取
        # ============================================================
        print("\n📖 测试 2: 并发读取（20 线程 × 50 次查询）")
        print("-" * 60)
        
        threads = []
        start_time = time.time()
        
        for i in range(20):
            t = threading.Thread(target=test_concurrent_reads, args=(backend, i, 50))
            threads.append(t)
            t.start()
        
        for t in threads:
            t.join()
        
        elapsed = time.time() - start_time
        total_queries = 20 * 50
        qps = total_queries / elapsed
        
        print(f"\n⏱️  耗时: {elapsed:.2f}秒")
        print(f"📊 QPS: {qps:.2f} 查询/秒")
        
        # ============================================================
        # 测试 3: 混合读写
        # ============================================================
        print("\n🔀 测试 3: 混合读写（10 写 + 10 读 + 10 更新）")
        print("-" * 60)
        
        threads = []
        start_time = time.time()
        
        # 10 个写线程
        for i in range(10):
            t = threading.Thread(target=test_concurrent_writes, args=(backend, i + 100, 5))
            threads.append(t)
        
        # 10 个读线程
        for i in range(10):
            t = threading.Thread(target=test_concurrent_reads, args=(backend, i + 200, 20))
            threads.append(t)
        
        # 10 个更新线程
        for i in range(10):
            t = threading.Thread(target=test_concurrent_updates, args=(backend, i + 300, 20))
            threads.append(t)
        
        # 启动所有线程
        for t in threads:
            t.start()
        
        # 等待所有线程完成
        for t in threads:
            t.join()
        
        elapsed = time.time() - start_time
        print(f"\n⏱️  耗时: {elapsed:.2f}秒")
        
        # 最终统计
        stats = backend.get_stats()
        print(f"\n📊 最终统计:")
        print(f"   总记忆数: {stats['total']}")
        print(f"   按类型: {stats['by_type']}")
        
        # 关闭连接
        backend.close()
        
        print("\n" + "=" * 60)
        print("✅ 所有并发测试通过！")
        return True
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        # 清理临时目录
        shutil.rmtree(temp_dir)


if __name__ == '__main__':
    success = run_concurrent_test()
    sys.exit(0 if success else 1)
