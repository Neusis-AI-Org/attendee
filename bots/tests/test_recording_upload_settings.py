"""Tests for the recording_upload_settings field added by the PB integration fork.

Covers serializer-level validation (the lowest-friction surface to test) and the
Bot model accessor. The bot_controller upload path is covered by the existing
recording-upload integration tests once they're extended to drive the new code
path; not duplicated here.
"""

from django.test import TestCase
from rest_framework import serializers as drf_serializers

from bots.serializers import CreateBotSerializer


class RecordingUploadSettingsValidationTests(TestCase):
    def setUp(self):
        # validate_* methods don't need a fully-instantiated serializer; this
        # bare instance is enough to reach the method under test.
        self.serializer = CreateBotSerializer()

    def test_valid_https_upload_url_accepted(self):
        result = self.serializer.validate_recording_upload_settings(
            {"upload_url": "https://storage.googleapis.com/bucket/object?sig=abc"}
        )
        self.assertEqual(result["upload_url"], "https://storage.googleapis.com/bucket/object?sig=abc")

    def test_optional_content_type_accepted(self):
        result = self.serializer.validate_recording_upload_settings(
            {"upload_url": "https://example.com/upload", "content_type": "audio/mp4"}
        )
        self.assertEqual(result["content_type"], "audio/mp4")

    def test_none_passes_through(self):
        self.assertIsNone(self.serializer.validate_recording_upload_settings(None))

    def test_missing_upload_url_rejected(self):
        with self.assertRaises(drf_serializers.ValidationError):
            self.serializer.validate_recording_upload_settings({})

    def test_non_https_upload_url_rejected(self):
        with self.assertRaises(drf_serializers.ValidationError):
            self.serializer.validate_recording_upload_settings(
                {"upload_url": "http://example.com/upload"}
            )

    def test_unknown_property_rejected(self):
        with self.assertRaises(drf_serializers.ValidationError):
            self.serializer.validate_recording_upload_settings(
                {"upload_url": "https://example.com/upload", "bucket_name": "x"}
            )


class BotRecordingUploadAccessorTests(TestCase):
    """Bot model accessors don't require a saved row — they read from .settings."""

    def test_returns_none_when_not_set(self):
        from bots.models import Bot

        bot = Bot(settings={})
        self.assertIsNone(bot.recording_upload_url())
        self.assertEqual(bot.recording_upload_content_type(), "video/mp4")

    def test_returns_upload_url_when_set(self):
        from bots.models import Bot

        bot = Bot(settings={"recording_upload_settings": {"upload_url": "https://example.com/u"}})
        self.assertEqual(bot.recording_upload_url(), "https://example.com/u")

    def test_returns_custom_content_type_when_set(self):
        from bots.models import Bot

        bot = Bot(
            settings={
                "recording_upload_settings": {
                    "upload_url": "https://example.com/u",
                    "content_type": "audio/mp4",
                }
            }
        )
        self.assertEqual(bot.recording_upload_content_type(), "audio/mp4")

    def test_null_settings_value_treated_as_empty(self):
        from bots.models import Bot

        bot = Bot(settings={"recording_upload_settings": None})
        self.assertIsNone(bot.recording_upload_url())
        self.assertEqual(bot.recording_upload_content_type(), "video/mp4")
