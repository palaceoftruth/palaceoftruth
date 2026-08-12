import ssl

from fastapi import Depends, Request
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
    sslrootcert = query.pop("sslrootcert", None) or settings.database_ssl_root_cert
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
        if not sslrootcert:
            raise ValueError(
                "DATABASE_SSL_ROOT_CERT or sslrootcert is required for sslmode=verify-full"
            )
        context = ssl.create_default_context(cafile=sslrootcert)
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
    # Unbound sessions are deliberately empty under forced RLS. Cross-tenant
    # control-plane work must opt in through system_async_session().
    info={"tenant_id": "__unbound__", "system_access": False},
)


def tenant_async_session(tenant_id: str) -> AsyncSession:
    """Create a fail-closed session bound to one tenant before it begins."""
    try:
        return async_session(
            info={"tenant_id": str(tenant_id), "system_access": False}
        )
    except TypeError:  # Narrow test and integration factories may omit options.
        return async_session()


def system_async_session() -> AsyncSession:
    """Create an explicit control-plane session that may cross tenants."""
    try:
        return async_session(
            info={"tenant_id": "__unbound__", "system_access": True}
        )
    except TypeError:
        return async_session()


async def bind_session_to_tenant(session: AsyncSession, tenant_id: str) -> None:
    """End a credential-discovery transaction and bind later work to its tenant."""
    info = getattr(session, "info", None)
    if info is None:  # Narrow test doubles may not implement SQLAlchemy metadata.
        return
    await session.rollback()
    info["tenant_id"] = str(tenant_id)
    info["system_access"] = False


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


async def get_credential_exchange_db(
    session: AsyncSession = Depends(get_db),
):
    """Allow only credential discovery; callers must bind before tenant writes."""
    info = getattr(session, "info", None)
    if info is not None:
        info["tenant_id"] = "__unbound__"
        info["system_access"] = True
    yield session
