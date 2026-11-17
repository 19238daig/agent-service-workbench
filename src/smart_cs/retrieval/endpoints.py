from urllib.parse import urlsplit


def validate_local_endpoint(value):
    import os,sys
    parsed=urlsplit(value)
    hosts={'127.0.0.1','localhost'}
    if sys.platform=='linux' and (os.getenv('SMART_CS_WORKER_CONTAINER')=='1' or os.getenv('SMART_CS_APP_CONTAINER')=='1'):
        hosts.add('host.docker.internal')
    if parsed.scheme!='http' or parsed.hostname not in hosts or parsed.username is not None or parsed.password is not None:
        raise ValueError('Local model endpoints must use credential-free loopback HTTP')
    if parsed.port is None or not 1<=parsed.port<=65535 or parsed.path not in {'','/'} or parsed.query or parsed.fragment:
        raise ValueError('Local model endpoints require a port and no path/query/fragment')
    return value.rstrip('/')
