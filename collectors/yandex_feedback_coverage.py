# поток: rev
"""Explicit API periods and direct checks of older open records."""
import calendar
import datetime as dt


def month_start(value):
    return dt.date(value.year,value.month,1)


def add_month(value,delta):
    index=value.year*12+value.month-1+delta
    return dt.date(index//12,index%12+1,1)


def question_jobs(now,pending_dates=()):
    today=now.date()
    current=month_start(today)
    months={current,add_month(current,-1)}
    months.update(month_start(value) for value in pending_dates if value and value.date()<=today)
    return [{'dateFrom':first.isoformat(),
             'dateTo':min(today,dt.date(first.year,first.month,calendar.monthrange(first.year,first.month)[1])).isoformat()}
            for first in sorted(months)]


def review_jobs(now,older_ids=()):
    # 179 days is below the API's maximum six-month interval in every season.
    jobs=[{'dateTimeFrom':(now-dt.timedelta(days=179)).isoformat(),
           'dateTimeTo':now.isoformat(),'reactionStatus':'ALL'}]
    identifiers=sorted({int(value) for value in older_ids if str(value).isdigit()})
    jobs.extend({'feedbackIds':identifiers[i:i+20]} for i in range(0,len(identifiers),20))
    return jobs
