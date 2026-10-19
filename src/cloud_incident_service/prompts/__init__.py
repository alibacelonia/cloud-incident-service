"""Versioned prompt assets."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from string import Template


@dataclass(frozen=True)
class PromptTemplate:
    version: str
    template: Template

    def render(self, **values: object) -> str:
        return self.template.substitute(**values)


def load_prompt(version: str) -> PromptTemplate:
    """Load `prompts/<version>.txt`; the version is recorded in report provenance and traces."""
    text = files("cloud_incident_service.prompts").joinpath(f"{version}.txt").read_text("utf-8")
    return PromptTemplate(version=version, template=Template(text.strip()))
