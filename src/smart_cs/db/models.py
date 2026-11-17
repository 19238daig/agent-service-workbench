"""Business facts, knowledge assets and labelled data have separate tables."""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, text, JSON, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, Numeric, String, Text, UniqueConstraint
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20), default='customer')
    source: Mapped[str] = mapped_column(String(32), default='local_demo')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (CheckConstraint("role IN ('customer','support','admin')", name='ck_user_role'),)


class AuthCredential(Base):
    __tablename__ = 'auth_credentials'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), primary_key=True)
    login_name: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthSession(Base):
    __tablename__ = 'auth_sessions'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    access_hash: Mapped[str] = mapped_column(String(64), unique=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthLoginAttempt(Base):
    __tablename__ = 'auth_login_attempts'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer)


class Order(Base):
    __tablename__ = 'orders'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    product_name: Mapped[str] = mapped_column(String(120))
    paid_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default='CNY')
    status: Mapped[str] = mapped_column(String(32))
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(32), default='synthetic_fixture')
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    refund_reserved_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    return_policy: Mapped[str] = mapped_column(String(24), default='unknown', server_default='unknown')
    return_condition: Mapped[str] = mapped_column(String(24), default='unknown', server_default='unknown')
    fact_version: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    __table_args__ = (
        CheckConstraint('paid_amount >= 0', name='ck_order_amount'),
        CheckConstraint('paid_amount <= 9999999999.99', name='ck_order_amount_max'),
        CheckConstraint("currency = 'CNY'", name='ck_order_currency'),
        CheckConstraint("status IN ('pending_shipment','shipped','delivered','cancelled')", name='ck_order_status'),
        UniqueConstraint('id','user_id',name='uq_order_owner'),
        Index('ix_order_owner_paid','user_id','paid_at'),
        CheckConstraint('refunded_amount IS NULL OR (refunded_amount >= 0 AND refunded_amount <= paid_amount)', name='ck_order_refunded_amount'),
        CheckConstraint('refund_reserved_amount IS NULL OR (refund_reserved_amount >= 0 AND refund_reserved_amount <= paid_amount)', name='ck_order_reserved_amount'),
        CheckConstraint('refunded_amount IS NULL OR refund_reserved_amount IS NULL OR refunded_amount + refund_reserved_amount <= paid_amount', name='ck_order_refund_total'),
        CheckConstraint('shipped_at IS NULL OR shipped_at >= paid_at', name='ck_order_shipped_time'),
        CheckConstraint("delivered_at IS NULL OR (shipped_at IS NOT NULL AND delivered_at >= shipped_at AND status = 'delivered')", name='ck_order_delivery_time'),
        CheckConstraint("return_policy IN ('standard_7d','excluded','unknown')", name='ck_order_return_policy'),
        CheckConstraint("return_condition IN ('intact','not_intact','unknown')", name='ck_order_return_condition'),
        CheckConstraint('fact_version >= 1', name='ck_order_fact_version'),
    )


class Ticket(Base):
    __tablename__ = 'tickets'
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    order_id: Mapped[str | None] = mapped_column(ForeignKey('orders.id'), nullable=True)
    type: Mapped[str] = mapped_column(String(32))
    priority: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), default='created')
    summary: Mapped[str] = mapped_column(String(500))
    details: Mapped[str] = mapped_column(Text, default='')
    requested_amount: Mapped[Decimal | None] = mapped_column(Numeric(12,2), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default='agent')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    version: Mapped[int] = mapped_column(Integer, default=1)
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    __table_args__ = (
        CheckConstraint("priority IN ('low','medium','high','urgent')", name='ck_ticket_priority'),
        CheckConstraint("status IN ('created','processing','pending_review','resolved','closed','escalated')", name='ck_ticket_status'),
        CheckConstraint("type IN ('refund','claim','account_open','account_change','complaint','general')",name='ck_ticket_type'),
        CheckConstraint('version >= 1',name='ck_ticket_version'),
        CheckConstraint("requested_amount IS NULL OR (type = 'refund' AND requested_amount > 0 AND requested_amount <= 9999999999.99)",name='ck_ticket_requested_amount'),
        ForeignKeyConstraint(['order_id','user_id'],['orders.id','orders.user_id'],name='fk_ticket_order_owner'),
        Index('ix_ticket_owner_created', 'user_id', 'created_at'),
        Index('ix_ticket_assignee_status', 'assignee_id', 'status', 'updated_at'),
    )


