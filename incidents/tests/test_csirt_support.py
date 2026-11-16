from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils.translation import override

from incidents.filters import IncidentFilter
from incidents.models import Answer, Incident, IncidentWorkflow, PredefinedAnswer, QuestionOptions
from incidents.tests.test_conditional_questions import create_predefined_answer


@pytest.fixture
def csirt_support(populate_incident_db):
    incident, other_incident = populate_incident_db["incidents"]
    original = QuestionOptions.objects.get(question__reference="8")
    option = QuestionOptions.objects.create(
        question=original.question,
        report=incident.sector_regulation.workflows.first(),
        category_option=original.category_option,
        position=99,
    )
    return SimpleNamespace(
        incident=incident,
        other_incident=other_incident,
        option=option,
        yes=create_predefined_answer(option.question, "Yes, national CSIRT", 1),
        no=create_predefined_answer(option.question, "No", 2),
        users=populate_incident_db["users"],
    )


def submit_answer(incident, option, *choices, text="", status="PASS") -> Answer:
    report = IncidentWorkflow.objects.create(incident=incident, workflow=option.report, review_status="PASS")
    IncidentWorkflow.objects.filter(pk=report.pk).update(review_status=status)
    answer = Answer.objects.create(incident_workflow=report, question_options=option, answer=text)
    answer.predefined_answers.set(choices)
    return answer


@pytest.mark.django_db
def test_support_filter_matches_the_selected_predefined_answer(csirt_support):
    submit_answer(csirt_support.incident, csirt_support.option, csirt_support.yes)
    submit_answer(csirt_support.other_incident, csirt_support.option, csirt_support.no)

    filtered = IncidentFilter({"csirt_support": csirt_support.yes.pk}, queryset=Incident.objects.all())

    assert filtered.is_valid(), filtered.errors
    assert list(filtered.qs) == [csirt_support.incident]


@pytest.mark.django_db
def test_an_old_support_answer_does_not_match_after_a_new_submission(csirt_support):
    submit_answer(csirt_support.incident, csirt_support.option, csirt_support.yes)
    latest = submit_answer(csirt_support.incident, csirt_support.option, csirt_support.no)

    filtered = IncidentFilter({"csirt_support": csirt_support.yes.pk}, queryset=Incident.objects.all())

    assert not filtered.qs.exists()
    assert IncidentFilter(queryset=Incident.objects.all()).qs.get(pk=csirt_support.incident.pk).csirt_support_answer_id == latest.pk


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["UNDE", "OUT"])
def test_unsubmitted_support_answers_do_not_replace_a_submitted_answer(csirt_support, status):
    submitted = submit_answer(csirt_support.incident, csirt_support.option, csirt_support.yes)
    submit_answer(csirt_support.incident, csirt_support.option, csirt_support.no, status=status)

    filtered = IncidentFilter({"csirt_support": csirt_support.yes.pk}, queryset=Incident.objects.all())

    assert filtered.qs.get().csirt_support_answer_id == submitted.pk


@pytest.mark.django_db
def test_support_filter_preserves_the_authorized_queryset(csirt_support):
    submit_answer(csirt_support.incident, csirt_support.option, csirt_support.yes)
    submit_answer(csirt_support.other_incident, csirt_support.option, csirt_support.yes)

    filtered = IncidentFilter({"csirt_support": csirt_support.yes.pk}, queryset=Incident.objects.filter(pk=csirt_support.incident.pk))

    assert list(filtered.qs) == [csirt_support.incident]


@pytest.mark.django_db
def test_empty_support_filter_includes_unanswered_incidents(csirt_support):
    submit_answer(csirt_support.incident, csirt_support.option, csirt_support.yes)

    filtered = IncidentFilter({"csirt_support": ""}, queryset=Incident.objects.all())

    assert set(filtered.qs) == {csirt_support.incident, csirt_support.other_incident}
    assert filtered.qs.get(pk=csirt_support.other_incident.pk).csirt_support_answer_id is None


