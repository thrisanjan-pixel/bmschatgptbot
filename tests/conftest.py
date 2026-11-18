"""Keep unittest temporary databases inside pytest's writable workspace."""
import tempfile

import pytest


@pytest.fixture(autouse=True)
def workspace_temporary_files(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
