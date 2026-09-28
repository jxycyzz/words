"""Audited, one-time import of a desktop WordLearner SQLite database."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .clock import timestamp
from .main import process_lock
from .store import Store, encoded


KIND = 'desktop-sqlite'
REQUIRED_TABLES = {
    'words','review_history','letter_mistakes','daily_review_words','daily_reward_rounds',
    'daily_review_state','daily_review_meta','settlement_email_events','settings','operation_logs','ai_cache',
}
LEGACY_POLICY = {
    'version':0,'word_count':75,'round_count':3,'perfect_reward_money':5.0,
    'reward_point_ceiling':100,'round_max_scores':[50,30,20],
}
CURRENT_POLICY = {
    'version':1,'word_count':75,'round_count':2,'perfect_reward_money':4.0,
    'reward_point_ceiling':400,'round_max_scores':[200,200],
}


def sha256(path: Path) -> str:
    digest=hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def _stamp_key(value) -> str:
    return str(value or '').replace('T',' ')[:19]


def _later(left, right):
    return left if _stamp_key(left)>=_stamp_key(right) else right


def _negative_id(source_id: int, maximum: int) -> int:
    return -(maximum-int(source_id)+1)


def _json_object(value) -> dict:
    try:
        parsed=json.loads(value or '{}')
        return parsed if isinstance(parsed,dict) else {'desktop_detail':parsed}
    except (TypeError,json.JSONDecodeError):
        return {'desktop_detail_raw':str(value or '')}


def _table_names(conn) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _validate_source(source) -> None:
    missing=sorted(REQUIRED_TABLES-_table_names(source))
    if missing:
        raise ValueError('单机数据库缺少必要表：'+', '.join(missing))
    result=source.execute('PRAGMA integrity_check').fetchone()[0]
    if result!='ok':
        raise ValueError('单机数据库完整性检查失败：'+str(result))


def _ensure_target_schema(target_path: Path) -> None:
    store=Store(target_path)
    store.conn.close()


def _backup_database(conn, destination: Path) -> None:
    destination.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(destination)) as backup:
        conn.backup(backup)


def _archive_source(source, archive_path: Path) -> str:
    archive_path.parent.mkdir(parents=True,exist_ok=True)
    temporary=archive_path.with_name(archive_path.name+'.tmp')
    temporary.unlink(missing_ok=True)
    try:
        with closing(sqlite3.connect(temporary)) as archive:
            source.backup(archive)
        with closing(sqlite3.connect(f'file:{temporary.as_posix()}?mode=ro',uri=True)) as check:
            result=check.execute('PRAGMA integrity_check').fetchone()[0]
        if result!='ok':
            raise ValueError('归档数据库完整性检查失败：'+str(result))
        temporary.replace(archive_path)
    finally:
        temporary.unlink(missing_ok=True)
    return sha256(archive_path)


def _policy_for_day(day, ids, rewards, setting_values, transition_day):
    raw=setting_values.get('daily_review_policy:'+day)
    if raw:
        try:
            policy=json.loads(raw)
            if isinstance(policy,dict):
                return policy,'desktop_setting'
        except json.JSONDecodeError:
            pass
    current=any(int(row['max_score'])>=100 for row in rewards)
    if not rewards and transition_day:
        current=day>=transition_day
    policy=dict(CURRENT_POLICY if current else LEGACY_POLICY)
    policy['round_max_scores']=list(policy['round_max_scores'])
    if ids:
        policy['word_count']=len(ids)
    return policy,'recorded_reward_scheme'


def import_desktop_database(source_path, target_path, archive_dir, source_label='desktop-primary') -> dict:
    source_path=Path(source_path).resolve()
    target_path=Path(target_path).resolve()
    archive_dir=Path(archive_dir).resolve()
    if source_path==target_path:
        raise ValueError('单机数据库和 B/S 数据库不能是同一个文件')
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    initial_stat=source_path.stat()
    source_bytes=initial_stat.st_size
    source_hash=sha256(source_path)
    source=sqlite3.connect(f'file:{source_path.as_posix()}?mode=ro',uri=True)
    source.row_factory=sqlite3.Row
    _validate_source(source)
    moment=datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_path=target_path.parent/'backups'/f'pre-desktop-import-{moment}.sqlite3'
    if target_path.is_file():
        before=sqlite3.connect(target_path); before.row_factory=sqlite3.Row
        if 'migration_imports' in _table_names(before):
            existing=before.execute('SELECT * FROM migration_imports WHERE kind=? AND source_label=?',(KIND,source_label)).fetchone()
            if existing:
                before.close(); source.close()
                if existing['source_sha256']!=source_hash:
                    raise ValueError('该来源已经导入过另一版本；为避免重复历史，必须先审核增量后再导入')
                return {'status':'already_imported','import_id':existing['id'],**json.loads(existing['summary'])}
        _backup_database(before,backup_path)
        before.close()
    _ensure_target_schema(target_path)
    target=sqlite3.connect(target_path)
    target.row_factory=sqlite3.Row
    target.execute('PRAGMA foreign_keys=ON')
    if not backup_path.exists():
        _backup_database(target,backup_path)
    imported_id_tables=('review_history','letter_mistakes','score_events','reward_attempts',
                        'operation_logs','settlement_events')
    negative_tables=[name for name in imported_id_tables
                     if target.execute(f'SELECT 1 FROM {name} WHERE id<0 LIMIT 1').fetchone()]
    if negative_tables:
        source.close(); target.close()
        raise ValueError('B/S 数据库已含旧版迁移记录，不能重复写入负数审计编号：'+', '.join(negative_tables))
    archive_path=archive_dir/f'desktop-{source_hash[:16]}.sqlite3'
    archive_hash=_archive_source(source,archive_path)
    final_stat=source_path.stat()
    if (final_stat.st_size,final_stat.st_mtime_ns)!=(initial_stat.st_size,initial_stat.st_mtime_ns) or sha256(source_path)!=source_hash:
        source.close(); target.close()
        raise ValueError('单机数据库在迁移过程中发生变化，请关闭单机版后重试')
    summary=defaultdict(int)
    summary.update({'source_words':source.execute('SELECT count(*) FROM words').fetchone()[0],
                    'source_history':source.execute('SELECT count(*) FROM review_history').fetchone()[0],
                    'source_mistakes':source.execute('SELECT count(*) FROM letter_mistakes').fetchone()[0],
                    'source_logs':source.execute('SELECT count(*) FROM operation_logs').fetchone()[0],
                    'source_ai_cache':source.execute('SELECT count(*) FROM ai_cache').fetchone()[0]})
    try:
        target.execute('BEGIN IMMEDIATE')
        cursor=target.execute('''INSERT INTO migration_imports(kind,source_label,source_sha256,source_bytes,
            archive_path,archive_sha256,imported_at,summary) VALUES(?,?,?,?,?,?,?,?)''',
            (KIND,source_label,source_hash,source_bytes,str(archive_path),archive_hash,timestamp(),'{}'))
        import_id=cursor.lastrowid
        source_words={int(row['id']):dict(row) for row in source.execute('SELECT * FROM words ORDER BY id')}
        word_map={}
        cache_word_map={}
        for source_id,row in source_words.items():
            key=str(row['word_key'] or row['word']).strip().casefold()
            existing_word=target.execute('SELECT * FROM words WHERE word_key=?',(key,)).fetchone()
            if existing_word is None:
                cursor=target.execute('''INSERT INTO words(word,word_key,translation,phonetic,created_on,updated_at,archived,
                    practice_count,correct_count,last_practiced_at,easiness,interval_days,due_on,repetitions,lapses)
                    VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?,?,?)''',
                    (row['word'],key,row['translation'],row['phonetic'],row['created_on'],row['updated_at'],
                     row['practice_count'],row['correct_count'],row['last_practiced_at'],row['easiness'],row['interval_days'],
                     row['due_on'],row['repetitions'],row['lapses']))
                target_id=int(cursor.lastrowid); summary['words_inserted']+=1
                cache_word_map[source_id]=target_id
            else:
                current=dict(existing_word); target_id=int(current['id'])
                source_is_newer=_stamp_key(row['last_practiced_at'])>_stamp_key(current['last_practiced_at'])
                metadata_source_is_newer=_stamp_key(row['updated_at'])>_stamp_key(current['updated_at'])
                state=row if source_is_newer else current
                meta=row if metadata_source_is_newer else current
                target.execute('''UPDATE words SET word=?,translation=?,phonetic=?,created_on=?,updated_at=?,
                    practice_count=?,correct_count=?,last_practiced_at=?,easiness=?,interval_days=?,due_on=?,repetitions=?,lapses=? WHERE id=?''',
                    (meta['word'],meta['translation'],meta['phonetic'],min(row['created_on'],current['created_on']),
                     _later(row['updated_at'],current['updated_at']),int(row['practice_count'])+int(current['practice_count']),
                     int(row['correct_count'])+int(current['correct_count']),_later(row['last_practiced_at'],current['last_practiced_at']),
                     state['easiness'],state['interval_days'],state['due_on'],state['repetitions'],
                     int(row['lapses'])+int(current['lapses']),target_id))
                summary['words_merged']+=1
                if metadata_source_is_newer or all(str(row[name] or '').strip()==str(current[name] or '').strip()
                                                   for name in ('word','translation','phonetic')):
                    cache_word_map[source_id]=target_id
            word_map[source_id]=target_id
            target.execute('INSERT INTO migration_word_map(import_id,source_word_id,target_word_id) VALUES(?,?,?)',
                           (import_id,source_id,target_id))

        history_max=source.execute('SELECT coalesce(max(id),0) FROM review_history').fetchone()[0]
        session_id='desktop-import:'+source_hash[:16]
        for row in source.execute('SELECT * FROM review_history ORDER BY id'):
            word=source_words[int(row['word_id'])]
            target.execute('''INSERT INTO review_history(id,word_id,word,translation,practiced_at,day,correct,quality,
                interval_days,easiness,due_on,session_id,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (_negative_id(row['id'],history_max),word_map[int(row['word_id'])],word['word'],word['translation'],
                 row['practiced_at'],str(row['practiced_at'])[:10],row['correct'],row['quality'],row['interval_days'],
                 row['easiness'],row['due_on'],session_id,'desktop/'+str(row['source'])))
            summary['history_imported']+=1

        mistake_max=source.execute('SELECT coalesce(max(id),0) FROM letter_mistakes').fetchone()[0]
        for row in source.execute('SELECT * FROM letter_mistakes ORDER BY id'):
            target.execute('''INSERT INTO letter_mistakes(id,word_id,practiced_at,position,expected_char,wrong_chars,
                answer_snapshot,session_id) VALUES(?,?,?,?,?,?,?,?)''',
                (_negative_id(row['id'],mistake_max),word_map[int(row['word_id'])],row['practiced_at'],row['position'],
                 row['expected_char'],row['wrong_chars'],row['answer_snapshot'],session_id))
            summary['mistakes_imported']+=1

        setting_values={str(row['key']):str(row['value']) for row in source.execute('SELECT * FROM settings')}
        for key,value in setting_values.items():
            target.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           ('desktop:'+key,value))
        desktop_usage=max(float(setting_values.get('usage_seconds','0') or 0),0)
        target_usage=max(float((target.execute("SELECT value FROM settings WHERE key='usage_seconds'").fetchone() or ['0'])[0] or 0),0)
        target.execute("INSERT INTO settings(key,value) VALUES('usage_seconds',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (str(target_usage+desktop_usage),))
        summary['usage_seconds_imported']=desktop_usage
        desktop_score=max(int(setting_values.get('total_score','0') or 0),0)
        if desktop_score:
            last_activity=source.execute("SELECT coalesce(max(practiced_at),'') FROM review_history").fetchone()[0] or timestamp()
            target.execute('INSERT INTO score_events(id,session_id,points,occurred_at) VALUES(-1,?,?,?)',
                           (session_id,desktop_score,last_activity))
        summary['score_imported']=desktop_score

        daily_words=defaultdict(list)
        for row in source.execute('SELECT * FROM daily_review_words ORDER BY day,position'):
            if int(row['word_id']) in word_map:
                daily_words[str(row['day'])].append(word_map[int(row['word_id'])])
        rewards=defaultdict(list)
        for row in source.execute('SELECT * FROM daily_reward_rounds ORDER BY id'):
            rewards[str(row['day'])].append(dict(row))
        transition=source.execute('SELECT min(day) FROM daily_reward_rounds WHERE max_score>=100').fetchone()[0]
        meta={str(row['day']):dict(row) for row in source.execute('SELECT * FROM daily_review_meta')}
        days=sorted(set(daily_words)|set(meta)|set(rewards))
        for day in days:
            if target.execute('SELECT 1 FROM daily_review WHERE day=?',(day,)).fetchone():
                summary['daily_conflicts_preserved']+=1
                continue
            ids=daily_words.get(day,[]); info=meta.get(day,{})
            policy,basis=_policy_for_day(day,ids,rewards.get(day,[]),setting_values,transition)
            retries={str(i):int(info.get(f'retry_round{i}_count',0) or 0) for i in (1,2,3)}
            target.execute('INSERT INTO daily_review(day,policy,word_ids,elapsed,restarts,retries) VALUES(?,?,?,?,?,?)',
                (day,encoded(policy),encoded(ids),float(info.get('elapsed_seconds',0) or 0),
                 int(info.get('restart_count',0) or 0),encoded(retries)))
            summary['daily_imported']+=1
            summary['daily_policy_from_'+basis]+=1

        reward_max=source.execute('SELECT coalesce(max(id),0) FROM daily_reward_rounds').fetchone()[0]
        for row in source.execute('SELECT * FROM daily_reward_rounds ORDER BY id'):
            result={name:row[name] for name in ('correct_chars','total_chars','duration_seconds','accuracy_percent','cpm',
                                                'speed_percent','max_score','reward_points')}
            result['actual_round_number']=row['game_round']
            target.execute('''INSERT INTO reward_attempts(id,day,session_id,round_number,attempt_number,result,recorded_at)
                VALUES(?,?,?,?,?,?,?)''',(_negative_id(row['id'],reward_max),row['day'],session_id,row['round_number'],
                row['id'],encoded(result),row['completed_at']))
            summary['reward_rounds_imported']+=1

        log_max=source.execute('SELECT coalesce(max(id),0) FROM operation_logs').fetchone()[0]
        for row in source.execute('SELECT * FROM operation_logs ORDER BY id'):
            detail=_json_object(row['detail_json'])
            detail['_desktop_provenance']={'source_id':row['id'],'subject_type':row['subject_type'],'subject_id':row['subject_id']}
            target.execute('INSERT INTO operation_logs(id,occurred_at,event_type,summary,detail_json) VALUES(?,?,?,?,?)',
                (_negative_id(row['id'],log_max),row['occurred_at'],row['event_type'],row['summary'],encoded(detail)))
            summary['logs_imported']+=1

        email_max=source.execute('SELECT coalesce(max(id),0) FROM settlement_email_events').fetchone()[0]
        for row in source.execute('SELECT * FROM settlement_email_events ORDER BY id'):
            target.execute('''INSERT INTO settlement_events(id,session_id,close_number,day,payload,status,created_at,
                attempts,last_error,next_attempt,sent_at) VALUES(?,?,?,?,?,?,?,?,?,0,?)''',
                (_negative_id(row['id'],email_max),f'{session_id}:email:{row["id"]}',1,row['day'],row['payload'],
                 'desktop_'+str(row['status']),row['created_at'],row['attempts'],row['last_error'],row['sent_at']))
            summary['settlements_imported']+=1

        cache_kinds={'word_note':'note','entry_check':'entry','error_hint':'error'}
        for row in source.execute('SELECT * FROM ai_cache ORDER BY id'):
            mapped=cache_word_map.get(int(row['subject_word_id'])) if row['subject_word_id'] is not None else None
            key=f'desktop:{row["kind"]}:{row["model"]}:{row["cache_key"]}'
            target.execute('INSERT OR IGNORE INTO ai_cache(cache_key,content,created_at,word_id,kind) VALUES(?,?,?,?,?)',
                (key,row['content'],row['created_at'],mapped,'desktop/'+cache_kinds.get(str(row['kind']),str(row['kind']))))
            summary['ai_cache_imported']+=1

        final_summary=dict(summary)
        final_summary.update({'source_sha256':source_hash,'source_bytes':source_bytes,'archive_path':str(archive_path),
                              'archive_sha256':archive_hash,'backup_path':str(backup_path)})
        target.execute('UPDATE migration_imports SET summary=? WHERE id=?',(encoded(final_summary),import_id))
        target.execute('INSERT INTO operation_logs(occurred_at,event_type,summary,detail_json) VALUES(?,?,?,?)',
            (timestamp(),'desktop_database_imported','已导入单机版数据库',encoded({'import_id':import_id,**final_summary})))
        if target.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('导入后外键检查失败')
        target.commit()
    except Exception:
        target.rollback(); source.close(); target.close(); raise
    source.close(); target.close()
    return {'status':'imported','import_id':import_id,**final_summary}


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description='Import a desktop WordLearner database into the B/S database.')
    parser.add_argument('source',type=Path)
    parser.add_argument('target',type=Path)
    parser.add_argument('--archive-dir',type=Path)
    parser.add_argument('--source-label',default='desktop-primary')
    args=parser.parse_args(argv)
    target=args.target.resolve()
    archive=(args.archive_dir or target.parent/'imports').resolve()
    with process_lock(target.parent):
        result=import_desktop_database(args.source,target,archive,args.source_label)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
