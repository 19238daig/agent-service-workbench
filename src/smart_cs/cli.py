"""Launch the local service and optionally open its API page."""
import argparse
import json
import os
import socket
import sys
import threading
import time
import subprocess
from urllib.request import ProxyHandler, Request, build_opener
import webbrowser

from dotenv import load_dotenv
import uvicorn

from smart_cs.settings import PROJECT_ROOT, ENV_PATH


def is_project_service(base_url):
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(Request(base_url + '/openapi.json'), timeout=2) as reply:
            schema = json.load(reply)
        return schema.get('info', {}).get('title') == '智能客服多Agent系统'
    except (OSError, ValueError):
        return False


def open_page_when_ready(base_url):
    for _ in range(120):
        if is_project_service(base_url):
            if webbrowser.open(base_url + '/', new=2):
                print('已在浏览器打开智能客服聊天界面。', flush=True)
            else:
                print('请手动打开：' + base_url + '/', flush=True)
            return
        time.sleep(0.25)
    print('浏览器未自动打开。请查看上方启动错误，或手动访问：' + base_url + '/', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--open-browser', action='store_true')
    parser.add_argument('--port', type=int)
    args = parser.parse_args()
    root = PROJECT_ROOT
    os.chdir(root)
    load_dotenv(ENV_PATH)
    if not (PROJECT_ROOT / 'frontend' / 'dist' / 'index.html').is_file():
        print('聊天页面尚未构建。请在 frontend 目录执行 npm ci 和 npm run build 后再启动。', flush=True)
        return 1
    host = os.getenv('HOST', '127.0.0.1')
    port = args.port if args.port is not None else int(os.getenv('PORT', '8000'))
    base_url = f'http://127.0.0.1:{port}'
    print('Python：' + sys.executable, flush=True)
    print('虚拟环境：' + sys.prefix, flush=True)
    print('聊天界面：' + base_url + '/', flush=True)
    print('接口文档：' + base_url + '/docs', flush=True)
    with socket.socket() as probe:
        probe.settimeout(2)
        listening = probe.connect_ex(('127.0.0.1', port)) == 0
    if listening:
        if not is_project_service(base_url):
            print(f'错误：端口 {port} 被其他程序占用，请先确认端口使用情况。', flush=True)
            return 1
        print('检测到智能客服服务已在运行。', flush=True)
        if args.open_browser:
            open_page_when_ready(base_url)
        return 0
    if os.getenv('DATABASE_URL', '').strip():
        command = [sys.executable, '-m', 'smart_cs.foundation', 'prepare']
        if os.getenv('FOUNDATION_AUTO_START', 'false').lower() == 'true':
            command.append('--start-services')
        print('正在检查数据库迁移和演示数据，已有业务记录会保留。', flush=True)
        if subprocess.run(command, cwd=root).returncode:
            print('数据服务尚未就绪。请启动 Docker Desktop 后重试。', flush=True)
            return 1
    if os.getenv('RAG_ENABLED', 'false').lower() == 'true':
        print('正在检查本地 BGE 向量与重排服务。', flush=True)
        if subprocess.run([sys.executable, '-m', 'smart_cs.rag_cli', 'start-models'], cwd=root).returncode:
            print('本地检索模型未就绪，请检查模型路径和 .local/rag 日志。', flush=True)
            return 1
    if os.getenv('JOB_WORKER_AUTO_START','false').lower()=='true':
        from smart_cs.background.startup import ensure_jobs
        print('正在检查 Linux 后台 Worker 和任务队列。',flush=True)
        try:ensure_jobs()
        except Exception:
            print('后台 Worker 暂未就绪；API 继续启动，待处理任务保存在数据库。请查看第19步文档。',flush=True)
    print('正在启动智能客服服务。此窗口用于显示运行日志；按 Ctrl+C 可停止服务。', flush=True)
    if args.open_browser:
        threading.Thread(target=open_page_when_ready, args=(base_url,), daemon=True).start()
    uvicorn.run('smart_cs.api.main:app', host=host, port=port,loop='smart_cs.persistence.loop:loop_factory')
    return 0


if __name__ == '__main__':
    sys.exit(main())
