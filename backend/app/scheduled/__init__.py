"""定时任务（2026-11-12 Sean 定，参考豆包的 /chat/cron-jobs、Codex、WorkBuddy）。

到点了，在任务挂着的那段对话里多跑一轮：把建任务时写好的那份完整交代当成这一轮的话，模型照常搜、读、写，结果留在对话里等用户回来看。
- 任务是独立的东西，有自己的页面和接口（/api/scheduled-tasks）；也可以在对话里说一句话让模型建（见 app.tools.schedule）。
  对话里建的挂在那段对话上；接口建的第一次跑时新开一段对话。对话删了任务还在，下次跑时再新开一段。
- 什么时候跑：重复的是 5 段的 cron（最密每小时一次），只跑一次的是一个时间点，都按北京时间（见 rule）。
- 只给登录用户，每人最多 config.SCHEDULED_TASKS_MAX 个（跑完了的一次性任务不算）；定时跑的照常扣这个人的额度，额度用完了这一次不跑。
- 没人在场：这一轮不出问题卡、不跟浏览器要位置（见 graph.research.history.SCHEDULED）。
- 到点发起、跑完记结果在运行器里（见 api.research_runner 的“定时任务”一节）：服务停着时错过的，起来以后只补一次；
  连着 config.SCHEDULED_MAX_FAILURES 次没跑成就自己停下，等用户来看。

rule：什么时候跑。store：任务的表（内存、Postgres 各一份）。service：建、改、删、列，和每次跑完以后怎么记。
"""
