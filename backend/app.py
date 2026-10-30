import hashlib
import hmac
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from threading import RLock
from fastapi import FastAPI, File, UploadFile, Request
from fastapi.responses import JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
from .engine import Engine, Problem, MAX_BYTES

class ValidationInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_id: str = Field(max_length=80)
    mapping: dict[str, str]
    policy: str = 'insert_only'

class LoginInput(BaseModel):
    password: str = Field(max_length=200)

class BodyLimit:
    def __init__(self, app):
        self.app = app
    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        size = 0
        async def bounded():
            nonlocal size
            message = await receive()
            size += len(message.get('body', b''))
            if size > MAX_BYTES + 65536:
                raise Problem('Request exceeds the upload limit.', 413)
            return message
        await self.app(scope, bounded, send)

def create_app(engine=None, public_demo=None):
    demo = os.getenv('PUBLIC_DEMO', 'true') == 'true' if public_demo is None else public_demo
    path = os.getenv('DATABASE_PATH', ':memory:' if demo else 'data/databridge.sqlite')
    if demo and path != ':memory:':
        raise RuntimeError('Public demo must use an isolated in-memory database.')
    if not demo and len(os.getenv('OPERATOR_PASSWORD', '')) < 16:
        raise RuntimeError('Private mode requires OPERATOR_PASSWORD with at least 16 characters.')
    if engine is None and path != ':memory:':
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    store = engine or Engine(path)
    if os.getenv('SEED_DEMO', 'true') == 'true':
        store.seed()
    sessions, attempts, gate = {}, {}, RLock()
    @asynccontextmanager
    async def lifespan(_app):
        yield
        if engine is None:
            store.close()
    app = FastAPI(title='DataBridge', version='0.1.0', lifespan=lifespan)
    app.add_middleware(BodyLimit)
    app.state.engine = store

    @app.exception_handler(Problem)
    async def domain_error(_request, exc):
        return JSONResponse({'error': exc.message}, status_code=exc.status)

    @app.middleware('http')
    async def security(request, call_next):
        origin = request.headers.get('origin')
        allowed = {os.getenv('APP_ORIGIN'), os.getenv('RENDER_EXTERNAL_URL'), str(request.base_url).rstrip('/')}
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and origin and origin not in allowed:
            return JSONResponse({'error': 'Origin not allowed.'}, status_code=403)
        value = request.cookies.get('db_session', '')
        digest = hashlib.sha256(value.encode()).hexdigest()
        with gate:
            expired = [k for k, v in sessions.items() if v['expires'] < time.time()]
            for k in expired:
                del sessions[k]
            identity = sessions.get(digest)
        request.state.identity = identity
        if request.url.path.startswith('/api/') and request.url.path not in ('/api/health', '/api/session', '/api/login', '/api/demo-login'):
            if not identity:
                return JSONResponse({'error': 'Open the workspace to continue.'}, status_code=401)
            if request.method not in ('GET', 'HEAD') and not hmac.compare_digest(request.headers.get('x-csrf-token', ''), identity['csrf']):
                return JSONResponse({'error': 'Session verification failed. Reload and try again.'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Cache-Control'] = 'no-store'
        return response

    def throttle(request):
        with gate:
            key = request.client.host if request.client else 'local'
            current = time.time()
            for old in [k for k, v in attempts.items() if v[1] < current]:
                del attempts[old]
            count, expiry = attempts.get(key, (0, current + 900))
            if count >= 100:
                raise Problem('Too many entry attempts; try again later.', 429)
            attempts[key] = (count + 1, expiry)

    def enter(request):
        with gate:
            current = time.time()
            if len(sessions) >= 1000:
                raise Problem('Demo session capacity reached. Try again later.', 429)
            value, csrf = secrets.token_hex(32), secrets.token_hex(32)
            old = hashlib.sha256(request.cookies.get('db_session', '').encode()).hexdigest()
            sessions.pop(old, None)
            sessions[hashlib.sha256(value.encode()).hexdigest()] = {'csrf': csrf, 'expires': current + 7200}
        response = JSONResponse({'ok': True})
        response.set_cookie('db_session', value, httponly=True, secure=os.getenv('COOKIE_SECURE') == 'true', samesite='strict', max_age=7200)
        return response

    @app.get('/api/health')
    def health():
        return {'status': 'ok', 'application': 'DataBridge', 'storage': 'disposable' if demo else 'persistent'}

    @app.get('/api/session')
    def session(request: Request):
        return {'authenticated': bool(request.state.identity), 'publicDemo': demo, 'csrf': (request.state.identity or {}).get('csrf', '')}

    @app.post('/api/demo-login')
    def demo_login(request: Request):
        throttle(request)
        if not demo:
            raise Problem('Demo entry is disabled.', 404)
        return enter(request)

    @app.post('/api/login')
    def login(request: Request, body: LoginInput):
        throttle(request)
        if demo or not hmac.compare_digest(body.password.encode(), os.getenv('OPERATOR_PASSWORD', '').encode()):
            raise Problem('Incorrect password.', 401)
        return enter(request)

    @app.post('/api/logout')
    def logout(request: Request):
        with gate:
            sessions.pop(hashlib.sha256(request.cookies.get('db_session', '').encode()).hexdigest(), None)
        response = JSONResponse({'ok': True})
        response.delete_cookie('db_session')
        return response

    @app.get('/api/workspace')
    def workspace():
        return store.overview()

    @app.post('/api/sources', status_code=201)
    async def upload(file: UploadFile = File()):
        try:
            content = await file.read(MAX_BYTES + 1)
            return store.upload(Path(file.filename or 'upload').name, content)
        finally:
            await file.close()

    @app.post('/api/runs/validate', status_code=201)
    def validate(body: ValidationInput):
        return store.validate(body.source_id, body.mapping, body.policy)

    @app.get('/api/runs/{run_id}')
    def get_run(run_id: str):
        return store.run(run_id)

    @app.post('/api/runs/{run_id}/commit')
    def commit(run_id: str):
        return store.commit(run_id)

    @app.get('/api/runs/{run_id}/errors.csv')
    def errors(run_id: str):
        return Response(store.rejected_csv(run_id), media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="databridge-errors.csv"'})

    @app.get('/api/records.csv')
    def records():
        return Response(store.export_csv(), media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="databridge-records.csv"'})

    dist = Path(__file__).resolve().parent.parent / 'frontend' / 'dist'
    if dist.exists():
        app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='assets')
        @app.get('/')
        def index():
            return FileResponse(dist / 'index.html')
    return app
