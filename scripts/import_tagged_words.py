"""Import a reviewed JSON vocabulary list without changing existing learning results."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from backend.store import Store


LEARNING_COLUMNS = (
    'id','created_on','archived','practice_count','correct_count','last_practiced_at',
    'easiness','interval_days','due_on','repetitions','lapses',
)
PROTECTED_TABLES = ('review_history','letter_mistakes','daily_review','reward_attempts','sessions',
                    'input_events','score_events','settlement_events')


def learning_digest(store, maximum_id):
    rows=store.rows('SELECT '+','.join(LEARNING_COLUMNS)+' FROM words WHERE id<=? ORDER BY id',(maximum_id,))
    payload=json.dumps(rows,ensure_ascii=False,separators=(',',':'),sort_keys=True).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def protected_counts(store):
    return {table:store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in PROTECTED_TABLES}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--database',required=True)
    parser.add_argument('--input',required=True,help='UTF-8 JSON array containing word, translation and phonetic')
    parser.add_argument('--source-file',required=True,help='Original source file retained for audit')
    parser.add_argument('--source-label',required=True)
    parser.add_argument('--tag',default='grade8_upper')
    parser.add_argument('--backup-dir',required=True)
    args=parser.parse_args()

    input_path=Path(args.input)
    source_path=Path(args.source_file)
    entries=json.loads(input_path.read_text(encoding='utf-8'))
    if not isinstance(entries,list) or not entries or len(entries)>5000:
        raise SystemExit('导入清单必须是 1–5000 行的 JSON 数组')
    for index,item in enumerate(entries,1):
        if not isinstance(item,dict) or not str(item.get('word') or '').strip():
            raise SystemExit(f'第 {index} 行缺少有效英文')

    source_bytes=source_path.read_bytes()
    source_sha256=hashlib.sha256(source_bytes).hexdigest()
    store=Store(args.database)
    maximum_id=store.conn.execute('SELECT coalesce(max(id),0) FROM words').fetchone()[0]
    before_digest=learning_digest(store,maximum_id)
    before_counts=protected_counts(store)
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
    backup=Path(args.backup_dir)/f'pre-{args.tag}-import-{stamp}.sqlite3'
    store.backup(backup)
    try:
        with store.conn:
            summary=store.import_tagged_words(entries,args.tag,args.source_label,source_sha256,len(source_bytes))
            if learning_digest(store,maximum_id)!=before_digest:
                raise RuntimeError('既有单词的学习状态发生变化，导入已回滚')
            if protected_counts(store)!=before_counts:
                raise RuntimeError('学习、计时、积分或结算记录发生变化，导入已回滚')
        integrity=store.conn.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity!='ok':
            raise RuntimeError('数据库完整性检查失败：'+str(integrity))
        summary.update({'source_sha256':source_sha256,'backup':str(backup),'integrity':integrity})
        print(json.dumps(summary,ensure_ascii=False,sort_keys=True))
    finally:
        store.conn.close()


if __name__=='__main__':
    main()
