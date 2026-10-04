from backend.integrations import _RequestPayloadSession


class RecordingSession:
    def __init__(self):
        self.headers = {}
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return object()

    def close(self):
        self.closed = True


def test_deepseek_payload_disables_thinking_and_removes_qwen_option():
    underlying = RecordingSession()
    session = _RequestPayloadSession(
        underlying,
        remove=('chat_template_kwargs',),
        extra={'thinking': {'type': 'disabled'}},
    )

    session.post('https://api.deepseek.com/v1/chat/completions', json={
        'model': 'deepseek-flash',
        'chat_template_kwargs': {'enable_thinking': False},
        'messages': [{'role': 'user', 'content': 'hello'}],
    })

    payload = underlying.calls[0][1]['json']
    assert payload['thinking'] == {'type': 'disabled'}
    assert 'chat_template_kwargs' not in payload
    assert payload['messages'][0]['content'] == 'hello'
    session.close()
    assert underlying.closed
