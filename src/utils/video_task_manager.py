""" 
视频生成异步任务队列管理器
支持任务排队、状态跟踪和结果查询
"""
import asyncio
import queue
import threading
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from src.utils.logger import logger
from src.utils import helper
from src.utils.deferred_delete import dequeue_path, enqueue_path, enqueue_paths
from src.utils.isolated_process import IsolatedProcessRunner, ProcessTimedOut
from src.utils.video_phase_process import run_video_phase
from src.utils.video_task_store import (
    get_completed_by_draft_id,
    prune_if_needed,
    save_completed_results_batch,
)
import src.pyJianYingDraft as draft
import config
import os
import sys
import subprocess
import json
import time

# draft_content.json 中 duration 为微秒；低于 3 秒视为空草稿，不进入剪映导出
MIN_DRAFT_EXPORT_DURATION_US = 3 * 1_000_000

# gen_video：同时下载草稿的最大并发，超出部分以协程等待，不占执行线程
DRAFT_DOWNLOAD_MAX_CONCURRENT = 3

# gen_video：上传到对象存储的最大并发
OBJECT_STORAGE_UPLOAD_MAX_CONCURRENT = 2

# 所有截止时间由父进程计时，包含控制器初始化及卡死的 COM 调用。
PHASE_TIMEOUT_SECONDS = {"download": 900.0, "probe": 15.0, "export": 360.0, "upload": 600.0}
MAX_IN_FLIGHT_TASKS = 32
SHUTDOWN_TIMEOUT_SECONDS = 15.0

# 路径未取得是导出状态契约失败，重复整个 UI 流程不会补齐证据。
# 保留常量供兼容调用，但不再对 rename None 重试。
EXPORT_RENAME_SRC_NONE_MAX_RETRIES = 0
EXPORT_RENAME_SRC_NONE_ERROR_MARKER = (
    "rename: src should be string, bytes or os.PathLike, not NoneType"
)
# UI Automation COM 瞬时错误（窗口切换/元素失效）时，额外重试导出阶段的次数
EXPORT_COM_UIA_MAX_RETRIES = 2

# 如果是Linux系统，则不导入uiautomation，并避免执行相关代码
try:
    from uiautomation import UIAutomationInitializerInThread  # type: ignore
except ImportError:
    # 在缺少依赖的系统上创建一个占位符
    class UIAutomationInitializerInThread:  # type: ignore
        def __enter__(self):
            pass
        def __exit__(self, *args):
            pass


class TaskStatus(Enum):
    """任务状态枚举"""
    PENDING = "pending"      # 等待中
    PROCESSING = "processing"  # 处理中
    COMPLETED = "completed"   # 已完成
    FAILED = "failed"        # 失败


@dataclass
class VideoGenTask:
    """视频生成任务数据类"""
    draft_url: str
    draft_id: str
    status: TaskStatus
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    video_url: str = ""
    error_message: str = ""
    progress: int = 0  # 进度百分比 0-100
    api_key: Optional[str] = None  # 存储API密钥用于计费
    outfile: str = ""  # 导出目标路径，在下载阶段生成
    export_outfile_history: List[str] = field(default_factory=list)  # 含重试产生的历史路径
    phase: str = "queued"
    phase_started_at: Optional[datetime] = None
    node_generation: int = 0


