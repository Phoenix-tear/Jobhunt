"""Remote eligibility: not "is it remote?" but "who is this remote job actually open to?".

Pure, deterministic and offline (the optional LLM tie-break for ambiguous shortlisted jobs lives
in the scoring layer). Evidence is tried from most to least trustworthy:

    1. structured fields of the source payload (Ashby workplaceType, Lever workplaceType,
       Greenhouse "Workplace Type" metadata, Himalayas locationRestrictions, ...)
    2. the location text ("Remote - US", "Remote (EMEA)", "Hybrid - London", "Anywhere")
    3. the title
    4. description phrases ("must be located in the United States", "fully remote", ...)

The result never claims more than the evidence supports: a plain "Remote" with nothing else
stays `remote_eligible_india = NULL` (unclear) instead of guessing.

Spring Boot analogy: this module is a stateless `@Component` (no I/O, no clock); `refresh_remote`
is the `@Transactional` service that applies it to rows.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from jobhunt.locations import LocationParser

PARSER_VERSION = 4


class WorkplaceType(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class RemoteScope(StrEnum):
    WORLDWIDE = "worldwide"
    REGIONAL = "regional"
    COUNTRY_RESTRICTED = "country_restricted"
    UNKNOWN = "unknown"


# --- regions ----------------------------------------------------------------------------------

_EUROPE = frozenset("GB IE DE FR NL ES IT PT PL SE NO DK FI CH AT BE CZ RO HU GR UA LT LV EE BG HR RS SK SI LU IS".split())
_MIDDLE_EAST_AFRICA = frozenset("AE SA IL EG ZA NG KE QA MA GH TR".split())
_NORTH_AMERICA = frozenset("US CA MX".split())
_LATAM = frozenset("MX BR AR CL CO PE UY CR".split())

REGION_COUNTRIES: dict[str, frozenset[str]] = {
    "APAC": frozenset("IN SG JP KR CN HK TW AU NZ ID MY TH VN PH PK BD LK NP".split()),
    "EUROPE": _EUROPE,
    "EMEA": _EUROPE | _MIDDLE_EAST_AFRICA,
    "NA": _NORTH_AMERICA,
    "LATAM": _LATAM,
    "AMER": _NORTH_AMERICA | _LATAM,
}

# (compiled pattern over LOWER-CASE text, region tag)
_REGION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), tag)
    for pattern, tag in (
        (r"\bemea\b", "EMEA"),
        (r"\b(?:middle\s+east|mena|gcc)\b", "EMEA"),
        (r"\b(?:apac|apj|asia[\s-]?pacific|asia|south\s+asia|southeast\s+asia|south-east\s+asia|oceania|anz)\b", "APAC"),
        (r"\b(?:europe|european|e\.?u\.?|eea|cet|cest|eet)\b", "EUROPE"),
        (r"\bnorth\s+america\b|\bnorth\s+american\b", "NA"),
        (r"\b(?:est|edt|pst|pdt|cst|cdt|mst|mdt|eastern\s+time|pacific\s+time|central\s+time|us\s+time\s*zones?)\b", "NA"),
        (r"\b(?:latam|latin\s+america|south\s+america|central\s+america)\b", "LATAM"),
        (r"\bamericas?\b|\bamer\b", "AMER"),
    )
)

_WORLDWIDE = re.compile(
    r"\b(?:worldwide|world[\s-]?wide|globally|global|anywhere(?:\s+in\s+the\s+world)?|work\s+from\s+anywhere|"
    r"any\s+(?:country|location)|all\s+countries|around\s+the\s+(?:world|globe)|across\s+the\s+globe)\b"
)
_EXCLUSION_SPLIT = re.compile(
    r"\b(?:excluding|except(?:\s+for)?|not\s+including|outside\s+(?:of\s+)?|other\s+than|apart\s+from)\b"
)

# --- workplace words --------------------------------------------------------------------------

_REMOTE_WORD = re.compile(
    r"\b(?:remote(?:ly)?|remoto|remota|work[\s-]from[\s-]home|wfh|telecommut\w*|teletrabajo|teletrabalho|"
    r"télétravail|homeoffice)\b",
    re.IGNORECASE,
)
# In a TITLE the bare word is too ambiguous ("Remote Sensing Engineer", "Remote Patient Monitoring"):
# only "(Remote)", "- Remote", ", Remote", "Remote - ..." style uses count.
_TITLE_WORKPLACE = re.compile(
    r"(?:[(\[\-–—,|:]\s*(?:fully\s+)?(remote|hybrid|on[\s-]?site)\b)"
    r"|(?:\b(remote|hybrid|on[\s-]?site)\s*[)\]])"
    r"|(?:^\s*(remote|hybrid|on[\s-]?site)\s*[-–—,|:(]\s*\w)",
    re.IGNORECASE,
)
_ANYWHERE_LOCATION = re.compile(r"^\s*(?:anywhere|worldwide|global|work\s+from\s+anywhere)\s*$", re.IGNORECASE)
_HYBRID_WORD = re.compile(r"\bhybrid\b", re.IGNORECASE)
_HYBRID_REMOTE_ADJACENT = re.compile(r"\bhybrid[\s-]*(?:/\s*)?remote\b", re.IGNORECASE)
_ONSITE_WORD = re.compile(r"\bon[\s-]?site\b|\bin[\s-]office\b|\boffice[\s-]based\b|\bclient\s+site\b", re.IGNORECASE)

# description phrases: (pattern, workplace, strength). Phrases about the ROLE beat vague ones.
_DESCRIPTION_WORKPLACE: tuple[tuple[re.Pattern[str], WorkplaceType, int], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), workplace, strength)
    for pattern, workplace, strength in (
        # Only statements about THIS role. Company boilerplate ("we are a fully remote company",
        # "employees working remotely worldwide", "remote-first", "work from home stipend") is ignored.
        (r"\b(?:this|the)\s+(?:role|position|job|opportunity)\s+(?:is|will\s+be)\s+(?:a\s+)?(?:fully\s+|100\s*%\s+|completely\s+)?remote\b", WorkplaceType.REMOTE, 3),
        (r"\b(?:fully\s+|100\s*%\s+|completely\s+)?remote\s+(?:position|role|job|opportunity)\b", WorkplaceType.REMOTE, 2),
        (r"\b(?:this|the)\s+(?:role|position|job)\s+is\s+(?:a\s+)?hybrid\b", WorkplaceType.HYBRID, 3),
        (r"\bhybrid\s+(?:role|position|job|opportunity)\b", WorkplaceType.HYBRID, 2),
        (r"\bhybrid\s+(?:work\s+)?(?:model|schedule|arrangement)\b", WorkplaceType.HYBRID, 1),
        (r"\b\d\s*(?:-\s*\d\s*)?days?\s+(?:a|per|each)\s+week\s+(?:in|at|from)\s+(?:the|our)\s+(?:office|hub)\b", WorkplaceType.HYBRID, 2),
        (r"\b(?:this|the)\s+(?:role|position|job)\s+is\s+(?:an?\s+)?on[\s-]?site\b", WorkplaceType.ONSITE, 3),
        (r"\bon[\s-]?site\s+(?:position|role|job)\b", WorkplaceType.ONSITE, 2),
        (r"\bmust\s+(?:be\s+)?(?:able\s+to\s+)?work\s+(?:on[\s-]?site|from\s+(?:the|our)\s+office)\b", WorkplaceType.ONSITE, 2),
        # labelled fields: "Work Model: On-site", "Working Pattern: Hybrid", "Location type: Remote"
        (r"\b(?:work(?:ing)?\s+(?:model|mode|pattern|arrangement|type|style|location|setup)|location\s+type|workplace(?:\s+type)?|job\s+(?:type|location\s+type))\s*[:\-–]\s*(?:fully\s+)?remote\b", WorkplaceType.REMOTE, 3),
        (r"\b(?:work(?:ing)?\s+(?:model|mode|pattern|arrangement|type|style|location|setup)|location\s+type|workplace(?:\s+type)?|job\s+(?:type|location\s+type))\s*[:\-–]\s*hybrid\b", WorkplaceType.HYBRID, 3),
        (r"\b(?:work(?:ing)?\s+(?:model|mode|pattern|arrangement|type|style|location|setup)|location\s+type|workplace(?:\s+type)?|job\s+(?:type|location\s+type))\s*[:\-–]\s*(?:fully\s+)?(?:on[\s-]?site|in[\s-]office|in[\s-]person|office)\b", WorkplaceType.ONSITE, 3),
        # recruiter hashtags used by LinkedIn-style postings
        (r"#li-remote\b", WorkplaceType.REMOTE, 3),
        (r"#li-hybrid\b", WorkplaceType.HYBRID, 3),
        (r"#li-on-?site\b", WorkplaceType.ONSITE, 3),
        # "requires you to be onsite", "in-office presence is required", "work onsite in <city>"
        (r"\b(?:role|position|job|team)\s+requires?\s+(?:you\s+to\s+(?:be|work)\s+)?(?:fully\s+)?(?:on[\s-]?site|in[\s-]office|in[\s-]person|in\s+the\s+office)\b", WorkplaceType.ONSITE, 3),
        (r"\b(?:requires?|required|requiring)\s+(?:in[\s-]office|on[\s-]?site|in[\s-]person)\s+(?:presence|attendance|work)\b", WorkplaceType.ONSITE, 2),
        (r"\bin[\s-]office\s+presence\s+(?:is\s+)?(?:required|expected|mandatory)\b", WorkplaceType.ONSITE, 2),
        (r"\b(?:work|working)\s+on[\s-]?site\s+(?:in|at|from)\b", WorkplaceType.ONSITE, 2),
        (r"\b(?:is|be|are)\s+(?:a\s+)?(?:fully\s+)?on[\s-]?site\s+(?:role|position|job|opportunity)\b", WorkplaceType.ONSITE, 2),
        (r"\bhybrid\s+(?:work(?:ing)?\s+)?(?:set[\s-]?up|environment|policy|working|structure|week|culture|basis)\b", WorkplaceType.HYBRID, 2),
        (r"\bhybrid[,:]?\s+\d\s*(?:-\s*\d\s*)?days?\b", WorkplaceType.HYBRID, 2),
        (r"\b(?:up\s+to\s+)?\d\s*(?:remote|wfh|work[\s-]from[\s-]home)\s+days?\s+(?:a|per|each)\s+week\b", WorkplaceType.HYBRID, 2),
        (r"\b(?:the\s+)?(?:role|position|job)\s+(?:can|may)\s+be\s+(?:done|performed|worked)\s+remotely\b", WorkplaceType.REMOTE, 2),
    )
)

# "<n> days a week" next to office wording: five days is on-site, fewer is hybrid.
_DAYS_PER_WEEK = re.compile(
    r"\b(?P<days>[1-5])\s*(?:-\s*[1-5]\s*)?\+?\s*(?:days?|x)\s*(?:a|per|each|/)\s*week\b", re.IGNORECASE)
_OFFICE_WORDING = re.compile(r"\b(?:in[\s-]person|on[\s-]?site|in[\s-]office|in\s+(?:the|our)\s+office|office)\b", re.IGNORECASE)
# a negation shortly before a phrase flips it: "does not offer hybrid work", "no remote option"
_NEGATION_BEFORE = re.compile(r"(?:\bnot\b|\bno\b|n't|\bwithout\b|\bnever\b|\bnor\b)[^.]{0,40}$", re.IGNORECASE)

# description phrases that state WHO may apply. The text after the match is parsed for places.
_RESTRICTION_TRIGGERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:must|should|need\s+to|required\s+to|have\s+to)\s+(?:be\s+)?(?:currently\s+)?(?:located|based|living|residing|resident|reside|live)\s+(?:in|within)\s+(?:the\s+)?",
        r"(?:open|available|restricted|limited)\s+(?:only\s+)?to\s+(?:candidates|applicants|residents|people|individuals)\s+(?:who\s+(?:are\s+)?)?(?:located|based|living|residing|resident)?\s*(?:in|within)\s+(?:the\s+)?",
        r"(?:candidates|applicants)\s+(?:must\s+)?(?:be\s+)?(?:located|based|residing|living)\s+(?:in|within)\s+(?:the\s+)?",
        r"(?:remote|work\s+remotely|working\s+remotely)\s+(?:from|within|in|across)\s+(?:anywhere\s+in\s+)?(?:the\s+)?",
        r"(?:authorized|authorised|eligible|legally\s+permitted|permitted)\s+to\s+work\s+(?:in|within)\s+(?:the\s+)?",
        r"(?:right|authorization|authorisation|eligibility)\s+to\s+work\s+(?:in|within)\s+(?:the\s+)?",
        r"(?:hiring|hire|recruiting)\s+(?:only\s+)?(?:in|from|within)\s+(?:the\s+)?",
    )
)
_BASED_IN_PLACE = re.compile(r"\b(U\.?S\.?|UK|U\.K\.|EU|Canada|India|United\s+States|Europe)[\s-]based\b")
_WORLDWIDE_PHRASES = re.compile(
    r"\b(?:work\s+from\s+anywhere|anywhere\s+in\s+the\s+world|remote\s+(?:position\s+|role\s+)?(?:worldwide|globally)|"
    r"open\s+to\s+(?:candidates|applicants)\s+(?:worldwide|globally|from\s+anywhere)|"
    r"(?:hire|hiring)\s+(?:talent\s+)?(?:worldwide|globally|from\s+anywhere|around\s+the\s+world))\b",
    re.IGNORECASE,
)

_WORK_AUTH = re.compile(
    r"(?:must|need\s+to|required\s+to|have\s+to|should)\s+(?:be\s+)?(?:legally\s+)?(?:currently\s+)?"
    r"(?:authorized|authorised|eligible|permitted|entitled)\s+to\s+work\s+(?:in|within|for)|"
    r"\blegally\s+(?:authorized|authorised)\s+to\s+work\b|"
    r"\b(?:right|authorization|authorisation|eligibility)\s+to\s+work\s+in\b|"
    r"\b(?:no|without)\s+(?:the\s+need\s+for\s+)?(?:visa\s+)?sponsorship\b|"
    r"(?:unable|not\s+able|cannot|can't|won't|will\s+not|do\s+not|does\s+not|don't|doesn't)\s+(?:to\s+)?"
    r"(?:provide\s+|offer\s+|support\s+)?(?:visa\s+|immigration\s+|employment\s+)?sponsor\w*|"
    r"\bsponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided)\b|"
    r"\bnot\s+(?:eligible\s+for|offering)\s+(?:visa\s+)?sponsorship\b|"
    r"\bnot\s+(?:now\s+or\s+in\s+the\s+future\s+)?require\s+(?:visa\s+)?sponsorship\b",
    re.IGNORECASE,
)
_WORK_AUTH_QUICK_CHECK = ("authoriz", "authoris", "sponsor", "right to work", "eligible to work", "eligibility to work")
_WINDOW_AFTER_TRIGGER = 140
_SOURCE_RANK = {"structured": 0, "text": 1, "description": 2, "none": 3}


# --- data -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteHints:
    """What the source's own structured fields say (None / empty = the source said nothing)."""

    workplace: WorkplaceType | None = None
    restriction_texts: tuple[str, ...] = ()
    worldwide: bool | None = None            # True: the source states no location restriction
    posting_country: str | None = None       # ISO-2 country of the posting itself (not a restriction)
    extra_location_texts: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemoteInputs:
    title: str | None = None
    location_raw: str | None = None
    description: str | None = None
    is_remote_flag: bool | None = None
    hints: RemoteHints = field(default_factory=RemoteHints)


