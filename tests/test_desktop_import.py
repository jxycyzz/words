import asyncio
import json
import sqlite3

import pytest

from backend.desktop_import import import_desktop_database
from backend.integrations import Integrations
from backend.store import Store


def desktop_fixture(path):
    db=sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE words(id INTEGER PRIMARY KEY,word TEXT,word_key TEXT,translation TEXT,phonetic TEXT,
          created_on TEXT,updated_at TEXT,practice_count INTEGER,correct_count INTEGER,last_practiced_at TEXT,
          easiness REAL,interval_days INTEGER,due_on TEXT,repetitions INTEGER,lapses INTEGER);
        CREATE TABLE review_history(id INTEGER PRIMARY KEY,word_id INTEGER,practiced_at TEXT,correct INTEGER,
          quality INTEGER,interval_days INTEGER,easiness REAL,due_on TEXT,source TEXT);
        CREATE TABLE letter_mistakes(id INTEGER PRIMARY KEY,word_id INTEGER,practiced_at TEXT,position INTEGER,
          expected_char TEXT,wrong_char TEXT,wrong_chars TEXT,answer_snapshot TEXT,source TEXT);
        CREATE TABLE daily_review_words(day TEXT,word_id INTEGER,position INTEGER,reason TEXT,created_at TEXT,updated_at TEXT);
        CREATE TABLE daily_reward_rounds(id INTEGER PRIMARY KEY,day TEXT,round_number INTEGER,game_round INTEGER,
          completed_at TEXT,correct_chars INTEGER,total_chars INTEGER,duration_seconds REAL,accuracy_percent REAL,
          cpm REAL,speed_percent REAL,max_score INTEGER,reward_points INTEGER);
        CREATE TABLE daily_review_state(day TEXT PRIMARY KEY,payload TEXT,updated_at TEXT);
        CREATE TABLE daily_review_meta(day TEXT PRIMARY KEY,restart_count INTEGER,retry_round1_count INTEGER,
          retry_round2_count INTEGER,retry_round3_count INTEGER,elapsed_seconds INTEGER,speed_multiplier REAL,updated_at TEXT);
        CREATE TABLE settlement_email_events(id INTEGER PRIMARY KEY,day TEXT,trigger TEXT,status TEXT,attempts INTEGER,
          sent_at TEXT,last_error TEXT,payload TEXT,created_at TEXT,updated_at TEXT);
        CREATE TABLE daily_settlement_emails(day TEXT PRIMARY KEY,status TEXT,attempts INTEGER,sent_at TEXT,last_error TEXT,updated_at TEXT);
        CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE operation_logs(id INTEGER PRIMARY KEY,occurred_at TEXT,event_type TEXT,summary TEXT,
          subject_type TEXT,subject_id TEXT,detail_json TEXT);
        CREATE TABLE ai_cache(id INTEGER PRIMARY KEY,kind TEXT,cache_key TEXT,model TEXT,content TEXT,
          created_at TEXT,updated_at TEXT,subject_word_id INTEGER);
    ''')
    db.executemany('INSERT INTO words VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',[
        (1,'cat','cat','猫','[kæt]','2026-01-01','2026-01-02 10:00:00',2,1,'2026-01-02 10:00:00',2.3,1,'2026-01-03',0,1),
        (2,'bed','bed','床','[bed]','2026-01-01','2026-01-02 10:01:00',1,1,'2026-01-02 10:01:00',2.6,1,'2026-01-03',1,0),
    ])
    db.executemany('INSERT INTO review_history VALUES(?,?,?,?,?,?,?,?,?)',[
        (1,1,'2026-01-02 10:00:00',0,2,1,2.3,'2026-01-03','game'),
        (3,2,'2026-01-02 10:01:00',1,5,1,2.6,'2026-01-03','game'),
    ])
    db.execute('INSERT INTO letter_mistakes VALUES(5,1,?,?,?,?,?,?,?)',
               ('2026-01-02 10:00:00',0,'c','x','["x","x","x"]','cat','game'))
    db.executemany('INSERT INTO daily_review_words VALUES(?,?,?,?,?,?)',[
        ('2026-01-02',1,0,'review','2026-01-02 09:00:00','2026-01-02 09:00:00'),
        ('2026-01-02',2,1,'review','2026-01-02 09:00:00','2026-01-02 09:00:00'),
    ])
    db.execute('INSERT INTO daily_reward_rounds VALUES(7,?,?,?,?,?,?,?,?,?,?,?,?)',
               ('2026-01-02',1,1,'2026-01-02 10:02:00',9,10,60,90,9,0,50,45))
    db.execute('INSERT INTO daily_review_state VALUES(?,?,?)',('2026-01-02','{"game_state":{"current_round":2}}','2026-01-02 10:02:00'))
    db.execute('INSERT INTO daily_review_meta VALUES(?,?,?,?,?,?,?,?)',('2026-01-02',1,2,0,0,60,.2,'2026-01-02 10:02:00'))
    payload=json.dumps({'day':'2026-01-02','reward_points':45,'reward_money':2.25})
    db.execute('INSERT INTO settlement_email_events VALUES(9,?,?,?,?,?,?,?,?,?)',
               ('2026-01-02','game_closed','sent',1,'2026-01-02 10:03:00','',payload,'2026-01-02 10:02:00','2026-01-02 10:03:00'))
    db.execute('INSERT INTO daily_settlement_emails VALUES(?,?,?,?,?,?)',('2026-01-02','sent',1,'2026-01-02 10:03:00','','2026-01-02 10:03:00'))
    db.executemany('INSERT INTO settings VALUES(?,?)',[('total_score','100'),('usage_seconds','10')])
    db.execute('INSERT INTO operation_logs VALUES(2,?,?,?,?,?,?)',
               ('2026-01-02 10:03:00','word_answered','desktop answer','word','2','{"correct":true}'))
    db.execute('INSERT INTO ai_cache VALUES(4,?,?,?,?,?,?,?)',
               ('word_note','desktop-key','model','desktop card','2026-01-02 10:00:00','2026-01-02 10:00:00',2))
    db.commit(); db.close()


def test_desktop_database_import_is_audited_complete_and_idempotent(tmp_path):
    source=tmp_path/'desktop.sqlite3'; target=tmp_path/'web.sqlite3'; archive=tmp_path/'imports'
    desktop_fixture(source)
    store=Store(target)
    cat=store.save_word({'word':'cat','translation':'猫咪','phonetic':'','created_on':'2026-02-01'})
    store.conn.execute('UPDATE words SET practice_count=1,correct_count=1,last_practiced_at=?,updated_at=?,easiness=2.7,repetitions=1,interval_days=1,due_on=? WHERE id=?',
                       ('2026-02-01T12:00:00+08:00','2026-02-01T12:00:00+08:00','2026-02-02',cat['id']))
    store.conn.execute('INSERT INTO score_events(session_id,points,occurred_at) VALUES(?,?,?)',('web',10,'2026-02-01T12:00:00+08:00'))
    store.set_setting('usage_seconds','5')
    store.conn.commit(); store.conn.close()

    result=import_desktop_database(source,target,archive)
    assert result['status']=='imported'
    assert result['words_inserted']==1 and result['words_merged']==1
    assert result['history_imported']==2 and result['score_imported']==100
    assert result['reward_rounds_imported']==1 and result['settlements_imported']==1
    assert (archive/f"desktop-{result['source_sha256'][:16]}.sqlite3").is_file()
    assert list((tmp_path/'backups').glob('pre-desktop-import-*.sqlite3'))

    store=Store(target)
    assert len(store.words())==2
    merged=store.get_word(cat['id'])
    assert merged['practice_count']==3 and merged['correct_count']==2
    assert merged['translation']=='猫咪' and merged['due_on']=='2026-02-02'
    assert store.summary()['total_score']==110
    assert store.summary()['review_usage_seconds']==60
    assert store.summary()['usage_seconds']==60
    assert store.get_setting('usage_seconds')=='15.0'  # Legacy whole-app time is retained but not displayed.
    report=store.report('2026-01-02','2026-01-02')
    assert report['history_total']==2
    assert report['daily'][0]['reward_points']==45
    assert report['daily'][0]['reward_money']==2.25
    assert store.rows("SELECT status FROM settlement_events WHERE id<0")==[{'status':'desktop_sent'}]
    assert store.rows("SELECT content FROM ai_cache WHERE kind='desktop/note'")==[{'content':'desktop card'}]
    bed=next(word for word in store.words() if word['word']=='bed')
    services=Integrations(tmp_path)
    class NoCallService:
        model='test-model'; base_url='https://example.invalid'
        def generate_word_note(self,*args):
            raise AssertionError('imported desktop card should prevent an AI call')
    services.service=lambda kind:NoCallService()
    promoted=asyncio.run(services.ai(store,'note',bed['id']))
    assert promoted=={'content':'desktop card','cached':True}
    assert store.rows("SELECT content FROM ai_cache WHERE word_id=? AND kind='note'",(bed['id'],))==[{'content':'desktop card'}]
    before={table:store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
            for table in ('words','review_history','letter_mistakes','operation_logs','score_events','reward_attempts')}
    store.conn.close()

    repeated=import_desktop_database(source,target,archive)
    assert repeated['status']=='already_imported'
    store=Store(target)
    after={table:store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in before}
    store.conn.close()
    assert after==before


def test_changed_source_is_blocked_after_same_source_label_was_imported(tmp_path):
    source=tmp_path/'desktop.sqlite3'; target=tmp_path/'web.sqlite3'
    desktop_fixture(source)
    import_desktop_database(source,target,tmp_path/'imports')
    db=sqlite3.connect(source)
    db.execute("INSERT INTO operation_logs VALUES(3,'2026-01-03','app_close','later','','','{}')")
    db.commit(); db.close()
    with pytest.raises(ValueError,match='避免重复历史'):
        import_desktop_database(source,target,tmp_path/'imports')
