import csv
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from swingtrader.config import Config          # noqa: E402
from swingtrader.data.synthetic import generate  # noqa: E402
from swingtrader.runtime import Dataset, load_universe  # noqa: E402

UNIVERSE = os.path.join(os.path.dirname(__file__), "..", "config", "universe_nifty100.csv")


@pytest.fixture(scope="session")
def universe():
    return load_universe(UNIVERSE)


@pytest.fixture(scope="session")
def cfg():
    c = Config()
    c.set("universe.file", UNIVERSE)
    return c


@pytest.fixture(scope="session")
def dataset(cfg):
    """A small synthetic dataset - enough bars to exercise every code path."""
    return Dataset.load(cfg, synthetic=True, seed=14, n_bars=1500)
