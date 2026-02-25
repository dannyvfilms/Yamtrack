from django.test import TestCase

from app import credits
from app.models import (
    CreditRoleType,
    Item,
    ItemPersonCredit,
    ItemStudioCredit,
    MediaTypes,
    Person,
    Sources,
    Studio,
)


class AuthorCreditSyncTests(TestCase):
    def setUp(self):
        self.item = Item.objects.create(
            media_id="book-1",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Book One",
            image="http://example.com/book-one.jpg",
        )

    def test_sync_item_author_credits_creates_people_and_credits(self):
        credits.sync_item_author_credits(
            self.item,
            [
                {
                    "person_id": "OL1A",
                    "name": "Author One",
                    "image": "http://example.com/author1.jpg",
                    "role": "Author",
                    "sort_order": 0,
                },
                {
                    "person_id": "OL2A",
                    "name": "Author Two",
                    "image": "http://example.com/author2.jpg",
                    "role": "Co-Author",
                    "sort_order": 1,
                },
            ],
        )

        self.assertEqual(
            Person.objects.filter(source=Sources.OPENLIBRARY.value).count(),
            2,
        )
        author_credits = ItemPersonCredit.objects.filter(
            item=self.item,
            role_type=CreditRoleType.AUTHOR.value,
        ).order_by("sort_order")
        self.assertEqual(author_credits.count(), 2)
        self.assertEqual(
            list(author_credits.values_list("person__source_person_id", flat=True)),
            ["OL1A", "OL2A"],
        )

    def test_sync_item_author_credits_replaces_only_author_role_rows(self):
        cast_person = Person.objects.create(
            source=Sources.OPENLIBRARY.value,
            source_person_id="CAST1",
            name="Narrator",
        )
        old_author = Person.objects.create(
            source=Sources.OPENLIBRARY.value,
            source_person_id="OLD1",
            name="Old Author",
        )
        ItemPersonCredit.objects.create(
            item=self.item,
            person=cast_person,
            role_type=CreditRoleType.CAST.value,
            role="Narrator",
        )
        ItemPersonCredit.objects.create(
            item=self.item,
            person=old_author,
            role_type=CreditRoleType.AUTHOR.value,
            role="Author",
        )

        credits.sync_item_author_credits(
            self.item,
            [
                {
                    "person_id": "OLNEW",
                    "name": "New Author",
                    "image": "http://example.com/new-author.jpg",
                    "role": "Author",
                    "sort_order": 0,
                },
            ],
        )

        self.assertEqual(
            ItemPersonCredit.objects.filter(
                item=self.item,
                role_type=CreditRoleType.CAST.value,
            ).count(),
            1,
        )
        author_credits = ItemPersonCredit.objects.filter(
            item=self.item,
            role_type=CreditRoleType.AUTHOR.value,
        )
        self.assertEqual(author_credits.count(), 1)
        self.assertEqual(author_credits.first().person.source_person_id, "OLNEW")


class CreditSyncSourceTests(TestCase):
    def test_sync_item_credits_from_metadata_uses_item_source(self):
        item = Item.objects.create(
            media_id="book-2",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="Book Two",
            image="http://example.com/book-two.jpg",
        )

        credits.sync_item_credits_from_metadata(
            item,
            {
                "cast": [
                    {
                        "person_id": "100",
                        "name": "Reader Person",
                        "role": "Reader",
                    },
                ],
                "crew": [],
                "studios_full": [
                    {
                        "studio_id": "200",
                        "name": "Publisher House",
                    },
                ],
            },
        )

        person = Person.objects.get(source_person_id="100")
        studio = Studio.objects.get(source_studio_id="200")
        self.assertEqual(person.source, Sources.HARDCOVER.value)
        self.assertEqual(studio.source, Sources.HARDCOVER.value)
        self.assertTrue(
            ItemPersonCredit.objects.filter(
                item=item,
                person=person,
                role_type=CreditRoleType.CAST.value,
            ).exists(),
        )
        self.assertTrue(
            ItemStudioCredit.objects.filter(item=item, studio=studio).exists(),
        )

    def test_upsert_person_profile_supports_non_tmdb_sources(self):
        person = credits.upsert_person_profile(
            Sources.OPENLIBRARY.value,
            "OL11A",
            {
                "name": "Open Author",
                "image": "http://example.com/author.jpg",
                "known_for_department": "Author",
                "biography": "Author bio",
                "gender": "unknown",
                "birth_date": "1965-01-02",
                "death_date": None,
                "place_of_birth": "London",
            },
        )

        self.assertIsNotNone(person)
        person = Person.objects.get(source=Sources.OPENLIBRARY.value, source_person_id="OL11A")
        self.assertEqual(person.source, Sources.OPENLIBRARY.value)
        self.assertEqual(person.source_person_id, "OL11A")
        self.assertEqual(person.name, "Open Author")
        self.assertEqual(person.biography, "Author bio")
