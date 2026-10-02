from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .clock import timestamp,today
from .domain.parent_settings import ParentSettingsManager
from .game import GameSession
from .integrations import Integrations
from .jobs import JobWorker
from .mail import MailWorker
from .review_export import daily_words_workbook
from .schemas import AIInput, Command, PasswordInput, PolicyInput, StartGame, WordIds, WordInput, StrictModel
from .store import Store,encoded
from .version import VERSION,build_id

ROOT = Path(__file__).resolve().parents[1]
COOKIE = 'wordlearner_bs_browser'
logger = logging.getLogger('wordlearner_bs')


def prune_database_backups(folder, keep=1):
    """Keep only the newest complete SQLite snapshots, regardless of their source."""
    snapshots=[]
    for path in folder.glob('*.sqlite3'):
        try:
            snapshots.append((path.stat().st_mtime_ns,path.name,path))
        except FileNotFoundError:
            pass
    for _,_,old in sorted(snapshots,reverse=True)[max(int(keep),0):]:
        for attempt in range(5):
            try:
                old.unlink(missing_ok=True)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(.05)


def environment_flag(name):
    return os.environ.get(name,'').strip().casefold() in ('1','true','yes','on')


def page_token(value):
    value = str(value or '')
    if 16 <= len(value) <= 64 and all(c in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in value):
        return value
    return None


def configured_hosts(testing=False):
    hosts = ['127.0.0.1','localhost','[::1]']
    hosts.extend(host.strip() for host in os.environ.get('WORDLEARNER_ALLOWED_HOSTS','').split(',') if host.strip())
    if testing:
        hosts.append('testserver')
    return list(dict.fromkeys(hosts))


@contextlib.contextmanager
def process_lock(directory):
    """Only one process may own in-memory gameplay and its SQLite store."""
    directory.mkdir(parents=True,exist_ok=True)
    with (directory/'server.lock').open('a+b') as handle:
        handle.seek(0)
        handle.write(b'0')
        handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('这个 B/S 数据目录已有服务运行，请使用现有服务') from None
        try:
            yield
        finally:
            if os.name=='nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(handle,fcntl.LOCK_UN)


