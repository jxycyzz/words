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
            DELETE FROM daily_review;
            DELETE FROM background_jobs;
            DELETE FROM ai_cache;
            DELETE FROM operation_logs;
            DELETE FROM settings;
            DELETE FROM words;
        ''')
    return {'ok':True}
