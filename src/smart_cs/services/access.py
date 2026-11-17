"""Visibility predicates shared by retrieval, catalogue and source-file access."""
from contextvars import ContextVar
from sqlalchemy import and_, or_
from smart_cs.db.models import KnowledgeDocument

principal_context = ContextVar('trusted_principal', default=None)


def document_visibility(identity=None):
    identity = identity or principal_context.get()
    scopes = ['public']
    if identity and identity.authenticated:
        if identity.role in {'support','admin'}:
            scopes.append('support')
        if identity.role == 'admin':
            scopes.append('admin')
        return or_(KnowledgeDocument.access_scope.in_(scopes), and_(
            KnowledgeDocument.access_scope == 'private', KnowledgeDocument.owner_id == identity.subject_id))
    return KnowledgeDocument.access_scope == 'public'


def can_read_document(document, identity):
    scope = document.access_scope
    if not identity.authenticated:return scope == 'public'
    return (scope == 'public' or
            (scope == 'private' and document.owner_id == identity.subject_id) or
            (scope == 'support' and identity.role in {'support','admin'}) or
            (scope == 'admin' and identity.role == 'admin'))