def create_app(data_dir=None, testing=False, services=None):
    directory = Path(data_dir or os.environ.get('WORDLEARNER_BS_DATA_DIR',ROOT/'data')).resolve()
    public_mode = environment_flag('WORDLEARNER_PUBLIC_MODE')
    secure_cookie = environment_flag('WORDLEARNER_COOKIE_SECURE')
    if not testing and not directory.is_relative_to(ROOT):
        raise RuntimeError('B/S 数据目录必须位于 bs-web 内，禁止访问桌面版数据库')

    def retire_other_sessions(store, keep_id, reason):
        """Keep one resumable game and close stale snapshots without changing study results."""
        rows=store.rows("SELECT id,payload FROM sessions WHERE status IN ('running','paused') AND id<>?",(keep_id,))
        retired=[]
        for row in rows:
            payload=json.loads(row['payload'])
            payload['terminal_reason']=reason
            if isinstance(payload.get('state'),dict):
                payload['state']['game_active']=False
            store.conn.execute('UPDATE sessions SET status=?,payload=?,updated_at=? WHERE id=?',
                               ('closed',encoded(payload),timestamp(),row['id']))
            retired.append(row['id'])
        if retired:
            store.log_operation('game_sessions_superseded','当前页面已关闭其他未完成游戏',
                                keep_session_id=keep_id,closed_session_ids=retired,reason=reason)
        return retired

    def claim_prepare_jobs(store, owner):
        claimed=[]
        for row in store.rows("SELECT id,payload FROM background_jobs WHERE kind='prepare_game' AND status IN ('pending','running')"):
            payload=json.loads(row['payload'])
            if payload.get('owner')==owner:
                continue
            payload['owner']=owner
            store.conn.execute('UPDATE background_jobs SET payload=?,updated_at=? WHERE id=?',
                               (encoded(payload),timestamp(),row['id']))
            claimed.append(row['id'])
        if claimed:
            store.log_operation('game_preparation_transferred','练习准备任务已由当前页面接管',job_ids=claimed)
        return claimed

    @asynccontextmanager
    async def lifespan(app):
        with process_lock(directory):
            store = Store(directory/'wordlearner-web.sqlite3')
            app.state.store = store
            app.state.services = services or Integrations(directory if testing else ROOT)
            app.state.game = None
            app.state.game_socket = None
            app.state.game_lease = None
            app.state.password_failures = []
            app.state.mail = MailWorker(store,app.state.services.config.get('email',{}))
            app.state.jobs = JobWorker(store,app.state.services,prepared_game)
            def backup():
                folder=directory/'backups'
                store.backup(folder/f'web-{time.time_ns()}.sqlite3')
                prune_database_backups(folder,keep=1)
            backup()
            rows = store.rows("SELECT * FROM sessions WHERE status IN ('running','paused') ORDER BY updated_at DESC,id DESC LIMIT 1")
            if rows:
                app.state.game = GameSession.restore(store,rows[0])
                with store.conn:
                    retire_other_sessions(store,app.state.game.id,'server_kept_latest_session')
            async def ticker():
                last_backup=time.monotonic()
                while True:
                    await asyncio.sleep(1/30)
                    now=time.monotonic()
                    if now-last_backup>=300:
                        try: backup()
                        except Exception: logger.exception('Scheduled backup failed')
                        last_backup=now
                    game = app.state.game
                    if game:
                        try:
                            with game.atomic():
                                game.tick()
                        except Exception:
                            logger.exception('Game tick failed; suspending session')
                            game.connected = False
                            game.status = 'paused'
                            game.message = '保存异常，游戏已暂停，请重启服务检查数据'
            tasks = [asyncio.create_task(ticker()),asyncio.create_task(app.state.jobs.run()),asyncio.create_task(app.state.mail.run())]
            try:
                yield
            finally:
                for task in tasks: task.cancel()
                for task in tasks:
                    with contextlib.suppress(asyncio.CancelledError): await task
                with store.conn:
                    if app.state.game:
                        app.state.game.disconnect()
                backup()
                with contextlib.suppress(Exception): app.state.services.close()
                store.conn.close()

    app = FastAPI(title='WordLearner B/S',version=VERSION,lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=configured_hosts(testing))

    @app.middleware('http')
    async def protect(request, call_next):
        if not testing and not public_mode and request.client and request.client.host not in ('127.0.0.1','::1'):
            return JSONResponse({'detail':'当前版本仅开放本机访问'},status_code=403)
        if request.method not in ('GET','HEAD','OPTIONS'):
            origin = request.headers.get('origin')
            if origin and urlparse(origin).netloc != request.headers.get('host'):
                return JSONResponse({'detail':'请求来源不匹配'},status_code=403)
            if request.headers.get('x-wordlearner-request')!='1':
                return JSONResponse({'detail':'缺少请求校验头'},status_code=403)
        cookie = request.cookies.get(COOKIE)
        if not cookie or len(cookie)!=43 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in cookie):
            cookie = secrets.token_urlsafe(32)
        request.state.owner = cookie
        page = page_token(request.headers.get('x-wordlearner-page'))
        request.state.page_explicit = page is not None
        if page:
            request.state.owner = page
        response = await call_next(request)
        if cookie!=request.cookies.get(COOKIE):
            response.set_cookie(COOKIE,cookie,httponly=True,samesite='strict',max_age=365*86400,
                secure=secure_cookie or request.url.scheme=='https')
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(ValueError)
    async def validation_error(request, exc):
        return JSONResponse({'detail':str(exc)},status_code=400)

    def store():
        return app.state.store

    def mail_status():
        latest=store().rows('SELECT id,day,status,attempts,last_error,sent_at FROM settlement_events ORDER BY id DESC LIMIT 1')
        pending=store().conn.execute("SELECT count(*) FROM settlement_events WHERE status IN ('pending','failed')").fetchone()[0]
        return {'enabled':app.state.mail.enabled,'pending_count':pending,'latest':latest[0] if latest else None}

    def active_game_summary(game, owned):
        result={'id':game.id,'mode':game.mode,'owned':owned}
        if game.review:
            result['review_scope']=game.selection_scope
        return result

    def game_for(game_id, owner):
        game = app.state.game
        if not game or game.id!=game_id or game.owner!=owner:
            raise HTTPException(404,'游戏会话不存在或不属于当前浏览器')
        return game

    async def transfer_game(game, owner):
        """Move an unfinished game to the latest page without changing results."""
        old_socket = app.state.game_socket
        changed = game.owner != owner or game.connected or old_socket is not None
        if changed:
            with game.atomic():
                if game.connected:
                    game.disconnect()
                game.owner = owner
                game.message = '游戏已由当前页面接管，其他页面已自动退出'
                game.save()
                store().log_operation('game_transferred','未完成游戏已由当前页面接管；学习结果未改变',session_id=game.id)
        app.state.game_socket = None
        app.state.game_lease = None
        if old_socket is not None:
            with contextlib.suppress(RuntimeError,WebSocketDisconnect):
                await old_socket.close(code=4001,reason='游戏已由另一页面接管')
        return changed

    def require_idle():
        game = app.state.game
        if game and game.connected and game.status=='running':
            raise ValueError('请先返回词库再编辑词条')
        if store().rows("SELECT id FROM background_jobs WHERE kind='prepare_game' AND status IN ('pending','running')"):
            raise ValueError('正在准备练习，请等待完成后再编辑')

    def prepared_game(payload):
        if payload['day']!=today(): raise ValueError('日期已变更，请重新开始')
        words=[store().get_word(i) for i in payload['word_ids']]
        if any(w['archived'] for w in words): raise ValueError('词条已归档，请重新选择')
        existing=app.state.game
        if existing and existing.connected: raise ValueError('已有游戏正在连接')
        with store().conn:
            if payload.get('resume_id'):
                rows=store().rows("SELECT * FROM sessions WHERE id=? AND status='paused'",(payload['resume_id'],))
                if not rows: raise ValueError('待恢复的游戏已结束')
                game=GameSession.restore(store(),rows[0])
                game.set_mode(payload['mode'])
                game.refresh_words(words)
            else:
                game=GameSession(store(),payload['owner'],payload['mode'],words,payload['policy'],
                                 selection_scope=payload.get('review_scope','all'))
                game.save()
            retire_other_sessions(store(),game.id,'new_session_selected')
            store().log_operation('no_microphone_review_prepared' if game.mode=='debug' else 'game_prepared',
                '免麦克风复习已准备：实际打字计入正式记录和奖励' if game.mode=='debug' else 'AI 学习卡检查完成，可以进入游戏',
                session_id=game.id,word_count=len(words),review_scope=game.selection_scope)
            if game.review: store().enqueue_job('preheat',{'day':today()})
        app.state.game=game
        return {'id':game.id,'resumed':bool(payload.get('resume_id'))}

    def dates(start, end):
        start = str(start or (date.fromisoformat(today())-timedelta(days=30)))
        end = str(end or today())
        if date.fromisoformat(start)>date.fromisoformat(end):
            raise ValueError('开始日期不能晚于结束日期')
        return start,end

    @app.get('/api/health')
    async def health():
        scope = 'single-learner' if public_mode else 'local-single-learner'
        return {'status':'ok','version':VERSION,'build_id':build_id(),'scope':scope}

    @app.get('/api/bootstrap')
    async def bootstrap(request: Request):
        game = app.state.game
        resumable = game and game.status in ('running','paused')
        return {'summary':store().summary(),'capabilities':{**app.state.services.capabilities(),'email':app.state.mail.enabled},'mail_status':mail_status(),
            'debug_default':bool(app.state.services.config.get('debug_mode',False)),
            'review_scopes':store().review_scopes(),
            'policy':ParentSettingsManager(store()).current_policy(),
            'has_parent_password':ParentSettingsManager(store()).has_password(),
            'active_game':active_game_summary(game,game.owner==request.state.owner) if resumable else None}

    @app.post('/api/session/claim')
    async def claim_session(request: Request):
        if not request.state.page_explicit:
            raise HTTPException(400,'缺少页面标识')
        game = app.state.game
        resumable = game and game.status in ('running','paused')
        if not resumable:
            rows=store().rows("SELECT * FROM sessions WHERE status IN ('running','paused') ORDER BY updated_at DESC,id DESC LIMIT 1")
            if rows:
                game=GameSession.restore(store(),rows[0])
                app.state.game=game
                resumable=True
        transferred = await transfer_game(game,request.state.owner) if resumable else False
        with store().conn:
            if resumable:
                retire_other_sessions(store(),game.id,'current_page_claimed_session')
            claim_prepare_jobs(store(),request.state.owner)
        return {'transferred':transferred,'active_game':active_game_summary(game,True) if resumable else None}

    @app.post('/api/presence')
    async def presence(body: StrictModel):
        # Presence refreshes status only. Review duration is measured by the authoritative game tick.
        review_usage = store().review_usage_seconds()
        return {'review_usage_seconds':review_usage,'usage_seconds':review_usage,'mail_status':mail_status()}

    @app.get('/api/jobs/{job_id}')
    async def get_job(job_id: int,request: Request):
        rows=store().rows('SELECT * FROM background_jobs WHERE id=?',(job_id,))
        if not rows: raise HTTPException(404,'任务不存在')
        job=rows[0]; payload=json.loads(job.pop('payload'))
        if payload.get('owner',request.state.owner)!=request.state.owner: raise HTTPException(404,'任务不存在')
        job['result']=json.loads(job['result'])
        return job

    @app.get('/api/words')
    async def words(search: str='',start: date|None=None,end: date|None=None):
        if start and end and start>end:
            raise ValueError('开始日期不能晚于结束日期')
        return store().words(search[:200],str(start or ''),str(end or ''))

    @app.post('/api/words')
    async def add_word(body: WordInput):
        require_idle()
        with store().conn:
            word=store().save_word(body.model_dump(mode='json'))
            return {**word,'ai_job_id':store().enqueue_job('saved_word',{'word_ids':[word['id']]})}

    @app.get('/api/words/{word_id}')
    async def get_word(word_id: int):
        return store().get_word(word_id)

    @app.put('/api/words/{word_id}')
    async def edit_word(word_id: int,body: WordInput):
        require_idle()
        with store().conn:
            word=store().save_word(body.model_dump(mode='json'),word_id)
            return {**word,'ai_job_id':store().enqueue_job('saved_word',{'word_ids':[word['id']]})}

    @app.post('/api/words/archive')
    async def archive(body: WordIds):
        require_idle()
        with store().conn:
            store().archive(list(set(body.ids)))
        return {'ok':True}

    @app.get('/api/lookup')
    async def lookup(word: str):
        if not word.strip() or len(word)>120:
            raise ValueError('请输入有效单词')
        return await app.state.services.lookup(word.strip())

    @app.post('/api/parent/password')
    async def initialize_password(body: PasswordInput):
        with store().conn:
            ParentSettingsManager(store()).initialize_password(body.password,body.confirmation)
        return {'ok':True}

    @app.put('/api/parent/policy')
    async def policy(body: PolicyInput):
        attempts = [t for t in app.state.password_failures if time.monotonic()-t<60]
        app.state.password_failures = attempts
        if len(attempts)>=5:
            raise HTTPException(429,'密码尝试过多，请一分钟后重试')
        manager = ParentSettingsManager(store())
        if not manager.verify_password(body.password):
            attempts.append(time.monotonic())
            raise ValueError('家长密码错误或尚未初始化')
        with store().conn:
            result = manager.save_policy(body.password,body.model_dump(exclude={'password'}))
        return result

    @app.get('/api/reports')
    async def reports(start: date|None=None,end: date|None=None):
        return store().report(*dates(start,end))

    @app.get('/api/logs')
    async def logs(search: str='',start: date|None=None,end: date|None=None,event_type: str=''):
        start,end=dates(start,end)
        return store().rows('''SELECT * FROM operation_logs WHERE (instr(summary,?)>0 OR instr(event_type,?)>0)
            AND substr(occurred_at,1,10) BETWEEN ? AND ? AND (?='' OR event_type=?)
            ORDER BY id DESC LIMIT 300''',(search[:200],search[:200],start,end,event_type[:100],event_type[:100]))

    @app.get('/api/settlements')
    async def settlements():
        rows = store().rows('SELECT * FROM settlement_events ORDER BY id DESC LIMIT 100')
        return [{**r,'payload':json.loads(r['payload'])} for r in rows]

    @app.post('/api/ai')
    async def ai(body: AIInput):
        start,end = dates(body.start,body.end)
        return await app.state.services.ai(store(),body.task,body.word_id,body.question,start,end,body.draft.model_dump(mode='json') if body.draft else None)

    @app.post('/api/games')
    async def start_game(request: Request,body: StartGame):
        pending=store().rows("SELECT id,payload FROM background_jobs WHERE kind='prepare_game' AND status IN ('pending','running') LIMIT 1")
        if pending:
            with store().conn:
                claim_prepare_jobs(store(),request.state.owner)
            return {'job_id':pending[0]['id'],'preparing':True}
        if not app.state.services.configured('ai'):
            raise ValueError('进入练习前需要完成 AI 学习卡检查，请配置 AI 服务后重试')
        existing = app.state.game
        resume_id=None
        daily_mode = body.mode in ('review','debug')
        requested_scope = body.review_scope if daily_mode else 'selected'
        if existing and existing.status in ('paused','running') and existing.day==today():
            if existing.owner!=request.state.owner:
                await transfer_game(existing,request.state.owner)
            if existing.connected:
                raise HTTPException(409,'游戏页面已经打开，请返回已有页面')
            same_scope=existing.selection_scope==requested_scope
            if same_scope and (existing.mode==body.mode or (daily_mode and existing.mode in ('review','debug'))):
                resume_id=existing.id
        if not resume_id:
            if daily_mode:
                candidates=store().rows("SELECT * FROM sessions WHERE status='paused' AND mode IN ('review','debug') AND day=? ORDER BY updated_at DESC",(today(),))
                rows=[]
                for candidate in candidates:
                    saved=json.loads(candidate['payload'])
                    if saved.get('selection_scope','all')==requested_scope:
                        rows=[candidate]
                        break
            else:
                rows=store().rows("SELECT * FROM sessions WHERE status='paused' AND mode=? AND day=? ORDER BY updated_at DESC LIMIT 1",(body.mode,today()))
            if rows:
                if rows[0]['owner']!=request.state.owner:
                    with store().conn:
                        store().conn.execute('UPDATE sessions SET owner=?,updated_at=? WHERE id=?',
                                             (request.state.owner,timestamp(),rows[0]['id']))
                        store().log_operation('game_transferred','未完成游戏已由当前页面接管；学习结果未改变',
                                              session_id=rows[0]['id'])
                resume_id=rows[0]['id']
        with store().conn:
            if existing and existing.status in ('paused','running') and existing.day!=today():
                existing.finish('day_changed')
            manager = ParentSettingsManager(store())
            policy = manager.current_policy()
            if daily_mode:
                if body.mode=='review' and not app.state.services.configured('asr'):
                    raise ValueError('一键复习需要语音锁定，请先配置 bs-web/config.json 的 ASR 服务；目前可以使用普通练习')
                daily = store().prepare_daily(policy,requested_scope)
                policy = daily['policy']
                if daily['elapsed']>=1800:
                    raise ValueError('今日一键复习 30 分钟已用完')
                ids = daily['word_ids']
            else:
                ids = list(dict.fromkeys(body.word_ids))
                if resume_id:
                    snapshot=json.loads(store().rows('SELECT payload FROM sessions WHERE id=?',(resume_id,))[0]['payload'])
                    ids=[w['id'] for w in snapshot['words'] if not store().get_word(w['id'])['archived']]
            if not ids:
                raise ValueError('请先勾选单词')
            words = [store().get_word(i) for i in ids]
            if any(w['archived'] for w in words):
                raise ValueError('所选词条已归档，请刷新词库')
            payload={'owner':request.state.owner,'mode':body.mode,'day':today(),
                'word_ids':ids,'policy':policy,'resume_id':resume_id,'review_scope':requested_scope}
            job_id=store().enqueue_job('prepare_game',payload)
        return {'job_id':job_id,'preparing':True}

    @app.get('/api/games/{game_id}')
    async def get_game(game_id: str,request: Request):
        return game_for(game_id,request.state.owner).view()

    @app.get('/api/games/{game_id}/daily-words.xlsx')
    async def export_daily_words(game_id: str,request: Request):
        rows=store().rows('SELECT * FROM sessions WHERE id=? AND owner=?',(game_id,request.state.owner))
        if not rows:
            raise HTTPException(404,'游戏会话不存在或不属于当前浏览器')
        session=rows[0]
        if session['mode'] not in ('review','debug'):
            raise ValueError('只有一键复习会生成当天单词记录')
        payload=json.loads(session['payload'])
        words=[]
        for snapshot in payload.get('words',[]):
            current=store().get_word(int(snapshot['id']))
            words.append({**current,'word':snapshot.get('answer') or current['word'],
                          'translation':current['translation'] or snapshot.get('prompt') or ''})
        scope={'all':'全部词库','grade8_upper':'初二上'}.get(payload.get('selection_scope'),'全部词库')
        content=daily_words_workbook(words,session['day'],scope)
        filename=f'当天单词记录{session["day"].replace("-","")}.xlsx'
        return Response(content,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={
            'Content-Disposition':f"attachment; filename=daily-words-{session['day'].replace('-','')}.xlsx; filename*=UTF-8''{quote(filename)}",
            'X-WordLearner-Filename':quote(filename),
        })

    @app.post('/api/games/{game_id}/voice')
    async def voice(game_id: str,request: Request):
        game = game_for(game_id,request.state.owner)
        ticket = request.headers.get('x-voice-ticket')
        if not game.connected or not ticket or ticket!=game.voice_ticket:
            raise ValueError('录音会话已失效')
        chunks, size = [],0
        async for chunk in request.stream():
            size += len(chunk)
            if size>520000:
                raise HTTPException(413,'录音过长')
            chunks.append(chunk)
        try:
            recognition_started = time.perf_counter()
            text = str(await app.state.services.transcribe(b''.join(chunks)) or '')
            recognition_ms = round((time.perf_counter()-recognition_started)*1000,1)
            logger.info('Voice recognition completed in %.1f ms for %d audio bytes',recognition_ms,size)
            with game.atomic():
                if game.voice_required:
                    game.apply_voice(ticket,text)
                else:
                    game.finish_voice_diagnostic(ticket,text)
        except ValueError:
            if ticket==game.voice_ticket:
                with game.atomic():
                    game.end_pause()
                    game.save()
            raise
        details = game.state.voice_match_details if game.voice_required else {}
        return {
            'transcript': text,
            'normalized_transcript': details.get('transcript',text.strip().casefold()),
            'recognized': bool(text.strip()),
            'matched': bool(game.state.voice_locked_runtime_id),
            'audio_bytes': size,
            'recognition_ms': recognition_ms,
            'message': game.message,
            'state': game.view(),
        }

    @app.websocket('/api/games/{game_id}/socket')
    async def game_socket(ws: WebSocket,game_id: str):
        remote_blocked = not testing and not public_mode and ws.client.host not in ('127.0.0.1','::1')
        if remote_blocked or urlparse(ws.headers.get('origin','')).netloc!=ws.headers.get('host'):
            await ws.close(code=1008)
            return
        lease = secrets.token_urlsafe(18)
        try:
            owner = page_token(ws.query_params.get('page')) or ws.cookies.get(COOKIE)
            game = game_for(game_id,owner)
            with game.atomic():
                game.connect()
        except (HTTPException,ValueError):
            await ws.close(code=1008)
            return
        app.state.game_socket = ws
        app.state.game_lease = lease
        try:
            await ws.accept()
        except RuntimeError:
            if app.state.game_lease==lease:
                app.state.game_socket = None
                app.state.game_lease = None
                with game.atomic():
                    game.disconnect()
            return
        async def sender():
            while game.connected and app.state.game_lease==lease:
                await ws.send_json({'type':'state','state':game.view()})
                await asyncio.sleep(.05)
            if app.state.game_lease==lease:
                await ws.close(code=1000)
        sender_task = asyncio.create_task(sender())
        try:
            async for raw in ws.iter_text():
                if len(raw)>2048:
                    await ws.close(code=1009)
                    break
                payload = None
                try:
                    payload = json.loads(raw)
                    if payload=={'type':'heartbeat'}:
                        game.last_contact = time.monotonic()
                        continue
                    command = Command.model_validate(payload)
                    if command.type=='voice_start' and not app.state.services.configured('asr'):
                        raise ValueError('语音识别尚未配置，请先配置独立 ASR 服务')
                    with game.atomic():
                        result = game.command(command.model_dump(exclude_none=True))
                    await ws.send_json({'type':'ack','seq':game.seq,**result})
                except (ValueError,ValidationError) as exc:
                    message = '无效的游戏操作' if isinstance(exc,(ValidationError,json.JSONDecodeError)) else str(exc)
                    await ws.send_json({'type':'error','message':message,'seq':game.seq})
        except (WebSocketDisconnect,RuntimeError):
            pass
        finally:
            sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError,RuntimeError,WebSocketDisconnect):
                await sender_task
            if app.state.game_lease==lease:
                app.state.game_socket = None
                app.state.game_lease = None
                with game.atomic():
                    game.disconnect()

    dist = ROOT/'frontend/dist'
    if (dist/'assets').exists():
        app.mount('/assets',StaticFiles(directory=dist/'assets'),name='assets')

    @app.get('/pcm-worklet.js')
    async def worklet():
        return FileResponse(ROOT/'frontend/public/pcm-worklet.js',media_type='application/javascript')

    @app.get('/')
    async def index():
        if not (dist/'index.html').exists():
            return JSONResponse({'detail':'前端尚未构建，请在 frontend 中执行 npm run build'},status_code=503)
        return FileResponse(dist/'index.html',headers={'Cache-Control':'no-cache'})

    return app


app = create_app()
