"""Classification, prefilter, graduation-year hint, India-city mapping and remote detection."""

from __future__ import annotations

import pytest

from jobhunt.classify import Classifier, TitlePrefilter, normalize_for_matching
from jobhunt.config import Config
from jobhunt.locations import LocationParser
from jobhunt.models import EmploymentType as ET
from jobhunt.models import ExperienceLevel as XL
from jobhunt.normalize import enrich_job
from tests.helpers import make_job


@pytest.fixture(scope="module")
def classifier(config: Config) -> Classifier:
    return Classifier.from_config(config)


@pytest.fixture(scope="module")
def prefilter(config: Config) -> TitlePrefilter:
    return TitlePrefilter.from_config(config)


@pytest.fixture(scope="module")
def parser(config: Config) -> LocationParser:
    return LocationParser(config.classification.extra_city_countries)


class TestExperienceLevel:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Software Engineer Intern", XL.INTERN),
            ("Summer 2027 Internship - Backend", XL.INTERN),
            ("Software Engineering Co-op", XL.INTERN),
            ("Software Engineer, New Grad", XL.NEW_GRAD),
            ("University Graduate - Machine Learning", XL.NEW_GRAD),
            ("Graduate Engineer Trainee (GET)", XL.NEW_GRAD),
            ("Entry-Level Data Analyst", XL.ENTRY),
            ("Junior Backend Developer", XL.ENTRY),
            ("Associate Software Engineer", XL.ENTRY),
            ("SDE I", XL.ENTRY),
            ("Software Engineer I", XL.ENTRY),
            ("Software Engineer II", XL.MID),
            ("SDE-2", XL.MID),
            ("Senior Software Engineer", XL.SENIOR),
            ("Sr. Data Scientist", XL.SENIOR),
            ("Staff Engineer, Infrastructure", XL.SENIOR),
            ("Principal ML Engineer", XL.SENIOR),
            ("Engineering Manager", XL.SENIOR),
            ("Senior Associate Engineer", XL.SENIOR),   # senior beats associate
            ("Software Engineer", XL.UNKNOWN),
        ],
    )
    def test_title_rules(self, classifier: Classifier, title: str, expected: XL) -> None:
        assert classifier.experience_level(title) is expected

    def test_member_of_technical_staff_is_not_senior(self, classifier: Classifier) -> None:
        assert classifier.experience_level("Member of Technical Staff") is XL.UNKNOWN

    @pytest.mark.parametrize(
        ("description", "expected"),
        [
            ("We are hiring recent graduates to join our team.", XL.NEW_GRAD),
            ("This is an entry-level role.", XL.ENTRY),
            ("Requires 0-2 years of experience in Python.", XL.ENTRY),
            ("Minimum 5+ years of professional experience.", XL.SENIOR),
            ("3-5 years of experience with distributed systems.", XL.MID),
            ("Our company was founded 10 years ago and we love dogs.", XL.UNKNOWN),
            ("", XL.UNKNOWN),
        ],
    )
    def test_description_fallback_when_title_is_silent(
        self, classifier: Classifier, description: str, expected: XL
    ) -> None:
        assert classifier.experience_level("Software Engineer", description) is expected

    def test_title_wins_over_description(self, classifier: Classifier) -> None:
        assert classifier.experience_level("Senior Engineer", "entry level friendly, 0-1 years") is XL.SENIOR


class TestEmploymentType:
    def test_intern_title(self, classifier: Classifier) -> None:
        assert classifier.employment_type("Data Science Intern") is ET.INTERNSHIP

    def test_source_hint_is_used(self, classifier: Classifier) -> None:
        assert classifier.employment_type("Engineer", hint="Part time") is ET.PART_TIME
        assert classifier.employment_type("Engineer", hint="fulltime_permanent") is ET.FULL_TIME
        assert classifier.employment_type("Engineer", hint="Contractor") is ET.CONTRACT

    def test_description_head_only(self, classifier: Classifier) -> None:
        assert classifier.employment_type("Engineer", "Full-time position based in Pune.") is ET.FULL_TIME
        late_mention = ("x " * 600) + "this is a part-time role"
        assert classifier.employment_type("Engineer", late_mention) is ET.UNKNOWN

    def test_unknown_by_default(self, classifier: Classifier) -> None:
        assert classifier.employment_type("Engineer") is ET.UNKNOWN


