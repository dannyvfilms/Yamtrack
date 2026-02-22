"""Management command to remove duplicate book-author relationships."""

from django.core.management.base import BaseCommand
from django.db.models import Count

from app.models import Book


class Command(BaseCommand):
    help = "Remove duplicate book-author relationships from the database"

    def handle(self, *args, **options):
        """Remove duplicate author-book relationships."""
        # Find all books
        duplicates_removed = 0
        
        for book in Book.objects.all():
            # Get all authors for this book and count them
            author_counts = (
                book.authors.values("id")
                .annotate(count=Count("id"))
                .filter(count__gt=1)
            )
            
            # If there are duplicates, remove them
            for author_data in author_counts:
                author_id = author_data["id"]
                # Get all relationships and keep only one
                relationships = book.authors.through.objects.filter(
                    book_id=book.id,
                    bookauthor_id=author_id,
                )
                # Delete all but the first one
                for rel in relationships[1:]:
                    rel.delete()
                    duplicates_removed += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Removed duplicate: Book={book.id}, Author={author_id}",
                        ),
                    )
        
        self.stdout.write(
            self.style.SUCCESS(
                f"\nTotal duplicate relationships removed: {duplicates_removed}",
            ),
        )
