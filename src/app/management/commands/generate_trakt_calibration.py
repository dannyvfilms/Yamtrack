"""Generate a Trakt popularity calibration fixture from real DB data."""

from __future__ import annotations

import json
import os

from django.core.management.base import BaseCommand

from app.models import Item, MediaTypes

# ---------------------------------------------------------------------------
# Hardcoded list of 250 titles with expected Trakt ranks
# ---------------------------------------------------------------------------
TITLES = [
    (1, "Deadpool"),
    (2, "Guardians of the Galaxy"),
    (3, "The Dark Knight"),
    (4, "Inception"),
    (5, "Logan"),
    (6, "Doctor Strange"),
    (7, "The Avengers"),
    (8, "Suicide Squad"),
    (9, "Wonder Woman"),
    (10, "Interstellar"),
    (11, "Arrival"),
    (12, "The Matrix"),
    (13, "Captain America: Civil War"),
    (14, "Star Wars: Episode VII - The Force Awakens"),
    (15, "Moana"),
    (16, "Avatar"),
    (17, "Guardians of the Galaxy Vol. 2"),
    (18, "The Martian"),
    (19, "Frozen"),
    (20, "Fantastic Beasts and Where to Find Them"),
    (21, "Rogue One: A Star Wars Story"),
    (22, "Iron Man"),
    (23, "Passengers"),
    (24, "Zootopia"),
    (25, "Fight Club"),
    (26, "The Lord of the Rings: The Fellowship of the Ring"),
    (27, "Spider-Man: Homecoming"),
    (28, "The Dark Knight Rises"),
    (29, "Batman v Superman: Dawn of Justice"),
    (30, "John Wick: Chapter 2"),
    (31, "The Wolf of Wall Street"),
    (32, "Mad Max: Fury Road"),
    (33, "Inside Out"),
    (34, "Django Unchained"),
    (35, "X-Men: Apocalypse"),
    (36, "The Lord of the Rings: The Return of the King"),
    (37, "Star Wars: Episode IV - A New Hope"),
    (38, "Kingsman: The Secret Service"),
    (39, "Captain America: The Winter Soldier"),
    (40, "Finding Nemo"),
    (41, "Ant-Man"),
    (42, "Avengers: Age of Ultron"),
    (43, "The Lord of the Rings: The Two Towers"),
    (44, "Jurassic World"),
    (45, "Thor: Ragnarok"),
    (46, "The Secret Life of Pets"),
    (47, "Batman Begins"),
    (48, "Forrest Gump"),
    (49, "John Wick"),
    (50, "Pulp Fiction"),
    (51, "Up"),
    (52, "The Accountant"),
    (53, "Get Out"),
    (54, "WALL\u00b7E"),
    (55, "Harry Potter and the Sorcerer's Stone"),
    (56, "Edge of Tomorrow"),
    (57, "The Hunger Games"),
    (58, "The Shawshank Redemption"),
    (59, "Kong: Skull Island"),
    (60, "The Lion King"),
    (61, "Baby Driver"),
    (62, "Big Hero 6"),
    (63, "Star Wars: Episode V - The Empire Strikes Back"),
    (64, "The Hobbit: An Unexpected Journey"),
    (65, "Back to the Future"),
    (66, "Avengers: Infinity War"),
    (67, "Iron Man 3"),
    (68, "The Incredibles"),
    (69, "Monsters, Inc."),
    (70, "Iron Man 2"),
    (71, "Despicable Me"),
    (72, "Hacksaw Ridge"),
    (73, "Toy Story"),
    (74, "Finding Dory"),
    (75, "Captain America: The First Avenger"),
    (76, "Now You See Me"),
    (77, "Black Panther"),
    (78, "Beauty and the Beast"),
    (79, "Thor"),
    (80, "X-Men: Days of Future Past"),
    (81, "The Hunger Games: Catching Fire"),
    (82, "The Revenant"),
    (83, "Harry Potter and the Chamber of Secrets"),
    (84, "Star Wars: Episode VI - Return of the Jedi"),
    (85, "Harry Potter and the Prisoner of Azkaban"),
    (86, "Star Trek Beyond"),
    (87, "Inglourious Basterds"),
    (88, "Thor: The Dark World"),
    (89, "Harry Potter and the Deathly Hallows: Part 2"),
    (90, "The Boss Baby"),
    (91, "Sing"),
    (92, "Gone Girl"),
    (93, "Harry Potter and the Goblet of Fire"),
    (94, "Pirates of the Caribbean: The Curse of the Black Pearl"),
    (95, "Split"),
    (96, "Gravity"),
    (97, "Jason Bourne"),
    (98, "Now You See Me 2"),
    (99, "Ghost in the Shell"),
    (100, "Seven"),
    (101, "The Imitation Game"),
    (102, "Ex Machina"),
    (103, "Harry Potter and the Order of the Phoenix"),
    (104, "Justice League"),
    (105, "The Hobbit: The Desolation of Smaug"),
    (106, "V for Vendetta"),
    (107, "Wreck-It Ralph"),
    (108, "Deadpool 2"),
    (109, "Man of Steel"),
    (110, "Harry Potter and the Half-Blood Prince"),
    (111, "The Magnificent Seven"),
    (112, "Gladiator"),
    (113, "Harry Potter and the Deathly Hallows: Part 1"),
    (114, "The Mummy"),
    (115, "Jumanji: Welcome to the Jungle"),
    (116, "Hidden Figures"),
    (117, "Trolls"),
    (118, "Jurassic Park"),
    (119, "The Fate of the Furious"),
    (120, "Star Wars: Episode VIII - The Last Jedi"),
    (121, "How to Train Your Dragon"),
    (122, "Central Intelligence"),
    (123, "Despicable Me 2"),
    (124, "Jack Reacher: Never Go Back"),
    (125, "The Hateful Eight"),
    (126, "Lucy"),
    (127, "Toy Story 3"),
    (128, "World War Z"),
    (129, "The Maze Runner"),
    (130, "The Jungle Book"),
    (131, "Kingsman: The Golden Circle"),
    (132, "Independence Day: Resurgence"),
    (133, "Dunkirk"),
    (134, "Shutter Island"),
    (135, "The Hangover"),
    (136, "Assassin's Creed"),
    (137, "Star Wars: Episode I - The Phantom Menace"),
    (138, "Miss Peregrine's Home for Peculiar Children"),
    (139, "Star Trek Into Darkness"),
    (140, "10 Cloverfield Lane"),
    (141, "Minions"),
    (142, "The Prestige"),
    (143, "Ratatouille"),
    (144, "Shrek"),
    (145, "Tangled"),
    (146, "Pacific Rim"),
    (147, "Star Trek"),
    (148, "Alien: Covenant"),
    (149, "Star Wars: Episode III - Revenge of the Sith"),
    (150, "Ready Player One"),
    (151, "The Amazing Spider-Man"),
    (152, "X-Men: First Class"),
    (153, "Pirates of the Caribbean: Dead Men Tell No Tales"),
    (154, "The Lego Movie"),
    (155, "Ghostbusters"),
    (156, "Home Alone"),
    (157, "Bad Moms"),
    (158, "Divergent"),
    (159, "Blade Runner 2049"),
    (160, "The Silence of the Lambs"),
    (161, "Sherlock Holmes"),
    (162, "The Great Wall"),
    (163, "War for the Planet of the Apes"),
    (164, "Blade Runner"),
    (165, "Sully"),
    (166, "Die Hard"),
    (167, "Warcraft"),
    (168, "Star Wars: Episode II - Attack of the Clones"),
    (169, "The Godfather"),
    (170, "The Hitman's Bodyguard"),
    (171, "Brave"),
    (172, "300"),
    (173, "Cars"),
    (174, "Kill Bill: Vol. 1"),
    (175, "Life"),
    (176, "The Green Mile"),
    (177, "Titanic"),
    (178, "Alien"),
    (179, "Sicario"),
    (180, "Inferno"),
    (181, "Coco"),
    (182, "The Nice Guys"),
    (183, "Skyfall"),
    (184, "War Dogs"),
    (185, "Prometheus"),
    (186, "La La Land"),
    (187, "Toy Story 2"),
    (188, "It"),
    (189, "Memento"),
    (190, "Raiders of the Lost Ark"),
    (191, "The Legend of Tarzan"),
    (192, "The Departed"),
    (193, "The Hunger Games: Mockingjay - Part 1"),
    (194, "Saving Private Ryan"),
    (195, "King Arthur: Legend of the Sword"),
    (196, "The Hobbit: The Battle of the Five Armies"),
    (197, "Deepwater Horizon"),
    (198, "The Bourne Identity"),
    (199, "The Fifth Element"),
    (200, "L\u00e9on: The Professional"),
    (201, "Sausage Party"),
    (202, "Zombieland"),
    (203, "Back to the Future Part II"),
    (204, "Spectre"),
    (205, "The Hunger Games: Mockingjay - Part 2"),
    (206, "Aladdin"),
    # rank 207 is also "Aladdin" – skip per spec
    (208, "The Grand Budapest Hotel"),
    (209, "Monsters University"),
    (210, "Dawn of the Planet of the Apes"),
    (211, "Baywatch"),
    (212, "Terminator 2: Judgment Day"),
    (213, "Pirates of the Caribbean: Dead Man's Chest"),
    (214, "Oblivion"),
    (215, "Catch Me If You Can"),
    (216, "Beauty and the Beast"),
    (217, "The Matrix Reloaded"),
    (218, "I Am Legend"),
    (219, "The Truman Show"),
    (220, "Looper"),
    (221, "Ted"),
    (222, "X-Men"),
    (223, "Men in Black"),
    (224, "Kick-Ass"),
    (225, "The Lego Batman Movie"),
    (226, "Whiplash"),
    (227, "Her"),
    (228, "The Amazing Spider-Man 2"),
    (229, "Transformers: The Last Knight"),
    (230, "The Girl on the Train"),
    (231, "The Shape of Water"),
    (232, "American History X"),
    (233, "Limitless"),
    (234, "Mission: Impossible - Rogue Nation"),
    (235, "Why Him?"),
    (236, "The Wolverine"),
    (237, "Maleficent"),
    (238, "London Has Fallen"),
    (239, "Three Billboards Outside Ebbing, Missouri"),
    (240, "The Intouchables"),
    (241, "Ocean's Eleven"),
    (242, "xXx: Return of Xander Cage"),
    (243, "Rise of the Planet of the Apes"),
    (244, "Spider-Man"),
    (245, "Valerian and the City of a Thousand Planets"),
    (246, "Pirates of the Caribbean: At World's End"),
    (247, "Elysium"),
    (248, "21 Jump Street"),
    (249, "Transformers"),
    (250, "American Sniper"),
]

