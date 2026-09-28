"""Explicit, one-time copy of service settings; never reads or writes learning data."""
import argparse
import json
from pathlib import Path


def migrate(source: Path, destination: Path):
    config=json.loads(destination.read_text(encoding='utf-8-sig')) if destination.exists() else {}
    ai_path=source/'ai_config.json'
    if ai_path.exists():
        legacy=json.loads(ai_path.read_text(encoding='utf-8-sig'))
        for kind,prefix in [('ai',''),('asr','asr_')]:
            target=config.setdefault(kind,{})
            for name in ('base_url','model','api_key'):
                if not target.get(name):target[name]=legacy.get(prefix+name,'')
    mail_path=source/'email_config.json'
    if mail_path.exists():
        legacy=json.loads(mail_path.read_text(encoding='utf-8-sig'))
        target=config.setdefault('email',{})
        for name in ('host','port','account','recipient','auth_code'):
            if not target.get(name):target[name]=legacy.get(name,'')
        # Copying credentials is not authorization to send messages.
        target.setdefault('enabled',False)
    destination.write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return {kind:bool(all(config.get(kind,{}).get(k) for k in ('base_url','model','api_key'))) for kind in ('ai','asr')}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,required=True)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    print(json.dumps(migrate(args.source,root/'config.json')))
