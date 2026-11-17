"""Index canonical published DB chunks; ingestion is a separate command."""
from smart_cs.retrieval.indexes import DenseIndexBuilder
from smart_cs.schemas.errors import ApplicationError


async def build_index(database,source_directory=None,*,activate=False,adapter=None):
    if source_directory is not None:
        raise ApplicationError('SOURCE_IMPORT_SEPARATED','请先用 ingestion.cli 导入并发布文档，再建立数据库切片索引。',400)
    return await DenseIndexBuilder(database,adapter).build(activate=activate)
