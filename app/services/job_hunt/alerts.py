"""Job alerts: creating them, and running one.

Three rules shape this file, and each one exists because the obvious implementation is worse.

*Consent is per-alert and defaults to off.* Creating an alert and enabling it are separate
acts. A global "notifications on" switch that silently opted every saved search in would mean a
student who wanted one alert about Dubai data-engineering roles gets told about everything they
ever searched for, and the way people respond to that is to turn the whole product's
notifications off.

*The database enforces duplicate suppression, not this code.* JobAlertDelivery has a unique
constraint on (alert_id, listing_id). Checking "have we sent this already?" in Python and then
inserting is a race that loses the moment two runs overlap -- which is exactly what happens when
one run is slow and the scheduler starts the next. So we attempt the insert and treat the
IntegrityError as "already told them".

*A failed run is recorded as a failure, not as silence.* If the provider is down we write
last_error_at and a status, and the student is shown "we couldn't check" rather than being left
to conclude there are no jobs. An alert that goes quiet because of an outage looks identical to
an empty job market unless you say so.

What this module does NOT do is schedule anything. `run_alert` is a pure function of (alert,
now) that something else must call -- a cron job, a worker, a management command. Nothing in
this repository calls it on a timer yet, and that is stated in the audit rather than implied by
the presence of the code.
"""

import datetime
from typing import List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.job_alert import ALERT_FREQUENCIES, JobAlert, JobAlertDelivery
from app.models.job_saved_search import JobSavedSearch
from app.models.profile import StudentProfile
from app.services import notification_service
from app.services.job_hunt import service as job_hunt_service
from app.services.job_hunt.providers.base import JobProvider, JobProviderError
from app.services.subscription_service import is_premium

# The free tier gets a real, useful alert -- one standing search, checked daily. Not zero:
# a job product whose core "tell me when something appears" loop is paywalled is not a free
# product, it is a demo. Premium raises the ceiling rather than unlocking the feature.
FREE_MAX_ALERTS = 1
PREMIUM_MAX_ALERTS = 10

# Most jobs a single run will announce. A provider that suddenly returns fifty new matches must
# not produce fifty notifications; the student gets the top few and the count.
MAX_NOTIFIED_PER_RUN = 5


class AlertError(Exception):
    pass


class AlertNotFoundError(AlertError):
    pass


class AlertLimitReachedError(AlertError):
    """Raised with a message naming the actual limit, so the API can say which tier it is."""


class InvalidAlertError(AlertError):
    pass


def max_alerts_for(db: Session, profile_id: int) -> int:
    return PREMIUM_MAX_ALERTS if is_premium(db, profile_id) else FREE_MAX_ALERTS


def _validate(frequency: str, day_of_week: Optional[int], hour_local: int,
              quiet_start: Optional[int], quiet_end: Optional[int]) -> None:
    if frequency not in ALERT_FREQUENCIES:
        raise InvalidAlertError(f"Frequency must be one of {', '.join(ALERT_FREQUENCIES)}")
    if frequency == "weekly" and day_of_week is None:
        raise InvalidAlertError("A weekly alert needs a day of the week")
    if day_of_week is not None and not 0 <= day_of_week <= 6:
        raise InvalidAlertError("Day of week must be 0 (Monday) to 6 (Sunday)")
    if not 0 <= hour_local <= 23:
        raise InvalidAlertError("Hour must be between 0 and 23")
    for value, name in ((quiet_start, "Quiet hours start"), (quiet_end, "Quiet hours end")):
        if value is not None and not 0 <= value <= 23:
            raise InvalidAlertError(f"{name} must be between 0 and 23")
    # Both or neither. One half of a window is not a window, and guessing the other half would
    # invent a quiet period the student never asked for.
    if (quiet_start is None) != (quiet_end is None):
        raise InvalidAlertError("Set both quiet-hours values, or neither")


