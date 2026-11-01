"""Use the same engine from a shell for a reproducible single-file import."""
import argparse
import json
from pathlib import Path
from .engine import Engine, FIELDS, Problem

def main():
    parser = argparse.ArgumentParser(description='Validate and optionally import a CSV/JSON source with DataBridge.')
    parser.add_argument('file', type=Path)
    parser.add_argument('--database', default=':memory:', help='SQLite file or :memory: for a disposable dry run')
    parser.add_argument('--mapping', type=Path, help='JSON object mapping canonical fields to source headers')
    parser.add_argument('--policy', choices=['insert_only', 'upsert'], default=None)
    parser.add_argument('--schema-id',default=None,help='Custom destination schema ID; default: ledger')
    parser.add_argument('--schema-version',type=int,help='Exact schema rule version; default: latest')
    parser.add_argument('--recipe-id',help='Saved recipe in the selected database')
    parser.add_argument('--recipe-version',type=int,help='Exact recipe version; default: latest')
    parser.add_argument('--delimiter',choices=[',',';','\t','|'],help='CSV delimiter for manual configuration')
    parser.add_argument('--commit', action='store_true', help='Commit valid rows; rejected rows remain quarantined')
    parser.add_argument('--errors', type=Path, help='Write a CSV error report')
    args = parser.parse_args()
    if args.database != ':memory:':
        Path(args.database).parent.mkdir(parents=True, exist_ok=True)
    engine = Engine(args.database)
    try:
        recipe = engine.recipe(args.recipe_id,args.recipe_version) if args.recipe_id else None
        schema = engine.schema(args.schema_id or 'ledger',args.schema_version) if not recipe else None
        mapping = json.loads(args.mapping.read_text()) if args.mapping else None if recipe else {f['key']:f['key'] for f in schema['definition']['fields']}
        delimiter = args.delimiter or (recipe['options']['delimiter'] if recipe else ',')
        if recipe and args.delimiter and args.delimiter!=recipe['options']['delimiter']:
            raise Problem('Delimiter cannot override a saved recipe.')
        source = engine.upload(args.file.name, args.file.read_bytes(),delimiter)
        run = engine.validate(source['id'], mapping, args.policy,schema_id=args.schema_id,schema_version=args.schema_version,recipe_id=args.recipe_id,recipe_version=args.recipe_version,options=None if recipe else {'delimiter':delimiter})
        if args.commit:
            run = engine.commit(run['id'])
        if args.errors:
            args.errors.write_bytes(engine.rejected_csv(run['id']))
        print(json.dumps({k:run[k] for k in ('id','status','total','counts')}, indent=2))
        return 2 if run['counts']['reject'] else 0
    except (Problem, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, str(exc) + '\n')
    finally:
        engine.close()

if __name__ == '__main__':
    raise SystemExit(main())
