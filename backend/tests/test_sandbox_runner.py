import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# Import runner policy code without requiring a live Docker daemon in unit tests.
fake_errors = SimpleNamespace(DockerException=Exception, ImageNotFound=Exception, NotFound=Exception)
fake_docker = MagicMock()
fake_docker.errors = fake_errors
sys.modules.setdefault("docker", fake_docker)
sys.modules.setdefault("docker.errors", fake_errors)

from app.sandbox_runner import clean_name, prepare_command, runtime_for


@pytest.mark.parametrize(
    ("project_type", "runtime"),
    [
        ("Node.js (Vite)", "node"),
        ("Python (FastAPI)", "python"),
        ("C# (.NET Blazor / Web)", "dotnet"),
        ("Java (Maven)", "maven"),
        ("Java/Kotlin (Gradle)", "gradle"),
        ("Rust", "rust"),
        ("Go", "go"),
        ("PHP (Laravel)", "php"),
        ("Static HTML / Web", "static"),
    ],
)
def test_runtime_selection(project_type, runtime):
    assert runtime_for(project_type) == runtime


def test_vite_is_bound_to_container_interface_and_selected_port():
    command = prepare_command("Node.js (Vite)", "npm run dev")
    assert "--host 0.0.0.0" in command
    assert "--port $PORT" in command


def test_repository_name_rejects_traversal():
    with pytest.raises(Exception):
        clean_name("../outside")
