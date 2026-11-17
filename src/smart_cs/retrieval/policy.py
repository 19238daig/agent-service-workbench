"""Current version within a visible source/ACL family; no expired-version revival."""
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select,func,case,or_
from smart_cs.db.models import KnowledgeDocument,KnowledgeIngestion
from smart_cs.services.access import document_visibility

COLLECTIONS=('consumer_reference','helpdesk_reference')


def business_date():return datetime.now(ZoneInfo('Asia/Shanghai')).date()


def current_document_ids(identity=None,as_of=None):
    day=(as_of or business_date()).isoformat()
    # Select the latest started revision before checking its expiration/status.
    # An expired/withdrawn latest revision must not revive an obsolete one.
    family=case((KnowledgeIngestion.id.is_not(None),KnowledgeIngestion.source_key),else_=KnowledgeDocument.id)
    ranking=select(KnowledgeDocument.id.label('document_id'),KnowledgeDocument.status.label('status'),
        KnowledgeDocument.effective_until.label('effective_until'),
        func.row_number().over(partition_by=[family,KnowledgeDocument.access_scope,func.coalesce(KnowledgeDocument.owner_id,'')],
            order_by=[KnowledgeDocument.effective_from.desc(),KnowledgeIngestion.created_at.desc().nulls_last(),KnowledgeDocument.id.desc()]).label('revision_rank')
        ).outerjoin(KnowledgeIngestion,KnowledgeIngestion.document_id==KnowledgeDocument.id).where(
            document_visibility(identity),KnowledgeDocument.status.in_(['published','withdrawn','superseded']),
            KnowledgeDocument.effective_from<=day).subquery()
    return select(ranking.c.document_id).where(ranking.c.revision_rank==1,ranking.c.status=='published',
        or_(ranking.c.effective_until.is_(None),ranking.c.effective_until>day))
