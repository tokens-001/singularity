"""状态采样器 —— 从同进程全局状态采集实时指标。

提供结构化的系统状态快照，包括：
  - 队列长度与状态
  - 当前执行的任务
  - 系统资源占用（CPU、内存、磁盘）
  - 执行器状态
  - 各组件运行状态
"""

from __future__ import annotations

import json
import os
import psutil
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional
from datetime import datetime

from ..scheduler.dispatcher import Dispatcher
from ..scheduler.tracker import TaskTracker
from ..scheduler.executor._base import BaseExecutor


@dataclass
class QueueMetrics:
    """队列指标数据结构。"""
    pending_tasks: int
    running_tasks: int
    completed_tasks: int
    failed_tasks: int
    queue_depth: int
    avg_queue_time: float
    max_queue_time: float


@dataclass
class TaskStatus:
    """当前任务状态数据结构。"""
    id: str
    name: str
    status: str
    priority: str
    assigned_executor: str
    start_time: float
    duration: float
    progress: float
    estimated_completion: float


@dataclass
class ResourceUsage:
    """系统资源使用情况数据结构。"""
    cpu_percent: float
    memory_percent: float
    memory_used_mb: float
    memory_total_mb: float
    disk_percent: float
    network_sent_mb: float
    network_recv_mb: float
    process_count: int
    thread_count: int


@dataclass
class ExecutorStatus:
    """执行器状态数据结构。"""
    name: str
    type: str
    is_active: bool
    task_count: int
    queue_length: int
    busy_percentage: float
    uptime_seconds: float
    errors_count: int


@dataclass
class SystemState:
    """完整的系统状态数据结构。"""
    timestamp: float
    uptime_seconds: float
    queues: QueueMetrics
    current_tasks: List[TaskStatus]
    resources: ResourceUsage
    executors: List[ExecutorStatus]
    system_info: Dict[str, Any]