def create_alert(
    db: Session,
    profile_id: int,
    *,
    name: str,
    saved_search_id: Optional[int] = None,
    filters: Optional[dict] = None,
    frequency: str = "daily",
    day_of_week: Optional[int] = None,
    hour_local: int = 9,
    quiet_hours_start: Optional[int] = None,
    quiet_hours_end: Optional[int] = None,
    is_enabled: bool = False,
) -> JobAlert:
    _validate(frequency, day_of_week, hour_local, quiet_hours_start, quiet_hours_end)

    if saved_search_id is not None:
        owned = (
            db.query(JobSavedSearch)
            .filter(
                JobSavedSearch.id == saved_search_id,
                JobSavedSearch.profile_id == profile_id,
            )
            .first()
        )
        # 404, not 403: confirming that someone else's saved search exists is itself a leak.
        if owned is None:
            raise AlertNotFoundError(f"Saved search {saved_search_id} not found")

    # Idempotence before quota, deliberately. A student who already has an alert on this
    # saved search and taps "create alert" on it again is asking for the thing they already
    # have -- answering that with a quota refusal would be confusing and, on the free tier
    # where the limit is one, would make their own existing alert look like someone else's.
    if saved_search_id is not None:
        already = (
            db.query(JobAlert)
            .filter(
                JobAlert.profile_id == profile_id,
                JobAlert.saved_search_id == saved_search_id,
            )
            .first()
        )
        if already is not None:
            return already

    existing = db.query(JobAlert).filter(JobAlert.profile_id == profile_id).count()
    limit = max_alerts_for(db, profile_id)
    if existing >= limit:
        raise AlertLimitReachedError(
            f"You can have {limit} job alert{'s' if limit != 1 else ''} on your current plan."
        )

    alert = JobAlert(
        profile_id=profile_id,
        saved_search_id=saved_search_id,
        name=name.strip(),
        # Only when there is no saved search behind it -- see the model docstring for why the
        # two are never both populated.
        filters=filters if saved_search_id is None else None,
        frequency=frequency,
        day_of_week=day_of_week,
        hour_local=hour_local,
        quiet_hours_start=quiet_hours_start,
        quiet_hours_end=quiet_hours_end,
        is_enabled=is_enabled,
    )
    db.add(alert)
    try:
        db.commit()
    except IntegrityError:
        # The one-alert-per-saved-search constraint. Two taps on Create, or two devices.
        db.rollback()
        duplicate = (
            db.query(JobAlert)
            .filter(
                JobAlert.profile_id == profile_id,
                JobAlert.saved_search_id == saved_search_id,
            )
            .first()
        )
        if duplicate is None:
            raise
        return duplicate
    db.refresh(alert)
    return alert


def get_alert(db: Session, profile_id: int, alert_id: int) -> JobAlert:
    alert = (
        db.query(JobAlert)
        .filter(JobAlert.id == alert_id, JobAlert.profile_id == profile_id)
        .first()
    )
    if alert is None:
        raise AlertNotFoundError(f"Job alert {alert_id} not found")
    return alert


def list_alerts(db: Session, profile_id: int) -> List[JobAlert]:
    return (
        db.query(JobAlert)
        .filter(JobAlert.profile_id == profile_id)
        .order_by(JobAlert.created_at.desc())
        .all()
    )


def update_alert(
    db: Session,
    profile_id: int,
    alert_id: int,
    *,
    name: Optional[str] = None,
    frequency: Optional[str] = None,
    day_of_week: Optional[int] = None,
    hour_local: Optional[int] = None,
    quiet_hours_start: Optional[int] = None,
    quiet_hours_end: Optional[int] = None,
    is_enabled: Optional[bool] = None,
    clear_quiet_hours: bool = False,
) -> JobAlert:
    alert = get_alert(db, profile_id, alert_id)

    new_frequency = frequency if frequency is not None else alert.frequency
    new_day = day_of_week if day_of_week is not None else alert.day_of_week
    new_hour = hour_local if hour_local is not None else alert.hour_local
    if clear_quiet_hours:
        new_quiet_start = new_quiet_end = None
    else:
        new_quiet_start = quiet_hours_start if quiet_hours_start is not None else alert.quiet_hours_start
        new_quiet_end = quiet_hours_end if quiet_hours_end is not None else alert.quiet_hours_end
    _validate(new_frequency, new_day, new_hour, new_quiet_start, new_quiet_end)

    if name is not None:
        alert.name = name.strip()
    alert.frequency = new_frequency
    alert.day_of_week = new_day
    alert.hour_local = new_hour
    alert.quiet_hours_start = new_quiet_start
    alert.quiet_hours_end = new_quiet_end
    if is_enabled is not None:
        alert.is_enabled = is_enabled
    alert.updated_at = _utcnow()
    db.commit()
    db.refresh(alert)
    return alert


def delete_alert(db: Session, profile_id: int, alert_id: int) -> None:
    alert = get_alert(db, profile_id, alert_id)
    # Deliveries go first: they reference the alert, and leaving them would both violate the
    # foreign key and keep a record of notifications for an alert the student has deleted.
    db.query(JobAlertDelivery).filter(JobAlertDelivery.alert_id == alert.id).delete(
        synchronize_session=False
    )
    db.delete(alert)
    db.commit()