@dataclass(frozen=True)
class RemoteInfo:
    workplace_type: WorkplaceType
    remote_scope: RemoteScope
    remote_regions: tuple[str, ...]
    remote_eligible_india: bool | None
    work_auth_required: bool
    source: str                   # structured | text | description | none
    evidence: str

    @property
    def regions_json(self) -> str | None:
        return json.dumps(list(self.remote_regions)) if self.remote_regions else None


@dataclass
class _Restriction:
    countries: set[str] = field(default_factory=set)
    regions: set[str] = field(default_factory=set)
    worldwide: bool = False
    excluded_countries: set[str] = field(default_factory=set)
    excluded_regions: set[str] = field(default_factory=set)
    note: str = ""   # the phrase that produced it (description evidence only)

    @property
    def empty(self) -> bool:
        return not (self.countries or self.regions or self.worldwide)

    def merge(self, other: "_Restriction") -> None:
        self.countries |= other.countries
        self.regions |= other.regions
        self.worldwide = self.worldwide or other.worldwide
        self.excluded_countries |= other.excluded_countries
        self.excluded_regions |= other.excluded_regions


# --- structured hints per source shape --------------------------------------------------------


def workplace_from_label(label: Any) -> WorkplaceType | None:
    """Map a free-text workplace label ("Remote", "On-site", ["Hybrid", "On-site"]) to a type."""
    if isinstance(label, (list, tuple)):
        label = " ".join(str(item) for item in label if item)
    if not isinstance(label, str) or not label.strip():
        return None
    text = label.lower()
    if "hybrid" in text:
        return WorkplaceType.HYBRID
    if "remote" in text or "telecommut" in text or "work from home" in text:
        return WorkplaceType.REMOTE
    if re.search(r"on[\s-]?site|in[\s-]?office|office|client\s+site|in[\s-]?person", text):
        return WorkplaceType.ONSITE
    return None


