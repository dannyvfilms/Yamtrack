# ruff: noqa: D101, D102

import fnmatch
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from app import celery_broker


class _FakeRedisPipeline:
    def __init__(self, client):
        self.client = client
        self.operations = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def srem(self, key, *members):
        self.operations.append(("srem", key, members))
        return self

    def sadd(self, key, *members):
        self.operations.append(("sadd", key, members))
        return self

    def execute(self):
        for operation, key, members in self.operations:
            bucket = self.client.data.setdefault(key, set())
            if operation == "srem":
                bucket.difference_update(members)
            elif operation == "sadd":
                bucket.update(members)
        return []


class _FakeRedisClient:
    def __init__(self, data):
        self.data = {key: set(values) for key, values in data.items()}

    def scan_iter(self, match=None):
        for key in self.data:
            if match is None or fnmatch.fnmatch(key, match):
                yield key

    def smembers(self, key):
        return set(self.data.get(key, set()))

    def pipeline(self):
        return _FakeRedisPipeline(self)


class CeleryBrokerRepairTests(SimpleTestCase):
    def test_normalize_kombu_binding_member_repairs_default_separator(self):
        legacy_member = celery_broker.KOMBU_REDIS_DEFAULT_SEPARATOR.join(
            [
                "reply.celery.pidbox",
                "",
                "celery@worker.celery.pidbox",
            ],
        )

        normalized = celery_broker.normalize_kombu_binding_member(
            legacy_member,
            desired_separator=":",
        )

        self.assertEqual(
            normalized,
            "reply.celery.pidbox::celery@worker.celery.pidbox",
        )

    @override_settings(
        CELERY_BROKER_URL="redis://example:6379/0",
        CELERY_BROKER_TRANSPORT_OPTIONS={
            "sep": ":",
            "global_keyprefix": "yamtrack_",
        },
    )
    @patch("app.celery_broker.redis.Redis.from_url")
    def test_repair_celery_redis_bindings_rewrites_legacy_members(
        self,
        mock_from_url,
    ):
        legacy_member = celery_broker.KOMBU_REDIS_DEFAULT_SEPARATOR.join(
            [
                "reply.celery.pidbox",
                "",
                "celery@worker.celery.pidbox",
            ],
        )
        key = "yamtrack__kombu.binding.reply.celery.pidbox"
        fake_client = _FakeRedisClient({key: {legacy_member}})
        mock_from_url.return_value = fake_client

        summary = celery_broker.repair_celery_redis_bindings()

        self.assertEqual(
            summary,
            {
                "keys": 1,
                "members": 1,
                "repaired": 1,
                "removed": 0,
            },
        )
        self.assertEqual(
            fake_client.data[key],
            {"reply.celery.pidbox::celery@worker.celery.pidbox"},
        )

    @override_settings(
        CELERY_BROKER_URL="redis://example:6379/0",
        CELERY_BROKER_TRANSPORT_OPTIONS={"sep": ":"},
    )
    @patch("app.celery_broker.redis.Redis.from_url")
    def test_repair_celery_redis_bindings_drops_malformed_members(
        self,
        mock_from_url,
    ):
        fake_client = _FakeRedisClient(
            {
                "_kombu.binding.reply.celery.pidbox": {
                    "invalid-binding-entry",
                },
            },
        )
        mock_from_url.return_value = fake_client

        summary = celery_broker.repair_celery_redis_bindings()

        self.assertEqual(summary["removed"], 1)
        self.assertEqual(
            fake_client.data["_kombu.binding.reply.celery.pidbox"],
            set(),
        )
