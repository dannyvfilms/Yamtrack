"""Management command to validate music library and compare with Plex data."""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from app.services.music_validation import (
    count_user_tracks,
    get_enrichment_status,
    get_missing_linkages,
    validate_music_library,
)

User = get_user_model()


class Command(BaseCommand):
    """Validate music library and compare with Plex data."""

    help = "Validate music library data quality and compare with Plex track counts"

    def add_arguments(self, parser):
        """Add command arguments."""
        parser.add_argument(
            "--username",
            type=str,
            help="Username to validate (defaults to first user if not specified)",
        )
        parser.add_argument(
            "--plex-count",
            type=int,
            help="Plex track count for comparison (e.g., 8332 tracks with plays)",
        )

    def handle(self, *args, **options):
        """Handle the command."""
        username = options.get("username")
        plex_count = options.get("plex_count")

        # Get user
        if username:
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                self.stdout.write(self.style.ERROR(f"User '{username}' not found."))
                return
        else:
            # Get first user as default
            user = User.objects.first()
            if not user:
                self.stdout.write(self.style.ERROR("No users found in database."))
                return
            self.stdout.write(f"Using user: {user.username}")

        self.stdout.write("\n" + "=" * 70)
        self.stdout.write("MUSIC LIBRARY VALIDATION REPORT")
        self.stdout.write("=" * 70 + "\n")

        # Run comprehensive validation
        validation = validate_music_library(user)
        track_counts = count_user_tracks(user)
        enrichment_status = get_enrichment_status(user)
        missing_linkages = get_missing_linkages(user)

        # Display track counts
        self.stdout.write("\n📊 TRACK COUNTS")
        self.stdout.write("-" * 70)
        self.stdout.write(f"Total Music Entries:      {validation['total_music_entries']:,}")
        self.stdout.write(f"Unique Tracks:            {validation['unique_tracks']:,}")
        self.stdout.write(f"Tracks with Plays:        {validation['with_plays']:,}")
        self.stdout.write(f"Tracks with Track Link:   {validation['with_track_link']:,} ({validation['percentages']['track_link']:.1f}%)")
        self.stdout.write(f"Tracks with Runtime:      {validation['with_runtime']:,} ({validation['percentages']['runtime']:.1f}%)")
        self.stdout.write(f"Tracks with MBID:         {validation['with_mbid']:,}")

        # Display Plex comparison if provided
        if plex_count:
            self.stdout.write("\n📊 PLEX COMPARISON")
            self.stdout.write("-" * 70)
            self.stdout.write(f"Plex Tracks with Plays:  {plex_count:,}")
            self.stdout.write(f"Yamtrack Unique Tracks:  {validation['unique_tracks']:,}")
            difference = validation["unique_tracks"] - plex_count
            if difference > 0:
                self.stdout.write(
                    self.style.WARNING(
                        f"Difference:                +{difference:,} (Yamtrack has more tracks)",
                    ),
                )
            elif difference < 0:
                self.stdout.write(
                    self.style.WARNING(
                        f"Difference:                {difference:,} (Yamtrack has fewer tracks)",
                    ),
                )
            else:
                self.stdout.write(self.style.SUCCESS("Difference:                0 (Perfect match!)"))

        # Display artist statistics
        self.stdout.write("\n👤 ARTIST STATISTICS")
        self.stdout.write("-" * 70)
        self.stdout.write(f"Total Artists:            {validation['artists']['total']:,}")
        self.stdout.write(f"Artists with MBID:        {validation['artists']['with_mbid']:,} ({validation['percentages']['artist_mbid']:.1f}%)")
        self.stdout.write(f"Artists Missing MBID:     {validation['artists']['missing_mbid']:,}")
        if validation["artists"]["missing_mbid"] > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"  → {validation['artists']['missing_mbid']} artists need MBID matching",
                ),
            )

        # Display album statistics
        self.stdout.write("\n💿 ALBUM STATISTICS")
        self.stdout.write("-" * 70)
        self.stdout.write(f"Total Albums:             {validation['albums']['total']:,}")
        self.stdout.write(f"Albums with Tracks:       {validation['albums']['with_tracks_populated']:,} ({validation['percentages']['album_tracks']:.1f}%)")
        self.stdout.write(f"Albums Missing Tracks:    {validation['albums']['missing_tracks']:,}")
        if validation["albums"]["missing_tracks"] > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"  → {validation['albums']['missing_tracks']} albums need track population",
                ),
            )

        # Display missing linkages
        self.stdout.write("\n🔗 MISSING LINKAGES")
        self.stdout.write("-" * 70)
        self.stdout.write(f"Music entries missing Track link:  {missing_linkages['missing_track_link']:,}")
        self.stdout.write(f"Music entries missing Artist link: {missing_linkages['missing_artist_link']:,}")
        self.stdout.write(f"Music entries missing Album link:  {missing_linkages['missing_album_link']:,}")
        self.stdout.write(f"Music entries missing Runtime:     {missing_linkages['missing_runtime']:,}")
        self.stdout.write(f"Music entries missing multiple:    {missing_linkages['missing_multiple']:,}")

        # Display enrichment status details
        if enrichment_status["artists"]["without_mbid"] > 0:
            self.stdout.write("\n⚠️  ARTISTS WITHOUT MBID (sample)")
            self.stdout.write("-" * 70)
            for artist in enrichment_status["artists"]["without_mbid_list"][:10]:
                self.stdout.write(f"  • {artist['name']} (ID: {artist['id']})")

        if enrichment_status["albums"]["without_tracks"] > 0:
            self.stdout.write("\n⚠️  ALBUMS WITHOUT TRACKS (sample)")
            self.stdout.write("-" * 70)
            for album in enrichment_status["albums"]["without_tracks_list"][:10]:
                self.stdout.write(f"  • {album['title']} - {album['artist']} (ID: {album['id']})")

        # Summary and recommendations
        self.stdout.write("\n" + "=" * 70)
        self.stdout.write("SUMMARY & RECOMMENDATIONS")
        self.stdout.write("=" * 70)

        issues = []
        if validation["artists"]["missing_mbid"] > 0:
            issues.append(f"• {validation['artists']['missing_mbid']} artists need MBID matching")
        if validation["albums"]["missing_tracks"] > 0:
            issues.append(f"• {validation['albums']['missing_tracks']} albums need track population")
        if missing_linkages["missing_track_link"] > 0:
            issues.append(f"• {missing_linkages['missing_track_link']} Music entries need Track links")
        if missing_linkages["missing_runtime"] > 0:
            issues.append(f"• {missing_linkages['missing_runtime']} Music entries need runtime data")

        if issues:
            self.stdout.write("\n⚠️  ISSUES FOUND:")
            for issue in issues:
                self.stdout.write(self.style.WARNING(f"  {issue}"))
            self.stdout.write("\n💡 RECOMMENDATIONS:")
            self.stdout.write(
                "  • Run enrichment task: This will match artist MBIDs, populate album tracks, "
                "and link Music entries to Tracks",
            )
            if missing_linkages["missing_track_link"] > 0 or missing_linkages["missing_runtime"] > 0:
                self.stdout.write(
                    "  • Run cleanup utilities: Use link_music_to_tracks() and backfill_music_runtimes() "
                    "to fix missing links and runtimes",
                )
            self.stdout.write(
                "  • Re-import from Plex: The new import workflow will track unique tracks properly "
                "and run enrichment automatically",
            )
        else:
            self.stdout.write(self.style.SUCCESS("\n✅ No issues found! Your music library is in good shape."))

        self.stdout.write("\n" + "=" * 70)

