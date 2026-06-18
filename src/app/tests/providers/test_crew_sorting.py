"""Tests for crew sorting priority and created_by injection in TMDB provider.

Run with -v 2 to see the full ranked crew list printed for each title.
The printed output is the primary diagnostic — if a test passes but the
printed order looks wrong, the assertion is missing a case.
"""

from unittest.mock import patch

from django.test import TestCase

from app.providers import tmdb


def _print_crew(title, crew):
    """Print every crew member in sorted order so test logs are scannable."""
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")
    for i, c in enumerate(crew):
        print(f"  {i + 1:2}. [{c['role'] or '(no role)'}]  {c['name']}")
    print()


# ---------------------------------------------------------------------------
# Helpers to build TMDB-shaped payloads using real role/job names
# ---------------------------------------------------------------------------

def _movie_crew(*members):
    """Wrap crew member dicts in a TMDB credits envelope."""
    return {"crew": list(members)}


def _mc(person_id, name, job, department, order=None):
    """Build a non-aggregate (movie) crew member dict."""
    return {
        "id": person_id,
        "name": name,
        "profile_path": None,
        "known_for_department": department,
        "gender": 2,
        "department": department,
        "job": job,
        "order": order,
    }


def _tc(person_id, name, jobs, department, order=None):
    """Build a TV aggregate crew member dict (jobs is a list of job-name strings)."""
    return {
        "id": person_id,
        "name": name,
        "profile_path": None,
        "known_for_department": department,
        "gender": 2,
        "department": department,
        "order": order,
        "jobs": [{"job": j, "department": department} for j in jobs],
    }


def _cb(person_id, name):
    """Build a TMDB created_by entry."""
    return {"id": person_id, "name": name, "profile_path": None, "gender": 2}


def _minimal_tv_response(title, created_by_list, aggregate_crew):
    return {
        "id": 999,
        "name": title,
        "original_name": title,
        "overview": "",
        "poster_path": None,
        "vote_average": 8.0,
        "vote_count": 1000,
        "first_air_date": "2008-01-20",
        "last_air_date": "2013-09-29",
        "status": "Ended",
        "number_of_seasons": 5,
        "number_of_episodes": 62,
        "episode_run_time": [47],
        "genres": [],
        "production_companies": [],
        "production_countries": [],
        "spoken_languages": [],
        "next_episode_to_air": None,
        "last_episode_to_air": None,
        "external_ids": {},
        "alternative_titles": {"results": []},
        "keywords": {"results": []},
        "content_ratings": {"results": []},
        "watch/providers": {"results": {}},
        "recommendations": {"results": []},
        "seasons": [],
        "created_by": created_by_list,
        "aggregate_credits": {"cast": [], "crew": aggregate_crew},
    }


