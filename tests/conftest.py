import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))

from engram.core.config import load_settings  # noqa: E402
from engram.core.engine import EngramEngine  # noqa: E402
from engram.memory.embeddings import onnx_model_available  # noqa: E402

DEMO = ROOT / "examples" / "demo-notes"
REPO_MODELS = ROOT / "data" / "models"


@pytest.fixture
def settings(tmp_path):
    # The hash provider is deterministic and needs no download: ideal for unit/integration tests.
    return load_settings(data_dir=tmp_path / "data", embedding_provider="hash")


@pytest.fixture
def engine(settings):
    e = EngramEngine(settings)
    yield e
    e.close()


@pytest.fixture
def demo_dir(tmp_path):
    d = tmp_path / "notes"
    shutil.copytree(DEMO, d)
    return d


def neural_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
    except ImportError:
        return False
    return onnx_model_available(REPO_MODELS, "multilingual-e5-small")


@pytest.fixture
def neural_engine(tmp_path):
    if not neural_available():
        pytest.skip("ONNX model not downloaded (engram models download)")
    s = load_settings(data_dir=tmp_path / "ndata", embedding_provider="onnx", models_path=REPO_MODELS)
    e = EngramEngine(s)
    yield e
    e.close()
