"""Exact-model manual lookup and a narrowly scoped, customer-confirmed workflow."""
import asyncio
import hashlib
import re
from datetime import timedelta, timezone
from pathlib import Path

from sqlalchemy import case, select, text

from smart_cs.db.models import AuthCredential, KnowledgeDocument, KnowledgeIngestion, Order, SelfServiceCase, User, utcnow
from smart_cs.repositories.tickets import TicketUnitOfWork
from smart_cs.retrieval.indexes import publication_lock
from smart_cs.retrieval.policy import current_document_ids
from smart_cs.schemas.errors import ApplicationError
from smart_cs.schemas.tickets import TicketRegistration
from smart_cs.services.tickets import ACTIVE
from smart_cs.settings import PROJECT_ROOT

MANUAL = re.compile(r'说明书|使用手册|用户手册')
HUMAN_REVIEW = re.compile(r'破损|损坏|损毁|破裂|碎|赔偿|赔付|补偿|退款|退货|划痕|漏电|冒烟|发热|受伤|召回|安全隐患')
SUMMARIES = {
    'manual': '商品说明书咨询',
    'damage': '商品破损待核实',
    'compensation': '赔偿诉求待核实',
    'other': '售后问题待核实',
}
REVIEW_ITEMS = {
    'manual': '核对商品准确型号及可提供的说明书版本',
    'damage': '核对订单、商品外观和收货情况',
    'compensation': '核对订单、用户诉求及可适用的售后规则',
    'other': '核对诉求及处理所需材料',
}


def conflict(code, message):
    return ApplicationError(code, message, 409)


def manual_ticket(ticket):
    content = f'{ticket.summary}\n{ticket.details}'
    return (ticket.type == 'general' and ticket.status == 'created' and ticket.assignee_id is None
            and ticket.requested_amount is None and MANUAL.search(content) and not HUMAN_REVIEW.search(content))


def check_file(doc, root):
    if not doc.file_path:
        return False
    path = (root / doc.file_path).resolve()
    if not path.is_relative_to((root / 'data').resolve()) or path.suffix.lower() not in {'.pdf','.html','.htm','.md','.txt'}:
        return False
    try:
        if not path.is_file() or path.stat().st_size > 25 * 1024 * 1024:
            return False
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest() == doc.content_hash
    except OSError:
        return False


