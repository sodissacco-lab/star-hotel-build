from django import forms
from django.forms import inlineformset_factory

from procurement.models import Requisition, RequisitionLine
from projects.models import SiteActivity, SiteDiary

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class DiaryForm(forms.ModelForm):
    class Meta:
        model = SiteDiary
        fields = ["date", "weather", "workers_present", "equipment_used", "challenges", "safety_incidents",
                  "instructions_issued", "remarks"]
        widgets = {
            "date": DATE,
            "equipment_used": forms.Textarea(attrs={"rows": 2}),
            "challenges": forms.Textarea(attrs={"rows": 2}),
            "safety_incidents": forms.Textarea(attrs={"rows": 2}),
            "instructions_issued": forms.Textarea(attrs={"rows": 2}),
            "remarks": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, project, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project

    def clean_date(self):
        day = self.cleaned_data["date"]
        if SiteDiary.objects.filter(project=self.project, date=day).exists():
            raise forms.ValidationError("A site diary for this date already exists.")
        return day


ActivityFormSet = inlineformset_factory(
    SiteDiary, SiteActivity, fields=["phase", "location", "description", "work_completed"],
    extra=5, can_delete=False, widgets={"work_completed": forms.Textarea(attrs={"rows": 2})},
)


class RequisitionForm(forms.ModelForm):
    class Meta:
        model = Requisition
        fields = ["needed_by", "phase", "emergency", "justification", "notes"]
        widgets = {"needed_by": DATE, "justification": forms.Textarea(attrs={"rows": 2}),
                   "notes": forms.Textarea(attrs={"rows": 2})}

    def clean(self):
        data = super().clean()
        if data.get("emergency") and not (data.get("justification") or "").strip():
            self.add_error("justification", "Emergency requisitions need a justification.")
        return data


LineFormSet = inlineformset_factory(
    Requisition, RequisitionLine, fields=["material", "quantity"], extra=5, can_delete=False,
)
