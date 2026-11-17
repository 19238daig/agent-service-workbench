"""Verify the installed package and root paths from an unrelated directory."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ProjectLayoutTests(unittest.TestCase):
    def test_installed_package_uses_project_configuration_outside_repo(self):
        root = Path(__file__).resolve().parents[1]
        probe = (
            "import json; from smart_cs.settings import PROJECT_ROOT, ENV_PATH, project_path; "
            "from smart_cs.api.main import ENV_PATH as api_env; "
            "print(json.dumps({'root':str(PROJECT_ROOT),'env':str(ENV_PATH),"
            "'api_env':str(api_env),'index':str(project_path('vector_store/faiss_index'))}))"
        )
        with tempfile.TemporaryDirectory() as unrelated:
            completed = subprocess.run(
                [sys.executable, "-c", probe], cwd=unrelated,
                check=True, capture_output=True, encoding="utf-8", timeout=30,
            )
        result = json.loads(completed.stdout)
        self.assertEqual(Path(result["root"]), root)
        self.assertEqual(Path(result["env"]), root / ".env")
        self.assertEqual(Path(result["api_env"]), root / ".env")
        self.assertEqual(Path(result["index"]), root / "vector_store/faiss_index")


if __name__ == "__main__":
    unittest.main()
