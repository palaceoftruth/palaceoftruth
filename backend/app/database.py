import ssl

from fastapi import Request
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session

from app.config import settings


def _engine_configuration() -> tuple[str, dict]:
    """Build bounded, timeout-aware connection settings without leaking TLS options."""

    url = make_url(settings.database_url)
    query = dict(url.query)
    sslmode = query.pop("sslmode", "")
    query.pop("sslrootcert", None)
    connect_args: dict = {
        "server_settings": {
            "statement_timeout": str(settings.database_statement_timeout_ms),
            "idle_in_transaction_session_timeout": str(
                settings.database_idle_transaction_timeout_ms
            ),
        }
    }
    if sslmode:
        if sslmode != "verify-full":
            raise ValueError("Only sslmode=verify-full is accepted for database TLS")
        context = ssl.create_default_context(cafile=settings.database_ssl_root_cert)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        connect_args["ssl"] = context
    return (
        url.set(query=query).render_as_string(hide_password=False),
        {
            "echo": False,
            "pool_size": settings.database_pool_size,
            "max_overflow": settings.database_max_overflow,
            "pool_timeout": settings.database_pool_timeout_seconds,
            "pool_recycle": settings.database_pool_recycle_seconds,
            "pool_pre_ping": True,
            "connect_args": connect_args,
        },
    )


class TenantSession(Session):
    """Synchronous session used under AsyncSession for transaction hooks."""


@event.listens_for(TenantSession, "after_begin")
def _apply_tenant_context(session: Session, _transaction, connection) -> None:
    tenant_id = str(session.info.get("tenant_id") or "__unbound__")
    connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": tenant_id},
    )
    connection.execute(
        text("SELECT set_config('app.system_access', :system_access, true)"),
        {"system_access": "true" if session.info.get("system_access") else "false"},
    )


_database_url, _engine_options = _engine_configuration()
engine = create_async_engine(_database_url, **_engine_options)
async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    sync_session_class=TenantSession,
    expire_on_commit=False,
    info={"tenant_id": "__unbound__", "system_access": True},
)


def tenant_async_session(tenant_id: str) -> AsyncSession:
    """Create a fail-closed session bound to one tenant before it begins."""
    return async_session(
        info={"tenant_id": str(tenant_id), "system_access": False}
    )


def system_async_session() -> AsyncSession:
    """Create an explicit control-plane session that may cross tenants."""
    return async_session(
        info={"tenant_id": "__unbound__", "system_access": True}
    )


class Base(DeclarativeBase):
    pass


async def get_db(request: Request):
    async with async_session() as session:
        tenant_id = getattr(request.state, "tenant_id", None)
        if not tenant_id:
            tenant_id = request.path_params.get("tenant_id")
        system_access = not tenant_id and (
            request.url.path.startswith("/api/v1/admin")
            or request.url.path == "/api/v1/metrics"
        )
        session.info["tenant_id"] = str(tenant_id or "__unbound__")
        session.info["system_access"] = system_access
        yield session