class TicketEvent(Base):
    __tablename__ = 'ticket_events'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey('tickets.id'), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    event_type: Mapped[str] = mapped_column(String(32))
    action_id: Mapped[str | None] = mapped_column(ForeignKey('action_requests.id'),nullable=True,unique=True)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ticket_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str] = mapped_column(String(500), default='', server_default='')
    assignee_before: Mapped[str | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    assignee_after: Mapped[str | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    __table_args__=(
        CheckConstraint("from_status IS NULL OR from_status IN ('created','processing','pending_review','resolved','closed','escalated')",name='ck_event_from_status'),
        CheckConstraint("to_status IN ('created','processing','pending_review','resolved','closed','escalated')",name='ck_event_to_status'),
        CheckConstraint('ticket_version IS NULL OR ticket_version >= 1',name='ck_event_ticket_version'),
        CheckConstraint("actor_role IS NULL OR actor_role IN ('customer','support','admin')",name='ck_event_actor_role'),
        UniqueConstraint('ticket_id','ticket_version',name='uq_event_ticket_version'),
    )


class ChatSession(Base):
    __tablename__='chat_sessions'
    id: Mapped[str]=mapped_column(String(128),primary_key=True)
    user_id: Mapped[str]=mapped_column(ForeignKey('users.id'))
    title: Mapped[str]=mapped_column(String(200),default='新对话')
    status: Mapped[str]=mapped_column(String(16),default='active')
    graph_version: Mapped[str]=mapped_column(String(64),default='four-agents-v1')
    thread_id: Mapped[str | None]=mapped_column(String(128),nullable=True)
    active_run_id: Mapped[str | None]=mapped_column(String(32),nullable=True)
    next_sequence: Mapped[int]=mapped_column(Integer,default=1)
    version: Mapped[int]=mapped_column(Integer,default=1)
    source: Mapped[str]=mapped_column(String(32),default='local_demo')
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    __table_args__=(
        UniqueConstraint('id','user_id',name='uq_session_owner'),
        UniqueConstraint('thread_id',name='uq_session_thread_id'),
        CheckConstraint("status IN ('active','archived')",name='ck_session_status'),
        CheckConstraint('next_sequence >= 1',name='ck_session_next_sequence'),
        CheckConstraint('version >= 1',name='ck_session_version'),
        Index('ix_session_owner_updated','user_id','updated_at'),
    )


class ChatMessage(Base):
    __tablename__='chat_messages'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    session_id: Mapped[str]=mapped_column(String(128))
    user_id: Mapped[str]=mapped_column(String(128))
    sequence: Mapped[int]=mapped_column(Integer)
    role: Mapped[str]=mapped_column(String(16))
    content: Mapped[str]=mapped_column(Text)
    source: Mapped[str]=mapped_column(String(32),default='local_demo')
    run_id: Mapped[str | None]=mapped_column(String(32),nullable=True)
    metadata_json: Mapped[dict | None]=mapped_column(JSON,nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    __table_args__=(
        ForeignKeyConstraint(['session_id','user_id'],['chat_sessions.id','chat_sessions.user_id'],name='fk_message_session_owner',ondelete='CASCADE'),
        UniqueConstraint('session_id','sequence',name='uq_message_session_sequence'),
        UniqueConstraint('id','session_id','user_id',name='uq_message_owner_identity'),
        ForeignKeyConstraint(['run_id','session_id','user_id'],['execution_runs.id','execution_runs.session_id','execution_runs.subject_id'],name='fk_message_run_owner'),
        CheckConstraint('sequence >= 1',name='ck_message_sequence'),
        CheckConstraint("role IN ('user','assistant')",name='ck_message_role'),
        CheckConstraint('length(trim(content)) > 0',name='ck_message_content'),
        Index('ix_message_owner_created','user_id','created_at'),
    )


class ChatTurn(Base):
    __tablename__='chat_turns'
    id: Mapped[str]=mapped_column(String(32),primary_key=True)
    session_id: Mapped[str]=mapped_column(String(128))
    user_id: Mapped[str]=mapped_column(String(128))
    current_run_id: Mapped[str]=mapped_column(String(32),unique=True)
    user_message_id: Mapped[str]=mapped_column(String(64),unique=True)
    assistant_message_id: Mapped[str | None]=mapped_column(String(64),nullable=True,unique=True)
    graph_version: Mapped[str]=mapped_column(String(64))
    authorization_role: Mapped[str]=mapped_column(String(20))
    context_fingerprint: Mapped[str]=mapped_column(String(64))
    model_fingerprint: Mapped[str]=mapped_column(String(64))
    status: Mapped[str]=mapped_column(String(20),default='running')
    attempt_count: Mapped[int]=mapped_column(Integer,default=1)
    error_code: Mapped[str | None]=mapped_column(String(64),nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    __table_args__=(
        ForeignKeyConstraint(['session_id','user_id'],['chat_sessions.id','chat_sessions.user_id'],name='fk_turn_session_owner'),
        ForeignKeyConstraint(['current_run_id','session_id','user_id'],['execution_runs.id','execution_runs.session_id','execution_runs.subject_id'],name='fk_turn_run_owner'),
        ForeignKeyConstraint(['user_message_id','session_id','user_id'],['chat_messages.id','chat_messages.session_id','chat_messages.user_id'],name='fk_turn_user_message'),
        ForeignKeyConstraint(['assistant_message_id','session_id','user_id'],['chat_messages.id','chat_messages.session_id','chat_messages.user_id'],name='fk_turn_assistant_message'),
        CheckConstraint("status IN ('running','completed','blocked','failed','cancelled','interrupted','waiting_approval')",name='ck_turn_status'),
        CheckConstraint("authorization_role IN ('customer','support','admin')",name='ck_turn_role'),
        CheckConstraint('attempt_count >= 1',name='ck_turn_attempts'),
        CheckConstraint("(status IN ('completed','blocked','waiting_approval') AND assistant_message_id IS NOT NULL) OR (status NOT IN ('completed','blocked','waiting_approval') AND assistant_message_id IS NULL)",name='ck_turn_publication'),
        Index('ix_turn_session_created','session_id','created_at'),
    )


class ActionRequest(Base):
    __tablename__ = 'action_requests'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[str] = mapped_column(String(128))
    turn_id: Mapped[str] = mapped_column(ForeignKey('chat_turns.id'), unique=True)
    actor_role: Mapped[str] = mapped_column(String(20))
    tool: Mapped[str] = mapped_column(String(40))
    parameter_hash: Mapped[str] = mapped_column(String(64))
    draft_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON)
    required_approval: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(24), default='awaiting_user')
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(['session_id','user_id'], ['chat_sessions.id','chat_sessions.user_id'], name='fk_action_session_owner'),
        CheckConstraint("actor_role IN ('customer','support','admin')", name='ck_action_role'),
        CheckConstraint("tool IN ('ticket_create','ticket_transition')", name='ck_action_tool'),
        CheckConstraint("required_approval IN ('user','staff')", name='ck_action_requirement'),
        CheckConstraint("status IN ('awaiting_user','awaiting_staff','approved','ready','rejected','cancelled','expired','stale','executed')", name='ck_action_status'),
        CheckConstraint('expires_at > created_at', name='ck_action_expiry'),
        Index('ix_action_owner_created','user_id','created_at'),
        Index('ix_action_queue','status','expires_at'),
    )


class Approval(Base):
    __tablename__ = 'approvals'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    action_id: Mapped[str] = mapped_column(ForeignKey('action_requests.id'))
    stage: Mapped[str] = mapped_column(String(8))
    principal_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    principal_role: Mapped[str] = mapped_column(String(20))
    decision: Mapped[str] = mapped_column(String(8))
    parameter_hash: Mapped[str] = mapped_column(String(64))
    draft_hash: Mapped[str] = mapped_column(String(64))
    note: Mapped[str] = mapped_column(String(500), default='')
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint('action_id','stage', name='uq_approval_stage'),
        CheckConstraint("stage IN ('user','staff')", name='ck_approval_stage'),
        CheckConstraint("decision IN ('approve','reject')", name='ck_approval_decision'),
        CheckConstraint("principal_role IN ('customer','support','admin')", name='ck_approval_role'),
        CheckConstraint("stage <> 'staff' OR principal_role IN ('support','admin')", name='ck_approval_staff'),
        CheckConstraint('expires_at > created_at', name='ck_approval_expiry'),
    )


