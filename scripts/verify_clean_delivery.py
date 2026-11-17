"""Repeat the documented bootstrap on a second set of genuinely empty volumes."""
import json,secrets
from dotenv import dotenv_values,set_key
import delivery_drill as drill
from manage_accounts import restrict_file


def main():
    root=drill.OUT;folder=root/'clean-install';folder.mkdir(exist_ok=True);env=folder/'environment.env'
    if env.exists():raise ValueError('Clean-install drill already initialized; retain evidence instead of overwriting')
    config=dotenv_values(drill.ENV);config.pop('DELIVERY_DATABASE',None)
    config.update(DELIVERY_DB_PASSWORD=secrets.token_urlsafe(32),DELIVERY_JOB_PASSWORD=secrets.token_urlsafe(32),
        DELIVERY_APP_PORT='18001',DELIVERY_DB_PORT='65433',DELIVERY_RAG_ENABLED='false',DELIVERY_RUN_EXECUTOR_ENABLED='false',
        DELIVERY_ENV_FILE='.local/delivery/clean-install/environment.env')
    env.touch();restrict_file(env)
    for key,value in config.items():set_key(env,key,value)
    drill.OUT=folder;drill.ENV=env;drill.PROJECT_NAME='smart-cs-delivery-clean'
    drill.COMPOSE=['docker','compose','--project-directory',str(drill.PROJECT_ROOT),'--env-file',str(env),'-f',str(drill.PROJECT_ROOT/'deploy/compose.delivery.yml'),'-p',drill.PROJECT_NAME]
    try:
        drill.run([*drill.COMPOSE,'up','-d','--wait','postgres','redis','job-redis'],'services')
        drill.fresh()
        report=json.loads((folder/'fresh.json').read_text(encoding='utf-8'));report.update(no_repair_steps_required=True,second_empty_volume_set=True)
        (root/'clean-install.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps({'clean_install_passed':True,'manual_repair_needed':False}))
    finally:drill.run([*drill.COMPOSE,'down'],'clean-drill-stop')


if __name__=='__main__':main()
