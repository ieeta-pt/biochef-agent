"""The TES API's ASGI app: every route needs a token, and every refusal is recorded.

TES 1.1, the part a workflow engine uses: create, get, list, cancel and
service-info. Nothing here is served without authentication, not even
service-info, and there is no /docs, /redoc or openapi.json, which would be
unauthenticated pages on a listener that exists to refuse.
"""

import asyncio
import hmac
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

import audit
from store import db
from bodylimit import BodySizeLimitMiddleware
from tesapi import records, tasks
from tesapi.config import MAX_BODY_BYTES, Config, hash_token
from tesapi.policy import Policy, Refused

VERSION = "1.1.0"


class Throttle:
    """At most one audit event per interval for something anyone can cause.

    A refused login costs a caller nothing, so a trail that wrote a line for each
    would be a way to fill the disk. The count of those not written goes in the
    next line, so nothing is hidden, only batched.
    """

    def __init__(self, interval=1.0, clock=time.monotonic):
        self.interval, self.clock = interval, clock
        self.last = None
        self.suppressed = 0

    def allow(self):
        moment = self.clock()
        if self.last is None or moment - self.last >= self.interval:
            self.last, suppressed, self.suppressed = moment, self.suppressed, 0
            return True, suppressed
        self.suppressed += 1
        return False, 0


def create_app(config: Config, runner, start_checks=None, bind=None) -> FastAPI:
    """`bind` is the database tasks are kept in; made from the configuration if absent."""
    policy = Policy(config)
    if bind is None:
        bind = db.make_engine(config.db_url)
    engine = tasks.Engine(config, policy, runner, bind)
    throttle = Throttle()

    @asynccontextmanager
    async def lifespan(_app):
        await asyncio.to_thread(engine.open)
        beating = asyncio.create_task(engine.heartbeat())
        try:
            if start_checks is not None:
                await asyncio.to_thread(start_checks)
            yield
        finally:
            beating.cancel()
            await asyncio.to_thread(engine.close)

    app = FastAPI(title="TES", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)
    app.state.engine, app.state.config, app.state.policy = engine, config, policy

    def refuse_audit(request, reason, status):
        allowed, suppressed = throttle.allow()
        if allowed:
            audit.record("tes.auth.refused", source=tasks.SOURCE, reason=reason,
                         status=status, suppressed=suppressed,
                         client=request.client.host if request.client else None)

    async def caller(request: Request) -> str:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        found = None
        if scheme.lower() == "bearer" and token:
            digest = hash_token(token.strip())
            for name, stored in config.tokens:
                # Every entry is compared, so the time does not say which matched.
                if hmac.compare_digest(digest, stored) and found is None:
                    found = name
        if found is None:
            refuse_audit(request, "no valid token", 401)
            raise HTTPException(status_code=401, detail="a bearer token is required",
                                headers={"WWW-Authenticate": "Bearer"})
        return found

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(status_code=exc.status_code,
                            content={"msg": exc.detail}, headers=exc.headers)

    @app.exception_handler(audit.AuditError)
    async def audit_error(request, exc):
        return JSONResponse(status_code=503, content={
            "msg": "the node cannot record this on its audit trail, so it will not do it"})

    @app.exception_handler(records.RecordError)
    async def record_error(request, exc):
        return JSONResponse(status_code=503, content={
            "msg": "the node cannot keep this task in its database, so it will not run it"})

    @app.get("/v1/service-info")
    async def service_info(who: str = Depends(caller)):
        return {"id": "org.biochef.agent.tes", "name": "biochef-agent TES API",
                "type": {"group": "org.ga4gh", "artifact": "tes", "version": VERSION},
                "description": "Runs one task at a time in an image this node lists, "
                               "with the node's own runner",
                "organization": {"name": "biochef-agent", "url": "https://biochef.app"},
                "version": VERSION,
                "storage": [f"file://{path}" for path in
                            dict.fromkeys(config.read_dirs + config.write_dirs)]}

    @app.post("/v1/tasks")
    async def create(request: Request, who: str = Depends(caller)):
        try:
            body = await request.json()
        except ValueError:
            body = None
        try:
            plan = tasks.parse(body, policy)
            task = engine.submit(who, plan)
        except Refused as refusal:
            audit.record("tes.task.refused", source=tasks.SOURCE, caller=who,
                         reason=refusal.reason[:300], status=refusal.status)
            raise HTTPException(status_code=refusal.status, detail=refusal.reason)
        except tasks.Lost:
            audit.record("tes.task.refused", source=tasks.SOURCE, caller=who,
                         reason="lost the database", status=503)
            raise HTTPException(status_code=503, detail="this TES listener lost its "
                                "place in the database and takes no task; another "
                                "listener holds it")
        except tasks.Full:
            audit.record("tes.task.refused", source=tasks.SOURCE, caller=who,
                         reason="queue full", status=503)
            raise HTTPException(status_code=503,
                                detail="the TES queue is full; retry later",
                                headers={"Retry-After": "5"})
        return {"id": task.id}

    def view_of(request):
        level = request.query_params.get("view", "MINIMAL").upper()
        if level not in ("MINIMAL", "BASIC", "FULL"):
            raise HTTPException(status_code=400, detail="view is MINIMAL, BASIC or FULL")
        return level

    @app.get("/v1/tasks")
    async def listing(request: Request, who: str = Depends(caller)):
        level = view_of(request)
        try:
            size = int(request.query_params.get("page_size", 256))
            offset = int(request.query_params.get("page_token") or 0)
        except ValueError:
            raise HTTPException(status_code=400, detail="page_size and page_token are numbers")
        size = max(1, min(size, 256))
        found = engine.list(who, request.query_params.get("name_prefix", ""),
                            request.query_params.get("state") or None)
        page = found[offset:offset + size]
        more = offset + size < len(found)
        return {"tasks": [task.view(level) for task in page],
                "next_page_token": str(offset + size) if more else ""}

    @app.get("/v1/tasks/{task_id}")
    async def one(task_id: str, request: Request, who: str = Depends(caller)):
        level = view_of(request)
        task = engine.get(who, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="no such task")
        return task.view(level)

    @app.post("/v1/tasks/{task_id}:cancel")
    async def cancel(task_id: str, who: str = Depends(caller)):
        task = engine.get(who, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="no such task")
        engine.cancel(task)
        return {}

    return app
