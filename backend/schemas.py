from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class WordInput(StrictModel):
    word: str = Field(min_length=1,max_length=120)
    translation: str = Field(default='',max_length=1000)
    phonetic: str = Field(default='',max_length=200)
    created_on: date
    tags: list[str] | None = Field(default=None,max_length=20)


class WordIds(StrictModel):
    ids: list[int] = Field(min_length=1,max_length=1000)


class StartGame(StrictModel):
    mode: Literal['practice','review','debug']
    word_ids: list[int] = Field(default_factory=list,max_length=1000)
    review_scope: Literal['all','grade8_upper'] = 'all'


class PasswordInput(StrictModel):
    password: str = Field(min_length=6,max_length=200)
    confirmation: str = Field(min_length=6,max_length=200)


class PolicyInput(StrictModel):
    password: str = Field(max_length=200)
    word_count: int = Field(ge=10,le=200)
    round_count: int = Field(ge=1,le=3)
    perfect_reward_money: float = Field(ge=0,le=100,allow_inf_nan=False)


class AIInput(StrictModel):
    task: Literal['note','entry','error','report','chat']
    word_id: int | None = None
    question: str = Field(default='',max_length=2000)
    start: date | None = None
    end: date | None = None
    draft: WordInput | None = None


class Command(StrictModel):
    seq: int = Field(ge=1,strict=True)
    type: Literal['key','speed','retry','restart','hint_start','hint_end','voice_start','voice_cancel','close']
    char: str | None = None
    value: Literal[0.2,0.5,0.7] | None = None
    badge: int | None = Field(default=None,ge=1,le=5)

    @model_validator(mode='after')
    def check(self):
        expected = {'key':'char','speed':'value','hint_start':'badge'}.get(self.type)
        for name in ('char','value','badge'):
            if name==expected and getattr(self,name) is None:
                raise ValueError('缺少操作参数')
            if name!=expected and getattr(self,name) is not None:
                raise ValueError('多余的操作参数')
        if self.char is not None and (len(self.char)!=1 or not self.char.isprintable()):
            raise ValueError('每次只能提交一个有效字符')
        return self
