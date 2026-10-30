import csv
import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import pytest
from backend.engine import Engine, Problem, FIELDS, parse_file, csv_bytes

MAPPING = {f: f for f in FIELDS}
HEADER = ','.join(FIELDS) + '\n'
def row(key='C-1', name='Avery Chen', email='avery@example.test', amount='12.50', day='2026-10-01', country='hk'):
    return f'{key},{name},{email},{amount},{day},{country}\n'
def stage(e, text, policy='insert_only'):
    s = e.upload('source.csv', (HEADER + text).encode())
    return e.validate(s['id'], MAPPING, policy)
@pytest.fixture
def engine():
    e = Engine()
    yield e
    e.close()

def test_validation_is_a_dry_run_and_errors_are_quarantined(engine):
    r = stage(engine, row() + row('C-2', email='invalid') + row('C-3', amount='-1'))
    assert r['counts'] == {'insert': 1, 'update': 0, 'skip': 0, 'reject': 2}
    assert engine.overview()['records'] == []
    committed = engine.commit(r['id'])
    assert committed['status'] == 'committed'
    assert len(engine.overview()['records']) == 1
    assert engine.overview()['records'][0]['country'] == 'HK'
    report = list(csv.reader(io.StringIO(engine.rejected_csv(r['id']).decode('utf-8-sig'))))
    assert len(report) == 3 and report[1][0] == '2' and 'email' in report[1][-1]

def test_replayed_validation_and_concurrent_commit_are_idempotent(engine):
    r = stage(engine, row())
    repeated = stage(engine, row())
    assert repeated['id'] == r['id']
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: engine.commit(r['id']), range(12)))
    assert all(r['status'] == 'committed' for r in results)
    assert len(engine.overview()['records']) == 1
    assert sum(e['action'] == 'committed' for e in engine.overview()['events']) == 1
    assert engine.revision() == 1
    again = stage(engine, row())
    assert again['counts']['skip'] == 1
    engine.commit(again['id'])
    assert engine.revision() == 1

def test_identical_duplicates_skip_and_conflicting_duplicates_reject(engine):
    r = stage(engine, row() + row() + row(name='Different customer'))
    assert r['counts']['insert'] == 1 and r['counts']['skip'] == 1 and r['counts']['reject'] == 1
    engine.commit(r['id'])
    assert engine.overview()['records'][0]['name'] == 'Avery Chen'

def test_stale_preview_cannot_overwrite_destination(engine):
    a, b = stage(engine, row()), stage(engine, row('C-2'))
    engine.commit(a['id'])
    with pytest.raises(Problem, match='destination changed') as exc:
        engine.commit(b['id'])
    assert exc.value.status == 409
    assert engine.run(b['id'])['status'] == 'validated'
    refreshed = engine.validate(b['source_id'], MAPPING, 'insert_only')
    assert refreshed['id'] != b['id']
    engine.commit(refreshed['id'])
    assert len(engine.overview()['records']) == 2

def test_upsert_is_explicit_and_keeps_historical_report(engine):
    first = stage(engine, row())
    engine.commit(first['id'])
    changed = stage(engine, row(amount='17.25'))
    assert changed['counts']['reject'] == 1
    upsert = engine.validate(changed['source_id'], MAPPING, 'upsert')
    assert upsert['counts']['update'] == 1
    engine.commit(upsert['id'])
    assert engine.overview()['records'][0]['amount_cents'] == 1725
    assert engine.run(first['id'])['results'][0]['record']['amount_cents'] == 1250

def test_atomic_rollback_covers_records_status_revision_and_audit(engine):
    run = stage(engine, row() + row('C-2'))
    engine.db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON records WHEN NEW.external_id='C-2' BEGIN SELECT RAISE(ABORT, 'synthetic storage failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        engine.commit(run['id'])
    assert engine.overview()['records'] == []
    assert engine.revision() == 0
    assert engine.run(run['id'])['status'] == 'validated'
    assert not any(e['action'] == 'committed' for e in engine.overview()['events'])

@pytest.mark.parametrize('amount', ['NaN', 'Infinity', '1.001', '-0.01', '1000000000.00'])
def test_invalid_money_never_reaches_destination(engine, amount):
    r = stage(engine, row(amount=amount))
    assert r['counts']['reject'] == 1

@pytest.mark.parametrize('content', [b'a,a\n1,2', b'a,b\n1', b'a,b\n1,2,3', b'a,b\n', b'\xff\xfe'])
def test_malformed_csv_is_rejected(content):
    with pytest.raises(Problem):
        parse_file('bad.csv', content)

def test_json_mapping_normalizes_without_guessing(engine):
    data = [{'id': 'C-3', 'full_name': ' Noor Ali ', 'contact': 'NOOR@EXAMPLE.TEST', 'total': 0, 'day': '2026-10-22', 'region': 'gb'}]
    s = engine.upload('source.json', json.dumps(data).encode())
    mapping = dict(zip(FIELDS, data[0].keys()))
    r = engine.validate(s['id'], mapping, 'insert_only')
    assert r['counts']['insert'] == 1
    engine.commit(r['id'])
    actual = engine.overview()['records'][0]
    assert actual['amount_cents'] == 0 and actual['name'] == 'Noor Ali' and actual['email'] == 'noor@example.test'

def test_rules_and_csv_export_handle_untrusted_values(engine):
    r = stage(engine, row(name='=HYPERLINK("untrusted")', day='2026-02-30'))
    assert r['counts']['reject'] == 1
    assert "'=HYPERLINK" in engine.rejected_csv(r['id']).decode('utf-8-sig')
    s = engine.upload('source.csv', (HEADER + row()).encode())
    with pytest.raises(Problem):
        engine.validate(s['id'], {**MAPPING, 'name': 'missing'}, 'insert_only')
    with pytest.raises(Problem):
        engine.validate(s['id'], {**MAPPING, 'name': 'email'}, 'insert_only')

def test_flat_json_and_size_limits():
    for value in ({'a': 1}, [{'a': {'nested': 'no'}}], []):
        with pytest.raises(Problem):
            parse_file('source.json', json.dumps(value).encode())
    with pytest.raises(Problem):
        parse_file('source.csv', b'a' * 2_000_001)
    with pytest.raises(Problem):
        parse_file('source.csv', ('a\n' + 'x\n' * 5001).encode())

def test_persistent_sqlite_reopens_with_source_provenance(tmp_path):
    path = str(tmp_path / 'pipeline.sqlite')
    first = Engine(path)
    r = stage(first, row())
    first.commit(r['id']);first.close()
    reopened = Engine(path)
    assert reopened.overview()['records'][0]['run_id'] == r['id']
    assert reopened.run(r['id'])['status'] == 'committed'
    reopened.close()