class TestGraduationYearHint:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Open to the class of 2027", 2027),
            ("For candidates graduating in May 2027", 2027),
            ("2027 New Grad Software Engineer", 2027),
            ("Graduates of 2026 welcome", 2026),
            ("Batch of 2027 pass-outs", 2027),
            ("2027 passouts only", 2027),
            ("Founded in 2015, graduate degree preferred", None),   # outside the plausible window
            ("Class of 2020 alumni", None),
            ("No year here", None),
        ],
    )
    def test_patterns(self, text: str, expected: int | None) -> None:
        assert Classifier.graduation_year_hint(text) == expected

    def test_title_is_checked_before_description(self) -> None:
        assert Classifier.graduation_year_hint("Class of 2027 Engineer", "graduating 2028") == 2027
        assert Classifier.graduation_year_hint(None, None) is None


class TestPrefilter:
    @pytest.mark.parametrize(
        "title",
        [
            "Software Engineer", "Backend Developer", "Full Stack Engineer", "SDE I", "Data Scientist",
            "Machine Learning Engineer", "AI Research Intern", "New Grad Software Engineer",
            "Member of Technical Staff", "Graduate Engineer Trainee", "ML Engineer (Remote)",
            "Software Engineer - Class of 2027",
        ],
    )
    def test_keeps(self, prefilter: TitlePrefilter, title: str) -> None:
        assert prefilter.keeps(title), title

    @pytest.mark.parametrize(
        "title",
        [
            "Senior Software Engineer", "Staff Backend Engineer", "Principal Engineer", "Director of Engineering",
            "VP Engineering", "Head of Data", "Engineering Manager", "Account Executive", "Sales Engineer",
            "Technical Recruiter", "Marketing Manager", "Legal Counsel", "Office Administrator",
            "Mechanical Engineer", "Barista",  # not tech and not matching include at all
        ],
    )
    def test_rejects(self, prefilter: TitlePrefilter, title: str) -> None:
        assert not prefilter.keeps(title), title

    def test_empty_or_missing_title_is_rejected(self, prefilter: TitlePrefilter) -> None:
        assert not prefilter.keeps(None)
        assert not prefilter.keeps("   ")

    def test_empty_include_list_keeps_everything_not_excluded(self, config: Config) -> None:
        permissive = TitlePrefilter([], config.prefilter)
        assert permissive.keeps("Chef de partie")
        assert not permissive.keeps("Senior Chef")

    def test_matching_is_whole_word(self, prefilter: TitlePrefilter) -> None:
        assert not prefilter.keeps("Receptionist")          # "ai"/"ml" must not match inside words
        assert prefilter.keeps("Embedded Software Developer")

    def test_normalization_treats_separators_as_spaces(self) -> None:
        assert normalize_for_matching("Entry-Level/Junior_Dev") == "entry level junior dev"