class StateSampler:
    """状态采样器，用于定期采集系统的运行状态。"""

    def __init__(self, 
                 dispatcher: Optional[Dispatcher] = None,
                 task_tracker: Optional[TaskTracker] = None,
                 executors: Optional[List[BaseExecutor]] = None):
        """
        初始化状态采样器
        
        Args:
            dispatcher: 调度器实例
            task_tracker: 任务跟踪器实例
            executors: 执行器列表
        """
        self.dispatcher = dispatcher
        self.task_tracker = task_tracker
        self.executors = executors or []
        self.start_time = time.time()
        self._lock = threading.Lock()

    def sample_state(self) -> SystemState:
        """采集当前系统状态并返回结构化数据。"""
        with self._lock:
            return SystemState(
                timestamp=time.time(),
                uptime_seconds=time.time() - self.start_time,
                queues=self._sample_queues(),
                current_tasks=self._sample_current_tasks(),
                resources=self._sample_resources(),
                executors=self._sample_executors(),
                system_info=self._sample_system_info()
            )

    def _sample_queues(self) -> QueueMetrics:
        """采样队列指标。"""
        # 获取队列统计信息
        pending = 0
        running = 0
        completed = 0
        failed = 0
        queue_depth = 0
        avg_queue_time = 0.0
        max_queue_time = 0.0

        if self.dispatcher:
            # 尝试从调度器获取队列状态
            pending = getattr(self.dispatcher, 'pending_tasks_count', 0)
            queue_depth = getattr(self.dispatcher, 'queue_depth', 0)
            
            # 获取任务统计
            if hasattr(self.dispatcher, 'task_stats'):
                stats = self.dispatcher.task_stats
                completed = stats.get('completed', 0)
                failed = stats.get('failed', 0)

        if self.task_tracker:
            # 获取任务跟踪器中的任务状态
            running = len([t for t in self.task_tracker.active_tasks if t.status == 'running'])
            completed = getattr(self.task_tracker, 'completed_tasks_count', 0)
            failed = getattr(self.task_tracker, 'failed_tasks_count', 0)

        return QueueMetrics(
            pending_tasks=pending,
            running_tasks=running,
            completed_tasks=completed,
            failed_tasks=failed,
            queue_depth=queue_depth,
            avg_queue_time=avg_queue_time,
            max_queue_time=max_queue_time
        )

    def _sample_current_tasks(self) -> List[TaskStatus]:
        """采样当前正在执行的任务。"""
        tasks = []

        if self.task_tracker:
            # 获取当前活动任务
            active_tasks = getattr(self.task_tracker, 'active_tasks', [])
            for task in active_tasks:
                try:
                    task_status = TaskStatus(
                        id=getattr(task, 'id', 'unknown'),
                        name=getattr(task, 'name', 'unnamed'),
                        status=getattr(task, 'status', 'unknown'),
                        priority=getattr(task, 'priority', 'normal'),
                        assigned_executor=getattr(task, 'executor_name', 'none'),
                        start_time=getattr(task, 'start_time', 0),
                        duration=time.time() - getattr(task, 'start_time', time.time()),
                        progress=getattr(task, 'progress', 0.0),
                        estimated_completion=getattr(task, 'estimated_completion', 0.0)
                    )
                    tasks.append(task_status)
                except Exception:
                    # 如果某个任务对象格式不正确，跳过它
                    continue

        # 如果没有任务跟踪器，尝试从调度器获取当前任务
        elif self.dispatcher and hasattr(self.dispatcher, 'current_tasks'):
            current_tasks = self.dispatcher.current_tasks
            for task in current_tasks:
                try:
                    task_status = TaskStatus(
                        id=getattr(task, 'id', 'unknown'),
                        name=getattr(task, 'name', 'unnamed'),
                        status=getattr(task, 'status', 'unknown'),
                        priority=getattr(task, 'priority', 'normal'),
                        assigned_executor=getattr(task, 'executor_name', 'none'),
                        start_time=getattr(task, 'start_time', 0),
                        duration=time.time() - getattr(task, 'start_time', time.time()),
                        progress=getattr(task, 'progress', 0.0),
                        estimated_completion=getattr(task, 'estimated_completion', 0.0)
                    )
                    tasks.append(task_status)
                except Exception:
                    continue

        return tasks

    def _sample_resources(self) -> ResourceUsage:
        """采样系统资源使用情况。"""
        # CPU 使用率
        cpu_percent = psutil.cpu_percent(interval=0.1)

        # 内存使用情况
        memory = psutil.virtual_memory()
        memory_used_mb = memory.used / (1024 * 1024)
        memory_total_mb = memory.total / (1024 * 1024)

        # 磁盘使用情况
        disk_usage = psutil.disk_usage('/')
        disk_percent = (disk_usage.used / disk_usage.total) * 100

        # 网络使用情况
        net_io = psutil.net_io_counters()
        network_sent_mb = net_io.bytes_sent / (1024 * 1024)
        network_recv_mb = net_io.bytes_recv / (1024 * 1024)

        # 进程和线程计数
        process_count = len(psutil.pids())
        thread_count = threading.active_count()

        return ResourceUsage(
            cpu_percent=cpu_percent,
            memory_percent=memory.percent,
            memory_used_mb=memory_used_mb,
            memory_total_mb=memory_total_mb,
            disk_percent=disk_percent,
            network_sent_mb=network_sent_mb,
            network_recv_mb=network_recv_mb,
            process_count=process_count,
            thread_count=thread_count
        )

    def _sample_executors(self) -> List[ExecutorStatus]:
        """采样执行器状态。"""
        executor_statuses = []

        if self.executors:
            for executor in self.executors:
                try:
                    # 获取执行器的基本信息
                    name = getattr(executor, 'name', 'unknown')
                    executor_type = getattr(executor, '__class__', type(executor)).__name__
                    
                    # 检查执行器是否活跃
                    is_active = getattr(executor, 'is_active', True)
                    
                    # 获取任务计数
                    task_count = getattr(executor, 'task_count', 0)
                    queue_length = getattr(executor, 'queue_length', 0)
                    
                    # 计算繁忙程度
                    busy_percentage = getattr(executor, 'busy_percentage', 0.0)
                    
                    # 计算正常运行时间
                    start_time = getattr(executor, 'start_time', time.time())
                    uptime_seconds = time.time() - start_time
                    
                    # 错误计数
                    errors_count = getattr(executor, 'errors_count', 0)

                    executor_status = ExecutorStatus(
                        name=name,
                        type=executor_type,
                        is_active=is_active,
                        task_count=task_count,
                        queue_length=queue_length,
                        busy_percentage=busy_percentage,
                        uptime_seconds=uptime_seconds,
                        errors_count=errors_count
                    )
                    executor_statuses.append(executor_status)
                except Exception as e:
                    # 如果某个执行器无法采样，记录错误但继续处理其他执行器
                    print(f"无法采样执行器状态: {e}")
                    continue

        return executor_statuses

    def _sample_system_info(self) -> Dict[str, Any]:
        """采样系统基础信息。"""
        return {
            "hostname": os.uname().nodename if hasattr(os, 'uname') else 'unknown',
            "platform": os.name,
            "python_version": f"{os.sys.version_info.major}.{os.sys.version_info.minor}.{os.sys.version_info.micro}",
            "pid": os.getpid(),
            "current_time": datetime.now().isoformat(),
            "load_average": os.getloadavg() if hasattr(os, 'getloadavg') else (0, 0, 0)
        }

    def to_json(self) -> str:
        """将当前状态采样结果转换为JSON字符串。"""
        state = self.sample_state()
        
        # 将数据类转换为字典
        result = {
            "timestamp": state.timestamp,
            "uptime_seconds": state.uptime_seconds,
            "queues": asdict(state.queues),
            "current_tasks": [asdict(task) for task in state.current_tasks],
            "resources": asdict(state.resources),
            "executors": [asdict(executor) for executor in state.executors],
            "system_info": state.system_info
        }
        
        return json.dumps(result, ensure_ascii=False, default=str)

    def get_metrics_for_monitoring(self) -> Dict[str, Any]:
        """为监控系统返回简化的指标数据。"""
        state = self.sample_state()
        
        return {
            "timestamp": state.timestamp,
            "cpu_percent": state.resources.cpu_percent,
            "memory_percent": state.resources.memory_percent,
            "disk_percent": state.resources.disk_percent,
            "pending_tasks": state.queues.pending_tasks,
            "running_tasks": state.queues.running_tasks,
            "queue_depth": state.queues.queue_depth,
            "active_executors": len([ex for ex in state.executors if ex.is_active]),
            "total_executors": len(state.executors),
            "thread_count": state.resources.thread_count,
            "process_count": state.resources.process_count
        }


