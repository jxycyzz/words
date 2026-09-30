import asyncio
import json
from datetime import date,timedelta

from .clock import today,timestamp
from .domain.parent_settings import ParentSettingsManager
from .store import encoded


class JobWorker:
    PREPARE_CONCURRENCY=4

    def __init__(self,store,services,on_prepared):
        self.store,self.services,self.on_prepared=store,services,on_prepared

    def update(self,job_id,**fields):
        fields['updated_at']=timestamp()
        with self.store.conn:
            self.store.conn.execute('UPDATE background_jobs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),job_id))

    async def once(self):
        jobs=self.store.rows("SELECT * FROM background_jobs WHERE status='pending' ORDER BY CASE kind WHEN 'prepare_game' THEN 0 WHEN 'saved_word' THEN 1 ELSE 2 END,id LIMIT 1")
        if not jobs:return
        job=jobs[0]; payload=json.loads(job['payload']); job_id=job['id']
        self.update(job_id,status='running')
        try:
            ids=payload.get('word_ids',[])
            if job['kind']=='preheat' and not payload.get('word_ids'):
                future=date.fromisoformat(today())+timedelta(days=1)
                limit=ParentSettingsManager(self.store).current_policy()['word_count']
                candidates=sorted(self.store.words(),key=lambda w:(0 if w['created_on'] in (today(),future.isoformat()) else 1,-self.store.review_score(w,future),w['due_on'],w['word'].casefold()))
                ids=[w['id'] for w in candidates[:limit]]
                payload['word_ids']=ids
                self.update(job_id,payload=encoded(payload))
            tasks=[(i,'note') for i in ids]
            if job['kind']=='saved_word':tasks += [(i,'entry') for i in ids]
            if job['kind']=='preheat':tasks += [(i,'error') for i in ids if self.store.mistakes(i)]
            self.update(job_id,total=len(tasks))
            previous=json.loads(job['result'])
            results=previous.get('items',[]); failures=previous.get('failures',[])
            async def run_task(word_id,kind):
                try:
                    word=self.store.get_word(word_id)
                    if word['archived']:
                        return None,None
                    result=await self.services.ai(self.store,kind,word_id)
                    return {'word_id':word_id,'word':word['word'],'kind':kind,**result},None
                except ValueError as exc:
                    return None,{'word_id':word_id,'kind':kind,'message':str(exc)}

            batch_size=self.PREPARE_CONCURRENCY if job['kind']=='prepare_game' else 1
            start_index=min(int(job['progress']),len(tasks))
            for batch_start in range(start_index,len(tasks),batch_size):
                if job['kind']!='prepare_game' and self.store.rows("SELECT id FROM background_jobs WHERE kind='prepare_game' AND status='pending'"):
                    self.update(job_id,status='pending')
                    return
                batch=tasks[batch_start:batch_start+batch_size]
                outcomes=await asyncio.gather(*(run_task(word_id,kind) for word_id,kind in batch))
                for item,failure in outcomes:
                    if item: results.append(item)
                    if failure: failures.append(failure)
                # Only persist a completed contiguous batch so a process restart can
                # safely resume at progress without skipping an unfinished card.
                self.update(job_id,progress=batch_start+len(batch),result=encoded({'items':results,'failures':failures}))
            if job['kind']=='prepare_game':
                if failures:raise ValueError('以下单词未完成 AI 查词，已停止进入练习：'+ '；'.join(f['message'] for f in failures[:3]))
                latest=self.store.rows('SELECT payload FROM background_jobs WHERE id=?',(job_id,))
                if latest:
                    payload=json.loads(latest[0]['payload'])
                result=self.on_prepared(payload)
            else:
                result={'items':results,'failures':failures}
            self.update(job_id,status='failed' if failures else 'completed',result=encoded(result),error='；'.join(f['message'] for f in failures[:3]) if failures else '')
            with self.store.conn:
                self.store.log_operation(job['kind']+'_completed','后台任务完成',job_id=job_id,failures=len(failures))
        except Exception as exc:
            self.update(job_id,status='failed',error=str(exc) if isinstance(exc,ValueError) else '后台任务失败，请重试')

    async def run(self):
        with self.store.conn:
            self.store.conn.execute("UPDATE background_jobs SET status='pending' WHERE status='running'")
        while True:
            await self.once()
            await asyncio.sleep(.2)
