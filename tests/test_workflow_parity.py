import asyncio
import json
import time
from datetime import date,timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.clock import today
from backend.domain.parent_settings import normalize_review_policy
from backend.game import GameSession
from backend.integrations import Integrations
from backend.jobs import JobWorker
from backend.mail import MailWorker,message_for,validate_payload
from backend.main import create_app
from backend.store import Store
from tests.fakes import FakeServices
from tests.test_server import Clock,word,game,type_word


@pytest.fixture
def store():
    instance=Store(':memory:')
    yield instance
    instance.conn.close()


def wait_job(client,job_id):
    for _ in range(150):
        job=client.get(f'/api/jobs/{job_id}').json()
        if job['status'] in ('completed','failed'): return job
        time.sleep(.02)
    pytest.fail('background job did not finish')


def test_preflight_failure_never_creates_game_or_scores(tmp_path):
    services=FakeServices(); services.fail=True
    with TestClient(create_app(tmp_path,testing=True,services=services)) as client:
        client.headers['X-WordLearner-Request']='1'
        w=client.post('/api/words',json={'word':'bed','created_on':today()}).json()
        result=client.post('/api/games',json={'mode':'practice','word_ids':[w['id']]}).json()
        assert wait_job(client,result['job_id'])['status']=='failed'
        assert client.get('/api/bootstrap').json()['active_game'] is None
        assert client.get('/api/bootstrap').json()['summary']['total_score']==0
        assert client.get('/api/reports').json()['history']==[]
        assert ('note',w['id']) in services.calls


def test_save_runs_note_and_entry_and_resume_adds_new_required_words(tmp_path):
    services=FakeServices()
    with TestClient(create_app(tmp_path,testing=True,services=services)) as client:
        client.headers['X-WordLearner-Request']='1'
        first=client.post('/api/words',json={'word':'bed','created_on':today()}).json()
        assert wait_job(client,first['ai_job_id'])['status']=='completed'
        assert services.calls==[('note',first['id']),('entry',first['id'])]
        result=client.post('/api/games',json={'mode':'review'}).json()
        prepared=wait_job(client,result['job_id'])
        session_id=prepared['result']['id']
        second=client.post('/api/words',json={'word':'sun','created_on':today()}).json()
        assert 'id' in second,second
        result=client.post('/api/games',json={'mode':'review'}).json()
        resumed=wait_job(client,result['job_id'])
        assert resumed['result']['id']==session_id
        assert {w.id for w in client.app.state.game.words}=={first['id'],second['id']}
        assert {w.id for w in client.app.state.game.state.pending_round}=={first['id'],second['id']}


def test_unconfigured_ai_prevents_both_modes_before_policy_freeze(tmp_path):
    with TestClient(create_app(tmp_path,testing=True)) as client:
        client.headers['X-WordLearner-Request']='1'
        w=client.post('/api/words',json={'word':'bed','created_on':today()}).json()
        for mode in ('practice','review','debug'):
            assert client.post('/api/games',json={'mode':mode,'word_ids':[w['id']]}).status_code==400
        assert client.get('/api/bootstrap').json()['summary']['daily'] is None


def test_preheat_does_not_freeze_tomorrow_policy(store):
    w=word(store)
    job_id=store.enqueue_job('preheat',{'day':today()}); store.conn.commit()
    worker=JobWorker(store,FakeServices(),None)
    asyncio.run(worker.once())
    assert store.rows('SELECT status FROM background_jobs WHERE id=?',(job_id,))[0]['status']=='completed'
    assert store.daily((date.fromisoformat(today())+timedelta(days=1)).isoformat()) is None


def test_real_letter_mistakes_use_one_based_positions(store):
    session,clock=game(store,'practice')
    for _ in range(3): session.command({'seq':session.seq+1,'type':'key','char':'x'})
    records=store.mistakes(session.words[0].id)
    assert records[0]['position']==1
    assert records[0]['expected_char']=='c'
    assert records[0]['wrong_chars']==['x','x','x']
    assert store.report(today(),today())['daily'][0]['practiced']==1


def test_error_hint_without_mistakes_does_not_call_ai(store,tmp_path):
    w=word(store)
    result=asyncio.run(Integrations(tmp_path).ai(store,'error',w['id']))
    assert '暂无' in result['content']
    assert store.rows('SELECT * FROM ai_cache')==[]


