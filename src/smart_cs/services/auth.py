"""Opaque server sessions. No client-supplied identity or self-assigned role."""
import asyncio
import hashlib
import hmac
import re
import secrets
from datetime import timedelta, timezone
from uuid import uuid4
from sqlalchemy import select, update, case
from smart_cs.db.models import AuthCredential, AuthSession, AuthLoginAttempt, User, utcnow
from smart_cs.schemas.errors import ApplicationError
from smart_cs.schemas.identity import IdentityContext

ACCESS_SECONDS = 900
REFRESH_SECONDS = 604800
PASSWORD_SLOTS = asyncio.Semaphore(2)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128 or len(password.encode()) > 512:
        raise ValueError('Password must contain 12–128 characters, at most 512 UTF-8 bytes')
    salt = secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=131072, r=8, p=1, maxmem=268435456)
    return 'scrypt$131072$8$1$' + salt + '$' + value.hex()


def verify_password(password, encoded):
    try:
        kind,n,r,p,salt,expected = encoded.split('$')
        if (kind,n,r,p) != ('scrypt','131072','8','1') or len(password.encode()) > 512:
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=131072, r=8, p=1, maxmem=268435456)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


# A fixed non-secret dummy record equalizes unknown-user password work.
DUMMY_HASH = 'scrypt$131072$8$1$' + '00'*16 + '$' + '00'*64


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def unauthenticated():
    return ApplicationError('UNAUTHENTICATED', '请登录后继续，或重新登录以更新会话。', 401)


def identity_for(user, session):
    return IdentityContext(subject_id=user.id, mode='authenticated_session', authenticated=True,
                           role=user.role, auth_session_id=session.id)


class AuthService:
    def __init__(self, database):
        self.database = database

    async def throttle(self, login_name, client_ip):
        """Database atomic UPSERT; attempts survive restarts and multiple workers."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        insert = pg_insert if self.database.engine.dialect.name == 'postgresql' else sqlite_insert
        now = utcnow(); cutoff = now - timedelta(minutes=5)
        exceeded = False
        async with self.database.sessions.begin() as session:
            for category, value, limit in [('name',login_name,10),('ip',client_ip,30)]:
                key = digest(category + ':' + value)
                stale = AuthLoginAttempt.window_started_at < cutoff
                statement = insert(AuthLoginAttempt).values(key=key, window_started_at=now, attempts=1)
                statement = statement.on_conflict_do_update(index_elements=['key'], set_={
                    'attempts': case((stale,1), else_=AuthLoginAttempt.attempts+1),
                    'window_started_at': case((stale,now), else_=AuthLoginAttempt.window_started_at)
                }).returning(AuthLoginAttempt.attempts)
                exceeded = (await session.scalar(statement)) > limit or exceeded
        if exceeded:
            raise ApplicationError('RATE_LIMITED', '登录尝试过于频繁，请在五分钟后重试。', 429)

    async def login(self, name, password, client_ip):
        name = name.strip().lower()
        await self.throttle(name, client_ip)
        async with self.database.sessions() as session:
            row = (await session.execute(select(AuthCredential,User).join(User).where(AuthCredential.login_name==name))).first()
        encoded = row[0].password_hash if row else DUMMY_HASH
        async with PASSWORD_SLOTS:
            valid = await asyncio.to_thread(verify_password, password, encoded)
        if not row or not valid or not row[0].enabled:
            raise ApplicationError('INVALID_CREDENTIALS', '账号或密码不正确，或账号已停用。', 401)
        access,refresh,csrf = (secrets.token_urlsafe(48) for _ in range(3))
        now=utcnow()
        async with self.database.sessions.begin() as session:
            # A concurrent reset/disable must invalidate this in-flight login.
            credential=await session.scalar(select(AuthCredential).where(AuthCredential.user_id==row[1].id).with_for_update())
            if not credential.enabled or credential.password_hash != encoded:
                raise unauthenticated()
            auth_session=AuthSession(id=uuid4().hex,user_id=row[1].id,access_hash=digest(access),refresh_hash=digest(refresh),
                csrf_hash=digest(csrf),access_expires_at=now+timedelta(seconds=ACCESS_SECONDS),
                refresh_expires_at=now+timedelta(seconds=REFRESH_SECONDS))
            session.add(auth_session)
        return access,refresh,csrf,row[1]

    async def authenticate(self, access):
        if not access or len(access)>256:
            raise unauthenticated()
        async with self.database.sessions() as session:
            row=(await session.execute(select(AuthSession,User).join(User).join(AuthCredential,AuthCredential.user_id==User.id).where(
                AuthSession.access_hash==digest(access),AuthSession.revoked_at.is_(None),
                AuthSession.access_expires_at>utcnow(),AuthSession.refresh_expires_at>utcnow(),AuthCredential.enabled.is_(True)))).first()
        if not row:
            raise unauthenticated()
        return identity_for(row[1],row[0])

    async def refresh(self, refresh, csrf):
        if not refresh or not csrf or max(len(refresh),len(csrf))>256:
            raise unauthenticated()
        access,new_refresh,new_csrf=(secrets.token_urlsafe(48) for _ in range(3))
        async with self.database.sessions.begin() as session:
            row=(await session.execute(select(AuthSession,User).join(User).join(AuthCredential,AuthCredential.user_id==User.id).where(
                AuthSession.refresh_hash==digest(refresh),AuthSession.revoked_at.is_(None),
                AuthSession.refresh_expires_at>utcnow(),AuthCredential.enabled.is_(True)))).first()
            if not row:
                raise unauthenticated()
            record,user=row
            if not hmac.compare_digest(record.csrf_hash,digest(csrf)):
                raise ApplicationError('CSRF_REJECTED','刷新校验失败，请重新登录。',403)
            changed=await session.scalar(update(AuthSession).where(AuthSession.id==record.id,
                AuthSession.refresh_hash==digest(refresh),AuthSession.revoked_at.is_(None)).values(
                access_hash=digest(access),refresh_hash=digest(new_refresh),csrf_hash=digest(new_csrf),
                access_expires_at=utcnow()+timedelta(seconds=ACCESS_SECONDS)).returning(AuthSession.id))
            if not changed:
                raise unauthenticated()
        return access,new_refresh,new_csrf,user

    async def logout(self, identity):
        async with self.database.sessions.begin() as session:
            await session.execute(update(AuthSession).where(AuthSession.id==identity.auth_session_id,
                AuthSession.user_id==identity.subject_id).values(revoked_at=utcnow()))

    async def provision(self, login_name, password, display_name, role='customer', user_id=None):
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{2,63}',login_name) or role not in {'customer','support','admin'}:
            raise ValueError('Invalid account name or role')
        encoded=await asyncio.to_thread(hash_password,password)
        async with self.database.sessions.begin() as session:
            row=User(id=user_id or 'account-'+uuid4().hex,display_name=display_name,role=role,source='authenticated_account')
            session.add(row);await session.flush()
            session.add(AuthCredential(user_id=row.id,login_name=login_name,password_hash=encoded))
        return row