OUTPUT_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "data",
    "trakt_popularity_calibration.json",
)


MIN_VOTES = 50


def _base_queryset():
    """Return the base queryset of backfilled movies with ratings."""
    return Item.objects.filter(
        media_type=MediaTypes.MOVIE.value,
        trakt_popularity_fetched_at__isnull=False,
        trakt_rating__isnull=False,
    )


def _best_candidate(qs):
    """
    From a queryset of candidates, pick the one with the highest vote count.

    Returns None if the best match has fewer than MIN_VOTES votes (likely a
    wrong match).
    """
    item = qs.order_by("-trakt_rating_count").first()
    if item is None:
        return None
    if (item.trakt_rating_count or 0) < MIN_VOTES:
        return None
    return item


def _find_item(title: str):
    """
    Try to match *title* against the DB using exact matching only.

    Strategy 1: exact case-insensitive match on the title.
    Strategy 2: if the title starts with "The ", try without it; if it doesn't,
                try prepending "The ".

    No substring/contains fallback — returns None on a miss.
    """
    base = _base_queryset()

    # Strategy 1: exact match
    item = _best_candidate(base.filter(title__iexact=title))
    if item:
        return item

    # Strategy 1b: WALL·E alternate spelling
    if title == "WALL\u00b7E":
        item = _best_candidate(base.filter(title__iexact="WALL-E"))
        if item:
            return item

    # Strategy 1c: Léon alternate (no accent)
    if title == "L\u00e9on: The Professional":
        item = _best_candidate(base.filter(title__iexact="Leon: The Professional"))
        if item:
            return item

    # Strategy 2: "The " article swap
    if title.lower().startswith("the "):
        alt = title[4:]
    else:
        alt = "The " + title

    item = _best_candidate(base.filter(title__iexact=alt))
    if item:
        return item

    return None