class ActionExecution(Base):
    __tablename__ = 'action_executions'
    action_id: Mapped[str] = mapped_column(ForeignKey('action_requests.id'),primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    ticket_id: Mapped[str] = mapped_column(ForeignKey('tickets.id'))
    event_id: Mapped[str] = mapped_column(ForeignKey('ticket_events.id'),unique=True)
    tool: Mapped[str] = mapped_column(String(40))
    parameter_hash: Mapped[str] = mapped_column(String(64))
    draft_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    result_hash: Mapped[str] = mapped_column(String(64))
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=utcnow)
    __table_args__ = (
        CheckConstraint("tool IN ('ticket_create','ticket_transition')",name='ck_execution_tool'),
        Index('ix_execution_owner_committed','user_id','committed_at'),
    )


class KnowledgeDocument(Base):
    __tablename__ = 'knowledge_documents'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    source: Mapped[str] = mapped_column(String(180))
    title: Mapped[str] = mapped_column(String(180))
    version: Mapped[str] = mapped_column(String(32))
    domain: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20), default='published')
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    effective_from: Mapped[str] = mapped_column(String(10))
    effective_until: Mapped[str | None] = mapped_column(String(10),nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), default='synthetic_demo')
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(64), default='text/markdown', server_default='text/markdown')
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    access_scope: Mapped[str] = mapped_column(String(16), default='public', server_default='public')
    owner_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    __table_args__ = (
        CheckConstraint("access_scope IN ('public','support','admin','private')", name='ck_document_scope'),
        CheckConstraint('effective_until IS NULL OR (length(effective_until) = 10 AND effective_until > effective_from)',name='ck_document_valid_interval'),
        CheckConstraint("(access_scope = 'private' AND owner_id IS NOT NULL) OR (access_scope <> 'private' AND owner_id IS NULL)", name='ck_document_owner'),
    )


