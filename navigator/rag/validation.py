"""Check a draft against the supplied snapshot, then request a model assessment.

No retrieval, generation, repair, or publication happens here. ComponentError is
left to the caller (including model failures); retries belong to orchestration.
"""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from pydantic import ValidationError

from navigator.interfaces import ComponentError, LLMClient
from navigator.schemas import (
    Draft, ErrorDetail, Fragment, Verification, VerificationRequest,
)


def _invalid(message: str) -> ComponentError:
    return ComponentError(ErrorDetail(code="invalid_model_output", message=message))


def validate_draft(
    draft: Draft | Mapping[str, object] | str,
    fragments: Sequence[Fragment],
) -> Draft:
    """Return a fresh structurally valid draft with exact, snapshot-bound quotes.

    This checks evidence presence, not whether a quote entails the claim. Source
    readiness/publication is the caller's responsibility. Whitespace, case and
    punctuation in quotes are never normalized.
    """
    try:
        if isinstance(draft, str):
            checked = Draft.model_validate_json(draft, strict=True)
        else:
            # Revalidate even existing models: assignment/model_construct can
            # bypass Pydantic checks, including inside nested evidence.
            payload = draft.model_dump(mode="json") if isinstance(draft, Draft) else draft
            checked = Draft.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise _invalid("Некорректная структура ответа.") from exc

    by_id = {fragment.id: fragment for fragment in fragments}
    if len(by_id) != len(fragments):
        # An ambiguous caller-supplied snapshot is an input error, not an LLM error.
        raise ValueError("Повтор fragment_id в переданных источниках")
    for group in ("summary", "steps"):
        for index, claim in enumerate(getattr(checked, group)):
            for evidence in claim.evidence:
                fragment = by_id.get(evidence.fragment_id)
                if fragment is None:
                    raise _invalid(f"{group}[{index}]: fragment_id отсутствует в источниках.")
                if not evidence.quote.strip() or evidence.quote not in fragment.text:
                    raise _invalid(f"{group}[{index}]: цитата отсутствует в указанном фрагменте.")
    if checked.status == "not_found" and (checked.summary or checked.steps):
        raise _invalid("Статус not_found не допускает утверждений.")
    return checked


@dataclass(frozen=True)
class AnswerValidation:
    """Both stages completed; model_assessment is fallible and is not proof.

    checked_draft passed only structural/quote checks. In particular a model
    assessment with complete=False must not be published as a full procedure.
    This object does not assign an HTTP status or authorize publication.
    """

    checked_draft: Draft
    model_assessment: Verification


async def validate_answer(
    draft: Draft | Mapping[str, object] | str,
    fragments: Sequence[Fragment],
    *,
    question: str,
    client: LLMClient,
    as_of: date | None = None,
) -> AnswerValidation:
    """Run deterministic checks before one sequential call to the existing client.

    The caller supplies the same client used for generation and serializes other
    requests to it. No retry, fallback or machine/profile switch is attempted.
    Client adapters can use prompts.build_verification_messages for this request.
    """
    # Isolate the checked snapshot from caller mutation during the async call.
    snapshot = [fragment.model_copy(deep=True) for fragment in fragments]
    checked = validate_draft(draft, snapshot)
    request = VerificationRequest(
        question=question, as_of=as_of, fragments=snapshot,
        draft=checked.model_copy(deep=True),
    )
    assessment = await client.verify(request)
    try:
        # Do not accept constructed/mutated models or coerced boolean strings.
        payload = assessment.model_dump(mode="json") if isinstance(assessment, Verification) else assessment
        assessment = Verification.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise _invalid("Некорректная структура смысловой оценки модели.") from exc
    return AnswerValidation(checked_draft=checked, model_assessment=assessment)
