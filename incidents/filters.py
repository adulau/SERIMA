import django_filters
from django.db.models import F, OuterRef, Q, QuerySet, Subquery
from django.db.models.functions import Coalesce
from django.utils.translation import gettext_lazy as _

from governanceplatform.helpers import get_sectors_grouped, normalize_reference
from governanceplatform.models import Sector

from .forms import DropdownCheckboxSelectMultiple
from .globals import REFERENCE_PREFIX
from .models import Answer, Incident, PredefinedAnswer, QuestionOptionsHistory, SectorRegulation


# define specific query to get the regulation
def sector_regulation(request):
    return SectorRegulation.objects.distinct()


def reference_matches(value: str) -> Q:
    """Match the reference as typed and as Crockford reads it, so a reference
    transcribed with I for 1 or O for 0 is still found without breaking the search on
    the legacy references that contain those letters."""
    lookup = Q(incident_id__icontains=value)
    normalized = normalize_reference(value, REFERENCE_PREFIX)
    if normalized != value.upper():
        lookup |= Q(incident_id__icontains=normalized)
    return lookup


class IncidentFilter(django_filters.FilterSet):
    incident_id = django_filters.CharFilter(method="filter_reference")
    affected_sectors = django_filters.MultipleChoiceFilter(widget=DropdownCheckboxSelectMultiple(), label=_("Sectors"))
    sector_regulation = django_filters.ModelChoiceFilter(queryset=sector_regulation)
    csirt_support = django_filters.ModelChoiceFilter(
        queryset=PredefinedAnswer.objects.filter(question__reference="8").prefetch_related("translations").order_by("position", "pk"),
        method="filter_csirt_support",
        label=_("Support required from national CSIRT"),
    )

    search = django_filters.CharFilter(method="filter_search", label=_("Search"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        grouped_choices = get_sectors_grouped(Sector.objects.all())
        self.filters["affected_sectors"].field.choices = grouped_choices
        # Snapshots store the pre-edit state, so the first later edit identifies the answered question.
        question_at_answer_time = QuestionOptionsHistory.objects.filter(
            questionoptions=OuterRef("question_options_id"), timestamp__gte=OuterRef("timestamp")
        ).order_by("timestamp", "pk")
        latest_support_answer = (
            Answer.objects.filter(incident_workflow__incident=OuterRef("pk"))
            .annotate(
                question_reference=Coalesce(
                    Subquery(question_at_answer_time.values("question__reference")[:1]), F("question_options__question__reference")
                )
            )
            .filter(question_reference="8")
            .exclude(incident_workflow__review_status__in=["UNDE", "OUT"])
            .order_by("-incident_workflow__timestamp", "-incident_workflow_id", "-timestamp", "-pk")
        )
        self.queryset = self.queryset.annotate(csirt_support_answer_id=Subquery(latest_support_answer.values("pk")[:1]))

    class Meta:
        model = Incident
        fields = [
            "incident_id",
            "incident_status",
            "is_significative_impact",
            "affected_sectors",
            "sector_regulation",
        ]

    def filter_reference(self, queryset, name, value):
        return queryset.filter(reference_matches(value))

    def filter_csirt_support(self, queryset: QuerySet[Incident], name: str, value: PredefinedAnswer) -> QuerySet[Incident]:
        return queryset.filter(csirt_support_answer_id__in=Answer.objects.filter(predefined_answers=value).values("pk"))

    def filter_search(self, queryset, name, value):
        return queryset.filter(
            reference_matches(value)
            | Q(contact_firstname__icontains=value)
            | Q(contact_lastname__icontains=value)
            | Q(technical_firstname__icontains=value)
            | Q(technical_lastname__icontains=value)
            | Q(company_name__icontains=value)
            | Q(company__identifier__icontains=value)
            | Q(company__name__icontains=value)
            | Q(regulator__translations__name__icontains=value)
            | Q(regulator__translations__full_name__icontains=value)
            | Q(sector_regulation__regulation__translations__label__icontains=value)
            | Q(affected_sectors__translations__name__icontains=value)
        ).distinct()
