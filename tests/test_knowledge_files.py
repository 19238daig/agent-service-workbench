from legacy_identity import override_identity
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import smart_cs.api.main as main


class KnowledgeFileAccessTests(unittest.TestCase):
    def test_only_published_files_inside_data_are_served_and_html_is_inert(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir()
            (root/'data/source.html').write_text('<script>alert(1)</script>',encoding='utf-8')
            (root/'data/source.pdf').write_bytes(b'%PDF-test')
            (root/'.env').write_text('private',encoding='utf-8')
            def doc(path,status='published',mime='text/html'):
                return SimpleNamespace(access_scope="public",owner_id=None,file_path=path,status=status,mime_type=mime,source='source.html')
            docs={'html':doc('data/source.html'),'pdf':doc('data/source.pdf',mime='application/pdf'),
                  'secret':doc('.env'),'traverse':doc('data/../.env'),'archived':doc('data/source.html','archived_demo')}
            class Session:
                async def __aenter__(self):return self
                async def __aexit__(self,*args):pass
                async def get(self,model,identifier):return docs.get(identifier)
            database=SimpleNamespace(sessions=Session)
            with override_identity(main.app), patch.object(main,'database',database),patch.object(main,'PROJECT_ROOT',root),patch.dict(os.environ,{'APP_MODE':'local_demo'}):
                client=TestClient(main.app)
                for identifier in ['secret','traverse','archived','missing']:
                    self.assertEqual(client.get(f'/api/knowledge/documents/{identifier}/file').status_code,404)
                html=client.get('/api/knowledge/documents/html/file')
                self.assertEqual(html.status_code,200)
                self.assertTrue(html.headers['content-type'].startswith('text/plain'))
                self.assertIn('attachment',html.headers['content-disposition'])
                self.assertEqual(html.headers['content-security-policy'],'sandbox')
                pdf=client.get('/api/knowledge/documents/pdf/file')
                self.assertEqual(pdf.headers['content-type'],'application/pdf')
                self.assertEqual(pdf.headers['x-content-type-options'],'nosniff')

if __name__=='__main__':unittest.main()