# 全局状态采样器实例
_global_sampler: Optional[StateSampler] = None


def get_global_sampler() -> Optional[StateSampler]:
    """获取全局状态采样器实例。"""
    return _global_sampler


def init_global_sampler(dispatcher: Optional[Dispatcher] = None,
                       task_tracker: Optional[TaskTracker] = None,
                       executors: Optional[List[BaseExecutor]] = None) -> StateSampler:
    """初始化全局状态采样器。"""
    global _global_sampler
    _global_sampler = StateSampler(
        dispatcher=dispatcher,
        task_tracker=task_tracker,
        executors=executors
    )
    return _global_sampler


def sample_current_state() -> str:
    """快速采样当前系统状态并返回JSON字符串。"""
    sampler = get_global_sampler()
    if sampler:
        return sampler.to_json()
    else:
        # 返回一个基本的状态信息
        basic_state = {
            "timestamp": time.time(),
            "uptime_seconds": 0,
            "queues": {
                "pending_tasks": 0,
                "running_tasks": 0,
                "completed_tasks": 0,
                "failed_tasks": 0,
                "queue_depth": 0,
                "avg_queue_time": 0.0,
                "max_queue_time": 0.0
            },
            "current_tasks": [],
            "resources": {
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "memory_percent": psutil.virtual_memory().percent,
                "memory_used_mb": psutil.virtual_memory().used / (1024 * 1024),
                "memory_total_mb": psutil.virtual_memory().total / (1024 * 1024),
                "disk_percent": psutil.disk_usage('/').percent,
                "network_sent_mb": 0,
                "network_recv_mb": 0,
                "process_count": len(psutil.pids()),
                "thread_count": threading.active_count()
            },
            "executors": [],
            "system_info": {
                "hostname": os.uname().nodename if hasattr(os, 'uname') else 'unknown',
                "platform": os.name,
                "python_version": f"{os.sys.version_info.major}.{os.sys.version_info.minor}.{os.sys.version_info.micro}",
                "pid": os.getpid(),
                "current_time": datetime.now().isoformat()
            }
        }
        return json.dumps(basic_state, ensure_ascii=False, default=str)