import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import pytest
from backend.engine import Engine, Problem, FIELDS

def definition(maximum='1000'):
    return {'key_field':'sku','fields':[
        {'key':'sku','label':'SKU','type':'text','max_length':80,'normalization':'upper'},
        {'key':'name','label':'Product name','type':'text'},
        {'key':'price','label':'Price','type':'decimal','minimum':'0','maximum':maximum},
        {'key':'released','label':'Release date','type':'date'},
        {'key':'region','label':'Region','type':'country','normalization':'upper','allowed_values':['HK','SG']},
        {'key':'note','label':'Note','type':'text','required':False} ]}
MAPPING={'sku':'Code','name':'Title','price':'Cost','released':'Launch','region':'Market','note':''}
OPTIONS={'delimiter':';','decimal_separator':',','date_format':'dmy'}
CONTENT=b'Code;Title;Cost;Launch;Market\na-1;Widget;12,50;01/11/2026;hk\n'
@pytest.fixture
def engine():
    e=Engine();yield e;e.close()
def setup(e):
    schema=e.save_schema('Supplier inventory',definition())
    recipe=e.save_recipe('Supplier weekly CSV',schema['id'],1,MAPPING,'upsert',OPTIONS)
    source=e.upload('supplier.csv',CONTENT,';')
    return schema,recipe,source

def test_recipe_applies_pinned_schema_and_explicit_locales(engine):
    schema,recipe,source=setup(engine)
    run=engine.validate(source['id'],recipe_id=recipe['id'],recipe_version=1)
    assert run['counts']['insert']==1
    assert run['results'][0]['record']=={'sku':'A-1','name':'Widget','price':'12.50','released':'2026-11-01','region':'HK','note':None}
    engine.commit(run['id'])
    assert engine.catalog(schema['id'])['records'][0]['price']=='12.50'
    assert engine.overview()['records']==[]
    assert b'A-1' in engine.export_csv(schema['id'])
    assert engine.run(run['id'])['context']['recipe']['version']==1

def test_rule_and_recipe_revisions_never_rewrite_old_reports_or_replay_identity(engine):
    schema,recipe,source=setup(engine)
    old=engine.validate(source['id'],recipe_id=recipe['id'])
    engine.save_schema(schema['name'],definition('10'),schema['id'],1)
    assert engine.validate(source['id'],recipe_id=recipe['id'])['id']==old['id']
    changed=engine.save_recipe(recipe['name'],schema['id'],2,MAPPING,'upsert',OPTIONS,recipe['id'],1)
    strict=engine.validate(source['id'],recipe_id=changed['id'])
    assert strict['counts']['reject']==1
    assert strict['context']['schema']['version']==2
    assert engine.run(old['id'])['context']['schema']['version']==1
    engine.commit(old['id'])  # A deliberately pinned version remains a valid decision.
    assert engine.catalog(schema['id'])['records'][0]['price']=='12.50'
    assert engine.run(strict['id'])['stale']
    assert engine.validate(source['id'],recipe_id=recipe['id'],recipe_version=1)['context']['recipe']['version']==1
    with pytest.raises(Problem,match='overridden'):
        engine.validate(source['id'],recipe_id=recipe['id'],recipe_version=1,policy='insert_only')

def test_config_edits_require_latest_version_and_keep_dataset_shape(engine):
    schema,recipe,_=setup(engine)
    updated=engine.save_schema(schema['name'],definition('50'),schema['id'],1)
    with pytest.raises(Problem,match='changed'): engine.save_schema(schema['name'],definition('60'),schema['id'],1)
    bad=definition();bad['fields'][2]['type']='integer'
    with pytest.raises(Problem,match='stable'): engine.save_schema(schema['name'],bad,schema['id'],updated['version'])
    engine.save_recipe(recipe['name'],schema['id'],1,MAPPING,'insert_only',OPTIONS,recipe['id'],1)
    with pytest.raises(Problem,match='changed'): engine.save_recipe(recipe['name'],schema['id'],1,MAPPING,'upsert',OPTIONS,recipe['id'],1)
    assert engine.schema(schema['id'],1)['definition']['fields'][2]['maximum']=='1000'

def test_schema_namespaces_and_destination_revisions_are_independent(engine):
    a,recipe,source=setup(engine)
    b=engine.save_schema('Other inventory',definition())
    first=engine.validate(source['id'],recipe_id=recipe['id'])
    other=engine.validate(source['id'],MAPPING,'upsert',b['id'],1,options=OPTIONS)
    engine.commit(other['id'])
    assert not engine.run(first['id'])['stale']
    engine.commit(first['id'])
    assert engine.catalog(a['id'])['revision']==engine.catalog(b['id'])['revision']==1
    assert len(engine.catalog(a['id'])['records'])==len(engine.catalog(b['id'])['records'])==1
    assert engine.revision()==0

def test_custom_upsert_snapshot_staleness_and_duplicate_commit(engine):
    schema,recipe,source=setup(engine)
    first=engine.validate(source['id'],recipe_id=recipe['id']);engine.commit(first['id'])
    changed=engine.upload('changed.csv',CONTENT.replace(b'12,50',b'18,00'),';')
    run=engine.validate(changed['id'],recipe_id=recipe['id'])
    with ThreadPoolExecutor(max_workers=4) as pool: list(pool.map(lambda _:engine.commit(run['id']),range(8)))
    assert engine.catalog(schema['id'])['revision']==2
    assert engine.catalog(schema['id'])['records'][0]['price']=='18.00'
    assert engine.run(first['id'])['results'][0]['record']['price']=='12.50'
    replay=engine.validate(changed['id'],recipe_id=recipe['id']);assert replay['counts']['skip']==1
    engine.commit(replay['id']);assert engine.catalog(schema['id'])['revision']==2

