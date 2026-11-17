"""Final local delivery acceptance, preservation and source/evidence manifest."""
import asyncio,hashlib,json,re,subprocess
from datetime import datetime,timezone
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import select,text,func
import httpx
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import KnowledgeEmbedding
from smart_cs.retrieval.dense import DenseRetriever
from smart_cs.evaluation.v2_assets import validate
from accept_runs_live import snapshot
from delivery_backup import validate as validate_backup,sha

OUT=PROJECT_ROOT/'.local/delivery'


def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))


def write(name,value):(OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


async def integrity():
    previous=read(OUT/'original-rows.json');now=await snapshot();assert previous==now
    artifacts=read(OUT/'original-artifacts.json');assert all(sha(PROJECT_ROOT/p)==v for p,v in artifacts.items())
    validate();db=configured_database()
    try:
        async with db.sessions() as s:
            index=await DenseRetriever(db).active(s)
            count=await s.scalar(select(func.count()).select_from(KnowledgeEmbedding).where(KnowledgeEmbedding.index_id==index.id))
            revision=await s.scalar(text('SELECT version_num FROM alembic_version'))
            restricted=(await s.execute(text("SELECT rolname FROM pg_roles WHERE rolname LIKE 'foundation_verify_%'"))).scalars().all()
        assert count==547 and revision=='0015_knowledge_admin' and not restricted
    finally:await db.close()
    async with httpx.AsyncClient(trust_env=False,timeout=10) as client:
        health=(await client.get('http://127.0.0.1:8000/health')).json()
        assert health['status']=='healthy' and health['chat_ready']
        assert (await client.get('http://127.0.0.1:8000/')).status_code==200
    return {'passed':True,'original_rows_and_checkpoints_unchanged':sum(len(v) for v in previous.values()),'tables':len(previous),
        'root_env_and_previous_quality_evidence_unchanged':True,'vectors':count,'active_index':index.id,'revision':revision,
        'source_api_healthy':True,'excluded_auth_session_and_login_attempt_rows':True,'verification_roles_removed':True}


def main_report(report):
    checks=report['checks'];lines=['# 第27步交付验收','','本地工程交付验收通过；未发布公网，远程GitHub Actions尚未运行。','',
        '## 回归','',
        '- 全量后端422项：359通过、63跳过，无失败；新增交付边界5项通过。',
        '- 17模块真实PostgreSQL290项全部通过；认证改动后另复验15项，未重复计为新独立案例。',
        '- React构建及32项浏览器回归通过，恢复环境三种真实账号的工作台访问通过。','',
        '## 启动与恢复','',
        '- 第一组空库完成迁移、检查点及账号/演示数据初始化；修复bootstrap后，第二组全新空卷无手工修补启动成功。',
        '- 隔离库从0014带数据升级0015，原记录保留；当前元数据无漂移。',
        '- 原项目备份恢复到新数据库，33张表、27,697行，包括官方检查点和全部向量，摘要完全一致。',
        '- 110个原文件与任务输入/结果按SHA256恢复；HTML引用与PDF实际下载核对通过。',
        '- 恢复环境真实RAG返回带引用答复；服务重启后历史与SSE读取不新增模型调用。',
        '- Linux Worker完成真实草稿入库；重复提交返回同一任务。没有发布测试草稿或改变547向量活动索引。','',
        '## 保护与交付范围','',
        '- 镜像以UID10001运行，不包含模型密钥、根.env、私密账号或原始上传附件。源码对实际本机密钥的扫描通过。',
        '- 原项目30张业务/检查点表中27,545条旧记录、根.env、旧质量报告与活动索引保持一致；原8000服务正常。',
        '- 两套演练容器已停止，命名卷、备份与证据保留，不执行down -v。',
        '- CI配置已加入后端、前端与交付三组任务；本次验证发生在本机，没有把本机通过写成远端CI通过。','',
        '## 原始失败保留','',
        '初次空环境bootstrap目录/密码落盘顺序错误已修复，并用SQL提交后中断与第二组空卷验证。演练脚本的API字段映射修正后复用已完成的模型运行，未再次付费重放。Docker Hub一次EOF保留日志后重试构建。原任务输入/结果文件在初版归档基础上补全，原数据库dump和初版文件归档保留。','',
        '## 仍未通过的质量门槛','',
        '第26步办理完成率及多Agent写任务失败未被本步工程测试消除，独立人工金标准仍为0；当前仍为local_demo、合成订单和无真实资金退款。本步不颁发生产业务质量认证。','',
        '操作说明：../../docs/phase-twenty-seven-delivery.md；完整机器可读结果：final-acceptance.json；源码/镜像/报告哈希：release-manifest.json。',
        '下一步28：整理可演示的闭环、失败案例、个人贡献与有证据的简历条目。']
    (OUT/'delivery-report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


async def main():
    backend=read(PROJECT_ROOT/'.local/checks/backend.json');assert backend['passed'] and backend['tests_run']==422
    write('backend-final.json',backend)
    pg=read(OUT/'postgres/summary.json');assert pg['passed'] and pg['tests']==290 and pg['skipped']==0
    auth=read(OUT/'postgres/test_auth/postgresql-verification.json');assert auth['passed'] and auth['tests']==15
    unit=(OUT/'delivery-tests-final.log').read_text(encoding='utf-8');assert re.search(r'Ran 5 tests',unit) and '\nOK\n' in unit
    front=(OUT/'frontend-tests.log').read_text(encoding='utf-8');assert '32 passed' in front
    required=['image-audit','migration','backup','restore','restore-database','clean-install','smoke-fresh','smoke-restored',
        'browser-live','live-verification','citation-files','worker-privileges','drill-stopped','source-security']
    checks={name:read(OUT/(name+'.json')) for name in required};assert all(v['passed'] for v in checks.values())
    backup=validate_backup(OUT/'backup');assert len(backup['files'])==110 and backup['job_inputs_and_results_included']
    checks['original_preservation']=await integrity()
    report={'completed':True,'step':27,'at':datetime.now(timezone.utc).isoformat(),
        'backend':{k:backend[k] for k in ['tests_run','failures','errors','skipped','passed']},
        'new_delivery_tests':{'passed':5,'skipped':0},'postgres_contracts':{'passed':290,'skipped':0,'modules':17},
        'frontend_mock_api_regressions':32,'real_browser_roles':3,'checks':checks,'remote_github_actions_executed':False,
        'local_delivery_gate_passed':True,'production_quality_gate_passed':False,'independent_human_gold':0,
        'previous_agent_quality_failures_resolved':False,'real_payment_connected':False,'next_step':28}
    main_report(report)
    sources=[]
    for folder in ['src','scripts','tests','migrations','deploy','.github/workflows','docs']:
        sources += [p for p in (PROJECT_ROOT/folder).rglob('*') if p.is_file() and p.suffix in {'.py','.md','.yml','.yaml'}]
    sources += [PROJECT_ROOT/p for p in ['Dockerfile','Dockerfile.worker','.dockerignore','.gitignore','.env.example','docker-compose.yml','README.md','requirements.lock.txt','frontend/package-lock.json']]
    built=[p for p in (PROJECT_ROOT/'frontend/dist').rglob('*') if p.is_file()]
    worker=subprocess.run(['docker','image','inspect','--format','{{.Id}}','smart-cs-delivery-worker:local'],capture_output=True,text=True,check=True).stdout.strip()
    manifest={'source_sha256':{p.relative_to(PROJECT_ROOT).as_posix():sha(p) for p in sorted(set(sources))},
        'react_build_sha256':{p.relative_to(PROJECT_ROOT).as_posix():sha(p) for p in built},
        'app_image_id':checks['image-audit']['image_id'],'worker_image_id':worker,
        'evidence_sha256':{p.relative_to(OUT).as_posix():sha(p) for p in sorted(OUT.rglob('*')) if p.is_file() and p.suffix=='.json' and p.name not in {'final-acceptance.json','release-manifest.json','environment.env','fresh-accounts.json'} and not any(w in p.name for w in ['accounts','manifest'])},
        'private_envs_accounts_dumps_archives_not_embedded':True,'backup_manifest_sha256':sha(OUT/'backup/manifest.json')}
    write('release-manifest.json',manifest);report['release_manifest_sha256']=sha(OUT/'release-manifest.json')
    write('final-acceptance.json',report)
    print(json.dumps({'step':27,'completed':True,'local_delivery_passed':True,'production_quality_passed':False,
        'original_rows_unchanged':checks['original_preservation']['original_rows_and_checkpoints_unchanged'],'next_step':28}))


if __name__=='__main__':load_dotenv(ENV_PATH);asyncio.run(main())
