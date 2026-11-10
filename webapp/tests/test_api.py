import io
import json
import time
from datetime import date, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient
from osgeo import gdal, osr

from webapp.backend.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / 'data')) as test_client:
        yield test_client


def upload(client, name, content):
    response = client.post('/api/uploads', files={'file': (name, content)})
    assert response.status_code == 201, response.text
    return response.json()['id']


def fixture_inputs(client, tmp_path):
    # Deliberately synthetic planar terrain, used only for numerical API tests.
    path = tmp_path / 'plane.tif'
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32645)
    ds = gdal.GetDriverByName('GTiff').Create(str(path), 64, 64, 1, gdal.GDT_Float32)
    ds.SetProjection(srs.ExportToWkt())
    ds.SetGeoTransform((460000, 30, 0, 5541000, 0, -30))
    ds.GetRasterBand(1).WriteArray(np.tile(2000 - np.arange(64)[:, None] * 30 * np.tan(np.pi / 6), (1, 64)))
    ds.GetRasterBand(1).SetNoDataValue(-9999)
    ds = None
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    geo = osr.SpatialReference()
    geo.ImportFromEPSG(4326)
    geo.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(srs, geo)
    west, south, _ = tr.TransformPoint(460600, 5539700)
    east, north, _ = tr.TransformPoint(461000, 5540100)
    return {'bbox': [west, south, east, north], 'dem_id': upload(client, 'plane.tif', path.read_bytes()),
            'buffer': 60, 'resolution': 30}


def wait_result(client, job_id):
    for _ in range(750):
        result = client.get('/api/jobs/' + job_id).json()
        if result['state'] in ('completed', 'failed', 'cancelled'):
            assert result['state'] == 'completed', result
            return result
        time.sleep(.08)
    pytest.fail('Background calculation timed out')


def test_reject_bad_inputs_and_paths(client):
    assert client.post('/api/jobs', json={'hazard': 'landslide', 'bbox': [3, 4, 2, 5]}).status_code == 422
    assert client.post('/api/jobs', json={'hazard': 'avalanche', 'bbox': [86, 50, 86.01, 50.01], 'parameters': {'mu': -1}}).status_code == 422
    assert client.post('/api/jobs', json={'hazard': 'landslide', 'bbox': [86, 50, 86.01, 50.01], 'dem_id': '../../secret'}).status_code == 422
    assert client.post('/api/uploads', files={'file': ('secret.vrt', b'<VRTDataset/>')}).status_code == 422
    assert client.get('/api/jobs/not-a-uuid').status_code == 404
    assert client.get('/api/catalog').status_code == 200


@pytest.mark.parametrize('hazard', ['landslide', 'avalanche', 'wildfire'])
def test_real_workflow_through_api(client, tmp_path, hazard):
    payload = dict(fixture_inputs(client, tmp_path), hazard=hazard)
    if hazard == 'avalanche':
        payload['parameters'] = {'duration': 2}
    if hazard == 'wildfire':
        bbox = payload['bbox']
        ring = [[bbox[0]-.02, bbox[1]-.02], [bbox[2]+.02, bbox[1]-.02],
                [bbox[2]+.02, bbox[3]+.02], [bbox[0]-.02, bbox[3]+.02], [bbox[0]-.02, bbox[1]-.02]]
        fuel = {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'properties': {'fuel': 'C2'},
                'geometry': {'type': 'Polygon', 'coordinates': [ring]}}]}
        payload.update(satellite=False, fuel_id=upload(client, 'fuels.geojson', json.dumps(fuel).encode()),
                       scenario_date='2026-07-15', ignitions=[[(bbox[0]+bbox[2])/2, (bbox[1]+bbox[3])/2]],
                       parameters={'duration_min': 5})
        rows = ['date,temp_c,rh_pct,wind_kmh,wind_from_deg,rain_mm']
        for i in range(40):
            rows.append(f'{date(2026,6,6)+timedelta(days=i)},25,35,12,270,0')
        payload['weather_id'] = upload(client, 'weather.csv', '\n'.join(rows).encode())
    response = client.post('/api/jobs', json=payload)
    assert response.status_code == 202, response.text
    result = wait_result(client, response.json()['id'])
    assert result['result']['layers']
    if hazard == 'landslide':
        assert result['result']['summary']['min_fs'] == pytest.approx(1.0434, abs=.02)
    layer = result['result']['layers'][0]
    assert client.get(layer['url']).content.startswith(b'\x89PNG')
    assert client.get(result['result']['report_url']).status_code == 200
    archive = client.get(result['result']['archive_url'])
    assert archive.status_code == 200 and archive.content.startswith(b'PK')
    assert client.get('/api/jobs').json()[0]['id'] == result['id']


def test_shutdown_recovery(tmp_path):
    from webapp.backend.storage import write_json
    from uuid import uuid4
    job_id = str(uuid4())
    folder = tmp_path / 'jobs' / job_id
    folder.mkdir(parents=True)
    write_json(folder/'job.json', {'id': job_id, 'state': 'running', 'created_at': '2026-01-01', 'progress': 20})
    with TestClient(create_app(tmp_path)) as client:
        result = client.get('/api/jobs/' + job_id).json()
        assert result['state'] == 'interrupted'


def test_cancels_running_and_queued_jobs(client, tmp_path):
    payload = dict(fixture_inputs(client, tmp_path), hazard='avalanche', parameters={'duration': 3600})
    first = client.post('/api/jobs', json=payload).json()['id']
    second = client.post('/api/jobs', json=payload).json()['id']
    assert client.post('/api/jobs/'+second+'/cancel').status_code == 202
    assert client.post('/api/jobs/'+first+'/cancel').status_code == 202
    for _ in range(100):
        states = [client.get('/api/jobs/'+key).json()['state'] for key in (first, second)]
        if states == ['cancelled', 'cancelled']:
            break
        time.sleep(.05)
    assert states == ['cancelled', 'cancelled']
    assert client.get('/api/jobs/'+first+'/archive').status_code == 409


def test_atomic_write_survives_transient_windows_lock(tmp_path, monkeypatch):
    from pathlib import Path
    from webapp.backend.storage import write_json, read_json
    real_replace = Path.replace
    calls = []
    def intermittent(self, target):
        calls.append(target)
        if len(calls) == 1:
            raise PermissionError('temporary reader lock')
        return real_replace(self, target)
    monkeypatch.setattr(Path, 'replace', intermittent)
    path = tmp_path/'state.json'
    write_json(path, {'value': 'complete'})
    assert read_json(path) == {'value': 'complete'}
    assert len(calls) == 2


def test_flat_terrain_is_not_reported_safe(client, tmp_path):
    payload = fixture_inputs(client, tmp_path)
    path = tmp_path/'plane.tif'
    ds = gdal.Open(str(path), gdal.GA_Update)
    ds.GetRasterBand(1).Fill(1000)
    ds = None
    payload.update(hazard='avalanche', dem_id=upload(client, 'flat.tif', path.read_bytes()))
    response = client.post('/api/jobs', json=payload)
    assert response.status_code == 202
    result = wait_result(client, response.json()['id'])
    assert result['result']['status'] == 'terrain_not_suitable'
    assert not result['result']['screening']['acceptable']
    assert result['request']['hazard'] == 'avalanche'
