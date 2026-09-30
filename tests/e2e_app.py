from backend.main import create_app
from .fakes import FakeServices

app=create_app(testing=True,services=FakeServices())


@app.post('/api/test-reset')
async def test_reset():
    """Keep each browser specification isolated inside the disposable E2E database."""
    store=app.state.store
    app.state.game=None
    with store.conn:
        store.conn.executescript('''
            DELETE FROM input_events;
            DELETE FROM score_events;
            DELETE FROM letter_mistakes;
            DELETE FROM review_history;
            DELETE FROM reward_attempts;
            DELETE FROM settlement_events;
            DELETE FROM sessions;
            DELETE FROM daily_review_scopes;
            DELETE FROM daily_review;
            DELETE FROM background_jobs;
            DELETE FROM ai_cache;
            DELETE FROM operation_logs;
            DELETE FROM catalog_imports;
            DELETE FROM settings;
            DELETE FROM words;
        ''')
    return {'ok':True}


@app.post('/api/test-seed-grade8')
async def test_seed_grade8():
    store=app.state.store
    entries=[{'word':f'grade-word-{index:03}','translation':f'初二上释义 {index}','phonetic':''} for index in range(80)]
    with store.conn:
        result=store.import_tagged_words(entries,'grade8_upper','e2e-fixture.xlsx','c'*64,1)
    return result
