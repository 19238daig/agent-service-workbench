"""Start only owned loopback model helpers without downloading weights."""
import os
import subprocess
import sys
import time
from pathlib import Path
import httpx
from smart_cs.settings import PROJECT_ROOT
from smart_cs.retrieval.endpoints import validate_local_endpoint


def ensure_runtime():
    logs=PROJECT_ROOT/'.local/rag';logs.mkdir(parents=True,exist_ok=True)
    base=validate_local_endpoint(os.getenv('RAG_EMBEDDING_URL','http://127.0.0.1:61535'))
    rerank=validate_local_endpoint(os.getenv('RAG_RERANKER_URL','http://127.0.0.1:61536'))
    env=os.environ.copy()
    env.update(OLLAMA_HOST=base.replace('http://',''),OLLAMA_MODELS=str(PROJECT_ROOT/'.local/ollama'),OLLAMA_MAX_LOADED_MODELS='1',OLLAMA_NUM_PARALLEL='1',PYTHONUTF8='1')
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    def ready(url,kind):
        try:
            r=httpx.get(url,trust_env=False,timeout=2)
            if not r.is_success:return False
            data=r.json()
            return isinstance(data.get('models'),list) if kind=='ollama' else data.get('service')=='smart-cs-local-reranker' and data.get('ready')
        except Exception:return False
    def launch(name,args):
        with (logs/(name+'.out.log')).open('ab') as out,(logs/(name+'.err.log')).open('ab') as err:
            process=subprocess.Popen(args,env=env,cwd=PROJECT_ROOT,stdout=out,stderr=err,creationflags=flags)
        (logs/(name+'.pid')).write_text(str(process.pid),encoding='ascii')
    if not ready(base+'/api/tags','ollama'):
        launch('ollama',[os.environ['RAG_OLLAMA_EXE'],'serve'])
        for i in range(40):
            if ready(base+'/api/tags','ollama'):break
            time.sleep(0.5)
        else:raise RuntimeError('Local Ollama is not ready; see .local/rag logs')
    if not ready(rerank+'/health','reranker'):
        env['RAG_RERANKER_PORT']=rerank.rsplit(':',1)[-1]
        launch('reranker',[os.getenv('RAG_WORKER_PYTHON',sys.executable),str(PROJECT_ROOT/'scripts/local_reranker_server.py')])
        for i in range(80):
            if ready(rerank+'/health','reranker'):break
            time.sleep(0.5)
        else:raise RuntimeError('Local reranker is not ready; see .local/rag logs')
    models=httpx.get(base+'/api/tags',trust_env=False,timeout=3).json()['models']
    model=os.getenv('RAG_EMBEDDING_MODEL','smart-cs-bge-m3')
    if not any(m['name'].split(':')[0]==model for m in models):
        source=Path(os.environ['RAG_EMBEDDING_GGUF']).resolve()
        if not source.is_file():raise ValueError('Local GGUF file not found')
        modelfile=logs/'Modelfile'
        modelfile.write_text('FROM "'+str(source).replace('\\','/')+'"\nPARAMETER num_ctx 2048\n',encoding='utf-8')
        result=subprocess.run([os.environ['RAG_OLLAMA_EXE'],'create',model,'-f',str(modelfile)],env=env,cwd=PROJECT_ROOT,capture_output=True)
        if result.returncode:raise RuntimeError('Local GGUF import failed; no new weights were downloaded')
