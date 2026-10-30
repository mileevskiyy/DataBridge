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

def parse_file(name, content):
    if len(content) > MAX_BYTES:
        raise Problem('Files must be smaller than 2 MB.', 413)
    try:
        text = content.decode('utf-8-sig')
        if name.lower().endswith('.csv'):
            reader = csv.DictReader(io.StringIO(text), strict=True)
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
    writer.writerow(headers)
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
        PRAGMA user_version=1;
        ''')

    def close(self):
        self.db.close()

    def revision(self):
        return self.db.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0]

    def source(self, source_id):
        row = self.db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
        if not row:
            raise Problem('Source not found.', 404)
        return {**dict(row), 'headers': json.loads(row['headers']), 'rows': json.loads(row['rows'])}

    def upload(self, name, content):
        headers, rows = parse_file(name, content)
        with self.lock:
            if self.db.execute('SELECT count(*) FROM sources').fetchone()[0] >= 100:
                raise Problem('This workspace has reached its 100-upload limit. Restart the disposable demo to reset it.', 429)
            if self.db.execute('SELECT coalesce(sum(length(rows)),0) FROM sources').fetchone()[0] + len(json.dumps(rows)) > 10_000_000:
                raise Problem('Workspace source-storage limit reached. Restart the disposable demo to reset it.', 429)
            sid = identifier()
            self.db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)', (sid, name[:160], hashlib.sha256(content).hexdigest(), json.dumps(headers), json.dumps(rows), now()))
            return {'id': sid, 'name': name[:160], 'headers': headers, 'row_count': len(rows), 'preview': rows[:5]}

    def validate(self, source_id, mapping, policy):
        with self.lock:
            source = self.source(source_id)
            if set(mapping) != set(FIELDS) or any(v not in source['headers'] for v in mapping.values()) or len(set(mapping.values())) != len(FIELDS):
                raise Problem('Map each target field to a different existing source column.')
            if policy not in ('insert_only', 'upsert'):
                raise Problem('Unknown conflict policy.')
            revision = self.revision()
            signature = hashlib.sha256(json.dumps([source['digest'], mapping, policy, revision], sort_keys=True).encode()).hexdigest()
            existing = self.db.execute('SELECT id FROM runs WHERE signature=?', (signature,)).fetchone()
            if existing:
                return self.run(existing['id'])
            if self.db.execute('SELECT count(*) FROM runs').fetchone()[0] >= 200:
                raise Problem('This workspace has reached its validation-run limit.', 429)
            seen, results = {}, []
            for number, raw in enumerate(source['rows'], 1):
                record, errors = normalize(raw, mapping)
                action = 'insert'
                if not errors:
                    key = record['external_id']
                    if key in seen:
                        if seen[key] == record:
                            action = 'skip'
                        else:
                            errors.append('external_id: conflicting duplicate within this file')
                    else:
                        seen[key] = record
                        current = self.db.execute('SELECT external_id,name,email,amount_cents,date,country FROM records WHERE external_id=?', (key,)).fetchone()
                        if current:
                            if dict(current) == record:
                                action = 'skip'
                            elif policy == 'upsert':
                                action = 'update'
                            else:
                                errors.append('external_id: already exists with different values; use upsert to update')
                results.append({'row': number, 'raw': raw, 'record': record, 'errors': errors, 'action': 'reject' if errors else action})
            if self.db.execute('SELECT coalesce(sum(length(results)),0) FROM runs').fetchone()[0] + len(json.dumps(results)) > 20_000_000:
                raise Problem('Workspace report-storage limit reached.', 429)
            rid = identifier()
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,NULL)', (rid, source_id, signature, json.dumps(mapping), policy, revision, 'validated', json.dumps(results), now()))
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
            result['stale'] = result['status'] == 'validated' and result['base_revision'] != self.revision()
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
                for row in run['results']:
                    if row['action'] not in ('insert', 'update'):
                        continue
                    r = row['record']
                    self.db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(external_id) DO UPDATE SET name=excluded.name,email=excluded.email,amount_cents=excluded.amount_cents,date=excluded.date,country=excluded.country,run_id=excluded.run_id,updated_at=excluded.updated_at', (r['external_id'], r['name'], r['email'], r['amount_cents'], r['date'], r['country'], rid, now()))
                self.db.execute("UPDATE runs SET status='committed',committed_at=? WHERE id=?", (now(), rid))
                if run['counts']['insert'] + run['counts']['update']:
                    self.db.execute("UPDATE metadata SET value=value+1 WHERE key='revision'")
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
            return {'runs': summaries, 'records': records, 'revision': self.revision(), 'events': [dict(r) for r in self.db.execute('SELECT * FROM events ORDER BY created_at DESC LIMIT 50')], 'fields': FIELDS}

    def rejected_csv(self, rid):
        run = self.run(rid)
        headers = list(run['mapping'].values())
        return csv_bytes(['source_row', *headers, 'validation_errors'], [[r['row'], *[r['raw'].get(h, '') for h in headers], '; '.join(r['errors'])] for r in run['results'] if r['errors']])

    def export_csv(self):
        records = self.overview()['records']
        return csv_bytes(FIELDS, [[r['external_id'], r['name'], r['email'], f"{r['amount_cents']/100:.2f}", r['date'], r['country']] for r in records])

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
