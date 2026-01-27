"""Management command to diagnose broken lists."""

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

from lists.models import CustomList

User = get_user_model()


class Command(BaseCommand):
    help = "Diagnose broken or invalid lists"

    def handle(self, *args, **options):
        self.stdout.write("Checking for broken lists...\n")

        # Check all lists
        all_lists = CustomList.objects.all()
        self.stdout.write(f"Total lists in database: {all_lists.count()}\n")

        # Check for lists with issues
        issues = []

        for custom_list in all_lists:
            list_issues = []

            # Check if owner exists
            try:
                owner = custom_list.owner
                if not owner:
                    list_issues.append("Missing owner")
            except Exception as e:
                list_issues.append(f"Owner error: {e}")

            # Check if list can be accessed via get_user_lists
            try:
                if owner:
                    user_lists = CustomList.objects.get_user_lists(owner)
                    if custom_list not in user_lists:
                        list_issues.append("Not returned by get_user_lists for owner")
            except Exception as e:
                list_issues.append(f"get_user_lists error: {e}")

            # Check if list can be accessed directly
            try:
                direct_list = CustomList.objects.get(id=custom_list.id)
                if direct_list != custom_list:
                    list_issues.append("Direct get returns different object")
            except CustomList.DoesNotExist:
                list_issues.append("Direct get raises DoesNotExist")
            except Exception as e:
                list_issues.append(f"Direct get error: {e}")

            if list_issues:
                issues.append({
                    "list": custom_list,
                    "issues": list_issues,
                })

        if issues:
            self.stdout.write(self.style.ERROR(f"\nFound {len(issues)} lists with issues:\n"))
            for issue in issues:
                custom_list = issue["list"]
                self.stdout.write(
                    f"  List ID {custom_list.id}: {custom_list.name} "
                    f"(Owner: {custom_list.owner.username if custom_list.owner else 'MISSING'}, "
                    f"Source: {custom_list.source})"
                )
                for problem in issue["issues"]:
                    self.stdout.write(f"    - {problem}")
        else:
            self.stdout.write(self.style.SUCCESS("\nNo broken lists found!"))

        # Check for specific list ID 4
        self.stdout.write("\n" + "=" * 50)
        self.stdout.write("Checking for list ID 4 specifically:\n")
        try:
            list_4 = CustomList.objects.get(id=4)
            self.stdout.write(f"List 4 EXISTS:")
            self.stdout.write(f"  Name: {list_4.name}")
            self.stdout.write(f"  Owner: {list_4.owner.username if list_4.owner else 'MISSING'}")
            self.stdout.write(f"  Source: {list_4.source}")
            self.stdout.write(f"  Visibility: {list_4.visibility}")
            
            # Check if it's accessible via get_user_lists
            if list_4.owner:
                user_lists = CustomList.objects.get_user_lists(list_4.owner)
                if list_4 in user_lists:
                    self.stdout.write(self.style.SUCCESS("  ✓ Accessible via get_user_lists"))
                else:
                    self.stdout.write(self.style.ERROR("  ✗ NOT accessible via get_user_lists"))
        except CustomList.DoesNotExist:
            self.stdout.write(self.style.ERROR("List 4 does NOT exist in database"))
