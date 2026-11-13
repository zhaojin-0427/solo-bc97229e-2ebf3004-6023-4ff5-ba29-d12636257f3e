"""Minimal ``fastapi`` replacement used only by the offline test shim.

It records route registrations like the real FastAPI and exposes a WSGI
callable so ``wsgiref`` can drive the same application object end to end.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
import types
from urllib.parse import parse_qs

from pydantic import BaseModel
from starlette import BackgroundTasks, HTTPException, Request

# Re-exported so application code can write ``from fastapi import HTTPException``
# identically under the shim and under real FastAPI.
__all__ = ["FastAPI", "BackgroundTasks", "HTTPException", "Request"]


def _serialize(value):
    if hasattr(value, "dict"):
        return value.dict()
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    return value


def _resolve_annotation(annotation, globalns):
    """Turn a string annotation (PEP 563) into a runtime type."""
    if not isinstance(annotation, str):
        return annotation
    try:
        return eval(annotation, globalns, {})
    except NameError:
        return object


def _plain_type(annotation):
    """Extract the first non-None member type of an optional/union."""
    if isinstance(annotation, types.UnionType):
        members = [a for a in annotation.__args__ if a is not type(None)]
        return members[0] if members else str
    return annotation


class FastAPI:
    def __init__(self, title: str = "", version: str = "", description: str = "",
                 lifespan=None, **_kwargs):
        self.title = title
        self.version = version
        self.description = description
        self.lifespan = lifespan
        self.routes: list[tuple[str, re.Pattern, list[str], object]] = []
        self._lifespan_started = False
        # A dedicated, long-lived event loop drives the lifespan exactly like
        # a real ASGI server. Per-request coroutines keep their own loops via
        # asyncio.run; the lifespan (and the background singletons it starts,
        # e.g. the relocation worker) must never be torn down between requests.
        self._lifespan_loop: asyncio.AbstractEventLoop | None = None
        self._lifespan_thread: threading.Thread | None = None
        self._lifespan_ready = threading.Event()
        self._lifespan_shutdown: asyncio.Event | None = None
        self._lifespan_cm = None
        self._lifespan_error: BaseException | None = None

    def _ensure_started(self) -> None:
        if self._lifespan_started or self.lifespan is None:
            return
        self._lifespan_loop = asyncio.new_event_loop()
        ready = self._lifespan_ready

        def runner() -> None:
            asyncio.set_event_loop(self._lifespan_loop)
            self._lifespan_shutdown = asyncio.Event()

            async def hold_lifespan() -> None:
                self._lifespan_cm = self.lifespan(self)
                try:
                    await self._lifespan_cm.__aenter__()
                except BaseException as exc:  # surface startup failure
                    self._lifespan_error = exc
                    ready.set()
                    return
                self._lifespan_started = True
                ready.set()
                # Park here until shutdown: the lifespan stays open for the
                # whole process, matching uvicorn behaviour.
                await self._lifespan_shutdown.wait()
                await self._lifespan_cm.__aexit__(None, None, None)

            self._lifespan_loop.run_until_complete(hold_lifespan())

        self._lifespan_thread = threading.Thread(
            target=runner, name="shim-lifespan", daemon=True
        )
        self._lifespan_thread.start()
        ready.wait(timeout=10)
        if self._lifespan_error is not None:
            raise self._lifespan_error

    def shutdown(self) -> None:
        if self._lifespan_loop is not None and self._lifespan_shutdown is not None:
            self._lifespan_loop.call_soon_threadsafe(self._lifespan_shutdown.set)
        if self._lifespan_thread is not None:
            self._lifespan_thread.join(timeout=5)

    def _register(self, method: str, path: str, handler, status_code=200, **_kwargs):
        pattern = re.sub(r"\{([^}]+)\}", r"(?P<\1>[^/]+)", path)
        self.routes.append(
            (method, re.compile(f"^{pattern}$"),
             re.findall(r"\{([^}]+)\}", path), handler, status_code)
        )
        return handler

    def get(self, path: str, **kwargs):
        def decorator(handler):
            return self._register("GET", path, handler, **kwargs)
        return decorator

    def put(self, path: str, **kwargs):
        def decorator(handler):
            return self._register("PUT", path, handler, **kwargs)
        return decorator

    def post(self, path: str, **kwargs):
        def decorator(handler):
            return self._register("POST", path, handler, **kwargs)
        return decorator

    async def _resolve(self, method: str, path: str, query: dict, body: bytes):
        for route_method, pattern, params, handler, status_code in self.routes:
            if route_method != method:
                continue
            match = pattern.match(path)
            if not match:
                continue
            signature = inspect.signature(handler)
            globalns = getattr(handler, "__globals__", {})
            kwargs = {}
            for name in params:
                raw = match.group(name)
                ann = _resolve_annotation(
                    signature.parameters[name].annotation, globalns
                )
                ann = _plain_type(ann)
                if ann is int:
                    try:
                        kwargs[name] = int(raw)
                    except ValueError as exc:
                        raise HTTPException(400, f"path parameter {name!r} must be an integer") from exc
                elif ann is float:
                    kwargs[name] = float(raw)
                elif ann is bool:
                    kwargs[name] = raw.lower() in ("1", "true", "yes")
                else:
                    kwargs[name] = raw

            for name, parameter in signature.parameters.items():
                if name in kwargs:
                    continue
                ann = _resolve_annotation(parameter.annotation, globalns)
                if isinstance(ann, type) and issubclass(ann, BaseModel):
                    if not body:
                        raise HTTPException(400, "request body is required")
                    try:
                        payload = json.loads(body.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise HTTPException(400, f"invalid JSON body: {exc}") from exc
                    if not isinstance(payload, dict):
                        raise HTTPException(400, "request body must be a JSON object")
                    try:
                        kwargs[name] = ann(**payload)
                    except TypeError as exc:
                        raise HTTPException(400, str(exc)) from exc
                elif name == "request":
                    kwargs[name] = Request(body)
                elif name == "background_tasks":
                    kwargs[name] = BackgroundTasks()
                elif parameter.default is not inspect.Parameter.empty:
                    # Optional query parameter with a default value.
                    raw = query.get(name, [None])[0]
                    if raw is None:
                        kwargs[name] = parameter.default
                    else:
                        target = _plain_type(ann)
                        try:
                            kwargs[name] = target(raw) if target in (int, float, bool) else raw
                        except (TypeError, ValueError) as exc:
                            raise HTTPException(400, f"invalid query parameter {name!r}") from exc
                elif name in query:
                    kwargs[name] = query[name][0]

            result = handler(**kwargs)
            if inspect.iscoroutine(result):
                result = await result
            background = kwargs.get("background_tasks")
            if background is not None:
                background.run_all()
            return status_code, result
        raise HTTPException(404, f"no route for {method} {path}")

    def __call__(self, environ, start_response):
        """WSGI entry point used by wsgiref in the offline test harness."""

        async def run():
            length = int(environ.get("CONTENT_LENGTH") or 0)
            body = environ["wsgi.input"].read(length) if length else b""
            query = parse_qs(environ.get("QUERY_STRING", ""))
            self._ensure_started()
            return await self._resolve(
                environ["REQUEST_METHOD"],
                environ["PATH_INFO"],
                query,
                body,
            )

        try:
            status, result = asyncio.run(run())
            payload = _serialize(result)
            data = json.dumps(
                payload, ensure_ascii=False, default=str
            ).encode("utf-8")
            start_response(
                f"{status} STATUS",
                [("content-type", "application/json; charset=utf-8"),
                 ("content-length", str(len(data)))],
            )
            return [data]
        except HTTPException as exc:
            data = json.dumps(
                {"detail": exc.detail}, ensure_ascii=False, default=str
            ).encode("utf-8")
            start_response(
                f"{exc.status_code} ERROR",
                [("content-type", "application/json; charset=utf-8"),
                 ("content-length", str(len(data)))],
            )
            return [data]
