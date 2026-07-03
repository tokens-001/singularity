#!/usr/bin/env python3
"""测试状态采样器功能。"""

import json
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from singularity.observer.state_sampler import StateSampler, sample_current_state

def test_basic_functionality():
    """测试基本功能。"""
    print("测试状态采样器的基本功能...")
    
    # 测试快速采样函数
    json_result = sample_current_state()
    print(f"快速采样结果长度: {len(json_result)}")
    
    # 解析JSON结果以确保其有效性
    try:
        parsed = json.loads(json_result)
        print("✓ JSON 格式正确")
        print(f"  时间戳: {parsed['timestamp']}")
        print(f"  队列待处理任务: {parsed['queues']['pending_tasks']}")
        print(f"  正在运行任务: {parsed['queues']['running_tasks']}")
        print(f"  CPU使用率: {parsed['resources']['cpu_percent']}%")
        print(f"  内存使用率: {parsed['resources']['memory_percent']}%")
    except json.JSONDecodeError as e:
        print(f"✗ JSON 解析失败: {e}")
        return False
    
    print("✓ 状态采样器基本功能测试通过\n")
    return True

def test_sampler_with_mock_components():
    """测试带有模拟组件的状态采样器。"""
    print("测试带有模拟组件的状态采样器...")
    
    # 创建一个基本的状态采样器实例
    sampler = StateSampler()
    
    # 生成JSON输出
    json_result = sampler.to_json()
    
    try:
        parsed = json.loads(json_result)
        print("✓ 采样器实例JSON格式正确")
        print(f"  采样器运行时间: {parsed['uptime_seconds']:.2f}s")
        print(f"  系统信息键数: {len(parsed['system_info'])}")
        print(f"  执行器数量: {len(parsed['executors'])}")
    except json.JSONDecodeError as e:
        print(f"✗ JSON 解析失败: {e}")
        return False
    
    print("✓ 模拟组件测试通过\n")
    return True

if __name__ == "__main__":
    print("开始测试状态采样器...\n")
    
    success = True
    success &= test_basic_functionality()
    success &= test_sampler_with_mock_components()
    
    if success:
        print("✓ 所有测试通过！")
    else:
        print("✗ 测试失败！")
        sys.exit(1)