class TestLocations:
    @pytest.mark.parametrize(
        ("raw", "country", "city"),
        [
            ("Bengaluru, Karnataka, India", "IN", "Bengaluru"),
            ("Bangalore", "IN", "Bengaluru"),
            ("Hyderabad", "IN", "Hyderabad"),
            ("Chennai, Tamil Nadu", "IN", "Chennai"),
            ("Pune, MH", "IN", "Pune"),
            ("Mumbai / Navi Mumbai", "IN", "Mumbai"),
            ("Gurgaon, Haryana", "IN", "Gurugram"),
            ("Noida, UP", "IN", "Noida"),
            ("Delhi NCR", "IN", "Delhi"),
            ("New Delhi", "IN", "Delhi"),
            ("Kolkata", "IN", "Kolkata"),
            ("Ahmedabad", "IN", "Ahmedabad"),
            ("India", "IN", None),
            ("San Francisco, CA", "US", "San Francisco"),
            ("Austin, TX, United States", "US", "Austin"),
            ("New York, NY", "US", "New York"),
            ("Seattle, WA", "US", "Seattle"),
            ("Toronto, ON", "CA", "Toronto"),
            ("London, UK", "GB", "London"),
            ("Berlin, Germany", "DE", "Berlin"),
            ("Singapore", "SG", "Singapore"),
            ("Brussels, Brussels, Belgium", "BE", "Brussels"),
            ("Zurich", "CH", "Zurich"),
            ("Winterthur", None, "Winterthur"),   # unknown city: keep the text, no country
        ],
    )
    def test_country_and_city(self, parser: LocationParser, raw: str, country: str | None, city: str | None) -> None:
        parsed = parser.parse(raw)
        assert (parsed.country, parsed.city) == (country, city)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Remote", True),
            ("Remote - India", True),
            ("Anywhere in the world", True),
            ("Work from home", True),
            ("Hybrid - Bengaluru", False),
            ("On-site, Pune", False),
            ("Bengaluru", None),
            ("", None),
            (None, None),
        ],
    )
    def test_remote_flag(self, parser: LocationParser, raw: str | None, expected: bool | None) -> None:
        assert parser.parse(raw).is_remote is expected

    def test_remote_hint_from_source_beats_text(self, parser: LocationParser) -> None:
        assert parser.parse("Bengaluru", workplace_hint="remote").is_remote is True
        assert parser.parse("Bengaluru", workplace_hint="hybrid").is_remote is False

    def test_remote_with_country_keeps_both(self, parser: LocationParser) -> None:
        parsed = parser.parse("Remote (India)")
        assert (parsed.country, parsed.is_remote) == ("IN", True)

    def test_multi_location_never_drops_countries_or_crashes(self, parser: LocationParser) -> None:
        parsed = parser.parse("Seattle, WA; Bengaluru, India; Remote - Canada")
        assert parsed.country == "US" and parsed.city == "Seattle" and parsed.is_remote is True
        assert set(parsed.all_countries) == {"US", "IN", "CA"}

    def test_greenhouse_comma_joined_city_list(self, parser: LocationParser) -> None:
        parsed = parser.parse("Seattle, San Francisco, New York City")
        assert (parsed.country, parsed.city) == ("US", "Seattle")

    def test_garbage_and_symbols_do_not_crash(self, parser: LocationParser) -> None:
        for raw in ["???", "—", "()", "a" * 5000, "<script>alert(1)</script>", "\n\t"]:
            parser.parse(raw)

    def test_pronoun_us_is_not_a_country(self, parser: LocationParser) -> None:
        assert parser.parse("Join us in the office").country is None
        assert parser.parse("Remote - US").country == "US"

    @pytest.mark.parametrize("placeholder", ["N/A", "n/a", "TBD", "-", "Unspecified", " . "])
    def test_placeholder_locations_yield_nothing(self, parser: LocationParser, placeholder: str) -> None:
        parsed = parser.parse(placeholder)
        assert (parsed.country, parsed.city, parsed.is_remote) == (None, None, None)

    @pytest.mark.parametrize(
        ("city", "expected"),
        [("bangalore", "Bengaluru"), ("pune", "Pune"), ("Gurgaon", "Gurugram"), ("zagreb", "Zagreb"),
         ("McLean", "McLean"), ("  ", None), (None, None)],
    )
    def test_canonical_city(self, parser: LocationParser, city: str | None, expected: str | None) -> None:
        assert parser.canonical_city(city) == expected

    def test_extra_cities_from_config(self) -> None:
        custom = LocationParser({"Kozhikode": "IN"})
        parsed = custom.parse("Kozhikode")
        assert (parsed.country, parsed.city) == ("IN", "Kozhikode")


class TestEnrichJob:
    def test_fills_location_and_classification(self, classifier: Classifier, parser: LocationParser) -> None:
        job = make_job(
            title="Software Engineer Intern", location_raw="Hyderabad, India",
            description_text="Open to the class of 2027. Full-time conversion possible.",
        )
        enrich_job(job, parser, classifier)
        assert (job.country, job.city) == ("IN", "Hyderabad")
        assert job.employment_type is ET.INTERNSHIP
        assert job.experience_level is XL.INTERN
        assert job.graduation_year_hint == 2027

    def test_source_stated_values_are_not_overwritten(self, classifier: Classifier, parser: LocationParser) -> None:
        job = make_job(title="Engineer", location_raw="London", country="IE", is_remote=False,
                       employment_type=ET.CONTRACT)
        enrich_job(job, parser, classifier)
        assert job.country == "IE" and job.is_remote is False and job.employment_type is ET.CONTRACT

    def test_source_city_spelling_is_canonicalized(self, classifier: Classifier, parser: LocationParser) -> None:
        job = make_job(title="Engineer", location_raw="bangalore", city="bangalore", country="IN")
        enrich_job(job, parser, classifier)
        assert job.city == "Bengaluru"

    def test_intern_level_forces_internship_type(self, classifier: Classifier, parser: LocationParser) -> None:
        job = make_job(title="Placement Engineer", employment_type=ET.FULL_TIME, experience_level=XL.INTERN)
        enrich_job(job, parser, classifier)
        assert job.employment_type is ET.INTERNSHIP

    def test_internship_hint_implies_intern_level(self, classifier: Classifier, parser: LocationParser) -> None:
        job = make_job(title="Backend Engineer", employment_type=ET.INTERNSHIP)
        enrich_job(job, parser, classifier)
        assert job.experience_level is XL.INTERN