class KnowledgeChunk(Base):
    __tablename__ = 'knowledge_chunks'
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey('knowledge_documents.id'), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    source_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_set: Mapped[str | None] = mapped_column(String(80), nullable=True)
    heading: Mapped[str | None] = mapped_column(String(500), nullable=True)
    kind: Mapped[str | None] = mapped_column(String(24), nullable=True)
    anchor: Mapped[str | None] = mapped_column(String(180), nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    locator: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Embeddings are versioned in a separate index table.
    embedding_status: Mapped[str] = mapped_column(String(24), default='pending')
    __table_args__ = (
        UniqueConstraint('document_id', 'chunk_set', 'ordinal', name='uq_chunk_set_position'),
        Index('uq_legacy_chunk_position','document_id','ordinal',unique=True,postgresql_where=text('chunk_set IS NULL'),sqlite_where=text('chunk_set IS NULL')),
        CheckConstraint('ordinal >= 0',name='ck_chunk_ordinal'),
        CheckConstraint('source_page IS NULL OR source_page >= 1',name='ck_chunk_page'),
        CheckConstraint('token_count IS NULL OR token_count >= 0',name='ck_chunk_tokens'),
    )


class KnowledgeIngestion(Base):
    __tablename__='knowledge_ingestions'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    document_id: Mapped[str]=mapped_column(ForeignKey('knowledge_documents.id'),unique=True)
    source_key: Mapped[str]=mapped_column(String(256))
    version: Mapped[str]=mapped_column(String(32))
    acl_key: Mapped[str]=mapped_column(String(64))
    artifact_sha256: Mapped[str]=mapped_column(String(64))
    profile_fingerprint: Mapped[str]=mapped_column(String(64))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    __table_args__=(
        UniqueConstraint('source_key','version','acl_key',name='uq_ingestion_source_version_acl'),
        UniqueConstraint('artifact_sha256','version','acl_key',name='uq_ingestion_artifact_version_acl'),
        CheckConstraint('length(artifact_sha256) = 64',name='ck_ingestion_artifact_hash'),
        CheckConstraint('length(profile_fingerprint) = 64',name='ck_ingestion_profile_hash'),
    )


class DatasetRelease(Base):
    __tablename__ = 'dataset_releases'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[str] = mapped_column(String(32))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    provenance: Mapped[str] = mapped_column(String(100))
    manifest: Mapped[dict] = mapped_column(JSON)


class DatasetSample(Base):
    __tablename__ = 'dataset_samples'
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    release_id: Mapped[str] = mapped_column(ForeignKey('dataset_releases.id'))
    task: Mapped[str] = mapped_column(String(32))
    split: Mapped[str] = mapped_column(String(16))
    group_id: Mapped[str] = mapped_column(String(128))
    label: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (
        CheckConstraint("split IN ('train','validation','test','challenge')", name='ck_dataset_split'),
        Index('ix_dataset_task_split', 'task', 'split'),
    )


class RetrievalIndex(Base):
    __tablename__ = 'retrieval_indexes'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    model_identity: Mapped[dict] = mapped_column(JSON)
    configuration: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index('uq_active_retrieval_index', 'active', unique=True,
        postgresql_where=text('active'), sqlite_where=text('active = 1')),)