_GREENHOUSE_WORKPLACE_FIELDS = frozenset(
    name.lower() for name in ("Workplace Type", "Location Type", "Work Arrangement", "Work Type", "Remote/Hybrid", "Work Model")
)


def extract_hints(raw: Mapping[str, Any] | None) -> RemoteHints:
    """Structured remote hints from a stored `raw_json` payload. The payload shape is detected
    from its keys (several sources, e.g. the Simplify lists, reuse the Greenhouse/Ashby shapes)."""
    if not isinstance(raw, Mapping):
        return RemoteHints()
    keys = set(raw)
    try:
        if {"workplaceType", "isRemote"} <= keys:  # Ashby
            workplace = workplace_from_label(raw.get("workplaceType"))
            if workplace is None and raw.get("isRemote") is True:
                workplace = WorkplaceType.REMOTE   # only trusted when workplaceType is absent
            address = raw.get("address") if isinstance(raw.get("address"), Mapping) else {}
            postal = address.get("postalAddress") if isinstance(address.get("postalAddress"), Mapping) else {}
            return RemoteHints(workplace=workplace, extra_location_texts=_ashby_secondary(raw),
                               posting_country=_country_name_hint(postal.get("addressCountry")))
        if {"categories", "workplaceType"} <= keys:  # Lever
            categories = raw.get("categories") if isinstance(raw.get("categories"), Mapping) else {}
            all_locations = categories.get("allLocations")
            return RemoteHints(
                workplace=workplace_from_label(raw.get("workplaceType")),
                posting_country=_iso2(raw.get("country")),
                extra_location_texts=tuple(str(item) for item in all_locations) if isinstance(all_locations, list) else (),
            )
        if {"on_site", "hybrid", "remote"} <= keys:  # Recruitee
            if raw.get("hybrid") is True:
                workplace: WorkplaceType | None = WorkplaceType.HYBRID
            elif raw.get("remote") is True:
                workplace = WorkplaceType.REMOTE
            elif raw.get("on_site") is True:
                workplace = WorkplaceType.ONSITE
            else:
                workplace = None
            return RemoteHints(workplace=workplace, posting_country=_iso2(raw.get("country_code")))
        if "telecommuting" in keys:  # Workable
            workplace = WorkplaceType.REMOTE if raw.get("telecommuting") is True else None
            return RemoteHints(workplace=workplace, posting_country=_country_name_hint(raw.get("country")))
        if "customField" in keys and isinstance(raw.get("location"), Mapping):  # SmartRecruiters
            location = raw["location"]
            if location.get("hybrid") is True:
                workplace = WorkplaceType.HYBRID
            elif location.get("remote") is True:
                workplace = WorkplaceType.REMOTE
            else:
                workplace = None
            return RemoteHints(workplace=workplace, posting_country=_iso2(location.get("country")))
        if "locationRestrictions" in keys:  # Himalayas: a remote board; an empty list = worldwide
            restrictions = tuple(str(item) for item in (raw.get("locationRestrictions") or []) if item)
            return RemoteHints(workplace=WorkplaceType.REMOTE, restriction_texts=restrictions,
                               worldwide=True if not restrictions else None)
        if "candidate_required_location" in keys:  # Remotive
            return RemoteHints(workplace=WorkplaceType.REMOTE,
                               restriction_texts=(str(raw.get("candidate_required_location") or ""),))
        if "region" in keys and "guid" in keys:  # We Work Remotely
            return RemoteHints(workplace=WorkplaceType.REMOTE, restriction_texts=(str(raw.get("region") or ""),))
        if "jobGeo" in keys:  # Jobicy
            return RemoteHints(workplace=WorkplaceType.REMOTE, restriction_texts=(str(raw.get("jobGeo") or ""),))
        if "epoch" in keys:  # RemoteOK
            location = str(raw.get("location") or "")
            return RemoteHints(workplace=WorkplaceType.REMOTE, restriction_texts=(location,) if location.strip() else ())
        if "job_types" in keys and "remote" in keys:  # Arbeitnow
            return RemoteHints(workplace=WorkplaceType.REMOTE if raw.get("remote") is True else None)
        if isinstance(raw.get("metadata"), list):  # Greenhouse
            labels = [
                item.get("value") for item in raw["metadata"]
                if isinstance(item, Mapping) and str(item.get("name", "")).lower() in _GREENHOUSE_WORKPLACE_FIELDS
            ]
            for label in labels:
                workplace = workplace_from_label(label)
                if workplace is not None:
                    return RemoteHints(workplace=workplace)
    except (AttributeError, TypeError):  # a payload shape we did not anticipate must never break a backfill
        return RemoteHints()
    return RemoteHints()


