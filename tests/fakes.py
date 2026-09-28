"""Network-only test doubles; actual database, game engine and scoring stay in use."""
from pathlib import Path
import io
import wave
from backend.integrations import Integrations


class FakeServices(Integrations):
    def __init__(self):
        super().__init__(Path(__file__).parent/'nonexistent-config')
        self.calls=[]
        self.fail=False

    def configured(self,kind): return kind in ('ai','asr')

    async def ai(self,store,task,word_id=None,*args,**kwargs):
        self.calls.append((task,word_id))
        if self.fail: raise ValueError('测试网络不可用')
        return {'content':'【录入检测】正常' if task=='entry' else '隔离测试学习卡','cached':False}

    async def transcribe(self,body):
        with wave.open(io.BytesIO(body),'rb') as audio:
            assert audio.getnchannels()==1 and audio.getsampwidth()==2 and audio.getframerate()==16000
            assert audio.getnframes()>1280
        return 'voiceword'