class MovieCrewSortingTests(TestCase):
    """
    Director and Screenplay must sort before all other roles on movies.

    Uses realistic TMDB job titles — the tricky cases are roles that
    *contain* the word "director" as a substring:
      - "Art Director"
      - "Supervising Art Director"
      - "Director of Photography"
    These must NOT steal priority from the actual Director.
    """

    def _sorted_crew(self, credits_dict):
        crew = tmdb.get_crew_credits(credits_dict)
        return crew

    def test_wake_up_dead_man(self):
        """
        Real-world bug: multiple art directors and DoP appeared before the actual Director.
        Crew in TMDB credits contains 'Art Director', 'Supervising Art Director',
        'Director of Photography', 'Screenplay', and 'Director' — Director must be #1.
        """
        credits = _movie_crew(
            _mc(10, "Paula Dal Santo", "Art Director", "Art", order=1),
            _mc(20, "Jane Brown", "Supervising Art Director", "Art", order=2),
            _mc(30, "Roger Deakins", "Director of Photography", "Camera", order=3),
            _mc(40, "Krysty Wilson-Cairns", "Screenplay", "Writing", order=4),
            _mc(50, "Sam Mendes", "Director", "Directing", order=5),
        )
        crew = self._sorted_crew(credits)
        _print_crew("Wake Up Dead Man", crew)

        roles = [c["role"] for c in crew]
        names = [c["name"] for c in crew]

        # Director must be first
        self.assertEqual(crew[0]["role"], "Director",
            f"Expected Director first, got: {list(zip(roles, names))}")
        self.assertEqual(crew[0]["name"], "Sam Mendes")

        # Screenplay second
        self.assertEqual(crew[1]["role"], "Screenplay",
            f"Expected Screenplay second, got: {list(zip(roles, names))}")

        # All non-priority roles must come after Screenplay
        priority_count = sum(1 for r in roles if r in ("Director", "Screenplay"))
        for i in range(priority_count, len(crew)):
            self.assertNotIn(crew[i]["role"], ("Director", "Screenplay"),
                f"Found priority role at position {i + 1}: {crew[i]}")

    def test_dune_part_two(self):
        """
        Denis Villeneuve appears as both Director and Screenplay.
        Art Director and DoP must fall after both of his entries.
        """
        credits = _movie_crew(
            _mc(11, "Claude Paré", "Art Director", "Art", order=1),
            _mc(12, "Greig Fraser", "Director of Photography", "Camera", order=2),
            _mc(13, "Patrice Vermette", "Production Designer", "Art", order=3),
            _mc(14, "Denis Villeneuve", "Screenplay", "Writing", order=4),
            _mc(14, "Denis Villeneuve", "Director", "Directing", order=5),
        )
        crew = self._sorted_crew(credits)
        _print_crew("Dune: Part Two", crew)

        roles = [c["role"] for c in crew]
        self.assertIn("Director", roles)
        director_idx = roles.index("Director")
        screenplay_idx = roles.index("Screenplay")

        for i, c in enumerate(crew):
            if c["role"] in ("Art Director", "Director of Photography", "Production Designer"):
                self.assertGreater(i, director_idx,
                    f"'{c['role']}' ({c['name']}) at position {i+1} is before Director at {director_idx+1}")
                self.assertGreater(i, screenplay_idx,
                    f"'{c['role']}' ({c['name']}) at position {i+1} is before Screenplay at {screenplay_idx+1}")

    def test_oppenheimer(self):
        """Director + Screenplay (same person, Christopher Nolan) both before Set Decorator."""
        credits = _movie_crew(
            _mc(15, "Ruth De Jong", "Set Decorator", "Art", order=1),
            _mc(16, "Hoyte Van Hoytema", "Director of Photography", "Camera", order=2),
            _mc(17, "Christopher Nolan", "Director", "Directing", order=3),
            _mc(17, "Christopher Nolan", "Screenplay", "Writing", order=4),
        )
        crew = self._sorted_crew(credits)
        _print_crew("Oppenheimer", crew)

        roles = [c["role"] for c in crew]
        director_idx = roles.index("Director")
        screenplay_idx = roles.index("Screenplay")
        dop_idx = roles.index("Director of Photography")
        dec_idx = roles.index("Set Decorator")

        self.assertLess(director_idx, dop_idx,
            f"Director at {director_idx+1} should be before DoP at {dop_idx+1}")
        self.assertLess(director_idx, dec_idx)
        self.assertLess(screenplay_idx, dop_idx)
        self.assertLess(screenplay_idx, dec_idx)

    def test_parasite(self):
        """'Original Screenplay' is a priority role; 'Art Director' and 'Director of Photography' are not."""
        credits = _movie_crew(
            _mc(20, "Lee Ha-jun", "Art Director", "Art", order=1),
            _mc(21, "Park Hyun-chul", "Supervising Art Director", "Art", order=2),
            _mc(22, "Hong Gyeong-pyo", "Director of Photography", "Camera", order=3),
            _mc(23, "Bong Joon-ho", "Original Screenplay", "Writing", order=4),
            _mc(23, "Bong Joon-ho", "Director", "Directing", order=5),
        )
        crew = self._sorted_crew(credits)
        _print_crew("Parasite", crew)

        roles = [c["role"] for c in crew]
        director_idx = roles.index("Director")
        screenplay_idx = roles.index("Original Screenplay")

        non_priority = [
            (i, c) for i, c in enumerate(crew)
            if c["role"] in ("Art Director", "Supervising Art Director", "Director of Photography")
        ]
        for i, c in non_priority:
            self.assertGreater(i, director_idx,
                f"'{c['role']}' ({c['name']}) at pos {i+1} is before Director at {director_idx+1}")
            self.assertGreater(i, screenplay_idx,
                f"'{c['role']}' ({c['name']}) at pos {i+1} is before Screenplay at {screenplay_idx+1}")

    def test_shawshank_redemption(self):
        """Baseline: Director then Screenplay, then all others regardless of TMDB order field."""
        credits = _movie_crew(
            # Give non-priority roles lower order numbers to ensure priority wins over order
            _mc(30, "Peter Smith", "Art Director", "Art", order=1),
            _mc(31, "Roger Deakins", "Director of Photography", "Camera", order=2),
            _mc(32, "Frank Darabont", "Director", "Directing", order=3),
            _mc(32, "Frank Darabont", "Screenplay", "Writing", order=4),
        )
        crew = self._sorted_crew(credits)
        _print_crew("The Shawshank Redemption", crew)

        self.assertEqual(crew[0]["role"], "Director",
            f"Expected Director at position 1, got: {[(c['role'], c['name']) for c in crew]}")
        self.assertEqual(crew[0]["name"], "Frank Darabont")
        self.assertEqual(crew[1]["role"], "Screenplay")

        roles = [c["role"] for c in crew]
        director_idx = roles.index("Director")
        art_dir_idx = roles.index("Art Director")
        dop_idx = roles.index("Director of Photography")
        self.assertLess(director_idx, art_dir_idx)
        self.assertLess(director_idx, dop_idx)