class KnowledgeEmbedding(Base):
    __tablename__ = 'knowledge_embeddings'
    index_id: Mapped[str] = mapped_column(ForeignKey('retrieval_indexes.id', ondelete='CASCADE'), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(ForeignKey('knowledge_chunks.id', ondelete='CASCADE'), primary_key=True)
    embedding: Mapped[list] = mapped_column(VECTOR(1024).with_variant(JSON(), 'sqlite'))


class RetrievalIndexDocument(Base):
    __tablename__='retrieval_index_documents'
    index_id: Mapped[str]=mapped_column(ForeignKey('retrieval_indexes.id',ondelete='CASCADE'),primary_key=True)
    document_id: Mapped[str]=mapped_column(ForeignKey('knowledge_documents.id'),primary_key=True)
    chunk_set: Mapped[str | None]=mapped_column(String(80),nullable=True)
    chunk_count: Mapped[int]=mapped_column(Integer)
    document_fingerprint: Mapped[str]=mapped_column(String(64))
    chunk_fingerprint: Mapped[str]=mapped_column(String(64))
    snapshot: Mapped[dict]=mapped_column(JSON)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    __table_args__=(CheckConstraint('chunk_count > 0',name='ck_index_document_chunk_count'),)


class BackgroundJob(Base):
    __tablename__='background_jobs'
    id: Mapped[str]=mapped_column(String(32),primary_key=True)
    user_id: Mapped[str]=mapped_column(ForeignKey('users.id'))
    idempotency_key: Mapped[str]=mapped_column(String(100))
    kind: Mapped[str]=mapped_column(String(32))
    payload: Mapped[dict]=mapped_column(JSON)
    payload_hash: Mapped[str]=mapped_column(String(64))
    binding: Mapped[dict]=mapped_column(JSON)
    status: Mapped[str]=mapped_column(String(24),default='queued')
    attempt: Mapped[int]=mapped_column(Integer,default=0)
    max_attempts: Mapped[int]=mapped_column(Integer,default=3)
    attempt_token: Mapped[str|None]=mapped_column(String(32),nullable=True)
    worker: Mapped[str|None]=mapped_column(String(160),nullable=True)
    heartbeat_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    lease_until: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    next_dispatch_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    dispatch_count: Mapped[int]=mapped_column(Integer,default=0)
    stage: Mapped[str]=mapped_column(String(40),default='queued')
    progress: Mapped[int]=mapped_column(Integer,default=0)
    total: Mapped[int]=mapped_column(Integer,default=0)
    checkpoint: Mapped[dict]=mapped_column(JSON,default=dict)
    result: Mapped[dict|None]=mapped_column(JSON,nullable=True)
    error_code: Mapped[str|None]=mapped_column(String(64),nullable=True)
    version: Mapped[int]=mapped_column(Integer,default=1)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    __table_args__=(
        UniqueConstraint('user_id','idempotency_key',name='uq_job_idempotency'),
        CheckConstraint("kind IN ('ingest','index','retrieval_evaluation')",name='ck_job_kind'),
        CheckConstraint("status IN ('queued','running','retry_wait','succeeded','failed','cancel_requested','cancelled')",name='ck_job_status'),
        CheckConstraint('attempt >= 0 AND attempt <= max_attempts AND max_attempts BETWEEN 1 AND 3',name='ck_job_attempts'),
        CheckConstraint('progress >= 0 AND total >= progress',name='ck_job_progress'),
        CheckConstraint('version >= 1 AND dispatch_count >= 0',name='ck_job_version'),
        Index('ix_job_dispatch','status','next_dispatch_at'),Index('ix_job_owner_created','user_id','created_at'),
    )

class BackgroundJobEvent(Base):
    __tablename__='background_job_events'
    id: Mapped[str]=mapped_column(String(32),primary_key=True)
    job_id: Mapped[str]=mapped_column(ForeignKey('background_jobs.id'))
    version: Mapped[int]=mapped_column(Integer)
    event_type: Mapped[str]=mapped_column(String(40))
    payload: Mapped[dict]=mapped_column(JSON)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    __table_args__=(UniqueConstraint('job_id','version',name='uq_job_event_version'),CheckConstraint('version >= 1',name='ck_job_event_version'))


class ExecutionRun(Base):
    __tablename__='execution_runs'
    id: Mapped[str]=mapped_column(String(32),primary_key=True)
    request_id: Mapped[str]=mapped_column(String(32),index=True)
    trace_id: Mapped[str]=mapped_column(String(32),index=True)
    session_id: Mapped[str]=mapped_column(String(128),index=True)
    subject_id: Mapped[str]=mapped_column(String(128),index=True)
    status: Mapped[str]=mapped_column(String(24))
    snapshot: Mapped[dict]=mapped_column(JSON)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    finished_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    __table_args__=(
        UniqueConstraint('id','session_id','subject_id',name='uq_run_owner_identity'),
        UniqueConstraint('id','request_id','trace_id',name='uq_run_correlation'),
        CheckConstraint("status IN ('queued','running','succeeded','blocked','failed','cancelled','waiting_approval','interrupted')",name='ck_run_status'),
        CheckConstraint("(status IN ('queued','running') AND finished_at IS NULL) OR (status NOT IN ('queued','running') AND finished_at IS NOT NULL)",name='ck_run_completion'),
    )


class ModelCallRecord(Base):
    __tablename__='model_call_records'
    sequence: Mapped[int]=mapped_column(Integer,primary_key=True,autoincrement=True)
    id: Mapped[str]=mapped_column(String(32),unique=True)
    run_id: Mapped[str]=mapped_column(ForeignKey('execution_runs.id',ondelete='CASCADE'),index=True)
    request_id: Mapped[str]=mapped_column(String(32),index=True)
    trace_id: Mapped[str]=mapped_column(String(32),index=True)
    kind: Mapped[str]=mapped_column(String(24))
    agent_role: Mapped[str]=mapped_column(String(64))
    operation: Mapped[str]=mapped_column(String(80))
    model: Mapped[str]=mapped_column(String(160))
    status: Mapped[str]=mapped_column(String(24))
    duration_ms: Mapped[float]=mapped_column(Numeric(14,3))
    payload: Mapped[dict]=mapped_column(JSON)
    __table_args__=(
        ForeignKeyConstraint(['run_id','request_id','trace_id'],['execution_runs.id','execution_runs.request_id','execution_runs.trace_id'],name='fk_call_run_correlation',ondelete='CASCADE'),
        CheckConstraint("kind IN ('chat','embedding','reranker')",name='ck_call_kind'),
        CheckConstraint("status IN ('succeeded','failed','cancelled')",name='ck_call_status'),
        CheckConstraint('duration_ms >= 0 AND duration_ms <= 99999999999.999',name='ck_call_duration'),
    )


class RunControl(Base):
    __tablename__='run_controls'
    run_id: Mapped[str]=mapped_column(String(32),primary_key=True)
    user_id: Mapped[str]=mapped_column(String(128))
    session_id: Mapped[str]=mapped_column(String(128))
    turn_id: Mapped[str]=mapped_column(ForeignKey('chat_turns.id'))
    auth_session_id: Mapped[str|None]=mapped_column(ForeignKey('auth_sessions.id'),nullable=True)
    client_request_id: Mapped[str]=mapped_column(String(100))
    request_hash: Mapped[str]=mapped_column(String(64))
    kind: Mapped[str]=mapped_column(String(8))
    status: Mapped[str]=mapped_column(String(24),default='queued')
    attempt_token: Mapped[str|None]=mapped_column(String(32),nullable=True)
    heartbeat_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    lease_until: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    cancel_code: Mapped[str|None]=mapped_column(String(64),nullable=True)
    next_event_sequence: Mapped[int]=mapped_column(Integer,default=1)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    __table_args__=(
        ForeignKeyConstraint(['run_id','session_id','user_id'],['execution_runs.id','execution_runs.session_id','execution_runs.subject_id'],name='fk_control_run_owner'),
        UniqueConstraint('user_id','client_request_id',name='uq_control_client_request'),
        CheckConstraint("kind IN ('new','resume')",name='ck_control_kind'),
        CheckConstraint("status IN ('queued','running','cancel_requested','succeeded','blocked','failed','cancelled','waiting_approval','interrupted')",name='ck_control_status'),
        CheckConstraint('next_event_sequence >= 1',name='ck_control_sequence'),
        Index('ix_control_dispatch','status','lease_until'),Index('ix_control_owner_created','user_id','created_at'))

class RunEvent(Base):
    __tablename__='run_events'
    run_id: Mapped[str]=mapped_column(ForeignKey('run_controls.run_id'),primary_key=True)
    sequence: Mapped[int]=mapped_column(Integer,primary_key=True)
    event_type: Mapped[str]=mapped_column(String(32))
    payload: Mapped[dict]=mapped_column(JSON)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    __table_args__=(CheckConstraint('sequence >= 1',name='ck_run_event_sequence'),)


class KnowledgeAdminEvent(Base):
    __tablename__='knowledge_admin_events'
    id: Mapped[str]=mapped_column(String(32),primary_key=True)
    user_id: Mapped[str]=mapped_column(ForeignKey('users.id'))
    kind: Mapped[str]=mapped_column(String(32))
    resource_id: Mapped[str]=mapped_column(String(128))
    payload: Mapped[dict]=mapped_column(JSON)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    __table_args__=(CheckConstraint("kind IN ('document_published','document_withdrawn','index_activated')",name='ck_knowledge_admin_kind'),Index('ix_knowledge_admin_created','created_at'))
