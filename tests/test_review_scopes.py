import json

from backend.clock import today
from backend.domain.parent_settings import normalize_review_policy
from backend.game import GameSession
from backend.store import Store


def payload(word, translation='释义', phonetic=''):
    return {'word':word,'translation':translation,'phonetic':phonetic,'created_on':today()}


def test_catalog_import_is_idempotent_and_preserves_existing_learning_state():
    store=Store(':memory:')
    existing=store.save_word(payload('Rose','原有释义',''))
    with store.conn:
        store.conn.execute('''UPDATE words SET practice_count=7,correct_count=5,last_practiced_at=?,
            easiness=2.7,interval_days=6,due_on=?,repetitions=3,lapses=1 WHERE id=?''',
            (today()+'T08:00:00+08:00',today(),existing['id']))
    before=dict(store.conn.execute('SELECT * FROM words WHERE id=?',(existing['id'],)).fetchone())
    entries=[
        {'word':'rose','translation':'玫瑰','phonetic':'/rəʊz/'},
        {'word':'Rose','translation':'罗斯','phonetic':'/rəʊz/'},
        {'word':'ancient','translation':'古代的；古老的','phonetic':'/ˈeɪnʃənt/'},
    ]
    with store.conn:
        first=store.import_tagged_words(entries,'grade8_upper','fixture.xlsx','a'*64,123)
    after=dict(store.conn.execute('SELECT * FROM words WHERE id=?',(existing['id'],)).fetchone())
    for key in ('practice_count','correct_count','last_practiced_at','easiness','interval_days','due_on','repetitions','lapses','created_on'):
        assert after[key]==before[key]
    assert after['translation']=='原有释义'
    assert after['phonetic']=='/rəʊz/'
    assert first['unique_words']==2 and first['inserted']==1 and first['tagged']==2
    assert store.conn.execute('SELECT count(*) FROM words').fetchone()[0]==2
    assert store.conn.execute("SELECT count(*) FROM word_tags WHERE tag_key='grade8_upper'").fetchone()[0]==2
    with store.conn:
        second=store.import_tagged_words(entries,'grade8_upper','fixture.xlsx','a'*64,123)
    assert second['inserted']==0
    assert store.conn.execute('SELECT count(*) FROM words').fetchone()[0]==2
    assert store.conn.execute("SELECT count(*) FROM word_tags WHERE tag_key='grade8_upper'").fetchone()[0]==2


def test_grade8_scope_selects_exactly_75_tagged_words_and_keeps_default_separate():
    store=Store(':memory:')
    entries=[{'word':f'grade-word-{n:03}','translation':f'释义{n}','phonetic':''} for n in range(100)]
    with store.conn:
        store.import_tagged_words(entries,'grade8_upper','fixture.xlsx','b'*64,456)
        outsider=store.save_word(payload('outside-word'))
    policy=normalize_review_policy({'word_count':75})
    with store.conn:
        grade=store.prepare_daily(policy,'grade8_upper')
    assert len(grade['word_ids'])==75
    assert outsider['id'] not in grade['word_ids']
    tagged={row['word_id'] for row in store.rows("SELECT word_id FROM word_tags WHERE tag_key='grade8_upper'")}
    assert set(grade['word_ids'])<=tagged
    with store.conn:
        default=store.prepare_daily(policy,'all')
    assert outsider['id'] in default['word_ids']
    assert store.daily_scope(today(),'grade8_upper')['word_ids']==grade['word_ids']
    assert store.daily_scope(today(),'all')['word_ids']==default['word_ids']
    with store.conn:
        repeated=store.prepare_daily(policy,'grade8_upper')
    assert repeated['word_ids']==grade['word_ids']


def test_review_session_persists_selection_scope_without_affecting_results():
    store=Store(':memory:')
    word=store.save_word(payload('scope-word'))
    with store.conn:
        store.prepare_daily(normalize_review_policy(),'all')
    game=GameSession(store,'owner','debug',[word],normalize_review_policy(),selection_scope='grade8_upper')
    with store.conn:
        game.save()
    row=store.rows('SELECT * FROM sessions WHERE id=?',(game.id,))[0]
    saved=json.loads(row['payload'])
    assert saved['selection_scope']=='grade8_upper'
    restored=GameSession.restore(store,row)
    assert restored.selection_scope=='grade8_upper'
    assert restored.view()['selection_scope_label']=='初二上'
