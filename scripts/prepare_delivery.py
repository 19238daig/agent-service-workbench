"""Create an isolated deployment configuration without modifying the root .env."""
import secrets
from dotenv import dotenv_values,set_key
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from manage_accounts import restrict_file


def main():
    folder=PROJECT_ROOT/'.local/delivery';folder.mkdir(parents=True,exist_ok=True);path=folder/'environment.env'
    if path.exists():print('Existing private deployment environment retained.');return
    original=dotenv_values(ENV_PATH)
    selected={k:v for k,v in original.items() if v and k.startswith(('OPENAI_','MODEL_','INTENT_','KNOWLEDGE_','TICKET_','COMPLIANCE_'))}
    selected.update(DELIVERY_DB_PASSWORD=secrets.token_urlsafe(32),DELIVERY_JOB_PASSWORD=secrets.token_urlsafe(32),
        DELIVERY_DB_PORT='65432',DELIVERY_APP_PORT='18000',DELIVERY_RAG_ENABLED='false',DELIVERY_RUN_EXECUTOR_ENABLED='true',
        DELIVERY_ENV_FILE='.local/delivery/environment.env')
    path.touch(exist_ok=False);restrict_file(path)
    for key,value in selected.items():set_key(path,key,value)
    print('Private deployment configuration saved under .local/delivery; root .env unchanged.')


if __name__=='__main__':main()
