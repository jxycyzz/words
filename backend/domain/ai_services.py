from __future__ import annotations
import base64
import json
import re
from dataclasses import dataclass
AI_BASE_URL = AI_MODEL = AI_AUTH_TOKEN = ""
QWEN_ASR_BASE_URL = QWEN_ASR_MODEL = QWEN_ASR_AUTH_TOKEN = ""

@dataclass(frozen=True)
class AIResult:
    content: str = ""
    error: str = ""
    cached: bool = False

@dataclass(frozen=True)
class ASRResult:
    transcript: str = ""
    error: str = ""

class AIService:
    def __init__(
        self,
        base_url: str = AI_BASE_URL,
        model: str = AI_MODEL,
        auth_token: str = AI_AUTH_TOKEN,
        timeout: float = 120.0,
        session=None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.auth_token = auth_token
        self.timeout = timeout
        self._session = session

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
            self._session.trust_env = False
        return self._session

    def generate_word_note(self, word) -> AIResult:
        messages = [
            self._system_message("word_note"),
            {
                "role": "user",
                "content": (
                    "请为这个英语学习词条生成学习卡，控制在5个小节以内：\n"
                    f"单词/短语：{word.word}\n"
                    f"释义：{word.translation or '无'}\n"
                    f"音标：{word.phonetic or '无'}\n"
                    f"练习次数：{word.practice_count}\n"
                    f"正确率：{word.accuracy_percent}%\n"
                    "必须包含：自然例句、常见搭配、词根词缀或构词提示、易混点、记忆方法。"
                ),
            },
        ]
        return self._complete(messages, max_tokens=1400)

    def generate_error_hint(
        self,
        word,
        recent_wrong_count: int,
        mastery: int,
        letter_mistakes: list[dict[str, object]] | None = None,
    ) -> AIResult:
        letter_mistakes = letter_mistakes or []
        messages = [
            self._system_message("error_hint"),
            {
                "role": "user",
                "content": (
                    "请根据真实打字错误记录，给出这个错词的短记忆提示，120字以内，必须具体、可操作。\n"
                    f"单词/短语：{word.word}\n"
                    f"释义：{word.translation or '无'}\n"
                    f"近期错误次数：{recent_wrong_count}\n"
                    f"掌握度：{mastery}%\n"
                    "真实输错字母记录如下，position 表示第几个字符：\n"
                    f"{json.dumps(letter_mistakes, ensure_ascii=False, sort_keys=True)}\n"
                    "只分析这些位置为什么容易输错，以及怎么记住正确字母。"
                    "不要和其他单词做对比，不要写易混词对比。"
                ),
            },
        ]
        return self._complete(messages, max_tokens=350, temperature=0.35)

    def generate_entry_check(self, word) -> AIResult:
        messages = [
            self._system_message("entry_check"),
            {
                "role": "user",
                "content": (
                    "请检查这个新录入的英语词条是否像真实英语单词、短语或常见表达。"
                    "第一行必须严格输出【录入检测】正常 或 【录入检测】疑似错误。"
                    "如果可能是拼写错误、大小写问题、空格问题或不自然表达，请指出并给出建议；"
                    "如果看起来正常，请给一句简短确认。\n"
                    f"词条：{word.word}\n"
                    f"释义：{word.translation or '无'}\n"
                    f"音标：{word.phonetic or '无'}"
                ),
            },
        ]
        return self._complete(messages, max_tokens=450, temperature=0.25)

    def generate_daily_report(self, start_date: str, end_date: str, payload: dict) -> AIResult:
        messages = [
            self._system_message("daily_report"),
            {
                "role": "user",
                "content": (
                    f"请基于 {start_date} 到 {end_date} 的学习数据生成AI学习报告，300-600字。\n"
                    "必须包含：总体表现、遗忘风险、重点复习词、明日建议。\n"
                    "数据如下：\n"
                    f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
                ),
            },
        ]
        return self._complete(messages, max_tokens=1000, temperature=0.4)

    def chat(self, user_message: str, context: dict) -> AIResult:
        messages = [
            self._system_message("chat"),
            {
                "role": "user",
                "content": (
                    "下面是当前单词学习上下文，请结合上下文回答用户问题。"
                    "如果问题和学习无关，也要简洁回答。\n"
                    f"上下文：{json.dumps(context, ensure_ascii=False, sort_keys=True)}\n"
                    f"用户问题：{user_message.strip()}"
                ),
            },
        ]
        return self._complete(messages, max_tokens=900, temperature=0.45)

    def _system_message(self, task: str = "chat") -> dict[str, str]:
        return {
            "role": "system",
            "content": (
                "你是中文英语学习助教。回答必须以中文解释为主，英文只用于单词、短语、例句和必要对照。"
                "/no_think。"
                "禁止输出思考过程、推理过程、Thinking、Reasoning、<think>标签或类似内容。"
                "不要使用 Markdown 粗体、标题符号、表格或代码块；直接给最终答案。"
                "不要编造用户没有提供的学习记录。"
                + self._format_instruction(task)
            ),
        }

    def _format_instruction(self, task: str) -> str:
        formats = {
            "word_note": (
                "\n学习卡必须使用纯文本中文格式，按下面标题输出：\n"
                "【核心释义】用中文解释含义和使用场景。\n"
                "【自然例句】给1-2个英文例句，并用中文解释句意。\n"
                "【常见搭配】列出常用搭配，中文说明用法。\n"
                "【易混点】说明容易混淆的词或短语，中文解释差别。\n"
                "【记忆方法】给一个具体中文记忆方法。\n"
                "不要输出 **、###、- ** 这类 Markdown 标记。"
            ),
            "error_hint": (
                "\n错词提示用纯文本中文，120字以内。格式必须换行："
                "【容易错的原因】\n...\n【记忆方法】\n..."
                "必须基于用户真实输错的字母位置分析，不要和其他单词或易混词做对比。"
            ),
            "entry_check": (
                "\n新词录入检测用纯文本中文，120字以内。格式必须换行："
                "【录入检测】正常 或 【录入检测】疑似错误\n【建议】\n..."
            ),
            "daily_report": (
                "\n报告用纯文本中文，300-600字。使用中文小标题，禁止 Markdown 标记。"
            ),
            "chat": (
                "\n回答以中文解释为主，英文只作为例子或术语。禁止输出思考过程和 Markdown 粗体。"
            ),
        }
        return formats.get(task, formats["chat"])

    def _complete(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = 800,
        temperature: float = 0.4,
    ) -> AIResult:
        try:
            response = self.session.post(
                f"{self.base_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.auth_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
                timeout=self.timeout,
            )
            if getattr(response, "status_code", 200) >= 400:
                detail = getattr(response, "text", "")
                message = f"{response.status_code} {getattr(response, 'reason', '')}".strip()
                if detail:
                    message = f"{message}：{detail[:500]}"
                return AIResult(error=f"AI 请求失败：{message}")
            response.raise_for_status()
            payload = response.json()
            content = self.clean_content(str(payload["choices"][0]["message"]["content"]))
            if not content:
                return AIResult(error="AI 返回内容为空")
            return AIResult(content=content)
        except Exception as exc:
            return AIResult(error=f"AI 请求失败：{exc}")

    def clean_content(self, content: str) -> str:
        text = content.strip()
        text = re.sub(r"(?is)<think>.*?</think>", "", text)
        text = re.sub(r"(?is)<thinking>.*?</thinking>", "", text)
        text = re.sub(r"(?is)^.*?</think>", "", text)
        section_markers = (
            "【核心释义】",
            "【容易错的原因】",
            "【总体表现】",
            "【今日表现】",
            "【学习总结】",
            "【回答】",
        )
        marker_positions: list[int] = []
        for marker in section_markers:
            start = 0
            while True:
                position = text.find(marker, start)
                if position < 0:
                    break
                marker_positions.append(position)
                start = position + len(marker)
        if marker_positions:
            chosen = marker_positions[-1]
            for position in reversed(marker_positions):
                marker = next(item for item in section_markers if text.startswith(item, position))
                after_marker = text[position + len(marker):].lstrip()
                if after_marker.startswith(("，", ",", "、", "；", ";", ")", "）", ".", "。", "…")):
                    continue
                if not after_marker.startswith("【"):
                    chosen = position
                    break
            text = text[chosen:]
        text = re.split(
            r"(?im)^\s*\d+\.\s*(check|validate|review|verify|constraints|检查|验证|自检|格式检查|约束检查)\b.*$",
            text,
            maxsplit=1,
        )[0]
        text = re.split(
            r"(?im)^\s*(here'?s\s+a\s+thinking\s+process|thinking\s+process|analysis|reasoning)\s*:?.*$",
            text,
            maxsplit=1,
        )[-1]
        text = re.sub(r"(?im)^\s*(thinking|reasoning|思考过程|推理过程|分析)\s*[:：].*$", "", text)
        text = re.sub(r"```(?:\w+)?\s*", "", text)
        text = text.replace("```", "")
        text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
        text = re.sub(r"__(.*?)__", r"\1", text)
        text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
        text = re.sub(r"(?m)^\s*[-*]\s+", "• ", text)
        text = self._normalize_section_breaks(text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _normalize_section_breaks(self, text: str) -> str:
        titles = [
            "核心释义",
            "自然例句",
            "常见搭配",
            "易混点",
            "记忆方法",
            "容易错的原因",
            "录入检测",
            "建议",
            "总体表现",
            "遗忘风险",
            "重点复习词",
            "明日建议",
        ]
        for title in titles:
            text = re.sub(rf"(?<!^)(?<!\n)(【{re.escape(title)}】)", r"\n\n\1", text)
            text = re.sub(rf"(【{re.escape(title)}】)[ \t]*(?!\n)", r"\1\n", text)
        return text

class QwenASRService:
    def __init__(
        self,
        base_url: str = QWEN_ASR_BASE_URL,
        model: str = QWEN_ASR_MODEL,
        auth_token: str = QWEN_ASR_AUTH_TOKEN,
        timeout: float = 15.0,
        session=None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.auth_token = auth_token
        self.timeout = timeout
        self._session = session

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
            self._session.trust_env = False
        return self._session

    def transcribe_wav(self, wav_bytes: bytes) -> ASRResult:
        if not wav_bytes:
            return ASRResult(error="没有录到声音")
        if not self.auth_token.strip():
            return ASRResult(
                error="语音识别未配置，请检查 ai_config.json 中的语音服务密钥"
            )

        audio_url = "data:audio/wav;base64," + base64.b64encode(wav_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": audio_url,
                                "format": "wav",
                            },
                        }
                    ],
                }
            ],
            "asr_options": {
                "language": "en",
                "enable_itn": False,
            },
            "stream": False,
        }
        try:
            response = self.session.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.auth_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
            if getattr(response, "status_code", 200) >= 400:
                detail = getattr(response, "text", "")
                return ASRResult(
                    error=self._http_error_message(response.status_code, detail)
                )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"].get("content", "")
            transcript = self._extract_transcript(content)
            if not transcript:
                return ASRResult(error="语音识别结果为空")
            return ASRResult(transcript=transcript)
        except Exception as exc:
            detail = re.sub(r"\s+", " ", str(exc)).strip()[:160]
            if "timeout" in type(exc).__name__.casefold() or "timed out" in detail.casefold():
                return ASRResult(error="语音识别连接超时，请稍后重试")
            if any(
                marker in detail.casefold()
                for marker in ("name resolution", "getaddrinfo", "connection")
            ):
                return ASRResult(error="无法连接语音识别服务，请检查网络或服务地址")
            return ASRResult(error=f"语音识别失败：{detail or type(exc).__name__}")

    @staticmethod
    def _http_error_message(status_code: int, detail: str) -> str:
        normalized = re.sub(r"\s+", " ", str(detail)).casefold()
        if status_code == 401 or any(
            marker in normalized
            for marker in ("api key", "authorization", "unauthorized", "invalid token")
        ):
            return "语音识别鉴权失败，请检查 ai_config.json 中的语音服务密钥"
        if status_code == 403:
            return "语音识别服务拒绝访问，请检查账号权限或余额"
        if status_code == 429:
            return "语音识别请求过于频繁，请稍后重试"
        if status_code >= 500:
            return "语音识别服务暂时不可用，请稍后重试"
        return f"语音识别请求失败（HTTP {status_code}）"

    def _extract_transcript(self, content) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("transcript") or item.get("content")
                    if text:
                        parts.append(str(text))
                elif item:
                    parts.append(str(item))
            return " ".join(parts).strip()
        if content:
            return str(content).strip()
        return ""
