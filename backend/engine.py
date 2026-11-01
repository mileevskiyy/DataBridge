"""Bounded file ingestion, deterministic validation and atomic promotion."""
import csv
import hashlib
import io
import json
import re
import sqlite3
import threading
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from pydantic import ValidationError
from .schema_rules import parse_definition, parse_options, normalize_custom, normalized_date, normalized_number, DEFAULT_OPTIONS

FIELDS = ('external_id', 'name', 'email', 'amount', 'date', 'country')
MAX_BYTES, MAX_ROWS = 2_000_000, 5000

def now():
    return datetime.now(timezone.utc).isoformat()

def identifier():
    return str(uuid4())

class Problem(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status
        super().__init__(message)

def parse_file(name, content, delimiter=','):
    if delimiter not in (',', ';', '\t', '|'):
        raise Problem('Unsupported CSV delimiter.')
    if len(content) > MAX_BYTES:
        raise Problem('Files must be smaller than 2 MB.', 413)
    try:
        text = content.decode('utf-8-sig')
        if name.lower().endswith('.csv'):
            reader = csv.DictReader(io.StringIO(text), strict=True, delimiter=delimiter)
            headers = reader.fieldnames or []
            if not headers or len(headers) != len(set(headers)) or any(not h.strip() for h in headers):
                raise Problem('CSV headers must be nonempty and unique.')
            rows = []
            for row in reader:
                if None in row or any(v is None for v in row.values()):
                    raise Problem('A CSV row has a different number of columns from the header.')
                rows.append(row)
                if len(rows) > MAX_ROWS:
                    raise Problem('At most 5,000 rows are allowed.', 413)
        elif name.lower().endswith('.json'):
            rows = json.loads(text)
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise Problem('JSON must contain an array of flat objects.')
            if any(not isinstance(v, (str, int, float, type(None))) or isinstance(v, bool) for r in rows for v in r.values()):
                raise Problem('JSON values must be text, numbers or null; nested data is not supported.')
            headers = list(dict.fromkeys(k for r in rows for k in r))
        else:
            raise Problem('Choose a .csv or .json file.')
    except (UnicodeDecodeError, json.JSONDecodeError, csv.Error, RecursionError):
        raise Problem('The file could not be parsed as UTF-8 CSV or JSON.') from None
    if not rows:
        raise Problem('The file contains no records.')
    if len(rows) > MAX_ROWS or len(headers) > 50 or any(len(h) > 100 for h in headers):
        raise Problem('Use at most 5,000 rows and 50 columns with short headers.', 413)
    if any(len(str(v)) > 2000 for r in rows for v in r.values()):
        raise Problem('Individual values must be at most 2,000 characters.', 413)
    return headers, rows

def normalize(row, mapping):
    values = {field: str(row.get(mapping[field], '') if row.get(mapping[field]) is not None else '').strip() for field in FIELDS}
    errors = []
    for field in FIELDS:
        if not values[field]:
            errors.append(f'{field}: required')
    if values['external_id'] and not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', values['external_id']):
        errors.append('external_id: use 1–80 letters, digits, dots, underscores or hyphens')
    if len(values['name']) > 120:
        errors.append('name: maximum 120 characters')
    values['email'] = values['email'].lower()
    if values['email'] and (len(values['email']) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', values['email'])):
        errors.append('email: invalid address')
    try:
        amount = Decimal(values['amount'])
        if not amount.is_finite() or amount < 0 or amount > Decimal('999999999.99') or amount.as_tuple().exponent < -2:
            raise InvalidOperation
        values['amount_cents'] = int(amount * 100)
    except (InvalidOperation, ValueError, OverflowError):
        errors.append('amount: use a nonnegative decimal with at most two decimal places')
    try:
        parsed = date.fromisoformat(values['date'])
        if parsed.isoformat() != values['date']:
            raise ValueError
    except ValueError:
        errors.append('date: use a valid YYYY-MM-DD date')
    values['country'] = values['country'].upper()
    if values['country'] and not re.fullmatch(r'[A-Z]{2}', values['country']):
        errors.append('country: use a two-letter code')
    values.pop('amount')
    return values, errors

def csv_bytes(headers, rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    # Prevent exported values from becoming spreadsheet formula expressions.
    safe = lambda x: "'" + str(x) if str(x).lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else str(x)
    writer.writerow([safe(h) for h in headers])
    writer.writerows([[safe(v) for v in row] for row in rows])
    return stream.getvalue().encode('utf-8-sig')

class Engine:
    def __init__(self, path=':memory:'):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
        INSERT OR IGNORE INTO metadata VALUES('revision',0);
        CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,name TEXT NOT NULL,digest TEXT NOT NULL,headers TEXT NOT NULL,rows TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),signature TEXT UNIQUE NOT NULL,mapping TEXT NOT NULL,policy TEXT NOT NULL,base_revision INTEGER NOT NULL,status TEXT NOT NULL,results TEXT NOT NULL,created_at TEXT NOT NULL,committed_at TEXT);
        CREATE TABLE IF NOT EXISTS records(external_id TEXT PRIMARY KEY,name TEXT NOT NULL,email TEXT NOT NULL,amount_cents INTEGER NOT NULL CHECK(amount_cents>=0),date TEXT NOT NULL,country TEXT NOT NULL,run_id TEXT NOT NULL REFERENCES runs(id),updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,run_id TEXT REFERENCES runs(id),action TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS schema_heads(id TEXT PRIMARY KEY,name TEXT NOT NULL,version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS schema_versions(schema_id TEXT NOT NULL REFERENCES schema_heads(id),version INTEGER NOT NULL,definition TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(schema_id,version));
        CREATE TABLE IF NOT EXISTS recipe_heads(id TEXT PRIMARY KEY,name TEXT NOT NULL,version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS recipe_versions(recipe_id TEXT NOT NULL REFERENCES recipe_heads(id),version INTEGER NOT NULL,snapshot TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(recipe_id,version));
        CREATE TABLE IF NOT EXISTS dataset_revisions(schema_id TEXT PRIMARY KEY REFERENCES schema_heads(id),revision INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS dataset_records(schema_id TEXT NOT NULL REFERENCES schema_heads(id),record_key TEXT NOT NULL,payload TEXT NOT NULL,run_id TEXT NOT NULL REFERENCES runs(id),updated_at TEXT NOT NULL,PRIMARY KEY(schema_id,record_key));

        ''')

        # Additive migration keeps all original sources, records and run decisions intact.
        for table, column, declaration in [('sources','parse_options',"TEXT NOT NULL DEFAULT '{}'"),('runs','context',"TEXT NOT NULL DEFAULT '{}'")]:
            if column not in [r['name'] for r in self.db.execute(f'PRAGMA table_info({table})')]:
                self.db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')
        self.db.execute('PRAGMA user_version=2')

    def close(self):
        self.db.close()

    def revision(self, schema_id='ledger'):
        if schema_id == 'ledger':
            return self.db.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0]
        row = self.db.execute('SELECT revision FROM dataset_revisions WHERE schema_id=?', (schema_id,)).fetchone()
        if not row: raise Problem('Schema not found.', 404)
        return row[0]

    def source(self, source_id):
        row = self.db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
        if not row:
            raise Problem('Source not found.', 404)
        return {**dict(row), 'headers': json.loads(row['headers']), 'rows': json.loads(row['rows']), 'parse_options': json.loads(row['parse_options'])}

    def upload(self, name, content, delimiter=','):
        headers, rows = parse_file(name, content, delimiter)
        with self.lock:
            if self.db.execute('SELECT count(*) FROM sources').fetchone()[0] >= 100:
                raise Problem('This workspace has reached its 100-upload limit. Restart the disposable demo to reset it.', 429)
            if self.db.execute('SELECT coalesce(sum(length(rows)),0) FROM sources').fetchone()[0] + len(json.dumps(rows)) > 10_000_000:
                raise Problem('Workspace source-storage limit reached. Restart the disposable demo to reset it.', 429)
            sid = identifier()
            self.db.execute('INSERT INTO sources(id,name,digest,headers,rows,created_at,parse_options) VALUES(?,?,?,?,?,?,?)', (sid, name[:160], hashlib.sha256(content).hexdigest(), json.dumps(headers), json.dumps(rows), now(), json.dumps({'delimiter':delimiter,'format':'csv' if name.lower().endswith('.csv') else 'json'})))
            return {'id': sid, 'name': name[:160], 'headers': headers, 'row_count': len(rows), 'preview': rows[:5], 'delimiter':delimiter}

    def validate(self, source_id, mapping=None, policy=None, schema_id=None, schema_version=None, recipe_id=None, recipe_version=None, options=None):
        with self.lock:
            source = self.source(source_id)
            recipe = self.recipe(recipe_id, recipe_version) if recipe_id else None
            if recipe:
                for supplied, saved in [(mapping,recipe['mapping']), (policy,recipe['policy']), (schema_id,recipe['schema_id']), (schema_version,recipe['schema_version']), (options,recipe['options'])]:
                    if supplied is not None and supplied != saved: raise Problem('A saved recipe cannot be overridden. Publish a new recipe version or use manual configuration.', 409)
                mapping,policy,schema_id,schema_version,options = recipe['mapping'],recipe['policy'],recipe['schema_id'],recipe['schema_version'],recipe['options']
            schema = self.schema(schema_id or 'ledger', schema_version)
            options = self.options(options)
            mapping = self.check_mapping(mapping or {}, schema, source['headers'])
            policy = policy or 'insert_only'
            if policy not in ('insert_only', 'upsert'): raise Problem('Unknown conflict policy.')
            if source['parse_options'].get('format', 'csv' if source['name'].lower().endswith('.csv') else 'json') == 'csv' and source['parse_options'].get('delimiter', ',') != options['delimiter']:
                raise Problem('CSV delimiter differs from the saved configuration. Upload the file again using that delimiter.')
            revision = self.revision(schema['id'])
            schema_snapshot = {k:v for k,v in schema.items() if k!='versions'}
            context = {'schema':schema_snapshot,'options':options,'recipe':recipe}
            signature = hashlib.sha256(json.dumps([source['digest'], mapping, policy, revision, context], sort_keys=True).encode()).hexdigest()
            existing = self.db.execute('SELECT id FROM runs WHERE signature=?', (signature,)).fetchone()
            if existing:
                return self.run(existing['id'])
            if self.db.execute('SELECT count(*) FROM runs').fetchone()[0] >= 200:
                raise Problem('This workspace has reached its validation-run limit.', 429)
            seen, results = {}, []
            for number, raw in enumerate(source['rows'], 1):
                if schema['id'] == 'ledger':
                    adjusted = dict(raw)
                    conversion_errors = []
                    for field, convert in ([('amount', lambda v: str(normalized_number(v,options)))] if options['decimal_separator'] != '.' else []) + ([('date', lambda v: normalized_date(v,options))] if options['date_format'] != 'iso' else []):
                        original = raw.get(mapping[field], '')
                        value = str('' if original is None else original).strip()
                        try: adjusted[mapping[field]] = convert(value)
                        except (ValueError, InvalidOperation): conversion_errors.append(f'{field}: does not match the selected parsing format')
                    record, errors = normalize(adjusted, mapping)
                    errors.extend(conversion_errors)
                else:
                    record, errors = normalize_custom(raw, mapping, schema['definition'], options)
                action = 'insert'
                if not errors:
                    key = record[schema['definition']['key_field']]
                    if key in seen:
                        if seen[key] == record:
                            action = 'skip'
                        else:
                            errors.append(f'{schema["definition"]["key_field"]}: conflicting duplicate within this file')
                    else:
                        seen[key] = record
                        current = self.current_record(schema['id'], key)
                        if current:
                            if current == record:
                                action = 'skip'
                            elif policy == 'upsert':
                                action = 'update'
                            else:
                                errors.append(f'{schema["definition"]["key_field"]}: already exists with different values; use upsert to update')
                results.append({'row': number, 'raw': raw, 'record': record, 'errors': errors, 'action': 'reject' if errors else action})
            if self.db.execute('SELECT coalesce(sum(length(results)),0) FROM runs').fetchone()[0] + len(json.dumps(results)) > 20_000_000:
                raise Problem('Workspace report-storage limit reached.', 429)
            rid = identifier()
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.db.execute('INSERT INTO runs(id,source_id,signature,mapping,policy,base_revision,status,results,created_at,committed_at,context) VALUES(?,?,?,?,?,?,?,?,?,NULL,?)', (rid, source_id, signature, json.dumps(mapping), policy, revision, 'validated', json.dumps(results), now(),json.dumps(context)))
                self.event(rid, 'validated', f'{len(results)} source rows evaluated')
                self.db.execute('COMMIT')
            except Exception:
                self.db.execute('ROLLBACK')
                raise
            return self.run(rid)

    def event(self, rid, action, detail):
        self.db.execute('INSERT INTO events VALUES(?,?,?,?,?)', (identifier(), rid, action, detail, now()))

    def run(self, rid):
        with self.lock:
            row = self.db.execute('SELECT runs.*,sources.name AS source_name FROM runs JOIN sources ON sources.id=runs.source_id WHERE runs.id=?', (rid,)).fetchone()
            if not row:
                raise Problem('Run not found.', 404)
            result = dict(row)
            result['mapping'], result['results'] = json.loads(row['mapping']), json.loads(row['results'])
            result['counts'] = {action: sum(r['action'] == action for r in result['results']) for action in ('insert', 'update', 'skip', 'reject')}
            result['total'] = len(result['results'])
            context = json.loads(row['context'])
            result['context'] = context or {'schema':self.schema('ledger'), 'options':dict(DEFAULT_OPTIONS), 'recipe':None}
            result['stale'] = result['status'] == 'validated' and result['base_revision'] != self.revision(result['context']['schema']['id'])
            return result

    def commit(self, rid):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                run = self.run(rid)
                if run['status'] == 'committed':
                    self.db.execute('COMMIT')
                    return run
                if run['stale']:
                    raise Problem('The destination changed after validation. Validate the source again before committing.', 409)
                schema_id = run['context']['schema']['id']
                for row in run['results']:
                    if row['action'] not in ('insert', 'update'):
                        continue
                    r = row['record']
                    if schema_id == 'ledger':
                        self.db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(external_id) DO UPDATE SET name=excluded.name,email=excluded.email,amount_cents=excluded.amount_cents,date=excluded.date,country=excluded.country,run_id=excluded.run_id,updated_at=excluded.updated_at', (r['external_id'], r['name'], r['email'], r['amount_cents'], r['date'], r['country'], rid, now()))
                    else:
                        self.db.execute('INSERT INTO dataset_records VALUES(?,?,?,?,?) ON CONFLICT(schema_id,record_key) DO UPDATE SET payload=excluded.payload,run_id=excluded.run_id,updated_at=excluded.updated_at', (schema_id,r[run['context']['schema']['definition']['key_field']],json.dumps(r),rid,now()))

                if self.db.execute('SELECT count(*) FROM dataset_records').fetchone()[0]>20000 or self.db.execute('SELECT coalesce(sum(length(payload)),0) FROM dataset_records').fetchone()[0]>20_000_000:
                    raise Problem('Custom destination-storage limit reached.',429)
                self.db.execute("UPDATE runs SET status='committed',committed_at=? WHERE id=?", (now(), rid))
                if run['counts']['insert'] + run['counts']['update']:
                    if schema_id == 'ledger': self.db.execute("UPDATE metadata SET value=value+1 WHERE key='revision'")
                    else: self.db.execute('UPDATE dataset_revisions SET revision=revision+1 WHERE schema_id=?',(schema_id,))
                self.event(rid, 'committed', f"{run['counts']['insert']} inserted; {run['counts']['update']} updated; {run['counts']['reject']} quarantined")
                self.db.execute('COMMIT')
            except Exception:
                self.db.execute('ROLLBACK')
                raise
            return self.run(rid)

    def overview(self):
        with self.lock:
            runs = [self.run(r['id']) for r in self.db.execute('SELECT id FROM runs ORDER BY created_at DESC').fetchall()]
            summaries = [{k: v for k, v in r.items() if k not in ('results', 'signature')} for r in runs]
            records = [dict(r) for r in self.db.execute('SELECT * FROM records ORDER BY updated_at DESC,external_id')]
            return {'runs': summaries, 'records': records, 'revision': self.revision(), 'events': [dict(r) for r in self.db.execute('SELECT * FROM events ORDER BY created_at DESC LIMIT 50')], 'fields': FIELDS, 'record_count':len(records)+self.db.execute('SELECT count(*) FROM dataset_records').fetchone()[0], 'schemas':self.schemas(), 'recipes':self.recipes()}

    def rejected_csv(self, rid):
        run = self.run(rid)
        headers = [h for h in run['mapping'].values() if h]
        return csv_bytes(['source_row', *headers, 'validation_errors'], [[r['row'], *[r['raw'].get(h, '') for h in headers], '; '.join(r['errors'])] for r in run['results'] if r['errors']])

    def export_csv(self, schema_id='ledger'):
        if schema_id != 'ledger':
            catalog = self.catalog(schema_id)
            fields = [f['key'] for f in catalog['schema']['definition']['fields']]
            return csv_bytes(fields, [[r.get(f) if r.get(f) is not None else '' for f in fields] for r in catalog['records']])
        records = self.overview()['records']
        return csv_bytes(FIELDS, [[r['external_id'], r['name'], r['email'], f"{r['amount_cents']/100:.2f}", r['date'], r['country']] for r in records])

    def options(self, raw=None):
        try: return parse_options(raw)
        except (ValidationError, ValueError) as exc: raise Problem(str(exc)[:1200]) from exc

    def schema(self, schema_id='ledger', version=None):
        if schema_id == 'ledger':
            if version not in (None, 1): raise Problem('Schema version not found.', 404)
            types = {'external_id':'text','name':'text','email':'email','amount':'decimal','date':'date','country':'country'}
            return {'id':'ledger','name':'Customer ledger','version':1,'versions':[1],'builtin':True,'definition':{'key_field':'external_id','fields':[{'key':key,'label':{'external_id':'External ID','name':'Customer name','email':'Email address','amount':'Amount · USD','date':'Record date','country':'Country code'}[key],'type':types[key],'required':True} for key in FIELDS]}}
        head = self.db.execute('SELECT * FROM schema_heads WHERE id=?',(schema_id,)).fetchone()
        if not head: raise Problem('Schema not found.',404)
        version = head['version'] if version is None else version
        row = self.db.execute('SELECT * FROM schema_versions WHERE schema_id=? AND version=?',(schema_id,version)).fetchone()
        if not row: raise Problem('Schema version not found.',404)
        versions = [r[0] for r in self.db.execute('SELECT version FROM schema_versions WHERE schema_id=? ORDER BY version DESC',(schema_id,))]
        return {'id':schema_id,'name':head['name'],'version':version,'versions':versions,'builtin':False,'definition':json.loads(row['definition'])}

    def schemas(self):
        return [self.schema('ledger'), *[self.schema(r[0]) for r in self.db.execute('SELECT id FROM schema_heads ORDER BY name,id')]]

    def check_mapping(self, mapping, schema, headers=None):
        rules = schema['definition']['fields']
        if set(mapping) != {f['key'] for f in rules} or any(not isinstance(v,str) or len(v)>100 for v in mapping.values()): raise Problem('Map every schema field; optional fields may be left unmapped.')
        selected = [v for v in mapping.values() if v]
        if len(set(selected)) != len(selected) or any(f['required'] and not mapping[f['key']] for f in rules): raise Problem('Required fields need distinct source columns; a column cannot be reused.')
        if headers is not None and any(v not in headers for v in selected): raise Problem('A mapped source column is missing. Choose the correct file or publish a new recipe version.')
        return dict(mapping)

    def save_schema(self, name, definition, schema_id=None, expected_version=None):
        try: definition = parse_definition(definition)
        except (ValidationError, ValueError) as exc: raise Problem(str(exc)[:1200]) from exc
        name = name.strip()
        if not 3 <= len(name) <= 100: raise Problem('Schema name must contain 3–100 characters.')
        if len(json.dumps(definition)) > 32000: raise Problem('Schema definition exceeds 32 KB.',413)
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                if schema_id:
                    old = self.schema(schema_id)
                    if old['builtin']: raise Problem('The built-in ledger schema is read-only.')
                    if old['version'] != expected_version: raise Problem('Schema changed. Reload the latest version before saving.',409)
                    if name != old['name']: raise Problem('Schema names are stable; create a new schema to rename or reshape a dataset.')
                    shape = lambda d: (d['key_field'], {f['key']:(f['type'],f['scale']) for f in d['fields']}, next(f['normalization'] for f in d['fields'] if f['key']==d['key_field']))
                    if shape(old['definition']) != shape(definition): raise Problem('Field keys/types/scales and key normalization are stable across versions. Create a new schema for structural changes.')
                    version = old['version']+1
                    if version > 20: raise Problem('Schema version limit reached.',429)
                    self.db.execute('UPDATE schema_heads SET version=? WHERE id=?',(version,schema_id))
                else:
                    if self.db.execute('SELECT count(*) FROM schema_heads').fetchone()[0] >= 20: raise Problem('Workspace schema limit reached.',429)
                    schema_id,version = identifier(),1
                    self.db.execute('INSERT INTO schema_heads VALUES(?,?,?)',(schema_id,name,version))
                    self.db.execute('INSERT INTO dataset_revisions VALUES(?,0)',(schema_id,))
                self.db.execute('INSERT INTO schema_versions VALUES(?,?,?,?)',(schema_id,version,json.dumps(definition),now()))
                self.configuration_budget()
                self.event(None,'schema.version_created',f'{name} · version {version}')
                self.db.execute('COMMIT')
            except Exception:
                self.db.execute('ROLLBACK');raise
            return self.schema(schema_id)

    def configuration_budget(self):
        size = self.db.execute('SELECT coalesce(sum(length(definition)),0) FROM schema_versions').fetchone()[0] + self.db.execute('SELECT coalesce(sum(length(snapshot)),0) FROM recipe_versions').fetchone()[0]
        if size > 2_000_000: raise Problem('Workspace configuration-storage limit reached.',429)

    def recipe(self, recipe_id, version=None):
        head = self.db.execute('SELECT * FROM recipe_heads WHERE id=?',(recipe_id,)).fetchone()
        if not head: raise Problem('Recipe not found.',404)
        version = head['version'] if version is None else version
        row = self.db.execute('SELECT snapshot FROM recipe_versions WHERE recipe_id=? AND version=?',(recipe_id,version)).fetchone()
        if not row: raise Problem('Recipe version not found.',404)
        return {'id':recipe_id,'version':version,**json.loads(row['snapshot'])}

    def recipes(self):
        return [{**self.recipe(r[0]),'versions':[v[0] for v in self.db.execute('SELECT version FROM recipe_versions WHERE recipe_id=? ORDER BY version DESC',(r[0],))]} for r in self.db.execute('SELECT id FROM recipe_heads ORDER BY name,id')]

    def save_recipe(self, name, schema_id, schema_version, mapping, policy, options=None, recipe_id=None, expected_version=None):
        name = name.strip()
        if not 3 <= len(name) <= 100: raise Problem('Recipe name must contain 3–100 characters.')
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                schema = self.schema(schema_id,schema_version)
                mapping = self.check_mapping(mapping,schema)
                if policy not in ('insert_only','upsert'): raise Problem('Unknown conflict policy.')
                snapshot = {'name':name,'schema_id':schema_id,'schema_version':schema['version'],'mapping':mapping,'policy':policy,'options':self.options(options)}
                if recipe_id:
                    head = self.recipe(recipe_id)
                    if head['version'] != expected_version: raise Problem('Recipe changed. Reload its latest version before saving.',409)
                    version = head['version']+1
                    if version>20: raise Problem('Recipe version limit reached.',429)
                    self.db.execute('UPDATE recipe_heads SET name=?,version=? WHERE id=?',(name,version,recipe_id))
                else:
                    if self.db.execute('SELECT count(*) FROM recipe_heads').fetchone()[0]>=20: raise Problem('Workspace recipe limit reached.',429)
                    recipe_id,version = identifier(),1
                    self.db.execute('INSERT INTO recipe_heads VALUES(?,?,?)',(recipe_id,name,version))
                self.db.execute('INSERT INTO recipe_versions VALUES(?,?,?,?)',(recipe_id,version,json.dumps(snapshot),now()))
                self.configuration_budget()
                self.event(None,'recipe.version_created',f'{name} · version {version}')
                self.db.execute('COMMIT')
            except Exception:
                self.db.execute('ROLLBACK');raise
            return self.recipe(recipe_id)

    def current_record(self, schema_id, key):
        if schema_id == 'ledger':
            row = self.db.execute('SELECT external_id,name,email,amount_cents,date,country FROM records WHERE external_id=?',(key,)).fetchone()
            return dict(row) if row else None
        row = self.db.execute('SELECT payload FROM dataset_records WHERE schema_id=? AND record_key=?',(schema_id,key)).fetchone()
        return json.loads(row[0]) if row else None

    def catalog(self, schema_id='ledger'):
        with self.lock:
            schema = self.schema(schema_id)
            if schema_id == 'ledger': records = [{**dict(r),'record_key':r['external_id']} for r in self.db.execute('SELECT * FROM records ORDER BY external_id')]
            else: records = [{**json.loads(r['payload']),'record_key':r['record_key'],'run_id':r['run_id'],'updated_at':r['updated_at']} for r in self.db.execute('SELECT * FROM dataset_records WHERE schema_id=? ORDER BY record_key',(schema_id,))]
            return {'schema':schema,'revision':self.revision(schema_id),'records':records}


    def seed(self):
        if self.overview()['runs']:
            return
        content = b'external_id,name,email,amount,date,country\nC-1001,Avery Chen,avery@example.test,245.90,2026-10-20,HK\nC-1002,Sam Rivera,sam@example.test,89.00,2026-10-21,SG\nC-1003,Noor Ali,noor@example.test,510.50,2026-10-22,GB\nC-1004,Jordan Lee,jordan@example.test,130.00,2026-10-23,AZ\n'
        mapping = {f: f for f in FIELDS}
        source = self.upload('customer-ledger-october.csv', content)
        self.commit(self.validate(source['id'], mapping, 'insert_only')['id'])
        review = content + b'C-1005,Morgan Blake,missing-at-sign,125.00,2026-10-24,US\nC-1006,Taylor Park,taylor@example.test,-20.00,2026-10-25,KR\nC-1007,Alex Quinn,alex@example.test,74.25,2026-10-25,CA\n'
        source = self.upload('partner-feed-review.csv', review)
        self.validate(source['id'], mapping, 'insert_only')
