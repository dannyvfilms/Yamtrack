from django import forms
from django_select2 import forms as s2forms

from lists.models import CustomList


class CollaboratorsWidget(s2forms.ModelSelect2MultipleWidget):
    """Custom widget for selecting multiple users."""

    search_fields = ["username__icontains"]


class CustomListForm(forms.ModelForm):
    """Form for creating new custom lists."""

    class Meta:
        """Bind form to model."""

        model = CustomList
        fields = ["name", "description", "collaborators", "visibility"]
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
        """Initialize form and conditionally include visibility field."""
        super().__init__(*args, **kwargs)
        # Only show visibility field when editing (instance has pk)
        if not self.instance.pk:
            # Remove visibility from fields for creation
            if "visibility" in self.fields:
                del self.fields["visibility"]
