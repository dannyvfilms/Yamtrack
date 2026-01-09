from django import forms
from django_select2 import forms as s2forms

from lists.models import CustomList


class CollaboratorsWidget(s2forms.ModelSelect2MultipleWidget):
    """Custom widget for selecting multiple users."""

    search_fields = ["username__icontains"]


class CustomListForm(forms.ModelForm):
    """Form for creating new custom lists."""

    is_public = forms.BooleanField(
        required=False,
        label="Public (read-only access)",
        help_text="Anyone with the link can view this list",
    )

    class Meta:
        """Bind form to model."""

        model = CustomList
        fields = [
            "name",
            "description",
            "collaborators",
            "is_public",
            "allow_recommendations",
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
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.initial["is_public"] = self.instance.visibility == "public"

    def save(self, commit=True):
        """Save the list with visibility mapped from the public toggle."""
        instance = super().save(commit=False)
        is_public = bool(self.cleaned_data.get("is_public"))
        instance.visibility = "public" if is_public else "private"
        if not is_public:
            instance.allow_recommendations = False
        if commit:
            instance.save()
            self.save_m2m()
        return instance
