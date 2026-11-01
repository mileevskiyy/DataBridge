import pytest
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.engine import Engine, FIELDS

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('SEED_DEMO', 'false')
    e = Engine()
    with TestClient(create_app(e, public_demo=True)) as c:
        yield c
    e.close()

def enter(c):
    assert c.post('/api/demo-login').status_code == 200
    return {'X-CSRF-Token': c.get('/api/session').json()['csrf']}

def test_demo_entry_origin_session_csrf_and_end_to_end_import(client):
    assert client.get('/api/workspace').status_code == 401
    assert client.post('/api/demo-login', headers={'Origin': 'https://untrusted.example'}).status_code == 403
    headers = enter(client)
    payload = b'external_id,name,email,amount,date,country\nC-1,Avery,avery@example.test,1.50,2026-10-01,HK\n'
    assert client.post('/api/sources', files={'file': ('source.csv', payload)}).status_code == 403
    uploaded = client.post('/api/sources', files={'file': ('source.csv', payload)}, headers=headers)
    assert uploaded.status_code == 201
    validated = client.post('/api/runs/validate', json={'source_id': uploaded.json()['id'], 'mapping': {f:f for f in FIELDS}, 'policy':'insert_only'}, headers=headers)
    assert validated.status_code == 201
    rid = validated.json()['id']
    assert client.post(f'/api/runs/{rid}/commit', headers=headers).status_code == 200
    assert len(client.get('/api/workspace').json()['records']) == 1
    assert 'C-1' in client.get('/api/records.csv').text
    assert client.post('/api/logout', headers=headers).status_code == 200
    assert client.get('/api/records.csv').status_code == 401

def test_session_rotation_and_incorrect_csrf(client):
    old_headers = enter(client)
    old_cookie = client.cookies.get('db_session')
    new_headers = enter(client)
    assert old_headers != new_headers
    assert client.post('/api/logout', headers=old_headers).status_code == 403
    assert client.get('/api/workspace', cookies={'db_session':old_cookie}).status_code == 401

def test_demo_cannot_attach_persistent_database(monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', 'data/real.sqlite')
    with pytest.raises(RuntimeError, match='in-memory'):
        create_app(public_demo=True)

def test_private_mode_has_no_public_entry(monkeypatch):
    monkeypatch.setenv('OPERATOR_PASSWORD', 'Synthetic-private-password')
    monkeypatch.setenv('SEED_DEMO', 'false')
    e = Engine()
    with TestClient(create_app(e, public_demo=False)) as c:
        assert c.post('/api/demo-login').status_code == 404
        assert c.post('/api/login', json={'password':'wrong'}).status_code == 401
        assert c.post('/api/login', json={'password':'Synthetic-private-password'}).status_code == 200
    e.close()

def test_untrusted_paths_and_oversize_upload(client):
    headers = enter(client)
    assert client.get('/api/runs/missing').status_code == 404
    assert client.post('/api/sources',files={'file':('huge.csv',b'a'*2_000_001)},headers=headers).status_code == 413

def test_versioned_schema_recipe_and_catalog_http_contract(client):
    from test_schemas import definition, MAPPING, OPTIONS, CONTENT
    assert client.get('/api/schemas').status_code==401
    headers=enter(client)
    payload={'name':'HTTP inventory','definition':definition()}
    assert client.post('/api/schemas',json=payload).status_code==403
    response=client.post('/api/schemas',json=payload,headers=headers);assert response.status_code==201
    schema=response.json()
    recipe_payload={'name':'HTTP supplier recipe','schema_id':schema['id'],'schema_version':1,'mapping':MAPPING,'policy':'upsert','options':OPTIONS}
    recipe=client.post('/api/recipes',json=recipe_payload,headers=headers).json()
    source=client.post('/api/sources?delimiter=%3B',files={'file':('feed.csv',CONTENT)},headers=headers).json()
    result=client.post('/api/runs/validate',json={'source_id':source['id'],'recipe_id':recipe['id'],'recipe_version':1},headers=headers);assert result.status_code==201
    run=result.json();assert run['context']['recipe']['id']==recipe['id']
    assert client.post('/api/runs/'+run['id']+'/commit',headers=headers).status_code==200
    catalog=client.get('/api/catalog',params={'schema_id':schema['id']}).json();assert catalog['records'][0]['sku']=='A-1'
    assert '12.50' in client.get('/api/records.csv',params={'schema_id':schema['id']}).text
    revised=client.put('/api/schemas/'+schema['id'],json={**payload,'definition':definition('10'),'expected_version':1},headers=headers);assert revised.status_code==200
    assert client.put('/api/schemas/'+schema['id'],json={**payload,'expected_version':1},headers=headers).status_code==409
    assert client.get('/api/schemas/'+schema['id'],params={'version':1}).json()['definition']['fields'][2]['maximum']=='1000'
    assert client.get('/api/runs/'+run['id']).json()['context']['schema']['version']==1
    assert client.get('/api/catalog',params={'schema_id':'missing'}).status_code==404
