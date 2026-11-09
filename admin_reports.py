"""Run on trusted admin machine/server only; never expose this command as a public API."""
import argparse
import sqlite3
from pathlib import Path
from app import db_connect

parser = argparse.ArgumentParser()
parser.add_argument('--export', type=Path, help='Directory to export securely stored reported previews')
args = parser.parse_args()
with db_connect() as db:
    rows = db.execute('SELECT id,created_at,reason,notes,review_image FROM reports ORDER BY created_at DESC').fetchall()
    if args.export:
        args.export.mkdir(mode=0o700, parents=True, exist_ok=True)
    for report_id, date, reason, notes, image in rows:
        print(f'{date} | {report_id} | {reason} | {notes[:200]}')
        if args.export and image:
            target = args.export / f'{report_id}.jpg'
            target.write_bytes(image)
            target.chmod(0o600)
            print(f'   Review file: {target}')
