from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from unittest.mock import patch

from app.discover import cache_repo
from app.discover.profile import compute_taste_profile, get_or_compute_taste_profile
from app.models import (
    Anime,
    CreditRoleType,
    DiscoverFeedback,
    DiscoverFeedbackType,
    Item,
    ItemPersonCredit,
    ItemStudioCredit,
    ItemTag,
    MediaTypes,
    Movie,
    Person,
    Sources,
    Status,
    Studio,
    TV,
    Tag,
)


class DiscoverProfileTests(TestCase):
    """Tests for taste profile computation."""

    def setUp(self):
        self.signal_patches = [
            patch("app.signals._handle_media_cache_change"),
            patch("app.signals._sync_owner_smart_lists_for_items"),
            patch("app.signals._schedule_credits_backfill_if_needed"),
            patch("app.models.Item.fetch_releases"),
        ]
        for patcher in self.signal_patches:
            patcher.start()

        self.user = get_user_model().objects.create_user(username="profile-user", password="testpass")

    def tearDown(self):
        for patcher in reversed(self.signal_patches):
            patcher.stop()

    def test_compute_taste_profile_prefers_recent_high_weight_genres(self):
        recent_item = Item.objects.create(
            media_id="201",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Recent Action",
            image="http://example.com/recent.jpg",
            genres=["Action"],
        )
        old_item = Item.objects.create(
            media_id="202",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Old Romance",
            image="http://example.com/old.jpg",
            genres=["Romance"],
        )

        with patch("app.models.providers.services.get_media_metadata", return_value={"max_progress": 1}):
            Movie.objects.create(
                item=recent_item,
                user=self.user,
                score=9,
                status=Status.COMPLETED.value,
                end_date=timezone.now() - timedelta(days=2),
            )
            Movie.objects.create(
                item=old_item,
                user=self.user,
                score=5,
                status=Status.COMPLETED.value,
                end_date=timezone.now() - timedelta(days=300),
            )

        tag = Tag.objects.create(user=self.user, name="Heist")
        ItemTag.objects.create(tag=tag, item=recent_item)

        profile = compute_taste_profile(self.user, MediaTypes.MOVIE.value)

        self.assertIn("action", profile.genre_affinity)
        self.assertGreater(profile.genre_affinity["action"], profile.genre_affinity.get("romance", 0))
        self.assertIn("heist", profile.tag_affinity)

    def test_compute_taste_profile_tv_works_without_end_date_field(self):
        tv_item = Item.objects.create(
            media_id="501",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="A TV Show",
            image="http://example.com/tv.jpg",
            genres=["Drama"],
        )
        TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            score=8,
        )

        profile = compute_taste_profile(self.user, MediaTypes.TV.value)

        self.assertIn("drama", profile.genre_affinity)

    def test_compute_taste_profile_phase_and_tag_affinity(self):
        recent_item = Item.objects.create(
            media_id="601",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Recent Cozy",
            image="http://example.com/recent-cozy.jpg",
            genres=["Animation"],
        )
        phase_item = Item.objects.create(
            media_id="602",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Phase Musical",
            image="http://example.com/phase-musical.jpg",
            genres=["Musical"],
        )
        old_item = Item.objects.create(
            media_id="603",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Old Classic",
            image="http://example.com/old-classic.jpg",
            genres=["Western"],
        )

        with patch("app.models.providers.services.get_media_metadata", return_value={"max_progress": 1}):
            Movie.objects.create(
                item=recent_item,
                user=self.user,
                score=8,
                status=Status.COMPLETED.value,
                end_date=timezone.now() - timedelta(days=10),
            )
            Movie.objects.create(
                item=phase_item,
                user=self.user,
                score=8,
                status=Status.COMPLETED.value,
                end_date=timezone.now() - timedelta(days=60),
            )
            Movie.objects.create(
                item=old_item,
                user=self.user,
                score=8,
                status=Status.COMPLETED.value,
                end_date=timezone.now() - timedelta(days=140),
            )

        cozy_tag = Tag.objects.create(user=self.user, name="Cozy")
        phase_tag = Tag.objects.create(user=self.user, name="Singalong")
        old_tag = Tag.objects.create(user=self.user, name="Classic")
        ItemTag.objects.create(tag=cozy_tag, item=recent_item)
        ItemTag.objects.create(tag=phase_tag, item=phase_item)
        ItemTag.objects.create(tag=old_tag, item=old_item)

        profile = compute_taste_profile(self.user, MediaTypes.MOVIE.value)

        self.assertIn("animation", profile.recent_genre_affinity)
        self.assertNotIn("musical", profile.recent_genre_affinity)
        self.assertIn("animation", profile.phase_genre_affinity)
        self.assertIn("musical", profile.phase_genre_affinity)
        self.assertNotIn("western", profile.phase_genre_affinity)

        self.assertIn("cozy", profile.recent_tag_affinity)
        self.assertNotIn("singalong", profile.recent_tag_affinity)
        self.assertIn("cozy", profile.phase_tag_affinity)
        self.assertIn("singalong", profile.phase_tag_affinity)
        self.assertNotIn("classic", profile.phase_tag_affinity)

    def test_compute_taste_profile_includes_negative_affinities_for_same_media_type(self):
        movie_item = Item.objects.create(
            media_id="701",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Dismissed Sci-Fi",
            image="http://example.com/dismissed-sci-fi.jpg",
            genres=["Sci-Fi"],
        )
        tv_item = Item.objects.create(
            media_id="702",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Dismissed TV",
            image="http://example.com/dismissed-tv.jpg",
            genres=["Mystery"],
        )
        tag = Tag.objects.create(user=self.user, name="Too Slow")
        ItemTag.objects.create(tag=tag, item=movie_item)
        person = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="person-negative",
            name="Actor Negative",
        )
        ItemPersonCredit.objects.create(
            item=movie_item,
            person=person,
            role_type="cast",
            role="Lead",
        )
        DiscoverFeedback.objects.create(
            user=self.user,
            item=movie_item,
            feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
        )
        DiscoverFeedback.objects.create(
            user=self.user,
            item=tv_item,
            feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
        )

        profile = compute_taste_profile(self.user, MediaTypes.MOVIE.value)

        self.assertIn("sci-fi", profile.negative_genre_affinity)
        self.assertIn("too slow", profile.negative_tag_affinity)
        self.assertIn("actor negative", profile.negative_person_affinity)
        self.assertNotIn("mystery", profile.negative_genre_affinity)

    def test_compute_taste_profile_populates_movie_metadata_affinities(self):
        item = Item.objects.create(
            media_id="801",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Family Mystery",
            image="http://example.com/family-mystery.jpg",
            genres=["Animation", "Mystery"],
            provider_keywords=["Whodunit", "Holiday"],
            provider_certification="PG",
            provider_collection_id="123",
            provider_collection_name="Mystery Collection",
            runtime_minutes=102,
            release_datetime=timezone.now() - timedelta(days=365 * 2),
            studios=["Pixar Animation Studios"],
        )
        director = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="director-1",
            name="Greta Gerwig",
        )
        lead = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="actor-1",
            name="Amy Poehler",
        )
        studio = Studio.objects.create(
            source=Sources.TMDB.value,
            source_studio_id="studio-1",
            name="Pixar Animation Studios",
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=director,
            role_type=CreditRoleType.CREW.value,
            role="Director",
            department="Directing",
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=lead,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
            sort_order=0,
        )
        ItemStudioCredit.objects.create(item=item, studio=studio)

        with patch("app.models.providers.services.get_media_metadata", return_value={"max_progress": 1}):
            Movie.objects.create(
                item=item,
                user=self.user,
                score=9,
                status=Status.COMPLETED.value,
                end_date=timezone.now() - timedelta(days=12),
            )

        profile = compute_taste_profile(self.user, MediaTypes.MOVIE.value)

        self.assertIn("whodunit", profile.keyword_affinity)
        self.assertIn("pixar", profile.studio_affinity)
        self.assertIn("mystery collection", profile.collection_affinity)
        self.assertIn("greta gerwig", profile.director_affinity)
        self.assertIn("amy poehler", profile.lead_cast_affinity)
        self.assertIn("PG", profile.certification_affinity)
        self.assertIn("90_109", profile.runtime_bucket_affinity)
        self.assertIn("2020s", profile.decade_affinity)
        self.assertIn("whodunit", profile.phase_keyword_affinity)

    def test_compute_taste_profile_builds_movie_comfort_library_and_rewatch_bundles(self):
        item = Item.objects.create(
            media_id="802",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Repeat Comfort",
            image="http://example.com/repeat-comfort.jpg",
            genres=["Animation", "Family"],
            provider_keywords=["Whodunit", "Holiday"],
            provider_certification="PG",
            provider_collection_id="124",
            provider_collection_name="Mystery Collection",
            runtime_minutes=99,
            release_datetime=timezone.now() - timedelta(days=365 * 3),
            studios=["Pixar Animation Studios"],
        )
        director = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="director-2",
            name="Pete Docter",
        )
        lead = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="actor-2",
            name="Amy Poehler",
        )
        studio = Studio.objects.create(
            source=Sources.TMDB.value,
            source_studio_id="studio-2",
            name="Pixar Animation Studios",
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=director,
            role_type=CreditRoleType.CREW.value,
            role="Director",
            department="Directing",
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=lead,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
            sort_order=0,
        )
        ItemStudioCredit.objects.create(item=item, studio=studio)

        with patch("app.models.providers.services.get_media_metadata", return_value={"max_progress": 1}):
            Movie.objects.create(
                item=item,
                user=self.user,
                score=10,
                status=Status.COMPLETED.value,
                end_date=timezone.now() - timedelta(days=30),
            )
            Movie.objects.create(
                item=item,
                user=self.user,
                score=9,
                status=Status.COMPLETED.value,
                end_date=timezone.now() - timedelta(days=180),
            )

        profile = compute_taste_profile(self.user, MediaTypes.MOVIE.value)

        self.assertIn("whodunit", profile.comfort_library_affinity["keywords"])
        self.assertIn("pixar", profile.comfort_library_affinity["studios"])
        self.assertIn("mystery collection", profile.comfort_library_affinity["collections"])
        self.assertIn("pete docter", profile.comfort_library_affinity["directors"])
        self.assertIn("PG", profile.comfort_library_affinity["certifications"])
        self.assertIn("90_109", profile.comfort_library_affinity["runtime_buckets"])
        self.assertIn("whodunit", profile.comfort_rewatch_affinity["keywords"])
        self.assertIn("pixar", profile.comfort_rewatch_affinity["studios"])

    def test_compute_taste_profile_builds_tv_comfort_library_bundle(self):
        item = Item.objects.create(
            media_id="tv-802",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Repeat Comfort Show",
            image="http://example.com/repeat-comfort-show.jpg",
            genres=["Animation", "Family"],
            provider_keywords=["Whodunit", "Holiday"],
            provider_certification="PG",
            provider_collection_id="224",
            provider_collection_name="Mystery Collection",
            runtime_minutes=52,
            release_datetime=timezone.now() - timedelta(days=365 * 3),
            studios=["Pixar Animation Studios"],
        )
        director = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="tv-director-2",
            name="Pete Docter",
        )
        lead = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="tv-actor-2",
            name="Amy Poehler",
        )
        studio = Studio.objects.create(
            source=Sources.TMDB.value,
            source_studio_id="tv-studio-2",
            name="Pixar Animation Studios",
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=director,
            role_type=CreditRoleType.CREW.value,
            role="Director",
            department="Directing",
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=lead,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
            sort_order=0,
        )
        ItemStudioCredit.objects.create(item=item, studio=studio)

        with patch("app.models.providers.services.get_media_metadata", return_value={"max_progress": 1}):
            TV.objects.create(
                item=item,
                user=self.user,
                score=10,
                status=Status.COMPLETED.value,
            )

        profile = compute_taste_profile(self.user, MediaTypes.TV.value)

        self.assertIn("whodunit", profile.keyword_affinity)
        self.assertIn("pixar", profile.studio_affinity)
        self.assertIn("mystery collection", profile.collection_affinity)
        self.assertIn("pete docter", profile.director_affinity)
        self.assertIn("amy poehler", profile.lead_cast_affinity)
        self.assertIn("PG", profile.certification_affinity)
        self.assertIn("<90", profile.runtime_bucket_affinity)
        self.assertIn("2020s", profile.decade_affinity)
        self.assertIn("whodunit", profile.comfort_library_affinity["keywords"])
        self.assertIn("pixar", profile.comfort_library_affinity["studios"])
        self.assertIn("mystery collection", profile.comfort_library_affinity["collections"])
        self.assertIn("pete docter", profile.comfort_library_affinity["directors"])
        self.assertIn("PG", profile.comfort_library_affinity["certifications"])
        self.assertIn("<90", profile.comfort_library_affinity["runtime_buckets"])

    def test_compute_taste_profile_builds_anime_comfort_library_and_rewatch_bundles(self):
        item = Item.objects.create(
            media_id="anime-802",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Repeat Comfort Anime",
            image="http://example.com/repeat-comfort-anime.jpg",
            genres=["Animation", "Fantasy"],
            provider_keywords=["Found Family", "Holiday"],
            provider_certification="PG",
            provider_collection_id="324",
            provider_collection_name="Magic Collection",
            runtime_minutes=24,
            release_datetime=timezone.now() - timedelta(days=365 * 4),
            studios=["Studio Pierrot"],
        )
        director = Person.objects.create(
            source=Sources.MAL.value,
            source_person_id="anime-director-2",
            name="Hayao Miyazaki",
        )
        lead = Person.objects.create(
            source=Sources.MAL.value,
            source_person_id="anime-actor-2",
            name="Maaya Sakamoto",
        )
        studio = Studio.objects.create(
            source=Sources.MAL.value,
            source_studio_id="anime-studio-2",
            name="Studio Pierrot",
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=director,
            role_type=CreditRoleType.CREW.value,
            role="Director",
            department="Directing",
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=lead,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
            sort_order=0,
        )
        ItemStudioCredit.objects.create(item=item, studio=studio)

        with patch("app.models.providers.services.get_media_metadata", return_value={"max_progress": 1}):
            Anime.objects.create(
                item=item,
                user=self.user,
                score=10,
                status=Status.COMPLETED.value,
                end_date=timezone.now() - timedelta(days=30),
            )
            Anime.objects.create(
                item=item,
                user=self.user,
                score=9,
                status=Status.COMPLETED.value,
                end_date=timezone.now() - timedelta(days=180),
            )

        profile = compute_taste_profile(self.user, MediaTypes.ANIME.value)

        self.assertIn("found family", profile.keyword_affinity)
        self.assertIn("studio pierrot", profile.studio_affinity)
        self.assertIn("magic collection", profile.collection_affinity)
        self.assertIn("hayao miyazaki", profile.director_affinity)
        self.assertIn("maaya sakamoto", profile.lead_cast_affinity)
        self.assertIn("PG", profile.certification_affinity)
        self.assertIn("<90", profile.runtime_bucket_affinity)
        self.assertIn("2020s", profile.decade_affinity)
        self.assertIn("found family", profile.comfort_library_affinity["keywords"])
        self.assertIn("studio pierrot", profile.comfort_library_affinity["studios"])
        self.assertIn("magic collection", profile.comfort_library_affinity["collections"])
        self.assertIn("hayao miyazaki", profile.comfort_library_affinity["directors"])
        self.assertIn("PG", profile.comfort_library_affinity["certifications"])
        self.assertIn("<90", profile.comfort_library_affinity["runtime_buckets"])
        self.assertIn("found family", profile.comfort_rewatch_affinity["keywords"])
        self.assertIn("studio pierrot", profile.comfort_rewatch_affinity["studios"])

    def test_get_or_compute_taste_profile_reuses_cached_movie_profile(self):
        item = Item.objects.create(
            media_id="804",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Cached Profile Movie",
            image="http://example.com/cached-profile.jpg",
            genres=["Comedy"],
        )
        with patch("app.models.providers.services.get_media_metadata", return_value={"max_progress": 1}):
            movie = Movie.objects.create(
                item=item,
                user=self.user,
                score=8,
                status=Status.COMPLETED.value,
                progressed_at=timezone.now() - timedelta(hours=1),
            )

        now = timezone.now()
        Movie.objects.filter(id=movie.id).update(
            created_at=now,
            progressed_at=now - timedelta(microseconds=100),
        )

        get_or_compute_taste_profile(self.user, MediaTypes.MOVIE.value, force=True)

        with patch("app.discover.profile.compute_taste_profile") as mock_compute:
            cached = get_or_compute_taste_profile(
                self.user,
                MediaTypes.MOVIE.value,
                force=False,
            )

        mock_compute.assert_not_called()
        self.assertIn("comedy", cached["genre_affinity"])

    def test_get_or_compute_taste_profile_returns_cached_world_rating_profile(self):
        now = timezone.now()
        with patch("app.models.providers.services.get_media_metadata", return_value={"max_progress": 1}):
            for index, (user_score, provider_rating) in enumerate(
                [(9, 8.9), (8, 8.2), (7, 7.5), (6, 6.8), (5, 6.1)],
                start=1,
            ):
                item = Item.objects.create(
                    media_id=f"world-cache-{index}",
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.MOVIE.value,
                    title=f"World Cache {index}",
                    image=f"http://example.com/world-cache-{index}.jpg",
                    genres=["Drama"],
                    provider_rating=provider_rating,
                    provider_rating_count=5000 + (index * 250),
                )
                Movie.objects.create(
                    item=item,
                    user=self.user,
                    score=user_score,
                    status=Status.COMPLETED.value,
                    end_date=now - timedelta(days=index * 7),
                )

        forced_profile = get_or_compute_taste_profile(
            self.user,
            MediaTypes.MOVIE.value,
            force=True,
        )
        with patch("app.discover.profile.compute_taste_profile") as mock_compute:
            cached_profile = get_or_compute_taste_profile(
                self.user,
                MediaTypes.MOVIE.value,
                force=False,
            )

        mock_compute.assert_not_called()
        self.assertEqual(
            forced_profile["world_rating_profile"]["sample_size"],
            5,
        )
        self.assertEqual(
            cached_profile["world_rating_profile"]["sample_size"],
            5,
        )
        self.assertGreater(
            cached_profile["world_rating_profile"]["confidence"],
            0.4,
        )

    def test_get_or_compute_taste_profile_recomputes_missing_world_rating_profile_backfill(self):
        now = timezone.now()
        with patch("app.models.providers.services.get_media_metadata", return_value={"max_progress": 1}):
            for index, (user_score, provider_rating) in enumerate(
                [(9, 8.9), (8, 8.2), (7, 7.5), (6, 6.8), (5, 6.1)],
                start=1,
            ):
                item = Item.objects.create(
                    media_id=f"world-backfill-{index}",
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.MOVIE.value,
                    title=f"World Backfill {index}",
                    image=f"http://example.com/world-backfill-{index}.jpg",
                    genres=["Drama"],
                    provider_rating=provider_rating,
                    provider_rating_count=6000 + (index * 300),
                )
                Movie.objects.create(
                    item=item,
                    user=self.user,
                    score=user_score,
                    status=Status.COMPLETED.value,
                    end_date=now - timedelta(days=index * 5),
                )

        cache_repo.set_taste_profile(
            self.user.id,
            MediaTypes.MOVIE.value,
            genre_affinity={},
            recent_genre_affinity={},
            phase_genre_affinity={},
            tag_affinity={},
            recent_tag_affinity={},
            phase_tag_affinity={},
            keyword_affinity={},
            recent_keyword_affinity={},
            phase_keyword_affinity={},
            studio_affinity={},
            recent_studio_affinity={},
            phase_studio_affinity={},
            collection_affinity={},
            recent_collection_affinity={},
            phase_collection_affinity={},
            director_affinity={},
            recent_director_affinity={},
            phase_director_affinity={},
            lead_cast_affinity={},
            recent_lead_cast_affinity={},
            phase_lead_cast_affinity={},
            certification_affinity={},
            recent_certification_affinity={},
            phase_certification_affinity={},
            runtime_bucket_affinity={},
            recent_runtime_bucket_affinity={},
            phase_runtime_bucket_affinity={},
            decade_affinity={},
            recent_decade_affinity={},
            phase_decade_affinity={},
            comfort_library_affinity={},
            comfort_rewatch_affinity={},
            person_affinity={},
            negative_genre_affinity={},
            negative_tag_affinity={},
            negative_person_affinity={},
            world_rating_profile={},
            activity_snapshot_at=now,
            ttl_seconds=3600,
        )

        profile = get_or_compute_taste_profile(
            self.user,
            MediaTypes.MOVIE.value,
            force=False,
        )

        self.assertEqual(profile["world_rating_profile"]["sample_size"], 5)
        self.assertGreater(profile["world_rating_profile"]["confidence"], 0.4)

    def test_compute_taste_profile_snapshot_includes_feedback_updates(self):
        item = Item.objects.create(
            media_id="805",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Feedback Snapshot Movie",
            image="http://example.com/feedback-snapshot.jpg",
            genres=["Mystery"],
        )
        with patch("app.models.providers.services.get_media_metadata", return_value={"max_progress": 1}):
            Movie.objects.create(
                item=item,
                user=self.user,
                score=7,
                status=Status.COMPLETED.value,
                end_date=timezone.now() - timedelta(days=10),
            )

        feedback = DiscoverFeedback.objects.create(
            user=self.user,
            item=item,
            feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
        )
        feedback_updated_at = timezone.now()
        DiscoverFeedback.objects.filter(id=feedback.id).update(
            created_at=feedback_updated_at - timedelta(minutes=1),
            updated_at=feedback_updated_at,
        )

        profile = compute_taste_profile(self.user, MediaTypes.MOVIE.value)

        self.assertEqual(profile.activity_snapshot_at, feedback_updated_at)
