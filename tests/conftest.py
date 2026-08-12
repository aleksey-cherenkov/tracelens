from __future__ import annotations

import pytest

from tracelens.accounting import account
from tracelens.detectors import build_context, run_all
from tracelens.health import compute
from tracelens.join import build_all
from tracelens.loader import load_dataset


@pytest.fixture(scope="session")
def dataset():
    return load_dataset()


@pytest.fixture(scope="session")
def traces(dataset):
    return build_all(dataset)


@pytest.fixture(scope="session")
def accounting(traces):
    return account(traces)


@pytest.fixture(scope="session")
def health(dataset, traces, accounting):
    return compute(dataset, traces, accounting)


@pytest.fixture(scope="session")
def context(dataset):
    return build_context(dataset)


@pytest.fixture(scope="session")
def findings(context):
    return {f.id: f for f in run_all(context)}
