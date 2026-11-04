"""模型看得到的当前时间从这里取：这一轮开始的时间（系统提示词里的日期、子 agent 的日期）和 get_current_time。
平时就是系统时钟；评测回放时整体挪到录像的那时（见 app.evals.tape），“明天”和录的时候是同一天，录好的天气、航班才对得上。
记账、排队、出错时间这些模型看不到的，照常用系统时钟。每个 planner 一个（评测时每个用例一个，同时跑的用例各挪各的）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo


@dataclass
class Clock:
    shift: timedelta = timedelta()

    def now(self, tz: tzinfo = UTC) -> datetime:
        return datetime.now(tz) + self.shift
