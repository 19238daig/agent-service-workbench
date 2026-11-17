"""Immutable, content-addressed inputs shared between Windows API and Linux worker."""
import hashlib
import os
import tempfile
from pathlib import Path
from smart_cs.settings import PROJECT_ROOT
from smart_cs.schemas.errors import ApplicationError

MAX_BYTES=25*1024*1024
EXTENSIONS={'.pdf','.html','.htm','.md','.txt'}

class JobFiles:
    def __init__(self,root=PROJECT_ROOT):self.root=Path(root).resolve();self.base=self.root/'.local/background-jobs'
    def input(self,sha,extension):
        import re
        if not re.fullmatch('[a-f0-9]{64}',sha) or extension not in EXTENSIONS:raise ApplicationError('INVALID_JOB_ARTIFACT','文件标识无效。',422)
        path=(self.base/'inputs'/sha/('source'+extension)).resolve()
        if not path.is_relative_to(self.base.resolve()) or not path.is_relative_to(self.root):raise ApplicationError('UNSAFE_JOB_PATH','文件路径无效。',422)
        return path
    def store(self,data,extension):
        if extension not in EXTENSIONS or not data or len(data)>MAX_BYTES:raise ApplicationError('JOB_FILE_INVALID','请使用不超过25MiB的PDF、HTML、Markdown或TXT文件。',422)
        sha=hashlib.sha256(data).hexdigest();path=self.input(sha,extension);path.parent.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(prefix='staged-',dir=path.parent)
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
            try:os.link(name,path)
            except FileExistsError:pass
        finally:Path(name).unlink(missing_ok=True)
        self.verify(sha,extension)
        return {'artifact_sha256':sha,'extension':extension,'bytes':len(data)}
    def verify(self,sha,extension):
        path=self.input(sha,extension)
        if not path.is_file() or path.stat().st_size>MAX_BYTES or hashlib.sha256(path.read_bytes()).hexdigest()!=sha:
            raise ApplicationError('JOB_ARTIFACT_CHANGED','任务文件缺失或校验不符。',409)
        return path
