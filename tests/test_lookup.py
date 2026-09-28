import asyncio
import json

import httpx
import pytest

from backend.integrations import Integrations

REAL_CLIENT = httpx.AsyncClient


def entry(word='bed', translation='n. 床；床位', phone='bed'):
    return {'ec': {'word': [{'return-phrase': {'l': {'i': word}}, 'ukphone': phone,
                            'trs': [{'tr': [{'l': {'i': [translation]}}]}]}]}}


def mock_client(monkeypatch, handler):
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs: REAL_CLIENT(**kwargs,transport=httpx.MockTransport(handler)))


def test_lookup_works_without_keys_and_caches(monkeypatch,tmp_path):
    calls=[]
    def handle(request):
        calls.append(request)
        assert request.url.params['q']=='bed'
        return httpx.Response(200,json=entry())
    mock_client(monkeypatch,handle)
    service=Integrations(tmp_path)
    result=asyncio.run(service.lookup('bed'))
    assert result['translation']=='n. 床；床位'
    assert result['phonetic']=='[bed]'
    assert asyncio.run(service.lookup('bed'))==result
    assert len(calls)==1


def test_baidu_failure_retains_dictionary_fields(monkeypatch,tmp_path):
    def handle(request):
        return httpx.Response(200,json=entry() if request.url.host=='dict.youdao.com' else {'error_code':'54001'})
    mock_client(monkeypatch,handle)
    service=Integrations(tmp_path)
    service.config={'translation':{'app_id':'test','secret_key':'isolated-test'}}
    assert asyncio.run(service.lookup('bed'))['phonetic']=='[bed]'
    assert asyncio.run(service.lookup('bed'))['translation']=='n. 床；床位'


def test_dictionary_failure_does_not_block_configured_translation(monkeypatch,tmp_path):
    def handle(request):
        if request.url.host=='dict.youdao.com':
            return httpx.Response(503)
        return httpx.Response(200,json={'trans_result':[{'dst':'床'}]})
    mock_client(monkeypatch,handle)
    service=Integrations(tmp_path)
    service.config={'translation':{'app_id':'test','secret_key':'isolated-test'}}
    result=asyncio.run(service.lookup('bed'))
    assert result['translation']=='床' and result['phonetic']==''
    assert result['warning']


def test_spelling_suggestion_not_used_as_requested_word(monkeypatch,tmp_path):
    mock_client(monkeypatch,lambda request:httpx.Response(200,json=entry('bed')))
    with pytest.raises(ValueError,match='未查到'):
        asyncio.run(Integrations(tmp_path).lookup('beddxyz'))


def test_network_failure_and_partial_result(monkeypatch,tmp_path):
    mock_client(monkeypatch,lambda request:httpx.Response(503))
    with pytest.raises(ValueError,match='暂时无法连接'):
        asyncio.run(Integrations(tmp_path).lookup('bed'))
    mock_client(monkeypatch,lambda request:httpx.Response(200,json=entry(phone='')))
    result=asyncio.run(Integrations(tmp_path).lookup('bed'))
    assert result['translation']=='n. 床；床位' and result['warning']=='未查到音标，请手动补充'


def test_asr_provider_reuses_http_session_between_words(tmp_path):
    (tmp_path/'config.json').write_text(json.dumps({'asr':{
        'base_url':'https://asr.invalid/v1','model':'fast-test','api_key':'isolated-test',
    }}),encoding='utf-8')
    integrations=Integrations(tmp_path)
    first=integrations.service('asr')
    provider_session=first.session
    second=integrations.service('asr')
    assert second is first
    assert second.session is provider_session
    integrations.close()
    assert integrations._asr_service is None