class SelfService:
    def __init__(self, database, root=PROJECT_ROOT):
        self.db = database
        self.root = Path(root).resolve()

    async def manual(self, session, identity, model_number):
        key = 'manual:' + model_number.lower()
        doc = await session.scalar(
            select(KnowledgeDocument).join(KnowledgeIngestion, KnowledgeIngestion.document_id == KnowledgeDocument.id)
            .where(KnowledgeIngestion.source_key == key, KnowledgeDocument.access_scope == 'public',
                   KnowledgeDocument.id.in_(current_document_ids(identity)))
        )
        return doc if doc and await asyncio.to_thread(check_file, doc, self.root) else None

    async def owned_case(self, session, identity, identifier, *, lock=False):
        statement = select(SelfServiceCase).where(SelfServiceCase.user_id == identity.subject_id, SelfServiceCase.id == identifier)
        row = await session.scalar(statement.with_for_update() if lock else statement)
        if row is None:
            raise ApplicationError('SELF_SERVICE_NOT_FOUND', '当前账号未找到此自助处理记录。', 404)
        return row

    async def owned_ticket(self, unit, identity, request):
        ticket = await unit.get(identity, request['ticket_id'])
        if ticket.user_id != identity.subject_id:
            raise ApplicationError('TICKET_NOT_FOUND', '当前账号未找到此工单。', 404)
        if ticket.status not in ACTIVE:
            raise conflict('TICKET_NOT_ACTIVE', '此工单已经结束，请刷新工单详情。')
        if request['order_id'] and request['order_id'] != ticket.order_id:
            raise conflict('TICKET_ORDER_MISMATCH', '订单号与原工单不一致。')
        return ticket

    async def latest_view(self, session, identity, row, doc=None):
        result = {'id': row.id, 'request': row.request, 'status': 'handoff_available', 'manual': None, 'receipt': row.receipt}
        if row.receipt:
            result['status'] = row.decision
            return result
        expires = row.created_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if utcnow() - expires > timedelta(minutes=30):
            result['status'] = 'expired'
            return result
        if row.document_id and doc is None:
            doc = await self.manual(session, identity, row.request['model_number'])
        if doc and doc.id == row.document_id and doc.content_hash == row.document_hash:
            result['status'] = 'manual_available'
            result['manual'] = {
                'title': doc.title, 'version': doc.version, 'source': doc.source,
                'publisher': (doc.metadata_json or {}).get('publisher') or '',
                'file_url': f'/api/knowledge/documents/{doc.id}/file',
            }
        return result

    async def start(self, identity, body):
        request = body.model_dump(mode='json', exclude={'request_id'})
        if request['model_number']:
            request['model_number'] = request['model_number'].upper()
        identifier = str(body.request_id)
        async with self.db.sessions.begin() as session:
            if self.db.engine.dialect.name == 'postgresql':
                await session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                                      {'key': f'self-service:{identity.subject_id}:{identifier}'})
            previous = await session.get(SelfServiceCase, (identity.subject_id, identifier))
            if previous:
                if previous.request != request:
                    raise conflict('SELF_SERVICE_REQUEST_CHANGED', '此次请求内容已变化，请重新开始处理。')
                return await self.latest_view(session, identity, previous)
            ticket = await self.owned_ticket(TicketUnitOfWork(session), identity, request) if request['ticket_id'] else None
            if request['order_id'] and not ticket:
                order = await session.scalar(select(Order.id).where(Order.id == request['order_id'], Order.user_id == identity.subject_id))
                if not order:
                    raise ApplicationError('ORDER_NOT_FOUND', '当前账号未找到此订单。', 404)
            doc = None
            if request['kind'] == 'manual' and not HUMAN_REVIEW.search(request['issue']) and (not ticket or manual_ticket(ticket)):
                doc = await self.manual(session, identity, request['model_number'])
            row = SelfServiceCase(
                user_id=identity.subject_id, id=identifier, request=request,
                ticket_version=ticket.version if ticket else None,
                document_id=doc.id if doc else None, document_hash=doc.content_hash if doc else None,
                created_at=utcnow(),
            )
            session.add(row)
            return await self.latest_view(session, identity, row, doc)

    async def get(self, identity, identifier):
        async with self.db.sessions() as session:
            return await self.latest_view(session, identity, await self.owned_case(session, identity, identifier))

    async def choose_staff(self, unit, current):
        if current and await unit.assignable_staff(current):
            return current
        return await unit.session.scalar(
            select(User.id).join(AuthCredential, AuthCredential.user_id == User.id)
            .where(User.role.in_(['support','admin']), AuthCredential.enabled.is_(True))
            .order_by(case((User.role == 'support', 0), else_=1), User.id).limit(1)
        )

    async def decide(self, identity, identifier, decision):
        async with self.db.sessions.begin() as session:
            row = await self.owned_case(session, identity, identifier, lock=True)
            if row.receipt:
                if row.decision != decision:
                    raise conflict('SELF_SERVICE_ALREADY_DECIDED', '此处理请求已有结果，请查看原工单。')
                return await self.latest_view(session, identity, row)
            request = row.request
            unit = TicketUnitOfWork(session)
            ticket = await self.owned_ticket(unit, identity, request) if request['ticket_id'] else None
            if ticket and ticket.version != row.ticket_version:
                raise conflict('TICKET_VERSION_CONFLICT', '工单已更新，请刷新详情后重新开始。')
            if decision == 'resolved':
                if not row.document_id or request['kind'] != 'manual' or (ticket and not manual_ticket(ticket)):
                    raise conflict('MANUAL_RESOLUTION_UNAVAILABLE', '当前问题需要客服核实，无法自助标记为已解决。')
                await publication_lock(self.db, session)
                evidence = await self.latest_view(session, identity, row)
                if evidence['status'] != 'manual_available':
                    raise conflict('MANUAL_OFFER_STALE', '说明书已更新或本次查找已过期，请重新查找。')
                title = SUMMARIES['manual']
                note = f"用户确认说明书已解决问题。型号 {request['model_number']}，资料 {row.document_id}，版本 {evidence['manual']['version']}。"
                target, event, staff = 'resolved', 'self_service_resolved', None
            else:
                kind = request['kind']
                ticket_type = 'complaint' if kind in {'damage','compensation'} else 'general'
                title = SUMMARIES[kind]
                staff = await self.choose_staff(unit, ticket.assignee_id if ticket else None)
                summary = request['issue']
                model = f"；型号 {request['model_number']}" if request['model_number'] else ''
                order_id = ticket.order_id if ticket else request['order_id']
                order = ''
                if order_id:
                    facts = await session.get(Order, order_id)
                    if not facts or facts.user_id != identity.subject_id:
                        raise ApplicationError('ORDER_NOT_FOUND', '当前账号未找到此订单。', 404)
                    statuses = {'pending_shipment':'待发货','shipped':'运输中','delivered':'已签收','cancelled':'已取消'}
                    order = f'；订单 {facts.id}，{statuses[facts.status]}，商品 {facts.product_name[:45]}'
                note = f'用户请求人工处理。待办：{REVIEW_ITEMS[kind]}。用户反馈：{summary[:170]}{model}{order}。'
                # Protected tickets retain their existing business status and approval rules.
                target = ticket.status if ticket and ticket.source == 'approved_action' else 'escalated'
                event = 'self_service_handoff'
            if not ticket:
                body = TicketRegistration(
                    type='general' if decision == 'resolved' else ticket_type,
                    summary=title, details=request['issue'],
                    order_id=request['order_id'], confirm_registration=True,
                )
                ticket = await unit.create(identity, body, 'self_service')
            ticket = await unit.change(
                ticket, identity, ticket.version, event, note, source='self_service', target_status=target,
                change_assignee=decision == 'handoff', assignee_id=staff,
            )
            row.decision = decision
            row.receipt = {'ticket_id': ticket.id, 'ticket_status': ticket.status, 'ticket_version': ticket.version,
                           'assigned_to_staff': bool(ticket.assignee_id)}
            return await self.latest_view(session, identity, row)
