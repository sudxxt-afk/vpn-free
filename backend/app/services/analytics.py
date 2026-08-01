from collections import defaultdict
from datetime import date, datetime, timedelta
from uuid import UUID


FUNNEL_EVENTS = ("bot_start", "vpn_issued", "site_visit", "happ_launch", "subscription_open")
ACTIVE_EVENTS = set(FUNNEL_EVENTS)


def sequential_funnel(events, start: datetime) -> dict[str, int]:
    progress: dict[UUID, int] = {}
    for event in events:
        user_id = event.telegram_user_id
        if user_id is None or event.created_at < start:
            continue
        current = progress.get(user_id, 0)
        if current < len(FUNNEL_EVENTS) and event.event_type == FUNNEL_EVENTS[current]:
            progress[user_id] = current + 1
    return {name: sum(1 for value in progress.values() if value > index) for index, name in enumerate(FUNNEL_EVENTS)}


def daily_retention_cohorts(
    first_starts: list[tuple[UUID, datetime]],
    activity_events,
    today: date,
    cohort_days: int = 14,
) -> list[dict]:
    first_day = today - timedelta(days=cohort_days - 1)
    cohort_users: dict[date, set[UUID]] = defaultdict(set)
    activity_days: dict[UUID, set[date]] = defaultdict(set)
    for user_id, started_at in first_starts:
        started = started_at.date()
        if first_day <= started <= today:
            cohort_users[started].add(user_id)
    for event in activity_events:
        if event.telegram_user_id is not None and event.event_type in ACTIVE_EVENTS:
            activity_days[event.telegram_user_id].add(event.created_at.date())
    result = []
    for offset in range(cohort_days):
        cohort_date = first_day + timedelta(days=offset)
        users = cohort_users.get(cohort_date, set())
        row: dict[str, str | int | float | None] = {"date": str(cohort_date), "users": len(users)}
        for day in (0, 1, 3, 7):
            key = f"d{day}"
            if not users or cohort_date + timedelta(days=day) > today:
                row[key] = None
                continue
            active = sum(1 for user_id in users if cohort_date + timedelta(days=day) in activity_days.get(user_id, set()))
            row[key] = round(active * 100 / len(users), 1)
        result.append(row)
    return result
