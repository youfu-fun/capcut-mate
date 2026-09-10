import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import os
import config
from src.router import v1_router
from src.utils.draft_downloader import download_draft
from src.utils.logger import logger
from src.middlewares import PrepareMiddleware, ResponseMiddleware, TraceContextMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    from src.utils.deferred_delete import deferred_delete_background_loop
    from src.utils.draft_cleanup import draft_cleanup_background_loop

    cleanup_task = asyncio.create_task(draft_cleanup_background_loop())
    deferred_delete_task = asyncio.create_task(deferred_delete_background_loop())
    try:
        yield
    finally:
        from src.utils.video_task_manager import task_manager

        # 先停止接单并通知执行子进程退出，最后确认 worker 清理完毕。
        # 不使用 to_thread(stop)：退出路径不能再次依赖被阻塞的线程池。
        task_manager.request_stop()
        for bg_task in (cleanup_task, deferred_delete_task):
            bg_task.cancel()
        try:
            await asyncio.gather(cleanup_task, deferred_delete_task, return_exceptions=True)
        finally:
            await task_manager.astop()


# 1. 创建 FastAPI 应用
app: FastAPI = FastAPI(title="CapCut Mate API", version="1.0", lifespan=lifespan)

# 本地部署直接提供草稿和成片文件，不再把下载地址指向作者云端。
os.makedirs(os.path.join(config.PROJECT_ROOT, "output"), exist_ok=True)
app.mount(
    "/output",
    StaticFiles(directory=os.path.join(config.PROJECT_ROOT, "output")),
    name="output",
)

# 2. 注册路由
app.include_router(router=v1_router, prefix="/openapi/capcut-mate", tags=["capcut-mate"])

# 3. 添加中间件（最后注册的 TraceContextMiddleware 最先处理请求，用于 W3C trace_id）
app.add_middleware(middleware_class=PrepareMiddleware)
app.add_middleware(middleware_class=ResponseMiddleware)
app.add_middleware(middleware_class=TraceContextMiddleware)

# 4. 打印所有路由
for r in app.routes:
    # 1. 取 HTTP 方法列表
    methods = getattr(r, "methods", None) or [getattr(r, "method", "WS")]
    # 2. 安全地取路径
    path = getattr(r, "path", "<unknown>")
    # 3. 安全地取函数名
    name = getattr(r, "name", "<unnamed>")
    logger.info("Route: %s %s -> %s", ",".join(sorted(methods)), path, name)

logger.info("CapCut Mate API")

# 5. 启动
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=30000, log_config=None, log_level="info")
