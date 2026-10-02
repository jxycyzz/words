import os
import time

from backend.main import prune_database_backups
from backend.store import Store


def test_backup_retention_keeps_only_newest_snapshot_across_all_prefixes(tmp_path):
    backup_dir=tmp_path/'backups'
    backup_dir.mkdir()
    snapshots=[
        backup_dir/'web-old.sqlite3',
        backup_dir/'pre-import.sqlite3',
        backup_dir/'post-import.sqlite3',
    ]
    base=time.time_ns()-10_000_000_000
    for index,path in enumerate(snapshots,1):
        path.write_bytes(f'snapshot-{index}'.encode())
        moment=base+index*1_000_000_000
        os.utime(path,ns=(moment,moment))
    unrelated=backup_dir/'source.json'
    unrelated.write_text('{}',encoding='utf-8')

    prune_database_backups(backup_dir,keep=1)

    assert [path.name for path in backup_dir.glob('*.sqlite3')]==['post-import.sqlite3']
    assert unrelated.exists()


def test_backup_closes_destination_before_retention_deletes_it(tmp_path):
    store=Store(':memory:')
    destination=tmp_path/'backups'/'snapshot.sqlite3'
    try:
        store.backup(destination)
        destination.unlink()
        assert not destination.exists()
    finally:
        store.conn.close()