class Command(BaseCommand):
    """Generate a Trakt calibration fixture from real DB data."""

    help = "Match 250 known movie titles against backfilled DB items and write a calibration fixture"

    def handle(self, *_args, **_options):
        os.makedirs(os.path.dirname(os.path.abspath(OUTPUT_PATH)), exist_ok=True)

        found_items = []
        found_count = 0
        # Track titles already matched to avoid re-using the same DB row for
        # the duplicate "Aladdin" entries.
        matched_ids: set[int] = set()

        for rank, title in TITLES:
            item = _find_item(title)

            # Avoid re-using the same DB item for duplicate titles (e.g. Aladdin)
            if item and item.pk in matched_ids:
                item = None

            if item:
                matched_ids.add(item.pk)
                found_count += 1
                rating = float(item.trakt_rating) if item.trakt_rating is not None else None
                votes = item.trakt_rating_count or 0
                self.stdout.write(
                    f"\u2713 {rank:3}. {title} "
                    f"(rating={rating}, votes={votes})"
                )
                found_items.append(
                    {
                        "title": title,
                        "rating": rating,
                        "votes": votes,
                        "expected_rank": rank,
                    }
                )
            else:
                self.stdout.write(
                    self.style.WARNING(f"\u2717 {rank:3}. {title}")
                )

        total = len(TITLES)
        self.stdout.write(f"\nFound: {found_count}/{total}")

        fixture = {
            "name": "trakt-popularity-real-data",
            "description": (
                f"Generated from real Trakt API data ({found_count} of {total} matched)"
            ),
            "parameters": {
                "prior_mean": 70.0,
                "prior_votes": 25000.0,
                "vote_offset": 10.0,
                "vote_exponent": 2.0,
            },
            "items": found_items,
        }

        abs_output = os.path.abspath(OUTPUT_PATH)
        with open(abs_output, "w", encoding="utf-8") as fh:
            json.dump(fixture, fh, indent=2, ensure_ascii=False)

        self.stdout.write(
            self.style.SUCCESS(f"Fixture written to {abs_output}")
        )
