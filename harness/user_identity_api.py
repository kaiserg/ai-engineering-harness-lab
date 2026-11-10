"""M3d1: comprobar identidad en localhost; aún separado de las rutas de negocio M4."""
import argparse
from contextlib import asynccontextmanager
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .contracts import ContractError
from .user_identity import UserIdentityStore,view


def create_app(*,store=None):
    @asynccontextmanager
    async def lifespan(app):
        if store is None:
            from .postgres_config import local_dsn
            from .postgres_store import PostgresRunStore
            app.state.identities=UserIdentityStore(PostgresRunStore(local_dsn()))
        else:app.state.identities=store
        yield
    app=FastAPI(title='Harness Lab · Frontera de identidad',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['127.0.0.1','localhost'])

    @app.middleware('http')
    async def boundary(request,call_next):
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        return response

    @app.exception_handler(ContractError)
    async def error(request,error):
        code=error.code
        status=401 if code=='UNAUTHENTICATED' else 403 if code=='FORBIDDEN' else 503
        return JSONResponse({'error':code if status in (401,403) else 'IDENTITY_UNAVAILABLE'},status_code=status,
            headers={'Cache-Control':'no-store',**({'WWW-Authenticate':'Bearer'} if status==401 else {})})

    @app.exception_handler(Exception)
    async def unexpected(request,error):return JSONResponse({'error':'IDENTITY_UNAVAILABLE'},status_code=503,headers={'Cache-Control':'no-store'})

    @app.get('/api/me')
    def me(request:Request):
        identity=request.app.state.identities.authenticate(request.headers.get('authorization'),
            request.headers.get('x-harness-workspace'))
        if request.query_params:return JSONResponse({'error':'INVALID_REQUEST'},status_code=422)
        return {'identity':view(identity),'mode':'local-user-credential','business_routes_connected':False}
    return app


def main():
    import uvicorn
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8004)
    args=parser.parse_args()
    if not 1<=args.port<=65535:parser.error('Puerto inválido')
    uvicorn.run(create_app(),host='127.0.0.1',port=args.port,access_log=False)


if __name__=='__main__':main()
