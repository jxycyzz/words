"""Independent SQLite store. Never discovers or opens the desktop database."""
from __future__ import annotations

import json
import math
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from .clock import today, timestamp
from .domain.parent_settings import normalize_review_policy
from .domain.rules import sm2_next_state
from .domain.models import Word
from .domain.review_selection import ReviewSelector


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS words(
                id INTEGER PRIMARY KEY, word TEXT NOT NULL, word_key TEXT NOT NULL UNIQUE,
                translation TEXT NOT NULL, phonetic TEXT NOT NULL DEFAULT '',
                created_on TEXT NOT NULL, updated_at TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
                practice_count INTEGER NOT NULL DEFAULT 0, correct_count INTEGER NOT NULL DEFAULT 0,
                last_practiced_at TEXT, easiness REAL NOT NULL DEFAULT 2.5,
                interval_days INTEGER NOT NULL DEFAULT 0, due_on TEXT NOT NULL,
                repetitions INTEGER NOT NULL DEFAULT 0, lapses INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS review_history(
                id INTEGER PRIMARY KEY, word_id INTEGER REFERENCES words(id), word TEXT NOT NULL,
                translation TEXT NOT NULL, practiced_at TEXT NOT NULL, day TEXT NOT NULL,
                correct INTEGER NOT NULL, quality INTEGER NOT NULL, interval_days INTEGER NOT NULL,
                easiness REAL NOT NULL, due_on TEXT NOT NULL, session_id TEXT NOT NULL, source TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS history_day ON review_history(day);
            CREATE TABLE IF NOT EXISTS letter_mistakes(
                id INTEGER PRIMARY KEY, word_id INTEGER REFERENCES words(id), practiced_at TEXT NOT NULL,
                position INTEGER NOT NULL, expected_char TEXT NOT NULL, wrong_chars TEXT NOT NULL,
                answer_snapshot TEXT NOT NULL, session_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_review(
                day TEXT PRIMARY KEY, policy TEXT NOT NULL, word_ids TEXT NOT NULL,
                elapsed REAL NOT NULL DEFAULT 0, restarts INTEGER NOT NULL DEFAULT 0,
                retries TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS reward_attempts(
                id INTEGER PRIMARY KEY, day TEXT NOT NULL, session_id TEXT NOT NULL,
                round_number INTEGER NOT NULL, attempt_number INTEGER NOT NULL,
                result TEXT NOT NULL, recorded_at TEXT NOT NULL,
                UNIQUE(session_id,round_number,attempt_number));
            CREATE TABLE IF NOT EXISTS sessions(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, mode TEXT NOT NULL, day TEXT NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS input_events(
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, seq INTEGER NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, occurred_at TEXT NOT NULL,
                UNIQUE(session_id,seq));
            CREATE TABLE IF NOT EXISTS score_events(
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, points INTEGER NOT NULL,
                occurred_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operation_logs(
                id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL, event_type TEXT NOT NULL,
                summary TEXT NOT NULL, detail_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_cache(
                cache_key TEXT PRIMARY KEY, content TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settlement_events(
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, close_number INTEGER NOT NULL,
                day TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL, UNIQUE(session_id,close_number));
            CREATE TABLE IF NOT EXISTS migration_imports(
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, source_label TEXT NOT NULL,
                source_sha256 TEXT NOT NULL, source_bytes INTEGER NOT NULL,
                archive_path TEXT NOT NULL, archive_sha256 TEXT NOT NULL,
                imported_at TEXT NOT NULL, summary TEXT NOT NULL,
                UNIQUE(kind,source_label), UNIQUE(source_sha256));
            CREATE TABLE IF NOT EXISTS migration_word_map(
                import_id INTEGER NOT NULL REFERENCES migration_imports(id),
                source_word_id INTEGER NOT NULL, target_word_id INTEGER NOT NULL REFERENCES words(id),
                PRIMARY KEY(import_id,source_word_id));
        ''')
        additions = {'settlement_events':{'attempts':'INTEGER NOT NULL DEFAULT 0','last_error':"TEXT NOT NULL DEFAULT ''",'next_attempt':'REAL NOT NULL DEFAULT 0','sent_at':"TEXT NOT NULL DEFAULT ''"},
                     'ai_cache':{'word_id':'INTEGER','kind':"TEXT NOT NULL DEFAULT ''"}}
        for table,columns in additions.items():
            existing={row[1] for row in self.conn.execute(f'PRAGMA table_info({table})')}
            for column,definition in columns.items():
                if column not in existing:
                    self.conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        self.conn.executescript('''CREATE TABLE IF NOT EXISTS background_jobs(
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            progress INTEGER NOT NULL DEFAULT 0,total INTEGER NOT NULL DEFAULT 0,result TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_status ON background_jobs(status,id);''')
        self.conn.commit()

    def rows(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args)]

    def get_setting(self, key):
        row = self.conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key, value):
        self.conn.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    def log_operation(self, event_type, summary, **details):
        self.conn.execute('INSERT INTO operation_logs(occurred_at,event_type,summary,detail_json) VALUES(?,?,?,?)',
                          (timestamp(), event_type, summary, encoded(details)))

    def clear_future_daily_review_preparation(self, after_day=None):
        # This version prepares AI caches only; it never freezes tomorrow's policy early.
        pass

    def get_word(self, word_id):
        row = self.conn.execute('SELECT * FROM words WHERE id=?', (word_id,)).fetchone()
        if row is None:
            raise ValueError('单词不存在')
        return self.decorate(dict(row))

    @staticmethod
    def retention(word, target=None):
        target = target or date.fromisoformat(today())
        if not word['practice_count'] or not word['last_practiced_at']:
            age = max((target - date.fromisoformat(word['created_on'])).days, 0)
            return max(0.05, math.exp(-age / 1.5))
        age = max((target - date.fromisoformat(word['last_practiced_at'][:10])).days, 0)
        return math.exp(-age / max(word['interval_days'], 1))

    def decorate(self, word):
        acc = word['correct_count'] / word['practice_count'] if word['practice_count'] else 0
        return {**word, 'accuracy': round(acc * 100),
                'mastery': round((self.retention(word) * .65 + acc * .35) * 100)}

    def words(self, search='', start='', end='', include_archived=False):
        clauses, args = ['1=1'], []
        if not include_archived:
            clauses.append('archived=0')
        if search:
            clauses.append('(instr(lower(word),lower(?))>0 OR instr(translation,?)>0)')
            args.extend([search, search])
        if start:
            clauses.append('created_on>=?')
            args.append(start)
        if end:
            clauses.append('created_on<=?')
            args.append(end)
        return [self.decorate(w) for w in self.rows(
            'SELECT * FROM words WHERE ' + ' AND '.join(clauses) + ' ORDER BY created_on DESC,id DESC', args)]

    def save_word(self, payload, word_id=None):
        word = ' '.join(payload['word'].strip().split())
        if not word:
            raise ValueError('请输入英文单词或短语')
        key = word.casefold()
        date.fromisoformat(payload['created_on'])
        values = (word, key, payload['translation'].strip(), payload['phonetic'].strip(), payload['created_on'], timestamp())
        try:
            if word_id is None:
                cur = self.conn.execute('''INSERT INTO words(word,word_key,translation,phonetic,created_on,updated_at,due_on)
                    VALUES(?,?,?,?,?,?,?)''', (*values, payload['created_on']))
                word_id = cur.lastrowid
            else:
                self.get_word(word_id)
                self.conn.execute('UPDATE words SET word=?,word_key=?,translation=?,phonetic=?,created_on=?,updated_at=? WHERE id=?', (*values, word_id))
        except sqlite3.IntegrityError:
            raise ValueError('该单词已存在（包括已归档词条），请勿重复录入') from None
        self.conn.execute('DELETE FROM ai_cache WHERE word_id=?',(word_id,))
        self.log_operation('word_saved', f'保存词条：{word}', word_id=word_id)
        return self.get_word(word_id)

    def archive(self, ids):
        for word_id in ids:
            self.conn.execute('UPDATE words SET archived=1,updated_at=? WHERE id=?', (timestamp(), word_id))
        self.log_operation('words_archived', f'归档 {len(ids)} 个词条，保留学习历史', word_ids=ids)

    def review_score(self, word, day):
        overdue = max((day - date.fromisoformat(word['due_on'] or word['created_on'])).days, 0)
        if not word['practice_count']:
            return 3 + min(overdue / 3, 2)
        return (1 if word['due_on'] <= day.isoformat() else 0) + overdue * .08 + (1-self.retention(word, day))*1.5 + (1-word['correct_count']/word['practice_count']) + min(word['lapses']*.25, 1)

    def daily(self, day=None):
        row = self.conn.execute('SELECT * FROM daily_review WHERE day=?', (day or today(),)).fetchone()
        if not row:
            return None
        result = dict(row)
        for key in ('policy', 'word_ids', 'retries'):
            result[key] = json.loads(result[key])
        return result

    def prepare_daily(self, policy):
        day = today()
        existing = self.daily(day)
        policy = existing['policy'] if existing else normalize_review_policy(policy)
        # Preserve the desktop overdue roll-forward behavior for prospective schedules.
        self.conn.execute('UPDATE words SET due_on=?,updated_at=? WHERE archived=0 AND due_on<?', (day,timestamp(),day))
        words = self.words()
        if not words:
            raise ValueError('当前没有可复习的单词，请先录入词条')
        domain_words=[Word(**{k:w[k] for k in Word.__dataclass_fields__}) for w in words]
        selector=ReviewSelector(domain_words,date.fromisoformat(day))
        chosen=selector.select(existing['word_ids'] if existing else [],policy['word_count'])
        self.conn.execute('''INSERT INTO daily_review(day,policy,word_ids) VALUES(?,?,?)
            ON CONFLICT(day) DO UPDATE SET word_ids=excluded.word_ids''', (day,encoded(policy),encoded(chosen)))
        if not existing:
            self.log_operation('daily_policy_frozen','当天复习规则已冻结',day=day,policy=policy)
        return self.daily(day)

    def answer(self, word_id, correct, session_id, mode):
        w = self.get_word(word_id)
        quality = 5 if correct else 2
        ease, reps, interval, due = sm2_next_state(quality,w['easiness'],w['repetitions'],w['interval_days'],date.fromisoformat(today()))
        self.conn.execute('''UPDATE words SET practice_count=practice_count+1,correct_count=correct_count+?,
            last_practiced_at=?,updated_at=?,easiness=?,repetitions=?,interval_days=?,due_on=?,lapses=lapses+? WHERE id=?''',
            (int(correct),timestamp(),timestamp(),ease,reps,interval,due,int(not correct),word_id))
        self.conn.execute('''INSERT INTO review_history(word_id,word,translation,practiced_at,day,correct,quality,
            interval_days,easiness,due_on,session_id,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (word_id,w['word'],w['translation'],timestamp(),today(),int(correct),quality,interval,ease,due,session_id,mode))

    def best_rounds(self, day):
        best = {}
        for row in self.rows('SELECT * FROM reward_attempts WHERE day=? ORDER BY id', (day,)):
            result = json.loads(row['result'])
            key = row['round_number']
            prior = best.get(key)
            if prior is None or (result['reward_points'],result['accuracy_percent']) > (prior['reward_points'],prior['accuracy_percent']):
                best[key] = {**result,'round_number':key,'recorded_at':row['recorded_at']}
        return best

    def money(self, day, points):
        daily = self.daily(day)
        if daily is None:
            return 0.0
        ceiling = max(int(daily['policy'].get('reward_point_ceiling',400) or 400),1)
        return round(float(daily['policy']['perfect_reward_money']) * min(max(points,0),ceiling) / ceiling, 2)

    def summary(self):
        total = self.conn.execute('SELECT coalesce(sum(points),0) FROM score_events').fetchone()[0]
        review = self.daily()
        preview_points, points = self.daily_points(today())
        return {'word_count':len(self.words()),'total_score':total,'review_seconds':review['elapsed'] if review else 0,
                'reward_points':points,'reward_money':self.money(today(),points),
                'saved_reward_points':points,'preview_reward_money':self.money(today(),preview_points),
                'usage_seconds':float(self.get_setting('usage_seconds') or 0),
                'daily':review,'today':today()}

    def add_usage(self, seconds):
        self.set_setting('usage_seconds',str(float(self.get_setting('usage_seconds') or 0)+max(seconds,0)))

    def wrong_words(self, start, end, limit=12):
        return self.rows('''SELECT word_id,word,translation,count(*) AS wrong_count FROM review_history
            WHERE day BETWEEN ? AND ? AND correct=0 GROUP BY word_id ORDER BY wrong_count DESC,max(id) DESC LIMIT ?''',(start,end,limit))

    def mistakes(self, word_id):
        rows=self.rows('SELECT * FROM letter_mistakes WHERE word_id=? ORDER BY id DESC LIMIT 8',(word_id,))
        return [{'position':r['position']+1,'expected_char':'空格' if r['expected_char']==' ' else r['expected_char'],
                 'wrong_char':('空格' if json.loads(r['wrong_chars'])[-1]==' ' else json.loads(r['wrong_chars'])[-1]),
                 'wrong_chars':['空格' if c==' ' else c for c in json.loads(r['wrong_chars'])],
                 'answer_snapshot':r['answer_snapshot'],'practiced_at':r['practiced_at']} for r in rows if json.loads(r['wrong_chars'])]

    def ai_context(self, start, end, word=None):
        report=self.report(start,end)
        practiced=sum(r['practiced'] for r in report['daily'])
        correct=sum(r['correct'] for r in report['daily'])
        return {'date_range':{'start':start,'end':end},'current_word':word,
            'summary':{**report['mastery_summary'],'practiced':practiced,'correct':correct,'accuracy_percent':round(correct/practiced*100) if practiced else 0,
                'word_count':len(report['mastery'])},'daily':report['daily'],
            'recent_wrong_words':self.wrong_words(start,end),
            'low_mastery_words':sorted(report['mastery'],key=lambda w:w['mastery'])[:12],
            'recent_history':report['history'][:30]}

    def enqueue_job(self, kind, payload):
        text=encoded(payload)
        previous=self.rows("SELECT id FROM background_jobs WHERE kind=? AND payload=? AND status IN ('pending','running')",(kind,text))
        if previous: return previous[0]['id']
        cursor=self.conn.execute('INSERT INTO background_jobs(kind,payload,created_at,updated_at) VALUES(?,?,?,?)',(kind,text,timestamp(),timestamp()))
        return cursor.lastrowid

    def settlement_payload(self, day, session_id, after_id, trigger):
        daily=self.daily(day)
        if daily is None:
            raise ValueError('结算缺少当天冻结规则，停止发送')
        history=self.rows('SELECT * FROM review_history WHERE day=? AND id>? AND session_id=?',(day,after_id,session_id))
        rounds=list(self.best_rounds(day).values())
        points=sum(r['reward_points'] for r in rounds)
        correct=sum(r['correct'] for r in history)
        return {'day':day,'session_id':session_id,'review_scope':'current_window','review_history_after_id':after_id,
            'practiced':len(history),'unique_words':len({r['word_id'] for r in history}),'correct':correct,
            'accuracy':round(correct/len(history)*100) if history else 0,
            'reward_rounds':rounds,'reward_points':points,'reward_money':self.money(day,points),
            'review_policy':daily['policy'],'review_elapsed_seconds':int(daily['elapsed']),
            'usage_seconds':int(float(self.get_setting('usage_seconds') or 0)),
            'completed_30_minutes':daily['elapsed']>=1800,'close_trigger':trigger,'closed_at':timestamp()}

    def daily_points(self, day):
        rounds = self.best_rounds(day)
        saved = sum(r['reward_points'] for r in rounds.values())
        rows = self.rows("SELECT payload FROM sessions WHERE day=? AND mode IN ('review','debug') ORDER BY updated_at DESC,rowid DESC LIMIT 1",(day,))
        preview = 0.0
        if rows:
            payload = json.loads(rows[0]['payload'])
            number = payload.get('reward_slot',payload['state']['current_round'])
            preview = max(payload['preview_points']-rounds.get(number,{}).get('reward_points',0),0)
        daily = self.daily(day)
        ceiling = max(int((daily or {}).get('policy',{}).get('reward_point_ceiling',400) or 400),1)
        return min(saved+preview,ceiling),saved

    def report(self, start, end):
        history = self.rows('SELECT * FROM review_history WHERE day BETWEEN ? AND ? ORDER BY id DESC',(start,end))
        days = sorted({h['day'] for h in history} | {r['day'] for r in self.rows('SELECT day FROM daily_review WHERE day BETWEEN ? AND ?',(start,end))},reverse=True)
        daily = []
        for day in days:
            items = [h for h in history if h['day']==day]
            count = len(items)
            correct = sum(h['correct'] for h in items)
            rounds = list(self.best_rounds(day).values())
            preview_points,points = self.daily_points(day)
            state = self.daily(day)
            required_dates={day,(date.fromisoformat(day)-timedelta(days=1)).isoformat()}
            required=sum(self.get_word(i)['created_on'] in required_dates for i in state['word_ids']) if state else 0
            daily.append({'day':day,'practiced':count,'unique_words':len({h['word_id'] for h in items}),
                'correct':correct,'accuracy':round(correct/count*100) if count else 0,
                'reward_points':points,'reward_money':self.money(day,points),'rounds':rounds,
                'saved_reward_points':points,'preview_reward_money':self.money(day,preview_points),
                'elapsed_seconds':state['elapsed'] if state else 0,'review_count':len(state['word_ids']) if state else 0,
                'required_count':required,'old_count':len(state['word_ids'])-required if state else 0,
                'review_policy':state['policy'] if state else None})
        words = self.words()
        buckets={'high':0,'medium':0,'low':0,'new':0,'due':0,'total':len(words)}
        for w in words:
            if w['due_on']<=today(): buckets['due']+=1
            bucket='new' if not w['practice_count'] else 'high' if w['mastery']>=80 else 'medium' if w['mastery']>=50 else 'low'
            buckets[bucket]+=1
        current = date.fromisoformat(today())
        curve = [{'day':n,'retention':round(sum(self.retention(w,current+timedelta(days=n)) for w in words)/len(words)*100,1) if words else 0} for n in range(31)]
        return {'daily':daily,'history':history[:1000],'history_total':len(history),'mastery':words,'mastery_summary':buckets,'curve':curve,
                'reward_money':round(sum(d['reward_money'] for d in daily),2)}

    def backup(self, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(str(destination)) as target:
            self.conn.backup(target)
