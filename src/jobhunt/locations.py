"""Location text -> (country ISO-2, city, remote flag).

Heuristic and dictionary-based on purpose (no geocoding service, no LLM). The raw text is
always stored untouched elsewhere; this module only derives structured hints from it.

Multi-location strings ("Seattle; Remote - US", "Seattle, San Francisco, New York City")
never raise and never drop data: the *first* recognizable place becomes the primary
country/city, and every country mentioned is reported in `all_countries`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --- countries ----------------------------------------------------------------------------

_COUNTRIES: dict[str, str] = {
    "united states": "US", "united states of america": "US", "america": "US",
    "canada": "CA", "united kingdom": "GB", "great britain": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "northern ireland": "GB", "india": "IN",
    "germany": "DE", "deutschland": "DE", "france": "FR", "netherlands": "NL", "holland": "NL",
    "ireland": "IE", "spain": "ES", "italy": "IT", "portugal": "PT", "poland": "PL",
    "sweden": "SE", "norway": "NO", "denmark": "DK", "finland": "FI", "switzerland": "CH",
    "austria": "AT", "belgium": "BE", "czechia": "CZ", "czech republic": "CZ", "romania": "RO",
    "hungary": "HU", "greece": "GR", "ukraine": "UA", "turkey": "TR", "turkiye": "TR",
    "türkiye": "TR", "israel": "IL", "united arab emirates": "AE", "saudi arabia": "SA",
    "egypt": "EG", "south africa": "ZA", "nigeria": "NG", "kenya": "KE", "singapore": "SG",
    "malaysia": "MY", "indonesia": "ID", "thailand": "TH", "vietnam": "VN", "viet nam": "VN",
    "philippines": "PH", "japan": "JP", "south korea": "KR", "korea": "KR", "china": "CN",
    "hong kong": "HK", "taiwan": "TW", "australia": "AU", "new zealand": "NZ", "brazil": "BR",
    "brasil": "BR", "mexico": "MX", "argentina": "AR", "chile": "CL", "colombia": "CO",
    "peru": "PE", "lithuania": "LT", "latvia": "LV", "estonia": "EE", "bulgaria": "BG",
    "croatia": "HR", "serbia": "RS", "slovakia": "SK", "slovenia": "SI", "luxembourg": "LU",
    "iceland": "IS", "pakistan": "PK", "bangladesh": "BD", "sri lanka": "LK", "nepal": "NP",
    "qatar": "QA", "morocco": "MA", "ghana": "GH", "costa rica": "CR", "uruguay": "UY",
}

# Short aliases that are only trusted when written in capitals (otherwise "us" = the pronoun).
_UPPERCASE_COUNTRY_TOKENS: dict[str, str] = {
    "USA": "US", "U.S.A.": "US", "U.S.": "US", "US": "US", "UK": "GB", "U.K.": "GB", "UAE": "AE",
}

# --- sub-national regions that imply a country --------------------------------------------

_US_MULTIWORD_STATES = (
    "new hampshire", "new jersey", "new mexico", "north carolina", "north dakota",
    "rhode island", "south carolina", "south dakota", "west virginia", "district of columbia",
)
_US_SINGLE_WORD_STATES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas",
    "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada", "ohio", "oklahoma", "oregon",
    "pennsylvania", "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "wisconsin", "wyoming",
)
_US_STATE_ABBREVIATIONS = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)
_CANADIAN_PROVINCES = (
    "ontario", "british columbia", "alberta", "quebec", "québec", "manitoba", "saskatchewan",
    "nova scotia", "new brunswick", "newfoundland",
)
_CANADIAN_ABBREVIATIONS = frozenset("ON BC AB QC MB SK NS NB NL PE".split())
_INDIAN_STATES = (
    "karnataka", "maharashtra", "telangana", "tamil nadu", "haryana", "uttar pradesh", "gujarat",
    "west bengal", "kerala", "rajasthan", "andhra pradesh", "odisha", "madhya pradesh", "punjab",
)

# --- cities: alias -> (canonical city, country) -------------------------------------------

_INDIA_CITIES: dict[str, str] = {
    "bengaluru": "Bengaluru", "bangalore": "Bengaluru", "bengalooru": "Bengaluru",
    "hyderabad": "Hyderabad", "secunderabad": "Hyderabad", "chennai": "Chennai", "madras": "Chennai",
    "pune": "Pune", "mumbai": "Mumbai", "bombay": "Mumbai", "navi mumbai": "Navi Mumbai",
    "thane": "Thane", "delhi": "Delhi", "new delhi": "Delhi", "delhi ncr": "Delhi", "delhi-ncr": "Delhi",
    "ncr": "Delhi", "national capital region": "Delhi", "gurgaon": "Gurugram", "gurugram": "Gurugram",
    "noida": "Noida", "greater noida": "Noida", "ghaziabad": "Ghaziabad", "faridabad": "Faridabad",
    "kolkata": "Kolkata", "calcutta": "Kolkata", "ahmedabad": "Ahmedabad", "gandhinagar": "Gandhinagar",
    "jaipur": "Jaipur", "kochi": "Kochi", "cochin": "Kochi", "thiruvananthapuram": "Thiruvananthapuram",
    "trivandrum": "Thiruvananthapuram", "coimbatore": "Coimbatore", "chandigarh": "Chandigarh",
    "mohali": "Mohali", "indore": "Indore", "lucknow": "Lucknow", "nagpur": "Nagpur",
    "bhubaneswar": "Bhubaneswar", "vadodara": "Vadodara", "surat": "Surat", "mysuru": "Mysuru",
    "mysore": "Mysuru", "visakhapatnam": "Visakhapatnam", "vizag": "Visakhapatnam", "patna": "Patna",
    "bhopal": "Bhopal", "kanpur": "Kanpur", "nashik": "Nashik", "madurai": "Madurai",
    "mangalore": "Mangaluru", "mangaluru": "Mangaluru", "dehradun": "Dehradun",
}

_WORLD_CITIES: dict[str, tuple[str, str]] = {
    # United States
    "san francisco": ("San Francisco", "US"), "sf": ("San Francisco", "US"), "bay area": ("San Francisco", "US"),
    "new york city": ("New York", "US"),
    "new york": ("New York", "US"), "nyc": ("New York", "US"), "seattle": ("Seattle", "US"),
    "austin": ("Austin", "US"), "boston": ("Boston", "US"), "los angeles": ("Los Angeles", "US"),
    "chicago": ("Chicago", "US"), "denver": ("Denver", "US"), "atlanta": ("Atlanta", "US"),
    "mountain view": ("Mountain View", "US"), "palo alto": ("Palo Alto", "US"),
    "sunnyvale": ("Sunnyvale", "US"), "san jose": ("San Jose", "US"), "menlo park": ("Menlo Park", "US"),
    "redmond": ("Redmond", "US"), "bellevue": ("Bellevue", "US"), "cupertino": ("Cupertino", "US"),
    "washington, dc": ("Washington", "US"), "washington dc": ("Washington", "US"),
    "san diego": ("San Diego", "US"), "portland": ("Portland", "US"), "pittsburgh": ("Pittsburgh", "US"),
    "raleigh": ("Raleigh", "US"), "dallas": ("Dallas", "US"), "houston": ("Houston", "US"),
    "miami": ("Miami", "US"), "salt lake city": ("Salt Lake City", "US"),
    # Canada
    "toronto": ("Toronto", "CA"), "vancouver": ("Vancouver", "CA"), "montreal": ("Montreal", "CA"),
    "ottawa": ("Ottawa", "CA"), "waterloo": ("Waterloo", "CA"), "calgary": ("Calgary", "CA"),
    # Europe
    "london": ("London", "GB"), "manchester": ("Manchester", "GB"), "edinburgh": ("Edinburgh", "GB"),
    "dublin": ("Dublin", "IE"), "berlin": ("Berlin", "DE"), "munich": ("Munich", "DE"),
    "hamburg": ("Hamburg", "DE"), "frankfurt": ("Frankfurt", "DE"), "paris": ("Paris", "FR"),
    "amsterdam": ("Amsterdam", "NL"), "rotterdam": ("Rotterdam", "NL"), "madrid": ("Madrid", "ES"),
    "barcelona": ("Barcelona", "ES"), "lisbon": ("Lisbon", "PT"), "zurich": ("Zurich", "CH"),
    "geneva": ("Geneva", "CH"), "stockholm": ("Stockholm", "SE"), "copenhagen": ("Copenhagen", "DK"),
    "oslo": ("Oslo", "NO"), "helsinki": ("Helsinki", "FI"), "warsaw": ("Warsaw", "PL"),
    "krakow": ("Krakow", "PL"), "prague": ("Prague", "CZ"), "vienna": ("Vienna", "AT"),
    "budapest": ("Budapest", "HU"), "bucharest": ("Bucharest", "RO"), "tallinn": ("Tallinn", "EE"),
    # Asia-Pacific / Middle East / Africa / LatAm
    "singapore": ("Singapore", "SG"), "tokyo": ("Tokyo", "JP"), "seoul": ("Seoul", "KR"),
    "shanghai": ("Shanghai", "CN"), "beijing": ("Beijing", "CN"), "shenzhen": ("Shenzhen", "CN"),
    "hong kong": ("Hong Kong", "HK"), "taipei": ("Taipei", "TW"), "kuala lumpur": ("Kuala Lumpur", "MY"),
    "jakarta": ("Jakarta", "ID"), "bangkok": ("Bangkok", "TH"), "manila": ("Manila", "PH"),
    "ho chi minh": ("Ho Chi Minh City", "VN"), "hanoi": ("Hanoi", "VN"), "sydney": ("Sydney", "AU"),
    "melbourne": ("Melbourne", "AU"), "brisbane": ("Brisbane", "AU"), "perth": ("Perth", "AU"),
    "auckland": ("Auckland", "NZ"), "dubai": ("Dubai", "AE"), "abu dhabi": ("Abu Dhabi", "AE"),
    "riyadh": ("Riyadh", "SA"), "tel aviv": ("Tel Aviv", "IL"), "cairo": ("Cairo", "EG"),
    "lagos": ("Lagos", "NG"), "nairobi": ("Nairobi", "KE"), "cape town": ("Cape Town", "ZA"),
    "johannesburg": ("Johannesburg", "ZA"), "sao paulo": ("Sao Paulo", "BR"),
    "são paulo": ("Sao Paulo", "BR"), "buenos aires": ("Buenos Aires", "AR"),
    "mexico city": ("Mexico City", "MX"), "bogota": ("Bogota", "CO"), "santiago": ("Santiago", "CL"),
}

# --- remote / workplace detection ---------------------------------------------------------

_REMOTE_PATTERN = re.compile(
    r"\b(remote|work[\s-]from[\s-]home|wfh|telecommut\w*|worldwide|anywhere|distributed)\b",
    re.IGNORECASE,
)
_ONSITE_PATTERN = re.compile(r"\b(hybrid|on[\s-]?site|in[\s-]office|office[\s-]based)\b", re.IGNORECASE)
_WORKPLACE_WORDS = re.compile(
    r"\b(remote|hybrid|on[\s-]?site|in[\s-]office|office[\s-]based|work[\s-]from[\s-]home|wfh|"
    r"telecommut\w*|worldwide|anywhere|distributed|flexible|multiple locations?)\b",
    re.IGNORECASE,
)
_STATE_ABBREVIATION = re.compile(r"(?:,|\()\s*([A-Z]{2})\b")
_UPPERCASE_TOKEN = re.compile(r"(?<![A-Za-z.])(U\.S\.A\.|U\.S\.|U\.K\.|USA|US|UK|UAE)(?![A-Za-z])")
_SEGMENT_SPLIT = re.compile(r"\s*(?:;|\||/|\band\b|\bor\b|\s[-–—]\s|,)\s*", re.IGNORECASE)
_MAX_CITY_LENGTH = 60
_MIN_CITY_LENGTH = 2
_PLACEHOLDER_LOCATIONS = frozenset(
    {"n/a", "na", "n.a.", "none", "null", "tbd", "tba", "-", "--", "?", "unknown", "not specified",
     "unspecified", "not applicable", "various", "multiple"}
)


@dataclass(frozen=True)
class ParsedLocation:
    country: str | None
    city: str | None
    is_remote: bool | None
    all_countries: tuple[str, ...] = ()


def _build_place_lookup(extra_cities: dict[str, str] | None) -> dict[str, tuple[str, str | None]]:
    """alias (lower-case) -> (country, canonical city or None)."""
    lookup: dict[str, tuple[str, str | None]] = {}
    for name, country in _COUNTRIES.items():
        lookup[name] = (country, None)
    for name in (*_US_SINGLE_WORD_STATES, *_US_MULTIWORD_STATES):
        lookup[name] = ("US", None)
    for name in _CANADIAN_PROVINCES:
        lookup[name] = ("CA", None)
    for name in _INDIAN_STATES:
        lookup[name] = ("IN", None)
    for alias, city in _INDIA_CITIES.items():
        lookup[alias] = ("IN", city)
    for alias, (city, country) in _WORLD_CITIES.items():
        lookup[alias] = (country, city)
    # User-supplied extras from config.yaml (city -> ISO-2); they win over built-ins.
    for city_name, country_code in (extra_cities or {}).items():
        lookup[city_name.lower()] = (country_code.upper(), city_name.title())
    return lookup


class LocationParser:
    """Compile the lookup once, parse many strings."""

    def __init__(self, extra_city_countries: dict[str, str] | None = None) -> None:
        self._lookup = _build_place_lookup(extra_city_countries)
        aliases = sorted(self._lookup, key=len, reverse=True)
        pattern = "|".join(re.escape(alias).replace(r"\ ", r"\s+") for alias in aliases)
        self._place_pattern = re.compile(rf"(?<![a-z0-9])({pattern})(?![a-z0-9])")

    def canonical_city(self, city: str | None) -> str | None:
        """Known alias -> canonical spelling ('bangalore' -> 'Bengaluru'); all-lowercase -> Title Case."""
        if not city or not city.strip():
            return None
        cleaned = city.strip()
        entry = self._lookup.get(re.sub(r"\s+", " ", cleaned.lower()))
        if entry and entry[1]:
            return entry[1]
        return cleaned.title() if cleaned.islower() else cleaned

    def place_strength(self, text: str | None) -> int:
        """0 = not a place, 1 = mentions a country/region inside other words ("Globex India"),
        2 = clearly a place: remote/hybrid wording, a known city, a "City, Region" list, or just a
        country/state name. Used to tell "Company" lines from "Location" lines in alert emails."""
        cleaned = (text or "").strip()
        if not cleaned or cleaned.lower().strip(" .") in _PLACEHOLDER_LOCATIONS:
            return 0
        if _REMOTE_PATTERN.search(cleaned) or _ONSITE_PATTERN.search(cleaned):
            return 2
        matches = self._find_places(cleaned)
        if not matches:
            return 0
        if any(city for _, _, city in matches):
            return 2
        if self._lookup.get(re.sub(r"\s+", " ", cleaned.lower())) is not None or cleaned.upper() in _UPPERCASE_COUNTRY_TOKENS:
            return 2
        return 2 if "," in cleaned else 1

    def parse(self, location_text: str | None, workplace_hint: str | None = None) -> ParsedLocation:
        text = (location_text or "").strip()
        if text.lower().strip(" .") in _PLACEHOLDER_LOCATIONS:
            text = ""
        is_remote = self._detect_remote(text, workplace_hint)
        if not text:
            return ParsedLocation(None, None, is_remote)

        matches = self._find_places(text)  # [(position, country, city|None)] in text order
        if not matches:
            return ParsedLocation(None, self._fallback_city(text), is_remote)

        primary_country = matches[0][1]
        city = next((city for _, country, city in matches if city and country == primary_country), None)
        if city is None:
            city = self._fallback_city(text)
        countries = tuple(dict.fromkeys(country for _, country, _ in matches))
        return ParsedLocation(primary_country, city, is_remote, countries)

    # -- internals -------------------------------------------------------------------------

    @staticmethod
    def _detect_remote(text: str, workplace_hint: str | None) -> bool | None:
        hint = (workplace_hint or "").lower().replace("_", "-")
        if hint in {"remote", "fully-remote"}:
            return True
        if _REMOTE_PATTERN.search(text):
            return True
        if hint in {"hybrid", "on-site", "onsite", "on site"} or _ONSITE_PATTERN.search(text):
            return False
        return None

    def _find_places(self, text: str) -> list[tuple[int, str, str | None]]:
        found: list[tuple[int, str, str | None]] = []
        for match in self._place_pattern.finditer(text.lower()):
            country, city = self._lookup[re.sub(r"\s+", " ", match.group(1))]
            found.append((match.start(), country, city))
        for match in _UPPERCASE_TOKEN.finditer(text):
            found.append((match.start(), _UPPERCASE_COUNTRY_TOKENS[match.group(1)], None))
        for match in _STATE_ABBREVIATION.finditer(text):
            abbreviation = match.group(1)
            if abbreviation in _US_STATE_ABBREVIATIONS:
                found.append((match.start(1), "US", None))
            elif abbreviation in _CANADIAN_ABBREVIATIONS:
                found.append((match.start(1), "CA", None))
        found.sort(key=lambda item: item[0])
        return found

    def _fallback_city(self, text: str) -> str | None:
        """First plausible segment when no known city matched ("Zurich Airport Area")."""
        cleaned = re.sub(r"\([^)]*\)", " ", text)
        cleaned = _WORKPLACE_WORDS.sub(" ", cleaned)
        for segment in _SEGMENT_SPLIT.split(cleaned):
            candidate = re.sub(r"\s+", " ", segment).strip(" -–—,;|/")
            if not candidate or not (_MIN_CITY_LENGTH <= len(candidate) <= _MAX_CITY_LENGTH):
                continue
            if not re.search(r"[^\W\d_]", candidate):  # needs at least one letter (any script)
                continue
            lowered = candidate.lower()
            if lowered in self._lookup and self._lookup[lowered][1] is None:
                continue  # that segment is a country/state, not a city
            if candidate.upper() in _UPPERCASE_COUNTRY_TOKENS or candidate in _US_STATE_ABBREVIATIONS:
                continue
            return candidate
        return None
