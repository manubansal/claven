from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def no_background_scans():
    """Background scans and jobs started by endpoints do nothing in server tests.

    Run for real against a mock Gmail service they never finish (the mock's
    nextPageToken is always truthy) and grew the test process to 14 GB.
    Tests that care which scan was started patch claven.server.threading.
    """
    with patch("claven.tasks._run_task") as run_task:
        yield run_task
