import json
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.clock import today
from backend.domain.parent_settings import ParentSettingsManager, normalize_review_policy
from backend.game import GameSession
from backend.main import create_app
from backend.schemas import Command
from backend.store import Store
from tests.fakes import FakeServices


class Clock:
    value = 0.0
    def __call__(self):
        return self.value
    def advance(self, delta=.033):
        self.value += delta


@pytest.fixture
def store():
    store = Store(':memory:')
    yield store
    store.conn.close()


def word(store, text='cat'):
    return store.save_word({'word':text,'translation':'测试释义','phonetic':'','created_on':today()})


def game(store, mode='review', words=None):
    words = words or [word(store)]
    policy = normalize_review_policy()
    if mode in ('review','debug'):
        store.prepare_daily(policy)
    clock = Clock()
    session = GameSession(store,'test-owner',mode,words,policy,clock)
    session.connect()
    clock.advance()
    session.tick()
    return session, clock


def type_word(session, text):
    if session.voice_required:
        # Isolated unit test stands in for an ASR response; no transcript API exists.
        session.state.lock_voice_target(text,500)
    for char in text:
        session.command({'seq':session.seq+1,'type':'key','char':char})


def test_points_come_only_from_events_and_replay_is_idempotent(store):
    session, clock = game(store,'practice')
    type_word(session,'cat')
    assert store.summary()['total_score']==10
    assert len(store.rows('SELECT * FROM review_history'))==1
    session.command({'seq':3,'type':'key','char':'t'})
    assert store.summary()['total_score']==10
    assert len(store.rows('SELECT * FROM input_events'))==3
    assert session.elapsed==pytest.approx(.033)


@pytest.mark.parametrize('payload',[
    {'seq':1,'type':'key','char':'a','score':999},
    {'seq':1,'type':'key','char':'cat'},
    {'seq':1,'type':'result','reward':400},
    {'seq':1,'type':'voice_start','transcript':'cat'},
    {'seq':1,'type':'speed','value':100},
    {'seq':True,'type':'key','char':'a'},
    {'seq':1,'type':'key','char':'a','badge':1},
])
def test_client_cannot_supply_results(payload):
    with pytest.raises(ValueError):
        Command.model_validate(payload)


def test_unlocked_review_typing_does_not_score(store):
    session, clock = game(store)
    session.command({'seq':1,'type':'key','char':'c'})
    assert session.state.active[0].progress==0
    assert store.summary()['total_score']==0


def test_two_round_rewards_and_report_agree(store):
    session, clock = game(store)
    type_word(session,'cat')
    session.save()
    assert session.view()['reward_money']==2
    assert store.summary()['reward_money']==0
    assert store.summary()['preview_reward_money']==2
    assert store.report(today(),today())['daily'][0]['reward_money']==0
    clock.advance(); session.tick()
    assert store.best_rounds(today())[1]['reward_points']==200
    clock.advance(1.1); session.tick()
    type_word(session,'cat')
    clock.advance(); session.tick()
    assert session.status=='completed'
    assert session.view()['reward_money']==4
    assert store.summary()['reward_money']==4
    payload = json.loads(store.rows('SELECT payload FROM settlement_events')[0]['payload'])
    assert payload['reward_money']==4
    assert store.summary()['total_score']==20


def test_retry_keeps_best_and_records_each_attempt(store):
    session,clock = game(store)
    type_word(session,'cat')
    clock.advance(); session.tick()
    session.command({'seq':session.seq+1,'type':'restart'})
    assert store.best_rounds(today())[1]['reward_points']==200
    clock.advance(); session.tick()
    type_word(session,'cat')
    clock.advance(); session.tick()
    assert len(store.rows('SELECT * FROM reward_attempts'))==2
    assert store.summary()['reward_money']==2


def test_disconnect_and_process_restore_preserve_progress_without_offline_time(store):
    session,clock = game(store,'practice')
    session.command({'seq':1,'type':'key','char':'c'})
    clock.advance(.5); session.tick()
    before = session.elapsed
    session.disconnect()
    clock.advance(900)
    session.tick()
    assert session.elapsed==before
    row = store.rows('SELECT * FROM sessions WHERE id=?',(session.id,))[0]
    restored = GameSession.restore(store,row,clock)
    restored.connect()
    assert restored.state.active[0].progress==1
    assert restored.seq==1
    assert restored.elapsed==before
    type_word(restored,'at')
    assert store.summary()['total_score']==10


