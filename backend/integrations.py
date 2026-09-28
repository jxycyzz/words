from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import secrets
import time
from collections import OrderedDict
import wave
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx

from .domain.ai_services import AIService, QwenASRService
from .store import encoded
from .clock import timestamp
from .clock import today
from datetime import date, timedelta


class Integrations:
    def __init__(self, root: Path):
        config_path = root/'config.json'
        self.config = json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.exists() else {}
        self.active_ai = asyncio.Semaphore(2)
        self.active_asr = asyncio.Semaphore(1)
        self.inflight = {}
        self.lookup_cache = OrderedDict()

    def configured(self, kind):
        cfg = self.config.get(kind,{})
        return bool(cfg.get('base_url') and cfg.get('api_key') and cfg.get('model'))

    def capabilities(self):
        return {'ai':self.configured('ai'),'asr':self.configured('asr'),
                'translation':True,
                'email':False}

    def service(self, kind):
        if not self.configured(kind):
            raise ValueError(f'{"AI" if kind=="ai" else "语音识别"}尚未配置，请在 bs-web/config.json 中填写独立服务配置后重启')
        cfg = self.config[kind]
        if urlparse(cfg['base_url']).scheme not in ('http','https'):
            raise ValueError('服务地址格式不正确')
        service=(AIService if kind=='ai' else QwenASRService)(base_url=cfg['base_url'],model=cfg['model'],auth_token=cfg['api_key'],timeout=120 if kind=='ai' else 30)
        service.session.headers.update({'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/139.0 Safari/537.36','Accept':'application/json'})
        return service

    async def transcribe(self, body):
        try:
            with wave.open(io.BytesIO(body),'rb') as audio:
                seconds = audio.getnframes()/audio.getframerate()
                if audio.getnchannels()!=1 or audio.getsampwidth()!=2 or audio.getframerate()!=16000 or not .08<=seconds<=15.5:
                    raise ValueError('录音需要为 0.08～15 秒、16kHz 单声道 PCM WAV')
        except (wave.Error,EOFError,ZeroDivisionError):
            raise ValueError('录音格式无效') from None
        async with self.active_asr:
            service = self.service('asr')
            result = await asyncio.to_thread(service.transcribe_wav,body)
            if result.error:
                raise ValueError('语音识别失败，请检查独立服务配置或稍后重试')
            return result.transcript

    async def ai(self, store, task, word_id=None, question='', start='', end='', draft=None):
        word = store.get_word(word_id) if word_id else None
        if not word and draft:
            word={'id':0,'word':draft['word'],'translation':draft.get('translation',''),'phonetic':draft.get('phonetic',''),
                  'practice_count':0,'correct_count':0,'accuracy':0,'mastery':0}
        if task=='error' and not word:
            recent=store.rows('SELECT word_id FROM letter_mistakes ORDER BY id DESC LIMIT 1')
            fallback=store.words()
            word=store.get_word(recent[0]['word_id']) if recent else min(fallback,key=lambda w:w['mastery']) if fallback else None
            word_id=word['id'] if word else None
        context = {'word':word}
        if task=='report':
            context=store.ai_context(start,end)
        elif task=='chat':
            context=store.ai_context((date.fromisoformat(today())-timedelta(days=6)).isoformat(),today(),word)
        elif task=='error':
            if not word:
                raise ValueError('暂无错词或低掌握度单词')
            context['mistakes'] = store.mistakes(word_id) if word_id else []
            if not context['mistakes']:
                return {'content':f'暂无“{word["word"]}”的具体输错字母记录。\n同一位置连续输错 3 次后才会记录字母错误；目前不生成没有实际依据的错因。','cached':False}
            start_wrong=(date.fromisoformat(today())-timedelta(days=30)).isoformat()
            context['recent_wrong_count']=store.conn.execute('SELECT count(*) FROM review_history WHERE word_id=? AND day>=? AND correct=0',(word_id,start_wrong)).fetchone()[0]
        service = self.service('ai')
        cache_payload = {'task':task,'model':service.model,'endpoint':service.base_url,'context':context,'question':question}
        if task in ('note','entry') and word:
            cache_payload['context'] = {k:word[k] for k in ('id','word','translation','phonetic')}
        key = hashlib.sha256(encoded(cache_payload).encode()).hexdigest()
        cached = store.conn.execute('SELECT content FROM ai_cache WHERE cache_key=?',(key,)).fetchone()
        if cached:
            return {'content':cached[0],'cached':True}
        if task in ('note','entry') and word_id:
            legacy=store.conn.execute('''SELECT content FROM ai_cache WHERE word_id=? AND kind=?
                ORDER BY created_at DESC,rowid DESC LIMIT 1''',(word_id,'desktop/'+task)).fetchone()
            if legacy:
                with store.conn:
                    store.conn.execute('INSERT OR REPLACE INTO ai_cache(cache_key,content,created_at,word_id,kind) VALUES(?,?,?,?,?)',
                                       (key,legacy[0],timestamp(),word_id,task))
                return {'content':legacy[0],'cached':True}
        async with self.active_ai:
            cached = store.conn.execute('SELECT content FROM ai_cache WHERE cache_key=?',(key,)).fetchone()
            if cached: return {'content':cached[0],'cached':True}
            ns = SimpleNamespace(**word,accuracy_percent=word['accuracy']) if word else None
            if task in ('note','entry','error') and ns is None:
                raise ValueError('请先选择一个词条')
            if task=='note':
                fn,args = service.generate_word_note,(ns,)
            elif task=='entry':
                fn,args = service.generate_entry_check,(ns,)
            elif task=='error':
                fn,args = service.generate_error_hint,(ns,context['recent_wrong_count'],ns.mastery,context['mistakes'])
            elif task=='report':
                fn,args = service.generate_daily_report,(start,end,context)
            else:
                fn,args = service.chat,(question,context)
            result = await asyncio.to_thread(fn,*args)
            if result.error or not result.content.strip():
                # Provider errors can contain credentials or internal URLs; never forward them.
                raise ValueError('AI 服务暂不可用，请检查配置或稍后重试')
            with store.conn:
                store.conn.execute('INSERT OR REPLACE INTO ai_cache(cache_key,content,created_at,word_id,kind) VALUES(?,?,?,?,?)',(key,result.content,timestamp(),word_id,task))
            return {'content':result.content,'cached':False}

    async def lookup(self, word):
        word = ' '.join(word.strip().split())
        if not word or len(word)>120:
            raise ValueError('请输入有效单词或短语')
        key = word.casefold()
        cached = self.lookup_cache.get(key)
        if cached and time.monotonic()-cached[0]<3600:
            return {**cached[1],'word':word}
        result = await self._lookup_online(word)
        if result['translation'] and result['phonetic']:
            self.lookup_cache[key] = (time.monotonic(),result)
            self.lookup_cache.move_to_end(key)
            while len(self.lookup_cache)>256:
                self.lookup_cache.popitem(last=False)
        return result

    @staticmethod
    def dictionary_entry(payload, word):
        """Read only the exact dictionary entry, never spelling suggestions."""
        entries = payload.get('ec',{}).get('word',[]) if isinstance(payload,dict) else []
        if not isinstance(entries,list):
            return '', ''
        for entry in entries:
            if not isinstance(entry,dict):
                continue
            phrase = entry.get('return-phrase',{})
            phrase = phrase.get('l',{}).get('i','') if isinstance(phrase,dict) else phrase
            if not isinstance(phrase,str) or ' '.join(phrase.split()).casefold()!=word.casefold():
                continue
            meanings = []
            for group in entry.get('trs',[]):
                for translation in group.get('tr',[]):
                    items = translation.get('l',{}).get('i',[])
                    if isinstance(items,str):
                        items = [items]
                    meanings.extend(item.strip() for item in items if isinstance(item,str) and item.strip())
            phone = entry.get('ukphone') or entry.get('usphone') or entry.get('phone') or ''
            phonetic = f'[{phone.strip()}]' if isinstance(phone,str) and phone.strip() else ''
            return '；'.join(dict.fromkeys(meanings))[:1000], phonetic[:200]
        return '', ''

    async def _lookup_online(self, word):
        config = self.config.get('translation',{})
        translation, phonetic = '', ''
        unavailable = False
        async with httpx.AsyncClient(timeout=10,trust_env=False,headers={'User-Agent':'WordLearner/0.1'}) as client:
            try:
                response = await client.get('https://dict.youdao.com/jsonapi',params={
                    'q':word,'dicts':json.dumps({'count':5,'dicts':[['ec']]},separators=(',',':'))})
                response.raise_for_status()
                translation,phonetic = self.dictionary_entry(response.json(),word)
            except (httpx.HTTPError,ValueError,TypeError,AttributeError):
                unavailable = True
            # Optional configured translation is independent of phonetics and dictionary availability.
            if config.get('app_id') and config.get('secret_key'):
                salt = secrets.token_hex(8)
                sign = hashlib.md5(f"{config['app_id']}{word}{salt}{config['secret_key']}".encode()).hexdigest()
                try:
                    response = await client.get('https://fanyi-api.baidu.com/api/trans/vip/translate',params={
                        'q':word,'from':'en','to':'zh','appid':config['app_id'],'salt':salt,'sign':sign})
                    response.raise_for_status()
                    translated = response.json()['trans_result'][0]['dst']
                    if isinstance(translated,str) and translated.strip():
                        translation = translated.strip()[:1000]
                except (httpx.HTTPError,KeyError,IndexError,TypeError,ValueError):
                    unavailable = True
        if not translation and not phonetic:
            raise ValueError('查词服务暂时无法连接，请稍后重试或手动填写' if unavailable else '未查到该词条，请检查拼写；也可以手动填写释义和音标')
        missing = [label for label,value in [('释义',translation),('音标',phonetic)] if not value]
        return {'word':word,'translation':translation,'phonetic':phonetic,
                'warning':('未查到'+ '和'.join(missing)+'，请手动补充') if missing else ''}
