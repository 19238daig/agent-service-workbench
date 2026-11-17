"""Local operator account provisioning. Passwords are never printed or passed in argv."""
import argparse
import asyncio
import json
import secrets
import os
import getpass
import subprocess
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import select, update
from smart_cs.settings import PROJECT_ROOT, ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import AuthCredential, AuthSession, User, utcnow
from smart_cs.services.auth import AuthService, hash_password,verify_password


def restrict_file(path):
    if os.name=='nt':
        owner=os.environ.get('USERDOMAIN','')+'\\'+getpass.getuser()
        result=subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r',owner+':F','SYSTEM:F'],capture_output=True)
        if result.returncode:raise RuntimeError('Could not restrict account file permissions')
    else:path.chmod(0o600)


def private_output(path, accounts):
    path.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive creation avoids overwriting the only copy of an existing password.
    with path.open('x',encoding='utf-8') as stream:
        json.dump({'url':'http://127.0.0.1:8000','accounts':accounts},stream,ensure_ascii=False,indent=2)
        stream.write('\n')
    restrict_file(path)


async def execute(args):
    db=configured_database()
    if db is None:raise ValueError('Database is not configured')
    try:
        service=AuthService(db)
        if args.action=='bootstrap':
            output=PROJECT_ROOT/'.local/auth/bootstrap-accounts.json'
            if output.exists():
                print('Existing credentials retained: .local/auth/bootstrap-accounts.json');return
            output.parent.mkdir(parents=True,exist_ok=True)
            pending=output.with_name('bootstrap-pending.json')
            accounts=json.loads(pending.read_text(encoding='utf-8'))['accounts'] if pending.exists() else []
            def save_pending():
                temporary=pending.with_name('bootstrap-'+secrets.token_hex(8)+'.writing')
                temporary.touch(exist_ok=False);restrict_file(temporary)
                temporary.write_text(json.dumps({'accounts':accounts},ensure_ascii=False,indent=2),encoding='utf-8')
                temporary.replace(pending)
            # New identities never inherit the anonymous or synthetic fixture data.
            for login,display,role in [('admin','本机管理员','admin'),('support','客服演示','support'),
                                       ('customer-a','用户 A','customer'),('customer-b','用户 B','customer')]:
                async with db.sessions() as session:
                    existing=await session.scalar(select(AuthCredential).where(AuthCredential.login_name==login))
                planned=next((a for a in accounts if a['login_name']==login),None)
                if existing:
                    if planned:
                        if not verify_password(planned['password'],existing.password_hash):raise ValueError('Pending account differs from the existing credential')
                        planned['user_id']=existing.user_id;save_pending()
                    continue
                password=planned['password'] if planned else secrets.token_urlsafe(24)
                if planned is None:
                    planned={'login_name':login,'password':password,'user_id':None,'role':role};accounts.append(planned)
                # Persist recovery intent before committing the credential to SQL.
                save_pending()
                user=await service.provision(login,password,display,role)
                planned['user_id']=user.id;save_pending()
            pending=output.with_name('bootstrap-pending.json')
            if pending.exists():accounts=json.loads(pending.read_text(encoding='utf-8'))['accounts']
            private_output(output,accounts)
            if pending.exists():pending.unlink()
            print('Credentials saved privately: .local/auth/bootstrap-accounts.json')
        elif args.action=='create':
            password=secrets.token_urlsafe(24)
            user=await service.provision(args.login,password,args.display_name or args.login,args.role)
            output=PROJECT_ROOT/'.local/auth'/('account-'+user.id+'.json')
            private_output(output,[{'login_name':args.login,'password':password,'user_id':user.id,'role':user.role}])
            print('Credentials saved privately: '+str(output.relative_to(PROJECT_ROOT)))
        else:
            async with db.sessions.begin() as session:
                credential=await session.scalar(select(AuthCredential).where(AuthCredential.login_name==args.login).with_for_update())
                if credential is None:raise ValueError('Account does not exist')
                if args.action=='disable':credential.enabled=False
                if args.action=='reset':
                    password=secrets.token_urlsafe(24)
                    credential.password_hash=await asyncio.to_thread(hash_password,password)
                    credential.enabled=True;credential.updated_at=utcnow()
                    output=PROJECT_ROOT/'.local/auth'/('reset-'+secrets.token_hex(6)+'.json')
                    private_output(output,[{'login_name':args.login,'password':password}])
                    print('Credentials saved privately: '+str(output.relative_to(PROJECT_ROOT)))
                await session.execute(update(AuthSession).where(AuthSession.user_id==credential.user_id).values(revoked_at=utcnow()))
            print('Account updated; existing sessions revoked.')
    finally:await db.close()


def main():
    load_dotenv(ENV_PATH)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['bootstrap','create','reset','disable'])
    parser.add_argument('--login')
    parser.add_argument('--display-name')
    parser.add_argument('--role',choices=['customer','support','admin'],default='customer')
    args=parser.parse_args()
    if args.action!='bootstrap' and not args.login:parser.error('--login is required')
    try:asyncio.run(execute(args))
    except Exception as error:
        print('Account operation failed: '+type(error).__name__);return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