class VideoGenTaskManager:
    """视频生成任务管理器 - 单例模式

    父进程负责队列与状态；下载、RPA、上传分别在受监管子进程内执行。
    异步信号量限制 3/1/2 并发，等待任务不占用线程；每台桌面仅运行一个服务进程。
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        self._initialized = True
        
        # 任务存储：{draft_url: VideoGenTask}
        self.tasks: Dict[str, VideoGenTask] = {}
        # 跨线程任务队列（HTTP 线程 put、worker 线程 get）。不能用 asyncio.Queue：其在 __init__
        # 时绑定的 loop 与 worker 内 new_event_loop() 不一致，会触发 “bound to a different event loop”。
        self.task_queue: queue.Queue = queue.Queue()
        # 同步方法仍供子进程及既有单元测试调用；父进程不在此锁上排队。
        self.export_video_lock = threading.Lock()
        self._export_metrics_lock = threading.Lock()
        self._export_phase_active = 0
        # 工作线程
        self.worker_thread: Optional[threading.Thread] = None
        # 停止标志
        self.stop_flag = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self._worker_event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._in_flight: set[asyncio.Task] = set()
        self._task_coroutines: Dict[asyncio.Task, VideoGenTask] = {}
        self._process_runner = IsolatedProcessRunner()
        self._node_generation = 0
        self._node_error = ""
        self._worker_failure: Optional[BaseException] = None
        self._shutdown_results: Dict[str, Dict[str, Any]] = {}
        self._next_persist_at = 0.0
        logger.info(
            "VideoGenTaskManager initialized: isolated subprocesses; "
            "download concurrency=%s, RPA concurrency=1, upload concurrency=%s",
            DRAFT_DOWNLOAD_MAX_CONCURRENT, OBJECT_STORAGE_UPLOAD_MAX_CONCURRENT,
        )
    
    def submit_task(self, draft_url: str, api_key: str = None) -> None:
        with self._lifecycle_lock:
            if self.stop_flag.is_set():
                raise RuntimeError("[SERVICE_STOPPING] 服务正在停止，不再接收导出任务")
            self._submit_task(draft_url, api_key)

    def _submit_task(self, draft_url: str, api_key: str = None) -> None:
        """
        提交视频生成任务
        
        Args:
            draft_url: 草稿URL
            api_key: API密钥，用于计费
        """
        # 提取草稿ID
        draft_id = helper.get_url_param(draft_url, "draft_id")
        if not draft_id:
            raise ValueError("无效的草稿URL")
        
        # 检查是否已有相同草稿的任务在进行
        if draft_url in self.tasks:
            existing_task = self.tasks[draft_url]
            if existing_task.status in [TaskStatus.PENDING, TaskStatus.PROCESSING]:
                logger.info(f"Task already exists for draft_url: {draft_url}")
                return
        
        # 创建新任务
        task = VideoGenTask(
            draft_url=draft_url,
            draft_id=draft_id,
            status=TaskStatus.PENDING,
            created_at=datetime.now(),
            api_key=api_key,  # 存储API密钥用于计费
            node_generation=self._node_generation,
        )
        
        # 存储任务
        self.tasks[draft_url] = task
        
        self._add_task_to_queue_sync(task)
        
        # 启动工作线程（如果还没启动）
        self._ensure_worker_running()
        
        logger.info(f"Task submitted for draft_url: {draft_url}")
    
    def _add_task_to_queue_sync(self, task: VideoGenTask) -> None:
        """Thread-safe enqueue from FastAPI worker threads (or any thread)."""
        self.task_queue.put(task)
        logger.info(f"Task added to queue: {task.draft_url}")
    
    def get_task_status(self, draft_url: str) -> Optional[Dict[str, Any]]:
        """
        根据草稿URL获取任务状态
        
        Args:
            draft_url: 草稿URL
            
        Returns:
            任务状态信息，如果不存在返回None
        """
        # 热路径：本进程内提交过的任务始终在内存，避免每次轮询都抢 SQLite 全局锁
        task = self.tasks.get(draft_url)
        if task:
            return {
                "draft_url": task.draft_url,
                "status": task.status.value,
                "progress": task.progress,
                "video_url": task.video_url,
                "error_message": task.error_message,
                "created_at": task.created_at.isoformat(),
                "started_at": task.started_at.isoformat() if task.started_at else None,
                "completed_at": task.completed_at.isoformat() if task.completed_at else None,
                "phase": task.phase,
                "phase_started_at": task.phase_started_at.isoformat() if task.phase_started_at else None,
                "queue_position": self._export_queue_position(task),
                "node_error": self._node_error,
            }

        prune_if_needed()
        draft_id = helper.get_url_param(draft_url, "draft_id")
        return get_completed_by_draft_id(draft_id)

    def get_active_render_count(self) -> int:
        """
        当前进行中的云渲染草稿数量：排队(pending) + 渲染中(processing)。
        不含已完成(completed)、失败(failed)。
        """
        active = (TaskStatus.PENDING, TaskStatus.PROCESSING)
        with self._lifecycle_lock:
            return sum(1 for t in self.tasks.values() if t.status in active)

    def _export_queue_position(self, task: VideoGenTask) -> Optional[int]:
        if task.phase != "waiting_export":
            return None
        with self._lifecycle_lock:
            waiting = sorted(
                (t for t in self.tasks.values() if t.phase == "waiting_export"),
                key=lambda t: t.phase_started_at or t.created_at,
            )
        return next((i for i, t in enumerate(waiting, 1) if t is task), None)

    def _ensure_worker_running(self):
        """确保工作线程正在运行"""
        if self.worker_thread is None or not self.worker_thread.is_alive():
            if self.stop_flag.is_set():
                raise RuntimeError("[SERVICE_STOPPING] 任务管理器已停止，请重新启动服务")
            self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self.worker_thread.start()
            logger.info("Worker thread started")
    
    def _worker_loop(self):
        """工作线程主循环"""
        logger.info("Worker loop started")
        
        # 在工作线程中创建新的事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._worker_event_loop = loop
        self._download_slots = asyncio.Semaphore(DRAFT_DOWNLOAD_MAX_CONCURRENT)
        self._export_slot = asyncio.Semaphore(1)
        self._upload_slots = asyncio.Semaphore(OBJECT_STORAGE_UPLOAD_MAX_CONCURRENT)
        try:
            loop.run_until_complete(self._async_worker_loop())
        except BaseException as exc:
            self._worker_failure = exc
            self.stop_flag.set()
            logger.exception("Video worker shutdown failed: %r", exc)
        finally:
            self._worker_event_loop = None
            loop.close()
    
    async def _async_worker_loop(self):
        """队列消费不依赖任何线程池，RPA 卡死也能响应停止。"""
        try:
            while not self.stop_flag.is_set():
                while len(self._in_flight) < MAX_IN_FLIGHT_TASKS and not self.stop_flag.is_set():
                    try:
                        task = self.task_queue.get_nowait()
                    except queue.Empty:
                        break
                    if task.status in (TaskStatus.FAILED, TaskStatus.COMPLETED):
                        self.task_queue.task_done()
                        continue
                    t = asyncio.create_task(self._process_task(task))
                    self._in_flight.add(t)
                    self._task_coroutines[t] = task
                    t.add_done_callback(self._log_async_task_done)
                    self.task_queue.task_done()
                if self._shutdown_results and time.monotonic() >= self._next_persist_at:
                    self._flush_shutdown_results()
                await asyncio.sleep(0.05)
        finally:
            pending = list(self._in_flight)
            for t in pending:
                if not t.cancelling():
                    t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            try:
                await self._process_runner.stop()
            finally:
                with self._lifecycle_lock:
                    unfinished = [t for t in self.tasks.values()
                                  if t.status in (TaskStatus.PENDING, TaskStatus.PROCESSING)]
                for task in unfinished:
                    self._fail_task(task, "[SERVICE_STOPPING] 服务停止，任务已取消；草稿已保留")
                self._flush_shutdown_results()
                while not self.task_queue.empty():
                    try:
                        self.task_queue.get_nowait()
                        self.task_queue.task_done()
                    except queue.Empty:
                        break

    def _log_async_task_done(self, fut: asyncio.Task) -> None:
        self._in_flight.discard(fut)
        task = self._task_coroutines.pop(fut, None)
        try:
            fut.result()
        except asyncio.CancelledError:
            if task is not None:
                self._fail_task(task, task.error_message or "[SERVICE_STOPPING] 服务停止，任务已取消")
        except Exception as e:
            logger.exception(f"Async task finished with error: {e}")

    def _persist_terminal_task(self, task: VideoGenTask) -> None:
        """终态先入批量队列；取消回调内不逐条等待数据库锁。"""
        if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            return
        task.completed_at = datetime.now()
        self._shutdown_results[task.draft_id] = {
            "draft_id": task.draft_id, "draft_url": task.draft_url,
            "status": task.status.value, "progress": task.progress,
            "video_url": task.video_url, "error_message": task.error_message,
            "created_at": task.created_at, "started_at": task.started_at,
            "completed_at": task.completed_at,
        }

    def _flush_shutdown_results(self) -> None:
        """批量短等待落库；停止时仅在执行进程清理完毕后调用。"""
        if not self._shutdown_results:
            return
        try:
            save_completed_results_batch(list(self._shutdown_results.values()))
            self._shutdown_results.clear()
        except Exception as exc:
            self._next_persist_at = time.monotonic() + 1.0
            if self.stop_flag.is_set():
                logger.error(
                    "Shutdown task persistence failed; %s terminal results remain only "
                    "in memory and will not survive exit: %r",
                    len(self._shutdown_results), exc,
                )
            else:
                logger.warning("Terminal task persistence deferred; count=%s error=%r",
                               len(self._shutdown_results), exc)

    async def _run_upload_and_finalize(self, task: VideoGenTask) -> None:
        """上传也有硬截止时间，停止服务不等待 SDK 线程；不自动重试计费。"""
        self._set_phase(task, "waiting_upload")
        try:
            async with self._upload_slots:
                self._raise_if_stopping()
                self._set_phase(task, "uploading")
                video_url, error_message = await self._run_phase(task, "upload")
            self._raise_if_stopping()
            if video_url:
                task.status = TaskStatus.COMPLETED
                task.video_url = video_url
                task.progress = 100
                self._set_phase(task, "completed")
                # 子进程中的延迟删除队列不共享；仅父进程安排清理。
                self._cleanup_files(task)
                self._persist_terminal_task(task)
            else:
                self._fail_task(task, error_message or "[UPLOAD_FAILED] 视频上传失败")
        except Exception as e:
            self._fail_task(task, f"[UPLOAD_FAILED] {e}；上传/扣费结果可能未确认，未自动重试")

    @staticmethod
    def _set_phase(task: VideoGenTask, phase: str) -> None:
        task.phase = phase
        task.phase_started_at = datetime.now()
        logger.info("Video task phase: draft_id=%s phase=%s", task.draft_id, phase)

    def _raise_if_stopping(self) -> None:
        if self.stop_flag.is_set():
            raise asyncio.CancelledError()

    def _fail_task(self, task: VideoGenTask, message: str) -> None:
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            return
        task.status = TaskStatus.FAILED
        task.error_message = message
        task.progress = 0
        self._set_phase(task, "failed")
        # 失败/取消保留草稿及可能仍由剪映写入的输出，不擅自删除现场。
        self._persist_terminal_task(task)

    def _mark_node_unready(self, message: str) -> None:
        with self._lifecycle_lock:
            self._node_error = message
            self._node_generation += 1
            previous = [t for t in self.tasks.values()
                        if t.node_generation < self._node_generation
                        and t.status in (TaskStatus.PENDING, TaskStatus.PROCESSING)
                        and t.phase not in ("waiting_upload", "uploading")]
        current = asyncio.current_task()
        running = {id(task): coro for coro, task in self._task_coroutines.items()}
        reason = "[RPA_NODE_NOT_READY] 前一个导出异常，排队任务已停止；请恢复剪映首页后重新提交。" + message
        for task in previous:
            coro = running.get(id(task))
            if coro is current:
                continue
            task.error_message = reason
            if coro is not None and not coro.done():
                if not coro.cancelling():
                    coro.cancel()
            else:
                self._fail_task(task, reason)

    def _check_node_generation(self, task: VideoGenTask) -> None:
        if task.node_generation < self._node_generation:
            raise RuntimeError(
                "[RPA_NODE_NOT_READY] 前一个导出异常，当前排队任务已停止；"
                "请将剪映恢复到首页后重新提交。" + self._node_error
            )

    async def _run_phase(self, task: VideoGenTask, phase: str):
        self._raise_if_stopping()
        try:
            return await run_video_phase(
                self._process_runner, task, phase, PHASE_TIMEOUT_SECONDS[phase]
            )
        except ProcessTimedOut as exc:
            raise RuntimeError(
                f"[{phase.upper()}_TIMEOUT] {phase} 阶段超过 {exc.timeout:g} 秒，"
                "执行子进程已终止；未关闭剪映，草稿已保留"
            ) from exc

    async def _process_task(self, task: VideoGenTask):
        """
        等待阶段仅占协程；所有同步执行受子进程截止时间约束。
        """
        logger.info(f"Processing task: {task.draft_url}")

        task.status = TaskStatus.PROCESSING
        task.started_at = datetime.now()
        task.progress = 10

        try:
            self._check_node_generation(task)
            self._set_phase(task, "waiting_download")
            async with self._download_slots:
                self._raise_if_stopping()
                self._check_node_generation(task)
                self._set_phase(task, "downloading")
                # 延迟删除是父进程内队列，不能只在下载子进程里取消。
                dequeue_path(os.path.join(config.DRAFT_SAVE_PATH, task.draft_id))
                prep_error = await self._run_phase(task, "download")
            if prep_error:
                self._fail_task(task, prep_error)
                return
            self._set_phase(task, "waiting_export")
            async with self._export_slot:
                self._raise_if_stopping()
                self._check_node_generation(task)
                try:
                    # 即使服务重启也先确认主页；不盲目关闭前一次弹窗/导出。
                    self._set_phase(task, "checking_node")
                    if not await self._run_phase(task, "probe"):
                        raise RuntimeError("[RPA_NODE_NOT_READY] 剪映未就绪，请恢复到首页后重试")
                    self._node_error = ""
                    self._set_phase(task, "exporting")
                    export_error = await self._run_phase(task, "export")
                    if export_error:
                        raise RuntimeError(export_error)
                    if not task.outfile or not os.path.isfile(task.outfile) or os.path.getsize(task.outfile) <= 0:
                        raise RuntimeError("[EXPORT_OUTPUT_MISSING] 导出完成但没有有效输出文件")
                except Exception as exc:
                    self._mark_node_unready(str(exc))
                    raise
            await self._run_upload_and_finalize(task)
        except asyncio.CancelledError:
            message = task.error_message or "[SERVICE_STOPPING] 服务停止，任务已取消；草稿已保留"
            if task.phase == "uploading":
                message += "；上传/扣费结果未确认，未自动重试"
            self._fail_task(task, message)
            raise
        except Exception as e:
            self._fail_task(task, str(e))
    
    def _check_draft_duration(self, task: VideoGenTask) -> bool:
        """
        检查草稿中的视频时长是否满足导出要求（draft_content.duration 单位为微秒）。
        时长不大于 0 或小于 3 秒均视为无效。

        Returns:
            bool: 是否允许继续导出
        """
        try:
            # 构建草稿内容文件路径
            draft_content_path = os.path.join(config.DRAFT_SAVE_PATH, task.draft_id, "draft_content.json")
            
            # 检查文件是否存在
            if not os.path.exists(draft_content_path):
                logger.error(
                    "draft_content.json not found: path=%s draft_id=%s",
                    draft_content_path,
                    task.draft_id,
                )
                return False
            
            # 读取并解析JSON文件
            with open(draft_content_path, 'r', encoding='utf-8') as f:
                draft_content = json.load(f)
            
            # 获取时长
            duration = draft_content.get("duration", 0)
            
            if duration <= 0:
                logger.error(
                    "draft duration invalid (<=0): duration_us=%s draft_id=%s",
                    duration,
                    task.draft_id,
                )
                return False

            if duration < MIN_DRAFT_EXPORT_DURATION_US:
                logger.error(
                    "draft duration below minimum export length (<3s): duration_us=%s "
                    "draft_id=%s",
                    duration,
                    task.draft_id,
                )
                return False
            
            logger.info(
                "draft duration check passed: duration_us=%s draft_id=%s",
                duration,
                task.draft_id,
            )
            return True
            
        except json.JSONDecodeError as e:
            logger.error(
                "failed to parse draft_content.json: %s draft_id=%s",
                e,
                task.draft_id,
            )
            return False
        except Exception as e:
            logger.error(
                "draft duration check error: %s draft_id=%s",
                e,
                task.draft_id,
            )
            return False

    def _phase_download_and_prepare(self, task: VideoGenTask) -> str:
        """
        下载草稿并校验时长（可并发执行）。

        Returns:
            错误信息，成功时返回空字符串。
        """
        try:
            task.progress = 20
            self._assign_export_outfile(task)

            if not sys.platform.startswith("win"):
                return "视频生成功能仅在Windows系统上可用"

            task.progress = 30

            download_error = self._download_draft(task)
            if download_error:
                logger.error(
                    "draft download failed: draft_url=%s error=%s",
                    task.draft_url,
                    download_error,
                )
                # 下载失败必须在此返回，禁止进入剪映导出流程
                return download_error

            ready_error = self._ensure_local_draft_ready(task)
            if ready_error:
                logger.error(
                    "local draft not ready after download: draft_id=%s error=%s",
                    task.draft_id,
                    ready_error,
                )
                return ready_error

            if not self._check_draft_duration(task):
                logger.error(
                    "draft duration check failed (empty or too short): draft_id=%s",
                    task.draft_id,
                )
                return f"草稿中视频时长不大于3秒，请检查草稿内容: {task.draft_id}"

            task.progress = 40
            return ""
        except Exception as exc:
            logger.exception(
                "Draft download/prepare failed: draft_id=%s, error=%s",
                task.draft_id,
                exc,
            )
            return f"草稿下载失败: {exc}"

    @staticmethod
    def _is_export_rename_src_none_error(error_message: str) -> bool:
        """是否为 original_path 为 None 触发的 shutil.move/rename 失败。"""
        return EXPORT_RENAME_SRC_NONE_ERROR_MARKER in error_message

    @staticmethod
    def _is_export_com_uia_error(error_message: str) -> bool:
        """是否为 Windows UI Automation 的瞬时 COM 错误（可重试）。"""
        from src.pyJianYingDraft.jianying_controller import is_com_uia_error

        return is_com_uia_error(Exception(error_message))

    @staticmethod
    def _is_export_retryable_error(error_message: str) -> bool:
        if VideoGenTaskManager._is_export_rename_src_none_error(error_message):
            return False
        return VideoGenTaskManager._is_export_com_uia_error(error_message)

    @staticmethod
    def _assign_export_outfile(task: VideoGenTask) -> str:
        """分配新的导出 mp4 路径并记入历史，便于上传后统一清理。"""
        path = os.path.join(config.DRAFT_DIR, f"{helper.gen_unique_id()}.mp4")
        task.outfile = path
        if path not in task.export_outfile_history:
            task.export_outfile_history.append(path)
        return path

    def _prepare_export_retry_outfile(self, task: VideoGenTask) -> None:
        """导出重试前生成新的 outfile，并将旧 mp4 加入延迟删除队列。"""
        old_outfile = task.outfile
        self._assign_export_outfile(task)
        if old_outfile:
            enqueue_path(old_outfile, is_dir=False)

    def _phase_export_only(self, task: VideoGenTask) -> str:
        """
        在受监管子进程内同步执行剪映导出；全局串行由父进程信号量保证。
        只重试瞬时 COM/UIA 错误。路径缺失、状态停滞、导出不完整等确定性
        失败立即返回，避免重复整套导出流程掩盖最早失败阶段。

        Returns:
            错误信息，成功时返回空字符串。
        """
        with self._export_metrics_lock:
            self._export_phase_active += 1
            export_phase_concurrent = self._export_phase_active
        logger.info(
            "Export phase entered (waiting for lock or exporting): "
            "concurrent=%s draft_id=%s",
            export_phase_concurrent,
            task.draft_id,
        )
        try:
            max_attempts = 1 + EXPORT_COM_UIA_MAX_RETRIES
            last_error = ""

            for attempt in range(1, max_attempts + 1):
                try:
                    if not self._export_video(task, task.outfile):
                        return "导出草稿失败"
                    return ""
                except Exception as exc:
                    last_error = f"导出草稿失败: {exc}"
                    logger.exception(
                        "Export draft failed: draft_id=%s attempt=%d/%d error=%s",
                        task.draft_id,
                        attempt,
                        max_attempts,
                        exc,
                    )
                    if not self._is_export_retryable_error(last_error):
                        return last_error
                    if attempt >= max_attempts:
                        return last_error

                    logger.warning(
                        "Export COM/UIA transient error, retrying export without "
                        "re-downloading draft: draft_id=%s retry=%d/%d",
                        task.draft_id,
                        attempt,
                        EXPORT_COM_UIA_MAX_RETRIES,
                    )
                    self._prepare_export_retry_outfile(task)

            return last_error
        finally:
            with self._export_metrics_lock:
                self._export_phase_active -= 1
                export_phase_concurrent = self._export_phase_active
            logger.info(
                "Export phase exited: concurrent=%s draft_id=%s",
                export_phase_concurrent,
                task.draft_id,
            )

    def _phase_cos_upload_finalize(self, task: VideoGenTask) -> Tuple[str, str]:
        """
        子进程内同步上传和扣费，最多 2 路并发由父进程限制。
        子进程的延迟删除队列不共享；最终文件清理由父进程在确认成功后执行。
        """
        try:
            task.progress = 95
            upload_url, upload_failed = self._upload_video_to_cos(task.outfile)
            self._calculate_and_charge(task, task.outfile)
            return self._handle_result(upload_url, upload_failed)
        except Exception as exc:
            logger.exception(
                f"Export draft failed: draft_id={task.draft_id}, error={exc}"
            )
            return "", f"导出草稿失败: {exc}"
        finally:
            self._cleanup_files(task)
    
    def _download_draft(self, task: VideoGenTask) -> str:
        """
        下载草稿

        Args:
            task: 视频生成任务

        Returns:
            错误信息，成功时返回空字符串。
            失败时一律返回以「草稿下载失败」开头的文案。
        """
        logger.info(f"Start downloading draft before export: {task.draft_url}")
        from src.utils.draft_downloader import (
            download_draft_with_result,
            format_draft_download_failure_message,
        )

        try:
            result = download_draft_with_result(task.draft_url)
        except Exception as exc:
            logger.exception(
                "Draft download raised unexpectedly: draft_url=%s error=%s",
                task.draft_url,
                exc,
            )
            return f"草稿下载失败: {exc}"

        if result.ok:
            logger.info(f"Draft downloaded successfully: {task.draft_url}")
            return ""

        error_message = format_draft_download_failure_message(result, task.draft_url)
        logger.error(
            "Failed to download draft: draft_url=%s kind=%s detail=%s",
            task.draft_url,
            result.kind.value if result.kind else None,
            result.detail,
        )
        return error_message

    def _ensure_local_draft_ready(self, task: VideoGenTask) -> str:
        """下载后校验本地草稿是否可导出；失败返回「草稿下载失败」文案。"""
        from src.utils.draft_downloader import (
            format_draft_download_failure_message,
            verify_local_draft_ready,
        )

        result = verify_local_draft_ready(task.draft_id)
        if result.ok:
            return ""
        return format_draft_download_failure_message(result, task.draft_url)
    
    def _export_video(self, task: VideoGenTask, outfile: str) -> bool:
        """
        导出视频
        
        Args:
            task: 视频生成任务
            outfile: 输出文件路径
        
        Returns:
            bool: 导出是否成功
        """
        # 使用专用锁确保任何时候只有一个线程执行导出视频操作
        logger.info(
            "Waiting for export_video_lock: draft_id=%s",
            task.draft_id,
        )
        with self.export_video_lock:
            logger.info(f"Begin to export draft: {task.draft_id} -> {outfile}")
            
            # 更新进度
            task.progress = 50
            
            # 检查JianyingController是否可用
            if draft.JianyingController is None:
                if sys.platform != "win32":
                    error_msg = "剪映自动导出功能仅在Windows平台可用"
                    logger.error(
                        "JianyingController unavailable: requires Windows platform"
                    )
                else:
                    error_msg = "缺少Windows依赖，请安装: pip install capcut-mate[windows]"
                    logger.error(
                        "JianyingController unavailable: install windows extras "
                        "(pip install capcut-mate[windows])"
                    )
                raise RuntimeError(error_msg)
            
            from src.utils.jianying_export_cleanup import recover_from_export_failure

            with UIAutomationInitializerInThread():
                # 此前需要将剪映打开，并位于目录页
                ctrl = draft.JianyingController()

                # 更新进度
                task.progress = 70

                # 未找到时由 find_and_click_draft 对本地目录 robocopy 后重试（最多 6 次）
                draft_dir = os.path.join(config.DRAFT_SAVE_PATH, task.draft_id)
                try:
                    ctrl.export_draft(task.draft_id, outfile, draft_dir=draft_dir)
                except Exception as exc:
                    logger.error(
                        "Export draft failed: draft_id=%s, error=%r",
                        task.draft_id,
                        exc,
                    )
                    recover_from_export_failure()
                    raise

            # 个别版本剪映不会抛异常，但文件未生成
            if not os.path.exists(outfile):
                logger.error(
                    "export finished but output file missing: draft_id=%s path=%s",
                    task.draft_id,
                    outfile,
                )
                recover_from_export_failure()
                return False

            logger.info(f"Export draft success: {outfile}")
            return True
    
    def _upload_video_to_cos(self, outfile: str) -> Tuple[str, bool]:
        """
        上传视频到对象存储（优先 COS，其次 OSS，最后 TOS）
        
        Args:
            outfile: 输出文件路径
        
        Returns:
            (upload_url, upload_failed): 上传后的URL和是否上传失败
        """
        upload_url = ""
        upload_failed = False
        
        try:
            from src.utils.upload_file import upload_file
            logger.info(f"Uploading video to object storage: {outfile}")
            upload_url = upload_file(outfile, expire_days=config.VIDEO_GEN_RETENTION_DAYS)
            logger.info(f"Video uploaded to object storage successfully: {upload_url}")
        except Exception as upload_error:
            logger.error(f"Failed to upload video to object storage: {upload_error}")
            upload_failed = True
        
        return upload_url, upload_failed
    
    def _calculate_and_charge(self, task: VideoGenTask, outfile: str) -> None:
        """
        计算并扣除费用（必需执行但不关心结果）
        
        Args:
            task: 视频生成任务
            outfile: 输出文件路径
        
        Returns:
            None: 无返回值
        """
        if config.ENABLE_APIKEY and task.api_key:
            try:
                # 导入获取媒体时长的函数
                from src.utils.media import get_media_duration
                
                # 获取视频时长（返回的是微秒）
                duration_us = get_media_duration(outfile)
                
                if duration_us and duration_us > 0:
                    # 将微秒转换为秒
                    video_duration = duration_us / 1_000_000  # 微秒转秒
                    
                    # 计算费用：0.005积分/秒
                    cost = video_duration * 0.005
                    
                    # 导入扣费函数
                    from src.utils.points import deduct_user_points
                    
                    # 扣除用户积分（必需执行但不关心结果）
                    charge_success = deduct_user_points(
                        api_key=task.api_key,
                        points=cost,
                        desc=f"剪映草稿导出视频，时长{video_duration:.2f}秒，费用{cost:.2f}积分"
                    )
                    
                    if charge_success:
                        logger.info(f"Successfully charged {cost:.2f} points for video duration {video_duration:.2f}s, API key: {task.api_key[:8]}***")
                    else:
                        logger.warning(f"Failed to charge {cost:.2f} points for video duration {video_duration:.2f}s, API key: {task.api_key[:8]}***")
                else:
                    logger.warning(f"Could not determine video duration for charging: {outfile}")
            except Exception as charge_error:
                logger.error(f"Error calculating or charging for video duration: {charge_error}")
    
    @staticmethod
    def _collect_export_outfile_paths(task: VideoGenTask) -> List[str]:
        """汇总本任务关联的全部本地导出 mp4 路径（含重试历史）。"""
        paths: List[str] = []
        for path in [*task.export_outfile_history, task.outfile]:
            if path and path not in paths:
                paths.append(path)
        return paths

    def _cleanup_files(
        self,
        task: VideoGenTask,
        *,
        preserve_draft: bool = False,
    ) -> None:
        """
        将任务产生的临时文件加入延迟删除队列，由后台定时任务无限重试删除。
        """
        mp4_paths = self._collect_export_outfile_paths(task)
        if mp4_paths:
            enqueue_paths(mp4_paths, is_dir=False)
            logger.info(
                "Enqueued export mp4 for deferred delete: draft_id=%s count=%s",
                task.draft_id,
                len(mp4_paths),
            )

        draft_path = os.path.join(config.DRAFT_SAVE_PATH, task.draft_id)
        if preserve_draft:
            logger.warning(
                "Preserving draft directory after export failure: "
                "draft_id=%s path=%s",
                task.draft_id,
                draft_path,
            )
        else:
            enqueue_path(draft_path, is_dir=True)
            logger.info(
                "Enqueued draft directory for deferred delete: draft_id=%s path=%s",
                task.draft_id,
                draft_path,
            )
    
    def _handle_result(self, upload_url: str, upload_failed: bool) -> Tuple[str, str]:
        """
        处理最终结果
        
        Args:
            upload_url: 上传后的URL
            upload_failed: 上传是否失败
        
        Returns:
            (video_url, error_message): 视频URL和错误信息
        """
        # 如果上传失败，返回错误信息；扣费结果不关心
        if upload_failed:
            return "", "视频上传失败"
        
        # 返回上传后的URL，扣费结果不阻塞视频生成
        return upload_url, ""
    
    def request_stop(self) -> None:
        """线程安全：停止接单，唤醒 worker 取消所有正在等待/执行的阶段。"""
        with self._lifecycle_lock:
            self.stop_flag.set()
            loop = self._worker_event_loop
            if loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(self._cancel_in_flight)
                except RuntimeError:
                    pass  # loop 已经完成退出

    def _cancel_in_flight(self) -> None:
        for task in list(self._in_flight):
            if not task.cancelling():
                task.cancel()

    def stop(self, timeout: float = SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """同步调用方使用；不可在 worker 自己的线程上 join。"""
        self.request_stop()
        worker = self.worker_thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=timeout)
            if worker.is_alive():
                raise RuntimeError("[SHUTDOWN_TIMEOUT] 未能确认所有执行进程已停止")
        if self._worker_failure is not None:
            raise RuntimeError("[SHUTDOWN_FAILED] 执行进程清理失败") from self._worker_failure
        logger.info("VideoGenTaskManager stopped")

    async def astop(self, timeout: float = SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """FastAPI 生命周期使用，不在事件循环内阻塞 join 或引入退出等待线程。"""
        self.request_stop()
        deadline = time.monotonic() + timeout
        while self.worker_thread is not None and self.worker_thread.is_alive():
            if time.monotonic() >= deadline:
                raise RuntimeError("[SHUTDOWN_TIMEOUT] 未能确认所有执行进程已停止")
            await asyncio.sleep(0.05)
        self.stop(timeout=0)


# 全局任务管理器实例
task_manager = VideoGenTaskManager()
