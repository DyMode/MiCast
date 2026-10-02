"""Optional fnOS TCP listener, independent of the Unix-socket management app."""

import asyncio

from fastapi import FastAPI
from uvicorn import Config, Server

from micast.config import settings
from micast.ports import reserve_tcp


class DlnaHttpListener:
    def __init__(self, dlna):
        self.dlna = dlna
        self.server = None
        self.task = None
        self.lease = None
        self._lock = asyncio.Lock()

    async def start(self):
        async with self._lock:
            if self.task and not self.task.done() and self.server.started:
                return
            await self._stop_locked()
            try:
                self.lease = reserve_tcp(
                    settings.preferred_port("port"),
                    settings.host,
                    strict=settings.port_is_strict("port"),
                )
                settings.apply_resolved_port("port", self.lease.port)
                from micast.routes.dlna import router

                app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
                app.include_router(router)
                self.server = Server(Config(app, lifespan="off", log_level="warning"))
                server, lease = self.server, self.lease

                async def serve():
                    try:
                        await server.serve(sockets=[lease.socket])
                    except SystemExit as exc:
                        raise RuntimeError("DLNA HTTP 启动失败") from exc
                    finally:
                        lease.close()
                        if not server.should_exit:
                            self.dlna.http_available = False
                            self.dlna.http_detail = "DLNA HTTP 监听意外退出，请重新检测"
                            if hasattr(self.dlna, "stop"):
                                await self.dlna.stop()
                            self.dlna.status = "error"
                            self.dlna.detail = self.dlna.http_detail

                self.task = asyncio.create_task(serve(), name="dlna-http")
                self.task.add_done_callback(
                    lambda task: None if task.cancelled() else task.exception()
                )
                deadline = asyncio.get_running_loop().time() + 5
                while not self.server.started:
                    if self.task.done():
                        await self.task
                        raise RuntimeError("DLNA HTTP 未启动")
                    if asyncio.get_running_loop().time() >= deadline:
                        raise RuntimeError("DLNA HTTP 启动超时")
                    await asyncio.sleep(0.02)
                self.dlna.http_available = True
                self.dlna.http_detail = ""
            except asyncio.CancelledError:
                await self._stop_locked()
                raise
            except Exception as exc:
                await self._stop_locked()
                self.dlna.http_available = False
                self.dlna.http_detail = str(exc)

    async def stop(self):
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self):
        if self.server:
            self.server.should_exit = True
        if self.task:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 5)
            except asyncio.CancelledError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
                if asyncio.current_task().cancelling():
                    if self.lease:
                        self.lease.close()
                    self.server = self.task = self.lease = None
                    raise
            except Exception:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        if self.lease:
            self.lease.close()
        self.server = self.task = self.lease = None