def _ashby_secondary(raw: Mapping[str, Any]) -> tuple[str, ...]:
    secondary = raw.get("secondaryLocations")
    if not isinstance(secondary, list):
        return ()
    return tuple(str(item.get("location")) for item in secondary if isinstance(item, Mapping) and item.get("location"))


def _iso2(value: Any) -> str | None:
    text = str(value or "").strip()
    return text.upper() if re.fullmatch(r"[A-Za-z]{2}", text) else None


_COUNTRY_NAME_PARSER = LocationParser()


def _country_name_hint(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return _iso2(text) or _COUNTRY_NAME_PARSER.parse(text).country


# --- the derivation ---------------------------------------------------------------------------


class RemoteDeriver:
    """Compile once, derive many. `home_country` decides what "open to me" means."""

    def __init__(self, parser: LocationParser | None = None, home_country: str = "IN") -> None:
        self._parser = parser or LocationParser()
        self._home = home_country.upper()

    # public ------------------------------------------------------------------------------------

    def derive(self, inputs: RemoteInputs) -> RemoteInfo:
        evidence: list[str] = []
        workplace, source = self._workplace(inputs, evidence)
        work_auth = self._work_auth(inputs.description)
        if work_auth:
            evidence.append("description states work-authorization / sponsorship terms")

        if workplace is not WorkplaceType.REMOTE:
            return RemoteInfo(workplace, RemoteScope.UNKNOWN, (), None, work_auth, source, "; ".join(evidence))

        restriction, restriction_source = self._restriction(inputs, evidence)
        if restriction.empty:
            return RemoteInfo(workplace, RemoteScope.UNKNOWN, (), None, work_auth, source, "; ".join(evidence))

        scope, regions = self._scope_and_regions(restriction)
        eligible = self._eligible(restriction)
        strongest = min((source, restriction_source), key=_SOURCE_RANK.__getitem__)
        return RemoteInfo(workplace, scope, regions, eligible, work_auth, strongest, "; ".join(evidence))

    # workplace type ----------------------------------------------------------------------------

    def _workplace(self, inputs: RemoteInputs, evidence: list[str]) -> tuple[WorkplaceType, str]:
        hints = inputs.hints
        if hints.workplace is not None:
            evidence.append(f"source field says {hints.workplace.value}")
            return hints.workplace, "structured"

        found = self._workplace_from_text(inputs.location_raw)
        if found is not None:
            evidence.append(f"location {inputs.location_raw!r} says {found.value}")
            return found, "text"
        found = self._workplace_from_title(inputs.title)
        if found is not None:
            evidence.append(f"title {inputs.title!r} says {found.value}")
            return found, "text"

        found_desc = self._workplace_from_description(inputs.description)
        if found_desc is not None:
            evidence.append(f"description says {found_desc[0].value}: {found_desc[1]!r}")
            return found_desc[0], "description"

        if inputs.is_remote_flag is True:
            evidence.append("stored remote flag")
            return WorkplaceType.REMOTE, "text"
        return WorkplaceType.UNKNOWN, "none"

    @staticmethod
    def _workplace_from_text(text: str | None) -> WorkplaceType | None:
        if not text or not text.strip():
            return None
        if _HYBRID_REMOTE_ADJACENT.search(text):
            return WorkplaceType.HYBRID
        if _REMOTE_WORD.search(text) or _ANYWHERE_LOCATION.match(text):
            return WorkplaceType.REMOTE
        if _HYBRID_WORD.search(text):
            return WorkplaceType.HYBRID
        if _ONSITE_WORD.search(text):
            return WorkplaceType.ONSITE
        return None

    @staticmethod
    def _workplace_from_title(title: str | None) -> WorkplaceType | None:
        if not title:
            return None
        match = _TITLE_WORKPLACE.search(title)
        if not match:
            return None
        word = next(group for group in match.groups() if group).lower().replace("-", "").replace(" ", "")
        return {"remote": WorkplaceType.REMOTE, "hybrid": WorkplaceType.HYBRID, "onsite": WorkplaceType.ONSITE}[word]

    @staticmethod
    def _workplace_from_description(description: str | None) -> tuple[WorkplaceType, str] | None:
        """(type, matched phrase) for the strongest statement about this role, earliest wins ties."""
        if not description:
            return None
        best: tuple[int, int, WorkplaceType, str] | None = None   # (-strength, position, type, phrase)
        for pattern, workplace, strength in _DESCRIPTION_WORKPLACE:
            for match in pattern.finditer(description):
                if _NEGATION_BEFORE.search(description[max(0, match.start() - 60): match.start()]):
                    continue
                candidate = (-strength, match.start(), workplace, match.group(0))
                if best is None or candidate[:2] < best[:2]:
                    best = candidate
                break
        for match in _DAYS_PER_WEEK.finditer(description):
            around = description[max(0, match.start() - 70): match.end() + 70]
            if not _OFFICE_WORDING.search(around):
                continue
            if _NEGATION_BEFORE.search(description[max(0, match.start() - 40): match.start()]):
                continue
            workplace = WorkplaceType.ONSITE if match.group("days") == "5" else WorkplaceType.HYBRID
            candidate = (-2, match.start(), workplace, match.group(0))
            if best is None or candidate[:2] < best[:2]:
                best = candidate
            break
        return (best[2], best[3]) if best else None

    # who may apply ----------------------------------------------------------------------------

    def _restriction(self, inputs: RemoteInputs, evidence: list[str]) -> tuple[_Restriction, str]:
        hints = inputs.hints

        structured = _Restriction()
        for text in hints.restriction_texts:
            structured.merge(self._restriction_from_text(text))
        if not structured.empty:
            evidence.append(f"source restriction {', '.join(hints.restriction_texts)!r}")
            return structured, "structured"
        if hints.worldwide:
            evidence.append("source lists no location restriction")
            return _Restriction(worldwide=True), "structured"

        location_texts = [inputs.location_raw or "", *hints.extra_location_texts]
        from_location = self._restriction_from_location(location_texts)
        if not from_location.empty:
            evidence.append(f"location {inputs.location_raw!r} restricts to "
                            f"{sorted(from_location.countries | from_location.regions) or 'worldwide'}")
            return from_location, "text"

        from_title = _Restriction()
        title = inputs.title or ""
        for match in _REMOTE_WORD.finditer(title):   # "Engineer - Remote (India)": look just past the word
            window = re.sub(r"[()\[\]|]", " ", title[match.start(): match.end() + 40])
            from_title.merge(self._restriction_from_text(window))
        if not from_title.empty and (from_title.countries or from_title.regions):
            evidence.append(f"title {inputs.title!r} restricts to {sorted(from_title.countries | from_title.regions)}")
            return from_title, "text"

        # Description phrases are only trusted when the location names no place of its own: a posting
        # located in "Bengaluru, India" must not be declared US-only by a boilerplate sentence.
        if not self._location_names_place(inputs.location_raw):
            from_description = self._restriction_from_description(inputs.description)
            if not from_description.empty:
                evidence.append(f"description restricts to "
                                f"{sorted(from_description.countries | from_description.regions) or 'worldwide'}: "
                                f"{from_description.note!r}")
                return from_description, "description"

        if hints.posting_country:
            evidence.append(f"posting country {hints.posting_country} (not stated as a restriction)")
        return _Restriction(), "none"

    def _restriction_from_location(self, texts: Iterable[str]) -> _Restriction:
        result = _Restriction()
        hub_restriction = _Restriction()
        for text in texts:
            for segment in re.split(r"[;|\n]+", text):
                # "EMEA only" / "US based" / "Europe region": the qualifier carries no place, drop it
                segment = re.sub(r"\s+(?:only|based|region|time\s*zones?)\s*$", "", segment, flags=re.IGNORECASE)
                if not segment.strip():
                    continue
                restriction = self._restriction_from_text(segment)
                if _REMOTE_WORD.search(segment) or _ANYWHERE_LOCATION.match(segment):
                    result.merge(restriction)
                    continue
                parsed = self._parser.parse(segment)
                country_only = parsed.country is not None and parsed.city is None   # "India", "United States"
                if country_only or self._is_region_only(segment):                   # "LATAM", "Europe"
                    result.merge(restriction)
                else:
                    hub_restriction.merge(restriction)
        if result.empty and not hub_restriction.empty:
            return _Restriction()   # "Seattle; Remote": hubs alone do not say who may apply remotely
        return result

    def _restriction_from_text(self, text: str) -> _Restriction:
        """Countries / regions / worldwide stated in a short location-like string."""
        restriction = _Restriction()
        if not text or not text.strip():
            return restriction
        positive, *negative_parts = _EXCLUSION_SPLIT.split(text)
        self._collect(positive, restriction, negative=False)
        for part in negative_parts:
            self._collect(part, restriction, negative=True)
        if restriction.countries or restriction.regions:
            restriction.worldwide = False   # "Anywhere in the US" is a restriction, not worldwide
        return restriction

    def _collect(self, text: str, restriction: _Restriction, *, negative: bool) -> None:
        parsed = self._parser.parse(text)
        lowered = text.lower()
        countries = set(parsed.all_countries)
        regions = {tag for pattern, tag in _REGION_PATTERNS if pattern.search(lowered)}
        if negative:
            restriction.excluded_countries |= countries
            restriction.excluded_regions |= regions
            return
        restriction.countries |= countries
        restriction.regions |= regions
        if not countries and not regions and _WORLDWIDE.search(lowered):
            restriction.worldwide = True

    def _restriction_from_description(self, description: str | None) -> _Restriction:
        result = _Restriction()
        if not description:
            return result
        for pattern in _RESTRICTION_TRIGGERS:
            for match in pattern.finditer(description):
                window = description[match.end(): match.end() + _WINDOW_AFTER_TRIGGER]
                window = re.split(r"[.\n;]", window, maxsplit=1)[0]
                restriction = self._restriction_from_text(window)
                if restriction.countries or restriction.regions:
                    result.merge(restriction)
                    result.note = result.note or (match.group(0) + window)[:120]
        for match in _BASED_IN_PLACE.finditer(description):
            result.merge(self._restriction_from_text(match.group(1).replace("-", " ")))
            result.note = result.note or match.group(0)
        if result.empty:
            worldwide = _WORLDWIDE_PHRASES.search(description)
            if worldwide:
                result.worldwide = True
                result.note = worldwide.group(0)
        return result

    def _location_names_place(self, text: str | None) -> bool:
        if not text or not text.strip():
            return False
        parsed = self._parser.parse(text)
        return bool(parsed.all_countries) or parsed.city is not None

    def _is_region_only(self, segment: str) -> bool:
        """"LATAM", "Europe", "APAC": a region name and nothing else."""
        remaining = segment.lower()
        matched = False
        for pattern, _ in _REGION_PATTERNS:
            remaining, count = pattern.subn(" ", remaining)
            matched = matched or count > 0
        return matched and not re.search(r"[^\W\d_]", remaining)

    # verdict ----------------------------------------------------------------------------------

    @staticmethod
    def _scope_and_regions(restriction: _Restriction) -> tuple[RemoteScope, tuple[str, ...]]:
        if restriction.regions:
            return RemoteScope.REGIONAL, tuple(sorted(restriction.countries | restriction.regions))
        if restriction.countries:
            return RemoteScope.COUNTRY_RESTRICTED, tuple(sorted(restriction.countries))
        return RemoteScope.WORLDWIDE, ()

    def _eligible(self, restriction: _Restriction) -> bool:
        home = self._home
        excluded = home in restriction.excluded_countries or any(
            home in REGION_COUNTRIES.get(tag, frozenset()) for tag in restriction.excluded_regions
        )
        if excluded:
            return False
        if restriction.worldwide:
            return True
        if home in restriction.countries:
            return True
        return any(home in REGION_COUNTRIES.get(tag, frozenset()) for tag in restriction.regions)

    @staticmethod
    def _work_auth(description: str | None) -> bool:
        if not description:
            return False
        lowered = description.lower()
        if not any(token in lowered for token in _WORK_AUTH_QUICK_CHECK):
            return False
        return _WORK_AUTH.search(description) is not None


# --- applying it to rows ----------------------------------------------------------------------

_ROW_COLUMNS = (
    "id, title, location_raw, description_text, description_hash, is_remote, raw_json, remote_basis, remote_source"
)


def remote_basis(home_country: str, description_hash: str | None, location_raw: str | None, is_remote: Any) -> str:
    """Fingerprint of everything a derivation depends on (a changed value forces a recompute)."""
    return f"v{PARSER_VERSION}|{home_country.upper()}|{description_hash or ''}|{location_raw or ''}|{is_remote if is_remote is not None else ''}"


def inputs_from_row(row: Mapping[str, Any]) -> RemoteInputs:
    raw: Mapping[str, Any] | None = None
    raw_text = row["raw_json"]
    if raw_text:
        try:
            parsed = json.loads(raw_text)
            raw = parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            raw = None
    flag = row["is_remote"]
    return RemoteInputs(
        title=row["title"],
        location_raw=row["location_raw"],
        description=row["description_text"],
        is_remote_flag=None if flag is None else bool(flag),
        hints=extract_hints(raw),
    )


@dataclass
class RefreshStats:
    examined: int = 0
    updated: int = 0
    skipped_current: int = 0
    skipped_llm: int = 0
    by_workplace: dict[str, int] = field(default_factory=dict)
    by_eligibility: dict[str, int] = field(default_factory=dict)


def refresh_remote(
    connection: sqlite3.Connection,
    deriver: RemoteDeriver,
    home_country: str,
    *,
    force: bool = False,
    only_open: bool = False,
    limit: int | None = None,
    batch_size: int = 500,
) -> RefreshStats:
    """Derive remote fields for stale rows (or every row with `force`) and store them.

    Incremental by default: a row is touched only when the inputs fingerprint changed. Rows whose
    verdict was settled by the LLM keep it unless their inputs changed (even with `force`).
    """
    stats = RefreshStats()
    where = "WHERE status = 'open'" if only_open else ""
    light = connection.execute(
        f"SELECT id, description_hash, location_raw, is_remote, remote_basis, remote_source FROM jobs {where} ORDER BY id"
    ).fetchall()

    stale_ids: list[int] = []
    for row in light:
        stats.examined += 1
        current = remote_basis(home_country, row["description_hash"], row["location_raw"], row["is_remote"])
        unchanged = row["remote_basis"] == current
        # an LLM verdict depends on the posting text, not on parser rules: a parser upgrade keeps it
        same_inputs = (row["remote_basis"] or "").split("|", 1)[-1] == current.split("|", 1)[-1]
        if (unchanged or same_inputs) and row["remote_source"] == "llm":
            stats.skipped_llm += 1
            continue
        if unchanged and not force:
            stats.skipped_current += 1
            continue
        stale_ids.append(int(row["id"]))
        if limit is not None and len(stale_ids) >= limit:
            break

    for start in range(0, len(stale_ids), batch_size):
        chunk = stale_ids[start:start + batch_size]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(f"SELECT {_ROW_COLUMNS} FROM jobs WHERE id IN ({placeholders})", chunk).fetchall()
        updates: list[tuple[Any, ...]] = []
        for row in rows:
            info = deriver.derive(inputs_from_row(row))
            basis = remote_basis(home_country, row["description_hash"], row["location_raw"], row["is_remote"])
            eligible = None if info.remote_eligible_india is None else int(info.remote_eligible_india)
            updates.append((
                info.workplace_type.value, info.remote_scope.value, info.regions_json, eligible,
                int(info.work_auth_required), info.source, info.evidence[:500], basis, row["id"],
            ))
            stats.by_workplace[info.workplace_type.value] = stats.by_workplace.get(info.workplace_type.value, 0) + 1
            key = "unknown" if eligible is None else ("yes" if eligible else "no")
            stats.by_eligibility[key] = stats.by_eligibility.get(key, 0) + 1
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(
                """UPDATE jobs SET workplace_type = ?, remote_scope = ?, remote_regions = ?,
                          remote_eligible_india = ?, work_auth_required = ?, remote_source = ?,
                          remote_evidence = ?, remote_basis = ? WHERE id = ?""",
                updates,
            )
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
        stats.updated += len(updates)
    return stats