@pytest.mark.django_db
def test_support_filter_does_not_duplicate_an_incident_with_multiple_choices(csirt_support):
    submit_answer(csirt_support.incident, csirt_support.option, csirt_support.yes, csirt_support.no)

    filtered = IncidentFilter({"csirt_support": csirt_support.yes.pk}, queryset=Incident.objects.all())

    assert list(filtered.qs) == [csirt_support.incident]


@pytest.mark.django_db
def test_support_choices_are_translated_and_limited_to_reference_8(csirt_support):
    csirt_support.yes.set_current_language("fr")
    csirt_support.yes.predefined_answer = "Oui, CSIRT national"
    csirt_support.yes.save()

    with override("fr"):
        field = IncidentFilter(queryset=Incident.objects.all()).form.fields["csirt_support"]
        assert set(field.queryset) == {csirt_support.yes, csirt_support.no}
        assert field.label_from_instance(field.queryset.get(pk=csirt_support.yes.pk)) == "Oui, CSIRT national"


@pytest.mark.django_db
def test_support_filter_works_without_the_configured_question():
    incident = Incident.objects.create()

    filtered = IncidentFilter(queryset=Incident.objects.all())

    assert filtered.qs.get().csirt_support_answer_id is None
    assert list(filtered.form.fields["csirt_support"].queryset) == []
    assert filtered.qs.get() == incident


@pytest.mark.django_db
def test_dashboard_displays_the_latest_support_answer_and_escapes_details(otp_client, csirt_support):
    submit_answer(csirt_support.incident, csirt_support.option, csirt_support.no)
    latest = submit_answer(csirt_support.incident, csirt_support.option, csirt_support.yes, text="<script>alert(1)</script>")
    regulator = next(user for user in csirt_support.users if user.email == "regadmin@reg1.lu")

    response = otp_client(regulator).get(reverse("incidents"))

    assert response.status_code == 200
    incidents = {incident.pk: incident for incident in response.context["incidents"]}
    assert incidents[csirt_support.incident.pk].csirt_support_answer.pk == latest.pk
    assert incidents[csirt_support.other_incident.pk].csirt_support_answer is None
    html = response.content.decode()
    assert "Support required from national CSIRT" in html
    assert 'name="csirt_support"' in html
    assert "Yes, national CSIRT" in html
    assert "Not answered" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html


@pytest.mark.django_db
def test_dashboard_support_filter_persists_and_resets_without_widening_access(otp_client, csirt_support):
    submit_answer(csirt_support.incident, csirt_support.option, csirt_support.yes)
    submit_answer(csirt_support.other_incident, csirt_support.option, csirt_support.yes)
    operator = next(user for user in csirt_support.users if user.email == "opadmin@com1.lu")
    client = otp_client(operator)
    session = client.session
    session["company_in_use"] = csirt_support.incident.company_id
    session.save()
    url = reverse("incidents")

    response = client.get(url, {"csirt_support": csirt_support.no.pk})
    assert response.status_code == 200
    assert not response.context["incidents"]
    assert not client.get(url).context["incidents"]

    response = client.get(url, {"csirt_support": csirt_support.yes.pk})
    assert list(response.context["incidents"]) == [csirt_support.incident]

    client.get(url, {"reset": "1"})
    response = client.get(url)
    assert list(response.context["incidents"]) == [csirt_support.incident]
    assert "csirt_support" not in client.session["incidents_filter_params"]


@pytest.mark.django_db
def test_support_filter_rejects_a_choice_from_another_question(csirt_support):
    unrelated = PredefinedAnswer.objects.exclude(question__reference="8").first()

    filtered = IncidentFilter({"csirt_support": unrelated.pk}, queryset=Incident.objects.all())

    assert not filtered.is_valid()
    assert "csirt_support" in filtered.errors