def test_ai_cache_is_invalidated_only_for_edited_word(store,tmp_path):
    one,two=word(store),word(store,'bed')
    services=Integrations(tmp_path)
    calls=[]
    class Service:
        model='test'; base_url='https://example.invalid'
        def generate_word_note(self,entry):
            calls.append(entry.word)
            return SimpleNamespace(content=entry.word,error=None)
    services.service=lambda kind:Service()
    async def run():
        for w in (one,two,one): await services.ai(store,'note',w['id'])
        assert calls==['cat','bed']
        store.save_word({**one,'translation':'猫咪'},one['id'])
        await services.ai(store,'note',two['id'])
        await services.ai(store,'note',one['id'])
    asyncio.run(run())
    assert calls==['cat','bed','cat']


@pytest.mark.parametrize('round_count',[1,2,3])
def test_all_supported_round_policies_agree_in_report_and_mail(store,round_count):
    w=word(store)
    policy=normalize_review_policy({'round_count':round_count,'perfect_reward_money':7.25})
    store.prepare_daily(policy)
    clock=Clock(); session=GameSession(store,'test','review',[w],policy,clock)
    session.connect()
    for number in range(round_count):
        clock.advance(1.1); session.last_contact=clock(); session.tick()
        type_word(session,'cat')
        clock.advance(); session.tick()
    assert session.status=='completed'
    assert store.summary()['reward_money']==7.25
    assert store.report(today(),today())['reward_money']==7.25
    payload=json.loads(store.rows('SELECT payload FROM settlement_events')[0]['payload'])
    validate_payload(payload)
    assert len(payload['reward_rounds'])==round_count
    assert payload['reward_points']==400
    message=message_for(payload,{'account':'sender@example.invalid','recipient':'parent@example.invalid'},1)
    assert '¥7.25/¥7.25' in message.get_content()
    assert f'{round_count} 轮' in message.get_content()


def test_close_snapshots_are_immutable_and_window_scoped(store):
    session,clock=game(store)
    type_word(session,'cat')
    session.command({'seq':session.seq+1,'type':'close'})
    original=store.rows('SELECT payload FROM settlement_events')[0]['payload']
    session.disconnect(); session.connect()
    clock.advance(); session.tick()
    session.command({'seq':session.seq+1,'type':'close'})
    rows=store.rows('SELECT payload FROM settlement_events ORDER BY id')
    assert rows[0]['payload']==original
    assert json.loads(rows[0]['payload'])['practiced']==1
    assert json.loads(rows[1]['payload'])['practiced']==0
    assert json.loads(rows[0]['payload'])['reward_money']==0
    assert json.loads(rows[1]['payload'])['reward_money']==2


