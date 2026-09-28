import json

import pytest
from fastapi.testclient import TestClient

from backend.clock import today
from backend.main import create_app
from backend.store import Store,encoded
from tests.fakes import FakeServices
from tests.test_server import game,type_word
from tests.test_workflow_parity import wait_job


def test_no_microphone_typing_creates_formal_scores_history_rewards_and_settlement():
    store=Store(':memory:')
    try:
        session,clock=game(store,'debug')
        type_word(session,'cat')
        assert session.state.score==10
        assert session.voice_required is False
        assert session.view()['reward_money']==2
        assert store.summary()['total_score']==10
        assert store.summary()['preview_reward_money']==2
        history=store.rows('SELECT * FROM review_history')
        assert len(history)==1 and history[0]['correct']==1 and history[0]['source']=='debug'
        assert store.get_word(session.words[0].id)['practice_count']==1
        clock.advance(); session.tick()
        assert store.best_rounds(today())[1]['reward_points']==200
        session.command({'seq':session.seq+1,'type':'close'})
        assert store.daily()['elapsed']>0
        assert len(store.rows('SELECT * FROM reward_attempts'))==1
        assert len(store.rows('SELECT * FROM settlement_events'))==1
    finally:store.conn.close()


def test_no_microphone_mistakes_are_formal_actual_records():
    store=Store(':memory:')
    try:
        session,_=game(store,'debug')
        for _ in range(3):
            session.command({'seq':session.seq+1,'type':'key','char':'x'})
        history=store.rows('SELECT * FROM review_history')
        assert len(history)==1 and history[0]['correct']==0 and history[0]['source']=='debug'
        assert len(store.rows('SELECT * FROM letter_mistakes'))==1
        assert store.summary()['total_score']==0
    finally:store.conn.close()


@pytest.mark.parametrize('mode',['review','debug'])
def test_daily_review_modes_continue_beyond_configured_rounds_with_capped_reward(mode):
    store=Store(':memory:')
    try:
        session,clock=game(store,mode)
        for completed_round in range(1,6):
            type_word(session,'cat')
            clock.advance(); session.tick()
            assert session.state.current_round==completed_round+1
            assert session.status=='running'
            clock.advance(1.1); session.tick()
        attempts=store.rows('SELECT round_number,result FROM reward_attempts ORDER BY id')
        assert [row['round_number'] for row in attempts]==[1,2,1,2,1]
        assert [json.loads(row['result'])['actual_round_number'] for row in attempts]==[1,2,3,4,5]
        assert store.summary()['total_score']==50
        assert len(store.rows('SELECT * FROM review_history'))==5
        assert sum(row['reward_points'] for row in store.best_rounds(today()).values())==400
        assert store.summary()['reward_money']==4
        assert session.view()['round_limit'] is None
    finally:store.conn.close()


def test_voice_review_can_start_after_reward_slots_are_full(tmp_path):
    store=Store(tmp_path/'wordlearner-web.sqlite3')
    saved=store.save_word({'word':'bed','translation':'床','phonetic':'[bed]','created_on':today()})
    store.prepare_daily({'word_count':75,'round_count':2,'perfect_reward_money':4})
    result=encoded({'correct_chars':3,'total_chars':3,'duration_seconds':1,'accuracy_percent':100,
                    'cpm':180,'speed_percent':100,'max_score':200,'reward_points':200})
    with store.conn:
        for slot in (1,2):
            store.conn.execute('''INSERT INTO reward_attempts(day,session_id,round_number,attempt_number,result,recorded_at)
                VALUES(?,?,?,?,?,?)''',(today(),'completed-earlier',slot,1,result,today()+'T10:00:00+08:00'))
    store.conn.close()

    with TestClient(create_app(tmp_path,testing=True,services=FakeServices())) as client:
        client.headers['X-WordLearner-Request']='1'
        response=client.post('/api/games',json={'mode':'review','word_ids':[saved['id']]})
        assert response.status_code==200,response.text
        prepared=wait_job(client,response.json()['job_id'])
        assert prepared['status']=='completed',prepared
        state=client.get('/api/games/'+prepared['result']['id']).json()
        assert state['mode']=='review' and state['round_limit'] is None


class NoAsrServices(FakeServices):
    def configured(self,kind):
        return kind=='ai'


def test_no_microphone_review_needs_ai_but_not_asr(tmp_path):
    with TestClient(create_app(tmp_path,testing=True,services=NoAsrServices())) as client:
        client.headers['X-WordLearner-Request']='1'
        from backend.clock import today
        client.post('/api/words',json={'word':'bed','created_on':today()})
        result=client.post('/api/games',json={'mode':'debug'})
        assert result.status_code==200,result.text
        prepared=wait_job(client,result.json()['job_id'])
        state=client.get('/api/games/'+prepared['result']['id']).json()
        assert state['mode']=='debug' and state['voice_required'] is False and state['total']==1
        assert client.get('/api/bootstrap').json()['summary']['daily'] is not None
        review=client.post('/api/games',json={'mode':'review'})
        assert review.status_code==400 and 'ASR' in review.text


def test_paused_daily_review_can_switch_to_no_microphone_without_new_session():
    store=Store(':memory:')
    try:
        session,_=game(store,'review')
        session.disconnect()
        row=store.rows('SELECT * FROM sessions WHERE id=?',(session.id,))[0]
        restored=type(session).restore(store,row)
        restored.set_mode('debug')
        restored.save()
        saved=store.rows('SELECT mode FROM sessions WHERE id=?',(session.id,))[0]
        assert saved['mode']=='debug'
        assert restored.review and not restored.voice_required
    finally:store.conn.close()


def test_api_switches_paused_voice_review_to_no_microphone_on_same_session(tmp_path):
    with TestClient(create_app(tmp_path,testing=True,services=FakeServices())) as client:
        client.headers['X-WordLearner-Request']='1'
        saved=client.post('/api/words',json={'word':'bed','created_on':today()}).json()
        wait_job(client,saved['ai_job_id'])
        voice_job=client.post('/api/games',json={'mode':'review'}).json()['job_id']
        session_id=wait_job(client,voice_job)['result']['id']
        keyboard_job=client.post('/api/games',json={'mode':'debug'}).json()['job_id']
        switched=wait_job(client,keyboard_job)['result']
        state=client.get('/api/games/'+session_id).json()
        assert switched['id']==session_id and switched['resumed'] is True
        assert state['mode']=='debug' and state['voice_required'] is False
    persisted=Store(tmp_path/'wordlearner-web.sqlite3')
    try:
        row=persisted.rows('SELECT mode FROM sessions WHERE id=?',(session_id,))[0]
        assert row['mode']=='debug'
    finally:persisted.conn.close()