def list_history(
    db: Session, profile_id: int, limit: int = 50
) -> List[JobAlertDelivery]:
    return (
        db.query(JobAlertDelivery)
        .filter(JobAlertDelivery.profile_id == profile_id)
        .order_by(JobAlertDelivery.notified_at.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )


def in_quiet_hours(alert: JobAlert, hour: int) -> bool:
    """Whether `hour` falls inside the alert's quiet window.

    Handles a window that wraps midnight (22 -> 7) as one range rather than rejecting it, since
    that is the shape almost every student actually wants. Start and end are both inclusive of
    the start hour and exclusive of the end hour, so 22->7 means "do not disturb from 22:00
    until 07:00".
    """
    start, end = alert.quiet_hours_start, alert.quiet_hours_end
    if start is None or end is None:
        return False
    if start == end:
        # A zero-width window. Read as "no quiet hours" rather than "always quiet", because
        # the latter would silently disable an alert the student believes is running.
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def is_due(alert: JobAlert, now: datetime.datetime) -> bool:
    """Whether this alert should run at `now`.

    Uses last_run_at rather than a stored "next run" so that a worker which was down for a day
    catches up once instead of firing every missed run in a burst -- a student coming back
    online should get one notification, not yesterday's as well.
    """
    if not alert.is_enabled:
        return False
    if alert.frequency == "weekly":
        if alert.day_of_week is not None and now.weekday() != alert.day_of_week:
            return False
        min_gap = datetime.timedelta(days=6)
    else:
        min_gap = datetime.timedelta(hours=20)

    if now.hour < alert.hour_local:
        return False
    if alert.last_run_at is not None and (now - alert.last_run_at) < min_gap:
        return False
    return True


def run_alert(
    db: Session,
    alert: JobAlert,
    profile: StudentProfile,
    provider: JobProvider,
    now: Optional[datetime.datetime] = None,
) -> Tuple[str, int]:
    """Run one alert. Returns (status, new_job_count).

    Never raises for a provider failure: an alert run is background work, and a provider being
    down is an expected condition to be recorded, not an exception to propagate into whatever
    scheduler called this. It does record the failure, so the student can be told the
    difference between "nothing new" and "we couldn't look".
    """
    now = now or _utcnow()
    alert.last_run_at = now

    if in_quiet_hours(alert, now.hour):
        # Deliberately not "run it and hold the notification": running would consume provider
        # quota and mark the jobs as already-seen, so the student would never hear about them.
        alert.last_run_status = "skipped_quiet_hours"
        db.commit()
        return "skipped_quiet_hours", 0

    filters = _filters_for(db, alert)
    try:
        outcome = job_hunt_service.search(
            db,
            profile,
            provider,
            keywords=filters.get("keywords"),
            location=filters.get("location"),
            work_mode=filters.get("work_mode"),
            employment_type=filters.get("employment_type"),
            experience_level=filters.get("experience_level"),
            salary_min=filters.get("salary_min"),
            max_days_old=filters.get("max_days_old"),
            page=1,
            page_size=20,
        )
    except JobProviderError:
        # No provider text reaches the student, here or anywhere else.
        alert.last_error_at = now
        alert.last_run_status = "provider_error"
        db.commit()
        return "provider_error", 0
    except job_hunt_service.SearchProfileIncompleteError:
        alert.last_error_at = now
        alert.last_run_status = "not_configured"
        db.commit()
        return "not_configured", 0

    new_listings = []
    for listing in outcome.listings:
        delivery = JobAlertDelivery(
            alert_id=alert.id, profile_id=alert.profile_id, listing_id=listing.id
        )
        db.add(delivery)
        try:
            db.flush()
        except IntegrityError:
            # Already announced. The constraint is the check -- see the module docstring.
            db.rollback()
            continue
        new_listings.append(listing)

    alert.last_success_at = now
    alert.last_new_job_count = len(new_listings)
    alert.last_run_status = "success" if new_listings else "no_new_jobs"

    if new_listings:
        shown = new_listings[:MAX_NOTIFIED_PER_RUN]
        lead = shown[0]
        extra = len(new_listings) - len(shown)
        title = f"{len(new_listings)} new job{'s' if len(new_listings) != 1 else ''} for {alert.name}"
        body = ", ".join(
            filter(None, [listing.title for listing in shown])
        )
        if extra > 0:
            body = f"{body} and {extra} more"
        elif lead.company:
            body = f"{body} — {lead.company}"
        notification_service.enqueue(
            db, alert.profile_id, kind="job_alert", title=title, body=body
        )

    db.commit()
    return alert.last_run_status, len(new_listings)


def _filters_for(db: Session, alert: JobAlert) -> dict:
    """The filters this alert searches with.

    Read live from the saved search when there is one, so editing the search changes what the
    alert looks for. A copy taken at creation time would drift, and the student would have no
    way to see which set the alert was actually using.
    """
    if alert.saved_search_id is not None:
        search = db.get(JobSavedSearch, alert.saved_search_id)
        if search is not None:
            return dict(search.filters or {})
    return dict(alert.filters or {})


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
