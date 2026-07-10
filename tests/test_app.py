import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["HOME_CAM_CAMERAS_JSON"] = '[{"id":"ch1","name":"門口","url":"demo://ch1"},{"id":"ch2","name":"車庫","url":"demo://ch2"},{"id":"ch3","name":"曬衣區","url":"demo://ch3"},{"id":"ch4","name":"路邊","url":"demo://ch4"}]'

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_health():
    r = client.get('/api/health')
    assert r.status_code == 200
    assert r.json()['ok'] is True
    assert r.json()['camera_count'] == 4


def test_cameras_hides_urls():
    r = client.get('/api/cameras')
    assert r.status_code == 200
    cams = r.json()['cameras']
    assert cams[0]['id'] == 'ch1'
    assert 'url' not in cams[0]


def test_single_snapshot_jpeg():
    r = client.get('/api/camera/ch1/snapshot')
    assert r.status_code == 200
    assert r.headers['content-type'].startswith('image/jpeg')
    assert r.content.startswith(b'\xff\xd8')


def test_grid_snapshot_jpeg():
    r = client.get('/api/grid/snapshot')
    assert r.status_code == 200
    assert r.headers['content-type'].startswith('image/jpeg')
    assert r.content.startswith(b'\xff\xd8')
    assert len(r.content) > 10000


def test_missing_camera():
    r = client.get('/api/camera/nope/snapshot')
    assert r.status_code == 404
