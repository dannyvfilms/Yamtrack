from django import forms
from django.db.models import Q
from django_select2 import forms as s2forms

from lists.models import CustomList


class CollaboratorsWidget(s2forms.ModelSelect2MultipleWidget):
    """Custom widget for selecting multiple users."""

    search_fields = ["username__icontains"]


class TagsField(forms.MultipleChoiceField):
    """Allow arbitrary tags while still supporting select2 choices."""

    def valid_value(self, value):
        return True


class CustomListForm(forms.ModelForm):
    """Form for creating new custom lists."""

    is_public = forms.BooleanField(
        required=False,
        label="Public (read-only access)",
        help_text="Anyone with the link can view this list",
    )
    is_smart = forms.BooleanField(
        required=False,
        label="Smart List",
        help_text="Automatically updates based on media types and filters",
    )
    tags = TagsField(
        required=False,
        label="List Tags",
        help_text="Group lists on your public profile",
        widget=s2forms.Select2TagWidget(
            attrs={
                "data-minimum-input-length": 1,
                "data-placeholder": "Start typing to add tags...",
                "data-allow-clear": "false",
            },
        ),
    )

    class Meta:
        """Bind form to model."""

        model = CustomList
        fields = [
            "name",
            "description",
            "tags",
            "collaborators",
            "is_public",
            "allow_recommendations",
            "is_smart",
        ]
        widgets = {
            "collaborators": CollaboratorsWidget(
                attrs={
                    "data-minimum-input-length": 1,
                    "data-placeholder": "Search users to add...",
                    "data-allow-clear": "false",
                },
            ),
        }

    def __init__(self, *args, **kwargs):
        """Initialize form and map visibility to a public toggle."""
        self.user = kwargs.pop("user", None)
        available_tags = kwargs.pop("available_tags", None)
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.initial["is_public"] = self.instance.visibility == "public"
            self.initial["tags"] = self._normalize_tags(self.instance.tags)
            self.initial["is_smart"] = self.instance.is_smart

        existing_tags = []
        if available_tags is not None:
            existing_tags.extend(list(available_tags))
        elif self.user:
            for custom_list in CustomList.objects.filter(
                Q(owner=self.user) | Q(collaborators=self.user),
            ).only("tags"):
                existing_tags.extend(custom_list.tags or [])

        if self.instance and self.instance.tags:
            existing_tags.extend(self.instance.tags)

        if self.data:
            field_key = self.add_prefix("tags")
            if hasattr(self.data, "getlist"):
                existing_tags.extend(self.data.getlist(field_key))
            else:
                value = self.data.get(field_key, [])
                if isinstance(value, (list, tuple)):
                    existing_tags.extend(value)
                elif value:
                    existing_tags.append(value)

        normalized_tags = self._normalize_tags(existing_tags)
        self.fields["tags"].choices = [
            (tag, tag) for tag in sorted(normalized_tags, key=str.lower)
        ]

    def save(self, commit=True):
        """Save the list with visibility mapped from the public toggle."""
        instance = super().save(commit=False)
        is_public = bool(self.cleaned_data.get("is_public"))
        instance.visibility = "public" if is_public else "private"
        if not is_public:
            instance.allow_recommendations = False

        is_smart = bool(self.cleaned_data.get("is_smart"))
        instance.is_smart = is_smart
        if not is_smart:
            instance.smart_media_types = []
            instance.smart_excluded_media_types = []
            instance.smart_filters = {}

        if commit:
            instance.save()
            self.save_m2m()
        return instance

    def clean_tags(self):
        """Normalize tags input."""
        tags = self.cleaned_data.get("tags") or []
        return self._normalize_tags(tags)

    @staticmethod
    def _normalize_tags(tags):
        cleaned = []
        seen = set()
        for tag in tags or []:
            if tag is None:
                continue
            if not isinstance(tag, str):
                tag = str(tag)
            normalized = " ".join(tag.strip().split())
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(normalized)
        return cleaned
