"""Server-owned game sessions; clients submit actions, never measured results."""
from __future__ import annotations

import json
import copy
import secrets
import time
from contextlib import contextmanager
from dataclasses import asdict

from .clock import today, timestamp
from .domain.game_state import GameState, HitEvent, MistakeEvent
from .domain.models import PracticeWord
from .domain.rules import calculate_reward_round
from .store import encoded

BASELINE = 500
PROFILES = {0.2:(3.0,70.0,.14),0.5:(2.0,70.0,.14),0.7:(1.0,70.0,.14)}


class GameSession:
    @contextmanager
    def atomic(self):
        """Roll back both SQLite and memory if processing or the commit fails."""
        before = copy.deepcopy({k:v for k,v in self.__dict__.items() if k not in ('store','clock')})
        try:
            with self.store.conn:
                yield
        except Exception:
            for key in list(self.__dict__):
                if key not in ('store','clock'):
                    del self.__dict__[key]
            self.__dict__.update(before)
            raise

    def __init__(self, store, owner, mode, words, policy, clock=time.monotonic):
        self.store, self.owner, self.mode, self.policy = store, owner, mode, policy
        self.clock = clock
        self.id, self.day = secrets.token_urlsafe(24), today()
        self.words = [PracticeWord(id=w['id'], prompt=w['translation'] or w.get('phonetic') or w['word'], answer=w['word']) for w in words]
        self.state = GameState(self.words,spawn_profiles=PROFILES,avoid_voice_conflicts=self.review)
        self.state.reset(clock())
        if not self.review: self.state.set_speed_multiplier(self.state.initial_speed_multiplier)
        self.seq = 0
        self.attempt = 1
        self.close_number = 0
        self.connected = False
        self.status = 'paused'
        self.elapsed = 0.0
        self.last_tick = clock()
        self.last_save = clock()
        self.last_contact = clock()
        self.message = ''
        self.pause_kind = None
        self.pause_at = 0.0
        self.voice_ticket = None
        self.voice_candidates = ()
        self.hint_runtime = None
        self.completed_word = None
        self.terminal_reason = None
        self.history_baseline = self.store.conn.execute('SELECT coalesce(max(id),0) FROM review_history').fetchone()[0]
        self.window_closed = False
        self.reset_stats()

    def reset_stats(self):
        self.correct = self.total = self.display_correct = self.display_total = 0
        self.round_elapsed = 0.0
        self.processed, self.credited, self.reservations = set(), set(), {}

    @property
    def review(self):
        return self.mode in ('review','debug')

    @property
    def voice_required(self):
        return self.mode == 'review'

    @property
    def unlimited_rounds(self):
        return self.review

    def reward_slot(self, number=None):
        """Map continued review rounds onto the configured reward slots and cap."""
        number = int(number or self.state.current_round)
        limit = max(int(self.policy['round_count']),1)
        return (number-1) % limit + 1 if self.unlimited_rounds else number

    def set_mode(self, mode):
        """Switch an unfinished daily review between microphone and keyboard input."""
        if mode == self.mode:
            return
        if self.mode not in ('review','debug') or mode not in ('review','debug'):
            raise ValueError('只能在语音复习和免麦克风复习之间切换')
        self.mode = mode
        self.state.avoid_voice_conflicts = True
        self.state.clear_voice_lock()

    def counts(self, word_id, runtime, free=False):
        if free or word_id in self.credited:
            return False
        if word_id not in self.reservations:
            self.reservations[word_id] = runtime
        return self.reservations[word_id] == runtime

    def sync_stats(self):
        self.display_correct, self.display_total = self.correct, self.total

    def progress(self):
        count = len(self.processed)
        if not count and self.display_total:
            count = self.state.round_processed_words
        return min(count / max(len(self.words),1),1.0)

    def cleanup(self):
        active = {w.runtime_id for w in self.state.active}
        self.reservations = {k:v for k,v in self.reservations.items() if v in active}

    def tick(self):
        now = self.clock()
        delta = min(max(now-self.last_tick,0),1.0)
        self.last_tick = now
        if self.status in ('completed','closed'):
            return
        if self.day != today():
            self.finish('day_changed')
            self.message = '日期已变更，旧日进度已保存，请重新进入今日复习'
            return
        if not self.connected or self.status != 'running':
            return
        if now-self.last_contact > 5:
            self.disconnect()
            self.message = '连接已中断，进度已保存'
            return
        if self.pause_kind:
            if now-self.pause_at >= (30 if self.pause_kind=='voice' else 15):
                self.end_pause()
                self.message = '暂停已超时，请重新操作'
                self.save()
            return
        if not self.state.game_active:
            return
        self.elapsed += delta
        self.round_elapsed += delta
        if self.review:
            daily = self.store.daily(self.day)
            elapsed = min(daily['elapsed']+delta,1800)
            self.store.conn.execute('UPDATE daily_review SET elapsed=? WHERE day=?',(elapsed,self.day))
            if elapsed >= 1800:
                self.record_round(self.state.current_round)
                self.finish('time_limit')
                self.message = '今日复习 30 分钟已结束，已保存实际结果'
                return
        before_round = self.state.current_round
        self.state.update(now,BASELINE)
        for missed in self.state.pop_missed_word_events():
            if self.counts(missed.word_id,missed.runtime_id,missed.manual_hint_used):
                self.total += max(len(missed.answer),1)
                self.processed.add(missed.word_id)
                self.reservations.pop(missed.word_id,None)
                self.store.answer(missed.word_id,False,self.id,self.mode)
                self.sync_stats()
        self.cleanup()
        if self.state.current_round > before_round:
            if self.review:
                self.record_round(before_round)
                if not self.unlimited_rounds and before_round >= self.policy['round_count']:
                    self.finish('rounds_completed')
                    self.message = '今日复习已完成，奖励已按实际记录结算'
                    return
            self.reset_stats()
            self.save()
        if now-self.last_save >= .5:
            self.save()

    def connect(self):
        if self.connected:
            raise ValueError('已有一个游戏页面连接，请先关闭另一个页面')
        self.connected = True
        if self.window_closed:
            self.history_baseline=self.store.conn.execute('SELECT coalesce(max(id),0) FROM review_history').fetchone()[0]
            self.window_closed=False
        if self.status not in ('completed','closed'):
            self.status = 'running'
        now = self.clock()
        self.last_tick = self.last_contact = now
        # Preserve remaining spawn delay rather than counting disconnected wall time.
        delay = getattr(self,'remaining_spawn',max(self.state.next_spawn_at-now,0))
        self.state.next_spawn_at = now + delay
        self.state._last_update = now
        self.save()

    def disconnect(self):
        now = self.clock()
        self.end_pause()
        if self.connected:
            self.remaining_spawn = max(self.state.next_spawn_at-now,0)
        self.connected = False
        if self.status not in ('completed','closed'):
            self.status = 'paused'
        self.save()

    def end_pause(self):
        now = self.clock()
        if self.pause_kind:
            self.state.next_spawn_at += max(now-self.pause_at,0)
        self.state._last_update = now
        self.last_tick = now
        self.pause_kind, self.voice_ticket, self.hint_runtime = None, None, None

    def input_char(self, char):
        if self.pause_kind or not self.state.game_active:
            return None
        if self.voice_required and self.state.voice_locked_word(BASELINE) is None:
            self.message = '请先按住空格读出单词，语音锁定后再输入'
            return None
        hit_runtime_id = None
        for event in self.state.handle_text(char,baseline_y=BASELINE):
            if isinstance(event,HitEvent):
                hit_runtime_id = event.runtime_id
            counts = self.counts(event.word_id,event.runtime_id,event.free_hint)
            if isinstance(event,MistakeEvent):
                if counts:
                    self.total += 1
                    self.store.answer(event.word_id,False,self.id,self.mode)
                    self.store.conn.execute('''INSERT INTO letter_mistakes(word_id,practiced_at,position,expected_char,wrong_chars,answer_snapshot,session_id)
                        VALUES(?,?,?,?,?,?,?)''',(event.word_id,timestamp(),event.position,event.expected_char,encoded(event.wrong_chars),event.answer,self.id))
                self.message = f'提示：第 {event.position+1} 位应输入 {event.expected_char}，该词会再次出现'
                continue
            if counts:
                self.correct += 1
                self.total += 1
            if event.completed:
                credited = counts and event.word_id not in self.credited
                if credited:
                    self.credited.add(event.word_id)
                    self.processed.add(event.word_id)
                    self.reservations.pop(event.word_id,None)
                    self.sync_stats()
                if (self.review and credited) or (not self.review and not event.manual_hint_used):
                    self.store.answer(event.word_id,True,self.id,self.mode)
                    self.store.conn.execute('INSERT INTO score_events(session_id,points,occurred_at) VALUES(?,?,?)',(self.id,event.score_delta,timestamp()))
                self.completed_word = {'word':event.answer,'runtime_id':event.runtime_id}
                self.message = f'完成：{event.answer}'
        self.cleanup()
        return hit_runtime_id

    def command(self, event):
        seq, kind = event['seq'], event['type']
        result = {}
        self.last_contact = self.clock()
        if seq <= self.seq:
            return result  # Idempotent replay must not repeat a visual hit effect.
        if seq != self.seq+1:
            raise ValueError('操作序号不连续，请重新连接恢复进度')
        if self.status != 'running':
            raise ValueError('当前游戏未运行')
        if self.pause_kind and kind not in ('voice_cancel','hint_end','close'):
            raise ValueError('请先结束录音或提示')
        if kind=='key':
            hit_runtime_id = self.input_char(event['char'])
            if hit_runtime_id:
                result['hit_runtime_id'] = hit_runtime_id
        elif kind=='speed':
            self.state.set_speed_multiplier(event['value'])
        elif kind in ('retry','restart'):
            self.end_pause()
            if self.review:
                daily = self.store.daily(self.day)
                if daily['elapsed']>=1800:
                    raise ValueError('今日复习时间已用完')
                if kind=='restart':
                    if daily['restarts']>=2:
                        raise ValueError('今天重新开始次数已用完（2/2）')
                    self.store.conn.execute('UPDATE daily_review SET restarts=restarts+1 WHERE day=?',(self.day,))
                else:
                    self.consume_retry()
            self.attempt += 1
            if kind=='restart':
                speed = self.state.speed_multiplier
                self.state.reset(self.clock())
                self.state.set_speed_multiplier(speed)
            else:
                self.state.retry_current_round(self.clock())
            self.reset_stats()
            self.message = '已重新开始，已记录的各轮最高奖励仍保留'
        elif kind=='hint_start':
            if not self.review or not self.state.game_active:
                raise ValueError('当前不能使用编号提示')
            target = self.state.active_word_by_badge(event['badge'],BASELINE)
            if target:
                self.state.add_repeats_for_runtime_id(target.runtime_id,3)
                self.pause_kind, self.pause_at = 'hint', self.clock()
                self.hint_runtime = target.runtime_id
        elif kind=='hint_end':
            if self.pause_kind=='hint':
                self.end_pause()
        elif kind=='voice_start':
            if not self.review or not self.state.game_active:
                raise ValueError('当前不能开始语音锁定')
            self.voice_candidates = self.state.capture_voice_candidates(BASELINE) if self.voice_required else ()
            if self.voice_required and not self.voice_candidates:
                raise ValueError('请等待单词出现')
            self.pause_kind, self.pause_at = 'voice', self.clock()
            self.voice_ticket = secrets.token_urlsafe(24)
        elif kind=='voice_cancel':
            if self.pause_kind=='voice':
                self.end_pause()
        elif kind=='close':
            self.end_pause()
            if self.review:
                self.consume_retry(strict=False)
            self.close_number += 1
            self.window_closed = True
            self.status = 'paused' if self.review else 'closed'
            self.save()
            self.settlement('user_closed')
            if self.review: self.store.enqueue_job('preheat',{'day':today(),'trigger':'closed'})
        if kind in ('retry','restart','close','speed'):
            self.store.log_operation('game_'+kind,'游戏操作：'+kind,session_id=self.id,round=self.state.current_round)
        self.seq = seq
        self.store.conn.execute('INSERT INTO input_events(session_id,seq,kind,payload,occurred_at) VALUES(?,?,?,?,?)',
                                (self.id,seq,kind,encoded(event),timestamp()))
        self.save()
        return result

    def consume_retry(self, strict=True):
        daily = self.store.daily(self.day)
        key = str(self.state.current_round)
        count, limit = daily['retries'].get(key,0), {1:3,2:2,3:1}.get(self.reward_slot(),0)
        if count>=limit:
            if strict:
                raise ValueError(f'第 {key} 轮重试次数已用完（{count}/{limit}）')
            return
        daily['retries'][key] = count+1
        self.store.conn.execute('UPDATE daily_review SET retries=? WHERE day=?',(encoded(daily['retries']),self.day))

    def apply_voice(self, ticket, transcript):
        if self.pause_kind!='voice' or ticket!=self.voice_ticket or self.clock()-self.pause_at>=30:
            raise ValueError('这次录音已失效，请重新录音')
        target = self.state.lock_voice_target(transcript,BASELINE,self.voice_candidates)
        self.message = f'已锁定：{target.prompt}' if target else self.state.voice_match_message
        self.store.log_operation('voice_matched',self.message,session_id=self.id,matched=bool(target))
        self.end_pause()
        self.save()

    def finish_voice_diagnostic(self, ticket, transcript):
        if self.pause_kind!='voice' or ticket!=self.voice_ticket or self.clock()-self.pause_at>=30:
            raise ValueError('这次录音已失效，请重新录音')
        self.message = '麦克风采音正常；免麦克风模式不会锁定单词' if transcript.strip() else '没有识别到有效读音，请检查麦克风后重试'
        self.end_pause()
        self.save()

    def record_round(self, number):
        slot = self.reward_slot(number)
        if not self.review or not 1<=slot<=self.policy['round_count'] or not self.display_total:
            return
        result = calculate_reward_round(self.display_correct,self.display_total,self.round_elapsed,
            self.policy['round_max_scores'][slot-1],self.progress())
        result['actual_round_number'] = number
        cycle = (number-1)//max(int(self.policy['round_count']),1)
        attempt_number = cycle*1_000_000+self.attempt
        self.store.conn.execute('''INSERT OR IGNORE INTO reward_attempts(day,session_id,round_number,attempt_number,result,recorded_at)
            VALUES(?,?,?,?,?,?)''',(self.day,self.id,slot,attempt_number,encoded(result),timestamp()))

    def settlement(self, trigger):
        if not self.review:
            return
        payload = self.store.settlement_payload(self.day,self.id,self.history_baseline,trigger)
        self.store.conn.execute('''INSERT OR IGNORE INTO settlement_events(session_id,close_number,day,payload,created_at)
            VALUES(?,?,?,?,?)''',(self.id,self.close_number,self.day,encoded(payload),timestamp()))

    def finish(self, trigger):
        already_closed=self.window_closed
        self.end_pause()
        self.status = 'completed'
        self.terminal_reason = trigger
        self.state.game_active = False
        if not already_closed: self.close_number += 1
        self.window_closed=True
        self.save()
        if not already_closed: self.settlement(trigger)
        self.store.log_operation('game_completed','游戏已结束',session_id=self.id,trigger=trigger)

    def preview_points(self):
        number = self.state.current_round
        slot = self.reward_slot(number)
        if self.status=='completed' and self.terminal_reason!='day_changed':
            return 0.0
        if not self.review or not 1<=slot<=self.policy['round_count'] or not self.display_total:
            return 0.0
        return self.policy['round_max_scores'][slot-1] * self.progress() * self.display_correct/self.display_total

    def save(self):
        now = self.clock()
        snapshot = self.state.snapshot(now)
        if not self.connected and hasattr(self,'remaining_spawn'):
            snapshot['next_spawn_delay'] = self.remaining_spawn
        payload = {'words':[asdict(w) for w in self.words],'policy':self.policy,'state':snapshot,
            'seq':self.seq,'attempt':self.attempt,'close_number':self.close_number,'elapsed':self.elapsed,
            'correct':self.correct,'total':self.total,'display_correct':self.display_correct,'display_total':self.display_total,
            'round_elapsed':self.round_elapsed,'processed':list(self.processed),'credited':list(self.credited),
            'reservations':self.reservations,'preview_points':self.preview_points(),'reward_slot':self.reward_slot(),
            'terminal_reason':self.terminal_reason,
            'history_baseline':self.history_baseline,'window_closed':self.window_closed,
            'random_state':self.state.random.getstate()}
        self.store.conn.execute('''INSERT INTO sessions VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            owner=excluded.owner,mode=excluded.mode,status=excluded.status,payload=excluded.payload,updated_at=excluded.updated_at''',
            (self.id,self.owner,self.mode,self.day,self.status,encoded(payload),timestamp()))
        self.last_save = now

    @classmethod
    def restore(cls, store, row, clock=time.monotonic):
        p = json.loads(row['payload'])
        words = [{'id':w['id'],'word':w['answer'],'translation':w['prompt']} for w in p['words']]
        instance = cls(store,row['owner'],row['mode'],words,p['policy'],clock)
        instance.id, instance.day = row['id'],row['day']
        instance.status = 'paused'
        instance.state.load_snapshot(p['state'],clock())
        def tuples(x):
            return tuple(tuples(v) for v in x) if isinstance(x,list) else x
        instance.state.random.setstate(tuples(p['random_state']))
        for name in ('seq','attempt','close_number','elapsed','correct','total','display_correct','display_total','round_elapsed'):
            setattr(instance,name,p[name])
        instance.processed,instance.credited = set(p['processed']),set(p['credited'])
        instance.reservations = {int(k):v for k,v in p['reservations'].items()}
        instance.history_baseline=p.get('history_baseline',0)
        instance.window_closed=p.get('window_closed',False)
        return instance

    def refresh_words(self, words):
        snapshot=self.state.snapshot(self.clock())
        if hasattr(self,'remaining_spawn'):
            snapshot['next_spawn_delay']=self.remaining_spawn
        rng=self.state.random.getstate()
        self.words=[PracticeWord(id=w['id'],prompt=w['translation'] or w['phonetic'] or w['word'],answer=w['word']) for w in words]
        state=GameState(self.words,spawn_profiles=PROFILES,avoid_voice_conflicts=self.review)
        state.load_snapshot(snapshot,self.clock())
        state.random.setstate(rng)
        self.state=state
        self.save()

    def view(self):
        rounds = self.store.best_rounds(self.day) if self.review else {}
        saved = sum(r['reward_points'] for r in rounds.values())
        improvement = max(self.preview_points()-rounds.get(self.reward_slot(),{}).get('reward_points',0),0)
        daily = self.store.daily(self.day) if self.review else None
        return {'id':self.id,'mode':self.mode,'voice_required':self.voice_required,'status':self.status,'day':self.day,'last_seq':self.seq,
            'round':self.state.current_round,'round_limit':None if self.unlimited_rounds else self.policy['round_count'] if self.review else None,
            'lives':self.state.lives,'score':self.state.score,'speed':self.state.speed_multiplier,
            'processed':self.state.round_processed_words,'total':self.state.round_total_words,
            'accuracy':round(self.display_correct/self.display_total*100,2) if self.display_total else 0,
            'elapsed_seconds':round(daily['elapsed'] if daily else self.elapsed,1),
            'reward_money':self.store.money(self.day,saved+improvement) if self.review else 0,
            'saved_reward_money':self.store.money(self.day,saved) if self.review else 0,
            'perfect_reward_money':self.policy['perfect_reward_money'],'pause':self.pause_kind,
            'voice_ticket':self.voice_ticket,'locked':self.state.voice_locked_runtime_id,
            'message':self.message,'game_active':self.state.game_active,'completed_word':self.completed_word,
            'restarts':daily['restarts'] if daily else 0,'retries':daily['retries'] if daily else {},
            'total_score':self.store.conn.execute('SELECT coalesce(sum(points),0) FROM score_events').fetchone()[0],
            'remaining_seconds':max(1800-int(daily['elapsed']),0) if daily else None,
            'hint_runtime':self.hint_runtime,'spawn_interval':self.state.spawn_interval,
            'active':[{'runtime_id':w.runtime_id,'word_id':w.word_id,'prompt':w.prompt,
                'display':w.answer if w.runtime_id==self.hint_runtime else w.masked_answer,
                'x':w.x_ratio,'y':w.y,'progress':w.progress,'hinted':w.manual_hint_used} for w in self.state.active]}