def test_pause_excludes_time_and_stale_voice_is_rejected(store):
    session,clock = game(store)
    before = session.elapsed
    session.command({'seq':1,'type':'voice_start'})
    ticket = session.voice_ticket
    clock.advance(10); session.last_contact=clock(); session.tick()
    assert session.elapsed==before
    session.apply_voice(ticket,'cat')
    with pytest.raises(ValueError):
        session.apply_voice(ticket,'cat')


def test_free_hint_does_not_create_reward_or_answer(store):
    session,clock = game(store)
    session.command({'seq':1,'type':'hint_start','badge':1})
    assert len(session.state.pending_round)==3
    session.command({'seq':2,'type':'hint_end'})
    type_word(session,'cat')
    assert store.summary()['total_score']==0
    assert store.rows('SELECT * FROM review_history')==[]
    assert session.view()['reward_money']==0


def test_retry_and_restart_caps_persist(store):
    session,clock = game(store)
    for _ in range(3):
        session.command({'seq':session.seq+1,'type':'retry'})
    with pytest.raises(ValueError):
        session.command({'seq':session.seq+1,'type':'retry'})
    assert store.daily()['retries']['1']==3
    for _ in range(2):
        session.command({'seq':session.seq+1,'type':'restart'})
    with pytest.raises(ValueError):
        session.command({'seq':session.seq+1,'type':'restart'})
    assert store.daily()['restarts']==2


def test_day_policy_is_frozen_and_password_protected(store):
    word(store)
    manager = ParentSettingsManager(store)
    manager.initialize_password('parent123','parent123')
    store.prepare_daily(manager.current_policy())
    manager.save_policy('parent123',{'word_count':90,'round_count':3,'perfect_reward_money':6})
    assert store.daily()['policy']['round_count']==2
    assert manager.current_policy()['round_count']==3
    with pytest.raises(ValueError):
        manager.save_policy('wrong',{})


def test_archive_preserves_historical_word_snapshot(store):
    w = word(store)
    store.answer(w['id'],True,'test','practice')
    store.archive([w['id']])
    assert store.words()==[]
    assert store.report(today(),today())['history'][0]['word']=='cat'


def test_required_new_words_can_exceed_target_and_list_is_stable(store):
    for n in range(12): word(store,f'word{n}')
    first = store.prepare_daily({'word_count':10})['word_ids']
    second = store.prepare_daily({'word_count':10})['word_ids']
    assert first==second and len(first)==12


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path,testing=True)) as client:
        client.headers['X-WordLearner-Request']='1'
        client.get('/api/bootstrap')
        yield client


def api_word(client,text='cat'):
    result = client.post('/api/words',json={'word':text,'translation':'猫','created_on':today()})
    assert result.status_code==200,result.text
    return result.json()


def test_api_crud_archive_and_extra_fields(client):
    w = api_word(client)
    assert len(client.get('/api/words').json())==1
    assert client.post('/api/words',json={'word':'bad','created_on':today(),'correct_count':999}).status_code==422
    assert client.post('/api/words',json={'word':' CAT ','created_on':today()}).status_code==400
    assert client.post('/api/words/archive',json={'ids':[w['id']]}).status_code==200
    assert client.get('/api/words').json()==[]


def test_cross_origin_and_missing_header_rejected(client):
    assert client.post('/api/words',json={},headers={'Origin':'https://attacker.example'}).status_code==403
    assert client.post('/api/words',json={},headers={'X-WordLearner-Request':''}).status_code==403


def test_public_deployment_host_and_secure_cookie(tmp_path,monkeypatch):
    monkeypatch.setenv('WORDLEARNER_PUBLIC_MODE','1')
    monkeypatch.setenv('WORDLEARNER_ALLOWED_HOSTS','words.rfdsx.online')
    monkeypatch.setenv('WORDLEARNER_COOKIE_SECURE','1')
    with TestClient(create_app(tmp_path,testing=True),base_url='https://words.rfdsx.online') as public_client:
        response=public_client.get('/api/health')
        assert response.status_code==200
        assert 'Secure' in response.headers['set-cookie']
        assert public_client.get('/api/health',headers={'Host':'attacker.example'}).status_code==400


