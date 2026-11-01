from django import forms

from documents.models import Attachment


class AttachmentForm(forms.ModelForm):
    class Meta:
        model = Attachment
        fields = ["kind", "caption", "file", "diary", "purchase_order"]
        widgets = {"file": forms.ClearableFileInput(attrs={"accept": "image/*,application/pdf"})}

    def __init__(self, *args, project, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["diary"].queryset = project.diaries.all()
        self.fields["purchase_order"].queryset = project.purchase_orders.all()
