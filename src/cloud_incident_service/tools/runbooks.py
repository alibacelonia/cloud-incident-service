"""Runbook processing and deterministic lexical retrieval (D-1, ADR-007).

Runbooks are Markdown files with a small front matter block::

    ---
    id: payments-5xx
    services: payments-api
    updated: 2026-09-14T00:00:00Z
    ---
    # Title
    ## Section heading
    Section text...

`services` is a comma-separated list of service names, or `*` for every service. Each `##`
section becomes a searchable unit with the stable source ID `runbook:<id>:<section-slug>`.
Runbook text is untrusted data: it is indexed and returned, never interpreted.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Annotated, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from cloud_incident_service.domain.request import ServiceName

RunbookId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
ANY_SERVICE = "*"

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORD_TEXT = (
    "a an and are as at be by for from in is it of on or that the this to was were with after "
    "before when if then than into over under not no"
)
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())
# Per matched query term; weights add up, so specific sections outrank their siblings.
TITLE_WEIGHT = 1
HEADING_WEIGHT = 2
BODY_WEIGHT = 1


class RunbookFormatError(ValueError):
    """Raised when a packaged runbook does not follow the expected format."""


class RunbookSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runbook_id: RunbookId
    runbook_title: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    services: tuple[ServiceName | Annotated[str, StringConstraints(pattern=r"^\*$")], ...] = Field(
        min_length=1
    )
    updated: AwareDatetime
    slug: Slug
    heading: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    body: Annotated[str, StringConstraints(min_length=1, max_length=5_000)]

    @property
    def source_id(self) -> str:
        return f"runbook:{self.runbook_id}:{self.slug}"

    def applies_to(self, service: str) -> bool:
        return ANY_SERVICE in self.services or service in self.services


class RankedSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    section: RunbookSection
    score: int = Field(ge=1)


class RunbookRetriever(Protocol):
    data_as_of: datetime

    def search(self, service: str, query: str, limit: int) -> list[RankedSection]: ...


def tokenize(text: str) -> set[str]:
    return {
        token
        for token in _TOKEN.findall(text.lower())
        if len(token) > 1 and token not in _STOPWORDS
    }


class LexicalRunbookRetriever:
    """Scores sections by matched query terms in the runbook title, section heading, and body."""

    def __init__(self, sections: list[RunbookSection], data_as_of: datetime) -> None:
        source_ids = [section.source_id for section in sections]
        if len(source_ids) != len(set(source_ids)):
            raise RunbookFormatError("runbook section source IDs must be unique")
        self.data_as_of = data_as_of
        self._index = [
            (
                section,
                tokenize(section.runbook_title),
                tokenize(section.heading),
                tokenize(section.body),
            )
            for section in sections
        ]

    def search(self, service: str, query: str, limit: int) -> list[RankedSection]:
        terms = tokenize(query)
        ranked = []
        for section, title_terms, heading_terms, body_terms in self._index:
            if not section.applies_to(service):
                continue
            score = sum(
                TITLE_WEIGHT * (term in title_terms)
                + HEADING_WEIGHT * (term in heading_terms)
                + BODY_WEIGHT * (term in body_terms)
                for term in terms
            )
            if score:
                ranked.append(RankedSection(section=section, score=score))
        # Highest score first; service-specific runbooks before generic ones; then source ID.
        ranked.sort(
            key=lambda item: (
                -item.score,
                ANY_SERVICE in item.section.services,
                item.section.source_id,
            )
        )
        return ranked[:limit]


def parse_runbook(text: str) -> list[RunbookSection]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise RunbookFormatError("runbook must start with a front matter block")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        raise RunbookFormatError("front matter block is not closed") from None

    metadata = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if not separator:
            raise RunbookFormatError(f"invalid front matter line: {line[:40]!r}")
        metadata[key.strip()] = value.strip()
    missing = {"id", "services", "updated"} - metadata.keys()
    if missing:
        raise RunbookFormatError(f"front matter is missing: {', '.join(sorted(missing))}")

    title = None
    sections: list[tuple[str, list[str]]] = []
    for line in lines[end + 1 :]:
        if line.startswith("# ") and title is None:
            title = line[2:].strip()
        elif line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
    if not title or not sections:
        raise RunbookFormatError(f"runbook {metadata['id']!r} needs a title and sections")

    services = tuple(service.strip() for service in metadata["services"].split(","))
    parsed = [
        RunbookSection(
            runbook_id=metadata["id"],
            runbook_title=title,
            services=services,
            updated=metadata["updated"],
            slug=_slugify(heading),
            heading=heading,
            body=" ".join(" ".join(body).split()),
        )
        for heading, body in sections
    ]
    slugs = [section.slug for section in parsed]
    if len(slugs) != len(set(slugs)):
        raise RunbookFormatError(f"runbook {metadata['id']!r} has duplicate section headings")
    return parsed


def load_runbooks(directory: Traversable | None = None) -> LexicalRunbookRetriever:
    """Load every `*.md` runbook in sorted order. The manifest supplies `data_as_of`."""
    directory = directory or files("cloud_incident_service.fixtures").joinpath("runbooks")
    manifest = json.loads(directory.joinpath("manifest.json").read_text(encoding="utf-8"))
    sections = []
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if entry.name.endswith(".md"):
            sections.extend(parse_runbook(entry.read_text(encoding="utf-8")))
    data_as_of = datetime.fromisoformat(manifest["data_as_of"])
    return LexicalRunbookRetriever(sections, data_as_of)


def _slugify(heading: str) -> str:
    return "-".join(_TOKEN.findall(heading.lower()))[:64] or "section"
