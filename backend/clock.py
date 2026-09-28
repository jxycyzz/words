from datetime import datetime
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')


def today() -> str:
    return datetime.now(ZONE).date().isoformat()


def timestamp() -> str:
    return datetime.now(ZONE).isoformat(timespec='seconds')
