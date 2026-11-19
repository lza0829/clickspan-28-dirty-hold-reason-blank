import os
import shutil
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
TMP = Path("/tmp/clickspan-pytest")

# 必须在 import app.* 之前指定独立的本地 SQLite 与夹具目录
os.environ["DATA_DIR"] = str(TMP / "data")
os.environ["FIXTURES_DIR"] = str(TMP / "fixtures")
sys.path.insert(0, str(BACKEND))


@pytest.fixture(autouse=True)
def fresh_db():
    TMP.mkdir(parents=True, exist_ok=True)
    (TMP / "fixtures").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "fixtures" / "seed_hits.json", TMP / "fixtures" / "seed_hits.json")
    shutil.copy(
        REPO / "fixtures" / "ingest_batch.json", TMP / "fixtures" / "ingest_batch.json"
    )
    from app.db import DB_PATH
    from app import seed

    if DB_PATH.exists():
        DB_PATH.unlink()
    seed.init_db()
    yield
    if DB_PATH.exists():
        DB_PATH.unlink()