def test_mail_failed_auth_retries_without_mutating_payload(store):
    session,clock=game(store)
    session.command({'seq':1,'type':'close'}); store.conn.commit()
    calls=[]
    class SMTP:
        fail=True
        def __init__(self,*args,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def login(self,*args):
            if self.fail: raise OSError('private credentials never exposed')
        def send_message(self,message):calls.append(message)
    config={'enabled':True,'host':'example.invalid','port':465,'account':'sender@example.invalid','recipient':'parent@example.invalid','auth_code':'test-secret'}
    worker=MailWorker(store,config,SMTP)
    original=store.rows('SELECT payload FROM settlement_events')[0]['payload']
    asyncio.run(worker.once())
    row=store.rows('SELECT * FROM settlement_events')[0]
    assert row['status']=='failed' and row['attempts']==1
    assert 'private' not in row['last_error']
    assert row['payload']==original
    asyncio.run(worker.once()); assert len(calls)==0
    SMTP.fail=False
    # Clock-only retry scheduling fixture, never changes learning results.
    with store.conn: store.conn.execute('UPDATE settlement_events SET next_attempt=0')
    asyncio.run(worker.once()); asyncio.run(worker.once())
    assert len(calls)==1
    assert store.rows('SELECT status FROM settlement_events')[0]['status']=='sent'
    assert store.rows('SELECT payload FROM settlement_events')[0]['payload']==original


def test_uncertain_delivery_is_not_automatically_repeated(store):
    session,_=game(store); session.command({'seq':1,'type':'close'}); store.conn.commit()
    config={'enabled':True,'host':'test','port':465,'account':'a@example.invalid','recipient':'b@example.invalid','auth_code':'test'}
    worker=MailWorker(store,config)
    sent=[]
    def ambiguous(message): sent.append(message); return 'uncertain','发送结果待确认'
    worker.deliver=ambiguous
    asyncio.run(worker.once()); asyncio.run(worker.once())
    assert len(sent)==1
    assert store.rows('SELECT status FROM settlement_events')[0]['status']=='uncertain'


def test_incomplete_mail_payload_is_blocked():
    with pytest.raises(ValueError): validate_payload({'day':today()})


def test_presence_does_not_accept_client_duration(tmp_path):
    with TestClient(create_app(tmp_path,testing=True)) as client:
        client.headers['X-WordLearner-Request']='1'
        assert client.post('/api/presence',json={'seconds':99999}).status_code==422
        assert client.post('/api/presence',json={}).status_code==200
        assert client.get('/api/bootstrap').json()['summary']['usage_seconds']<2


def test_disabled_mail_is_visible_and_never_attempts_delivery(tmp_path):
    services=FakeServices()
    with TestClient(create_app(tmp_path,testing=True,services=services)) as client:
        client.headers['X-WordLearner-Request']='1'
        client.post('/api/words',json={'word':'bed','created_on':today()})
        job=client.post('/api/games',json={'mode':'review'}).json()
        session_id=wait_job(client,job['job_id'])['result']['id']
        with client.websocket_connect(f'/api/games/{session_id}/socket',headers={'Origin':'http://testserver'}) as socket:
            socket.receive_json()
            socket.send_json({'type':'close','seq':1})
            for _ in range(20):
                if socket.receive_json()['type']=='ack':break
        status=client.get('/api/bootstrap').json()['mail_status']
        assert status['enabled'] is False
        assert status['pending_count']==1
        assert status['latest']['status']=='pending' and status['latest']['attempts']==0
        assert client.post('/api/presence',json={}).json()['mail_status']==status


def test_selected_list_stays_stable_and_replaces_archived_old_word(store):
    old=(date.fromisoformat(today())-timedelta(days=20)).isoformat()
    for n in range(20):store.save_word({'word':f'old{n}','translation':'旧词','phonetic':'','created_on':old})
    initial=store.prepare_daily({'word_count':10})['word_ids']
    required=word(store,'new')
    changed=store.prepare_daily({'word_count':10})['word_ids']
    assert required['id'] in changed and len(changed)==10
    assert len(set(initial)&set(changed))==9
    store.archive([changed[0]])
    final=store.prepare_daily({'word_count':10})['word_ids']
    assert changed[0] not in final and len(final)==10
    assert store.prepare_daily({'word_count':10})['word_ids']==final


def test_practice_keeps_selected_speed_across_rounds(store):
    session,clock=game(store,'practice')
    speed=session.state.speed_multiplier
    type_word(session,'cat'); clock.advance(); session.tick()
    assert session.state.current_round==2
    assert session.state.speed_multiplier==speed


def test_time_limit_records_only_actual_partial_round_and_no_close_retry(store):
    session,clock=game(store,words=[word(store,'cat'),word(store,'sun')])
    type_word(session,session.state.active[0].answer)
    for _ in range(4):clock.advance(1);session.last_contact=clock();session.tick()
    assert len(session.state.active)==1
    # Actual hint actions extend the isolated simulation without creating scores.
    for _ in range(1):
        session.command({'seq':session.seq+1,'type':'hint_start','badge':1})
        session.command({'seq':session.seq+1,'type':'hint_end'})
    for _ in range(1801):
        if session.status=='completed':break
        for falling in list(session.state.active):
            if not falling.manual_hint_used:
                badge=session.state.active.index(falling)+1
                session.command({'seq':session.seq+1,'type':'hint_start','badge':badge})
                session.command({'seq':session.seq+1,'type':'hint_end'})
            type_word(session,falling.answer)
        clock.advance(1);session.last_contact=clock();session.tick()
    assert session.terminal_reason=='time_limit'
    assert store.daily()['elapsed']==1800
    assert store.daily()['retries']=={}
    assert store.summary()['total_score']==10
    assert store.summary()['reward_money']==1
    payload=json.loads(store.rows('SELECT payload FROM settlement_events')[0]['payload'])
    assert payload['completed_30_minutes'] and payload['reward_money']==1
    validate_payload(payload)


def test_report_has_thirty_day_curve_and_excludes_archived_mastery(store):
    w=word(store)
    report=store.report(today(),today())
    assert len(report['curve'])==31 and report['curve'][-1]['day']==30
    assert report['mastery_summary']=={'high':0,'medium':0,'low':0,'new':1,'due':1,'total':1}
    store.archive([w['id']])
    assert store.report(today(),today())['mastery_summary']['total']==0


def test_midnight_does_not_settle_an_explicitly_closed_window_twice(store,monkeypatch):
    import backend.game as module
    session,clock=game(store)
    session.command({'seq':1,'type':'close'}); session.disconnect()
    monkeypatch.setattr(module,'today',lambda:(date.fromisoformat(today())+timedelta(days=1)).isoformat())
    clock.advance(); session.tick()
    assert session.status=='completed'
    assert len(store.rows('SELECT * FROM settlement_events'))==1
