"""
Tests for the /images REST endpoints (gallery listing, serving, deletion).

Creates a temporary outputs directory with synthetic image files + JSON
metadata sidecars, then verifies the endpoints handle listing, path
traversal prevention, and deletion correctly.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient
from alita.api.server import app, get_agent


class _StubEasyDiffusion:
    def __init__(self, outputs_dir):
        self.outputs_dir = outputs_dir


class _StubAgentImages:
    def __init__(self, outputs_dir):
        self.easy_diffusion = _StubEasyDiffusion(outputs_dir)


@pytest.fixture
def stub_agent_images(tmp_path, monkeypatch):
    outputs = tmp_path / "outputs"
    outputs.mkdir()

    # Create a couple of synthetic images with metadata sidecars.
    (outputs / "alita_test1.png").write_bytes(b"\x89PNG fake image 1")
    (outputs / "alita_test1.json").write_text(json.dumps({
        "filename": "alita_test1.png",
        "prompt": "a red circle",
        "seed": 42,
        "sampler": "euler_a",
        "steps": 25,
        "cfg_scale": 7.5,
        "width": 512,
        "height": 512,
    }))
    (outputs / "alita_test2.jpg").write_bytes(b"\xff\xd8 fake jpeg")
    (outputs / "alita_test2.json").write_text(json.dumps({
        "filename": "alita_test2.jpg",
        "prompt": "a blue square",
        "seed": 99,
    }))
    # A non-image file that should be skipped.
    (outputs / "readme.txt").write_text("not an image")

    agent = _StubAgentImages(outputs)
    monkeypatch.setattr("alita.api.server.get_agent", lambda: agent)
    return agent


def test_list_images_returns_images_with_metadata(stub_agent_images):
    client = TestClient(app)
    r = client.get("/images")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2  # readme.txt was skipped

    filenames = {img["filename"] for img in body["images"]}
    assert "alita_test1.png" in filenames
    assert "alita_test2.jpg" in filenames

    # Metadata was read from sidecar.
    test1 = next(img for img in body["images"] if img["filename"] == "alita_test1.png")
    assert test1["prompt"] == "a red circle"
    assert test1["seed"] == 42
    assert test1["sampler"] == "euler_a"
    assert test1["width"] == 512


def test_list_images_empty_dir(tmp_path, monkeypatch):
    outputs = tmp_path / "empty_outputs"
    outputs.mkdir()
    agent = _StubAgentImages(outputs)
    monkeypatch.setattr("alita.api.server.get_agent", lambda: agent)

    client = TestClient(app)
    r = client.get("/images")
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_serve_image_returns_file(stub_agent_images):
    client = TestClient(app)
    r = client.get("/images/alita_test1.png/file")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert b"fake image 1" in r.content


def test_serve_image_not_found(stub_agent_images):
    client = TestClient(app)
    assert client.get("/images/nonexistent.png/file").status_code == 404


def test_serve_image_path_traversal_blocked(stub_agent_images):
    """Must not be able to escape the outputs directory via ../."""
    client = TestClient(app)
    r = client.get("/images/..%2F..%2F..%2Fetc%2Fpasswd/file")
    assert r.status_code == 404


def test_delete_image_removes_file_and_sidecar(stub_agent_images):
    client = TestClient(app)
    r = client.delete("/images/alita_test1.png")
    assert r.status_code == 200

    # Verify both the image and metadata sidecar are gone.
    outputs = stub_agent_images.easy_diffusion.outputs_dir
    assert not (outputs / "alita_test1.png").exists()
    assert not (outputs / "alita_test1.json").exists()

    # The other image is still there.
    assert (outputs / "alita_test2.jpg").exists()


def test_delete_image_not_found(stub_agent_images):
    client = TestClient(app)
    assert client.delete("/api/images/nonexistent.png").status_code == 404