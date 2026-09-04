from __future__ import annotations

import pytest

from pyorm.connection import create_engine, set_engine
from pyorm.models import create_all, registry

import tests.sample_models  # noqa: F401  — register models


@pytest.fixture()
def engine():
    engine = create_engine("sqlite:///:memory:")
    set_engine(engine)
    registry.finalize()
    create_all(engine)
    yield engine
    engine.close()
    set_engine(None)