def test_missing_headers_delimiter_mismatch_and_optional_mapping_are_explicit(engine):
    schema,recipe,_=setup(engine)
    wrong=engine.upload('wrong.csv',CONTENT.replace(b';',b'|'),'|')
    with pytest.raises(Problem,match='delimiter'): engine.validate(wrong['id'],recipe_id=recipe['id'])
    missing=engine.upload('missing.csv',CONTENT.replace(b'Title',b'Other'),';')
    with pytest.raises(Problem,match='missing'):engine.validate(missing['id'],recipe_id=recipe['id'])
    with pytest.raises(Problem,match='distinct'):engine.save_recipe('Bad mapping',schema['id'],1,{**MAPPING,'name':'Code'},'upsert',OPTIONS)

def test_custom_commit_and_configuration_changes_roll_back_on_failed_audit(engine):
    schema,recipe,source=setup(engine)
    run=engine.validate(source['id'],recipe_id=recipe['id'])
    engine.db.execute("CREATE TRIGGER fail_commit BEFORE INSERT ON events WHEN NEW.action='committed' BEGIN SELECT RAISE(ABORT,'forced commit failure'); END")
    with pytest.raises(sqlite3.IntegrityError):engine.commit(run['id'])
    assert engine.catalog(schema['id'])['records']==[]
    assert engine.catalog(schema['id'])['revision']==0
    assert engine.run(run['id'])['status']=='validated'
    engine.db.execute("CREATE TRIGGER fail_schema BEFORE INSERT ON events WHEN NEW.action='schema.version_created' BEGIN SELECT RAISE(ABORT,'forced config failure'); END")
    with pytest.raises(sqlite3.IntegrityError):engine.save_schema(schema['name'],definition('10'),schema['id'],1)
    assert engine.schema(schema['id'])['version']==1

def test_restart_preserves_recipes_schema_history_and_custom_provenance(tmp_path):
    path=str(tmp_path/'custom.sqlite');e=Engine(path)
    schema,recipe,source=setup(e);run=e.validate(source['id'],recipe_id=recipe['id']);e.commit(run['id']);e.close()
    e=Engine(path)
    try:
        assert e.recipe(recipe['id'])['schema_version']==1
        assert e.catalog(schema['id'])['records'][0]['run_id']==run['id']
        assert e.run(run['id'])['context']['options']==OPTIONS
    finally:e.close()

def test_v1_columns_are_migrated_without_revalidating_historical_rows(tmp_path):
    path=str(tmp_path/'legacy.sqlite');e=Engine(path);e.seed()
    rid=e.overview()['runs'][0]['id'];results=e.run(rid)['results']
    e.db.execute('ALTER TABLE runs DROP COLUMN context');e.db.execute('ALTER TABLE sources DROP COLUMN parse_options');e.db.execute('PRAGMA user_version=1');e.close()
    e=Engine(path)
    try:
        assert e.run(rid)['results']==results
        assert e.run(rid)['context']['schema']['id']=='ledger'
        assert len(e.overview()['records'])==4
    finally:e.close()

@pytest.mark.parametrize('mutate',[
 lambda d:d.update(key_field='missing'),
 lambda d:d['fields'].append(d['fields'][0]),
 lambda d:d['fields'][0].update(required=False),
 lambda d:d['fields'][2].update(minimum='NaN'),
 lambda d:d['fields'][2].update(minimum='100',maximum='1'),
 lambda d:d['fields'][1].update(key='run_id'),
])
def test_invalid_schema_contracts_are_rejected_before_mutation(engine,mutate):
    d=definition();mutate(d)
    with pytest.raises(Problem):engine.save_schema('Invalid contract',d)
    assert len(engine.schemas())==1

def test_cli_reuses_saved_recipe_and_persists_custom_destination(tmp_path):
    import subprocess,sys
    path=str(tmp_path/'cli.sqlite');e=Engine(path);schema,recipe,_=setup(e);e.close()
    source=tmp_path/'feed.csv';source.write_bytes(CONTENT)
    result=subprocess.run([sys.executable,'-m','backend.cli',str(source),'--database',path,'--recipe-id',recipe['id'],'--recipe-version','1','--commit'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['counts']['insert']==1
    e=Engine(path)
    try:assert e.catalog(schema['id'])['records'][0]['sku']=='A-1'
    finally:e.close()

def test_extreme_numeric_bound_is_rejected_without_decimal_context_overflow(engine):
    d=definition();d['fields'][2]['maximum']='1e999999999'
    with pytest.raises(Problem):engine.save_schema('Overflow contract',d)

def test_schema_limit_is_rejected_without_publishing_partial_version(engine):
    schema=engine.save_schema('Version bounded',definition())
    for version in range(1,20):engine.save_schema(schema['name'],definition(str(1000+version)),schema['id'],version)
    with pytest.raises(Problem,match='limit'):engine.save_schema(schema['name'],definition(),schema['id'],20)
    assert engine.schema(schema['id'])['version']==20


def test_error_report_escapes_untrusted_source_headers(engine):
    from backend.engine import csv_bytes
    assert b"'=HYPERLINK" in csv_bytes(['=HYPERLINK(1)'],[['safe']])
