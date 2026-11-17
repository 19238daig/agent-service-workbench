"""Explicit adapter for pre-auth offline workflow tests, never imported by src/.

These tests exercise deterministic fake-model behaviour. The separate auth suite
uses real password/session verification and no dependency overrides.
"""
from contextlib import contextmanager
from fastapi import Request
from smart_cs.schemas.identity import IdentityContext
from smart_cs.api.dependencies import current_identity


async def offline_identity(request: Request):
    body=await request.json() if request.method=='POST' else {}
    subject=body.get('user_id') or request.query_params.get('user_id') or 'anonymous'
    return IdentityContext(subject_id=subject,role='admin')


@contextmanager
def override_identity(app):
    prior=app.dependency_overrides.copy()
    app.dependency_overrides[current_identity]=offline_identity
    try:yield
    finally:app.dependency_overrides.clear();app.dependency_overrides.update(prior)


def install_offline_identity(test, app):
    context=override_identity(app);context.__enter__()
    test.addCleanup(context.__exit__,None,None,None)
