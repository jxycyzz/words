"""Persisted settlement snapshots and conservative SMTP delivery semantics."""
import asyncio
import json
import smtplib
import ssl
import time
from email.message import EmailMessage

from .clock import timestamp

REQUIRED={'day','session_id','practiced','unique_words','correct','accuracy','reward_rounds','reward_points',
          'reward_money','review_policy','review_elapsed_seconds','usage_seconds','completed_30_minutes','closed_at','close_trigger','review_scope'}


def validate_payload(payload):
    if not isinstance(payload,dict) or not REQUIRED.issubset(payload):
        raise ValueError('结算数据不完整，禁止发送默认零值邮件')
    policy=payload['review_policy']
    rounds=payload['reward_rounds']
    if not isinstance(policy,dict) or not isinstance(rounds,list):
        raise ValueError('结算规则或轮次数据无效')
    if sum(r['reward_points'] for r in rounds)!=payload['reward_points']:
        raise ValueError('奖励积分与实际保存轮次不符')
    money=round(float(policy['perfect_reward_money'])*min(max(payload['reward_points'],0),policy['reward_point_ceiling'])/policy['reward_point_ceiling'],2)
    if money!=payload['reward_money']:
        raise ValueError('奖励金与当天冻结规则不符')
    cumulative_review=payload.get('review_usage_seconds',payload['usage_seconds'])
    if not 0<=payload['correct']<=payload['practiced'] or payload['review_elapsed_seconds']<0 or cumulative_review<0:
        raise ValueError('结算统计数据无效')


def message_for(payload, config, event_id):
    validate_payload(payload)
    policy=payload['review_policy']
    rounds={r['round_number']:r for r in payload['reward_rounds']}
    reached=payload['completed_30_minutes']
    lines=['WordLearner 每日一键复习结算','',f"日期：{payload['day']}",f"关闭时间：{payload['closed_at']}",
        '状态：'+('已达到今日 30 分钟上限并关闭' if reached else '游戏界面已关闭，当前进度已保存'),
        f"每日规则：{policy['word_count']} 个单词，{policy['round_count']} 个奖励槽位；复习不限轮",
        f"本次窗口复习记录：{payload['practiced']} 次",f"本次不重复单词：{payload['unique_words']} 个",
        f"本次正确记录：{payload['correct']} 次",f"本次复习正确率：{payload['accuracy']:.1f}%",'',
        f"各奖励槽位最佳实际记录（最多 {policy['round_count']} 项）："]
    for number, maximum in enumerate(policy['round_max_scores'],1):
        r=rounds.get(number)
        lines.append(f"第 {number} 轮：{r['reward_points']}/{r['max_score']} 分，字母准确率 {r['accuracy_percent']:.1f}%" if r else f'第 {number} 轮：未完成（0/{maximum} 分）')
    if len(rounds)<policy['round_count']:
        lines.append('奖励槽位尚未全部产生记录；上方金额按当前已保存的真实记录计算。')
    def duration(s):
        h,left=divmod(int(s),3600); m,s=divmod(left,60)
        return f'{h:02}:{m:02}:{s:02}'
    cumulative_review=payload.get('review_usage_seconds')
    cumulative_line=(f"一键复习累计用时：{duration(cumulative_review)}" if cumulative_review is not None
                     else f"软件累计使用时长：{duration(payload['usage_seconds'])}")
    lines.extend([f"当日奖励总分：{payload['reward_points']}/{policy['reward_point_ceiling']}",
        f"当日奖励金：¥{payload['reward_money']:.2f}/¥{policy['perfect_reward_money']:.2f}",
        f"今日一键复习用时：{duration(payload['review_elapsed_seconds'])}",
        cumulative_line,'','此邮件由 WordLearner 在一键复习游戏界面关闭后自动发送。'])
    message=EmailMessage()
    message['Subject']=f"WordLearner 一键复习关闭结算 - {payload['day']}"
    message['From']=config['account']; message['To']=config['recipient']
    message['Message-ID']=f"<wordlearner-{payload['session_id']}-{event_id}@wordlearner.local>"
    message.set_content('\n'.join(lines))
    return message


class MailWorker:
    def __init__(self,store,config,smtp_factory=smtplib.SMTP_SSL):
        self.store,self.config,self.smtp_factory=store,config,smtp_factory

    @property
    def enabled(self):
        return bool(self.config.get('enabled') and all(self.config.get(k) for k in ('host','port','account','recipient','auth_code')))

    def deliver(self,message):
        sending=False
        try:
            with self.smtp_factory(self.config['host'],int(self.config['port']),timeout=20,context=ssl.create_default_context()) as client:
                client.login(self.config['account'],self.config['auth_code'])
                sending=True
                client.send_message(message)
            return 'sent',''
        except Exception as exc:
            # SMTP cannot prove non-delivery after DATA; do not automatically duplicate uncertain mail.
            return ('uncertain' if sending else 'failed'),f'邮件发送失败（{type(exc).__name__}）'

    async def once(self):
        if not self.enabled: return
        rows=self.store.rows("SELECT * FROM settlement_events WHERE status IN ('pending','failed') AND next_attempt<=? ORDER BY id LIMIT 1",(time.time(),))
        if not rows: return
        event=rows[0]
        try:
            message=message_for(json.loads(event['payload']),self.config,event['id'])
        except (ValueError,KeyError,TypeError,ZeroDivisionError):
            with self.store.conn:
                self.store.conn.execute("UPDATE settlement_events SET status='invalid',last_error=? WHERE id=?",('结算数据不完整或不一致，已停止发送',event['id']))
                self.store.log_operation('settlement_payload_rejected','结算数据验证失败，未发送邮件',event_id=event['id'])
            return
        with self.store.conn:
            changed=self.store.conn.execute("UPDATE settlement_events SET status='sending',attempts=attempts+1 WHERE id=? AND status IN ('pending','failed')",(event['id'],)).rowcount
        if not changed:return
        status,error=await asyncio.to_thread(self.deliver,message)
        with self.store.conn:
            self.store.conn.execute('UPDATE settlement_events SET status=?,last_error=?,sent_at=?,next_attempt=? WHERE id=?',
                (status,error,timestamp() if status=='sent' else '',time.time()+min(60*2**min(event['attempts'],8),3600),event['id']))
            self.store.log_operation('settlement_email_'+status,'结算邮件：'+status,event_id=event['id'])

    async def run(self):
        with self.store.conn:
            self.store.conn.execute("UPDATE settlement_events SET status='uncertain',last_error='服务中断，发送结果待确认' WHERE status='sending'")
        while True:
            await self.once()
            await asyncio.sleep(2)