def test_services_fail_explicitly_without_config(client):
    if not client.get('/api/bootstrap').json()['capabilities']['ai']:
        assert client.post('/api/ai',json={'task':'chat','question':'test'}).status_code==400


def test_game_session_owner_and_input_contract(client):
    client.app.state.services=FakeServices()
    client.app.state.jobs.services=client.app.state.services
    w = api_word(client)
    result = client.post('/api/games',json={'mode':'practice','word_ids':[w['id']]})
    job_id=result.json()['job_id']
    for _ in range(100):
        job=client.get(f'/api/jobs/{job_id}').json()
        if job['status'] in ('completed','failed'):break
        time.sleep(.02)
    assert job['status']=='completed',job
    game_id=job['result']['id']
    with client.websocket_connect(f'/api/games/{game_id}/socket',headers={'Origin':'http://testserver'}) as ws:
        state = ws.receive_json()
        assert state['type']=='state'
        ws.send_json({'seq':1,'type':'key','char':'c','score':999})
        for _ in range(20):
            msg=ws.receive_json()
            if msg['type']=='error': break
        assert msg['type']=='error' and msg['seq']==0
        ws.send_text('{broken json')
        for _ in range(20):
            msg=ws.receive_json()
            if msg['type']=='error': break
        assert msg['type']=='error'
    client.cookies.clear()
    assert client.get(f'/api/games/{game_id}').status_code==404
    assert client.post('/api/games',json={'mode':'practice','word_ids':[w['id']]}).status_code==409


def test_parent_api_rejects_bad_password(client):
    assert client.post('/api/parent/password',json={'password':'parent123','confirmation':'parent123'}).status_code==200
    payload={'password':'wrong','word_count':80,'round_count':1,'perfect_reward_money':2}
    assert client.put('/api/parent/policy',json=payload).status_code==400
    payload['password']='parent123'
    assert client.put('/api/parent/policy',json=payload).status_code==200
    assert client.get('/api/bootstrap').json()['policy']['word_count']==80


def test_data_directory_cannot_escape_new_project(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(tmp_path)


def test_database_failure_rolls_back_memory_and_pending_results(store,monkeypatch):
    session,clock = game(store,'practice')
    type_word(session,'ca')
    store.conn.commit()
    original = store.answer
    def fail(*args):
        raise RuntimeError('simulated storage failure in isolated test')
    monkeypatch.setattr(store,'answer',fail)
    with pytest.raises(RuntimeError):
        with session.atomic():
            session.command({'seq':3,'type':'key','char':'t'})
    assert session.state.score==0
    assert session.state.active[0].progress==2
    assert session.seq==2
    assert store.rows('SELECT * FROM review_history')==[]
    monkeypatch.setattr(store,'answer',original)
    with session.atomic():
        session.command({'seq':3,'type':'key','char':'t'})
    assert store.summary()['total_score']==10


def test_unconfigured_review_does_not_freeze_policy(client):
    api_word(client)
    if not client.get('/api/bootstrap').json()['capabilities']['asr']:
        assert client.post('/api/games',json={'mode':'review'}).status_code==400
        assert client.get('/api/bootstrap').json()['summary']['daily'] is None


def test_day_rollover_retains_measured_partial_preview(store,monkeypatch):
    import backend.game as module
    session,clock = game(store,words=[word(store,'cat'),word(store,'sun')])
    actual = session.state.active[0].answer
    type_word(session,actual)
    before=session.view()['reward_money']
    assert before==1
    oldday=today()
    monkeypatch.setattr(module,'today',lambda: (date.fromisoformat(oldday)+timedelta(days=1)).isoformat())
    clock.advance(); session.tick()
    assert session.status=='completed'
    assert store.report(oldday,oldday)['daily'][0]['reward_money']==0
    assert store.report(oldday,oldday)['daily'][0]['preview_reward_money']==before
