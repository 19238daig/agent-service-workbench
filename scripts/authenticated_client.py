"""Local verification credentials; never send them to an arbitrary --url host."""
from contextlib import contextmanager, asynccontextmanager
import json
from smart_cs.settings import PROJECT_ROOT


def account(client, login_name):
    if str(client.base_url).rstrip('/') not in {'http://127.0.0.1:8000','http://localhost:8000'}:
        raise ValueError('Automatic verification login is limited to this local application')
    records=json.loads((PROJECT_ROOT/'.local/auth/bootstrap-accounts.json').read_text(encoding='utf-8'))['accounts']
    return next(record for record in records if record['login_name']==login_name)


@contextmanager
def authenticated_client(client, login_name='admin'):
    record=account(client,login_name)
    response=client.post('/api/auth/login',json={'login_name':login_name,'password':record['password']})
    if response.status_code!=200:raise RuntimeError('Verification login failed')
    client.headers['Authorization']='Bearer '+response.json()['access_token']
    try:yield response.json()['user']
    finally:
        client.post('/api/auth/logout')
        client.headers.pop('Authorization',None)


@asynccontextmanager
async def authenticated_async_client(client, login_name='admin'):
    record=account(client,login_name)
    response=await client.post('/api/auth/login',json={'login_name':login_name,'password':record['password']})
    if response.status_code!=200:raise RuntimeError('Verification login failed')
    client.headers['Authorization']='Bearer '+response.json()['access_token']
    try:yield response.json()['user']
    finally:
        await client.post('/api/auth/logout')
        client.headers.pop('Authorization',None)
