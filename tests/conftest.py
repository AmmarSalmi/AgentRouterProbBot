from pathlib import Path
import shutil
from uuid import uuid4

import pytest


@pytest.fixture
def completed_response():
    return {
        "id": "resp-test",
        "object": "response",
        "status": "completed",
        "error": None,
        "output": [{
            "id": "msg-test",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "OK", "annotations": []}],
        }],
        "usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
    }


@pytest.fixture
def state_directory():
    # Match the existing tests' workspace storage. Python 3.13's private pytest
    # temp directories are not accessible to the managed Windows sandbox.
    directory = Path(__file__).parent / "__pycache__" / f"telegram-{uuid4().hex}"
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)
