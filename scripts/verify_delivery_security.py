"""Check publishable source against actual local secrets without printing them."""
import json,subprocess
from pathlib import Path
from dotenv import dotenv_values
from smart_cs.settings import PROJECT_ROOT,ENV_PATH


def main():
    values=dotenv_values(ENV_PATH)
    secrets=[v.encode() for k,v in values.items() if v and len(v)>=8 and (k.endswith('API_KEY') or k in {'POSTGRES_PASSWORD','JOB_DB_PASSWORD'})]
    process=subprocess.run(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=PROJECT_ROOT,capture_output=True,check=True)
    names=set(process.stdout.decode().split('\0'))-{''};hits=[];scanned=0
    for name in sorted(names):
        path=PROJECT_ROOT/name
        if not path.is_file():continue
        data=path.read_bytes();scanned+=1
        if any(secret in data for secret in secrets):hits.append(name)
    tracked=subprocess.run(['git','ls-files','--error-unmatch','.env'],cwd=PROJECT_ROOT,capture_output=True).returncode==0
    report={'passed':not hits and not tracked,'source_files_scanned':scanned,'actual_secret_values_exposed':False,
        'files_containing_local_secrets':hits,'root_env_tracked':tracked,'remote_publish_performed':False}
    folder=PROJECT_ROOT/'.local/delivery';folder.mkdir(exist_ok=True)
    (folder/'source-security.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
