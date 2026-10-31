"""Typed Pydantic models for the person-agnostic Profile (Brief v11 Phase 2).

An immutable, hashable Profile object representing candidate identity, level policies,
role families, resume bases, location policies, scoring weights, and profile file paths.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class Person(BaseModel):
    model_config = ConfigDict(frozen=True)

    display_name: str = "the candidate"
    degree: str = "B.Tech"
    field: str = "Computer Science and Engineering (AI & ML)"
    institution: str = "University"
    graduation_year: int = 2027
    career_stage: Literal["student", "new_grad", "early_career"] = "student"


class LevelPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_required_years: int = 2
    years_penalty_per_year: float = 6.0
    years_penalty_cap: float = 12.0
    junior_boost: float = 6.0
    internships_wanted: bool = True
    graduate_programmes_wanted: bool = True
    senior_words: tuple[str, ...] = (
        "senior", "sr", "lead", "principal", "staff", "head", "director", "vp", "chief", "manager", "architect"
    )
    excluded_roles: tuple[str, ...] = (
        "java", ".net", "dotnet", "php", "c#", "c++", "embedded", "firmware", "hardware",
        "salesforce", "sap", "abap", "mainframe", "cobol", "qa", "quality assurance",
        "test engineer", "automation engineer", "manual tester", "support", "it support",
        "desktop support", "helpdesk", "technician", "network engineer", "system admin", "sysadmin",
        "sales", "marketing", "business development", "recruiter", "talent acquisition",
    )
    exclude_levels: tuple[str, ...] = ("senior",)
    maybe_levels: tuple[str, ...] = ("mid",)
    exempt_levels: tuple[str, ...] = ("intern", "new_grad")


class RoleFamily(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    keywords: tuple[str, ...] = ()
    title_patterns: tuple[str, ...] = ()
    negative_keywords: tuple[str, ...] = ()
    embedding_query: str = ""
    recipe: str = ""
    weight: float = 1.0
    score_adjustment: float = 0.0


class BaseResume(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    tex_path: str
    optional: bool = False


class LocationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    home_country: str = "India"
    home_cities: tuple[str, ...] = (
        "Bengaluru", "Bangalore", "Hyderabad", "Pune", "Chennai", "Mumbai", "Delhi", "Noida", "Gurugram"
    )
    accepted_regions: tuple[str, ...] = ("india", "remote_open_to_india")
    remote_scope_rules: tuple[str, ...] = ("worldwide", "anywhere", "apac", "asia", "india")
    workplace_types: tuple[str, ...] = ("remote", "hybrid", "onsite")
    relocation: bool = False
    work_authorization_required: tuple[str, ...] = (
        "us_work_authorization", "citizenship_required", "security_clearance"
    )


class ScoringPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    weight_fit: float = 0.55
    weight_similarity: float = 0.25
    weight_location: float = 0.20
    caps: dict[str, float] = Field(default_factory=dict)
    eligibility_blockers: tuple[str, ...] = (
        "phd", "masters", "security_clearance", "us_work_authorization", "citizenship_required"
    )


class ProfilePaths(BaseModel):
    model_config = ConfigDict(frozen=True)

    facts: str = "data/profile/facts.yaml"
    summaries: str = "data/profile/variants.yaml"
    application_answers: str = "data/profile/application_answers.yaml"
    company_rules: str = "data/profile/company_rules.yaml"
    learned_rules: str = "data/profile/learned_rules.yaml"
    writing_samples: str = "data/profile/writing_samples.md"


class Profile(BaseModel):
    """The root person-agnostic Profile model."""
    model_config = ConfigDict(frozen=True)

    person: Person = Field(default_factory=Person)
    level_policy: LevelPolicy = Field(default_factory=LevelPolicy)
    role_families: tuple[RoleFamily, ...] = Field(default_factory=tuple)
    bases: tuple[BaseResume, ...] = Field(default_factory=tuple)
    location_policy: LocationPolicy = Field(default_factory=LocationPolicy)
    scoring: ScoringPolicy = Field(default_factory=ScoringPolicy)
    paths: ProfilePaths = Field(default_factory=ProfilePaths)
    candidate_blurb: str = ""

    @property
    def content_hash(self) -> str:
        """Stable SHA-256 hash of the profile contents."""
        data = self.model_dump(mode="json")
        encoded = json.dumps(data, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]

    @property
    def authoritative_base(self) -> str:
        """The primary/authoritative base resume id."""
        for b in self.bases:
            if not b.optional:
                return b.id
        return self.bases[0].id if self.bases else "ai"

    def get_role_family(self, family_id: str) -> RoleFamily | None:
        for rf in self.role_families:
            if rf.id == family_id:
                return rf
        return None

    def render_scoring_blurb(self) -> str:
        if self.candidate_blurb:
            return self.candidate_blurb
        # Canonical legacy blurb
        return (
            f"The candidate is a fresh graduate (class of {self.person.graduation_year}) "
            f"in {self.location_policy.home_country}, looking for AI/ML engineering and "
            f"full-stack / backend software engineering roles. You receive two resume profiles "
            f'(they never contain contact details): "ai" is authoritative and up to date, "fullstack" is older.'
        )

    def render_classify_blurb(self) -> str:
        return (
            f"You triage job postings for one candidate: a fresh graduate (class of {self.person.graduation_year}) "
            f"in {self.location_policy.home_country} targeting AI/ML engineering and "
            f"full-stack / backend software engineering roles."
        )

    def render_triage_blurb(self) -> str:
        return (
            f"You triage job postings for one candidate: a fresh graduate (class of {self.person.graduation_year}) "
            f"in {self.location_policy.home_country} who can take internships, entry-level and new-grad roles."
        )


__all__ = [
    "BaseResume",
    "LevelPolicy",
    "LocationPolicy",
    "Person",
    "Profile",
    "ProfilePaths",
    "RoleFamily",
    "ScoringPolicy",
]
