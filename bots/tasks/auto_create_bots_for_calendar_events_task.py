import logging

from django.db import transaction
from django.utils import timezone

from bots.models import Bot, BotStates, CalendarEvent, CalendarStates, Recording, TranscriptionTypes

logger = logging.getLogger(__name__)

# How far ahead to create bots for upcoming events
LOOKAHEAD_MINUTES = 30
# How many minutes before the meeting the bot should join
JOIN_BEFORE_MINUTES = 1


def auto_create_bots_for_calendar_events():
    """
    Create scheduled bots for upcoming calendar events that have meeting URLs
    and don't already have a bot.
    """
    now = timezone.now()
    lookahead = now + timezone.timedelta(minutes=LOOKAHEAD_MINUTES)

    # Find upcoming events with meeting URLs that don't already have an active bot
    events = CalendarEvent.objects.filter(
        calendar__state=CalendarStates.CONNECTED,
        start_time__gte=now,
        start_time__lte=lookahead,
        is_deleted=False,
    ).exclude(
        meeting_url__isnull=True,
    ).exclude(
        meeting_url="",
    ).select_related("calendar", "calendar__project")

    created_count = 0
    for event in events:
        # Skip if there's already a bot for this event (in any non-terminal state)
        existing_bot = event.bots.exclude(
            state__in=[BotStates.FATAL_ERROR, BotStates.ENDED]
        ).first()
        if existing_bot:
            continue

        try:
            join_at = event.start_time - timezone.timedelta(minutes=JOIN_BEFORE_MINUTES)
            if join_at < now:
                join_at = now

            with transaction.atomic():
                bot = Bot.objects.create(
                    project=event.calendar.project,
                    meeting_url=event.meeting_url,
                    name="Neusis Bot",
                    calendar_event=event,
                    join_at=join_at,
                    state=BotStates.SCHEDULED,
                    settings={
                        "recording_settings": {"format": "mp3"},
                        "automatic_leave_settings": {
                            "only_participant_in_meeting_timeout_seconds": 60,
                            "silence_timeout_seconds": 600,
                        },
                    },
                    deduplication_key=f"cal-{event.object_id}",
                )

                Recording.objects.create(
                    bot=bot,
                    recording_type=bot.recording_type(),
                    transcription_type=TranscriptionTypes.NON_REALTIME,
                    is_default_recording=True,
                )

            created_count += 1
            logger.info(
                "Auto-created bot %s for calendar event %s (%s) at %s",
                bot.object_id,
                event.object_id,
                event.name,
                event.start_time.isoformat(),
            )
        except Exception:
            logger.exception(
                "Failed to auto-create bot for calendar event %s (%s)",
                event.object_id,
                event.name,
            )

    if created_count:
        logger.info("Auto-created %d bots for upcoming calendar events", created_count)
