from __future__ import annotations

import pytest

from tracelens.analysis import of_export
from tracelens.loader import load


@pytest.fixture(scope="session")
def export():
    return load()


@pytest.fixture(scope="session")
def log(export):
    return export.log


@pytest.fixture(scope="session")
def analysis(export):
    return of_export(export)


@pytest.fixture(scope="session")
def grouping(analysis):
    return analysis.grouping


@pytest.fixture(scope="session")
def routes(analysis):
    return analysis.routes