class TVCrewSortingTests(TestCase):
    """
    For TV shows:
    - Creator (from TMDB created_by) must appear first.
    - TMDB aggregate_credits crew for TV often does NOT include episode directors —
      they're per-episode, not show-level. The aggregate crew tends to have
      recurring dept heads: DoPs, Art Directors, Set Decorators, etc.
    - Creators should appear before all those roles.
    - If a Creator also appears in aggregate_credits under a different job, they
      must not be duplicated.
    """

    def _tv_crew(self, title, created_by_list, aggregate_crew):
        response = _minimal_tv_response(title, created_by_list, aggregate_crew)
        with patch("app.providers.tmdb.services.api_request", return_value=response):
            with patch("app.providers.tmdb.cache.get", return_value=None):
                with patch("app.providers.tmdb.cache.set"):
                    result = tmdb.process_tv(response, media_id="999")
        return result["crew"]

    def test_breaking_bad(self):
        """
        Real TMDB aggregate crew for Breaking Bad has DoPs and Art Directors,
        but no episode directors at the show level. Vince Gilligan (created_by)
        must appear first as Creator before all the dept-head crew.
        """
        created_by = [_cb(66633, "Vince Gilligan")]
        # Realistic aggregate crew (no Director entries — TMDB doesn't include them here)
        aggregate_crew = [
            _tc(1001, "Michael Slovis", ["Director of Photography"], "Camera", order=1),
            _tc(1002, "Reynaldo Villalobos", ["Director of Photography"], "Camera", order=2),
            _tc(1003, "Arthur Albert", ["Director of Photography"], "Camera", order=3),
            _tc(1004, "Paula Dal Santo", ["Assistant Art Director"], "Art", order=4),
            _tc(1005, "Nelson Cragg", ["Director of Photography"], "Camera", order=5),
        ]
        crew = self._tv_crew("Breaking Bad", created_by, aggregate_crew)
        _print_crew("Breaking Bad", crew)

        names = [c["name"] for c in crew]
        roles = [c["role"] for c in crew]

        # Creator must be first
        self.assertEqual(crew[0]["name"], "Vince Gilligan",
            f"Expected Vince Gilligan first. Got: {list(zip(names, roles))}")
        self.assertEqual(crew[0]["role"], "Creator")

        # All DoPs and Art Directors must come after Creator
        for i, c in enumerate(crew[1:], start=1):
            self.assertNotEqual(c["role"], "Creator",
                f"Unexpected Creator at position {i+1}: {c['name']}")

    def test_severance(self):
        """
        Dan Erickson (Creator) before Art Directors and DoP.
        'Assistant Art Director' must not be treated as a priority role.
        """
        created_by = [_cb(2001, "Dan Erickson")]
        aggregate_crew = [
            _tc(2002, "Ben Stiller", ["Director of Photography"], "Camera", order=1),
            _tc(2003, "Amy Chen", ["Assistant Art Director"], "Art", order=2),
            _tc(2004, "Lisa Park", ["Set Decorator"], "Art", order=3),
        ]
        crew = self._tv_crew("Severance", created_by, aggregate_crew)
        _print_crew("Severance", crew)

        names = [c["name"] for c in crew]
        roles = [c["role"] for c in crew]

        self.assertEqual(crew[0]["name"], "Dan Erickson",
            f"Creator should be first. Got: {list(zip(names, roles))}")
        self.assertEqual(crew[0]["role"], "Creator")

        # Verify non-priority roles don't sneak above Creator
        creator_idx = roles.index("Creator")
        for role in ("Director of Photography", "Assistant Art Director", "Set Decorator"):
            if role in roles:
                self.assertGreater(roles.index(role), creator_idx,
                    f"'{role}' appears before Creator")

    def test_the_bear_creator_deduplication(self):
        """
        Christopher Storer appears in both created_by AND aggregate_credits (as Screenplay).
        He must appear exactly once, as Creator (not as Screenplay), and must be first.
        """
        created_by = [_cb(3001, "Christopher Storer")]
        aggregate_crew = [
            # Same person in aggregate with a different job — must be deduplicated
            _tc(3001, "Christopher Storer", ["Screenplay"], "Writing", order=0),
            _tc(3002, "Joanna Calo", ["Director of Photography"], "Camera", order=1),
            _tc(3003, "Alice Birch", ["Writer"], "Writing", order=2),
            _tc(3004, "Bob Smith", ["Supervising Art Director"], "Art", order=3),
        ]
        crew = self._tv_crew("The Bear", created_by, aggregate_crew)
        _print_crew("The Bear", crew)

        storer_entries = [c for c in crew if c["name"] == "Christopher Storer"]
        roles = [c["role"] for c in crew]
        names = [c["name"] for c in crew]

        self.assertEqual(len(storer_entries), 1,
            f"Christopher Storer should appear once, found {len(storer_entries)}: {storer_entries}")
        self.assertEqual(storer_entries[0]["role"], "Creator",
            f"Storer's role should be Creator, got '{storer_entries[0]['role']}'")
        self.assertEqual(crew[0]["name"], "Christopher Storer",
            f"Creator must be first. Got: {list(zip(names, roles))}")

        # Writer (Alice Birch) must come before Art/Camera dept
        writer_idx = next((i for i, c in enumerate(crew) if c["role"] == "Writer"), None)
        sup_art_idx = next((i for i, c in enumerate(crew) if c["role"] == "Supervising Art Director"), None)
        if writer_idx is not None and sup_art_idx is not None:
            self.assertLess(writer_idx, sup_art_idx,
                f"Writer at {writer_idx+1} should be before Supervising Art Director at {sup_art_idx+1}")

    def test_succession_full_chain(self):
        """
        Full priority chain for a drama TV show:
        Creator → Writer → Supervising Art Director → Assistant Art Director.
        No episode directors in aggregate crew (realistic for TV).
        """
        created_by = [_cb(4001, "Jesse Armstrong")]
        aggregate_crew = [
            _tc(4002, "Mark Mylod", ["Director of Photography"], "Camera", order=2),
            _tc(4003, "Tony Roche", ["Writer"], "Writing", order=3),
            _tc(4004, "Alice Park", ["Assistant Art Director"], "Art", order=1),
            _tc(4005, "Lucy Forbes", ["Supervising Art Director"], "Art", order=0),
        ]
        crew = self._tv_crew("Succession", created_by, aggregate_crew)
        _print_crew("Succession", crew)

        names = [c["name"] for c in crew]
        roles = [c["role"] for c in crew]

        self.assertEqual(crew[0]["name"], "Jesse Armstrong",
            f"Creator must be first. Got: {list(zip(names, roles))}")
        self.assertEqual(crew[0]["role"], "Creator")

        creator_idx = roles.index("Creator")
        writer_idx = next((i for i, r in enumerate(roles) if r == "Writer"), None)
        sup_art_idx = next((i for i, r in enumerate(roles) if r == "Supervising Art Director"), None)
        asst_art_idx = next((i for i, r in enumerate(roles) if r == "Assistant Art Director"), None)

        self.assertIsNotNone(writer_idx, "Writer not found in crew")
        self.assertLess(creator_idx, writer_idx,
            f"Creator at {creator_idx+1} should be before Writer at {writer_idx+1}")
        if sup_art_idx is not None:
            self.assertLess(writer_idx, sup_art_idx,
                f"Writer at {writer_idx+1} should be before Supervising Art Director at {sup_art_idx+1}")
        if asst_art_idx is not None:
            self.assertLess(writer_idx, asst_art_idx)

    def test_fallout_multiple_creators(self):
        """
        Two creators (Graham Wagner, Geneva Robertson-Dworet).
        Both must appear before any other crew.
        No episode directors in aggregate crew — only Supervising Art Director and DoP.
        """
        created_by = [
            _cb(5001, "Graham Wagner"),
            _cb(5002, "Geneva Robertson-Dworet"),
        ]
        aggregate_crew = [
            _tc(5003, "Clare Kilner", ["Director of Photography"], "Camera", order=2),
            _tc(5004, "Kim Smith", ["Assistant Art Director"], "Art", order=1),
            _tc(5005, "Bob Davis", ["Supervising Art Director"], "Art", order=0),
        ]
        crew = self._tv_crew("Fallout", created_by, aggregate_crew)
        _print_crew("Fallout", crew)

        names = [c["name"] for c in crew]
        roles = [c["role"] for c in crew]

        creator_entries = [(i, c) for i, c in enumerate(crew) if c["role"] == "Creator"]
        self.assertEqual(len(creator_entries), 2,
            f"Expected 2 creators, found {len(creator_entries)}: {[(i+1, c['name']) for i,c in creator_entries]}")

        creator_names = {c["name"] for _, c in creator_entries}
        self.assertIn("Graham Wagner", creator_names)
        self.assertIn("Geneva Robertson-Dworet", creator_names)

        last_creator_idx = max(i for i, _ in creator_entries)
        non_creator_roles = [
            (i, c) for i, c in enumerate(crew) if c["role"] != "Creator"
        ]
        for i, c in non_creator_roles:
            self.assertGreater(i, last_creator_idx,
                f"'{c['role']}' ({c['name']}) at pos {i+1} appears before last Creator at {last_creator_idx+1}. "
                f"Full order: {list(zip(names, roles))}")
