from django.conf import settings
from django.db import models
from django.db.models import Prefetch, Q

from app.models import Item


class CustomListManager(models.Manager):
    """Manager for custom lists."""

    def get_user_lists(self, user):
        """Return the custom lists that the user owns or collaborates on."""
        return (
            self.filter(Q(owner=user) | Q(collaborators=user))
            .select_related("owner")
            .prefetch_related(
                "collaborators",
                Prefetch(
                    "items",
                    queryset=Item.objects.order_by("-customlistitem__date_added"),
                ),
                Prefetch(
                    "customlistitem_set",
                    queryset=CustomListItem.objects.order_by("-date_added"),
                ),
            )
            .distinct()
        )

    def get_user_lists_with_item(self, user, item):
        """Return user lists with item membership status."""
        return (
            self.filter(Q(owner=user) | Q(collaborators=user))
            .annotate(
                has_item=models.Exists(
                    CustomListItem.objects.filter(
                        custom_list_id=models.OuterRef("id"),
                        item=item,
                    ),
                ),
            )
            .prefetch_related("collaborators")
            .distinct()
            .order_by("name")
        )

    def get_public_list(self, list_id):
        """Return a public list by ID."""
        return (
            self.filter(id=list_id, visibility="public")
            .select_related("owner")
            .prefetch_related("collaborators")
            .first()
        )


class CustomList(models.Model):
    """Model for custom lists."""

    VISIBILITY_CHOICES = [
        ("public", "Public"),
        ("private", "Private"),
    ]

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    collaborators = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="collaborated_lists",
        blank=True,
    )
    items = models.ManyToManyField(
        Item,
        related_name="custom_lists",
        blank=True,
        through="CustomListItem",
    )
    visibility = models.CharField(
        max_length=10,
        choices=VISIBILITY_CHOICES,
        default="private",
    )
    allow_recommendations = models.BooleanField(
        default=False,
        help_text="Allow anyone to recommend items to add to this list (only for public lists)",
    )

    objects = CustomListManager()

    class Meta:
        """Meta options for the model."""

        ordering = ["name"]

    def __str__(self):
        """Return the name of the custom list."""
        return self.name

    def user_can_view(self, user):
        """Check if the user can view the list."""
        # Public lists are viewable by anyone
        if self.visibility == "public":
            return True
        if not user or not user.is_authenticated:
            return False
        # Private lists are only viewable by owner or collaborators
        return self.owner == user or user in self.collaborators.all()

    def user_can_edit(self, user):
        """Check if the user can edit the list."""
        if not user or not user.is_authenticated:
            return False
        return self.owner == user or user in self.collaborators.all()

    def user_can_delete(self, user):
        """Check if the user can delete the list."""
        if not user or not user.is_authenticated:
            return False
        return self.owner == user

    @property
    def is_public(self):
        """Return whether the list is public."""
        return self.visibility == "public"

    def can_recommend(self):
        """Check if recommendations are allowed for this list."""
        return self.visibility == "public" and self.allow_recommendations

    @property
    def image(self):
        """Return the image of the first item in the list."""
        return self.items.first().image if self.items.first() else settings.IMG_NONE


class CustomListItemManager(models.Manager):
    """Manager for custom list items."""

    def get_last_added_date(self, custom_list):
        """Return the last time an item was added to a specific list."""
        try:
            return self.filter(custom_list=custom_list).latest("date_added").date_added
        except self.model.DoesNotExist:
            return None


class CustomListItem(models.Model):
    """Model for items in custom lists."""

    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    custom_list = models.ForeignKey(CustomList, on_delete=models.CASCADE)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="The user who added this item to the list",
    )
    date_added = models.DateTimeField(auto_now_add=True)

    objects = CustomListItemManager()

    class Meta:
        """Meta options for the model."""

        ordering = ["date_added"]
        constraints = [
            models.UniqueConstraint(
                fields=["item", "custom_list"],
                name="%(app_label)s_customlistitem_unique_item_list",
            ),
        ]

    def __str__(self):
        """Return the name of the list item."""
        return self.item.title


class ListRecommendation(models.Model):
    """Model for item recommendations to custom lists."""

    custom_list = models.ForeignKey(
        CustomList,
        on_delete=models.CASCADE,
        related_name="recommendations",
    )
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    recommended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="The user who recommended this item (null if anonymous)",
    )
    anonymous_name = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Display name for anonymous recommenders",
    )
    note = models.TextField(
        blank=True,
        default="",
        help_text="Optional note from the recommender explaining their recommendation",
    )
    date_recommended = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Meta options for the model."""

        ordering = ["-date_recommended"]
        constraints = [
            models.UniqueConstraint(
                fields=["item", "custom_list"],
                name="%(app_label)s_listrecommendation_unique_item_list",
            ),
        ]

    def __str__(self):
        """Return a string representation of the recommendation."""
        return f"{self.item.title} recommended for {self.custom_list.name}"

    @property
    def recommender_display_name(self):
        """Return the display name of the recommender."""
        if self.recommended_by:
            return self.recommended_by.username
        return self.anonymous_name or "Anonymous"


class ListActivityType(models.TextChoices):
    """Choices for list activity types."""

    ITEM_ADDED = "item_added", "Item Added"
    ITEM_REMOVED = "item_removed", "Item Removed"
    RECOMMENDATION_APPROVED = "recommendation_approved", "Recommendation Approved"
    RECOMMENDATION_DENIED = "recommendation_denied", "Recommendation Denied"
    LIST_CREATED = "list_created", "List Created"
    LIST_EDITED = "list_edited", "List Edited"
    COLLABORATOR_ADDED = "collaborator_added", "Collaborator Added"
    COLLABORATOR_REMOVED = "collaborator_removed", "Collaborator Removed"


class ListActivity(models.Model):
    """Model for tracking list activity history."""

    custom_list = models.ForeignKey(
        CustomList,
        on_delete=models.CASCADE,
        related_name="activities",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="The user who performed this action",
    )
    activity_type = models.CharField(
        max_length=30,
        choices=ListActivityType.choices,
    )
    item = models.ForeignKey(
        Item,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="The item involved in this activity (if applicable)",
    )
    details = models.TextField(
        blank=True,
        default="",
        help_text="Additional details about the activity",
    )
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Meta options for the model."""

        ordering = ["-timestamp"]
        verbose_name_plural = "List activities"

    def __str__(self):
        """Return a string representation of the activity."""
        return f"{self.get_activity_type_display()} - {self.custom_list.name}"

    @property
    def description(self):
        """Return a human-readable description of the activity."""
        user_name = self.user.username if self.user else "Someone"
        item_title = self.item.title if self.item else "an item"

        descriptions = {
            ListActivityType.ITEM_ADDED: f"{user_name} added {item_title}",
            ListActivityType.ITEM_REMOVED: f"{user_name} removed {item_title}",
            ListActivityType.RECOMMENDATION_APPROVED: f"{user_name} approved {item_title}",
            ListActivityType.RECOMMENDATION_DENIED: f"{user_name} denied {item_title}",
            ListActivityType.LIST_CREATED: f"{user_name} created the list",
            ListActivityType.LIST_EDITED: f"{user_name} edited the list",
            ListActivityType.COLLABORATOR_ADDED: f"{user_name} added a collaborator",
            ListActivityType.COLLABORATOR_REMOVED: f"{user_name} removed a collaborator",
        }
        base_desc = descriptions.get(self.activity_type, "Unknown activity")
        if self.details:
            return f"{base_desc}: {self.details}"
        return base_desc
